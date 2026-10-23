import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, h, nextTick, ssrContextKey } from 'vue'
import ModelIterationPanel from './ModelIterationPanel.vue'

const mocks = vi.hoisted(() => ({ config: vi.fn(), capability: vi.fn(), profiles: vi.fn(), sources: vi.fn(), preview: vi.fn(), start: vi.fn(), cancel: vi.fn(), confirm: vi.fn(), requestId: vi.fn(), save: vi.fn(), evaluations: vi.fn(), poll: undefined as undefined | ((signal: AbortSignal, current: () => boolean) => Promise<void>) }))
vi.mock('@/phoneFactoryApi', () => ({ newRunRequestId: mocks.requestId, modelIterationApi: { evaluationConfig: mocks.config, evaluationCapabilities: mocks.capability, evaluationProfiles: mocks.profiles, evaluationSources: mocks.sources, evaluationPreview: mocks.preview, remoteStartEvaluation: mocks.start, cancelEvaluation: mocks.cancel, saveEvaluationConfig: mocks.save, evaluations: mocks.evaluations } }))
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
  const app = renderer.createApp(component, { runs, onReady: (value: boolean) => events.ready.push(value), onConfig: (value: ReturnType<typeof config>) => events.config.push(value), onProfile: (value: unknown) => events.profile.push(value), onMode: (value: string) => events.mode.push(value), onStarted: (value: string) => events.started.push(value), onChanged: (value: string) => events.changed.push(value) })
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount({})
  const state = app._instance!.setupState as Record<string, any>
  return { app, events, state }
}

beforeEach(() => {
  vi.resetAllMocks()
  mocks.capability.mockResolvedValue({ ok: true, supported: true, profiles_supported: true, offline_supported: true, message: '支持' })
  mocks.config.mockResolvedValue(config())
  mocks.profiles.mockResolvedValue({ profiles: [{ id: 'v1', name: '规则一', digest: 'hash-1' }, { id: 'v2', name: '规则二', digest: 'hash-2' }], errors: [] })
  mocks.sources.mockResolvedValue({ sources: [{ run_id: 'source-a', trajectory_count: 1 }, { run_id: 'source-b', trajectory_count: 1 }], allowed_roots: ['D:/trajectories'] })
  let sequence = 0; mocks.requestId.mockImplementation(() => `request-${++sequence}`)
})

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
    await view.state.startOffline(); await flush()
    expect(mocks.start.mock.calls[0][0]).toEqual(mocks.start.mock.calls[1][0])
    expect(mocks.start.mock.calls[1][0]).toEqual({ request_id: 'request-1', evaluator_config: config(), evaluation_profile_id: 'v1', evaluation_profile_digest: 'hash-1', checklist_source: 'auto', preview_id: 'snapshot-a' })
    expect(view.events.started).toEqual(['eval-new'])
    await view.state.startOffline(); await flush()
    expect(mocks.start.mock.calls[2][0].request_id).toBe('request-2')
    view.state.selectedProfileId = 'v2'; await flush(); await view.state.startOffline(); await flush()
    expect(mocks.start.mock.calls[3][0]).toMatchObject({ evaluation_profile_id: 'v2', evaluation_profile_digest: 'hash-2', request_id: 'request-3' })
    view.app.unmount()
  })

  it('blocks invalid or empty previews and aborts preview work when unmounted', async () => {
    const view = mount(); await flush()
    view.state.executionMode = 'evaluate_only'; view.state.sourceKind = 'directory'; view.state.sourcePath = 'D:/trajectories'; await flush()
    mocks.preview.mockResolvedValueOnce({ ...preview('invalid'), valid: false, errors: ['截图缺失'] })
    await view.state.previewSource(); await view.state.startOffline()
    expect(view.state.preview.errors).toEqual(['截图缺失']); expect(mocks.start).not.toHaveBeenCalled()
    mocks.preview.mockResolvedValueOnce({ ...preview('empty'), trajectory_count: 0 })
    await view.state.previewSource(); await view.state.startOffline(); expect(mocks.start).not.toHaveBeenCalled()
    const pending = deferred<ReturnType<typeof preview>>(); mocks.preview.mockReturnValueOnce(pending.promise)
    const work = view.state.previewSource(); await flush(); view.app.unmount()
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
