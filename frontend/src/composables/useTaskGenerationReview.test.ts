import { afterEach, describe, expect, it, vi } from 'vitest'
import type { TaskGenerationJob, TaskGenerationResult } from '@/types'
import { useTaskGenerationReview } from './useTaskGenerationReview'

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value))
const job = (id: string, status: TaskGenerationJob['status'] = 'succeeded'): TaskGenerationJob => ({ job_id: id, kind: 'task_generation', status, stage: status, created_at: '2026-09-07', started_at: null, completed_at: null, current_item: null, completed_items: 1, total_items: 1, percent: 100, generate_n: 5, result_count: 2, errors: [], warnings: [], error: null, execution_units: [{ execution_unit_id: 'u', task_type_id: 't', scene: '旧场景', capability: '能力', sub_capability: '类型', app: '甲' }] })
const rows: TaskGenerationResult[] = ['pre', 'main'].map((id, i) => ({ result_id: id, task_uuid: id, dependency_group_id: 'g', pre_dependency: i ? 'weak' : 'pre_node', pre_task_uuid: i ? 'pre' : null, task: id, app: '甲', scene: '场景', capability: '能力', sub_capability: '类型', deleted: false }))
const cleanups: Array<() => void> = []
function setup() {
  const api = {
    taskGenerationJobs: vi.fn(async () => [job('A'), job('B')]),
    taskGenerationJob: vi.fn(async (id: string) => job(id)),
    taskGenerationResults: vi.fn(async () => ({ results: clone(rows), errors: [] })),
    patchTaskGenerationResult: vi.fn(async (_job: string, id: string, patch: { task?: string; deleted?: boolean }) => ({ ...clone(rows.find(r => r.result_id === id)!), ...patch })),
    taskGenerationExport: vi.fn(async () => ({ filename: 'out.xlsx', download_url: '', created_at: '', row_count: 2 })),
  }
  const feedback = { error: vi.fn(), decide: vi.fn(async (): Promise<'save' | 'discard' | 'cancel'> => 'save'), confirmDelete: vi.fn(async () => true) }
  const ws = useTaskGenerationReview(api, feedback)
  cleanups.push(ws.dispose)
  return { ws, api, feedback }
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes }); return { promise, resolve } }
afterEach(() => { cleanups.splice(0).forEach(dispose => dispose()); vi.useRealTimers() })

describe('generation records and protected review', () => {
  it('loads persisted summaries and job detail without polling completed jobs', async () => {
    vi.useFakeTimers()
    const { ws, api } = setup()
    await ws.refreshJobs()
    expect(ws.selectedJob.value?.job_id).toBe('A')
    expect(ws.results.value).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(5000)
    expect(api.taskGenerationJobs).toHaveBeenCalledTimes(1)
  })
  it('polls active jobs, preserves snapshots and loads results when finished', async () => {
    vi.useFakeTimers()
    const { ws, api } = setup()
    api.taskGenerationJob.mockResolvedValue(job('A', 'running'))
    api.taskGenerationJobs.mockResolvedValue([{ ...job('A', 'running'), execution_units: undefined }])
    await ws.refreshJobs()
    expect(api.taskGenerationResults).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1500)
    expect(ws.selectedJob.value?.execution_units?.[0]?.scene).toBe('旧场景')
    api.taskGenerationJobs.mockResolvedValue([job('A')]); api.taskGenerationJob.mockResolvedValue(job('A'))
    await vi.advanceTimersByTimeAsync(1500)
    expect(ws.results.value).toHaveLength(2)
    const calls = api.taskGenerationJobs.mock.calls.length
    await vi.advanceTimersByTimeAsync(5000)
    expect(api.taskGenerationJobs).toHaveBeenCalledTimes(calls)
  })
  it('discards stale detail/results responses during rapid switching', async () => {
    const { ws, api } = setup()
    const old = deferred<{ results: TaskGenerationResult[]; errors: [] }>()
    api.taskGenerationResults.mockImplementation(id => id === 'A' ? old.promise : Promise.resolve({ results: [{ ...rows[1]!, task: 'B的任务' }], errors: [] }))
    const first = ws.selectJob(job('A'))
    await vi.waitFor(() => expect(api.taskGenerationResults).toHaveBeenCalled())
    await ws.selectJob(job('B'))
    old.resolve({ results: clone(rows), errors: [] }); await first
    expect(ws.selectedJob.value?.job_id).toBe('B')
    expect(ws.results.value[0]!.task).toBe('B的任务')
  })
  it('keeps text and current job on save failure and cancellation', async () => {
    const { ws, api, feedback } = setup()
    await ws.selectJob(job('A')); await ws.startEdit(ws.results.value[1]!)
    ws.editingText.value = '修正内容'
    api.patchTaskGenerationResult.mockRejectedValue(new Error('保存失败'))
    expect(await ws.selectJob(job('B'))).toBe(false)
    expect(ws.selectedJob.value?.job_id).toBe('A'); expect(ws.editingText.value).toBe('修正内容')
    feedback.decide.mockResolvedValue('cancel')
    expect(await ws.protect()).toBe(false); expect(ws.dirty.value).toBe(true)
  })
  it('saves before switching and captures original job/result identity', async () => {
    const { ws, api } = setup()
    await ws.selectJob(job('A')); await ws.startEdit(ws.results.value[1]!)
    ws.editingText.value = '修改'
    await ws.selectJob(job('B'))
    expect(api.patchTaskGenerationResult).toHaveBeenCalledWith('A', 'main', { task: '修改' })
    expect(ws.selectedJob.value?.job_id).toBe('B'); expect(ws.dirty.value).toBe(false)
  })
  it('discards only on explicit choice and does not PATCH unchanged text', async () => {
    const { ws, api, feedback } = setup()
    await ws.selectJob(job('A')); await ws.startEdit(ws.results.value[1]!)
    expect(await ws.protect()).toBe(true); expect(feedback.decide).not.toHaveBeenCalled()
    ws.editingText.value = '修改'; feedback.decide.mockResolvedValue('discard')
    await ws.startEdit(ws.results.value[0]!)
    expect(ws.editingId.value).toBe('pre'); expect(api.patchTaskGenerationResult).not.toHaveBeenCalled()
  })
  it('deletes and restores the whole group using the existing patch', async () => {
    const { ws, api } = setup()
    await ws.selectJob(job('A'))
    await ws.toggleDeleted(ws.results.value[1]!)
    expect(ws.results.value.every(r => r.deleted)).toBe(true)
    await ws.toggleDeleted(ws.results.value[0]!)
    expect(ws.results.value.every(r => !r.deleted)).toBe(true)
    expect(api.patchTaskGenerationResult).toHaveBeenLastCalledWith('A', 'pre', { deleted: false })
  })
  it('protects export and passes only the job ID, never frontend filters', async () => {
    const { ws, api, feedback } = setup()
    await ws.selectJob(job('A')); await ws.startEdit(ws.results.value[1]!)
    ws.editingText.value = '修改'; feedback.decide.mockResolvedValue('cancel')
    expect(await ws.exportResults()).toBeNull(); expect(api.taskGenerationExport).not.toHaveBeenCalled()
    feedback.decide.mockResolvedValue('save'); await ws.exportResults()
    expect(api.taskGenerationExport).toHaveBeenCalledWith('A'); expect(ws.dirty.value).toBe(false)
  })
  it('offers retry after load failure and leaves empty results explicit', async () => {
    const { ws, api } = setup()
    api.taskGenerationResults.mockRejectedValueOnce(new Error('网络异常'))
    await ws.selectJob(job('A')); expect(ws.detailError.value).toBe('网络异常')
    api.taskGenerationResults.mockResolvedValue({ results: [], errors: [] })
    await ws.selectJob(job('A'), true)
    expect(ws.detailError.value).toBe(''); expect(ws.results.value).toEqual([])
  })
  it('stops polling and ignores outstanding responses after disposal', async () => {
    vi.useFakeTimers()
    const { ws, api } = setup()
    api.taskGenerationJobs.mockResolvedValue([job('A', 'running')]); api.taskGenerationJob.mockResolvedValue(job('A', 'running'))
    await ws.refreshJobs(); ws.dispose()
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.taskGenerationJobs).toHaveBeenCalledTimes(1)
  })
  it('does not apply a delayed response after leaving the page', async () => {
    const { ws, api } = setup()
    const late = deferred<TaskGenerationJob>()
    api.taskGenerationJob.mockReturnValue(late.promise)
    const request = ws.selectJob(job('A'))
    await vi.waitFor(() => expect(api.taskGenerationJob).toHaveBeenCalled())
    ws.dispose(); late.resolve(job('A')); await request
    expect(api.taskGenerationResults).not.toHaveBeenCalled()
    expect(ws.results.value).toEqual([])
  })
  it('does not delete from a different job if the confirmation becomes stale', async () => {
    const { ws, api, feedback } = setup()
    const confirmation = deferred<boolean>()
    feedback.confirmDelete.mockReturnValue(confirmation.promise)
    await ws.selectJob(job('A'))
    const deleting = ws.toggleDeleted(ws.results.value[1]!)
    await vi.waitFor(() => expect(feedback.confirmDelete).toHaveBeenCalled())
    await ws.selectJob(job('B')); confirmation.resolve(true); await deleting
    expect(api.patchTaskGenerationResult).not.toHaveBeenCalled()
  })
})
