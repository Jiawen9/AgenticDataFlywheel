import { activeBatchItems, publishedBatch } from '@/utils/batchLifecycle'
import { computed, ref } from 'vue'
import { newRunRequestId, type FactoryBatchDetail, type FactoryBatchSummary, type FactoryState, type TaskRow, type RunOptions } from '@/phoneFactoryApi'

interface BatchApi {
  list(): Promise<FactoryBatchSummary[]>
  detail(id: string): Promise<FactoryBatchDetail>
  workbook(id: string): Promise<Blob>
}
interface FactoryApi {
  addTask(description: string, filename: string, contentBase64: string, sourceBatchId?: string): Promise<FactoryState>
  remoteStartRun(options: RunOptions): Promise<{ ok: boolean; message?: string; error?: string }>
  state(): Promise<FactoryState>
}

async function blobToBase64(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer())
  const chunks: string[] = []
  for (let offset = 0; offset < bytes.length; offset += 8192) chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + 8192)))
  return btoa(chunks.join(''))
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  try { link.click() }
  finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 0) }
}

export function usePhoneCollectionBatches(batchApi: BatchApi, factoryApi: FactoryApi, options: {
  setTasks: (tasks: TaskRow[]) => void
  isBlocked?: () => boolean
  downloadFile?: (blob: Blob, filename: string) => void
  runOptions?: () => Pick<RunOptions, 'vla' | 'run_mode' | 'config'>
}) {
  const retiredIds = new Set<string>()
  const batches = ref<FactoryBatchSummary[]>([])
  const selectedBatchId = ref('')
  const selectedBatch = ref<FactoryBatchDetail | null>(null)
  const loadingBatches = ref(false)
  const loadingBatch = ref(false)
  const busy = ref(false)
  const downloading = ref(false)
  const error = ref('')
  let selectionRevision = 0
  let listRevision = 0
  let disposed = false
  const pendingRequests = new Map<string, string>()
  const blocked = computed(() => busy.value || Boolean(options.isBlocked?.()))

  async function selectBatch(id: string): Promise<boolean> {
    if (publishedBatch(id) || retiredIds.has(id)) { retireBatches([id]); return false }
    if (disposed || blocked.value) return false
    const revision = ++selectionRevision
    selectedBatchId.value = id
    selectedBatch.value = null
    error.value = ''
    loadingBatch.value = Boolean(id)
    if (!id) return true
    try {
      const batch = await batchApi.detail(id)
      if (disposed || revision !== selectionRevision) return false
      selectedBatch.value = batch
      return true
    } catch (cause) {
      if (!disposed && revision === selectionRevision) error.value = (cause as Error).message
      return false
    } finally {
      if (!disposed && revision === selectionRevision) loadingBatch.value = false
    }
  }

  async function loadBatches(preferredId?: string) {
    if (disposed || blocked.value) return
    const revision = ++listRevision, selection = selectionRevision
    loadingBatches.value = true
    error.value = ''
    try {
      const rows = await batchApi.list()
      if (disposed || revision !== listRevision) return
      batches.value = activeBatchItems(rows).filter(batch => !retiredIds.has(batch.batch_id))
      if (preferredId && selection === selectionRevision) await selectBatch(preferredId)
    } catch (cause) {
      if (!disposed && revision === listRevision) error.value = (cause as Error).message
    } finally {
      if (!disposed && revision === listRevision) loadingBatches.value = false
    }
  }

  async function runBatch(batchId: string, phoneId = '', app = '', overrides?: Pick<RunOptions, 'vla' | 'run_mode' | 'config'>) {
    if (publishedBatch(batchId) || retiredIds.has(batchId)) throw new Error('该批次已发布，处理已结束')
    if (disposed || busy.value) throw new Error('正在处理采集任务，请稍候')
    busy.value = true
    const supplied = overrides || options.runOptions?.() || { vla: '' }
    const runOptions = { ...supplied, ...(supplied.config ? { config: { ...supplied.config } } : {}) }
    const revision = selectionRevision
    const followsSelection = selectedBatchId.value === batchId
    const isCurrent = () => !disposed && (!followsSelection || revision === selectionRevision) && !retiredIds.has(batchId) && !publishedBatch(batchId)
    const ensureCurrent = () => {
      if (!isCurrent()) throw new Error('批次选择已变化或已发布，本次未继续下发')
    }
    try {
      const batch = selectedBatch.value?.batch_id === batchId ? selectedBatch.value : await batchApi.detail(batchId)
      ensureCurrent()
      const workbook = await batchApi.workbook(batchId)
      ensureCurrent()
      const content = await blobToBase64(workbook)
      ensureCurrent()
      const imported = await factoryApi.addTask(`采集批次 ${batchId} · ${batch.task_count} 条任务`, batch.filename, content, batchId)
      ensureCurrent()
      options.setTasks(imported.tasks)
      const task = imported.tasks.find(row => row.source_batch_id === batchId && row.filename === batch.filename)
      if (!task) throw new Error('批次文件未成功登记，不能开始运行')
      const parameters = { filename: task.filename, phone_id: phoneId, app, ...runOptions }
      const key = JSON.stringify(parameters)
      const requestId = pendingRequests.get(key) || newRunRequestId()
      pendingRequests.set(key, requestId)
      const remote = await factoryApi.remoteStartRun({ ...parameters, request_id: requestId })
      if (!remote.ok) throw new Error(remote.error || '手机工厂未接受本次运行请求')
      ensureCurrent()
      pendingRequests.delete(key)
      // The server owns run status; a late client write must not revive a run.
      if (isCurrent()) {
        try { const state = await factoryApi.state(); if (isCurrent()) options.setTasks(state.tasks) }
        catch { /* The next refresh recovers state without resubmitting accepted work. */ }
      }
      return remote
    } finally { if (!disposed) busy.value = false }
  }

  async function downloadBatch() {
    if (disposed || blocked.value || !selectedBatch.value) return false
    const batch = selectedBatch.value, revision = selectionRevision
    busy.value = true
    downloading.value = true
    try {
      const workbook = await batchApi.workbook(batch.batch_id)
      if (disposed || revision !== selectionRevision) return false
      ;(options.downloadFile || downloadBlob)(workbook, batch.filename)
      return true
    } finally {
      if (!disposed) { busy.value = false; downloading.value = false }
    }
  }

  function retireBatches(ids: string[], forceReset = false) {
    ids.forEach(id => retiredIds.add(id))
    const filenames = new Set(ids.map(id => `collection-batch-${id}.xlsx`))
    for (const key of pendingRequests.keys()) { if (filenames.has((JSON.parse(key) as { filename: string }).filename)) pendingRequests.delete(key) }
    batches.value = batches.value.filter(batch => !ids.includes(batch.batch_id))
    if (!forceReset && !ids.includes(selectedBatchId.value)) return
    ++listRevision; loadingBatches.value = false
    ++selectionRevision; selectedBatchId.value = ''; selectedBatch.value = null; loadingBatch.value = false; error.value = ''
  }
  function dispose() { disposed = true; selectionRevision += 1; listRevision += 1 }
  return { batches, selectedBatchId, selectedBatch, loadingBatches, loadingBatch, busy, downloading, error, blocked, selectBatch, loadBatches, runBatch, downloadBatch, retireBatches, dispose }
}
