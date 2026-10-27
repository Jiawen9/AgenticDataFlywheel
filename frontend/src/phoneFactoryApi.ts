import { notifyBatchesPublished, activeBatchItems } from '@/utils/batchLifecycle'
import type { CollectionBatchDetail, CollectionBatchSummary } from '@/collectionBatchesApi'

export type AgentType = 'newagent' | 'pipelineagent'
export interface AgentCapability { available: boolean; reason?: string; collection: boolean; capture_logprobs: boolean; model_iteration_evaluation: boolean; offline_evaluation: boolean }
export type AgentCapabilities = Partial<Record<AgentType, AgentCapability>>
export interface AgentProvenance { agent_type?: AgentType; agent_version?: string; trajectory_format?: string; normalizer_version?: string; runtime_digest?: string; configuration_digest?: string }
export interface FactorySettings extends FactoryConfig { agent_type?: AgentType }
export interface PhoneAppRow { phone_id: string; app: string; status: string }
export interface CollectionWarning { sheet: string; row: number; field: string; message: string }
export interface TaskRow { warnings?: CollectionWarning[]; original_filename?: string; description: string; filename: string; status: string; source_batch_id?: string }
export interface FactoryState { imported_task?: Pick<TaskRow, 'filename' | 'source_batch_id' | 'warnings'>; phones: string[]; apps: string[]; phoneApps: PhoneAppRow[]; vla: string[]; tasks: TaskRow[] }
export interface FactoryConfig { sampling_enabled: boolean; temperature: number; top_p: number; use_experience_lib: boolean; capture_logprobs: boolean }
export interface EvaluatorModel { base_url: string; model: string; api_key_env: string }
export interface EvaluatorConfig { text: EvaluatorModel; vision: EvaluatorModel }
export type EvaluationExecutionMode = 'collect_and_evaluate' | 'evaluate_only'
export interface EvaluationProfile { id: string; name: string; digest: string }
export type EvaluationIssue = string | { message?: string; error?: string; case_id?: string; path?: string }
export interface EvaluationSource extends AgentProvenance { run_id?: string; source_run_id?: string; path?: string; source_path?: string; run_mode?: string; status?: string; created_at?: string; trajectory_count?: number }
export type EvaluationSourceInput = { source_run_id: string; source_path?: never } | { source_path: string; source_run_id?: never }
export interface EvaluationPreview { preview_id: string; valid: boolean; trajectory_count: number; cases: Array<{ source_trajectory_id: string; case_id: string; task: string; phone_id?: string; key_elements?: string }>; errors: EvaluationIssue[]; source: EvaluationSource; source_digest: string }
export interface EvaluationRunMetadata { execution_mode?: EvaluationExecutionMode; evaluation_profile_id?: string; evaluation_profile_name?: string; evaluation_profile_digest?: string; source?: EvaluationSource }
export interface StartEvaluationOptions { request_id: string; evaluator_config: EvaluatorConfig; evaluation_profile_id: string; evaluation_profile_digest: string; preview_id: string; checklist_source: 'auto' }
export interface EvaluationSummary { total: number; collection_completed: number; collection_failed: number; evaluation_completed: number; evaluation_failed: number; evaluation_running: number; evaluation_pending: number; success: number; partial_success: number; fail: number; success_rate: number | null }
export interface EvaluationCase extends AgentProvenance { case_key: string; phone_id: string; case_id: string; session_id?: string; source_trajectory_id?: string; checklist_source?: string; collection_status: string; evaluation_status: string; verdict?: string | null; failure_reason?: string; attempt_count: number; report_file_id?: string | null; report_name?: string; errors: Array<{ error?: string; message?: string } | string> }
export interface EvaluationResults extends EvaluationRunMetadata { run_id: string; run_mode: 'modeliter'; summary: EvaluationSummary; cases: EvaluationCase[]; stale?: boolean; sync_error?: string }
export interface RunOptions extends AgentProvenance { evaluator_config?: EvaluatorConfig; evaluation_profile_id?: string; evaluation_profile_digest?: string; checklist_source?: 'llm' | 'auto'; evaluation_mode?: 'checklist'; filename: string; phone_id?: string; app?: string; request_id: string; vla: string; run_mode?: 'generate' | 'modeliter'; config?: FactoryConfig }
export interface RemoteResult { ok: boolean; message?: string; error?: string; run_id?: string; collection_run_id?: string; batch_id?: string }
export interface AdbDevice { serial: string; model: string; battery: number | null }
export interface PhoneMonitor extends AgentProvenance { run_id?: string; ok: boolean; screenshot: string | null; log: string; running: boolean; device_size?: { width: number; height: number } | null }
export interface FactoryRun extends EvaluationRunMetadata, AgentProvenance { evaluation_summary?: EvaluationSummary; run_id?: string; collection_run_id?: string; batch_id?: string; status: string; created_at?: string; filename?: string; vla?: string; error?: string; dispatch_error?: string; transfer_error?: string; transfer_status?: string; remote_status?: string; remote_errors?: Array<{ error?: string; message?: string }>; errors?: Array<{ error?: string; message?: string }>; trajectory_count?: number }
export interface ReportFile { name: string; size: number; modified?: number; file_id?: string }
export interface ReportFolder extends AgentProvenance { index: number; dir_name: string; modified?: number; run_id?: string; files: ReportFile[] }
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
    evaluationConfig: () => req<EvaluatorConfig>('/evaluation-config'),
    saveEvaluationConfig: (config: EvaluatorConfig) => req<EvaluatorConfig>('/evaluation-config', jsonInit('PUT', config)),
    evaluationCapabilities: () => req<{ ok: boolean; supported: boolean; offline_supported?: boolean; profiles_supported?: boolean; message: string }>('/evaluation-capabilities'),
    evaluationProfiles: () => req<{ profiles: EvaluationProfile[]; errors: EvaluationIssue[] }>('/evaluation-profiles'),
    evaluationSources: () => req<{ sources: EvaluationSource[]; allowed_roots: string[] }>('/evaluation-sources'),
    evaluationPreview: (source: EvaluationSourceInput, signal?: AbortSignal) => req<EvaluationPreview>('/evaluation-preview', jsonInit('POST', source, signal)),
    remoteStartEvaluation: (options: StartEvaluationOptions) => req<RemoteResult>('/remote/start-evaluation', jsonInit('POST', options)),
    evaluations: (runId: string, signal?: AbortSignal) => req<EvaluationResults>(`/runs/${encodeURIComponent(runId)}/evaluations`, { signal }),
    cancelEvaluation: (runId: string) => req<FactoryRun>(`/runs/${encodeURIComponent(runId)}/cancel`, jsonInit('POST', {})),
    state: (signal?: AbortSignal) => req<FactoryState>('/state', { signal }),
    addPhone: (phoneId: string) => req<FactoryState>('/phones', jsonInit('POST', { phone_id: phoneId })),
    addApp: (app: string) => req<FactoryState>('/apps', jsonInit('POST', { app })),
    addPhoneApp: (phoneId: string, app: string) => req<FactoryState>('/phone-apps', jsonInit('POST', { phone_id: phoneId, app })),
    removePhoneApp: (phoneId: string, app: string) => req<FactoryState>('/phone-apps', jsonInit('DELETE', { phone_id: phoneId, app })),
    saveVla: (value: string) => req<FactoryState>('/vla', jsonInit('POST', { value })),
    addTask: (description: string, filename: string, contentBase64: string, sourceBatchId?: string, requestId?: string) => req<FactoryState>('/tasks', jsonInit('POST', { description, filename, content_base64: contentBase64, ...(sourceBatchId ? { source_batch_id: sourceBatchId } : {}), ...(requestId ? { request_id: requestId } : {}) })),
    startTask: (filename: string) => req<FactoryState>('/tasks/start', jsonInit('POST', { filename })),
    removeTask: (filename: string) => req<FactoryState>('/tasks', jsonInit('DELETE', { filename })),
    agentCapabilities: () => req<{ ok: boolean; agent_types: AgentCapabilities }>('/agent-capabilities'),
    config: () => req<FactorySettings>('/config'),
    saveConfig: (config: FactorySettings) => req<FactorySettings>('/config', jsonInit('POST', config)),
    remoteAddPhone: (phoneId: string) => req<RemoteResult>('/remote/add-phone', jsonInit('POST', { phone_id: phoneId })),
    remoteDeletePhone: (phoneId: string) => req<RemoteResult & { state?: FactoryState }>('/remote/del-phone', jsonInit('POST', { phone_id: phoneId })),
    remoteStartRun: (options: RunOptions) => req<RemoteResult>('/remote/start-run', jsonInit('POST', options)),
    remoteStatus: (phones: string[], signal?: AbortSignal) => req<{ ok: boolean; statuses: Array<{ phone_id: string; status: string; agent_type?: AgentType; run_id?: string }> }>('/remote/status', jsonInit('POST', { phones }, signal)),
    adbDevices: (signal?: AbortSignal) => req<{ ok: boolean; devices: AdbDevice[] }>('/remote/adb-devices', jsonInit('POST', {}, signal)),
    monitor: (phoneId: string, signal?: AbortSignal) => req<PhoneMonitor>('/remote/monitor', jsonInit('POST', { phone_id: phoneId }, signal)),
    runs: (signal?: AbortSignal) => req<{ runs: FactoryRun[] }>(base === '/api/model-iter' ? '/runs' : '/collection-runs', { signal }),
    cancelCollection: (id: string) => req<FactoryRun>(`/collection-runs/${encodeURIComponent(id)}/cancel`, jsonInit('POST', {})),
    syncRun: (id: string) => req<FactoryRun>(`/collection-runs/${encodeURIComponent(id)}/sync`, jsonInit('POST', {})),
    reports: (signal?: AbortSignal) => req<{ ok: boolean; folders: ReportFolder[] }>('/reports', { signal }),
    async reportDownload(folder: ReportFolder, file: ReportFile) {
      const params = new URLSearchParams(folder.run_id && file.file_id ? { run_id: folder.run_id, file_id: file.file_id, name: file.name } : { folder: folder.dir_name, name: file.name })
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
