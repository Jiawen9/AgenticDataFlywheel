import { afterEach, describe, expect, it, vi } from 'vitest'
import { useTrajectoryPreprocessing } from './useTrajectoryPreprocessing'
import type { TaskPage } from '@/businessPageApi'

const reads = vi.hoisted(() => ({ tasks: vi.fn() }))
vi.mock('@/businessPageApi', () => ({ preprocessingTaskPage: reads.tasks, jobPage: vi.fn() }))
// This unit exercises task loading; batch navigation is covered independently.
vi.mock('./useBusinessBatchChoices', () => ({ useBusinessBatchChoices: () => null }))
const disposers: Array<() => void> = []
function setup(summaryPages = true) {
  const flow = useTrajectoryPreprocessing({} as Parameters<typeof useTrajectoryPreprocessing>[0], async () => true, { summaryPages })
  flow.batchId.value = 'batch'; flow.annotationVersion.value = 'v1'
  disposers.push(flow.dispose)
  return flow
}
function page(completed = 90): TaskPage {
  const task = { task_id: 'one', goal: 'task', warning: '', first_trajectory: 'tr1', trajectory_count: 1, step_count: 1, annotated: true, tree_status: 'pending' }
  return { items: [task], tasks: [task], task_ids: ['one'], ready_task_ids: [], task_count: 100,
    tree_counts: { completed, stale: 3, pending: 7 }, tree_counts_scope: 'batch',
    quality_counts: { completed: 0, stale: 0, pending: 100 }, total: 100, filtered_total: 100,
    latest: [], scope_counts: null, offset: 0, limit: 10, has_more: true, index_state: 'ready', counts_complete: true }
}
afterEach(() => { disposers.splice(0).forEach(dispose => dispose()); vi.clearAllMocks() })

describe('preprocessing whole-batch tree counts', () => {
  it('uses backend whole-batch totals across pagination and search, not the visible tasks', async () => {
    reads.tasks.mockResolvedValue(page())
    const flow = setup()
    await flow.changeTaskPage(1)
    expect(flow.tasks.value).toHaveLength(1)
    expect(flow.treeCounts.value).toEqual({ completed: 90, stale: 3, pending: 7 })
    await flow.changeTaskPage(2)
    await flow.searchTasks('some task')
    expect(flow.treeCounts.value.completed).toBe(90)
    expect(reads.tasks).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 0, q: 'some task' }))
  })

  it('discards outdated count responses and shows unknown when the index is incomplete', async () => {
    const flow = setup()
    let resolve!: (value: TaskPage) => void
    reads.tasks.mockImplementationOnce(() => new Promise<TaskPage>(yes => { resolve = yes }))
    const old = flow.changeTaskPage(1)
    await vi.waitFor(() => expect(reads.tasks).toHaveBeenCalledTimes(1))
    reads.tasks.mockResolvedValue(page(95))
    await flow.changeTaskPage(2)
    resolve(page(50)); await old
    expect(flow.treeCounts.value.completed).toBe(95)
    reads.tasks.mockResolvedValue({ ...page(0), counts_complete: false, tree_counts: { completed: null, stale: null, pending: null } })
    await flow.changeTaskPage(3)
    expect(flow.treeCounts.value).toEqual({ completed: null, stale: null, pending: null })
  })

  it('retains complete-list calculation in legacy mode', () => {
    const flow = setup(false)
    flow.tasks.value = [
      ...page().items.map(task => ({ ...task, tree_status: 'succeeded' })),
      ...page().items.map(task => ({ ...task, task_id: 'two', tree_status: 'stale' })),
      ...page().items.map(task => ({ ...task, task_id: 'three', tree_status: 'failed' })),
    ]
    expect(flow.treeCounts.value).toEqual({ completed: 1, stale: 1, pending: 1 })
    expect(reads.tasks).not.toHaveBeenCalled()
  })
})
