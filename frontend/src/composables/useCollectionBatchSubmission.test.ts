import { afterEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import type { CollectionBatchDetail, CollectionBatchSummary } from '@/collectionBatchesApi'
import type { TaskGenerationJob } from '@/types'
import { useCollectionBatchSubmission } from './useCollectionBatchSubmission'

const job = (id: string, status: TaskGenerationJob['status'] = 'succeeded') => ({ job_id: id, status } as TaskGenerationJob)
const batch = (id: string): CollectionBatchDetail => ({ schema_version: 1, batch_id: `batch-${id}`, source_job_id: id, kind: 'task_generation', job_status: 'succeeded', knowledge_base_version: null, created_at: '2026-09-09T10:00:00', task_count: 2, apps: ['示例App'], filename: `collection-batch-${id}.xlsx`, download_url: '', snapshot: { schema_version: 1, job_id: id, kind: 'task_generation', job_status: 'succeeded', knowledge_base_version: null, task_count: 0, tasks: [], errors: [], warnings: [] } })
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes }); return { promise, resolve } }
const cleanups: Array<() => void> = []
function setup(existing: CollectionBatchSummary[] = []) {
  const selected = ref(job('A')), count = ref(2), busy = ref(false)
  const api = { list: vi.fn(async (_id: string) => existing), submit: vi.fn(async (id: string) => batch(id)), workbook: vi.fn(async () => new Blob(['workbook'])) }
  const runProtected = vi.fn(async (action: (id: string) => Promise<CollectionBatchDetail | null>) => {
    busy.value = true
    try { return await action(selected.value.job_id) } finally { busy.value = false }
  })
  const state = useCollectionBatchSubmission(api, { job: () => selected.value, resultCount: () => count.value, busy: () => busy.value, runProtected })
  cleanups.push(state.dispose)
  return { state, api, selected, count, busy, runProtected }
}
afterEach(() => { cleanups.splice(0).forEach(dispose => dispose()) })

describe('generation collection batch submission', () => {
  it('restores an existing frozen batch even when all source results are deleted', async () => {
    const { state, count, api } = setup([batch('A')])
    count.value = 0
    await vi.waitFor(() => expect(state.loaded.value).toBe(true))
    expect(state.batch.value?.batch_id).toBe('batch-A')
    expect(state.canSubmit.value).toBe(false)
    expect(await state.download()).toMatchObject({ filename: 'collection-batch-A.xlsx' })
    expect(api.submit).not.toHaveBeenCalled()
  })

  it('only permits first submission for succeeded/partial jobs with remaining tasks', async () => {
    const { state, selected, count, busy } = setup()
    await vi.waitFor(() => expect(state.loaded.value).toBe(true))
    expect(state.canSubmit.value).toBe(true)
    selected.value = job('A', 'partial'); expect(state.canSubmit.value).toBe(true)
    for (const status of ['running', 'queued', 'awaiting_confirmation', 'failed', 'interrupted'] as const) {
      selected.value = job('A', status); expect(state.canSubmit.value).toBe(false)
    }
    selected.value = job('A'); count.value = 0; expect(state.canSubmit.value).toBe(false)
    count.value = 2; busy.value = true; expect(state.canSubmit.value).toBe(false)
  })

  it('submits only a job ID through the page protection and suppresses repeated clicks', async () => {
    const { state, api, runProtected } = setup(), pending = deferred<CollectionBatchDetail>()
    await vi.waitFor(() => expect(state.canSubmit.value).toBe(true))
    api.submit.mockReturnValue(pending.promise)
    const first = state.submit()
    expect(await state.submit()).toBe(false)
    expect(runProtected).toHaveBeenCalledTimes(1)
    expect(api.submit).toHaveBeenCalledWith('A')
    pending.resolve(batch('A')); expect(await first).toBe(true)
    expect(state.batch.value?.batch_id).toBe('batch-A')
    expect(await state.submit()).toBe(false)
    expect(api.submit).toHaveBeenCalledTimes(1)
  })

  it('does not POST when protecting unsaved edits is cancelled', async () => {
    const { state, api, runProtected } = setup()
    await vi.waitFor(() => expect(state.canSubmit.value).toBe(true))
    runProtected.mockResolvedValue(null)
    expect(await state.submit()).toBe(false)
    expect(api.submit).not.toHaveBeenCalled()
    expect(state.submitting.value).toBe(false)
  })

  it('rejects a changed job after an asynchronous dirty-edit decision', async () => {
    const { state, api, selected, runProtected } = setup(), decision = deferred<void>()
    await vi.waitFor(() => expect(state.canSubmit.value).toBe(true))
    runProtected.mockImplementation(async action => { await decision.promise; return action(selected.value.job_id) })
    const request = state.submit()
    selected.value = job('B'); decision.resolve(); await request
    expect(api.submit).not.toHaveBeenCalled()
    expect(state.batch.value).toBeNull()
  })

  it('ignores stale list and submit responses after switching jobs', async () => {
    const { state, api, selected } = setup(), old = deferred<CollectionBatchDetail>()
    await vi.waitFor(() => expect(state.canSubmit.value).toBe(true))
    api.submit.mockReturnValue(old.promise)
    const submitting = state.submit()
    selected.value = job('B')
    await vi.waitFor(() => expect(api.list).toHaveBeenCalledWith('B'))
    old.resolve(batch('A')); expect(await submitting).toBe(false)
    expect(state.batch.value).toBeNull()
    const list = deferred<CollectionBatchSummary[]>()
    api.list.mockImplementation(id => id === 'B' ? list.promise : Promise.resolve([batch('C')]))
    const refreshing = state.refresh()
    selected.value = job('C')
    await vi.waitFor(() => expect(state.batch.value?.batch_id).toBe('batch-C'))
    list.resolve([batch('B')]); await refreshing
    expect(state.batch.value?.batch_id).toBe('batch-C')
  })

  it('retains the server error and can recover after a submission response is lost', async () => {
    const { state, api } = setup()
    await vi.waitFor(() => expect(state.canSubmit.value).toBe(true))
    api.submit.mockRejectedValue(new Error('连接中断'))
    expect(await state.submit()).toBe(false)
    expect(state.error.value).toBe('连接中断')
    api.list.mockResolvedValue([batch('A')])
    await state.refresh()
    expect(state.batch.value?.batch_id).toBe('batch-A')
    expect(state.error.value).toBe('')
    expect(api.submit).toHaveBeenCalledTimes(1)
  })

  it('blocks submission when its existing-batch lookup failed instead of claiming not submitted', async () => {
    const { state, api } = setup()
    await vi.waitFor(() => expect(state.loaded.value).toBe(true))
    api.list.mockRejectedValueOnce(new Error('读取失败'))
    await state.refresh()
    expect(state.loaded.value).toBe(false)
    expect(state.canSubmit.value).toBe(false)
    expect(await state.submit()).toBe(false)
    await state.refresh()
    expect(state.canSubmit.value).toBe(true)
  })

  it('ignores any pending response after disposal', async () => {
    const { state, api } = setup(), pending = deferred<CollectionBatchDetail>()
    await vi.waitFor(() => expect(state.canSubmit.value).toBe(true))
    api.submit.mockReturnValue(pending.promise)
    const request = state.submit()
    state.dispose(); pending.resolve(batch('A')); expect(await request).toBe(false)
    expect(state.batch.value).toBeNull()
  })
})
