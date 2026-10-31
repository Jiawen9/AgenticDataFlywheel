import type { EvaluationCase, FactoryRun, ReportFolder } from '@/phoneFactoryApi'

export const factoryRunId = (run: FactoryRun) => run.collection_run_id || run.run_id || ''
export const newestFactoryRuns = (runs: FactoryRun[]) => [...runs].sort((a, b) =>
  (b.created_at || '').localeCompare(a.created_at || '') || factoryRunId(b).localeCompare(factoryRunId(a)))

export function factoryRunErrors(run: FactoryRun): string[] {
  return [...new Set([run.transfer_error, run.dispatch_error, run.error,
    ...(run.errors || []).map(item => item.error || item.message),
    ...(run.remote_errors || []).map(item => item.error || item.message)].filter((value): value is string => !!value))]
}

export function factoryRunActive(run: FactoryRun): boolean {
  return ['dispatching', 'queued', 'starting', 'running'].includes(run.status)
    || ['waiting', 'syncing', 'pending', 'downloading', 'retry_wait'].includes(run.transfer_status || '')
    || ['queued', 'starting', 'running'].includes(run.collection_status || '')
    || !!(run.evaluation_summary?.evaluation_running || run.evaluation_summary?.evaluation_pending)
}

export function factoryRunAbnormal(run: FactoryRun): boolean {
  return ['failed', 'interrupted', 'partial'].includes(run.status) || run.remote_status === 'partial'
    || ['failed', 'remote_failed'].includes(run.transfer_status || '') || factoryRunErrors(run).length > 0
    || ['failed', 'partial', 'interrupted'].includes(run.collection_status || '')
    || !!run.collection_summary?.collection_failed_count
    || !!(run.evaluation_summary?.evaluation_failed || run.evaluation_summary?.collection_failed)
}

export function evaluationCaseAbnormal(row: EvaluationCase): boolean {
  return row.collection_status === 'failed' || ['failed', 'needs_review', 'collection_failed'].includes(row.evaluation_status)
    || row.verdict === 'fail' || row.verdict === 'partial_success' || !!row.failure_reason || !!row.errors?.length
}

export function evaluationCaseActive(row: EvaluationCase): boolean {
  return ['pending', 'running'].includes(row.collection_status) || ['pending', 'running', 'retry_wait'].includes(row.evaluation_status)
}

export function flatReportFiles(folders: ReportFolder[]) {
  return [...folders].sort((a, b) => (b.modified || 0) - (a.modified || 0) || b.index - a.index)
    .flatMap(folder => folder.files.map(file => ({ folder, file, key: JSON.stringify([folder.run_id || folder.dir_name, file.file_id || file.name]) })))
}

export function caseErrors(row: EvaluationCase): string[] {
  return [...new Set([row.failure_reason, ...(row.errors || []).map(item => typeof item === 'string' ? item : item.error || item.message)].filter((value): value is string => !!value))]
}
