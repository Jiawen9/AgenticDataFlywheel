import { afterEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, defineComponent, h } from 'vue'
import { useRunStorage, type RunStorageState } from './useRunStorage'
import type { StorageOperation } from '@/phoneFactoryApi'
const polling = vi.hoisted(() => ({ work: [] as Array<(signal: AbortSignal) => Promise<void>> }))
vi.mock('./useFactoryPolling', () => ({ useFactoryPolling: (work: any) => { polling.work.push(work); return { start() {} } } }))
const mounted: Array<{ unmount(): void }> = []
afterEach(() => { mounted.splice(0).forEach(app => app.unmount()); polling.work = [] })
const store = () => { const values = new Map<string, string>(); return { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => { values.set(key, value) } } }
const operation = (kind: StorageOperation['kind'], status = 'queued'): StorageOperation => ({ operation_id: 'op', run_id: 'run', kind, status })
function mount(storage = store()) {
  const api = { scan: vi.fn(async () => operation('scan')), cleanup: vi.fn(async () => operation('cleanup')), archive: vi.fn(async () => operation('archive')), operation: vi.fn(async () => operation('cleanup')), archiveDownloadUrl: (id: string) => '/archive/' + id }
  const onState = vi.fn(), onArchive = vi.fn(); let state!: RunStorageState
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const app = renderer.createApp(defineComponent({ setup() { state = useRunStorage({ api, storage, onState, onArchive }); return () => h('div') } })); app.mount({}); mounted.push(app)
  return { app, api, state, storage, onState, onArchive }
}
describe('collector storage operations', () => {
  it('deduplicates clicks and polls independently of the details drawer or selection', async () => {
    const view = mount(); await view.state.start(['run', 'run'], 'cleanup', 'retire')
    await view.state.start(['run'], 'cleanup', 'retire')
    expect(view.api.cleanup).toHaveBeenCalledTimes(1)
    view.api.operation.mockResolvedValueOnce({ ...operation('cleanup', 'completed'), collector_data_state: 'retired', execution_ids: ['run', 'cx-child'], summary: { deleted_files: 20, deleted_bytes: 1000 } })
    await polling.work[0]!(new AbortController().signal)
    expect(view.onState).toHaveBeenLastCalledWith('cx-child', 'retired', 'op')
    expect(view.state.tasks[0]?.result?.summary?.deleted_files).toBe(20)
    expect(view.state.pending('run')).toBe(false)
  })
  it('restores a lost acknowledgement after refresh and replays exactly the same request', async () => {
    const view = mount(); view.api.cleanup.mockRejectedValueOnce(new Error('network lost'))
    await view.state.start(['run'], 'cleanup', 'retire')
    const original = view.api.cleanup.mock.calls[0]!.slice(0, 3)
    view.app.unmount()
    const restored = mount(view.storage)
    await polling.work.at(-1)!(new AbortController().signal)
    expect(restored.api.cleanup.mock.calls[0]!.slice(0, 3)).toEqual(original)
    expect(restored.state.tasks).toHaveLength(1)
  })
  it('keeps partial deletion visible and retries remaining paths using the original request', async () => {
    const view = mount()
    view.api.cleanup.mockResolvedValueOnce({ ...operation('cleanup', 'partial'), collector_data_state: 'cleanup_failed', errors: [{ path: 'locked.json', error: 'WinError 32' }], summary: { deleted_files: 10 } })
    await view.state.start(['run'], 'cleanup', 'retire')
    expect(view.onState).toHaveBeenLastCalledWith('run', 'cleanup_failed', 'op')
    const identity = view.api.cleanup.mock.calls[0]!.slice(0, 3)
    await view.state.retry(view.state.tasks[0]!)
    expect(view.api.cleanup.mock.calls[1]!.slice(0, 3)).toEqual(identity)
  })
  it('reports unsupported collectors without retrying forever or blocking other selected runs', async () => {
    const view = mount(); view.api.cleanup.mockRejectedValueOnce(Object.assign(new Error('请升级采集端'), { status: 501 }))
    await view.state.start(['first', 'second'], 'cleanup', 'retire')
    expect(view.api.cleanup).toHaveBeenCalledTimes(2)
    expect(view.state.tasks[0]?.rejected).toBe(true)
    // Unsupported collectors and explicit request rejects do not loop.
    view.api.cleanup.mockRejectedValueOnce(Object.assign(new Error('不支持'), { status: 400 }))
    await view.state.retry(view.state.tasks[0]!)
    await polling.work[0]!(new AbortController().signal)
    expect(view.state.tasks[0]?.rejected).toBe(true)
    expect(view.state.tasks[0]?.error).toBe('不支持')
  })
  it('does not mark cache cleanup as retirement and updates explicit archive readiness', async () => {
    const view = mount(); await view.state.start(['run'], 'cleanup', 'cache')
    expect(view.onState).not.toHaveBeenCalled()
    view.api.archive.mockResolvedValueOnce(operation('archive', 'completed'))
    await view.state.start(['run'], 'archive')
    expect(view.onArchive).toHaveBeenCalledWith('run', 'ready')
  })
})

it('does not mark an old collector unavailable when the capability check rejected cleanup', async () => {
  const view = mount(); view.api.cleanup.mockResolvedValueOnce({ ...operation('cleanup', 'failed'), error: '请升级采集端' })
  await view.state.start(['run'], 'cleanup', 'retire')
  expect(view.onState).not.toHaveBeenCalled()
  expect(view.state.tasks[0]?.result?.status).toBe('failed')
})

it('clears ready archive only when successful cache cleanup explicitly resets it', async () => {
  const view = mount()
  view.api.cleanup.mockResolvedValueOnce({ ...operation('cleanup', 'completed'), archive_status: 'not_requested' })
  await view.state.start(['run'], 'cleanup', 'cache')
  expect(view.onArchive).toHaveBeenLastCalledWith('run', 'not_requested')
  view.onArchive.mockClear()
  view.api.cleanup.mockResolvedValueOnce({ ...operation('cleanup', 'completed'), summary: { deleted_files: 0 } })
  await view.state.start(['legacy'], 'cleanup', 'cache')
  expect(view.onArchive).not.toHaveBeenCalled()
})

it('does not resurrect retired data when an older operation response arrives late', async () => {
  const view = mount()
  view.api.cleanup.mockResolvedValueOnce({ ...operation('cleanup', 'completed'), collector_data_state: 'retired' })
  await view.state.start(['run'], 'cleanup', 'retire')
  view.onState.mockClear()
  view.api.cleanup.mockResolvedValueOnce({ ...operation('cleanup', 'failed'), collector_data_state: 'available' })
  await view.state.retry(view.state.tasks[0]!)
  expect(view.onState).not.toHaveBeenCalled()
})
