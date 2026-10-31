import type { FactoryRun } from '@/phoneFactoryApi'

export const runStatus = (status: string) => ({ dispatching: '正在下发', queued: '排队中', starting: '启动中', running: '运行中', completed: '已完成', succeeded: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断', partial: '部分完成', ready: '结果已就绪', waiting: '等待回传', syncing: '正在回传', downloading: '正在下载', verified: '已校验', timeout: '超时', retry_wait: '等待重试', remote_failed: '采集无有效轨迹', pending: '等待中' } as Record<string, string>)[status] || status

export function collectionProgressText(run: Partial<FactoryRun>): string {
  const summary = run.collection_summary
  if (!summary || !hasCollectionProgress(run)) return ''
  return `计划 ${summary.planned_trajectory_count} 条 · 采集完成 ${summary.collection_completed_count} · 失败 ${summary.collection_failed_count} · 取消 ${summary.collection_cancelled_count} · 已回传 ${summary.transferred_trajectory_count ?? 0}/${summary.sealed_trajectory_count}`
}

export function hasCollectionProgress(run: Partial<FactoryRun>): boolean {
  const summary = run.collection_summary
  return !!summary && run.execution_mode !== 'evaluate_only' && [summary.original_task_count,
    summary.planned_trajectory_count, summary.collection_completed_count, summary.collection_failed_count,
    summary.collection_cancelled_count, summary.sealed_trajectory_count, summary.diagnostic_count]
    .every(value => Number.isInteger(value) && value >= 0)
}

export function canPreprocessCollection(run: FactoryRun): boolean {
  if (!run.batch_id || run.execution_mode === 'evaluate_only') return false
  if (run.collection_artifacts_version === 1 || run.execution_mode === 'collect_and_evaluate') return run.transfer_status === 'completed' && (run.collection_summary?.transferred_trajectory_count ?? run.trajectory_count ?? 0) > 0
  return run.status === 'completed'
}

export function runDisplayStatus(run: FactoryRun): string {
  return runStatus(run.status === 'completed' && run.remote_status === 'partial' ? 'partial' : run.status)
}

export function runMessage(run: FactoryRun): string {
  const errors = [...new Set([...(run.errors || []), ...(run.remote_errors || [])]
    .map(item => item.error || item.message).filter(Boolean))].join('；')
  return run.transfer_error || run.dispatch_error || run.error || errors || collectionProgressText(run) || (run.trajectory_count != null ? `${run.trajectory_count} 条轨迹` : runStatus(run.transfer_status || ''))
}

export function canCancelCollectionRun(run: FactoryRun): boolean {
  return ['dispatching', 'queued', 'starting', 'running'].includes(run.status)
}
