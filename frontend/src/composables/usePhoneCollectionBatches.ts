import { computed, ref } from 'vue'
import type { CollectionBatchDetail, CollectionBatchSummary } from '@/collectionBatchesApi'
import type { FactoryState, TaskRow } from '@/phoneFactoryApi'

interface BatchApi {
  list(): Promise<CollectionBatchSummary[]>
  detail(id: string): Promise<CollectionBatchDetail>
  workbook(id: string): Promise<Blob>
}
interface FactoryApi {
  addTask(description: string, filename: string, contentBase64: string, sourceBatchId?: string): Promise<FactoryState>
  remoteStartRun(filename: string, phoneId: string, app: string): Promise<{ ok: boolean; message?: string; error?: string }>
  startTask(filename: string): Promise<FactoryState>
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
}) {
  const batches = ref<CollectionBatchSummary[]>([])
  const selectedBatchId = ref('')
  const selectedBatch = ref<CollectionBatchDetail | null>(null)
  const loadingBatches = ref(false)
  const loadingBatch = ref(false)
  const busy = ref(false)
  const downloading = ref(false)
  const error = ref('')
  let selectionRevision = 0
  let listRevision = 0
  let disposed = false
  const blocked = computed(() => busy.value || Boolean(options.isBlocked?.()))

  async function selectBatch(id: string): Promise<boolean> {
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
      batches.value = rows
      if (preferredId && selection === selectionRevision) await selectBatch(preferredId)
    } catch (cause) {
      if (!disposed && revision === listRevision) error.value = (cause as Error).message
    } finally {
      if (!disposed && revision === listRevision) loadingBatches.value = false
    }
  }

  async function runBatch(batchId: string, phoneId = '', app = '') {
    if (disposed || busy.value) throw new Error('正在处理采集任务，请稍候')
    busy.value = true
    const revision = selectionRevision
    const ensureCurrent = () => {
      if (disposed || revision !== selectionRevision) throw new Error('批次选择已变化，本次未继续下发')
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
      if (task.status === '运行中') throw new Error('该采集批次已经在运行中，请勿重复下发')
      const remote = await factoryApi.remoteStartRun(task.filename, phoneId, app)
      if (!remote.ok) throw new Error(remote.error || '手机工厂未接受本次运行请求')
      // Once the phone service accepted the work, save that fact even if this
      // component was unmounted while awaiting its response.
      const state = await factoryApi.startTask(task.filename)
      if (!disposed && revision === selectionRevision) options.setTasks(state.tasks)
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

  function dispose() { disposed = true; selectionRevision += 1; listRevision += 1 }
  return { batches, selectedBatchId, selectedBatch, loadingBatches, loadingBatch, busy, downloading, error, blocked, selectBatch, loadBatches, runBatch, downloadBatch, dispose }
}
