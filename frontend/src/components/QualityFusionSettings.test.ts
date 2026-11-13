import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, h, nextTick, ssrContextKey } from 'vue'
import type { QualitySettings } from '@/types'
import QualityFusionSettings from './QualityFusionSettings.vue'

const mocks = vi.hoisted(() => ({ load: vi.fn(), save: vi.fn() }))
vi.mock('@/api', () => ({ api: { qualitySettings: mocks.load, saveQualitySettings: mocks.save } }))
const profile = { id: 'v1', name: '冻结规则', digest: 'digest-v1' }
const settings = (selected = true): QualitySettings => ({ supported: true, profiles: [profile], model_configured: true, execution_backend: 'platform', evaluator_config: { text: { base_url: 'http://text/v1', model: 'text-model', api_key_env: '' }, vision: { base_url: 'http://vision/v1', model: 'vision-model', api_key_env: 'VISION_KEY' } }, evaluation_profile_id: selected ? profile.id : '', evaluation_profile_digest: selected ? profile.digest : '' })
const flush = async () => { for (let index = 0; index < 5; index++) { await Promise.resolve(); await nextTick() } }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { resolve, promise } }
const mounted: ReturnType<typeof mount>[] = []
function mount() {
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const ready: boolean[] = []
  const app = renderer.createApp({ setup: () => () => h({ ...QualityFusionSettings, render: () => h('div') }, { onReady: (value: boolean) => ready.push(value) }) })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({})
  const result = { app, ready, state: app._instance!.subTree.component!.setupState as Record<string, any> }
  mounted.push(result)
  return result
}
beforeEach(() => { vi.resetAllMocks(); mocks.load.mockResolvedValue(settings()) })
afterEach(() => { mounted.splice(0).forEach(view => view.app.unmount()) })

describe('quality profile settings', () => {
  it('loads platform-local settings without writing or reading model-iteration settings', async () => {
    const view = mount(); await flush()
    expect(view.ready.at(-1)).toBe(true)
    expect(mocks.save).not.toHaveBeenCalled()
  })
  it('saves only edited local model configuration and requires saving before submission', async () => {
    const view = mount(); await flush()
    view.state.models.text.model = 'new-text-model'; await flush()
    expect(view.ready.at(-1)).toBe(false)
    const config = { ...settings().evaluator_config!, text: { ...settings().evaluator_config!.text, model: 'new-text-model' } }
    mocks.save.mockResolvedValue({ ...settings(), evaluator_config: config })
    await view.state.saveModels(); await flush()
    expect(mocks.save).toHaveBeenCalledWith({ evaluator_config: config })
    expect(view.ready.at(-1)).toBe(true)
  })
  it('keeps unsaved model changes on a profile-only save and rejects a half-filled model', async () => {
    const view = mount(); await flush()
    view.state.models.text.model = ''; await view.state.saveModels()
    expect(mocks.save).not.toHaveBeenCalled(); expect(view.state.error).toContain('同时填写')
    view.state.models.text.model = 'unsaved-model'; mocks.save.mockResolvedValue(settings())
    await view.state.selectProfile('v1'); await flush()
    expect(view.state.models.text.model).toBe('unsaved-model'); expect(view.ready.at(-1)).toBe(false)
  })
  it('requires an explicit valid profile and persists only its identity before enabling submit', async () => {
    mocks.load.mockResolvedValue(settings(false))
    const view = mount(); await flush(); expect(view.ready.at(-1)).toBe(false)
    const saving = deferred<QualitySettings>(); mocks.save.mockReturnValue(saving.promise)
    const request = view.state.selectProfile('v1'); await flush()
    expect(mocks.save).toHaveBeenCalledWith({ evaluation_profile_id: 'v1', evaluation_profile_digest: 'digest-v1' })
    expect(view.ready.at(-1)).toBe(false)
    await view.state.load(); expect(mocks.load).toHaveBeenCalledTimes(1)
    saving.resolve(settings()); await request; await flush()
    expect(view.ready.at(-1)).toBe(true)
  })
  it.each([
    { supported: false, reason: '采集端需升级' },
    { model_configured: false, reason: '评测模型未配置' },
    { evaluation_profile_digest: 'changed' },
  ])('does not enable submissions with unsupported or stale settings: %o', async overrides => {
    mocks.load.mockResolvedValue({ ...settings(), ...overrides })
    const view = mount(); await flush()
    expect(view.ready.at(-1)).toBe(false)
  })
  it('keeps a save error visible and ignores a load that completes after navigation', async () => {
    mocks.save.mockRejectedValue(new Error('规则版本已变化'))
    const view = mount(); await flush(); await view.state.selectProfile('v1')
    expect(view.state.error).toContain('规则版本已变化'); expect(view.ready.at(-1)).toBe(false)
    const pending = deferred<QualitySettings>(); mocks.load.mockReturnValue(pending.promise)
    const request = view.state.load(); view.app.unmount(); const count = view.ready.length
    pending.resolve(settings()); await request
    expect(view.ready.length).toBe(count)
  })
})
