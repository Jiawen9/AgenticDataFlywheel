import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, h, nextTick, ref, ssrContextKey } from 'vue'
import ModelIterationPanel from './ModelIterationPanel.vue'
import { useEvaluationProgress } from '@/composables/useEvaluationProgress'

const mocks = vi.hoisted(() => ({ config: vi.fn(), capability: vi.fn(), profiles: vi.fn(), sources: vi.fn(), preview: vi.fn(), previewState: vi.fn(), previewControl: vi.fn(), retryPreparation: vi.fn(), start: vi.fn(), cancel: vi.fn(), confirm: vi.fn(), requestId: vi.fn(), save: vi.fn(), evaluations: vi.fn(), poll: undefined as undefined | ((signal: AbortSignal, current: () => boolean) => Promise<void>) }))
vi.mock('@/phoneFactoryApi', () => ({ newRunRequestId: mocks.requestId, modelIterationApi: { evaluationConfig: mocks.config, evaluationCapabilities: mocks.capability, evaluationProfiles: mocks.profiles, evaluationSources: mocks.sources, evaluationPreview: mocks.preview, evaluationPreviewState: mocks.previewState, evaluationPreviewControl: mocks.previewControl, retryEvaluationPreparation: mocks.retryPreparation, remoteStartEvaluation: mocks.start, cancelEvaluation: mocks.cancel, saveEvaluationConfig: mocks.save, evaluations: mocks.evaluations } }))
vi.mock('element-plus', () => ({ ElMessageBox: { confirm: mocks.confirm } }))
vi.mock('@/composables/useFactoryPolling', () => ({ useFactoryPolling: (work: typeof mocks.poll) => { mocks.poll = work; return { start() {} } } }))
const config = () => ({ text: { base_url: 'http://text/v1/', model: 'text', api_key_env: 'TEXT_KEY' }, vision: { base_url: 'http://vision/v1/', model: 'vision', api_key_env: 'VISION_KEY' } })
const preview = (id: string) => ({ preview_id: id, valid: true, trajectory_count: 1, cases: [{ source_trajectory_id: id, case_id: 'case-1', task: '测试任务' }], errors: [], source: { run_id: id }, source_digest: id })
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
const flush = async () => { for (let index = 0; index < 5; index++) { await Promise.resolve(); await nextTick() } }

// Run the real component setup and lifecycle without a browser. The repository's
// Node test environment compiles Vue templates for SSR, so only rendering is stubbed.
function mount(runs: Array<{ run_id: string; status: string }> = []) {
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const events = { ready: [] as boolean[], config: [] as ReturnType<typeof config>[], profile: [] as any[], mode: [] as string[], started: [] as string[], changed: [] as string[] }
  const component = { ...ModelIterationPanel, render: () => h('div') }
  const app = renderer.createApp({ setup() {
    const progress = useEvaluationProgress(ref(runs), value => { events.changed.push(value) })
    return () => h(component, { runs, progress, onReady: (value: boolean) => events.ready.push(value), onConfig: (value: ReturnType<typeof config>) => events.config.push(value), onProfile: (value: unknown) => events.profile.push(value), onMode: (value: string) => events.mode.push(value), onStarted: (value: string) => { progress.selectRun(value); events.started.push(value) } })
  } })
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount({})
  const state = app._instance!.subTree.component!.setupState as Record<string, any>
  return { app, events, state }
}

beforeEach(() => {
  vi.resetAllMocks()
  const saved = new Map<string, string>()
  vi.stubGlobal('sessionStorage', { getItem: (key: string) => saved.get(key) || null, setItem: (key: string, value: string) => { saved.set(key, value) } })
  mocks.capability.mockResolvedValue({ ok: true, supported: true, profiles_supported: true, offline_supported: true, offline_evaluation_jobs: { version: 1 }, message: '支持' })
  mocks.config.mockResolvedValue(config())
  mocks.profiles.mockResolvedValue({ profiles: [{ id: 'v1', name: '规则一', digest: 'hash-1' }, { id: 'v2', name: '规则二', digest: 'hash-2' }], errors: [] })
  mocks.sources.mockResolvedValue({ sources: [{ run_id: 'source-a', trajectory_count: 1 }, { run_id: 'source-b', trajectory_count: 1 }], allowed_roots: ['D:/trajectories'] })
  let sequence = 0; mocks.requestId.mockImplementation(() => `request-${++sequence}`)
})

afterEach(() => vi.unstubAllGlobals())

describe('model iteration interactions', () => {
  it('serializes loading and saving so a late reload cannot overwrite saved settings', async () => {
    const loading = deferred<ReturnType<typeof config>>(); mocks.config.mockReturnValue(loading.promise)
    const view = mount(); await flush()
    expect(view.state.loading).toBe(true)
    await view.state.save(); expect(mocks.save).not.toHaveBeenCalled()
    loading.resolve(config()); await flush()
    view.state.draft.text.model = 'updated-text'
    await flush(); expect(view.events.ready.at(-1)).toBe(false)
    const saved = deferred<ReturnType<typeof config>>(); mocks.save.mockReturnValue(saved.promise)
    const saving = view.state.save(); await flush()
    await view.state.load(); expect(mocks.config).toHaveBeenCalledTimes(1)
    const updated = config(); updated.text.model = 'updated-text'; saved.resolve(updated); await saving; await flush()
    expect(view.events.config.at(-1)?.text.model).toBe('updated-text')
    expect(view.events.ready.at(-1)).toBe(true)
    view.app.unmount()
  })

  it('ignores a late result after selecting another run', async () => {
    const view = mount([{ run_id: 'eval-a', status: 'running' }, { run_id: 'eval-b', status: 'running' }]); await flush()
    view.state.selectedRun = 'eval-a'; await flush()
    const old = deferred<any>(); mocks.evaluations.mockReturnValueOnce(old.promise)
    const first = mocks.poll!(new AbortController().signal, () => true)
    view.state.selectedRun = 'eval-b'; await flush()
    const result = { run_id: 'eval-b', summary: { total: 1, success_rate: null }, cases: [{ case_key: 'B' }] }
    mocks.evaluations.mockResolvedValueOnce(result)
    await mocks.poll!(new AbortController().signal, () => true); await flush()
    old.resolve({ ...result, run_id: 'eval-a', cases: [{ case_key: 'A' }] }); await first; await flush()
    expect(view.state.results.cases).toEqual([{ case_key: 'B' }])
    view.app.unmount()
  })

  it('does not call new endpoints or enable starts against an older collector', async () => {
    mocks.capability.mockResolvedValue({ ok: true, supported: true, message: '旧版异步评测' })
    const view = mount(); await flush()
    expect(view.events.ready.at(-1)).toBe(false)
    expect(mocks.profiles).not.toHaveBeenCalled(); expect(mocks.sources).not.toHaveBeenCalled()
    view.state.executionMode = 'evaluate_only'; view.state.sourceRunId = 'source-a'; await flush()
    await view.state.previewSource(); await view.state.startOffline()
    expect(mocks.preview).not.toHaveBeenCalled(); expect(mocks.start).not.toHaveBeenCalled()
    view.app.unmount()
  })

  it('invalidates source previews immediately and rejects a late response for a previous source', async () => {
    const view = mount(); await flush()
    view.state.executionMode = 'evaluate_only'; view.state.sourceRunId = 'source-a'; await flush()
    const old = deferred<ReturnType<typeof preview>>(); mocks.preview.mockReturnValueOnce(old.promise)
    const first = view.state.previewSource(); await flush()
    const oldSignal = mocks.preview.mock.calls[0][1]
    view.state.sourceRunId = 'source-b'
    expect(oldSignal.aborted).toBe(true); expect(view.state.preview).toBeNull()
    mocks.preview.mockResolvedValueOnce(preview('b'))
    await view.state.previewSource(); await flush()
    old.resolve(preview('a')); await first; await flush()
    expect(view.state.preview.preview_id).toBe('b')
    expect(view.state.canStartOffline).toBe(true)
    view.state.sourceKind = 'directory'; view.state.sourcePath = 'D:/trajectories/new'
    expect(view.state.preview).toBeNull(); expect(view.state.canStartOffline).toBe(false)
    view.app.unmount()
  })

  it('retries an uncertain start with the same identity, then creates a new run for intentional reevaluation', async () => {
    const view = mount(); await flush()
    view.state.executionMode = 'evaluate_only'; view.state.sourceRunId = 'source-a'; await flush()
    mocks.preview.mockResolvedValue(preview('snapshot-a')); await view.state.previewSource(); await flush()
    mocks.start.mockRejectedValueOnce(new Error('连接中断')).mockResolvedValue({ ok: true, run_id: 'eval-new' })
    await view.state.startOffline(); await flush()
    expect(view.state.startError).toContain('原参数重试')
    await view.state.recoverStart(); await flush()
    expect(mocks.start.mock.calls[0][0]).toEqual(mocks.start.mock.calls[1][0])
    expect(mocks.start.mock.calls[1][0]).toEqual({ request_id: 'request-2', evaluator_config: config(), evaluation_profile_id: 'v1', evaluation_profile_digest: 'hash-1', checklist_source: 'auto', preview_id: 'snapshot-a' })
    expect(view.events.started).toEqual(['eval-new'])
    await view.state.startOffline(); await flush()
    expect(mocks.start.mock.calls[2][0].request_id).toBe('request-3')
    view.state.selectedProfileId = 'v2'; await flush(); await view.state.startOffline(); await flush()
    expect(mocks.start.mock.calls[3][0]).toMatchObject({ evaluation_profile_id: 'v2', evaluation_profile_digest: 'hash-2', request_id: 'request-4' })
    view.app.unmount()
  })

  it('blocks invalid or empty previews and aborts preview work when unmounted', async () => {
    const view = mount(); await flush()
    view.state.executionMode = 'evaluate_only'; view.state.sourceKind = 'directory'; view.state.sourcePath = 'D:/trajectories'; await flush()
    mocks.preview.mockResolvedValueOnce({ ...preview('invalid'), valid: false, errors: ['截图缺失'] })
    await view.state.previewSource(); await view.state.startOffline()
    expect(view.state.preview.errors).toEqual(['截图缺失']); expect(mocks.start).not.toHaveBeenCalled()
    mocks.preview.mockResolvedValueOnce({ ...preview('empty'), trajectory_count: 0 })
    await view.state.previewSource(true); await view.state.startOffline(); expect(mocks.start).not.toHaveBeenCalled()
    const pending = deferred<ReturnType<typeof preview>>(); mocks.preview.mockReturnValueOnce(pending.promise)
    const work = view.state.previewSource(true); await flush(); view.app.unmount()
    expect(mocks.preview.mock.calls.at(-1)![1].aborted).toBe(true)
    pending.resolve(preview('late')); await work; expect(view.state.preview).toBeNull()
  })
  it('cancels the selected independent run only after confirmation and refreshes its results', async () => {
    const view = mount([{ run_id: 'offline-a', status: 'running', execution_mode: 'evaluate_only' } as any]); await flush()
    mocks.confirm.mockRejectedValueOnce('cancel')
    await view.state.cancelRun(); expect(mocks.cancel).not.toHaveBeenCalled()
    mocks.confirm.mockResolvedValueOnce('confirm'); mocks.cancel.mockResolvedValue({ run_id: 'offline-a', status: 'cancelled' })
    await view.state.cancelRun()
    expect(mocks.confirm.mock.calls.at(-1)![0]).not.toContain('采集及')
    expect(mocks.cancel).toHaveBeenCalledWith('offline-a'); expect(view.events.changed).toEqual(['offline-a'])
    view.app.unmount()
  })
})

it('polls asynchronous previews and drops a late page after source selection changes', async () => {
  vi.useFakeTimers()
  const view = mount()
  try {
    await flush()
    view.state.executionMode = 'evaluate_only'; view.state.sourceRunId = 'source-a'; await flush()
    mocks.preview.mockResolvedValue({ ...preview('prepare'), valid: false, status: 'preparing', trajectory_count: 0, cases: [] })
    await view.state.previewSource(); await flush()
    expect(view.state.canStartOffline).toBe(false)
    mocks.previewState.mockResolvedValue({ ...preview('prepare'), status: 'completed', coverage: { total: 2, included: 1, excluded: 1 }, errors: [{ error: 'missing image' }] })
    await vi.advanceTimersByTimeAsync(2000); await flush()
    expect(view.state.canStartOffline).toBe(true)
    expect(view.state.preview.coverage.excluded).toBe(1)
    const old = deferred<any>(); mocks.previewState.mockReturnValueOnce(old.promise)
    const page = view.state.loadPreviewPage(); await flush()
    view.state.sourceRunId = 'source-b'; await flush()
    old.resolve(preview('prepare')); await page; await flush()
    expect(view.state.preview).toBeNull()
  } finally { view.app.unmount(); vi.useRealTimers() }
})

describe('durable offline evaluation jobs', () => {
  it('replays a lost preview acknowledgement after refresh and only rechecks with a new request on explicit action', async () => {
    const first = mount(); await flush()
    first.state.executionMode = 'evaluate_only'; first.state.sourceKind = 'directory'; first.state.sourcePath = 'D:/轨迹/case-01'; await flush()
    mocks.preview.mockRejectedValueOnce(new Error('504'))
    await first.state.previewSource()
    const request = mocks.preview.mock.calls[0][0]
    expect(request).toEqual({ source_path: 'D:/轨迹/case-01', request_id: 'request-1' })
    first.app.unmount()
    mocks.preview.mockResolvedValue({ ...preview('accepted'), status: 'completed', job_version: 1, revision: 1 })
    const restored = mount(); await flush(); await flush()
    expect(restored.state.executionMode).toBe('evaluate_only')
    expect(restored.state.sourcePath).toBe('D:/轨迹/case-01')
    expect(mocks.preview.mock.calls[1][0]).toEqual(request)
    await restored.state.previewSource(true)
    expect(mocks.preview.mock.calls[2][0].request_id).not.toBe(request.request_id)
    restored.app.unmount()
  })

  it('restores an accepted preview by querying its ID without starting another scan', async () => {
    const first = mount(); await flush()
    first.state.executionMode = 'evaluate_only'; first.state.sourceRunId = 'source-a'; await flush()
    mocks.preview.mockResolvedValue({ ...preview('accepted'), status: 'preparing', job_version: 1, revision: 1, phase: 'scanning', progress: { discovered: 12 } })
    await first.state.previewSource(); first.app.unmount()
    mocks.previewState.mockResolvedValue({ ...preview('accepted'), status: 'completed', job_version: 1, revision: 2 })
    const restored = mount(); await flush(); await flush()
    expect(mocks.preview).toHaveBeenCalledTimes(1)
    expect(mocks.previewState.mock.calls[0][0]).toBe('accepted')
    expect(restored.state.canStartOffline).toBe(true)
    restored.app.unmount()
  })

  it('does not let an old preview response replace the source restored after refresh', async () => {
    const first = mount(); await flush()
    first.state.executionMode = 'evaluate_only'; first.state.sourceRunId = 'source-a'; await flush()
    const late = deferred<any>(); mocks.preview.mockReturnValueOnce(late.promise)
    const pending = first.state.previewSource()
    first.state.sourceRunId = 'source-b'; await flush()
    mocks.preview.mockResolvedValueOnce({ ...preview('b'), status: 'completed' })
    await first.state.previewSource()
    late.resolve({ ...preview('a'), status: 'completed' }); await pending
    first.app.unmount()
    mocks.previewState.mockResolvedValue({ ...preview('b'), status: 'completed' })
    const restored = mount(); await flush(); await flush()
    expect(restored.state.sourceRunId).toBe('source-b')
    expect(mocks.previewState.mock.calls.at(-1)[0]).toBe('b')
    restored.app.unmount()
  })

  it('keeps the snapshot and polls after a temporary state-query failure without issuing another submit', async () => {
    vi.useFakeTimers()
    const view = mount()
    try {
      await flush(); view.state.executionMode = 'evaluate_only'; view.state.sourceRunId = 'source-a'; await flush()
      const preparing = { ...preview('pending'), valid: false, status: 'preparing', job_version: 1, revision: 1, phase: 'scanning', progress: { discovered: 20 } }
      mocks.preview.mockResolvedValue(preparing); await view.state.previewSource()
      mocks.previewState.mockRejectedValueOnce(new Error('gateway'))
      await vi.advanceTimersByTimeAsync(2000)
      expect(view.state.preview.progress.discovered).toBe(20)
      expect(view.state.sourceError).toContain('保留本次任务')
      mocks.previewState.mockResolvedValue({ ...preparing, status: 'completed', valid: true })
      await vi.advanceTimersByTimeAsync(2000)
      expect(view.state.canStartOffline).toBe(true)
      expect(mocks.preview).toHaveBeenCalledTimes(1)
    } finally { view.app.unmount(); vi.useRealTimers() }
  })

  it('reuses a cancellation identity after response loss and fences a preview retry by revision', async () => {
    const view = mount(); await flush()
    view.state.executionMode = 'evaluate_only'; view.state.sourceRunId = 'source-a'; await flush()
    mocks.preview.mockResolvedValue({ ...preview('pending'), valid: false, status: 'preparing', job_version: 1, revision: 4 })
    await view.state.previewSource()
    mocks.previewControl.mockRejectedValueOnce(new Error('connection lost')).mockResolvedValueOnce({ ...preview('pending'), valid: false, status: 'cancelled', job_version: 1, revision: 5 })
    await view.state.controlPreview('cancel'); await view.state.controlPreview('cancel')
    expect(mocks.previewControl.mock.calls[0]).toEqual(mocks.previewControl.mock.calls[1])
    expect(mocks.previewControl.mock.calls[0]).toEqual(['pending', 'cancel', { request_id: 'request-2', expected_revision: 4 }])
    view.state.preview = { ...preview('pending'), valid: false, status: 'failed', job_version: 1, revision: 6, retryable: true }
    mocks.previewControl.mockResolvedValueOnce({ ...preview('pending'), valid: false, status: 'preparing', job_version: 1, revision: 7 })
    await view.state.controlPreview('retry')
    expect(mocks.previewControl.mock.calls[2]).toEqual(['pending', 'retry', { request_id: 'request-3', expected_revision: 6 }])
    expect(view.state.previewLoading).toBe(true)
    view.app.unmount()
  })

  it('does not restore preparing when an older poll arrives after cancellation', async () => {
    vi.useFakeTimers()
    const view = mount()
    try {
      await flush(); view.state.executionMode = 'evaluate_only'; view.state.sourceRunId = 'source-a'; await flush()
      const preparing = { ...preview('pending'), valid: false, status: 'preparing', job_version: 1, revision: 4 }
      mocks.preview.mockResolvedValue(preparing); await view.state.previewSource()
      const late = deferred<any>(); mocks.previewState.mockReturnValueOnce(late.promise)
      await vi.advanceTimersByTimeAsync(2000)
      mocks.previewControl.mockResolvedValueOnce({ ...preparing, status: 'cancelled', revision: 5 })
      await view.state.controlPreview('cancel')
      late.resolve(preparing); await flush()
      expect(view.state.preview.status).toBe('cancelled')
      expect(view.state.previewLoading).toBe(false)
      await vi.advanceTimersByTimeAsync(4000)
      expect(mocks.previewState).toHaveBeenCalledTimes(1)
    } finally { view.app.unmount(); vi.useRealTimers() }
  })

  it('polls an asynchronously accepted retry and exposes a rejected control before allowing a fresh operation', async () => {
    vi.useFakeTimers()
    const view = mount()
    try {
      await flush(); view.state.executionMode = 'evaluate_only'; view.state.sourceRunId = 'source-a'; await flush()
      const failed = { ...preview('job'), valid: false, status: 'failed', phase: 'failed', job_version: 1, revision: 4, retryable: true }
      mocks.preview.mockResolvedValue(failed); await view.state.previewSource()
      mocks.previewControl.mockResolvedValueOnce({ ...failed, phase: 'retrying' })
      await view.state.controlPreview('retry')
      expect(view.state.previewControlPending).toBe(true)
      await view.state.controlPreview('retry')
      expect(mocks.previewControl).toHaveBeenCalledTimes(1)
      mocks.previewState.mockResolvedValueOnce({ ...failed, revision: 5, control_error: '任务状态已变化，请刷新后重试' })
      await vi.advanceTimersByTimeAsync(2000)
      expect(view.state.preview.control_error).toContain('任务状态已变化')
      expect(view.state.previewControlPending).toBe(false)
      mocks.previewControl.mockResolvedValueOnce({ ...failed, revision: 5, phase: 'retrying' })
      await view.state.controlPreview('retry')
      expect(mocks.previewControl.mock.calls[1][2]).toEqual({ request_id: 'request-3', expected_revision: 5 })
    } finally { view.app.unmount(); vi.useRealTimers() }
  })

  it('restores an uncertain start with its frozen evaluator settings despite changed defaults', async () => {
    const first = mount(); await flush()
    first.state.executionMode = 'evaluate_only'; first.state.sourceRunId = 'source-a'; await flush()
    mocks.preview.mockResolvedValue({ ...preview('ready'), status: 'completed' }); await first.state.previewSource()
    mocks.start.mockRejectedValueOnce(new Error('connection lost'))
    await first.state.startOffline(); const frozen = mocks.start.mock.calls[0][0]
    await first.state.previewSource(true); expect(mocks.preview).toHaveBeenCalledTimes(1)
    first.app.unmount()
    const changed = config(); changed.text.model = 'new-default'
    mocks.config.mockResolvedValue(changed); mocks.previewState.mockResolvedValue({ ...preview('ready'), status: 'completed' })
    mocks.start.mockResolvedValue({ ok: true, run_id: 'same-run' })
    const restored = mount(); await flush(); await flush()
    expect(restored.state.canStartOffline).toBe(false)
    await restored.state.recoverStart()
    expect(mocks.start.mock.calls[1][0]).toEqual(frozen)
    expect(restored.events.started).toEqual(['same-run'])
    restored.app.unmount()
  })

  it('retries a paused input-preparation job without resubmitting a collection', async () => {
    const view = mount([{ run_id: 'offline-a', status: 'failed', execution_mode: 'evaluate_only', preparation: { status: 'paused', revision: 8, retryable: true, error: 'disk full' } } as any]); await flush()
    expect(view.state.canRetryPreparation).toBe(true)
    expect(view.state.canCancel).toBe(true)
    mocks.retryPreparation.mockRejectedValueOnce(new Error('lost ack')).mockResolvedValueOnce({ run_id: 'offline-a', status: 'preparing' })
    await view.state.retryPreparation(); await view.state.retryPreparation()
    expect(mocks.retryPreparation.mock.calls[0]).toEqual(mocks.retryPreparation.mock.calls[1])
    expect(mocks.retryPreparation.mock.calls[0]).toEqual(['offline-a', { request_id: 'request-1', expected_revision: 8 }])
    expect(mocks.start).not.toHaveBeenCalled()
    expect(view.events.started).toEqual(['offline-a'])
    view.app.unmount()
  })

  it('requires asynchronous-job support for new previews from either source', async () => {
    mocks.capability.mockResolvedValue({ ok: true, supported: true, profiles_supported: true, offline_supported: true })
    const view = mount(); await flush()
    view.state.executionMode = 'evaluate_only'; view.state.sourceKind = 'directory'; view.state.sourcePath = 'D:/old'; await flush()
    await view.state.previewSource(); expect(mocks.preview).not.toHaveBeenCalled()
    view.state.sourceKind = 'run'; view.state.sourceRunId = 'source-a'; await flush()
    await view.state.previewSource()
    expect(mocks.preview).not.toHaveBeenCalled()
    expect(view.state.canStartOffline).toBe(false)
    view.app.unmount()
  })
})
