import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent } from 'vue'
import { renderToString } from '@vue/server-renderer'
import type { TaskGenerationResult } from '@/types'
const reads = vi.hoisted(() => ({ history: vi.fn(), results: vi.fn() }))
vi.mock('@/generationReadApi', () => ({ generationHistory: reads.history, generationResults: reads.results }))
import { useGenerationHistory, useGenerationResultPage } from './useGenerationPages'

async function setup() {
  let state!: { history: ReturnType<typeof useGenerationHistory>; results: ReturnType<typeof useGenerationResultPage> }
  await renderToString(createSSRApp(defineComponent({ setup() { state = { history: useGenerationHistory('task_generation'), results: useGenerationResultPage() }; return () => null } })))
  return state
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
const row = (id: string): TaskGenerationResult => ({ result_id: id, task: id, app: '甲', scene: '场景', capability: '能力', sub_capability: '类型', deleted: false })
const resultPage = (id: string) => ({ results: [row(id)], errors: [], total: 20, offset: 0, limit: 10, has_more: true, counts: { all: 200, active: 190, main: 100, prerequisites: 90, strong: 0 }, apps: ['甲'], types: [] })

describe('paged generation reads', () => {
  afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); reads.history.mockReset(); reads.results.mockReset() })
  it('backs off failed polling, retains the readable snapshot and resets the interval after success', async () => {
    vi.useFakeTimers()
    const state = await setup()
    reads.history.mockResolvedValueOnce({ items: [{ job_id: 'kept' }], filtered_total: 1 })
      .mockRejectedValueOnce(new Error('temporary')).mockRejectedValueOnce(new Error('temporary'))
      .mockResolvedValue({ items: [{ job_id: 'recovered' }], filtered_total: 1 })
    await state.history.read()
    state.history.startPolling(() => state.history.read(), () => true)
    await vi.advanceTimersByTimeAsync(5000)
    expect(state.history.snapshot.value?.items[0]?.job_id).toBe('kept')
    await vi.advanceTimersByTimeAsync(9999); expect(reads.history).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1); expect(reads.history).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(19999); expect(reads.history).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(1); expect(state.history.snapshot.value?.items[0]?.job_id).toBe('recovered')
    await vi.advanceTimersByTimeAsync(4999); expect(reads.history).toHaveBeenCalledTimes(4)
    await vi.advanceTimersByTimeAsync(1); expect(reads.history).toHaveBeenCalledTimes(5)
  })
  it('isolates saved filters by the API root while restoring them within the same target', async () => {
    const values = new Map<string, string>()
    vi.stubGlobal('sessionStorage', { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key, value) })
    vi.stubEnv('VITE_API_BASE_URL', 'https://first.example/v1/')
    const first = await setup(); first.history.update({ search: 'first-only', offset: 20 })
    const restored = await setup(); expect(restored.history.query.value.search).toBe('first-only')
    vi.stubEnv('VITE_API_BASE_URL', 'https://second.example/v1')
    const second = await setup(); expect(second.history.query.value.search).toBe('')
    expect(second.history.query.value.offset).toBe(0)
  })
  it('keeps the new history page when an earlier filtered request arrives late', async () => {
    const state = await setup(), late = deferred<unknown>()
    reads.history.mockReturnValueOnce(late.promise).mockResolvedValueOnce({ items: [{ job_id: 'new' }], filtered_total: 40 })
    const first = state.history.read()
    state.history.update({ offset: 10, search: 'new' })
    await state.history.read()
    late.resolve({ items: [{ job_id: 'old' }], filtered_total: 100 }); await first
    expect(state.history.snapshot.value?.items[0]?.job_id).toBe('new')
    expect(reads.history).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 10, search: 'new', kind: 'task_generation' }))
  })
  it('loads only the selected results page and keeps whole-job counts after local saved edits', async () => {
    const state = await setup()
    reads.results.mockResolvedValue(resultPage('first'))
    const first = await state.results.read('A')
    expect(reads.results).toHaveBeenLastCalledWith('A', { offset: 0, limit: 10, show_deleted: false })
    expect(state.results.metadata(first.results)?.counts.active).toBe(190)
    expect(state.results.metadata([{ ...first.results[0]!, deleted: true }])?.counts.active).toBe(189)
    state.results.update({ offset: 20, limit: 20, app: '甲' })
    await state.results.read('A')
    expect(reads.results).toHaveBeenLastCalledWith('A', expect.objectContaining({ offset: 20, limit: 20, app: '甲' }))
  })
  it('discards the previous job response after switching jobs and resets only result paging', async () => {
    const state = await setup(), late = deferred<unknown>()
    reads.results.mockReturnValueOnce(late.promise).mockResolvedValueOnce(resultPage('B-row'))
    const first = state.results.read('A')
    state.results.update({ offset: 30 })
    await state.results.read('B')
    late.resolve(resultPage('A-row')); await first
    expect(state.results.snapshot.value?.results[0]?.result_id).toBe('B-row')
    expect(state.results.query.value.offset).toBe(0)
  })
})
