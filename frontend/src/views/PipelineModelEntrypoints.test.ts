import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import PhoneFactoryCollectionView from './PhoneFactoryCollectionView.vue'
import PipelineView from './PipelineView.vue'
import { api } from '@/api'
import { factoryBatchesApi, modelIterationApi, phoneFactoryApi, type AgentCapabilities, type FactoryBatchDetail, type FactoryConfig, type FactorySettings, type FactoryState, type RunOptions } from '@/phoneFactoryApi'
import { useDisplayIdentifiers } from '@/composables/useDisplayIdentifiers'

vi.mock('element-plus', () => ({ ElMessage: { success: vi.fn(), warning: vi.fn(), error: vi.fn() }, ElMessageBox: {} }))
// SSR deliberately does not mount browser timers. The page's real batch
// composable, model helpers, request builders and mode-specific APIs are used.
vi.mock('@/composables/useFactoryPolling', () => ({ useFactoryPolling: () => ({ start: vi.fn(), stop: vi.fn(), dispose: vi.fn() }) }))

const sampling: FactoryConfig = { sampling_enabled: true, temperature: 0.35, top_p: 0.8, use_experience_lib: false, capture_logprobs: true }
const capabilities: AgentCapabilities = {
  newagent: { available: true, collection: true, repeat_times: true, max_repeat_times: 100, capture_logprobs: true, model_iteration_evaluation: true, offline_evaluation: true, model_overrides: true },
  pipelineagent: { available: true, collection: true, repeat_times: true, max_repeat_times: 100, capture_logprobs: true, model_iteration_evaluation: true, offline_evaluation: true, model_overrides: true },
}
const defaults = (suffix = 'saved') => ({ vla: `http://execution-${suffix}.invalid/v1`, execution_model: `vla-${suffix}`, planner_config: { base_url: `http://planner-${suffix}.invalid/v1`, model: `planner-${suffix}`, api_key_env: 'PLANNER_API_KEY' } })
const settings = (suffix = 'saved'): FactorySettings => ({ ...sampling, agent_type: 'pipelineagent', repeat_times: 3, model_defaults: defaults(suffix) })
const source = (): FactoryBatchDetail => ({ schema_version: 1, batch_id: 'entrypoint-source', source_job_id: null, kind: 'manual_collection', job_status: 'succeeded', knowledge_base_version: null, created_at: '2026-10-29', task_count: 1, apps: ['AppA'], filename: 'source.xlsx', download_url: '/source.xlsx', snapshot: { tasks: [] } })
const factoryState = (): FactoryState => ({ phones: ['phone-1'], apps: ['AppA'], phoneApps: [{ phone_id: 'phone-1', app: 'AppA', status: '空闲' }], vla: ['http://legacy-execution.invalid/v1'], tasks: [{ description: '采集任务', filename: 'source.xlsx', source_batch_id: source().batch_id, status: '未运行' }] })
const cleanConfig = (value: unknown) => {
  expect(value).toEqual(sampling)
  for (const key of ['agent_type', 'model_defaults', 'newagent_model_defaults', 'planner_config', 'execution_model', 'vla', 'repeat_times']) expect(value).not.toHaveProperty(key)
}

beforeEach(() => {
  vi.spyOn(useDisplayIdentifiers(), 'resolve').mockResolvedValue(undefined)
  vi.spyOn(api, 'listPipelines').mockResolvedValue([])
  vi.spyOn(api, 'preprocessingBatches').mockResolvedValue([{ batch_id: 'ready-result', label: '已采集结果', task_count: 1, ready_trajectory_count: 1 }] as never)
  vi.spyOn(factoryBatchesApi, 'list').mockResolvedValue([source()])
  vi.spyOn(factoryBatchesApi, 'detail').mockResolvedValue(source())
  vi.spyOn(factoryBatchesApi, 'workbook').mockResolvedValue(new Blob(['test workbook']))
  for (const factory of [phoneFactoryApi, modelIterationApi]) {
    vi.spyOn(factory, 'state').mockResolvedValue(factoryState())
    vi.spyOn(factory, 'config').mockResolvedValue(settings(factory === modelIterationApi ? 'evaluation' : 'saved'))
    vi.spyOn(factory, 'agentCapabilities').mockResolvedValue({ ok: true, agent_types: capabilities })
    vi.spyOn(factory, 'addTask').mockResolvedValue(factoryState())
    vi.spyOn(factory, 'remoteStartRun').mockResolvedValue({ ok: true, run_id: 'accepted-model-run', batch_id: 'accepted-result', task_source_batch_id: source().batch_id })
    vi.spyOn(factory, 'runs').mockResolvedValue({ runs: [] })
  }
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

async function render(which: 'factory' | 'pipeline', mode: 'generate' | 'modeliter' = 'generate') {
  const app = which === 'factory' ? createSSRApp(PhoneFactoryCollectionView, { mode }) : createSSRApp(PipelineView)
  const name = which === 'factory' ? 'PhoneFactoryCollectionView' : 'PipelineView'
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] })
  app.use(router)
  // Pipeline creation navigation is outside these request contract tests.
  vi.spyOn(router, 'replace').mockResolvedValue(undefined)
  let view: any
  app.mixin({ created() { if (this.$options.__name === name) view = this.$.setupState } })
  const empty = defineComponent({ render: () => h('div') })
  for (const tag of ['button', 'icon', 'alert', 'progress', 'tag', 'table', 'table-column', 'input', 'switch', 'input-number', 'select', 'option', 'form-item', 'radio-group', 'radio-button', 'radio', 'dialog', 'form', 'drawer', 'pagination', 'popover']) app.component(`el-${tag}`, empty)
  await renderToString(app)
  if (which === 'factory') {
    await view.loadState(); await view.loadAgentCapabilities(); view.loadingFactory = false
  }
  return view
}

describe('pipeline model configuration across platform entrypoints', () => {
  it('freezes repetition count for retries and blocks invalid or unsupported counts before dispatch', async () => {
    const view = await render('factory')
    const send = vi.mocked(phoneFactoryApi.remoteStartRun)
    send.mockRejectedValueOnce(new Error('acknowledgement lost'))
    await view.handleStartTask(factoryState().tasks[0])
    const first = send.mock.calls[0]![0]
    expect(first.repeat_times).toBe(3)
    await view.handleStartTask(factoryState().tasks[0])
    expect(send.mock.calls[1]![0].request_id).toBe(first.request_id)
    view.repeatTimes = 4
    await view.handleStartTask(factoryState().tasks[0])
    expect(send.mock.calls[2]![0].repeat_times).toBe(4)
    expect(send.mock.calls[2]![0].request_id).not.toBe(first.request_id)
    expect(first.repeat_times).toBe(3)
    view.repeatTimes = 0; await view.handleStartTask(factoryState().tasks[0])
    view.repeatTimes = 101; await view.handleStartTask(factoryState().tasks[0])
    view.repeatTimes = 2
    view.agentCapabilities = { pipelineagent: { ...capabilities.pipelineagent, repeat_times: false } }
    await view.handleStartTask(factoryState().tasks[0])
    expect(send).toHaveBeenCalledTimes(3)
  })
  it('uses the real production batch runOptions callback and snapshots the current planner/executor separately from sampling', async () => {
    const view = await render('factory')
    expect(view.newVla).toBe(defaults().vla)
    expect(view.models.execution_model).toBe('vla-saved')
    cleanConfig(view.config)
    Object.assign(view.models, { execution_model: 'vla-next', planner_model: 'planner-next' })
    view.newVla = 'http://execution-next.invalid/v1'
    await view.handleStartTask(factoryState().tasks[0])
    const first = vi.mocked(phoneFactoryApi.remoteStartRun).mock.calls[0]![0]
    expect(first).toMatchObject({ agent_type: 'pipelineagent', run_mode: 'generate', repeat_times: 3, filename: 'source.xlsx', vla: 'http://execution-next.invalid/v1', execution_model: 'vla-next', planner_config: { ...defaults().planner_config, model: 'planner-next' } })
    cleanConfig(first.config)
    expect(factoryBatchesApi.workbook).toHaveBeenCalledWith(source().batch_id)
    expect(phoneFactoryApi.addTask).toHaveBeenCalledWith(expect.any(String), 'source.xlsx', btoa('test workbook'), source().batch_id)
    expect(modelIterationApi.remoteStartRun).not.toHaveBeenCalled()
    Object.assign(view.models, { execution_model: 'vla-later', planner_model: 'planner-later' })
    await view.handleStartTask(factoryState().tasks[0])
    const second = vi.mocked(phoneFactoryApi.remoteStartRun).mock.calls[1]![0]
    expect(second.execution_model).toBe('vla-later')
    expect(first.execution_model).toBe('vla-next'); expect(first.planner_config!.model).toBe('planner-next')
    expect(second.request_id).not.toBe(first.request_id)
  })

  it('sends custom batch overrides without mutating saved defaults or the next ordinary run', async () => {
    const view = await render('factory')
    const originalModels = JSON.parse(JSON.stringify(view.models))
    view.openCustomRun(factoryState().phoneApps[0])
    expect(view.runDialogModels).toEqual(originalModels)
    Object.assign(view.runDialogModels, { execution_model: 'vla-custom', planner_base_url: 'http://custom-planner.invalid/v1', planner_model: 'planner-custom', planner_api_key_env: 'CUSTOM_KEY' })
    view.runDialogRepeatTimes = 5
    view.runDialogVla = 'http://custom-execution.invalid/v1'; view.runDialogTaskKey = `batch:${source().batch_id}`
    await view.confirmRun()
    const custom = vi.mocked(phoneFactoryApi.remoteStartRun).mock.calls[0]![0]
    expect(custom).toMatchObject({ phone_id: 'phone-1', app: 'AppA', repeat_times: 5, vla: 'http://custom-execution.invalid/v1', execution_model: 'vla-custom', planner_config: { base_url: 'http://custom-planner.invalid/v1', model: 'planner-custom', api_key_env: 'CUSTOM_KEY' } })
    cleanConfig(custom.config)
    expect(view.models).toEqual(originalModels); expect(view.newVla).toBe(defaults().vla)
    expect(view.runDialogVisible).toBe(false)
    await view.handleStartTask(factoryState().tasks[0])
    const ordinary = vi.mocked(phoneFactoryApi.remoteStartRun).mock.calls[1]![0]
    expect(ordinary.repeat_times).toBe(3); expect(view.repeatTimes).toBe(3)
    expect(ordinary.execution_model).toBe('vla-saved'); expect(ordinary.planner_config).toEqual(defaults().planner_config)
  })

  it('dispatches collection evaluation through its own API with evaluator settings and selects the accepted run', async () => {
    const view = await render('factory', 'modeliter')
    view.repeatTimes = 9 // A stale or externally assigned production default cannot change evaluation collection.
    view.evaluationReady = true
    view.evaluationProfile = { id: 'rules-v1', digest: 'rules-hash' }
    view.evaluatorConfig = { text: { base_url: 'http://text.invalid/v1', model: 'text', api_key_env: 'TEXT_KEY' }, vision: { base_url: 'http://vision.invalid/v1', model: 'vision', api_key_env: 'VISION_KEY' } }
    await view.handleStartTask(factoryState().tasks[0])
    const request = vi.mocked(modelIterationApi.remoteStartRun).mock.calls[0]![0]
    expect(request).toMatchObject({ run_mode: 'modeliter', repeat_times: 1, execution_model: 'vla-evaluation', planner_config: defaults('evaluation').planner_config, vla: defaults('evaluation').vla, evaluator_config: view.evaluatorConfig, evaluation_profile_id: 'rules-v1', evaluation_profile_digest: 'rules-hash' })
    cleanConfig(request.config)
    expect(phoneFactoryApi.remoteStartRun).not.toHaveBeenCalled()
    expect(factoryBatchesApi.workbook).not.toHaveBeenCalled()
    expect(view.evaluationProgress.selectedRun.value).toBe('accepted-model-run')
    expect(modelIterationApi.runs).toHaveBeenCalled()
  })

  it('hydrates Pipeline creation defaults, preserves edits on a choices-only refresh, and nests overrides only for collection', async () => {
    const create = vi.spyOn(api, 'createPipeline').mockResolvedValue({ pipeline_id: 'created-pipeline', batch_id: 'created-result' } as never)
    const view = await render('pipeline')
    await view.loadChoices()
    expect(view.draft.agent_type).toBe('pipelineagent'); expect(view.draft.vla).toBe(defaults().vla)
    expect(view.models).toMatchObject({ execution_model: 'vla-saved', planner_model: 'planner-saved', planner_custom: true })
    expect(view.draft).not.toHaveProperty('model_defaults')
    Object.assign(view.models, { execution_model: 'edited-vla', planner_model: 'edited-planner' })
    view.draft.vla = 'http://edited-execution.invalid/v1'
    await view.loadChoices(false)
    expect(view.models.execution_model).toBe('edited-vla'); expect(view.draft.vla).toBe('http://edited-execution.invalid/v1')
    Object.assign(view.draft, { name: 'collect with models', batch_id: source().batch_id, start_mode: 'collect' }); await nextTick()
    await view.create()
    const collect = create.mock.calls[0]![0]
    expect(collect.collection_config).toMatchObject({ agent_type: 'pipelineagent', repeat_times: 3, execution_model: 'edited-vla', planner_config: { ...defaults().planner_config, model: 'edited-planner' }, vla: 'http://edited-execution.invalid/v1' })
    cleanConfig(collect.collection_config!.config)
    expect(collect).not.toHaveProperty('execution_model'); expect(collect).not.toHaveProperty('planner_config')
    Object.assign(view.draft, { name: 'continue existing', start_mode: 'existing', batch_id: 'ready-result' }); await nextTick(); await view.create()
    expect(create.mock.calls[1]![0]).not.toHaveProperty('collection_config')
    Object.assign(view.draft, { name: 'newagent collection', start_mode: 'collect', batch_id: source().batch_id, agent_type: 'newagent' }); await nextTick(); await view.create()
    expect(create.mock.calls[2]![0].collection_config).not.toHaveProperty('execution_model')
    expect(create.mock.calls[2]![0].collection_config).not.toHaveProperty('planner_config')
  })

  it.each(['generate', 'modeliter'] as const)('saves %s defaults to the correct config endpoint while sampling saves preserve an unsaved model draft', async mode => {
    const view = await render('factory', mode)
    const saved = settings(mode === 'modeliter' ? 'evaluation' : 'saved')
    const fetch = vi.fn(async (_url: string, init: RequestInit) => {
      const body = JSON.parse(String(init.body)) as FactorySettings
      return { ok: true, json: async () => ({ ...body, model_defaults: body.model_defaults || saved.model_defaults }) }
    })
    vi.stubGlobal('fetch', fetch)
    Object.assign(view.models, { execution_model: 'unsaved-executor', planner_model: 'unsaved-planner' })
    view.newVla = 'http://unsaved-execution.invalid/v1'
    await view.handleConfigChange()
    expect(fetch.mock.calls[0]![0]).toBe(mode === 'modeliter' ? '/api/model-iter/config' : '/api/phone-factory/config')
    const sampleBody = JSON.parse(String(fetch.mock.calls[0]![1].body))
    expect(sampleBody).toEqual({ ...sampling, agent_type: 'pipelineagent', ...(mode === 'generate' ? { repeat_times: 3 } : {}) })
    expect(view.models.execution_model).toBe('unsaved-executor'); expect(view.models.planner_model).toBe('unsaved-planner')
    expect(view.newVla).toBe('http://unsaved-execution.invalid/v1')
    await view.saveModelDefaults()
    const defaultBody = JSON.parse(String(fetch.mock.calls[1]![1].body))
    expect(defaultBody.model_defaults).toEqual({ vla: 'http://unsaved-execution.invalid/v1', execution_model: 'unsaved-executor', planner_config: { ...saved.model_defaults!.planner_config, model: 'unsaved-planner' } })
    if (mode === 'generate') expect(defaultBody.repeat_times).toBe(3)
    else expect(defaultBody).not.toHaveProperty('repeat_times')
    expect(defaultBody).not.toHaveProperty('planner_config'); expect(defaultBody).not.toHaveProperty('execution_model')
    cleanConfig(view.config)
  })

  it('does not send retained pipeline model drafts after switching a production run to NewAgent', async () => {
    const view = await render('factory')
    view.agentType = 'newagent'
    await view.handleStartTask(factoryState().tasks[0])
    const request: RunOptions = vi.mocked(phoneFactoryApi.remoteStartRun).mock.calls[0]![0]
    expect(request.repeat_times).toBe(3)
    expect(request.agent_type).toBe('newagent'); expect(request).not.toHaveProperty('execution_model'); expect(request).not.toHaveProperty('planner_config')
    cleanConfig(request.config)
  })

  it.each(['generate', 'modeliter'] as const)('uses isolated NewAgent defaults for %s and freezes requests independently of later edits', async mode => {
    const factory = mode === 'generate' ? phoneFactoryApi : modelIterationApi
    const saved: FactorySettings = { ...settings(mode), agent_type: 'newagent', newagent_model_defaults: { execution_model: `newagent-${mode}` } }
    vi.mocked(factory.config).mockResolvedValue(saved)
    const view = await render('factory', mode)
    expect(view.models.execution_model).toBe(`vla-${mode}`)
    expect(view.activeModels.execution_model).toBe(`newagent-${mode}`)
    expect(view.newVla).toBe(factoryState().vla[0])
    cleanConfig(view.config)
    if (mode === 'modeliter') {
      view.evaluationReady = true
      view.evaluationProfile = { id: 'rules-v1', digest: 'rules-hash' }
      view.evaluatorConfig = { text: { base_url: 'http://text.invalid/v1', model: 'text', api_key_env: 'TEXT_KEY' }, vision: { base_url: 'http://vision.invalid/v1', model: 'vision', api_key_env: 'VISION_KEY' } }
    }
    const send = vi.mocked(factory.remoteStartRun)
    send.mockRejectedValueOnce(new Error('acknowledgement lost'))
    await view.handleStartTask(factoryState().tasks[0])
    const first = send.mock.calls[0]![0]
    expect(first).toMatchObject({ agent_type: 'newagent', execution_model: `newagent-${mode}`, repeat_times: mode === 'generate' ? 3 : 1 })
    expect(first).not.toHaveProperty('planner_config')
    cleanConfig(first.config)
    await view.handleStartTask(factoryState().tasks[0])
    expect(send.mock.calls[1]![0].request_id).toBe(first.request_id)
    view.newagentModels.execution_model = 'changed-newagent'
    await view.handleStartTask(factoryState().tasks[0])
    expect(send.mock.calls[2]![0].execution_model).toBe('changed-newagent')
    expect(send.mock.calls[2]![0].request_id).not.toBe(first.request_id)
    expect(first.execution_model).toBe(`newagent-${mode}`)
    view.agentType = 'pipelineagent'
    expect(view.activeModels.execution_model).toBe(`vla-${mode}`)
    view.agentType = 'newagent'
    expect(view.activeModels.execution_model).toBe('changed-newagent')
  })

  it('keeps custom NewAgent edits isolated from Pipeline and the next ordinary run', async () => {
    vi.mocked(phoneFactoryApi.config).mockResolvedValue({ ...settings(), agent_type: 'newagent', newagent_model_defaults: { execution_model: 'newagent-default' } })
    const view = await render('factory')
    view.openCustomRun(factoryState().phoneApps[0])
    expect(view.activeRunModels.execution_model).toBe('newagent-default')
    view.runDialogNewagentModels.execution_model = 'custom-newagent'
    view.runDialogAgentType = 'pipelineagent'
    expect(view.activeRunModels.execution_model).toBe('vla-saved')
    view.runDialogAgentType = 'newagent'
    expect(view.activeRunModels.execution_model).toBe('custom-newagent')
    view.runDialogTaskKey = `batch:${source().batch_id}`
    await view.confirmRun()
    const send = vi.mocked(phoneFactoryApi.remoteStartRun)
    expect(send.mock.calls[0]![0]).toMatchObject({ agent_type: 'newagent', execution_model: 'custom-newagent' })
    expect(send.mock.calls[0]![0]).not.toHaveProperty('planner_config')
    await view.handleStartTask(factoryState().tasks[0])
    expect(send.mock.calls[1]![0].execution_model).toBe('newagent-default')
    expect(view.models.execution_model).toBe('vla-saved')
  })

  it.each(['generate', 'modeliter'] as const)('saves %s NewAgent defaults without overwriting retained unsaved Pipeline drafts', async mode => {
    const factory = mode === 'generate' ? phoneFactoryApi : modelIterationApi
    const saved: FactorySettings = { ...settings(mode), agent_type: 'newagent', newagent_model_defaults: { execution_model: 'saved-newagent' } }
    vi.mocked(factory.config).mockResolvedValue(saved)
    const view = await render('factory', mode)
    view.models.execution_model = 'unsaved-pipeline'
    view.newagentModels.execution_model = 'new-default'
    const fetch = vi.fn(async (_url: string, init: RequestInit) => {
      const body = JSON.parse(String(init.body)) as FactorySettings
      return { ok: true, json: async () => ({ ...saved, ...body }) }
    })
    vi.stubGlobal('fetch', fetch)
    await view.handleConfigChange()
    expect(JSON.parse(String(fetch.mock.calls[0]![1].body))).not.toHaveProperty('newagent_model_defaults')
    expect(view.newagentModels.execution_model).toBe('new-default')
    await view.saveModelDefaults()
    const body = JSON.parse(String(fetch.mock.calls[1]![1].body))
    expect(fetch.mock.calls[1]![0]).toBe(mode === 'generate' ? '/api/phone-factory/config' : '/api/model-iter/config')
    expect(body.newagent_model_defaults).toEqual({ execution_model: 'new-default' })
    expect(body).not.toHaveProperty('model_defaults')
    expect(body).not.toHaveProperty('execution_model')
    expect(view.models.execution_model).toBe('unsaved-pipeline')
    expect(view.newagentModels.execution_model).toBe('new-default')
    cleanConfig(view.config)
    view.agentType = 'pipelineagent'
    view.newagentModels.execution_model = 'another-unsaved-newagent'
    await view.saveModelDefaults()
    expect(JSON.parse(String(fetch.mock.calls[2]![1].body))).not.toHaveProperty('newagent_model_defaults')
    expect(view.newagentModels.execution_model).toBe('another-unsaved-newagent')
  })

  it('freezes NewAgent automatic collection defaults while existing-data workflows remain free of model overrides', async () => {
    vi.mocked(phoneFactoryApi.config).mockResolvedValue({ ...settings(), agent_type: 'newagent', newagent_model_defaults: { execution_model: 'auto-newagent' } })
    const create = vi.spyOn(api, 'createPipeline').mockResolvedValue({ pipeline_id: 'created', batch_id: 'result' } as never)
    const view = await render('pipeline')
    await view.loadChoices()
    expect(view.draft).not.toHaveProperty('newagent_model_defaults')
    expect(view.activeModels.execution_model).toBe('auto-newagent')
    expect(view.draft.vla).toBe(factoryState().vla[0])
    view.newagentModels.execution_model = 'frozen-newagent'
    await view.loadChoices(false)
    expect(view.newagentModels.execution_model).toBe('frozen-newagent')
    Object.assign(view.draft, { name: 'newagent auto', mode: 'automatic', start_mode: 'collect', batch_id: source().batch_id })
    await nextTick(); await view.create()
    const first = create.mock.calls[0]![0]
    expect(first.collection_config).toMatchObject({ agent_type: 'newagent', execution_model: 'frozen-newagent' })
    expect(first.collection_config).not.toHaveProperty('planner_config')
    cleanConfig(first.collection_config!.config)
    view.newagentModels.execution_model = 'later'
    expect(first.collection_config!.execution_model).toBe('frozen-newagent')
    view.draft.agent_type = 'pipelineagent'
    expect(view.activeModels.execution_model).toBe('vla-saved')
    Object.assign(view.draft, { start_mode: 'existing', batch_id: 'ready-result' })
    await nextTick(); await view.create()
    expect(create.mock.calls[1]![0]).not.toHaveProperty('collection_config')
  })

  it('blocks unsupported NewAgent overrides before ordinary, custom and automatic dispatch but allows blank legacy defaults', async () => {
    vi.mocked(phoneFactoryApi.config).mockResolvedValue({ ...settings(), agent_type: 'newagent', newagent_model_defaults: { execution_model: 'explicit-id' } })
    vi.mocked(phoneFactoryApi.agentCapabilities).mockResolvedValue({ ok: true, agent_types: { ...capabilities, newagent: { ...capabilities.newagent!, model_overrides: false } } })
    const view = await render('factory')
    expect(view.agentIssue).toContain('NewAgent')
    await view.handleStartTask(factoryState().tasks[0])
    expect(phoneFactoryApi.remoteStartRun).not.toHaveBeenCalled()
    view.openCustomRun(factoryState().phoneApps[0])
    view.runDialogTaskKey = `batch:${source().batch_id}`
    await view.confirmRun()
    expect(phoneFactoryApi.remoteStartRun).not.toHaveBeenCalled()
    view.runDialogVisible = false
    view.newagentModels.execution_model = ''
    await view.handleStartTask(factoryState().tasks[0])
    expect(vi.mocked(phoneFactoryApi.remoteStartRun).mock.calls[0]![0]).not.toHaveProperty('execution_model')
    const create = vi.spyOn(api, 'createPipeline').mockResolvedValue({ pipeline_id: 'created', batch_id: 'result' } as never)
    const pipeline = await render('pipeline')
    await pipeline.loadChoices()
    Object.assign(pipeline.draft, { name: 'blocked', mode: 'automatic', start_mode: 'collect', batch_id: source().batch_id })
    await nextTick(); await pipeline.create()
    expect(pipeline.createError).toContain('NewAgent')
    expect(create).not.toHaveBeenCalled()
  })
})
