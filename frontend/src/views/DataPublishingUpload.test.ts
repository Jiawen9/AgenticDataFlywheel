import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, h, nextTick, ref, ssrContextKey } from 'vue'

const requests = vi.hoisted(() => ({
  datasetReleaseCandidates: vi.fn(async () => []), datasetReleases: vi.fn(async () => []),
  datasetUploadCapabilities: vi.fn(async () => ({ internal: { configured: true, directory_upload: true, default_prefix: 'default/', bucket: 'bucket', region: 'region' } })),
  listPipelines: vi.fn(async () => []), uploadDatasetRelease: vi.fn(), datasetUploadJob: vi.fn(),
}))
vi.mock('@/api', () => ({ api: requests, datasetReleaseExcelUrl: () => '' }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }) }))
vi.mock('@/composables/useBatchLifecycle', () => ({ useBatchLifecycle: () => ({ notice: ref(null) }) }))
vi.mock('@/composables/useDisplayIdentifiers', () => ({ useDisplayIdentifiers: () => ({ displayId: (_: string, id: string) => id, matches: () => true }) }))
vi.mock('@/composables/usePipelineContext', () => ({ usePipelineContext: () => ({
  historyOnly: ref(false), isPipelineRoute: ref(false), releaseId: ref(''), refresh: async () => {},
}) }))
vi.mock('element-plus', async importOriginal => ({ ...await importOriginal<object>(), ElMessage: { error: vi.fn(), success: vi.fn(), warning: vi.fn() } }))
import DataPublishingView from './DataPublishingView.vue'

const apps: Array<{ unmount(): void }> = []
const flush = async () => { for (let i = 0; i < 10; i++) { await Promise.resolve(); await nextTick() } }
const release = (summary?: object) => ({ release_id: 'release-one', name: '发布', excel_paths: [{ available: true }], internal_upload: summary })
const job = { job_id: 'job-one', mode: 'internal', status: 'uploading', folder_upload: true, release_id: 'release-one' }
function mount() {
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const component = { ...DataPublishingView, render: () => h('div') }
  const app = renderer.createApp({ render: () => h(component) })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); apps.push(app)
  return { app, state: app._instance!.subTree.component!.setupState as Record<string, any> }
}
beforeEach(() => {
  vi.useFakeTimers(); vi.clearAllMocks()
  const storage = new Map<string, string>()
  vi.stubGlobal('localStorage', { getItem: (key: string) => storage.get(key) || null, setItem: (key: string, value: string) => storage.set(key, value), removeItem: (key: string) => storage.delete(key) })
  requests.uploadDatasetRelease.mockResolvedValue(job)
  requests.datasetUploadJob.mockResolvedValue(job)
})
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('publishing upload interaction', () => {
  it('uses an editable destination dialog and keeps polling after it closes', async () => {
    const { state } = mount(); await flush()
    state.startUpload(release()); expect(state.uploadDialogVisible).toBe(true)
    expect(requests.uploadDatasetRelease).not.toHaveBeenCalled()
    state.destinationPrefix = ' new-target/ '
    await state.confirmUpload(); await flush()
    expect(requests.uploadDatasetRelease).toHaveBeenCalledWith('release-one', 'internal', { include_trajectories: true, destination_prefix: 'new-target/' })
    expect(state.uploadDialogVisible).toBe(false)
    const calls = requests.datasetUploadJob.mock.calls.length
    await vi.advanceTimersByTimeAsync(801)
    expect(requests.datasetUploadJob.mock.calls.length).toBeGreaterThan(calls)
  })
  it('allows a successful release to choose another target and defaults retry to the original', async () => {
    const { state } = mount(); await flush()
    const previous = { job_id: 'job-failed', status: 'succeeded', folder_upload: true, destination: { prefix: 'old/', bucket: 'original-bucket', region: 'original-region' } }
    expect(state.uploadDisabled(release(previous))).toBe(false)
    expect(state.uploadDisabled({ ...release(previous), source_kind: 'external_manual' })).toBe(true)
    state.startUpload(release(previous)); expect(state.destinationPrefix).toBe('old/')
    expect(state.retryUploadId).toBeUndefined()
    state.startUpload(release({ ...previous, status: 'failed' })); expect(state.destinationPrefix).toBe('old/')
    state.destinationPrefix = '/old//'
    expect(state.uploadDestination.bucket).toBe('original-bucket')
    await state.confirmUpload()
    expect(requests.uploadDatasetRelease.mock.calls[0]?.[2]).toEqual({ include_trajectories: true, destination_prefix: '/old//', retry_job_id: 'job-failed' })
  })
  it('uses current target settings and drops retry identity when a failed job changes prefix', async () => {
    const { state } = mount(); await flush()
    state.startUpload(release({ job_id: 'old-job', status: 'interrupted', folder_upload: true,
      destination: { prefix: 'old/', bucket: 'old-bucket', region: 'old-region' } }))
    state.destinationPrefix = 'another/'
    expect(state.uploadDestination.bucket).toBe('bucket')
    await state.confirmUpload()
    expect(requests.uploadDatasetRelease.mock.calls[0]?.[2]).toEqual({ include_trajectories: true, destination_prefix: 'another/' })
  })
  it('allows folder retry when the already confirmed published workbook is no longer available', async () => {
    const { state } = mount(); await flush()
    const previous = { job_id: 'partly-uploaded', status: 'failed', folder_upload: true,
      destination: { prefix: 'old/', bucket: 'bucket', region: 'region' } }
    const missingExcel = { ...release(previous), excel_paths: [{ available: false }] }
    expect(state.uploadDisabled(missingExcel)).toBe(false)
    state.startUpload(missingExcel); await state.confirmUpload()
    expect(requests.uploadDatasetRelease.mock.calls[0]?.[2].retry_job_id).toBe('partly-uploaded')
    state.activeUpload = null
    expect(state.uploadDisabled({ ...missingExcel, source_kind: 'external_manual' })).toBe(true)
  })
  it('keeps an existing job on polling errors and ignores a response after unmount', async () => {
    const { state, app } = mount(); await flush()
    state.startUpload(release()); await state.confirmUpload(); await flush()
    requests.datasetUploadJob.mockRejectedValueOnce(new Error('offline'))
    await vi.advanceTimersByTimeAsync(801)
    expect(state.activeUpload.job_id).toBe('job-one')
    expect(state.pollError).toContain('offline')
    let resolve!: (value: object) => void
    requests.datasetUploadJob.mockReturnValueOnce(new Promise(done => { resolve = done }))
    await vi.advanceTimersByTimeAsync(3001)
    app.unmount(); apps.splice(apps.indexOf(app), 1)
    resolve({ ...job, job_id: 'late' }); await flush()
    expect(state.activeUpload.job_id).toBe('job-one')
  })
})
