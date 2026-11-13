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
  it('does not race a background detail refresh against an explicit tree selection', async () => {
    const { client, flow } = setup(), slow = deferred<TrajectoryTreeNode>()
    await flow.selectBatch('one')
    client.tree.mockReturnValueOnce(slow.promise)
    const selected = flow.viewTree('A')
    await flow.refresh()
    expect(client.tree).toHaveBeenCalledTimes(1)
    expect(flow.loadingTree.value).toBe(true)
    slow.resolve({ id: 1, label: 'selected-tree' } as TrajectoryTreeNode); await selected
    expect(flow.tree.value?.label).toBe('selected-tree')
    expect(flow.loadingTree.value).toBe(false)
  })
  it('keeps the mounted tree and current task while refreshing scores', async () => {
    const { client, flow } = setup(), slow = deferred<TreeRun>()
    client.runQuality.mockResolvedValue({ run_id: 'current', tasks: [{ task_id: 'A', status: 'succeeded', rubric_ready: true }] })
    await flow.selectBatch('one'); await flow.viewTree('A')
    const tree = flow.tree.value, quality = flow.quality.value
    client.batchTree.mockReturnValueOnce(slow.promise)
    const request = flow.refresh()
    expect(flow.tree.value).toBe(tree); expect(flow.quality.value).toBe(quality)
    expect(flow.selectedTaskId.value).toBe('A'); expect(flow.loadingTree.value).toBe(false)
    slow.resolve(built('one')); await request
    expect(flow.tree.value?.label).toBe('A'); expect(flow.selectedTaskId.value).toBe('A')
  })
  it('does not steal a task selected while a background refresh is pending', async () => {
    const { client, flow } = setup(), slow = deferred<TreeRun>()
    client.batchTree.mockResolvedValue(built('one', ['A', 'B']))
    await flow.selectBatch('one'); await flow.viewTree('A')
    client.batchTree.mockReturnValueOnce(slow.promise)
    const request = flow.refresh()
    await flow.viewTree('B')
    slow.resolve(built('one', ['A', 'B'])); await request
    expect(flow.selectedTaskId.value).toBe('B'); expect(flow.tree.value?.label).toBe('B')
  })
  it('keeps prior data on refresh failure and rejects an older refresh detail', async () => {
    const { client, flow } = setup(), slow = deferred<TrajectoryTreeNode>()
    await flow.selectBatch('one'); await flow.viewTree('A')
    const current = flow.tree.value
    client.batchTree.mockRejectedValueOnce(new Error('temporary connection failure'))
    await flow.refresh()
    expect(flow.tree.value).toBe(current); expect(flow.error.value).toContain('temporary')
    client.tree.mockReturnValueOnce(slow.promise)
    const request = flow.refresh()
    await vi.waitFor(() => expect(client.tree).toHaveBeenCalledTimes(2))
    await flow.selectBatch('two'); await flow.viewTree('A')
    slow.resolve({ id: 99, label: 'old-tree' } as TrajectoryTreeNode); await request
    expect(flow.batchId.value).toBe('two'); expect(flow.tree.value?.id).toBe(1)
  })
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
  it('rejects late fusion and Q scores from another batch with the same task ID', async () => {
    const { client, flow } = setup(), slow = deferred<TaskQualityResult>()
    client.runQuality.mockImplementation(async () => ({ run_id: 'current', tasks: [{ task_id: 'A', status: 'succeeded', rubric_ready: true }] }))
    await flow.selectBatch('one')
    client.taskQuality.mockReturnValueOnce(slow.promise)
    const old = flow.viewTree('A')
    await flow.selectBatch('two')
    client.taskQuality.mockResolvedValue({ run_id: 'two', task_id: 'A', node_scores: { '0': { q: .9, scored_count: 1, total_count: 1 } } } as TaskQualityResult)
    await flow.viewTree('A')
    slow.resolve({ run_id: 'one', task_id: 'A', node_scores: { '0': { q: .2, scored_count: 1, total_count: 1 } } } as TaskQualityResult)
    await old
    expect(flow.quality.value?.run_id).toBe('two')
    expect(flow.quality.value?.node_scores?.['0']?.q).toBe(.9)
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
  it.each(['running', 'failed', 'interrupted'] as const)('reads explicit partial results in %s state without counting completion', async status => {
    const { client, flow } = setup({ waitingForQuality: () => true })
    client.runQuality.mockResolvedValue({ run_id: 'one', tasks: [{ task_id: 'A', status, rubric_ready: true, fusion_complete: false, partial_results_available: true }] })
    await flow.selectBatch('one'); await flow.viewTree('A')
    expect(client.taskQuality).toHaveBeenCalledWith('one', 'A')
    expect(flow.counts.value.completed).toBe(0)
    expect(client.correctionRecommendation).not.toHaveBeenCalled()
  })
  it('waits for the tree, but reads partial scoring before downstream readiness', async () => {
    let treePending = true, qualityPending = true
    const { client, flow } = setup({ waitingForTree: () => treePending, waitingForQuality: () => qualityPending })
    await flow.loadBatches('one')
    expect(flow.batchId.value).toBe('one')
    expect(flow.error.value).toBe('')
    expect(client.batchTree).not.toHaveBeenCalled()
    expect(client.runQuality).not.toHaveBeenCalled()
    treePending = false; await flow.refresh()
    expect(client.batchTree).toHaveBeenCalledWith('one')
    expect(client.runQuality).toHaveBeenCalledWith('one')
    expect(client.correctionRecommendation).not.toHaveBeenCalled()
    expect(flow.currentTree.value?.batch_id).toBe('one')
    qualityPending = false; await flow.refresh()
    expect(client.runQuality).toHaveBeenCalledWith('one')
    expect(client.correctionRecommendation).toHaveBeenCalledWith('one')
    flow.dispose()
  })

  it('exposes ten partial evaluations and Checklist errors after the Pipeline quality stage fails', async () => {
    const { client, flow } = setup({ waitingForQuality: () => true })
    const sourceError = '质检轨迹缺少可供采集服务只读访问的原件目录'
    const evaluations = Object.fromEntries(Array.from({ length: 10 }, (_, index) => [`tr${index}`, {
      trajectory_id: `tr${index}`, global_score: 2.8548, step_evaluations: [],
      checklist: { status: 'error', error: sourceError },
      fusion: { valid: false, score: null, invalid_reasons: ['checklist_incomplete'] },
    }]))
    client.runQuality.mockResolvedValue({ run_id: 'one', tasks: [{ task_id: 'A', status: 'succeeded', rubric_ready: true, scoring_version: 'checklist_fusion_v1', fusion_complete: false, fusion_eligible_count: 0 }] })
    client.taskQuality.mockResolvedValue({ run_id: 'one', task_id: 'A', evaluations,
      node_scores: { '1': { q: null, scored_count: 0, total_count: 10, status: 'unavailable' } },
    } as TaskQualityResult)
    await flow.selectBatch('one'); await flow.viewTree('A')
    expect(client.taskQuality).toHaveBeenCalledWith('one', 'A')
    expect(Object.keys(flow.quality.value?.evaluations ?? {})).toHaveLength(10)
    expect(flow.quality.value?.evaluations.tr0?.global_score).toBe(2.8548)
    expect(flow.quality.value?.evaluations.tr0?.checklist?.error).toBe(sourceError)
    expect(flow.quality.value?.node_scores?.['1']?.scored_count).toBe(0)
    expect(flow.error.value).toBe('')
    expect(client.correctionRecommendation).not.toHaveBeenCalled()
    expect(flow.recommendation.value).toBeNull()
  })

  it('refreshes a selected task from pending to partial without requiring whole-batch success', async () => {
    const { client, flow } = setup({ waitingForQuality: () => true })
    await flow.selectBatch('one'); await flow.viewTree('A')
    expect(client.taskQuality).not.toHaveBeenCalled()
    expect(flow.error.value).toBe('')
    client.runQuality.mockResolvedValue({ run_id: 'one', tasks: [{ task_id: 'A', status: 'succeeded', rubric_ready: true, fusion_complete: false }] })
    await flow.refresh()
    expect(flow.selectedTaskId.value).toBe('A')
    expect(client.taskQuality).toHaveBeenCalledWith('one', 'A')
    expect(flow.quality.value?.task_id).toBe('A')
    expect(client.correctionRecommendation).not.toHaveBeenCalled()
  })

  it.each([
    [409, '该任务尚无有效质检结果，请先完成质检'],
    [404, '该任务还没有质检结果'],
  ])('keeps the tree when a previously available quality result becomes absent (%s)', async (status, message) => {
    const { client, flow } = setup({ waitingForQuality: () => true })
    client.runQuality.mockResolvedValue({ run_id: 'one', tasks: [{ task_id: 'A', status: 'succeeded', rubric_ready: true }] })
    client.taskQuality.mockRejectedValue(new ApiError(String(message), Number(status)))
    await flow.selectBatch('one'); await flow.viewTree('A')
    expect(flow.tree.value?.id).toBe(1)
    expect(flow.quality.value).toBeNull()
    expect(flow.error.value).toBe('')
  })

  it.each([404, 409, 500])('does not hide unrelated quality request failures (%s)', async status => {
    const { client, flow } = setup({ waitingForQuality: () => true })
    client.runQuality.mockResolvedValue({ run_id: 'one', tasks: [{ task_id: 'A', status: 'succeeded', rubric_ready: true }] })
    client.taskQuality.mockRejectedValue(new ApiError('真实请求错误', status))
    await flow.selectBatch('one'); await flow.viewTree('A')
    expect(flow.tree.value?.id).toBe(1)
    expect(flow.quality.value).toBeNull()
    expect(flow.error.value).toBe('真实请求错误')
  })

  it('ignores a late failed-task detail after switching tasks in the same batch', async () => {
    const { client, flow } = setup({ waitingForQuality: () => true }), old = deferred<TaskQualityResult>()
    client.runQuality.mockResolvedValue({ run_id: 'one', tasks: ['A', 'B'].map(task_id => ({ task_id, status: 'succeeded', rubric_ready: true })) })
    await flow.selectBatch('one')
    client.taskQuality.mockReturnValueOnce(old.promise)
    const first = flow.viewTree('A')
    await flow.viewTree('B')
    old.resolve({ run_id: 'one', task_id: 'A' } as TaskQualityResult); await first
    expect(flow.selectedTaskId.value).toBe('B')
    expect(flow.tree.value?.label).toBe('B')
    expect(flow.quality.value?.task_id).toBe('B')
  })

  it('discards a late recommendation if quality becomes incomplete while it is loading', async () => {
    let waiting = false
    const { client, flow } = setup({ waitingForQuality: () => waiting }), old = deferred<CorrectionRecommendation>()
    client.correctionRecommendation.mockReturnValueOnce(old.promise)
    const loading = flow.selectBatch('one')
    waiting = true
    old.resolve({ batch_id: 'one', status: 'ready', tasks: [] } as unknown as CorrectionRecommendation)
    await loading
    expect(flow.recommendation.value).toBeNull()
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
