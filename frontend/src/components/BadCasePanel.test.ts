import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import BadCasePanel from './BadCasePanel.vue'
import { pipelineLoopApi } from '@/pipelineLoopApi'
import { api } from '@/api'
import type { BadCase } from '@/pipelineLoopApi'
import type { Pipeline } from '@/types/pipeline'
const sample = (id: string, missing: string[] = []): BadCase => ({ candidate_id: id, source_run_id: 'eval1', case_id: 'CASE1', task: '任务', app: 'App', verdicts: ['fail'], source_count: 3, missing_fields: missing, failures: [{ description: '检查项未通过' }] })
afterEach(() => vi.restoreAllMocks())
async function panel(props: { pipeline?: Partial<Pipeline>; onConfirmed?: (pipeline: Pipeline) => void } = {}) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] })
  await router.push('/'); await router.isReady()
  const app = createSSRApp(BadCasePanel, { runId: 'eval1', parentPipelineId: 'parent1', ...props }); app.use(router)
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
  it('confirms final BadCases on the current evaluation pipeline without creating a new one', async () => {
    const p = { pipeline_id: 'p-eval', status: 'waiting_for_evaluation' as const, storage_revision: 4 }
    const updated = { ...p, status: 'waiting_for_generation_config', feedback_id: 'fb1' } as Pipeline
    const confirm = vi.spyOn(pipelineLoopApi, 'confirmBadcases').mockResolvedValue(updated)
    const create = vi.spyOn(api, 'createPipeline')
    const onConfirmed = vi.fn()
    const { state } = await panel({ pipeline: p, onConfirmed })
    state.data = { index_state: 'ready', final: false, summary: { candidate: 1 }, items: [sample('a')] }
    state.selected = new Set(['a']); await state.feedback()
    expect(confirm).not.toHaveBeenCalled()
    state.data.final = true; await state.feedback()
    expect(confirm).toHaveBeenCalledWith('p-eval', { run_id: 'eval1', candidate_ids: ['a'], expected_revision: 4, request_id: expect.any(String) })
    expect(onConfirmed).toHaveBeenCalledWith(updated)
    expect(create).not.toHaveBeenCalled()
  })
  it('only finishes without expansion when the final summary has no business candidates', async () => {
    const p = { pipeline_id: 'p-empty', status: 'waiting_for_evaluation' as const, storage_revision: 4 }
    const confirm = vi.spyOn(pipelineLoopApi, 'confirmBadcases').mockResolvedValue({ ...p, status: 'no_publishable_data' } as Pipeline)
    const { state } = await panel({ pipeline: p })
    state.data = { index_state: 'ready', final: true, summary: { candidate: 0, needs_completion: 1, technical: 2 }, items: [] }
    await state.confirmScoped([])
    expect(confirm).not.toHaveBeenCalled()
    state.data.summary.needs_completion = 0; await state.confirmScoped([])
    expect(confirm).toHaveBeenCalledWith('p-empty', expect.objectContaining({ run_id: 'eval1', candidate_ids: [] }))
  })
})
