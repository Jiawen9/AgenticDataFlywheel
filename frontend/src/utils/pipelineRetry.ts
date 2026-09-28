import type { PipelineRetryInfo, PipelineStatus } from '@/types/pipeline'

/** Uses the server deadline, so a refreshed or backgrounded tab never restarts the countdown. */
export function retryProgress(info: PipelineRetryInfo | null | undefined, status: PipelineStatus, now = Date.now()): string {
  if (!info || !info.attempts || !info.max_attempts) return ''
  const next = Math.min(info.max_attempts, info.attempts + 1)
  if (status === 'retry_waiting') {
    const remaining = info.next_retry_at ? Math.max(0, Math.ceil((Date.parse(info.next_retry_at) - now) / 1000)) : 0
    return remaining > 0
      ? `临时故障，${remaining} 秒后进行第 ${next}/${info.max_attempts} 次尝试`
      : `临时故障，即将进行第 ${next}/${info.max_attempts} 次尝试`
  }
  if (status === 'paused' && info.next_retry_at) return `已暂停自动重试；继续后进行第 ${next}/${info.max_attempts} 次尝试`
  if (status === 'failed') return `已尝试 ${info.attempts}/${info.max_attempts} 次，自动重试已停止；可手动重试`
  if (status === 'running' && info.attempts > 1 && !info.next_retry_at) return `正在进行第 ${info.attempts}/${info.max_attempts} 次尝试`
  return ''
}
