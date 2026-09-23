import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { reactive, ref } from 'vue'
const hooks = vi.hoisted(() => ({ cleanup: [] as Array<() => void>, route: {} as Record<string, unknown> }))
vi.mock('vue', async original => ({ ...await original<typeof import('vue')>(), onMounted: (callback: () => void) => callback(), onBeforeUnmount: (callback: () => void) => hooks.cleanup.push(callback) }))
vi.mock('vue-router', () => ({ useRoute: () => hooks.route }))
vi.mock('@/api', () => ({ api: { pipeline: vi.fn(), listPipelines: vi.fn(), controlPipeline: vi.fn() } }))
import { api } from '@/api'
import { usePipelineContext } from './usePipelineContext'
import type { Pipeline } from '@/types/pipeline'
import { publishedBatch } from '@/utils/batchLifecycle'
function record(overrides: Partial<Pipeline> = {}): Pipeline {
  return { pipeline_id: 'p', name: 'test', batch_id: 'batch', mode: 'manual', start_mode: 'existing', task_ids: ['A'], status: 'running', current_step: 'quality', storage_revision: 4, created_at: 'now', updated_at: 'now', collection_run_ids: [], steps: [{ id: 'quality', label: '质检', status: 'running', job_ids: ['exact-job'], percent: 25 }, { id: 'correction', label: '修正', status: 'pending', job_ids: [] }], ...overrides }
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes }); return { promise, resolve } }
beforeEach(() => {
  vi.useFakeTimers(); vi.clearAllMocks()
  vi.stubGlobal('window', new EventTarget()); vi.stubGlobal('document', Object.assign(new EventTarget(), { visibilityState: 'visible' })); vi.stubGlobal('BroadcastChannel', undefined)
  hooks.route = reactive({ path: '/quality', query: { pipeline_id: 'p', batch_id: 'batch', step_id: 'quality' } })
  vi.mocked(api.pipeline).mockResolvedValue(record()); vi.mocked(api.listPipelines).mockResolvedValue([])
})
afterEach(() => { hooks.cleanup.splice(0).forEach(callback => callback()); vi.useRealTimers(); vi.unstubAllGlobals() })
describe('authoritative Pipeline module context', () => {
  it('binds exact jobs and observing never submits a job or control action', async () => {
    const context = usePipelineContext({ stepId: 'quality' }); expect(context.readOnly.value).toBe(true)
    await context.refresh()
    expect(context.jobIds.value).toEqual(['exact-job']); expect(context.step.value?.percent).toBe(25)
    expect(api.controlPipeline).not.toHaveBeenCalled(); expect(api.listPipelines).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1501); expect(api.pipeline).toHaveBeenCalledTimes(2)
  })
  it('does not let an older poll undo a completed control revision', async () => {
    const context = usePipelineContext(); await context.refresh()
    context.pipeline.value = record({ storage_revision: 5, status: 'paused' })
    await context.refresh()
    expect(context.pipeline.value.status).toBe('paused'); expect(context.pipeline.value.storage_revision).toBe(5)
  })
  it('waits before a child job exists then discovers the binding without navigation or submission', async () => {
    vi.mocked(api.pipeline).mockResolvedValueOnce(record({ steps: [{ id: 'quality', label: '质检', status: 'pending', job_ids: [] }] }))
    const context = usePipelineContext(); await context.refresh(); expect(context.isWaiting.value).toBe(true)
    await vi.advanceTimersByTimeAsync(1501); expect(context.jobIds.value).toEqual(['exact-job']); expect(context.isWaiting.value).toBe(false)
    expect(api.controlPipeline).not.toHaveBeenCalled()
  })
  it('looks up managed state even without Pipeline route parameters', async () => {
    hooks.route.query = {}; vi.mocked(api.listPipelines).mockResolvedValue([record()])
    const batch = ref('batch'), context = usePipelineContext({ batchId: batch, stepId: 'quality' })
    await context.refresh(); expect(context.managed.value).toBe(true); expect(context.readOnly.value).toBe(true)
    expect(api.listPipelines).toHaveBeenCalledWith({ batch_id: 'batch', active_only: true }, expect.any(AbortSignal))
    vi.mocked(api.listPipelines).mockResolvedValue([]); batch.value = 'other'; expect(context.resolved.value).toBe(false)
    await context.refresh(); expect(context.managed.value).toBe(false); expect(context.readOnly.value).toBe(false)
  })
  it('invalidates late responses on batch and route changes', async () => {
    const old = deferred<Pipeline>(); vi.mocked(api.pipeline).mockReturnValueOnce(old.promise)
    const context = usePipelineContext(); const signal = vi.mocked(api.pipeline).mock.calls[0]![1]!
    hooks.route.query = { pipeline_id: 'new', batch_id: 'new-batch', step_id: 'quality' }
    vi.mocked(api.pipeline).mockResolvedValue(record({ pipeline_id: 'new', batch_id: 'new-batch' }))
    // The route watcher runs synchronously, so explicitly refresh after its first mismatched mock result.
    await context.refresh(); await context.refresh()
    old.resolve(record()); await Promise.resolve()
    expect(signal.aborted).toBe(true); expect(context.pipeline.value?.pipeline_id).toBe('new')
  })
  it('fails closed when the route batch does not match or the backend is unavailable', async () => {
    vi.mocked(api.pipeline).mockResolvedValue(record({ batch_id: 'wrong' }))
    const context = usePipelineContext(); await context.refresh(); expect(context.error.value).toContain('不匹配'); expect(context.readOnly.value).toBe(true)
    vi.mocked(api.pipeline).mockRejectedValue(new Error('offline')); await context.refresh(); expect(context.error.value).toBe('offline'); expect(context.readOnly.value).toBe(true)
  })
  it('allows only the correction checkpoint to edit and confirms both revisions', async () => {
    hooks.route.path = '/correction/expert-action'; hooks.route.query = { pipeline_id: 'p', batch_id: 'batch', step_id: 'correction' }
    vi.mocked(api.pipeline).mockResolvedValue(record({ status: 'waiting_for_correction', current_step: 'correction' }))
    vi.mocked(api.controlPipeline).mockResolvedValue(record({ current_step: 'cot', storage_revision: 5 }))
    const context = usePipelineContext({ stepId: 'correction' }); await context.refresh(); expect(context.readOnly.value).toBe(false)
    await context.confirmCorrection(12); expect(api.controlPipeline).toHaveBeenCalledWith('p', 'confirm-correction', 4, 12); expect(context.readOnly.value).toBe(true)
  })
  it('does not unlock quality by forging step_id=correction', async () => {
    hooks.route.query = { pipeline_id: 'p', batch_id: 'batch', step_id: 'correction' }
    vi.mocked(api.pipeline).mockResolvedValue(record({ status: 'waiting_for_correction' }))
    const context = usePipelineContext({ stepId: 'quality' }); await context.refresh(); expect(context.canCorrect.value).toBe(false); expect(context.readOnly.value).toBe(true)
  })
  it('pauses polling while hidden and rejects pending work on disposal', async () => {
    const context = usePipelineContext(); await context.refresh()
    Object.assign(document, { visibilityState: 'hidden' }); document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(6000); expect(api.pipeline).toHaveBeenCalledTimes(1)
    Object.assign(document, { visibilityState: 'visible' }); document.dispatchEvent(new Event('visibilitychange')); await context.refresh()
    expect(api.pipeline).toHaveBeenCalledTimes(2)
    context.dispose(); window.dispatchEvent(new Event('focus')); await vi.advanceTimersByTimeAsync(6000); expect(api.pipeline).toHaveBeenCalledTimes(2)
  })
  it('isolates terminated unpublished history from the current editable batch', async () => {
    vi.mocked(api.pipeline).mockResolvedValue(record({ status: 'terminated' }))
    const context = usePipelineContext(); await context.refresh()
    expect(context.historyOnly.value).toBe(true); expect(context.managed.value).toBe(false); expect(context.readOnly.value).toBe(true)
    hooks.route.query = { batch_id: 'batch' }; await context.refresh()
    expect(context.historyOnly.value).toBe(false); expect(context.readOnly.value).toBe(false)
  })
  it('publishes targeted cache invalidation while retaining Pipeline history context', async () => {
    hooks.route.query = { pipeline_id: 'p', batch_id: 'pipeline-published-context', step_id: 'quality' }
    vi.mocked(api.pipeline).mockResolvedValue(record({ batch_id: 'pipeline-published-context', status: 'succeeded', release_id: 'release-1' }))
    const context = usePipelineContext(); await context.refresh()
    expect(publishedBatch('pipeline-published-context')?.release_id).toBe('release-1'); expect(context.pipeline.value?.pipeline_id).toBe('p'); expect(context.readOnly.value).toBe(true)
  })
})
