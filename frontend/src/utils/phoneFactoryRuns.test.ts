import { describe, expect, it } from 'vitest'
import { runDisplayStatus, runMessage } from './phoneFactoryRuns'

describe('factory run status and warnings', () => {
  it('shows partial remote results after trajectories are successfully transferred', () => {
    const run = { status: 'completed', remote_status: 'partial', trajectory_count: 2,
      errors: [], remote_errors: [{ error: 'task-a step2: logprobs missing' }] }
    expect(runDisplayStatus(run)).toBe('部分完成')
    expect(runMessage(run)).toBe('task-a step2: logprobs missing')
    expect(run.status).toBe('completed')
    expect(run.trajectory_count).toBe(2)
  })
  it('keeps active and failed transfer status and deduplicates manifest warnings', () => {
    const warning = { error: 'step3: invalid logprobs' }
    expect(runDisplayStatus({ status: 'running', remote_status: 'partial' })).toBe('运行中')
    expect(runDisplayStatus({ status: 'failed', remote_status: 'partial' })).toBe('失败')
    expect(runMessage({ status: 'completed', errors: [warning], remote_errors: [warning] })).toBe(warning.error)
    expect(runMessage({ status: 'running', transfer_error: 'download failed', remote_errors: [warning] })).toBe('download failed')
    expect(runMessage({ status: 'completed', trajectory_count: 2 })).toBe('2 条轨迹')
  })
})
