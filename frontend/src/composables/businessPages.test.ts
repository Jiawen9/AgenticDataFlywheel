import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, defineComponent, h, nextTick } from 'vue'
import { useBatchQualityWorkspace } from './useBatchQualityWorkspace'
import { useTrajectoryPreprocessing } from './useTrajectoryPreprocessing'
import { invalidateSummaryCache } from '@/utils/summaryCache'
import { batchChoices, qualityTaskPage, preprocessingTaskPage, jobPage } from '@/businessPageApi'

vi.mock('@/businessPageApi', () => ({ batchChoices: vi.fn(), qualityTaskPage: vi.fn(), preprocessingTaskPage: vi.fn(), jobPage: vi.fn() }))
const envelope = (items: any[], extra = {}) => ({ items, latest: items.slice(0, 3), total: items.length, filtered_total: items.length, scope_counts: { total: items.length, active: 0, abnormal: 0 }, offset: 0, limit: 10, has_more: false, counts_complete: true, index_state: 'ready', ...extra })
const task = (id: string) => ({ task_id: id, goal: id, annotated: true, trajectory_count: 10, step_count: 10, tree_status: 'succeeded', quality_status: 'succeeded', status: 'succeeded', rubric_ready: true, fusion_complete: true })
const summary = (batch: string, page = 0) => envelope([task('task' + page)], { run_id: batch, batch_id: batch, tasks: [task('task' + page)], task_ids: ['task0', 'task1'], ready_task_ids: ['task0', 'task1'], task_count: 2, quality_counts: { completed: 2, pending: 0, stale: 0 }, revision: 'r1', completed_at: '', model_name: '' })
const apps: { unmount(): void }[] = []
function mount<T>(setup: () => T) {
  let flow!: T
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const app = renderer.createApp(defineComponent({ setup() { flow = setup(); return () => h('div') } })); app.mount({}); apps.push(app)
  return flow
}
const flush = async () => { for (let i = 0; i < 5; i++) { await Promise.resolve(); await nextTick() } }
const deferred = <T,>() => { let resolve!: (v: T) => void; const promise = new Promise<T>(yes => { resolve = yes }); return { promise, resolve } }
beforeEach(() => {
  vi.clearAllMocks(); invalidateSummaryCache()
  vi.stubGlobal('document', Object.assign(new EventTarget(), { hidden: false })); vi.stubGlobal('window', new EventTarget()); vi.stubGlobal('BroadcastChannel', undefined)
  const storage: Record<string, string> = {}
  vi.stubGlobal('sessionStorage', { getItem: (key: string) => storage[key], setItem: (key: string, value: string) => { storage[key] = value }, removeItem: (key: string) => { delete storage[key] } })
  vi.mocked(batchChoices).mockImplementation(async q => envelope([{ batch_id: String(q?.batch_id || 'b1'), annotation_version: 'a1', kind: 'manual_collection' }]) as any)
  vi.mocked(qualityTaskPage).mockImplementation(async id => summary(id) as any)
  vi.mocked(preprocessingTaskPage).mockImplementation(async q => summary(String(q.batch_id)) as any)
  vi.mocked(jobPage).mockResolvedValue(envelope([]))
})
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); invalidateSummaryCache(); vi.unstubAllGlobals() })
function qualityClient() {
  return { preprocessingBatches: vi.fn(), batchTree: vi.fn(), runQuality: vi.fn(), correctionRecommendation: vi.fn(),
    tree: vi.fn(async () => ({ id: 0, children: [] })), taskQuality: vi.fn(async () => ({ evaluations: {} })), treeRun: vi.fn() } as any
}
describe('business page summary loading', () => {
  it('opens only the first list and waits for an explicit batch selection', async () => {
    const client = qualityClient(), flow = mount(() => useBatchQualityWorkspace(client, { summaryPages: true }))
    await flow.loadBatches(); await flush()
    expect(batchChoices).toHaveBeenCalled(); expect(flow.batchId.value).toBe('')
    expect(qualityTaskPage).not.toHaveBeenCalled(); expect(client.batchTree).not.toHaveBeenCalled(); expect(client.correctionRecommendation).not.toHaveBeenCalled()
    flow.dispose()
  })
  it('resolves an explicit batch outside the current page and loads only its summaries', async () => {
    const client = qualityClient(), flow = mount(() => useBatchQualityWorkspace(client, { summaryPages: true }))
    await flow.loadBatches('b-deep')
    expect(batchChoices).toHaveBeenCalledWith({ batch_id: 'b-deep', limit: 1 })
    expect(flow.batchId.value).toBe('b-deep'); expect(flow.readyTaskIds.value).toEqual(['task0', 'task1'])
    expect(client.tree).not.toHaveBeenCalled(); expect(client.correctionRecommendation).not.toHaveBeenCalled()
    await flow.viewTree('task0'); expect(client.tree).toHaveBeenCalledTimes(1)
    await flow.refresh(); expect(client.tree).toHaveBeenCalledTimes(1)
    flow.dispose()
  })
  it('resolves a legacy tree-run deep link outside the current batch page', async () => {
    const client = qualityClient(), flow = mount(() => useBatchQualityWorkspace(client, { summaryPages: true }))
    client.treeRun.mockResolvedValue({ run_id: 'legacy', batch_id: 'b-page-two' })
    await flow.loadBatches(undefined, 'legacy')
    expect(flow.batchId.value).toBe('b-page-two')
    expect(batchChoices).toHaveBeenCalledWith({ batch_id: 'b-page-two', limit: 1 })
    expect(flow.error.value).toBe('')
    flow.dispose()
  })
  it('a version observed while paging invalidates old evidence and refreshes the opened task', async () => {
    const client = qualityClient(), flow = mount(() => useBatchQualityWorkspace(client, { summaryPages: true }))
    client.tree.mockResolvedValueOnce({ id: 1, label: 'old' })
    await flow.loadBatches('b1'); await flow.viewTree('task0')
    const slow = deferred<any>()
    client.tree.mockReturnValueOnce(slow.promise)
    vi.mocked(qualityTaskPage).mockResolvedValue({ ...summary('b1', 1), revision: 'r2', selected_task: task('task0') } as any)
    const paging = flow.changeTaskPage(2)
    await flush()
    expect(flow.tree.value).toBeNull(); expect(flow.quality.value).toBeNull()
    slow.resolve({ id: 2, label: 'new' }); await paging
    expect(flow.tree.value?.label).toBe('new')
    const calls = client.tree.mock.calls.length
    await flow.refresh()
    expect(client.tree).toHaveBeenCalledTimes(calls)
    expect(flow.selectedTaskId.value).toBe('task0')
    flow.dispose()
  })
  it('refreshes changed quality versions even when their summary counts are identical', async () => {
    const client = qualityClient(), flow = mount(() => useBatchQualityWorkspace(client, { summaryPages: true }))
    vi.mocked(qualityTaskPage).mockResolvedValue({ ...summary('b1'), quality_revision: 'q1' } as any)
    await flow.loadBatches('b1'); await flow.viewTree('task0')
    const slow = deferred<any>(), originalTree = flow.tree.value
    client.taskQuality.mockReturnValueOnce(slow.promise)
    vi.mocked(qualityTaskPage).mockResolvedValue({ ...summary('b1', 1), quality_revision: 'q2', selected_task: task('task0') } as any)
    const paging = flow.changeTaskPage(2); await flush()
    expect(flow.quality.value).toBeNull(); expect(flow.tree.value).toBe(originalTree)
    slow.resolve({ evaluations: { current: { global_score: 4 } } }); await paging
    expect(flow.quality.value?.evaluations).toHaveProperty('current')
    flow.dispose()
  })
  it('discards an older page response and keeps the opened tree while paging', async () => {
    const client = qualityClient(), flow = mount(() => useBatchQualityWorkspace(client, { summaryPages: true }))
    await flow.loadBatches('b1'); await flow.viewTree('task0')
    const prior = flow.tree.value, slow = deferred<any>()
    vi.mocked(qualityTaskPage).mockReturnValueOnce(slow.promise).mockResolvedValueOnce(summary('b1', 2) as any)
    const first = flow.changeTaskPage(2); await flow.changeTaskPage(3)
    slow.resolve(summary('b1', 1)); await first
    expect(flow.currentTree.value?.tasks[0]?.task_id).toBe('task2')
    expect(flow.tree.value).toBe(prior); expect(flow.selectedTaskId.value).toBe('task0')
    flow.dispose()
  })
  it('keeps existing summaries after a failed refresh', async () => {
    const flow = mount(() => useBatchQualityWorkspace(qualityClient(), { summaryPages: true }))
    await flow.loadBatches('b1')
    const prior = flow.currentTree.value
    vi.mocked(qualityTaskPage).mockRejectedValueOnce(new Error('temporary network failure'))
    await flow.refresh(); expect(flow.currentTree.value).toBe(prior); expect(flow.error.value).toContain('temporary')
    flow.dispose()
  })
  it('preprocessing waits to read source bodies and retains all task ids across pages', async () => {
    const client = { tasks: vi.fn(), builds: vi.fn(), collectionSourceRuns: vi.fn(async () => []), collectionSourceTasks: vi.fn(async () => []), createBuild: vi.fn(async () => ({ job_id: 'j1', status: 'succeeded', batch_id: 'b1' })) } as any
    const flow = mount(() => useTrajectoryPreprocessing(client, async () => true, { summaryPages: true }))
    await flow.loadBatches('b1')
    expect(client.tasks).not.toHaveBeenCalled(); expect(client.collectionSourceRuns).not.toHaveBeenCalled()
    expect(flow.allTaskIds.value).toEqual(['task0', 'task1'])
    flow.selectedTasks.value = [...flow.allTaskIds.value]
    await flow.changeTaskPage(2)
    await flow.submitBuild()
    expect(client.createBuild).toHaveBeenCalledWith(['task0', 'task1'], { batch_id: 'b1', annotation_version: 'a1' })
    await flow.loadSources(); expect(client.collectionSourceRuns).toHaveBeenCalledWith('b1')
    flow.dispose()
  })
  it('adopts a newer preprocessing input observed by background list refresh', async () => {
    const client = { tasks: vi.fn(), builds: vi.fn(), collectionSourceRuns: vi.fn(), collectionSourceTasks: vi.fn() } as any
    const flow = mount(() => useTrajectoryPreprocessing(client, async () => true, { summaryPages: true }))
    await flow.loadBatches('b1')
    flow.trajectoryData['task0/old'] = { trajectory_id: 'old' } as any
    vi.mocked(batchChoices).mockImplementation(async q => envelope([{ batch_id: String(q?.batch_id || 'b1'), annotation_version: 'a2', kind: 'manual_collection' }]) as any)
    await flow.choices!.reload()
    await vi.waitFor(() => expect(flow.annotationVersion.value).toBe('a2'))
    expect(flow.trajectoryData).toEqual({})
    expect(preprocessingTaskPage).toHaveBeenLastCalledWith(expect.objectContaining({ batch_id: 'b1', annotation_version: 'a2' }))
    flow.dispose()
  })
  it('preserves manual edits when a newer preprocessing version is declined', async () => {
    const client = { tasks: vi.fn(), builds: vi.fn(), collectionSourceRuns: vi.fn(), collectionSourceTasks: vi.fn() } as any
    const protect = vi.fn(async () => true)
    const flow = mount(() => useTrajectoryPreprocessing(client, protect, { summaryPages: true }))
    await flow.loadBatches('b1')
    flow.trajectoryData['task0/old'] = { trajectory_id: 'old' } as any
    protect.mockResolvedValue(false)
    vi.mocked(batchChoices).mockImplementation(async q => envelope([{ batch_id: String(q?.batch_id || 'b1'), annotation_version: 'a2', kind: 'manual_collection' }]) as any)
    await flow.choices!.reload()
    await vi.waitFor(() => expect(flow.error.value).toContain('保留当前编辑'))
    expect(flow.annotationVersion.value).toBe('a1'); expect(flow.trajectoryData['task0/old']).toBeTruthy()
    const prompts = protect.mock.calls.length
    await flow.choices!.reload(); await flush()
    expect(protect).toHaveBeenCalledTimes(prompts)
    protect.mockResolvedValue(true)
    await flow.loadBatches()
    expect(flow.annotationVersion.value).toBe('a2'); expect(flow.trajectoryData).toEqual({})
    flow.dispose()
  })
})
