import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AugmentationPreview, AugmentationSeed, TaskGenerationJob, TaskGenerationResult } from '@/types'
import { useAugmentationPreview } from './useAugmentationPreview'

const job = (id: string, status: TaskGenerationJob['status'] = 'awaiting_confirmation'): TaskGenerationJob => ({ job_id: id, kind: 'augmentation', status, stage: status, created_at: '2026-09-08', started_at: null, completed_at: null, current_item: null, completed_items: 1, total_items: 1, percent: 100, generate_n: 5, result_count: 1, errors: [], warnings: [], error: null, knowledge_base_version: `snapshot-${id}` })
const seed = (id: string): AugmentationSeed => ({ seed_id: `seed-${id}`, source_row: 2, app: '爱奇艺', task: `失败用例 ${id}`, scene: '影音', capability: '视频', sub_capability: '搜索', classification_source: 'model', classification_status: 'classified', mapping_status: 'matched', node_path_ids: ['scene', 'capability', 'type', `app-${id}`], generation_status: 'waiting', result_count: 0 })
const preview = (id: string, includeTree = true): AugmentationPreview => ({ job_id: id, available: true, tree: includeTree ? { version: `snapshot-${id}`, scenes: [], leaf_count: 0, execution_unit_count: 0, warnings: [] } : undefined, seeds: [seed(id)], stats: { total: 1, matched: 1, unmatched: 0, classification_failed: 0, eligible: 1 } })
const row = (id: string): TaskGenerationResult => ({ result_id: `result-${id}`, seed_id: `seed-${id}`, app: '爱奇艺', scene: '影音', capability: '视频', sub_capability: '搜索', task: `变体 ${id}`, source_task: `失败用例 ${id}`, deleted: false })
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes }); return { promise, resolve } }
const cleanups: Array<() => void> = []
function setup() {
  const api = {
    taskGenerationJobs: vi.fn(async () => [job('A'), job('B')]),
    taskGenerationJob: vi.fn(async (id: string) => job(id)),
    augmentationPreview: vi.fn(async (id: string, includeTree = true) => preview(id, includeTree)),
    createAugmentation: vi.fn(async (_file: File, _count: number, _autoStart = true) => job('new')),
    startAugmentation: vi.fn(async (id: string) => job(id, 'running')),
    taskGenerationResults: vi.fn(async (id: string) => ({ results: [row(id)], errors: [] })),
    patchTaskGenerationResult: vi.fn(async (id: string, _rowId: string, patch: { task?: string; deleted?: boolean }) => ({ ...row(id), ...patch })),
    taskGenerationExport: vi.fn(async () => ({ filename: 'out.xlsx', download_url: '', created_at: '', row_count: 1 })),
  }
  const feedback = { error: vi.fn(), decide: vi.fn(async (): Promise<'save' | 'discard' | 'cancel'> => 'save'), confirmDelete: vi.fn(async () => true) }
  const state = useAugmentationPreview(api, feedback)
  cleanups.push(state.dispose)
  return { state, api, feedback }
}
afterEach(() => { cleanups.splice(0).forEach(dispose => dispose()); vi.useRealTimers() })

describe('augmentation snapshot and confirmation lifecycle', () => {
  it('protects edits before an external collection submission and holds the shared page lock', async () => {
    const { state, api } = setup(), pending = deferred<string>()
    api.taskGenerationJob.mockImplementation(async id => job(id, 'succeeded'))
    await state.selectJob(job('A', 'succeeded')); await state.startEdit(row('A'))
    state.editingText.value = '保存后提交'
    const action = vi.fn(async () => { expect(state.busy.value).toBe(true); return pending.promise })
    const request = state.runExternalAction(action)
    await vi.waitFor(() => expect(action).toHaveBeenCalled())
    expect(api.patchTaskGenerationResult).toHaveBeenCalledWith('A', 'result-A', { task: '保存后提交' })
    expect(await state.selectJob(job('B'))).toBe(false)
    expect(await state.startEdit(row('A'))).toBe(false)
    pending.resolve('frozen'); expect(await request).toBe('frozen')
    expect(state.busy.value).toBe(false)
  })
  it('unlocks a failed external collection submission and discards its completion after disposal', async () => {
    const { state, api } = setup(), pending = deferred<string>()
    api.taskGenerationJob.mockImplementation(async id => job(id, 'succeeded'))
    await state.selectJob(job('A', 'succeeded'))
    await expect(state.runExternalAction(async () => { throw new Error('冻结失败') })).rejects.toThrow('冻结失败')
    expect(state.busy.value).toBe(false)
    const action = vi.fn(async () => pending.promise), request = state.runExternalAction(action)
    await vi.waitFor(() => expect(action).toHaveBeenCalled())
    state.dispose(); pending.resolve('late'); expect(await request).toBeNull()
  })
  it('uploads with auto_start=false and never starts generation while loading the preview', async () => {
    const { state, api } = setup(), file = { name: 'failed.xlsx' } as File
    expect(await state.create(file, 10)).toBe(true)
    expect(api.createAugmentation).toHaveBeenCalledWith(file, 10, false)
    expect(state.selectedJob.value?.job_id).toBe('new')
    expect(state.preview.value?.tree?.version).toBe('snapshot-new')
    expect(api.startAugmentation).not.toHaveBeenCalled()
  })

  it('restores a selected confirmation job and does not keep polling or auto-start it', async () => {
    vi.useFakeTimers()
    const { state, api } = setup()
    await state.loadJobs('B')
    expect(state.selectedJob.value?.job_id).toBe('B')
    expect(state.preview.value?.seeds[0]?.seed_id).toBe('seed-B')
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.taskGenerationJob).toHaveBeenCalledTimes(1)
    expect(api.taskGenerationResults).not.toHaveBeenCalled()
    expect(api.startAugmentation).not.toHaveBeenCalled()
  })

  it('caches the full snapshot during progress polling and loads results only on completion', async () => {
    vi.useFakeTimers()
    const { state, api } = setup()
    api.taskGenerationJob.mockImplementation(async id => job(id, 'running'))
    await state.selectJob(job('A', 'running'))
    expect(api.augmentationPreview).toHaveBeenLastCalledWith('A', true)
    await vi.advanceTimersByTimeAsync(1200)
    expect(api.augmentationPreview).toHaveBeenLastCalledWith('A', false)
    expect(state.preview.value?.tree?.version).toBe('snapshot-A')
    expect(api.taskGenerationResults).not.toHaveBeenCalled()
    api.taskGenerationJob.mockImplementation(async id => job(id, 'succeeded'))
    await vi.advanceTimersByTimeAsync(1200)
    expect(state.results.value).toEqual([row('A')])
    const count = api.taskGenerationJob.mock.calls.length
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.taskGenerationJob).toHaveBeenCalledTimes(count)
  })

  it('reads the preview after the terminal job response so final seed state cannot lag behind', async () => {
    const { state, api } = setup(), pending = deferred<TaskGenerationJob>()
    api.taskGenerationJob.mockReturnValue(pending.promise)
    api.augmentationPreview.mockResolvedValue({ ...preview('A'), seeds: [{ ...seed('A'), generation_status: 'generating' }] })
    const request = state.selectJob(job('A', 'running'))
    await vi.waitFor(() => expect(api.taskGenerationJob).toHaveBeenCalled())
    expect(api.augmentationPreview).not.toHaveBeenCalled()
    api.augmentationPreview.mockResolvedValue({ ...preview('A'), seeds: [{ ...seed('A'), generation_status: 'succeeded', result_count: 5 }] })
    pending.resolve(job('A', 'succeeded')); await request
    expect(state.selectedJob.value?.status).toBe('succeeded')
    expect(state.preview.value?.seeds[0]?.generation_status).toBe('succeeded')
    expect(state.preview.value?.seeds[0]?.result_count).toBe(5)
  })

  it('starts once on explicit confirmation and refuses another start for the running job', async () => {
    vi.useFakeTimers()
    const { state, api } = setup()
    await state.selectJob(job('A'))
    api.taskGenerationJob.mockImplementation(async id => job(id, 'running'))
    expect(await state.start()).toBe(true)
    expect(api.startAugmentation).toHaveBeenCalledWith('A')
    expect(await state.start()).toBe(false)
    expect(api.startAugmentation).toHaveBeenCalledTimes(1)
  })

  it('does not start when all classifications failed', async () => {
    const { state, api } = setup()
    api.augmentationPreview.mockResolvedValue({ ...preview('A'), stats: { total: 1, matched: 0, unmatched: 0, classification_failed: 1, eligible: 0 } })
    await state.selectJob(job('A'))
    expect(await state.start()).toBe(false)
    expect(api.startAugmentation).not.toHaveBeenCalled()
  })

  it('clears the old job immediately and ignores its late preview and result responses', async () => {
    const { state, api } = setup(), old = deferred<AugmentationPreview>()
    api.taskGenerationJob.mockImplementation(async id => job(id, 'succeeded'))
    api.augmentationPreview.mockImplementation((id, includeTree = true) => id === 'A' ? old.promise : Promise.resolve(preview(id, includeTree)))
    const first = state.selectJob(job('A', 'succeeded'))
    await vi.waitFor(() => expect(api.augmentationPreview).toHaveBeenCalledWith('A', true))
    await state.selectJob(job('B', 'succeeded'))
    expect(state.results.value).toEqual([row('B')])
    expect(state.preview.value?.tree?.version).toBe('snapshot-B')
    old.resolve(preview('A')); await first
    expect(state.selectedJob.value?.job_id).toBe('B')
    expect(state.preview.value?.seeds[0]?.seed_id).toBe('seed-B')
    expect(state.results.value).toEqual([row('B')])
  })

  it('keeps historical results when the preview is unavailable and does not substitute the current tree', async () => {
    const { state, api } = setup()
    api.taskGenerationJob.mockImplementation(async id => job(id, 'succeeded'))
    api.augmentationPreview.mockResolvedValue({ ...preview('A', false), available: false, seeds: [] })
    await state.selectJob(job('A', 'succeeded'))
    expect(state.preview.value?.available).toBe(false)
    expect(state.preview.value?.tree).toBeUndefined()
    expect(state.results.value).toEqual([row('A')])
    const exported = await state.exportResults()
    expect(exported?.jobId).toBe('A')
  })

  it('retains readable errors and retries the full tree after a failed initial preview', async () => {
    const { state, api } = setup()
    api.augmentationPreview.mockRejectedValueOnce(new Error('读取失败'))
    await state.selectJob(job('A'))
    expect(state.detailError.value).toContain('读取失败')
    expect(state.loading.value).toBe(false)
    await state.refresh()
    expect(api.augmentationPreview).toHaveBeenLastCalledWith('A', true)
    expect(state.detailError.value).toBe('')
    expect(state.preview.value?.tree?.version).toBe('snapshot-A')
  })

  it('preserves unsaved edits on failure and saves the selected job before exporting all results', async () => {
    const { state, api, feedback } = setup()
    api.taskGenerationJob.mockImplementation(async id => job(id, 'succeeded'))
    await state.selectJob(job('A', 'succeeded')); await state.startEdit(row('A'))
    state.editingText.value = '人工修改'
    api.patchTaskGenerationResult.mockRejectedValueOnce(new Error('保存失败'))
    expect(await state.selectJob(job('B'))).toBe(false)
    expect(state.selectedJob.value?.job_id).toBe('A')
    expect(state.editingText.value).toBe('人工修改')
    expect(feedback.error).toHaveBeenCalledWith('保存失败')
    await state.exportResults()
    expect(api.patchTaskGenerationResult).toHaveBeenLastCalledWith('A', 'result-A', { task: '人工修改' })
    expect(api.taskGenerationExport).toHaveBeenCalledWith('A')
    expect(state.results.value[0]?.task).toBe('人工修改')
  })

  it('ignores a delete confirmation if the user switches jobs before confirming', async () => {
    const { state, api, feedback } = setup(), answer = deferred<boolean>()
    api.taskGenerationJob.mockImplementation(async id => job(id, 'succeeded'))
    feedback.confirmDelete.mockReturnValue(answer.promise)
    await state.selectJob(job('A', 'succeeded'))
    const deleting = state.toggleDeleted(row('A'))
    await vi.waitFor(() => expect(feedback.confirmDelete).toHaveBeenCalled())
    await state.selectJob(job('B', 'succeeded')); answer.resolve(true); await deleting
    expect(api.patchTaskGenerationResult).not.toHaveBeenCalled()
    expect(state.results.value).toEqual([row('B')])
  })

  it('does not edit running results and loads interrupted checkpoints for review', async () => {
    vi.useFakeTimers()
    const { state, api } = setup()
    api.taskGenerationJob.mockImplementation(async id => job(id, 'running'))
    await state.selectJob(job('A', 'running'))
    expect(await state.startEdit(row('A'))).toBe(false)
    await state.toggleDeleted(row('A'))
    expect(await state.exportResults()).toBeNull()
    expect(api.patchTaskGenerationResult).not.toHaveBeenCalled()
    expect(api.taskGenerationExport).not.toHaveBeenCalled()
    api.taskGenerationJob.mockImplementation(async id => job(id, 'interrupted'))
    await state.refresh()
    expect(state.results.value).toEqual([row('A')])
    expect(await state.startEdit(row('A'))).toBe(true)
  })

  it('stops polling and does not apply outstanding responses after disposal', async () => {
    vi.useFakeTimers()
    const { state, api } = setup(), late = deferred<TaskGenerationJob>()
    api.taskGenerationJob.mockReturnValue(late.promise)
    const request = state.selectJob(job('A', 'running'))
    await Promise.resolve(); await Promise.resolve()
    state.dispose(); late.resolve(job('A', 'running')); await request
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.augmentationPreview).not.toHaveBeenCalled()
    expect(api.taskGenerationJob).toHaveBeenCalledTimes(1)
    expect(state.preview.value).toBeNull()
  })
})
