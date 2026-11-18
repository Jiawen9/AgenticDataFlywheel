import { request } from './api'
import { newRunRequestId } from './phoneFactoryApi'
import { invalidateSummaryCache } from './utils/summaryCache'
import type { Pipeline, PipelineCollectionConfig } from './types/pipeline'
import type { TaskGenerationJob } from './types'
export interface BadCase { candidate_id: string; source_run_id: string; case_id: string; task: string; app: string; verdicts: string[]; source_count: number; missing_fields: string[]; failures: Array<{ reason?: string; failure_reason?: string; checklist?: unknown; [key: string]: unknown }>; status?: string }
export interface BadCasePage { index_state?: string; final?: boolean; error?: string; items: BadCase[]; total: number; summary: { candidate: number; technical: number; needs_completion: number } }
const post = <T>(path: string, body: unknown) => request<T>(path, { method: 'POST', body: JSON.stringify(body) })
export const pipelineLoopApi = {
  async confirm(id: string, body: { request_id: string; expected_revision: number; collection_config: PipelineCollectionConfig }) {
    const value = await post<{ pipeline: Pipeline }>(`/api/pipelines/${encodeURIComponent(id)}/confirm-generation`, body)
    invalidateSummaryCache(); return value.pipeline
  },
  async confirmBadcases(id: string, body: { run_id: string; candidate_ids: string[]; request_id: string; expected_revision: number }) {
    const value = await post<{ pipeline: Pipeline }>(`/api/pipelines/${encodeURIComponent(id)}/confirm-badcases`, body)
    invalidateSummaryCache()
    return value.pipeline
  },
  refresh(runId: string) { return post('/api/badcases/refresh', { run_id: runId }) },
  badcases(runId: string, offset: number, limit: number, status = 'candidate', signal?: AbortSignal) {
    return request<BadCasePage>('/api/badcases?' + new URLSearchParams({ run_id: runId, offset: String(offset), limit: String(limit), status }), { signal, cache: 'no-store' })
  },
  patch(id: string, value: { task: string; app: string }) { return request<BadCase>(`/api/badcases/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(value) }) },
  feedback(runId: string, candidateIds: string[], requestId: string) { return post<{ feedback_id: string; count: number }>('/api/badcases/feedback', { run_id: runId, candidate_ids: candidateIds, request_id: requestId }) },
  feedbackDetail(id: string) { return request<{ feedback_id: string; seeds: Array<{ task: string; app: string }> }>(`/api/badcases/feedback/${encodeURIComponent(id)}`) },
  createBadcaseJob(value: { feedback_id: string; generate_n: number; request_id: string; pipeline_id?: string; auto_start: boolean }) { return post<TaskGenerationJob>('/api/task-generation/badcase-jobs', value) },
}

const pending = new Map<string, { identity: string; id: string }>()
/** Persist the latest intent per operation, never large input/evidence payloads. */
export function loopRequestId(scope: string, identity: unknown): string {
  const key = 'adf.loop.' + scope, value = JSON.stringify(identity)
  let saved = pending.get(key)
  if (!saved) { try { saved = JSON.parse(sessionStorage.getItem(key) || 'null') || undefined } catch { /* optional storage */ } }
  if (saved?.identity === value) return saved.id
  const entry = { identity: value, id: 'loop_' + newRunRequestId().replaceAll('-', '') }
  pending.set(key, entry)
  try { sessionStorage.setItem(key, JSON.stringify(entry)) } catch { /* memory fallback */ }
  return entry.id
}

/** Forget an acknowledged intent only; a newer in-flight request stays intact. */
export function completeLoopRequest(scope: string, requestId: string): void {
  const key = 'adf.loop.' + scope
  if (pending.get(key)?.id === requestId) pending.delete(key)
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || 'null')
    if (saved?.id === requestId) sessionStorage.removeItem(key)
  } catch { /* optional storage */ }
}
