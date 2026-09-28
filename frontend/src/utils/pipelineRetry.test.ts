import { describe, expect, it } from 'vitest'
import { retryProgress } from '@/utils/pipelineRetry'
import type { PipelineRetryInfo } from '@/types/pipeline'

const now = Date.parse('2026-09-23T04:00:00Z')
const info = (attempts: number, delay: number | null): PipelineRetryInfo => ({
  attempts, max_attempts: 3, last_error: 'HTTP 500 · 请求 r-1',
  next_retry_at: delay === null ? null : new Date(now + delay * 1000).toISOString(),
})

describe('Pipeline retry progress', () => {
  it('counts down from the persisted deadline and advances the attempt label', () => {
    expect(retryProgress(info(1, 30), 'retry_waiting', now)).toBe('临时故障，30 秒后进行第 2/3 次尝试')
    expect(retryProgress(info(1, 30), 'retry_waiting', now + 1100)).toBe('临时故障，29 秒后进行第 2/3 次尝试')
    expect(retryProgress(info(2, 120), 'retry_waiting', now)).toBe('临时故障，120 秒后进行第 3/3 次尝试')
    expect(retryProgress(info(2, 120), 'retry_waiting', now + 120001)).toBe('临时故障，即将进行第 3/3 次尝试')
  })

  it('keeps pause and exhausted failure distinct from an active attempt', () => {
    expect(retryProgress(info(1, 30), 'paused', now)).toBe('已暂停自动重试；继续后进行第 2/3 次尝试')
    expect(retryProgress(info(3, null), 'failed', now)).toBe('已尝试 3/3 次，自动重试已停止；可手动重试')
    expect(retryProgress(info(2, null), 'running', now)).toBe('正在进行第 2/3 次尝试')
    expect(retryProgress(undefined, 'failed', now)).toBe('')
  })
})
