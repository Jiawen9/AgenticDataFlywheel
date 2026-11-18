import { request } from '@/api'
import type { CorrectionBatch, CorrectionCotJob, CorrectionCotResponse, CorrectionGroupSummary, CorrectionSession, DatasetRelease, DatasetReleaseCandidate } from '@/types'
import { summaryParams, type SummaryPage, type SummaryQuery } from '@/utils/summaryCache'
import type { CorrectionTaskTree } from '@/utils/correctionTree'

const page = <T>(path: string, query: SummaryQuery) => request<SummaryPage<T>>(`${path}?${summaryParams(query)}`)
export type ReleaseSummary = DatasetRelease & { excel_count?: number; training_count?: number; training_ready_count?: number; availability_checked?: boolean }
export type GroupSummary = CorrectionGroupSummary & { cot_row_count?: number; generated_row_count?: number }
export const readPageApi = {
  correctionBatches: (query: SummaryQuery = {}) => page<CorrectionBatch>('/api/correction/batches', query),
  sessions: (query: SummaryQuery = {}) => page<CorrectionSession>('/api/correction/sessions', query),
  session: async (id: string, reconcile = false) => (await request<{ session: CorrectionSession }>(`/api/correction/sessions/${encodeURIComponent(id)}?view=summary${reconcile ? '&reconcile=true' : ''}`)).session,
  groups: (id: string, query: SummaryQuery = {}) => page<GroupSummary>(`/api/correction/sessions/${encodeURIComponent(id)}/tasks`, query),
  correctionTaskTree: (id: string, task: string, revision?: number) => request<CorrectionTaskTree>(
    `/api/correction/sessions/${encodeURIComponent(id)}/tasks?${summaryParams({ view: 'tree', task_id: task, expected_revision: revision })}`,
  ),
  cot: (id: string, group: string) => request<CorrectionCotResponse>(`/api/correction/sessions/${encodeURIComponent(id)}/cot?group_id=${encodeURIComponent(group)}`),
  cotJobs: (id: string) => page<CorrectionCotJob>('/api/correction/cot-jobs', { session_id: id, status: 'active', limit: 10 }),
  releases: (query: SummaryQuery = {}) => page<ReleaseSummary>('/api/dataset-releases', query),
  candidates: (query: SummaryQuery = {}) => page<DatasetReleaseCandidate>('/api/dataset-releases/candidates', query),
}

