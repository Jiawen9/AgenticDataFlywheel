import { describe, expect, it } from 'vitest'
import type { FactoryRun } from '@/phoneFactoryApi'
import { runDisplayStatus, runMessage, canCancelCollectionRun, canPreprocessCollection } from './phoneFactoryRuns'

describe('factory run status and warnings', () => {
  it('only offers stop for active collection states including durable starting', () => {
    for (const status of ['dispatching', 'queued', 'starting', 'running']) expect(canCancelCollectionRun({ status })).toBe(true)
    for (const status of ['completed', 'partial', 'failed', 'interrupted', 'cancelled']) expect(canCancelCollectionRun({ status })).toBe(false)
    expect(runDisplayStatus({ status: 'starting' })).toBe('启动中')
  })
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

it('uses execution reconciliation rather than local failure or transfer status for stop', () => {
  expect(canCancelCollectionRun({ status: 'failed', can_cancel_collection: true })).toBe(true)
  expect(canCancelCollectionRun({ status: 'running', collection_status: 'cancelled', can_cancel_collection: false })).toBe(false)
  expect(runDisplayStatus({ status: 'running', collection_status: 'cancelled', transfer_status: 'downloading' })).toBe('已取消')
})
it('requires settled qualified input while allowing a stable partial receipt', () => {
  const run = { status: 'completed', batch_id: 'b', receipt_policy_version: 2, receipt_closed: true, transfer_status: 'partial', collection_summary: { preprocessing_eligible_count: 1 } } as any
  expect(canPreprocessCollection(run)).toBe(true)
  expect(canPreprocessCollection({ ...run, receipt_closed: false })).toBe(false)
  expect(canPreprocessCollection({ ...run, collection_summary: { preprocessing_eligible_count: 0 } })).toBe(false)
})

it('keeps saved platform preprocessing available after collector cleanup while disabling collector stop', () => {
  const run = { status: 'failed', batch_id: 'b', collector_data_state: 'retired' as const, can_cancel_collection: true, receipt_policy_version: 2, receipt_closed: true, transfer_status: 'completed', collection_summary: { preprocessing_eligible_count: 2 } } as FactoryRun
  expect(canCancelCollectionRun(run)).toBe(false)
  expect(canPreprocessCollection(run)).toBe(true)
})
