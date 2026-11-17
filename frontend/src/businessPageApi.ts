import { request } from '@/api'
import type { PreprocessingBatch, TaskSummary, TreeRun, TreeRunTask, QualityTaskSummary } from '@/types'
import { summaryParams, type SummaryPage, type SummaryQuery } from '@/utils/summaryCache'

export const batchChoices = (query: SummaryQuery = {}) => request<SummaryPage<PreprocessingBatch>>('/api/trajectory-preprocessing/batches?' + summaryParams(query))
export type TreeCounts = { completed: number | null; stale: number | null; pending: number | null }
export type TaskPage = SummaryPage<TaskSummary> & { tasks: TaskSummary[]; task_ids: string[]; ready_task_ids: string[]; task_count: number; quality_counts: { completed: number; stale: number; pending: number }; tree_counts?: TreeCounts; tree_counts_scope?: 'batch' }
export const preprocessingTaskPage = (query: SummaryQuery) => request<TaskPage>('/api/tasks?' + summaryParams({ ...query, all_ids: true }))
export type QualityPage = SummaryPage<QualityTaskSummary & { quality_status: QualityTaskSummary['status'] }> & TreeRun & { ready_task_ids: string[]; quality_counts: { completed: number; stale: number; pending: number }; quality_revision?: string; selected_task?: TreeRunTask & QualityTaskSummary & { quality_status: QualityTaskSummary['status'] } }
export const qualityTaskPage = (batchId: string, query: SummaryQuery = {}) => request<QualityPage>(`/api/data-batches/${encodeURIComponent(batchId)}/tree?` + summaryParams({ ...query, all_ids: true }))
export const jobPage = <T>(kind: 'quality-jobs' | 'tree-builds', query: SummaryQuery) => request<SummaryPage<T>>(`/api/${kind}?` + summaryParams(query))
