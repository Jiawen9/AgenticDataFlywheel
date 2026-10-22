import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, h, nextTick, ssrContextKey } from 'vue'
import ModelIterationPanel from './ModelIterationPanel.vue'

const mocks = vi.hoisted(() => ({ config: vi.fn(), capability: vi.fn(), save: vi.fn(), evaluations: vi.fn(), poll: undefined as undefined | ((signal: AbortSignal, current: () => boolean) => Promise<void>) }))
vi.mock('@/phoneFactoryApi', () => ({ modelIterationApi: { evaluationConfig: mocks.config, evaluationCapabilities: mocks.capability, saveEvaluationConfig: mocks.save, evaluations: mocks.evaluations } }))
vi.mock('@/composables/useFactoryPolling', () => ({ useFactoryPolling: (work: typeof mocks.poll) => { mocks.poll = work; return { start() {} } } }))
const config = () => ({ text: { base_url: 'http://text/v1/', model: 'text', api_key_env: 'TEXT_KEY' }, vision: { base_url: 'http://vision/v1/', model: 'vision', api_key_env: 'VISION_KEY' } })
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
const flush = async () => { for (let index = 0; index < 5; index++) { await Promise.resolve(); await nextTick() } }

// Run the real component setup and lifecycle without a browser. The repository's
// Node test environment compiles Vue templates for SSR, so only rendering is stubbed.
function mount(runs: Array<{ run_id: string; status: string }> = []) {
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const events = { ready: [] as boolean[], config: [] as ReturnType<typeof config>[] }
  const component = { ...ModelIterationPanel, render: () => h('div') }
  const app = renderer.createApp(component, { runs, onReady: (value: boolean) => events.ready.push(value), onConfig: (value: ReturnType<typeof config>) => events.config.push(value) })
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount({})
  const state = app._instance!.setupState as Record<string, any>
  return { app, events, state }
}

beforeEach(() => { vi.resetAllMocks(); mocks.capability.mockResolvedValue({ ok: true, supported: true, message: '支持' }); mocks.config.mockResolvedValue(config()) })

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
})
