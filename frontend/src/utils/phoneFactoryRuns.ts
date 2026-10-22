import type { FactoryRun } from '@/phoneFactoryApi'

const runStatus = (status: string) => ({ dispatching: '正在下发', queued: '排队中', running: '运行中', completed: '已完成', succeeded: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断', partial: '部分完成', ready: '结果已就绪', waiting: '等待回传', syncing: '正在回传', pending: '等待中' } as Record<string, string>)[status] || status

export function runDisplayStatus(run: FactoryRun): string {
  return runStatus(run.status === 'completed' && run.remote_status === 'partial' ? 'partial' : run.status)
}

export function runMessage(run: FactoryRun): string {
  const errors = [...new Set([...(run.errors || []), ...(run.remote_errors || [])]
    .map(item => item.error || item.message).filter(Boolean))].join('；')
  return run.transfer_error || run.dispatch_error || run.error || errors || (run.trajectory_count != null ? `${run.trajectory_count} 条轨迹` : runStatus(run.transfer_status || ''))
}
