import { notifyBatchesPublished, activeBatchItems } from '@/utils/batchLifecycle'
import type { CollectionBatchDetail, CollectionBatchSummary } from '@/collectionBatchesApi'

export interface PhoneAppRow { phone_id: string; app: string; status: string }
export interface CollectionWarning { sheet: string; row: number; field: string; message: string }
export interface TaskRow { warnings?: CollectionWarning[]; original_filename?: string; description: string; filename: string; status: string; source_batch_id?: string }
export interface FactoryState { imported_task?: Pick<TaskRow, 'filename' | 'source_batch_id' | 'warnings'>; phones: string[]; apps: string[]; phoneApps: PhoneAppRow[]; vla: string[]; tasks: TaskRow[] }
export interface FactoryConfig { sampling_enabled: boolean; temperature: number; top_p: number; use_experience_lib: boolean }
export interface RunOptions { filename: string; phone_id?: string; app?: string; request_id: string; vla: string; run_mode?: 'generate' | 'modeliter'; config?: FactoryConfig }
export interface RemoteResult { ok: boolean; message?: string; error?: string; run_id?: string; collection_run_id?: string; batch_id?: string }
export interface AdbDevice { serial: string; model: string; battery: number | null }
export interface PhoneMonitor { ok: boolean; screenshot: string | null; log: string; running: boolean; device_size?: { width: number; height: number } | null }
export interface FactoryRun { run_id?: string; collection_run_id?: string; batch_id?: string; status: string; created_at?: string; filename?: string; vla?: string; error?: string; dispatch_error?: string; transfer_error?: string; transfer_status?: string; errors?: Array<{ error?: string; message?: string }>; trajectory_count?: number }
export interface ReportFile { name: string; size: number; modified?: number; file_id?: string }
export interface ReportFolder { index: number; dir_name: string; modified?: number; run_id?: string; files: ReportFile[] }
export type FactoryBatchSummary = Omit<CollectionBatchSummary, 'kind' | 'source_job_id'> & { warnings?: CollectionWarning[]; kind: CollectionBatchSummary['kind'] | 'manual_collection'; source_job_id: string | null }
export type FactoryBatchDetail = Omit<CollectionBatchDetail, 'kind' | 'source_job_id' | 'snapshot'> & FactoryBatchSummary & { snapshot: { tasks: Array<Record<string, unknown>> } }

export function newRunRequestId(): string {
  if (globalThis.crypto.randomUUID) return globalThis.crypto.randomUUID()
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
function jsonInit(method: string, body: unknown, signal?: AbortSignal): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal }
}
async function checked(path: string, init?: RequestInit) {
  const response = await fetch(`${API_BASE}${path}`, { ...init, cache: 'no-store' })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    const detail = payload?.detail
    if (detail?.code === 'batch_published' && detail.batch_id) notifyBatchesPublished({ batch_ids: [detail.batch_id], release_id: detail.release_id ?? null, published_at: detail.published_at })
    throw new Error(detail?.message || (typeof detail === 'string' ? detail : '') || payload?.error || `${response.status} ${response.statusText}`)
  }
  return response
}
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await checked(path, init)
  const payload = await response.json()
  if (!payload || payload.error || payload.ok === false) throw new Error(payload?.error || payload?.message || '服务返回了无效响应')
  return payload as T
}
export function createFactoryApi(base: '/api/phone-factory' | '/api/model-iter') {
  const req = <T>(path: string, init?: RequestInit) => request<T>(base + path, init)
  return {
    state: (signal?: AbortSignal) => req<FactoryState>('/state', { signal }),
    addPhone: (phoneId: string) => req<FactoryState>('/phones', jsonInit('POST', { phone_id: phoneId })),
    addApp: (app: string) => req<FactoryState>('/apps', jsonInit('POST', { app })),
    addPhoneApp: (phoneId: string, app: string) => req<FactoryState>('/phone-apps', jsonInit('POST', { phone_id: phoneId, app })),
    removePhoneApp: (phoneId: string, app: string) => req<FactoryState>('/phone-apps', jsonInit('DELETE', { phone_id: phoneId, app })),
    saveVla: (value: string) => req<FactoryState>('/vla', jsonInit('POST', { value })),
    addTask: (description: string, filename: string, contentBase64: string, sourceBatchId?: string, requestId?: string) => req<FactoryState>('/tasks', jsonInit('POST', { description, filename, content_base64: contentBase64, ...(sourceBatchId ? { source_batch_id: sourceBatchId } : {}), ...(requestId ? { request_id: requestId } : {}) })),
    startTask: (filename: string) => req<FactoryState>('/tasks/start', jsonInit('POST', { filename })),
    removeTask: (filename: string) => req<FactoryState>('/tasks', jsonInit('DELETE', { filename })),
    config: () => req<FactoryConfig>('/config'),
    saveConfig: (config: FactoryConfig) => req<FactoryConfig>('/config', jsonInit('POST', config)),
    remoteAddPhone: (phoneId: string) => req<RemoteResult>('/remote/add-phone', jsonInit('POST', { phone_id: phoneId })),
    remoteDeletePhone: (phoneId: string) => req<RemoteResult & { state?: FactoryState }>('/remote/del-phone', jsonInit('POST', { phone_id: phoneId })),
    remoteStartRun: (options: RunOptions) => req<RemoteResult>('/remote/start-run', jsonInit('POST', options)),
    remoteStatus: (phones: string[], signal?: AbortSignal) => req<{ ok: boolean; statuses: Array<{ phone_id: string; status: string }> }>('/remote/status', jsonInit('POST', { phones }, signal)),
    adbDevices: (signal?: AbortSignal) => req<{ ok: boolean; devices: AdbDevice[] }>('/remote/adb-devices', jsonInit('POST', {}, signal)),
    monitor: (phoneId: string, signal?: AbortSignal) => req<PhoneMonitor>('/remote/monitor', jsonInit('POST', { phone_id: phoneId }, signal)),
    runs: (signal?: AbortSignal) => req<{ runs: FactoryRun[] }>(base === '/api/model-iter' ? '/runs' : '/collection-runs', { signal }),
    syncRun: (id: string) => req<FactoryRun>(`/collection-runs/${encodeURIComponent(id)}/sync`, jsonInit('POST', {})),
    reports: (signal?: AbortSignal) => req<{ ok: boolean; folders: ReportFolder[] }>('/reports', { signal }),
    async reportDownload(folder: ReportFolder, file: ReportFile) {
      const params = new URLSearchParams(folder.run_id && file.file_id ? { run_id: folder.run_id, file_id: file.file_id } : { folder: folder.dir_name, name: file.name })
      return (await checked(`${base}/report-download?${params}`)).blob()
    },
  }
}
export const phoneFactoryApi = createFactoryApi('/api/phone-factory')
export const modelIterationApi = createFactoryApi('/api/model-iter')
export const factoryBatchesApi = {
  async list(): Promise<FactoryBatchSummary[]> { return activeBatchItems((await request<{ batches: FactoryBatchSummary[] }>('/api/phone-factory/batches')).batches) },
  detail: (id: string) => request<FactoryBatchDetail>(`/api/phone-factory/batches/${encodeURIComponent(id)}`),
  async workbook(id: string) { return (await checked(`/api/phone-factory/batches/${encodeURIComponent(id)}/workbook`)).blob() },
}
