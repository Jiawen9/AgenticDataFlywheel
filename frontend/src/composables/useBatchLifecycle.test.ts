import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
const hooks = vi.hoisted(() => ({ cleanup: [] as Array<() => void> }))
vi.mock('vue', async original => ({ ...await original<typeof import('vue')>(), onMounted: (callback: () => void) => callback(), onBeforeUnmount: (callback: () => void) => hooks.cleanup.push(callback) }))
vi.mock('@/api', () => ({ api: { batchLifecycle: vi.fn() } }))
import { api } from '@/api'
import { useBatchLifecycle } from './useBatchLifecycle'
beforeEach(() => { vi.stubGlobal('window', new EventTarget()); vi.stubGlobal('document', Object.assign(new EventTarget(), { visibilityState: 'visible' })); vi.stubGlobal('BroadcastChannel', undefined) })
afterEach(() => { hooks.cleanup.splice(0).forEach(callback => callback()); vi.unstubAllGlobals(); vi.clearAllMocks() })
describe('lifecycle revalidation', () => {
  it('revalidates the current batch after focus and stops listening on unmount', async () => {
    vi.mocked(api.batchLifecycle).mockResolvedValue({ batch_id: 'focus-a', status: 'published', release_id: 'r', published_at: 'now' })
    const onPublished = vi.fn(), refreshChoices = vi.fn(async () => {})
    useBatchLifecycle({ currentBatch: () => 'focus-a', onPublished, refreshChoices })
    window.dispatchEvent(new Event('focus'))
    await vi.waitFor(() => expect(onPublished).toHaveBeenCalledOnce())
    expect(refreshChoices).toHaveBeenCalledOnce()
    hooks.cleanup.splice(0).forEach(callback => callback())
    window.dispatchEvent(new Event('focus'))
    expect(onPublished).toHaveBeenCalledOnce()
  })
  it('checks a resumed visible tab but does not erase anything on network failure', async () => {
    vi.mocked(api.batchLifecycle).mockRejectedValue(new Error('offline'))
    const onPublished = vi.fn()
    useBatchLifecycle({ currentBatch: () => 'offline-a', onPublished })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.waitFor(() => expect(api.batchLifecycle).toHaveBeenCalledWith('offline-a'))
    expect(onPublished).not.toHaveBeenCalled()
  })
})
