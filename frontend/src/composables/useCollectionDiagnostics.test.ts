import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, defineComponent, h, nextTick, ref } from 'vue'
import type { CollectionDiagnostics, FactoryRun, PhoneOwner, PhoneOwnership } from '@/phoneFactoryApi'
import { useCollectionDiagnostics, type CollectionDiagnosticsState } from './useCollectionDiagnostics'

const polling = vi.hoisted(() => ({ registrations: 0, start: vi.fn(), work: undefined as any }))
vi.mock('./useFactoryPolling', () => ({ useFactoryPolling: (work: any, delay: number) => { polling.registrations++; polling.work = work; expect(delay).toBe(5000); return { start: polling.start } } }))
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
const snapshot = (run = 'a'): CollectionDiagnostics => ({ supported: true, run_id: run, observed_at: new Date().toISOString(), phones: [{ phone_id: 'p1', status: 'error', phase: 'model_request', model_role: 'planner', can_resume: true }], summary: { total: 1, healthy: 0, warning: 0, error: 1 } })
const flush = async () => { await nextTick(); await Promise.resolve(); await nextTick() }
const mounted: Array<{ unmount(): void }> = []
function mount(allowed = true) {
  const runs = ref<FactoryRun[]>([{ run_id: 'a', status: 'running', created_at: '2026-11-03' }, { run_id: 'b', status: 'completed', created_at: '2026-11-02' }])
  const api = { diagnostics: vi.fn(async (id: string, _signal?: AbortSignal) => snapshot(id)), resumePreview: vi.fn(async () => ({ preview_id: 'preview', selection_digest: 'digest', expected_revision: 4, instances: [{ phone_id: 'p1' }], blocked_phones: [], requires_unknown_phone_ids: [] as string[] })), resumeCollection: vi.fn(async (_id: string, body: any, _signal?: AbortSignal) => ({ operation_id: body.request_id, status: 'accepted', run_id: 'a' })), resumeOperation: vi.fn(async (_id: string, request: string, _signal?: AbortSignal) => ({ operation_id: request, status: 'accepted' })) }
  const confirmUnknown = vi.fn(async () => true)
  let state!: CollectionDiagnosticsState
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const app = renderer.createApp(defineComponent({ setup() { state = useCollectionDiagnostics(runs, api, { canResume: () => allowed, confirmUnknown }); return () => h('div') } }))
  app.mount({}); mounted.push(app)
  return { app, runs, api, state, confirmUnknown }
}
beforeEach(() => { vi.useFakeTimers(); vi.clearAllMocks(); polling.registrations = 0 })
afterEach(() => { mounted.splice(0).forEach(app => app.unmount()); vi.useRealTimers() })

const owner: PhoneOwner = { execution_id: 'old-execution', state_revision: 10, ownership_token: 'proof', availability: 'free', supported_actions: ['reconcile', 'stop'] }
const ownership: PhoneOwnership = { availability: 'free', stale: false, checked_at: '2026-11-04T00:00:00Z', owners: [owner] }
function controls(view: ReturnType<typeof mount>) {
  const api = Object.assign(view.api, {
    phoneOwnership: vi.fn(async (_id: string, _phone: string, _signal?: AbortSignal) => ownership),
    phoneControl: vi.fn(async (_id: string, phone: string, action: 'reconcile' | 'stop', body: any, _signal?: AbortSignal) => ({ operation_id: body.request_id, execution_id: body.execution_id, phone_id: phone, action, status: 'accepted' })),
    phoneControlOperation: vi.fn(async (_id: string, request: string, _signal?: AbortSignal) => ({ operation_id: request, execution_id: owner.execution_id, phone_id: 'p1', action: 'reconcile' as const, status: 'completed' })),
  })
  api.diagnostics.mockResolvedValue({ ...snapshot(), phone_control: { version: 1 }, phones: [{ ...snapshot().phones[0]!, ownership }] })
  return api
}

describe('phone ownership and scoped controls', () => {
  it('keeps controls unavailable on an old collector and never stops on diagnostic refresh', async () => {
    const view = mount(), api = controls(view)
    api.diagnostics.mockResolvedValueOnce(snapshot())
    await view.state.refresh(); await view.state.controlPhone('p1', 'stop', owner)
    expect(api.phoneControl).not.toHaveBeenCalled()
    await view.state.refresh(); expect(api.phoneControl).not.toHaveBeenCalled()
  })
  it('sends the exact owner and version once, then polls duplicate clicks', async () => {
    const view = mount(), api = controls(view); await view.state.refresh()
    await view.state.controlPhone('p1', 'reconcile', owner)
    await view.state.controlPhone('p1', 'reconcile', owner)
    expect(api.phoneControl).toHaveBeenCalledTimes(1)
    expect(api.phoneControl.mock.calls[0]?.slice(0, 3)).toEqual(['a', 'p1', 'reconcile'])
    expect(api.phoneControl.mock.calls[0]?.[3]).toMatchObject({ execution_id: 'old-execution', expected_state_revision: 10, ownership_token: 'proof' })
    expect(api.phoneControlOperation).toHaveBeenCalledTimes(1)
    expect(view.state.controlFor('p1')?.result?.status).toBe('completed')
  })
  it('discards a late ownership query after A to B to A selection', async () => {
    const view = mount(), api = controls(view); await view.state.refresh()
    const pending = deferred<PhoneOwnership>(); api.phoneOwnership.mockReturnValueOnce(pending.promise)
    const work = view.state.checkOwnership('p1')
    view.state.selectRun('b'); view.state.selectRun('a')
    pending.resolve({ ...ownership, availability: 'busy', checked_at: '2099-01-01T00:00:00Z' }); await work
    expect(view.state.ownershipFor('p1')?.availability).toBe('free')
  })
  it('retries a lost acknowledgement using the original request and no new target', async () => {
    const view = mount(), api = controls(view); await view.state.refresh()
    api.phoneControl.mockRejectedValueOnce(new Error('lost ack'))
    api.phoneControlOperation.mockRejectedValueOnce(Object.assign(new Error('not yet accepted'), { status: 404 }))
    await view.state.controlPhone('p1', 'reconcile', owner)
    expect(api.phoneControl).toHaveBeenCalledTimes(2)
    expect(api.phoneControl.mock.calls[0]?.slice(0, 4)).toEqual(api.phoneControl.mock.calls[1]?.slice(0, 4))
    expect(api.resumeCollection).not.toHaveBeenCalled()
  })
  it('freezes an explicit device revision into continuation without replacing model configuration', async () => {
    const view = mount(); await view.state.refresh()
    await view.state.continuePhones(['p1'], { adb_path: 'D:/adb/adb.exe', app_list: { Demo: 'com.example.demo' } })
    expect(view.api.resumeCollection.mock.calls[0]?.[1]).toMatchObject({ device_config_revision: { adb_path: 'D:/adb/adb.exe', app_list: { Demo: 'com.example.demo' } } })
    expect(view.api.resumeCollection.mock.calls[0]?.[1]).not.toHaveProperty('planner_config')
  })
})

describe('page-owned collection diagnostics and resume', () => {
  it('waits for run type metadata before diagnostics and never probes an independent evaluation', async () => {
    const view = mount()
    view.state.selectRun('offline-new')
    await view.state.refresh()
    expect(view.api.diagnostics).not.toHaveBeenCalled()
    view.runs.value = [...view.runs.value, { run_id: 'offline-new', status: 'preparing', execution_mode: 'evaluate_only' }]
    await flush(); await view.state.refresh()
    expect(view.api.diagnostics).not.toHaveBeenCalled()
    expect(view.state.snapshot.value?.phones).toEqual([])
    view.state.selectRun('a'); await view.state.refresh()
    expect(view.api.diagnostics).toHaveBeenCalledTimes(1)
  })
  it('retains last data and selection through polling failures and marks snapshots older than 20 seconds', async () => {
    const view = mount(); await polling.work(new AbortController().signal, () => true)
    expect(polling.registrations).toBe(1); expect(view.state.snapshot.value?.phones[0]?.model_role).toBe('planner')
    expect(view.state.stale.value).toBe(false)
    view.api.diagnostics.mockRejectedValueOnce(new Error('offline')); await view.state.refresh()
    expect(view.state.snapshot.value?.run_id).toBe('a'); expect(view.state.error.value).toContain('offline')
    await vi.advanceTimersByTimeAsync(21000); expect(view.state.stale.value).toBe(true)
    view.state.selectRun('just-created'); view.runs.value = [...view.runs.value]; await flush()
    expect(view.state.selectedRunId.value).toBe('just-created')
  })
  it('aborts and discards A→B→A late responses and never updates an unmounted page', async () => {
    const view = mount(), late = deferred<CollectionDiagnostics>()
    view.api.diagnostics.mockReturnValueOnce(late.promise)
    const work = view.state.refresh(), signal = view.api.diagnostics.mock.calls[0]![1]
    view.state.selectRun('b'); view.state.selectRun('a')
    expect(signal?.aborted).toBe(true)
    late.resolve(snapshot()); await work; expect(view.state.snapshot.value).toBeUndefined()
    const another = deferred<CollectionDiagnostics>(); view.api.diagnostics.mockReturnValueOnce(another.promise)
    const remaining = view.state.refresh(); view.app.unmount(); another.resolve(snapshot()); await remaining
    expect(view.state.snapshot.value).toBeUndefined()
  })
  it('previews known stopped phones and submits once without an unnecessary confirmation', async () => {
    const view = mount(); await view.state.refresh(); await view.state.continuePhones(['p1'])
    expect(view.confirmUnknown).not.toHaveBeenCalled(); expect(view.api.resumePreview).toHaveBeenCalledTimes(1)
    const body = view.api.resumeCollection.mock.calls[0]![1]
    expect(body).toMatchObject({ preview_id: 'preview', selection_digest: 'digest', expected_revision: 4 })
    expect(body).not.toHaveProperty('confirm_unknown_phone_ids')
    await view.state.continuePhones(['p1'])
    expect(view.api.resumeCollection).toHaveBeenCalledTimes(1); expect(view.api.resumeOperation).toHaveBeenCalled()
    expect(view.state.operationFor('p1')?.result?.status).toBe('accepted')
  })
  it('confirms an unknown phone group once and retries a lost acknowledgement with the exact same request', async () => {
    const view = mount(); await view.state.refresh()
    view.api.resumePreview.mockResolvedValueOnce({ preview_id: 'preview', selection_digest: 'digest', expected_revision: 4, instances: [{}], blocked_phones: [], requires_unknown_phone_ids: ['p1', 'p2'] })
    view.api.resumeCollection.mockRejectedValueOnce(new Error('connection lost'))
    view.api.resumeOperation.mockRejectedValueOnce(Object.assign(new Error('not found yet'), { status: 404 }))
    await view.state.continuePhones(['p2', 'p1'])
    expect(view.confirmUnknown).toHaveBeenCalledExactlyOnceWith(['p1', 'p2'])
    expect(view.state.operationFor('p1')?.phase).toBe('waiting')
    const first = { ...view.api.resumeCollection.mock.calls[0]![1] }
    await view.state.continuePhones(['p1', 'p2'])
    expect(view.api.resumePreview).toHaveBeenCalledTimes(1); expect(view.confirmUnknown).toHaveBeenCalledTimes(1)
    expect(view.api.resumeCollection.mock.calls[1]![1]).toEqual(first)
    expect(first.confirm_unknown_phone_ids).toEqual(['p1', 'p2'])
    expect(view.api.resumeOperation.mock.calls[0]![1]).toBe(first.request_id)
  })
  it('never resumes after unknown confirmation is cancelled or when the run is read-only', async () => {
    const view = mount(); await view.state.refresh()
    view.api.resumePreview.mockResolvedValueOnce({ preview_id: 'preview', selection_digest: 'digest', expected_revision: 4, instances: [{}], blocked_phones: [], requires_unknown_phone_ids: ['p1'] })
    view.confirmUnknown.mockResolvedValueOnce(false); await view.state.continuePhones(['p1'])
    expect(view.api.resumeCollection).not.toHaveBeenCalled()
    const readonly = mount(false); await readonly.state.refresh(); await readonly.state.continuePhones(['p1'])
    expect(readonly.api.resumePreview).not.toHaveBeenCalled()
  })
  it('declines uncertain phones without blocking known phones, and excludes newly uncertain phones without asking again', async () => {
    const view = mount(); await view.state.refresh()
    view.api.resumePreview
      .mockResolvedValueOnce({ preview_id: 'mixed', selection_digest: 'mixed-digest', expected_revision: 4, instances: [{ phone_id: 'p1' }, { phone_id: 'p2' }, { phone_id: 'p3' }], blocked_phones: [], requires_unknown_phone_ids: ['p2'] })
      .mockResolvedValueOnce({ preview_id: 'changed', selection_digest: 'changed-digest', expected_revision: 4, instances: [{ phone_id: 'p1' }, { phone_id: 'p3' }], blocked_phones: [], requires_unknown_phone_ids: ['p3'] })
      .mockResolvedValueOnce({ preview_id: 'known-only', selection_digest: 'known-digest', expected_revision: 4, instances: [{ phone_id: 'p1' }], blocked_phones: [], requires_unknown_phone_ids: [] })
    view.confirmUnknown.mockResolvedValueOnce(false)
    const work = view.state.continuePhones(['p1', 'p2', 'p3'])
    const requestId = view.state.operations[0].requestId
    await work
    expect(view.confirmUnknown).toHaveBeenCalledExactlyOnceWith(['p2'])
    expect(view.api.resumePreview.mock.calls.map(call => call[1])).toEqual([['p1', 'p2', 'p3'], ['p1', 'p3'], ['p1']])
    expect(view.api.resumeCollection).toHaveBeenCalledTimes(1)
    expect(view.api.resumeCollection.mock.calls[0]![1]).toEqual({ request_id: requestId, preview_id: 'known-only', selection_digest: 'known-digest', expected_revision: 4 })
    expect(view.state.operationFor('p1')?.phase).toBe('waiting')
    expect(view.state.operationFor('p2')).toBeUndefined()
    expect(view.state.operationFor('p3')).toBeUndefined()
  })
  it('exposes unsupported fallback and keeps evaluate-only progress on its existing snapshot endpoint', async () => {
    const view = mount(); view.api.diagnostics.mockRejectedValueOnce(Object.assign(new Error('old endpoint'), { status: 404 }))
    await view.state.refresh(); expect(view.state.snapshot.value?.supported).toBe(false)
    await view.state.continuePhones(['p1']); expect(view.api.resumePreview).not.toHaveBeenCalled()
    view.runs.value = [{ run_id: 'a', status: 'running', execution_mode: 'evaluate_only' }]; await flush()
    await view.state.refresh(); expect(view.api.diagnostics).toHaveBeenCalledTimes(1)
    expect(view.state.snapshot.value?.message).toContain('当前评测快照')
  })
})

it('stops remote diagnostic reads and rejects late responses when cleanup starts', async () => {
  const view = mount(), pending = deferred<CollectionDiagnostics>(); view.api.diagnostics.mockReturnValueOnce(pending.promise)
  const work = view.state.refresh(); view.runs.value = view.runs.value.map(run => run.run_id === 'a' ? { ...run, collector_data_state: 'retiring' as const } : run)
  await flush(); pending.resolve(snapshot('a')); await work
  await view.state.refresh()
  expect(view.api.diagnostics).toHaveBeenCalledTimes(1)
  expect(view.state.snapshot.value?.supported).toBe(false)
  await view.state.continuePhones(['p1'])
  expect(view.api.resumePreview).not.toHaveBeenCalled()
})
