import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api'
import { useBatchQualityWorkspace } from './useBatchQualityWorkspace'
import type { CorrectionRecommendation, PreprocessingBatch, RunQualitySummary, TaskQualityResult, TrajectoryTreeNode, TreeRun } from '@/types'
const task = (id: string, state = 'succeeded') => ({ task_id: id, goal: id, tree_file: '', trajectory_count: 1, original_step_count: 2, tree_step_count: 2, ignored_step_count: 0, action_node_count: 2, tree_status: state })
const built = (batch: string, ids = ['A']): TreeRun => ({ run_id: 'current', batch_id: batch, completed_at: '', model_name: '', task_ids: ids, task_count: ids.length, total_original_steps: ids.length * 2, total_tree_steps: ids.length * 2, tasks: ids.map(id => task(id)) } as TreeRun)
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes }); return { promise, resolve } }
function setup(options: Parameters<typeof useBatchQualityWorkspace>[1] = {}) {
  const client = {
    preprocessingBatches: vi.fn(async () => [{ batch_id: 'one', task_count: 2 }, { batch_id: 'two', task_count: 1 }] as PreprocessingBatch[]),
    batchTree: vi.fn(async (id: string) => built(id)),
    runQuality: vi.fn(async (id: string) => ({ batch_id: id, run_id: 'current', tasks: [] }) as RunQualitySummary),
    correctionRecommendation: vi.fn(async (id: string) => ({ batch_id: id, status: 'blocked', tasks: [] }) as CorrectionRecommendation),
    tree: vi.fn(async (_id: string, taskId: string) => ({ id: 1, label: taskId }) as TrajectoryTreeNode),
    taskQuality: vi.fn(async (id: string, taskId: string) => ({ run_id: id, task_id: taskId }) as TaskQualityResult),
    treeRun: vi.fn(async (id: string) => ({ ...built('two'), run_id: id })),
  }
  const flow = useBatchQualityWorkspace(client, options)
  return { client, flow }
}
describe('business batch quality workspace', () => {
  it('does not cancel an unaffected batch selection while its older batch list arrives', async () => {
    const { client, flow } = setup(), pending = deferred<PreprocessingBatch[]>()
    client.preprocessingBatches.mockReturnValueOnce(pending.promise)
    const request = flow.loadBatches('two')
    await vi.waitFor(() => expect(client.preprocessingBatches).toHaveBeenCalled())
    flow.retireBatches(['one'])
    pending.resolve([{ batch_id: 'one' }, { batch_id: 'two' }] as PreprocessingBatch[])
    await request
    expect(flow.batchId.value).toBe('two')
    expect(flow.currentTree.value?.batch_id).toBe('two')
    expect(flow.batches.value.map(batch => batch.batch_id)).toEqual(['two'])
  })
  it('retirement clears only its batch and rejects delayed details without choosing the next batch', async () => {
    const { client, flow } = setup(), slow = deferred<TrajectoryTreeNode>()
    await flow.loadBatches('one'); client.tree.mockReturnValueOnce(slow.promise)
    const request = flow.viewTree('A'); flow.retireBatches(['one'])
    slow.resolve({ id: 99, label: 'obsolete' } as TrajectoryTreeNode); await request
    expect(flow.batchId.value).toBe(''); expect(flow.tree.value).toBeNull(); expect(flow.currentTree.value).toBeNull()
    expect(flow.batches.value.map(batch => batch.batch_id)).toEqual(['two'])
    await flow.selectBatch('two'); flow.retireBatches(['one'])
    expect(flow.batchId.value).toBe('two'); expect(flow.currentTree.value?.batch_id).toBe('two')
  })
  it('keeps separately processed tasks in the single current batch and computes completed/pending/stale counts', async () => {
    const { client, flow } = setup()
    client.batchTree.mockResolvedValue(built('one', ['A', 'B', 'C']))
    client.runQuality.mockResolvedValue({ run_id: 'current', batch_id: 'one', tasks: [{ task_id: 'A', status: 'succeeded', rubric_ready: true }, { task_id: 'B', status: 'stale', rubric_ready: false }] })
    await flow.loadBatches('one')
    expect(flow.batches.value.map(batch => batch.batch_id)).toEqual(['one', 'two'])
    expect(flow.currentTree.value?.tasks.map(task => task.task_id)).toEqual(['A', 'B', 'C'])
    expect(flow.counts.value).toEqual({ completed: 1, pending: 1, stale: 1 })
    expect(client.batchTree).toHaveBeenCalledWith('one')
    expect(client.runQuality).toHaveBeenCalledWith('one')
  })
  it('rejects a delayed batch summary after switching and clears old scores immediately', async () => {
    const { client, flow } = setup(), slow = deferred<TreeRun>()
    client.batchTree.mockImplementation(id => id === 'one' ? slow.promise : Promise.resolve(built(id, ['B'])))
    const first = flow.selectBatch('one')
    await flow.selectBatch('two')
    slow.resolve(built('one', ['A'])); await first
    expect(flow.batchId.value).toBe('two')
    expect(flow.currentTree.value?.tasks.map(task => task.task_id)).toEqual(['B'])
    expect(flow.recommendation.value?.batch_id).toBe('two')
  })
  it('rejects a stale tree response with the same task ID after a batch switch', async () => {
    const { client, flow } = setup(), slow = deferred<TrajectoryTreeNode>()
    await flow.selectBatch('one')
    client.tree.mockReturnValueOnce(slow.promise)
    const first = flow.viewTree('A')
    await flow.selectBatch('two'); await flow.viewTree('A')
    slow.resolve({ id: 99, label: 'obsolete' } as TrajectoryTreeNode); await first
    expect(flow.tree.value?.id).toBe(1)
    expect(flow.loadingTree.value).toBe(false)
  })
  it('only requests valid task quality and disables tasks whose tree became stale', async () => {
    const { client, flow } = setup()
    client.batchTree.mockResolvedValue({ ...built('one', ['A', 'B']), tasks: [task('A'), task('B', 'stale')] } as TreeRun)
    client.runQuality.mockResolvedValue({ run_id: 'current', tasks: [{ task_id: 'A', status: 'succeeded', rubric_ready: true }, { task_id: 'B', status: 'stale', rubric_ready: false }] })
    await flow.selectBatch('one'); await flow.viewTree('A')
    expect(flow.readyTasks.value.map(task => task.task_id)).toEqual(['A'])
    expect(client.taskQuality).toHaveBeenCalledWith('one', 'A')
    expect(flow.quality.value?.task_id).toBe('A')
  })
  it('resolves an old run link to the current business batch, never to the frozen run', async () => {
    const { client, flow } = setup()
    await flow.loadBatches(undefined, 'old-run')
    expect(client.treeRun).toHaveBeenCalledWith('old-run')
    expect(flow.batchId.value).toBe('two')
    expect(client.batchTree).toHaveBeenCalledWith('two')
    expect(client.batchTree).not.toHaveBeenCalledWith('one')
    expect(flow.currentTree.value?.run_id).toBe('current')
  })
  it.each([410, 404])('does not replace an expired or unknown legacy link with the default batch (%s)', async status => {
    const { client, flow } = setup()
    client.treeRun.mockRejectedValue(new ApiError('该运行已失效，请查看批次当前结果', status))
    await flow.loadBatches(undefined, 'unavailable-run')
    expect(flow.batchId.value).toBe('')
    expect(flow.currentTree.value).toBeNull()
    expect(flow.error.value).toBe('该运行已失效，请查看批次当前结果')
    expect(client.batchTree).not.toHaveBeenCalled()
    expect(client.runQuality).not.toHaveBeenCalled()
  })
  it.each(['missing-batch', ''])('does not default an explicitly requested invalid batch (%s)', async id => {
    const { client, flow } = setup()
    await flow.loadBatches(id)
    expect(flow.batchId.value).toBe('')
    expect(flow.error.value).toContain('指定批次不存在')
    expect(client.treeRun).not.toHaveBeenCalled()
    expect(client.batchTree).not.toHaveBeenCalled()
  })
  it('clears a previously loaded batch when an old link is rejected', async () => {
    const { client, flow } = setup()
    await flow.loadBatches('one')
    client.batchTree.mockClear(); client.runQuality.mockClear()
    client.treeRun.mockRejectedValue(new ApiError('expired', 410))
    await flow.loadBatches(undefined, 'expired')
    expect(flow.batchId.value).toBe('')
    expect(flow.currentTree.value).toBeNull()
    expect(flow.recommendation.value).toBeNull()
    expect(client.batchTree).not.toHaveBeenCalled()
    expect(client.runQuality).not.toHaveBeenCalled()
  })
  it('ignores a delayed alias resolution after the user selects a different batch', async () => {
    const { client, flow } = setup(), slow = deferred<TreeRun>()
    client.treeRun.mockReturnValue(slow.promise)
    const first = flow.loadBatches(undefined, 'slow')
    await vi.waitFor(() => expect(client.treeRun).toHaveBeenCalledWith('slow'))
    await flow.selectBatch('one')
    slow.resolve(built('two')); await first
    expect(flow.batchId.value).toBe('one')
    expect(client.batchTree).not.toHaveBeenCalledWith('two')
  })
  it('reports unavailable current results instead of silently showing stale data', async () => {
    const { client, flow } = setup()
    await flow.selectBatch('one')
    client.batchTree.mockRejectedValue(new Error('service unavailable'))
    await flow.selectBatch('two')
    expect(flow.currentTree.value).toBeNull()
    expect(flow.error.value).toBe('service unavailable')
  })
})


describe('Pipeline quality waiting', () => {
  it('does not request missing upstream artifacts, and loads each result only after readiness', async () => {
    let treePending = true, qualityPending = true
    const { client, flow } = setup({ waitingForTree: () => treePending, waitingForQuality: () => qualityPending })
    await flow.loadBatches('one')
    expect(flow.batchId.value).toBe('one')
    expect(flow.error.value).toBe('')
    expect(client.batchTree).not.toHaveBeenCalled()
    expect(client.runQuality).not.toHaveBeenCalled()
    treePending = false; await flow.refresh()
    expect(client.batchTree).toHaveBeenCalledWith('one')
    expect(client.runQuality).not.toHaveBeenCalled()
    expect(flow.currentTree.value?.batch_id).toBe('one')
    qualityPending = false; await flow.refresh()
    expect(client.runQuality).toHaveBeenCalledWith('one')
    expect(client.correctionRecommendation).toHaveBeenCalledWith('one')
    flow.dispose()
  })
})


it('keeps historical Pipeline results separate from a now-modified active batch', async () => {
  const { client, flow } = setup({ historyOnly: () => true })
  await flow.loadBatches('one'); await flow.refreshChoices()
  expect(client.preprocessingBatches).not.toHaveBeenCalled()
  expect(client.batchTree).not.toHaveBeenCalled()
  expect(client.runQuality).not.toHaveBeenCalled()
  expect(flow.currentTree.value).toBeNull()
  expect(flow.error.value).toBe('')
  flow.dispose()
})
