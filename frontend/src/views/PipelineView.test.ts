import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h, nextTick, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import { api } from '@/api'
import { factoryBatchesApi, phoneFactoryApi } from '@/phoneFactoryApi'
import PipelineView from './PipelineView.vue'
import { useDisplayIdentifiers } from '@/composables/useDisplayIdentifiers'

vi.mock('element-plus', () => ({ ElMessage: { success: vi.fn() }, ElMessageBox: {} }))
vi.mock('@/composables/usePipelineContext', () => ({
  usePipelineContext: () => ({ pipeline: ref(null), error: ref(''), refresh: vi.fn() }),
}))
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('Pipeline collection configuration', () => {
  it('searches a display alias while preserving the full batch ID in collection requests', async () => {
    const batchId = 'manual_' + 'a'.repeat(64)
    const alias = 'b261026-001'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ items: [{ kind: 'batch', id: batchId, scope: '', display_id: alias, mapped: true }] }) }))
    await useDisplayIdentifiers().resolve([{ kind: 'batch', id: batchId }])
    vi.spyOn(phoneFactoryApi, 'config').mockResolvedValue({ capture_logprobs: false } as never)
    vi.spyOn(phoneFactoryApi, 'state').mockResolvedValue({ phones: [], apps: [], phoneApps: [], tasks: [], vla: ['http://mock.invalid/vla'] })
    vi.spyOn(factoryBatchesApi, 'list').mockResolvedValue([{ batch_id: batchId, task_count: 2 }] as never)
    vi.spyOn(api, 'preprocessingBatches').mockResolvedValue([{ batch_id: batchId, label: '导入的验证用例', task_count: 2 }] as never)
    const create = vi.spyOn(api, 'createPipeline').mockResolvedValue({ pipeline_id: 'pipeline-alias-test' } as never)
    const app = createSSRApp(PipelineView)
    app.use(createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] }))
    let view: any
    app.mixin({ created() { if (this.$options.__name === 'PipelineView') view = this.$.setupState } })
    const empty = defineComponent({ setup: () => () => h('div') })
    for (const name of ['button', 'icon', 'alert', 'progress', 'tag', 'table', 'table-column', 'input', 'switch', 'input-number', 'select', 'option', 'form-item', 'radio-group', 'radio-button', 'dialog', 'form']) app.component('el-' + name, empty)
    await renderToString(app)
    await view.loadChoices()
    for (const keyword of [alias, batchId, '验证用例']) {
      view.batchSearch = keyword
      await nextTick()
      expect(view.visibleChoices.map((item: { batch_id: string }) => item.batch_id)).toEqual([batchId])
    }
    Object.assign(view.draft, { name: 'display alias', batch_id: batchId, start_mode: 'collect' })
    await view.create()
    expect(create.mock.calls[0]?.[0].batch_id).toBe(batchId)
    expect(create.mock.calls[0]?.[0].batch_id).not.toBe(alias)
  })

  it('hydrates the saved logprob default and preserves it in a collection request', async () => {
    const saved = { sampling_enabled: true, temperature: 0.35, top_p: 0.75, use_experience_lib: false, capture_logprobs: true }
    vi.spyOn(phoneFactoryApi, 'config').mockResolvedValue(saved)
    vi.spyOn(phoneFactoryApi, 'state').mockResolvedValue({ phones: [], apps: [], phoneApps: [], tasks: [], vla: ['http://mock.invalid/vla'] })
    vi.spyOn(factoryBatchesApi, 'list').mockResolvedValue([{ batch_id: 'batch-a', task_count: 2 }] as never)
    vi.spyOn(api, 'preprocessingBatches').mockResolvedValue([])
    const create = vi.spyOn(api, 'createPipeline').mockResolvedValue({ pipeline_id: 'pipeline-a' } as never)
    const app = createSSRApp(PipelineView)
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] })
    app.use(router)
    let view: any
    app.mixin({ created() { if (this.$options.__name === 'PipelineView') view = this.$.setupState } })
    const empty = defineComponent({ setup: () => () => h('div') })
    const pass = defineComponent({ setup: (_props, { slots }) => () => h('div', null, slots.default?.()) })
    for (const name of ['button', 'icon', 'alert', 'progress', 'tag', 'table', 'table-column', 'input', 'switch', 'input-number', 'select', 'option', 'form-item', 'radio-group', 'radio-button']) app.component('el-' + name, empty)
    app.component('el-dialog', pass); app.component('el-form', pass)
    await renderToString(app)
    expect(view.draft.capture_logprobs).toBe(false)
    await view.loadChoices()
    expect(view.draft.capture_logprobs).toBe(true)
    view.draft.start_mode = 'collect'
    await nextTick()
    Object.assign(view.draft, { name: 'capture', batch_id: 'batch-a' })
    await view.create()
    expect(create.mock.calls[0]?.[0].collection_config?.config).toEqual(saved)
    view.draft.capture_logprobs = false
    await view.create()
    expect(create.mock.calls[1]?.[0].collection_config?.config).toEqual({ ...saved, capture_logprobs: false })
    expect(saved.capture_logprobs).toBe(true)
    view.agentCapabilities = { pipelineagent: { available: true, collection: true, capture_logprobs: true, model_iteration_evaluation: true, offline_evaluation: true } }
    view.draft.agent_type = 'pipelineagent'
    await view.create()
    expect(create.mock.calls[2]?.[0].collection_config?.agent_type).toBe('pipelineagent')
    expect(create.mock.calls[2]?.[0].collection_config?.config).not.toHaveProperty('agent_type')
    view.agentCapabilities.pipelineagent.available = false
    view.agentCapabilities.pipelineagent.reason = 'PipelineAgent 尚未部署'
    await view.create()
    expect(create).toHaveBeenCalledTimes(3)
    expect(view.createError).toContain('尚未部署')
  })
})
