import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, defineComponent, h, nextTick, ref } from 'vue'
import { useSummaryPage } from './useSummaryPage'
import { cachedSummary, invalidateSummaryCache, sharedRead, summaryKey, type SummaryPage } from '@/utils/summaryCache'
import { notifyBatchesPublished } from '@/utils/batchLifecycle'

const deferred = <T,>() => { let resolve!: (v: T) => void; const promise = new Promise<T>(yes => { resolve = yes }); return { promise, resolve } }
const page = (id = 'first', active = 0): SummaryPage<any> => ({ items: [{ run_id: id, status: active ? 'running' : 'completed' }], latest: [{ run_id: id }], total: 1000, filtered_total: 1000, scope_counts: {total: 1000, active, abnormal: 17}, offset: 0, limit: 10, has_more: true, index_state: 'ready', counts_complete: true })
const apps: Array<{unmount(): void}> = []
function mount(load: (q: any) => Promise<SummaryPage<any>>, scope = 'test', prefetch = false) {
  const query = ref({ offset: 0, limit: 10, search: '' }), enabled = ref(true)
  let state!: ReturnType<typeof useSummaryPage<any>>
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const app = renderer.createApp(defineComponent({ setup() { state = useSummaryPage(scope, query, load, { enabled, prefetch }); return () => h('div') } })); app.mount({}); apps.push(app)
  return { state, query, enabled }
}
const flush = async () => { await Promise.resolve(); await nextTick(); await Promise.resolve(); await nextTick() }
beforeEach(() => {
  vi.useFakeTimers(); invalidateSummaryCache()
  vi.stubGlobal('window', new EventTarget()); vi.stubGlobal('document', Object.assign(new EventTarget(), { hidden: false }))
  vi.stubGlobal('BroadcastChannel', undefined)
  const values: Record<string, string> = {}
  vi.stubGlobal('sessionStorage', { setItem(k: string, v: string) { values[k] = v; this[k] = v }, getItem(k: string) { return values[k] ?? null }, removeItem(k: string) { delete values[k]; delete this[k] } })
})
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); invalidateSummaryCache(); vi.useRealTimers(); vi.unstubAllGlobals() })
describe('shared summary reads', () => {
  it('deduplicates page readers and focus/visibility, retains snapshots on failure, pauses hidden polling', async () => {
    const pending = deferred<SummaryPage<any>>(), load = vi.fn().mockReturnValueOnce(pending.promise).mockRejectedValue(new Error('offline'))
    const a = mount(load), b = mount(load)
    window.dispatchEvent(new Event('focus')); document.dispatchEvent(new Event('visibilitychange'))
    expect(load).toHaveBeenCalledTimes(1)
    pending.resolve(page('first', 1)); await a.state.refresh(); await flush()
    expect(a.state.data.value?.items).toEqual(b.state.data.value?.items)
    await vi.advanceTimersByTimeAsync(5001)
    expect(a.state.data.value?.items[0].run_id).toBe('first'); expect(a.state.error.value).toBe('offline')
    Object.assign(document, {hidden: true}); document.dispatchEvent(new Event('visibilitychange'))
    const calls = load.mock.calls.length; await vi.advanceTimersByTimeAsync(120000); expect(load).toHaveBeenCalledTimes(calls)
  })
  it('preserves page scope counts and rejects responses from a previous page', async () => {
    const old = deferred<SummaryPage<any>>(), load = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValue({...page('page2'), offset: 10})
    const a = mount(load); a.query.value = { offset: 10, limit: 10, search: '' }; await nextTick(); await a.state.refresh()
    old.resolve(page('old')); await flush()
    expect(a.state.data.value?.items[0].run_id).toBe('page2'); expect(a.query.value.offset).toBe(10)
    expect(a.state.counts.value?.abnormal).toBe(17)
  })
  it('invalidates an in-flight pre-publication result before starting the replacement read', async () => {
    const old = deferred<SummaryPage<any>>(), load = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValue(page('published'))
    const a = mount(load)
    notifyBatchesPublished({batch_ids: ['b1'], release_id: 'r1'}); await a.state.refresh()
    old.resolve(page('pre-publish')); await flush()
    expect(load).toHaveBeenCalledTimes(2); expect(a.state.data.value?.items[0].run_id).toBe('published')
  })
  it('stores only bounded display fields, restores them immediately and refreshes in background', async () => {
    const key = summaryKey('persist', {offset: 0, limit: 10, search: ''})
    await sharedRead(key, async () => ({...page(), items: [{run_id: 'saved', status: 'completed', request: {api_key: 'secret'}, vla: 'private-url', steps: new Array(500).fill('full detail')}]}), {persist: true})
    const serialized = Object.values(sessionStorage).filter(value => typeof value === 'string').join('')
    expect(serialized).not.toContain('secret'); expect(serialized).not.toContain('private-url'); expect(serialized).not.toContain('full detail')
    expect(serialized.length).toBeLessThan(256 * 1024)
    const pending = deferred<SummaryPage<any>>()
    const a = mount(() => pending.promise, 'persist')
    expect(a.state.data.value?.items[0].run_id).toBe('saved'); expect(a.state.cached.value).toBe(true)
    pending.resolve(page('fresh')); await a.state.refresh(); expect(a.state.cached.value).toBe(false)
    expect(cachedSummary<SummaryPage<any>>(key)?.value.items[0].run_id).toBe('fresh')
  })
  it('does not fetch a closed detail tab and polls terminal history at 30 seconds', async () => {
    const load = vi.fn().mockResolvedValue(page()), a = mount(load)
    await a.state.refresh(); a.enabled.value = false; await nextTick()
    await vi.advanceTimersByTimeAsync(60000); expect(load).toHaveBeenCalledTimes(1)
    a.enabled.value = true; await nextTick(); await a.state.refresh()
    await vi.advanceTimersByTimeAsync(5000); expect(load).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(25000); expect(load).toHaveBeenCalledTimes(3)
  })
})


it('prefetches only one next summary page while idle and suppresses hidden work', async () => {
  const load = vi.fn(async (query: any) => ({...page(), offset: query.offset})), a = mount(load, 'prefetch', true)
  await a.state.refresh(); await vi.advanceTimersByTimeAsync(401)
  expect(load.mock.calls.map(call => call[0].offset)).toEqual([0, 10])
  await vi.advanceTimersByTimeAsync(30001)
  expect(load.mock.calls.map(call => call[0].offset)).toEqual([0, 10, 0])
  a.query.value = {offset: 10, limit: 10, search: ''}; await nextTick(); await a.state.refresh()
  Object.assign(document, {hidden: true}); document.dispatchEvent(new Event('visibilitychange'))
  await vi.advanceTimersByTimeAsync(500)
  expect(load.mock.calls.filter(call => call[0].offset === 20)).toHaveLength(0)
})


it('keeps the old page total while a different page is pending so pagination cannot snap to page one', async () => {
  const pending = deferred<SummaryPage<any>>(), load = vi.fn().mockResolvedValueOnce(page('first')).mockReturnValueOnce(pending.promise)
  const a = mount(load, 'stable-page'); await a.state.refresh()
  a.query.value = {offset: 10, limit: 10, search: ''}; await nextTick()
  expect(a.state.total.value).toBe(1000); expect(a.state.items.value[0].run_id).toBe('first')
  pending.resolve({...page('second'), offset: 10}); await a.state.refresh()
  expect(a.state.items.value[0].run_id).toBe('second'); expect(a.query.value.offset).toBe(10)
})


it('keeps same-query rows while the rebuilt index reports unknown counts', async () => {
  const load = vi.fn().mockResolvedValueOnce(page('saved')).mockResolvedValueOnce({...page(), items: [], latest: [], total: null, filtered_total: null, scope_counts: {total: null, active: null, abnormal: null}, counts_complete: false, index_state: 'building'})
  const a = mount(load, 'rebuilding'); await a.state.refresh(); await a.state.reload()
  expect(a.state.items.value[0].run_id).toBe('saved'); expect(a.state.data.value?.counts_complete).toBe(false)
  expect(a.state.counts.value?.total).toBeNull(); expect(a.state.cached.value).toBe(true)
})
