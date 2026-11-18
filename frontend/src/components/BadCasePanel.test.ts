import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import BadCasePanel from './BadCasePanel.vue'
import { pipelineLoopApi } from '@/pipelineLoopApi'
import { api } from '@/api'
import type { BadCase } from '@/pipelineLoopApi'
const sample = (id: string, missing: string[] = []): BadCase => ({ candidate_id: id, source_run_id: 'eval1', case_id: 'CASE1', task: '任务', app: 'App', verdicts: ['fail'], source_count: 3, missing_fields: missing, failures: [{ description: '检查项未通过' }] })
afterEach(() => vi.restoreAllMocks())
async function panel() {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] })
  await router.push('/'); await router.isReady()
  const app = createSSRApp(BadCasePanel, { runId: 'eval1', parentPipelineId: 'parent1' }); app.use(router)
  let state: any
  app.mixin({ created() { if (this.$options.__name === 'BadCasePanel') state = this.$.setupState } })
  const passthrough = defineComponent({ setup: (_props, { slots }) => () => h('div', slots.default?.()) })
  for (const name of ['button','drawer','input','select','option','table','pagination','alert','checkbox']) app.component('el-' + name, passthrough)
  app.component('el-table-column', defineComponent({ setup: () => () => h('span') }))
  app.directive('loading', { getSSRProps: () => ({}) })
  await renderToString(app)
  return { state, router }
}
describe('BadCase selection and feedback', () => {
  it('retains cross-page choices and never selects technical or incomplete items by page action', async () => {
    const { state } = await panel()
    state.data = { items: [sample('a'), sample('missing', ['app'])], total: 2, summary: {} }
    state.choosePage(); expect([...state.selected]).toEqual(['a'])
    state.data = { items: [sample('b')], total: 3, summary: {} }; state.choosePage()
    expect([...state.selected]).toEqual(['a', 'b'])
    state.status = 'technical'; state.data.items = [sample('tech')]; state.choosePage()
    expect([...state.selected]).toEqual(['a', 'b'])
  })
  it('freezes selection before creating a new linked augmentation pipeline', async () => {
    const freeze = vi.spyOn(pipelineLoopApi, 'feedback').mockResolvedValue({ feedback_id: 'fb1', count: 1 })
    const create = vi.spyOn(api, 'createPipeline').mockResolvedValue({ pipeline_id: 'next1', batch_id: null, generation_kind: 'augmentation', feedback_id: 'fb1' } as never)
    const { state, router } = await panel(); state.selected = new Set(['a']); await state.feedback()
    expect(freeze).toHaveBeenCalledWith('eval1', ['a'], expect.any(String))
    expect(create.mock.calls[0]![0]).toMatchObject({ start_mode: 'generate', generation_kind: 'augmentation', parent_pipeline_id: 'parent1', feedback_id: 'fb1' })
    expect(create.mock.calls[0]![0]).not.toHaveProperty('batch_id')
    expect(router.currentRoute.value.path).toBe('/task-generation/augmentation')
  })
  it('keeps confirmed selection on failure and reuses request identities on retry', async () => {
    const freeze = vi.spyOn(pipelineLoopApi, 'feedback').mockRejectedValueOnce(new Error('临时失败')).mockResolvedValue({ feedback_id: 'fb1', count: 1 })
    vi.spyOn(api, 'createPipeline').mockResolvedValue({ pipeline_id: 'next1', generation_kind: 'augmentation' } as never)
    const { state } = await panel(); state.selected = new Set(['a']); await state.feedback()
    expect(state.error).toBe('临时失败'); expect([...state.selected]).toEqual(['a'])
    await state.feedback(); expect(freeze.mock.calls[0]![2]).toBe(freeze.mock.calls[1]![2])
  })
})
