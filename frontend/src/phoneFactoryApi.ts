import { notifyBatchesPublished, activeBatchItems } from '@/utils/batchLifecycle'
import type { CollectionBatchDetail, CollectionBatchSummary } from '@/collectionBatchesApi'

export type AgentType = 'newagent' | 'pipelineagent'
export interface AgentCapability { available: boolean; reason?: string; collection: boolean; capture_logprobs: boolean; model_iteration_evaluation: boolean; offline_evaluation: boolean; model_overrides?: boolean; repeat_times?: boolean; max_repeat_times?: number; collection_artifacts_version?: number }
export type AgentCapabilities = Partial<Record<AgentType, AgentCapability>>
export interface AgentProvenance { agent_type?: AgentType; agent_version?: string; trajectory_format?: string; normalizer_version?: string; runtime_digest?: string; configuration_digest?: string }
export interface PlannerConfig { base_url: string; model: string; api_key_env?: string }
export interface ModelOverrides { execution_model?: string; planner_config?: PlannerConfig }
export interface PipelineModelDefaults { vla: string; execution_model: string; planner_config: PlannerConfig | null }
export interface NewAgentModelDefaults { execution_model: string }
export interface FactorySettings extends FactoryConfig { agent_type?: AgentType; model_defaults?: PipelineModelDefaults; newagent_model_defaults?: NewAgentModelDefaults; repeat_times?: number }
export interface PhoneAppRow { phone_id: string; app: string; status: string }
export interface CollectionWarning { sheet: string; row: number; field: string; message: string }
export interface CollectionBatchLineage { batch_role?: 'collection_result'; task_source_batch_id?: string }
export interface TaskRow extends CollectionBatchLineage { warnings?: CollectionWarning[]; original_filename?: string; description: string; filename: string; status: string; source_batch_id?: string }
export interface FactoryState { imported_task?: Pick<TaskRow, 'filename' | 'source_batch_id' | 'warnings'>; phones: string[]; apps: string[]; phoneApps: PhoneAppRow[]; vla: string[]; tasks: TaskRow[] }
export interface FactoryConfig { sampling_enabled: boolean; temperature: number; top_p: number; use_experience_lib: boolean; capture_logprobs: boolean }
export interface EvaluatorModel { base_url: string; model: string; api_key_env: string }
export interface EvaluatorConfig { text: EvaluatorModel; vision: EvaluatorModel }
export type EvaluationExecutionMode = 'collect_and_evaluate' | 'evaluate_only'
export interface EvaluationProfile { id: string; name: string; digest: string }
export type EvaluationIssue = string | { message?: string; error?: string; case_id?: string; path?: string }
export interface EvaluationSource extends AgentProvenance { run_id?: string; source_run_id?: string; path?: string; source_path?: string; run_mode?: string; status?: string; created_at?: string; trajectory_count?: number }
export type EvaluationSourceInput = ({ source_run_id: string; source_path?: never } | { source_path: string; source_run_id?: never }) & { request_id?: string }
export interface EvaluationCoverage { total: number; included: number; excluded: number }
export interface CollectionSnapshotMetadata { stale_collection_snapshot?: boolean; source_collection_snapshot_id?: string | null; current_collection_snapshot_id?: string | null; collection_snapshot_pending?: boolean }
export interface EvaluationJobProgress { discovered?: number; checked?: number; eligible?: number; excluded?: number; prepared?: number; total?: number }
export interface EvaluationPreparation { status: string; phase?: string; revision?: number; progress?: EvaluationJobProgress; error?: string; retryable?: boolean }
export interface EvaluationJobControl { request_id: string; expected_revision: number }
export interface EvaluationPreview extends CollectionSnapshotMetadata { control_error?: string; job_version?: number; revision?: number; phase?: string; progress?: EvaluationJobProgress; error?: string; retryable?: boolean; status?: 'preparing' | 'completed' | 'failed' | 'cancelled'; coverage?: EvaluationCoverage; excluded?: EvaluationIssue[]; warnings?: EvaluationIssue[]; has_more?: boolean; case_offset?: number; sync_error?: string; preview_id: string; valid: boolean; trajectory_count: number; cases: Array<{ source_trajectory_id: string; case_id: string; task: string; phone_id?: string; key_elements?: string }>; errors: EvaluationIssue[]; source: EvaluationSource; source_digest: string }
export interface EvaluationRunMetadata extends CollectionSnapshotMetadata { input_mode?: string; preparation_control_error?: string; preparation_control_pending?: boolean; preparation?: EvaluationPreparation; execution_mode?: EvaluationExecutionMode; evaluation_profile_id?: string; evaluation_profile_name?: string; evaluation_profile_digest?: string; source?: EvaluationSource }
export interface StartEvaluationOptions { request_id: string; evaluator_config: EvaluatorConfig; evaluation_profile_id: string; evaluation_profile_digest: string; preview_id: string; checklist_source: 'auto' }
export interface EvaluationSummary { total: number; collection_completed: number; collection_failed: number; evaluation_completed: number; evaluation_failed: number; evaluation_running: number; evaluation_pending: number; success: number; partial_success: number; fail: number; success_rate: number | null }
export interface EvaluationCase extends AgentProvenance { case_key: string; phone_id: string; case_id: string; session_id?: string; source_trajectory_id?: string; checklist_source?: string; collection_status: string; evaluation_status: string; verdict?: string | null; failure_reason?: string; attempt_count: number; report_file_id?: string | null; report_name?: string; errors: Array<{ error?: string; message?: string } | string> }
export interface EvaluationResults extends EvaluationRunMetadata { coverage?: EvaluationCoverage; excluded?: EvaluationIssue[]; evidence_warnings?: EvaluationIssue[]; run_id: string; run_mode: 'modeliter'; summary: EvaluationSummary; cases: EvaluationCase[]; stale?: boolean; sync_error?: string }
export interface RunOptions extends AgentProvenance, ModelOverrides { repeat_times?: number; evaluator_config?: EvaluatorConfig; evaluation_profile_id?: string; evaluation_profile_digest?: string; checklist_source?: 'llm' | 'auto'; evaluation_mode?: 'checklist'; filename: string; phone_id?: string; app?: string; request_id: string; vla: string; run_mode?: 'generate' | 'modeliter'; config?: FactoryConfig }
export interface RemoteResult extends CollectionBatchLineage, AgentProvenance { ok: boolean; message?: string; error?: string; run_id?: string; collection_run_id?: string; batch_id?: string; status?: string }
export interface AdbDevice { serial: string; model: string; battery: number | null }
export interface PhoneMonitor extends AgentProvenance { run_id?: string; ok: boolean; screenshot: string | null; log: string; running: boolean; device_size?: { width: number; height: number } | null }
export interface CollectionSummary { collection_not_run_count?: number; outcome_count?: number; received_outcome_count?: number; received_artifact_count?: number; quarantined_count?: number; evaluation_eligible_count?: number; preprocessing_eligible_count?: number; original_task_count: number; planned_trajectory_count: number; collection_completed_count: number; collection_failed_count: number; collection_cancelled_count: number; sealed_trajectory_count: number; diagnostic_count: number; transferred_trajectory_count?: number; transferred_diagnostic_count?: number }
export interface CollectionArtifact { receipt_status?: string; artifact_id: string; kind: 'trajectory' | 'diagnostic' | 'outcome'; eligibility?: { evaluation: boolean; preprocessing: boolean; issues: Array<{ code: string; scope?: string; path?: string; step?: number }> }; status?: string; collection_case_id?: string; phone_id?: string; expanded_case_id?: string; repeat_index?: number; repeat_times?: number; execution_status?: string; transfer_status?: string; error?: string; transfer_error?: string; source_trajectory_id?: string }
export type CollectorDataState = 'available' | 'retiring' | 'retired' | 'cleanup_failed'
export interface StorageOperation { collector_data_state?: CollectorDataState; archive_status?: string; operation_id: string; request_id?: string; run_id: string; kind: 'scan' | 'cleanup' | 'archive'; mode?: 'retire' | 'cache' | null; status: string; phase?: string; created_at?: string; updated_at?: string; execution_ids?: string[]; dependent_run_ids?: string[]; summary?: { files?: number; bytes?: number; deleted_files?: number; deleted_bytes?: number }; items?: Array<{ run_id?: string; path: string; category?: string; files?: number; bytes?: number }>; errors?: Array<{ path?: string; error: string }>; error?: string; download_path?: string }
export interface FactoryRun extends EvaluationRunMetadata, AgentProvenance, CollectionBatchLineage, ModelOverrides { collector_data_state?: CollectorDataState; cleanup_operation_id?: string; input_mode?: string; receipt_storage?: { paused: boolean; pending?: boolean; error?: string | null; notification_error?: string | null; last_error?: string | null }; collection_artifact_count?: number; receipt_policy_version?: number; receipt_closed?: boolean; archive_status?: string; can_cancel_collection?: boolean; stop_requested_at?: string; control_error?: string; control_terminal?: boolean; phone_states?: Record<string, { status: string; reason?: string; stop_confirmed?: boolean }>; repeat_times?: number; collection_artifacts_version?: number; collection_status?: string; collection_summary?: CollectionSummary; collection_artifacts?: CollectionArtifact[]; batch_status?: 'active' | 'published'; release_id?: string | null; published_at?: string | null; evaluation_summary?: EvaluationSummary; run_id?: string; collection_run_id?: string; batch_id?: string; status: string; created_at?: string; filename?: string; vla?: string; error?: string; dispatch_error?: string; transfer_error?: string; transfer_status?: string; remote_status?: string; remote_errors?: Array<{ error?: string; message?: string }>; errors?: Array<{ error?: string; message?: string }>; trajectory_count?: number }
export interface ArtifactPage { items: CollectionArtifact[]; total: number; next_offset?: number; has_more: boolean }
export interface ArtifactDetail extends CollectionArtifact { manifest: Record<string, unknown>; files: Array<{ path: string; size: number; sha256: string }> }
export interface PhoneOwner { execution_id: string; state_revision: number; ownership_token: string; availability: 'free' | 'busy' | 'unknown'; reason_code?: string; supported_actions: Array<'reconcile' | 'stop'>; parent?: { pid?: number; exists?: boolean; identity?: boolean | null; created_at?: string }; child?: { pid?: number; exists?: boolean; identity?: boolean | null; created_at?: string }; marker_state?: string }
export interface PhoneOwnership { phone_id?: string; supported?: boolean; availability: 'free' | 'busy' | 'unknown'; owner_execution_id?: string | null; checked_at?: string; received_at?: string; stale?: boolean; reason_code?: string; message?: string; sync_error?: string; supported_actions?: string[]; owners: PhoneOwner[] }
export interface PhoneControlRequest { request_id: string; execution_id: string; expected_state_revision: number; ownership_token: string }
export interface PhoneControlOperation { operation_id: string; request_id?: string; run_id?: string; execution_id: string; phone_id: string; action: 'reconcile' | 'stop'; status: string; error?: string; ownership?: PhoneOwnership }
export interface PhoneDiagnosis { io_error?: { operation?: string; path?: string; errno?: number; winerror?: number; recovery_candidate?: string }; ownership?: PhoneOwnership; historical_blocker_execution_id?: string; error_kind?: string; phone_id: string; status: string; phase?: string; model_role?: string; reason_code?: string; reason?: string; message?: string; confidence?: 'confirmed' | 'suspected' | 'needs_confirmation'; evidence?: Array<{ source: string; detail: string }>; suggestion?: string; model_call?: { role?: string; model?: string; status?: string; endpoint?: string; timeout_seconds?: number; elapsed_ms?: number; attempt?: number; retry_count?: number; status_code?: number; error_kind?: string; error?: string }; last_activity_at?: string; observed_at?: string; phase_started_at?: string; phase_elapsed_seconds?: number; elapsed_seconds?: number; stale?: boolean; execution_id?: string; case_id?: string; app?: string; expanded_case_id?: string; repeat_index?: number; repeat_times?: number; process_exit_confirmed?: boolean; persistence_error?: string; can_resume?: boolean; requires_confirmation?: boolean }
export interface CollectionDiagnostics { collection_resume?: { version: number; device_config_revision?: number }; phone_control?: { version: number }; phone_control_supported?: boolean; supported: boolean; run_id: string; batch_id?: string; observed_at?: string; stale?: boolean; loading?: boolean; message?: string; phones: PhoneDiagnosis[]; summary: { total: number; healthy: number; warning: number; error: number }; receipt_storage?: FactoryRun['receipt_storage']; persistence_error?: string; control_error?: string; sync_error?: string }
export interface CollectionCaseGroup { case_id: string; collection_case_id?: string; phone_id: string; app: string; task?: string; repeat_times: number; planned_count: number; completed_count: number; failed_count: number; not_run_count: number; active_count: number; received_count: number; attempt_count: number; can_resume?: boolean; latest_diagnostic?: string | { message?: string; reason_code?: string } }
export interface CollectionCasePage { supported: boolean; preparing?: boolean; message?: string; items: CollectionCaseGroup[]; total: number; offset: number; limit: number; scope_counts?: Record<string, number>; filtered_counts?: Record<string, number>; revision?: string | number }
export interface CollectionAttempt { execution_id?: string; attempt_id?: string; status?: string; execution_status?: string; receipt_status?: string; error?: string; message?: string; created_at?: string; finished_at?: string; artifact_id?: string }
export interface CollectionInstance extends CollectionAttempt { repeat_index: number; repeat_times: number; attempt_count: number; attempts: CollectionAttempt[]; active_attempt?: CollectionAttempt | null; current_result?: CollectionAttempt | null }
export interface CollectionInstancePage { supported?: boolean; preparing?: boolean; items: CollectionInstance[]; total: number; revision?: string | number }
export interface ResumePreview { preview_id: string; selection_digest: string; expected_revision: string | number; instances: Array<Record<string, unknown>>; blocked_phones: Array<string | { phone_id: string; reason?: string; message?: string }>; requires_unknown_phone_ids: string[]; counts?: Record<string, number> }
export interface DeviceConfigRevision { adb_path?: string; app_list?: Record<string, string> }
export interface ResumeRequest { device_config_revision?: DeviceConfigRevision; request_id: string; preview_id: string; selection_digest: string; expected_revision: string | number; confirm_unknown_phone_ids?: string[] }
export interface ResumeOperation { operation_id: string; status: string; execution_id?: string; run_id?: string; message?: string; error?: string; phones?: Array<{ phone_id: string; status: string; message?: string }> }
export interface ReportFile { name: string; size: number; modified?: number; file_id?: string }
export interface ReportFolder extends AgentProvenance, CollectionSnapshotMetadata { index: number; dir_name: string; modified?: number; run_id?: string; files: ReportFile[] }
export type FactoryBatchSummary = Omit<CollectionBatchSummary, 'kind' | 'source_job_id'> & CollectionBatchLineage & AgentProvenance & { warnings?: CollectionWarning[]; kind: CollectionBatchSummary['kind'] | 'manual_collection'; source_job_id: string | null }
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
    throw Object.assign(new Error(detail?.message || (typeof detail === 'string' ? detail : '') || payload?.error || `${response.status} ${response.statusText}`), { status: response.status, code: detail?.code || payload?.code })
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
  async function previewSnapshot(path: string, init?: RequestInit): Promise<EvaluationPreview> {
    const value = await (await checked(base + path, init)).json()
    // A failed background job still has a queryable identity and retry action.
    // Do not discard that snapshot merely because it carries an error message.
    if (!value || typeof value.preview_id !== 'string' || typeof value.valid !== 'boolean') throw new Error(value?.error || value?.message || '预览状态响应无效')
    return value
  }
  async function operation(path: string, init?: RequestInit): Promise<ResumeOperation> {
    const value = await (await checked(base + path, init)).json()
    // A durable uncertain operation legitimately carries an error alongside its
    // stable identity. It is a state snapshot, not a failed HTTP envelope.
    if (!value || typeof value.operation_id !== 'string' || typeof value.status !== 'string') throw new Error(value?.error || value?.message || '续跑状态响应无效')
    return value
  }
  async function phoneControlOperation(path: string, init?: RequestInit): Promise<PhoneControlOperation> {
    const value = await (await checked(base + path, init)).json()
    if (!value || typeof value.operation_id !== 'string' || typeof value.status !== 'string' || typeof value.phone_id !== 'string') throw new Error(value?.error || '手机控制响应无效')
    return value
  }
  return {
    phoneOwnership: (id: string, phone: string, signal?: AbortSignal) => req<PhoneOwnership>(`/collection-runs/${encodeURIComponent(id)}/phones/${encodeURIComponent(phone)}/ownership`, { signal }),
    phoneControl: (id: string, phone: string, action: 'reconcile' | 'stop', body: PhoneControlRequest, signal?: AbortSignal) => phoneControlOperation(`/collection-runs/${encodeURIComponent(id)}/phones/${encodeURIComponent(phone)}/${action}`, jsonInit('POST', body, signal)),
    phoneControlOperation: (id: string, request: string, signal?: AbortSignal) => phoneControlOperation(`/collection-runs/${encodeURIComponent(id)}/phone-control-operations/${encodeURIComponent(request)}`, { signal }),
    diagnostics: (id: string, signal?: AbortSignal) => req<CollectionDiagnostics>(`/collection-runs/${encodeURIComponent(id)}/diagnostics`, { signal }),
    casePage: (id: string, page: { offset: number; limit: number; app?: string; phone_id?: string; status?: string; q?: string }, signal?: AbortSignal) => req<CollectionCasePage>(`/collection-runs/${encodeURIComponent(id)}/cases?${new URLSearchParams({ offset: String(page.offset), limit: String(page.limit), app: page.app || '', phone_id: page.phone_id || '', status: page.status || '', q: page.q || '' })}`, { signal }),
    caseInstances: (id: string, caseId: string, phoneId: string, signal?: AbortSignal) => req<CollectionInstancePage>(`/collection-runs/${encodeURIComponent(id)}/cases/${encodeURIComponent(caseId)}/instances?${new URLSearchParams({ phone_id: phoneId })}`, { signal }),
    resumePreview: (id: string, phoneIds: string[], signal?: AbortSignal) => req<ResumePreview>(`/collection-runs/${encodeURIComponent(id)}/resume-preview`, jsonInit('POST', { phone_ids: phoneIds }, signal)),
    resumeCollection: (id: string, body: ResumeRequest, signal?: AbortSignal) => operation(`/collection-runs/${encodeURIComponent(id)}/resume`, jsonInit('POST', body, signal)),
    resumeOperation: (id: string, operationId: string, signal?: AbortSignal) => operation(`/collection-runs/${encodeURIComponent(id)}/resume-operations/${encodeURIComponent(operationId)}`, { signal }),
    evaluationConfig: () => req<EvaluatorConfig>('/evaluation-config'),
    saveEvaluationConfig: (config: EvaluatorConfig) => req<EvaluatorConfig>('/evaluation-config', jsonInit('PUT', config)),
    evaluationCapabilities: () => req<{ ok: boolean; supported: boolean; offline_supported?: boolean; profiles_supported?: boolean; offline_evaluation_jobs?: { version: number }; jobs_supported?: boolean; message: string }>('/evaluation-capabilities'),
    evaluationProfiles: () => req<{ profiles: EvaluationProfile[]; errors: EvaluationIssue[] }>('/evaluation-profiles'),
    evaluationSources: () => req<{ sources: EvaluationSource[]; allowed_roots: string[] }>('/evaluation-sources'),
    evaluationPreview: (source: EvaluationSourceInput, signal?: AbortSignal) => previewSnapshot('/evaluation-preview', jsonInit('POST', source, signal)),
    evaluationPreviewState: (id: string, signal?: AbortSignal, offset = 0, limit = 50) => previewSnapshot(`/evaluation-previews/${encodeURIComponent(id)}?offset=${offset}&limit=${limit}`, { signal }),
    evaluationPreviewControl: (id: string, action: 'cancel' | 'retry', body: EvaluationJobControl, signal?: AbortSignal) => previewSnapshot(`/evaluation-previews/${encodeURIComponent(id)}/${action}`, jsonInit('POST', body, signal)),
    retryEvaluationPreparation: (id: string, body: EvaluationJobControl, signal?: AbortSignal) => req<FactoryRun>(`/runs/${encodeURIComponent(id)}/retry-preparation`, jsonInit('POST', body, signal)),
    artifactPage: (id: string, page: { offset: number; limit: number; search?: string; status?: string }, signal?: AbortSignal) => req<ArtifactPage>(`/collection-runs/${encodeURIComponent(id)}/artifacts?${new URLSearchParams({ offset: String(page.offset), limit: String(page.limit), search: page.search || '', status: page.status || '' })}`, { signal }),
    artifactDetail: (id: string, artifact: string, signal?: AbortSignal, executionId?: string) => req<ArtifactDetail>(`/collection-runs/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(artifact)}${executionId ? `?${new URLSearchParams({ execution_id: executionId })}` : ''}`, { signal }),
    artifactFileUrl: (id: string, artifact: string, path: string, executionId?: string) => `${base}/collection-runs/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(artifact)}/file?${new URLSearchParams({ path, ...(executionId ? { execution_id: executionId } : {}) })}`,
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
    runs: (signal?: AbortSignal) => req<{ runs: FactoryRun[] }>(base === '/api/model-iter' ? '/runs' : '/collection-runs?include_published=true', { signal }),
    cancelCollection: (id: string) => req<FactoryRun>(`/collection-runs/${encodeURIComponent(id)}/cancel`, jsonInit('POST', {})),
    syncRun: (id: string) => req<FactoryRun>(`/collection-runs/${encodeURIComponent(id)}/sync`, jsonInit('POST', {})),
    retryTransfer: (id: string) => req<FactoryRun>(`/runs/${encodeURIComponent(id)}/retry-transfer`, jsonInit('POST', {})),
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
  async list(options: { batch_role?: 'source' | 'collection_result' | 'all'; include_published?: boolean } = {}): Promise<FactoryBatchSummary[]> {
    const role = options.batch_role ?? 'source', includePublished = options.include_published ?? role === 'source'
    const query = new URLSearchParams({ batch_role: role, include_published: String(includePublished) })
    const rows = (await request<{ batches: FactoryBatchSummary[] }>(`/api/phone-factory/batches?${query}`)).batches
    return includePublished ? rows : activeBatchItems(rows)
  },
  detail: (id: string) => request<FactoryBatchDetail>(`/api/phone-factory/batches/${encodeURIComponent(id)}`),
  async workbook(id: string) { return (await checked(`/api/phone-factory/batches/${encodeURIComponent(id)}/workbook`)).blob() },
}


// Storage operations always target the phone-factory gateway, including eval runs.
// Failed durable jobs are valid snapshots and must remain queryable/retryable.
async function storageOperation(path: string, init?: RequestInit): Promise<StorageOperation> {
  const value = await (await checked('/api/phone-factory/storage' + path, init)).json()
  if (!value || typeof value.operation_id !== 'string' || typeof value.status !== 'string') throw new Error(value?.error || '存储操作响应无效')
  return value
}
export const factoryStorageApi = {
  scan: (id: string, requestId: string, signal?: AbortSignal) => storageOperation(`/runs/${encodeURIComponent(id)}/scan`, jsonInit('POST', { request_id: requestId }, signal)),
  cleanup: (id: string, requestId: string, mode: 'retire' | 'cache', signal?: AbortSignal) => storageOperation(`/runs/${encodeURIComponent(id)}/cleanup`, jsonInit('POST', { request_id: requestId, mode }, signal)),
  archive: (id: string, requestId: string, signal?: AbortSignal) => storageOperation(`/runs/${encodeURIComponent(id)}/archive`, jsonInit('POST', { request_id: requestId }, signal)),
  operation: (id: string, signal?: AbortSignal) => storageOperation(`/operations/${encodeURIComponent(id)}`, { signal }),
  archiveDownloadUrl: (id: string) => `${API_BASE}/api/phone-factory/storage/runs/${encodeURIComponent(id)}/archive-download`,
}
