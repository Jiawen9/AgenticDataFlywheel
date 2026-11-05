import { onBeforeUnmount, reactive } from 'vue'
import { factoryStorageApi, newRunRequestId, type CollectorDataState, type StorageOperation } from '@/phoneFactoryApi'
import { useFactoryPolling } from './useFactoryPolling'

export interface StorageTask {
  runId: string; requestId: string; kind: StorageOperation['kind']; mode?: 'retire' | 'cache'
  result?: StorageOperation; sending: boolean; error: string; rejected?: boolean
}
export const storageOperationDone = (status?: string) => ['completed', 'partial', 'failed', 'rejected'].includes(status || '')
export function storagePhase(value: StorageOperation): string {
  const phase: Record<string, string> = { scan: '统计运行数据', cleanup: '清理运行数据', archive: '生成完整归档', waiting_connection: '等待采集端恢复连接', queued: '已受理，等待处理', running: '处理中', stopping: '停止相关任务', draining: '释放文件占用', deleting: '正在删除', scanning: '统计运行数据', building: '生成完整归档', completed: '已完成', partial: '部分完成，可重试剩余项', failed: '未完成，可重试', retiring: '正在清理', cancelled: '已取消' }
  return phase[value.phase || ''] || phase[value.status] || value.phase || value.status
}
export function storageBytes(value?: number): string {
  if (value == null || !Number.isFinite(value)) return '未统计'
  if (value < 1024) return `${value} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']; let size = value / 1024, index = 0
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index++ }
  return `${size.toFixed(2)} ${units[index]}`
}

/** Page-owned jobs continue while the details drawer is closed. */
export function useRunStorage(options: {
  api?: typeof factoryStorageApi
  storage?: Pick<Storage, 'getItem' | 'setItem'>
  scope?: string
  onState?: (runId: string, state: CollectorDataState, operationId: string) => void
  onArchive?: (runId: string, status: string) => void
} = {}) {
  const api = options.api || factoryStorageApi
  const key = `adf:collector-storage:v1:${options.scope || import.meta.env.VITE_API_BASE_URL || 'same-origin'}`
  let storage = options.storage
  const tasks = reactive<StorageTask[]>([])
  let disposed = false
  const controllers = new Set<AbortController>()
  try {
    storage ??= globalThis.sessionStorage
    const rows = JSON.parse(storage?.getItem(key) || '[]')
    if (Array.isArray(rows)) for (const row of rows) {
      if (typeof row?.runId === 'string' && typeof row.requestId === 'string' && ['scan', 'cleanup', 'archive'].includes(row.kind))
        tasks.push({ ...row, sending: false, error: String(row.error || '') })
    }
  } catch { /* Session storage availability does not gate cleanup. */ }
  const retiredRuns = new Set(tasks.flatMap(task => task.result?.collector_data_state === 'retired' ? [task.runId, ...(task.result.execution_ids || [])] : []))
  function persist() {
    const pending = tasks.filter(task => !storageOperationDone(task.result?.status) && !task.rejected)
    const done = tasks.filter(task => storageOperationDone(task.result?.status) || task.rejected).slice(-30)
    try { storage?.setItem(key, JSON.stringify([...done, ...pending])) } catch { /* Keep active identities in memory. */ }
  }
  async function bounded<T>(work: (signal: AbortSignal) => Promise<T>, outer?: AbortSignal): Promise<T> {
    const controller = new AbortController(); controllers.add(controller)
    const abort = () => controller.abort()
    outer?.addEventListener('abort', abort, { once: true }); if (outer?.aborted) abort()
    const timer = setTimeout(abort, 15000)
    try { return await work(controller.signal) }
    finally { clearTimeout(timer); controllers.delete(controller); outer?.removeEventListener('abort', abort) }
  }
  function update(task: StorageTask, result: StorageOperation) {
    if (disposed) return
    task.result = result; task.error = result.error || ''; task.rejected = false
    if (task.kind === 'cleanup' && task.mode === 'retire' && result.collector_data_state) {
      // A rejected capability check has no cleanup intent. Do not disable an old
      // collector merely because its operation failed before touching run data.
      for (const id of new Set([task.runId, ...(result.execution_ids || [])])) {
        if (retiredRuns.has(id) && result.collector_data_state !== 'retired') continue
        if (result.collector_data_state === 'retired') retiredRuns.add(id)
        options.onState?.(id, result.collector_data_state, result.operation_id)
      }
    }
    if (task.kind === 'archive') options.onArchive?.(task.runId, result.status === 'completed' ? 'ready' : storageOperationDone(result.status) ? 'failed' : 'building')
    if (task.kind === 'cleanup' && task.mode === 'cache' && result.status === 'completed' && result.archive_status) options.onArchive?.(task.runId, result.archive_status)
    persist()
  }
  async function send(task: StorageTask, outer?: AbortSignal) {
    if (task.sending || disposed) return
    task.sending = true; task.error = ''; task.rejected = false; persist()
    try {
      const result = await bounded(signal => task.kind === 'cleanup' ? api.cleanup(task.runId, task.requestId, task.mode || 'retire', signal)
        : task.kind === 'archive' ? api.archive(task.runId, task.requestId, signal) : api.scan(task.runId, task.requestId, signal), outer)
      if (!outer?.aborted) update(task, result)
    } catch (cause) {
      if (disposed || outer?.aborted) return
      const issue = cause as Error & { status?: number }
      task.error = issue.message || '请求结果尚未确认，使用同一编号继续查询'
      task.rejected = issue.status === 501 || (!!issue.status && issue.status >= 400 && issue.status < 500 && ![408, 429].includes(issue.status))
    } finally { task.sending = false; persist() }
  }
  async function poll(task: StorageTask, signal?: AbortSignal) {
    if (disposed || task.sending || task.rejected || storageOperationDone(task.result?.status)) return
    if (!task.result) { await send(task, signal); return } // Lost acknowledgement: replay exact POST identity.
    try {
      const result = await bounded(abort => api.operation(task.result!.operation_id, abort), signal)
      if (!signal?.aborted) update(task, result)
    } catch (cause) {
      if (!disposed && !signal?.aborted) task.error = `等待同步：${(cause as Error).message}`
    }
  }
  async function batches<T>(rows: T[], work: (row: T) => Promise<void>) {
    for (let index = 0; index < rows.length && !disposed; index += 2) await Promise.allSettled(rows.slice(index, index + 2).map(work))
  }
  useFactoryPolling(async signal => {
    await batches(tasks.filter(task => !storageOperationDone(task.result?.status) && !task.rejected), task => poll(task, signal))
  }, 5000)
  async function start(ids: string[], kind: StorageOperation['kind'], mode?: 'retire' | 'cache') {
    const queued: StorageTask[] = []
    for (const runId of new Set(ids.filter(Boolean))) {
      let item = [...tasks].reverse().find(task => task.runId === runId && task.kind === kind && task.mode === mode && !storageOperationDone(task.result?.status) && !task.rejected)
      if (!item) { item = reactive({ runId, requestId: newRunRequestId(), kind, mode, sending: false, error: '' }); tasks.push(item) }
      queued.push(item)
    }
    persist(); await batches(queued, task => task.result ? poll(task) : send(task))
  }
  async function retry(task: StorageTask) { await send(task) }
  function latest(runId: string, kind?: StorageOperation['kind']) { return [...tasks].reverse().find(task => task.runId === runId && (!kind || task.kind === kind)) }
  const pending = (runId: string) => tasks.some(task => task.runId === runId && !storageOperationDone(task.result?.status) && !task.rejected)
  onBeforeUnmount(() => { disposed = true; controllers.forEach(controller => controller.abort()); persist() })
  return { tasks, start, retry, latest, pending, archiveDownloadUrl: api.archiveDownloadUrl }
}
export type RunStorageState = ReturnType<typeof useRunStorage>
