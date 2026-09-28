import { afterEach, describe, expect, it, vi } from 'vitest'
import { useTrajectoryPreprocessing } from './useTrajectoryPreprocessing'
import type { BuildJob, CollectionSourceTask, CollectionSourceRun, PreprocessingBatch, PreprocessingJob, TrajectoryRecord, TrajectoryScope } from '@/types'

const batch = (id: string, version: string | null = 'v1'): PreprocessingBatch => ({
  batch_id: id, kind: 'existing_trajectories', label: id, task_count: 1, ready_trajectory_count: 1,
  ready_step_count: 2, collection_status: 'ready', preprocessing_status: version ? 'succeeded' : 'not_started',
  latest_job: null, annotation_version: version, artifacts: [], can_start: !version, reason: null,
})
const job = (id = 'a', status: PreprocessingJob['status'] = 'running'): PreprocessingJob => ({
  job_id: 'pre-' + id, batch_id: id, status, stage: status === 'running' ? 'annotating' : status,
  completed_steps: status === 'succeeded' ? 2 : 1, total_steps: 2, current_task: 'same', current_trajectory: 'same-1',
  current_step: 1, percent: status === 'succeeded' ? 100 : 50, error: null, artifacts: [], annotation_version: status === 'succeeded' ? 'v1' : null,
})
const task = (text: string) => ({ task_id: 'same', goal: text, warning: '', first_trajectory: 'same-1', trajectory_count: 1, step_count: 2, annotated: true })
const record = (text = 'click'): TrajectoryRecord => ({ trajectory_id: 'same-1', step_count: 1, steps: [{
  step: 1, excel_row: 2, image: 'same/1.png', image_url: '', xml: '', action_text: text, action: { action: 'click' }, action_summary: text, actions_box: '[0,0,10,10]',
}] })
const buildJob = (id = 'a') => ({ job_id: 'tree-' + id, batch_id: id, annotation_version: 'v1', status: 'running', created_at: '2026-09-15', stage: 'classifying' }) as BuildJob
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes }); return { promise, resolve } }
const disposers: Array<() => void> = []
function setup(protect = vi.fn(async () => true), options: Parameters<typeof useTrajectoryPreprocessing>[2] = {}) {
  const client = {
    preprocessingBatches: vi.fn(async () => [batch('a'), batch('b'), batch('waiting', null)]),
    collectionSourceTasks: vi.fn(async (_id: string) => [] as CollectionSourceTask[]), collectionSourceRuns: vi.fn(async (_id: string) => [] as CollectionSourceRun[]),
    createPreprocessing: vi.fn(async (id: string) => job(id)),
    preprocessingJob: vi.fn(async () => job()), retryPreprocessing: vi.fn(async () => job()),
    tasks: vi.fn(async (scope?: TrajectoryScope) => [task(scope!.batch_id)]),
    trajectories: vi.fn(async (_id: string, _scope?: TrajectoryScope) => ({ task_id: 'same', trajectories: [{ trajectory_id: 'same-1', step_count: 1 }] })),
    trajectory: vi.fn(async (_id: string, _trajectory: string, _scope?: TrajectoryScope) => record()),
    updateBBox: vi.fn(async () => ({ actions_box: '[1,1,11,11]', annotation_version: 'v2' })),
    createBuild: vi.fn(async (_ids: string[], _scope?: TrajectoryScope) => buildJob()),
    build: vi.fn(async () => buildJob()), builds: vi.fn(async () => [] as BuildJob[]),
  }
  const flow = useTrajectoryPreprocessing(client, protect, options)
  disposers.push(flow.dispose)
  return { flow, client, protect }
}
afterEach(() => { disposers.splice(0).forEach(dispose => dispose()); vi.useRealTimers() })

describe('batch preprocessing workspace', () => {
  it('reads imported Rollout task mappings through their own source endpoint before preprocessing', async () => {
    const { flow, client } = setup()
    client.preprocessingBatches.mockResolvedValue([{ ...batch('local-rollout', null), kind: 'rollout_import' }])
    const importedTask = { task_id: 'case-A', collection_case_id: 'case-A', task: 'Imported goal', app: 'Demo' }
    client.collectionSourceTasks.mockResolvedValue([importedTask])
    await flow.loadBatches('local-rollout')
    expect(client.collectionSourceTasks).toHaveBeenCalledWith('local-rollout', 'rollout_import')
    expect(flow.sourceTasks.value).toEqual([importedTask])
    expect(flow.scope.value).toBeNull()
    expect(client.createPreprocessing).not.toHaveBeenCalled()
    expect(client.tasks).not.toHaveBeenCalled()
  })

  it('retires the selected batch without saving drafts and rejects its delayed trajectory', async () => {
    const { flow, client, protect } = setup(), pending = deferred<TrajectoryRecord>()
    await flow.loadBatches('a'); await flow.expandTasks(['same'])
    client.trajectory.mockReturnValueOnce(pending.promise)
    const request = flow.expandTrajectory('same', 'same-1')
    await vi.waitFor(() => expect(client.trajectory).toHaveBeenCalled())
    protect.mockClear(); flow.retireBatches(['a'])
    pending.resolve(record('old')); await request
    expect(flow.batchId.value).toBe(''); expect(flow.scope.value).toBeNull()
    expect(flow.tasks.value).toEqual([]); expect(flow.trajectoryData).toEqual({})
    expect(flow.batches.value.map(batch => batch.batch_id)).toEqual(['b', 'waiting'])
    expect(protect).not.toHaveBeenCalled(); expect(client.updateBBox).not.toHaveBeenCalled()
  })
  it('does not let an older batch refresh erase a newly started job or stop its polling', async () => {
    vi.useFakeTimers()
    const { flow, client } = setup(), pending = deferred<PreprocessingBatch[]>()
    await flow.loadBatches('waiting')
    client.preprocessingBatches.mockReturnValueOnce(pending.promise)
    const staleRefresh = flow.loadBatches()
    expect(await flow.start()).toBe(true)
    pending.resolve([batch('waiting', null)]);await staleRefresh
    expect(flow.preprocessingJob.value?.job_id).toBe('pre-waiting')
    expect(flow.processing.value).toBe(true)
    expect(flow.annotationVersion.value).toBe('')
    client.preprocessingJob.mockResolvedValue(job('waiting'))
    await vi.advanceTimersByTimeAsync(1500)
    expect(client.preprocessingJob).toHaveBeenCalledWith('pre-waiting')
  })
  it('shows frozen source tasks before annotation exists and rejects their late replies after switching', async () => {
    const { flow, client } = setup(), pending = deferred<CollectionSourceTask[]>()
    const source = (id: string) => ({ task_id: id, collection_case_id: id, task: 'source ' + id, app: 'App' })
    client.preprocessingBatches.mockResolvedValue([{ ...batch('a', null), kind: 'task_generation' }, { ...batch('b', null), kind: 'augmentation' }])
    client.collectionSourceTasks.mockImplementation(id => id === 'a' ? pending.promise : Promise.resolve([source(id)]))
    await flow.loadBatches()
    const older = flow.selectBatch('a')
    await vi.waitFor(() => expect(client.collectionSourceTasks).toHaveBeenCalledWith('a'))
    await flow.selectBatch('b')
    pending.resolve([source('a')]);await older
    expect(flow.sourceTasks.value).toEqual([source('b')])
    expect(flow.scope.value).toBeNull()
    expect(client.tasks).not.toHaveBeenCalled()
    expect(client.collectionSourceRuns).toHaveBeenCalledWith('b')
  })
  it('reused successful preprocessing reloads tasks from the newest human annotation version', async () => {
    const { flow, client } = setup()
    client.preprocessingBatches.mockResolvedValue([{ ...batch('a', 'v3'), can_start: true }])
    client.createPreprocessing.mockResolvedValue(job('a', 'succeeded'))
    await flow.loadBatches('a')
    expect(await flow.start()).toBe(true)
    expect(flow.annotationVersion.value).toBe('v3')
    expect(flow.tasks.value).toHaveLength(1)
    expect(client.tasks).toHaveBeenLastCalledWith({ batch_id: 'a', annotation_version: 'v3' })
  })
  it('uses only the explicitly selected batch and pins the annotation version for every read', async () => {
    const { flow, client } = setup()
    await flow.loadBatches()
    expect(client.tasks).not.toHaveBeenCalled()
    await flow.selectBatch('a'); await flow.expandTasks(['same']); await flow.expandTrajectory('same', 'same-1')
    expect(client.tasks).toHaveBeenCalledWith({ batch_id: 'a', annotation_version: 'v1' })
    expect(client.trajectories).toHaveBeenCalledWith('same', { batch_id: 'a', annotation_version: 'v1' })
    expect(client.trajectory).toHaveBeenCalledWith('same', 'same-1', { batch_id: 'a', annotation_version: 'v1' })
    await flow.selectBatch('waiting')
    expect(flow.scope.value).toBeNull()
    expect(flow.tasks.value).toEqual([])
    expect(client.tasks).toHaveBeenCalledTimes(1)
    expect(await flow.selectBatch('unknown')).toBe(false)
    expect(flow.batchId.value).toBe('waiting')
  })
  it('restores server jobs on reopening without starting any work or choosing a localStorage job', async () => {
    const { flow, client } = setup()
    client.preprocessingBatches.mockResolvedValue([{ ...batch('a', null), latest_job: job() }])
    client.builds.mockResolvedValue([buildJob('b'), buildJob('a')])
    await flow.loadBatches('a')
    expect(flow.preprocessingJob.value?.job_id).toBe('pre-a')
    expect(flow.buildJob.value?.job_id).toBe('tree-a')
    expect(client.createPreprocessing).not.toHaveBeenCalled()
    expect(client.createBuild).not.toHaveBeenCalled()
  })
  it('rejects late task data and does not report an obsolete selection as successful', async () => {
    const { flow, client } = setup(), pending = deferred<ReturnType<typeof task>[]>()
    await flow.loadBatches()
    client.tasks.mockImplementation(scope => scope!.batch_id === 'a' ? pending.promise : Promise.resolve([task('b')]))
    const older = flow.selectBatch('a')
    await vi.waitFor(() => expect(client.tasks).toHaveBeenCalledTimes(1))
    expect(await flow.selectBatch('b')).toBe(true)
    pending.resolve([task('a')])
    expect(await older).toBe(false)
    expect(flow.tasks.value[0]?.goal).toBe('b')
  })
  it('rejects late trajectory responses with identical IDs from another batch', async () => {
    const { flow, client } = setup(), pending = deferred<TrajectoryRecord>()
    await flow.loadBatches('a'); await flow.expandTasks(['same'])
    client.trajectory.mockReturnValueOnce(pending.promise)
    const older = flow.expandTrajectory('same', 'same-1')
    await vi.waitFor(() => expect(client.trajectory).toHaveBeenCalledTimes(1))
    await flow.selectBatch('b'); await flow.expandTasks(['same']); await flow.expandTrajectory('same', 'same-1')
    pending.resolve(record('wrong batch')); await older
    expect(flow.trajectoryData['same/same-1']?.steps[0]?.action_text).toBe('click')
  })
  it('cancelling unsaved protection preserves scope, expansion, cache and task selection', async () => {
    const { flow, protect, client } = setup()
    await flow.loadBatches('a'); await flow.expandTasks(['same']); await flow.expandTrajectory('same', 'same-1')
    flow.selectedTasks.value = ['same']
    protect.mockResolvedValue(false)
    expect(await flow.selectBatch('b')).toBe(false)
    expect(await flow.expandTasks([])).toBe(false)
    expect(await flow.expandTrajectory('same', '')).toBe(false)
    expect(await flow.submitBuild()).toBe(false)
    expect(flow.batchId.value).toBe('a')
    expect(flow.expandedTasks.value).toEqual(['same'])
    expect(flow.expandedTrajectories.same).toBe('same-1')
    expect(flow.selectedTasks.value).toEqual(['same'])
    expect(client.createBuild).not.toHaveBeenCalled()
  })
  it('save-and-continue builds with the newly committed version and clears other cached trajectories', async () => {
    const { flow, client, protect } = setup()
    await flow.loadBatches('a'); await flow.expandTasks(['same']); await flow.expandTrajectory('same', 'same-1')
    flow.trajectoryData['other/old'] = record('obsolete')
    flow.selectedTasks.value = ['same']
    client.preprocessingBatches.mockResolvedValue([batch('a', 'v2')])
    protect.mockImplementationOnce(async () => { await flow.saveBBox('same', 'same-1', flow.trajectoryData['same/same-1']!.steps[0]!, [1, 1, 11, 11]); return true })
    expect(await flow.submitBuild()).toBe(true)
    expect(client.updateBBox.mock.calls[0]).toEqual(['same', 'same-1', 1, 2, [1, 1, 11, 11], { batch_id: 'a', annotation_version: 'v1' }])
    expect(client.createBuild).toHaveBeenCalledWith(['same'], { batch_id: 'a', annotation_version: 'v2' })
    expect(flow.trajectoryData['other/old']).toBeUndefined()
  })
  it('refresh follows the current artifact after protecting unsaved edits', async () => {
    const { flow, client, protect } = setup()
    await flow.loadBatches('a'); await flow.expandTasks(['same'])
    client.preprocessingBatches.mockResolvedValue([batch('a', 'v2')])
    protect.mockResolvedValueOnce(false)
    await flow.loadBatches()
    expect(flow.annotationVersion.value).toBe('v1')
    expect(flow.newerVersion.value).toBe(true)
    expect(flow.expandedTasks.value).toEqual(['same'])
    await flow.loadBatches()
    expect(flow.annotationVersion.value).toBe('v2')
    expect(flow.expandedTasks.value).toEqual([])
  })
  it('blocks repeat submission, switches and runs while a start request is pending', async () => {
    const { flow, client } = setup(), pending = deferred<PreprocessingJob>()
    await flow.loadBatches('waiting')
    client.createPreprocessing.mockReturnValueOnce(pending.promise)
    const first = flow.start()
    await vi.waitFor(() => expect(client.createPreprocessing).toHaveBeenCalledTimes(1))
    expect(await flow.start()).toBe(false)
    expect(await flow.selectBatch('b')).toBe(false)
    pending.resolve(job('waiting')); expect(await first).toBe(true)
    expect(flow.batchId.value).toBe('waiting')
    expect(flow.selectedBatch.value?.preprocessing_status).toBe('running')
    await flow.selectBatch('b');await flow.selectBatch('waiting')
    expect(flow.preprocessingJob.value?.status).toBe('running')
  })
  it('retries only failed jobs and obtains the ready version after persisted completion', async () => {
    const { flow, client } = setup()
    client.preprocessingBatches.mockResolvedValue([{ ...batch('a', null), latest_job: job('a', 'interrupted') }])
    await flow.loadBatches('a')
    expect(await flow.start(true)).toBe(true)
    expect(client.retryPreprocessing).toHaveBeenCalledWith('pre-a')
    client.preprocessingJob.mockResolvedValue(job('a', 'succeeded'))
    client.preprocessingBatches.mockResolvedValue([{ ...batch('a'), latest_job: job('a', 'succeeded') }])
    await flow.poll()
    expect(flow.annotationVersion.value).toBe('v1')
    expect(flow.tasks.value).toHaveLength(1)
    expect(flow.processing.value).toBe(false)
    expect(await flow.start(true)).toBe(false)
  })
  it('a late polling response cannot replace the newly selected batch or restore old data', async () => {
    const { flow, client } = setup(), pending = deferred<PreprocessingJob>()
    client.preprocessingBatches.mockResolvedValue([{ ...batch('a', null), latest_job: job() }, batch('b')])
    await flow.loadBatches('a')
    client.preprocessingJob.mockReturnValueOnce(pending.promise)
    const poll = flow.poll()
    await flow.selectBatch('b')
    pending.resolve(job('a', 'succeeded')); await poll
    expect(flow.batchId.value).toBe('b')
    expect(flow.preprocessingJob.value).toBeNull()
    expect(flow.tasks.value[0]?.goal).toBe('b')
  })
})


describe('Pipeline preprocessing bindings', () => {
  it('ignores a newer unrelated build and displays only the authoritative jobs', async () => {
    const { flow, client } = setup(undefined, { readOnly: () => true, boundJobs: () => ({ preprocessing: ['bound-pre'], tree: ['bound-tree'] }) })
    client.preprocessingBatches.mockResolvedValue([{ ...batch('a'), latest_job: { ...job(), job_id: 'unrelated-pre' } }])
    client.preprocessingJob.mockResolvedValue({ ...job(), job_id: 'bound-pre' })
    client.build.mockResolvedValue({ ...buildJob(), job_id: 'bound-tree' })
    client.builds.mockResolvedValue([{ ...buildJob(), job_id: 'unrelated-tree' }])
    await flow.loadBatches('a')
    expect(flow.preprocessingJob.value?.job_id).toBe('bound-pre')
    expect(flow.buildJob.value?.job_id).toBe('bound-tree')
    expect(client.builds).not.toHaveBeenCalled()
    expect(client.createPreprocessing).not.toHaveBeenCalled()
    expect(client.createBuild).not.toHaveBeenCalled()
  })
  it('waits with no job and can bind a later dispatch without submitting work', async () => {
    let ids: string[] = []
    const { flow, client } = setup(undefined, { readOnly: () => true, boundJobs: () => ({ preprocessing: ids, tree: [] }) })
    client.preprocessingBatches.mockResolvedValue([{ ...batch('a'), latest_job: job() }])
    await flow.loadBatches('a')
    expect(flow.preprocessingJob.value).toBeNull()
    expect(client.preprocessingJob).not.toHaveBeenCalled()
    ids = ['pre-a']; await flow.bindJobs()
    expect(flow.preprocessingJob.value?.job_id).toBe('pre-a')
    expect(client.createPreprocessing).not.toHaveBeenCalled()
  })
  it('rejects read-only writes while keeping trajectory inspection available', async () => {
    const { flow, client } = setup(undefined, { readOnly: () => true, boundJobs: () => ({ preprocessing: [], tree: [] }) })
    await flow.loadBatches('a'); await flow.expandTasks(['same']); await flow.expandTrajectory('same', 'same-1')
    flow.selectedTasks.value = ['same']
    expect(await flow.start()).toBe(false)
    expect(await flow.submitBuild()).toBe(false)
    await expect(flow.saveBBox('same', 'same-1', record().steps[0]!, [1, 1, 10, 10])).rejects.toThrow('Pipeline')
    expect(client.trajectory).toHaveBeenCalled()
    expect(client.updateBBox).not.toHaveBeenCalled()
    expect(client.createBuild).not.toHaveBeenCalled()
  })
  it('does not accept an old job response after the authoritative binding changes', async () => {
    let ids: string[] = []
    const { flow, client } = setup(undefined, { boundJobs: () => ({ preprocessing: ids, tree: [] }) })
    await flow.loadBatches('a')
    const slow = deferred<PreprocessingJob>()
    ids = ['old']; client.preprocessingJob.mockReturnValueOnce(slow.promise)
    const old = flow.bindJobs()
    ids = ['new']; client.preprocessingJob.mockResolvedValue({ ...job(), job_id: 'new' })
    await flow.bindJobs()
    slow.resolve({ ...job(), job_id: 'old' }); await old
    expect(flow.preprocessingJob.value?.job_id).toBe('new')
  })
})


it('does not read the current batch artifacts through an ended Pipeline history link', async () => {
  const { flow, client } = setup(undefined, { historyOnly: () => true, readOnly: () => true })
  await flow.loadBatches('a')
  expect(client.preprocessingBatches).not.toHaveBeenCalled()
  expect(client.tasks).not.toHaveBeenCalled()
  expect(client.builds).not.toHaveBeenCalled()
  expect(flow.scope.value).toBeNull()
})
