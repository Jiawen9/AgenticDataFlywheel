import { describe, expect, it, vi } from 'vitest'
import { createRenderer, createSSRApp, defineComponent, h, nextTick, ref, shallowReactive, ssrContextKey } from 'vue'
import { renderToString } from '@vue/server-renderer'
import FactoryRunDetailsDrawer from './FactoryRunDetailsDrawer.vue'
import type { FactoryRun } from '@/phoneFactoryApi'
import { invalidateSummaryCache } from '@/utils/summaryCache'
import type { EvaluationProgress } from '@/composables/useEvaluationProgress'

const runs = (): FactoryRun[] => Array.from({ length: 55 }, (_, i) => ({ run_id: `run-${String(i).padStart(2, '0')}`, status: i < 5 ? 'running' : 'completed', created_at: `2026-10-29T00:${String(i).padStart(2, '0')}:00` }))
const makeProgress = () => {
  const selectedRun = ref('run-54')
  return { selectedRun, results: ref({ summary: { total: 55 }, cases: Array.from({ length: 55 }, (_, i) => ({ case_key: `key-${i}`, case_id: `case-${i}`, phone_id: 'phone', collection_status: 'completed', evaluation_status: i < 5 ? 'running' : 'completed', errors: [] })) }), busy: ref(false), resultError: ref(''), cancelling: ref(false), downloading: ref(''), canCancel: ref(false), resultMetadata: ref({}), selectRun: (id: string) => { selectedRun.value = id }, cancelRun: vi.fn(), download: vi.fn() } as unknown as EvaluationProgress
}
function mount(extra: Record<string, any> = {}) {
  const progress = makeProgress()
  const props = shallowReactive({ modelValue: false, tab: 'runs' as const, evaluation: true, runs: runs(), progress, folders: [{ index: 1, dir_name: 'legacy', files: Array.from({ length: 55 }, (_, i) => ({ name: `report-${i}.xlsx`, size: 1 })) }], loadingReports: false, downloadingReport: '', runFilter: 'all' as const, focusedRun: '', ...extra })
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const component = { ...FactoryRunDetailsDrawer, render: () => h('div') }
  const tab = vi.fn()
  const app = renderer.createApp({ render: () => h(component, { ...props, 'onUpdate:tab': tab }) })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({})
  return { app, props, progress, tab, state: app._instance!.subTree.component!.setupState as Record<string, any> }
}

describe('shared factory details drawer', () => {
  it('pages each list independently at 10, 20 or 50 and resets only changed searches or filters', async () => {
    const view = mount()
    expect(view.state.pagedRuns).toHaveLength(10)
    expect(view.state.pagedCases).toHaveLength(10)
    expect(view.state.pagedReports).toHaveLength(10)
    view.state.runControls.page = 3; view.state.runControls.size = 20; await nextTick()
    expect(view.state.runControls.page).toBe(1); expect(view.state.pagedRuns).toHaveLength(20)
    view.state.caseControls.size = 50; await nextTick(); expect(view.state.pagedCases).toHaveLength(50)
    view.state.reportControls.page = 4; view.state.runControls.status = 'active'; await nextTick()
    expect(view.state.pagedRuns).toHaveLength(5); expect(view.state.reportControls.page).toBe(4)
    view.state.reportControls.query = 'report-54'; await nextTick()
    expect(view.state.reportControls.page).toBe(1); expect(view.state.pagedReports).toHaveLength(1)
    view.app.unmount()
  })
  it('retains drawer state across close/reopen, clamps shorter snapshots, and synchronizes the selected evaluation run', async () => {
    const view = mount()
    view.state.runControls.page = 4
    view.props.modelValue = true; await nextTick(); view.props.modelValue = false; await nextTick()
    expect(view.state.runControls.page).toBe(4)
    view.props.runs = view.props.runs.slice(0, 12); await nextTick()
    expect(view.state.runControls.page).toBe(2)
    view.state.caseControls.page = 3; view.progress.selectRun('run-03'); await nextTick()
    expect(view.state.selectedRun).toBe('run-03'); expect(view.state.caseControls.page).toBe(1)
    view.state.showCases('run-02'); await nextTick()
    expect(view.progress.selectedRun.value).toBe('run-02'); expect(view.tab).toHaveBeenCalledWith('cases')
    view.state.showStorage('run-03'); expect(view.state.storageFocusedRun).toBe('run-03'); expect(view.tab).toHaveBeenCalledWith('storage')
    view.progress.busy.value = true; view.state.showCases('run-01')
    expect(view.progress.selectedRun.value).toBe('run-02')
    view.app.unmount()
  })
  it('renders only the applicable tabs and retains the legacy report download identity', async () => {
    const render = async (evaluation: boolean) => {
      const app = createSSRApp(FactoryRunDetailsDrawer, { modelValue: true, tab: 'runs', evaluation, runs: [], progress: makeProgress(), folders: [], loadingReports: false, downloadingReport: '' })
      const passthrough = defineComponent({ setup: (_p, { slots }) => () => h('div', slots.default?.()) })
      for (const name of ['el-drawer', 'el-button', 'el-select', 'el-option']) app.component(name, passthrough)
      for (const name of ['el-input', 'el-alert', 'el-table', 'el-table-column', 'el-pagination']) app.component(name, defineComponent({ render: () => h('div') }))
      return renderToString(app)
    }
    const production = await render(false), evaluation = await render(true)
    expect(production.match(/role="tab"/g)).toHaveLength(1)
    expect(evaluation.match(/role="tab"/g)).toHaveLength(3)
    const view = mount()
    expect(view.state.pagedReports[0].folder.dir_name).toBe('legacy')
    expect(view.state.pagedReports[0].folder.run_id).toBeUndefined()
    view.app.unmount()
  })
  it.each([
    { agent_type: 'newagent' as const, execution_model: 'served-newagent', execution_mode: 'collect_and_evaluate' as const, execution: true, planner: false },
    { agent_type: 'pipelineagent' as const, execution_model: 'served-pipeline', execution_mode: 'collect_and_evaluate' as const, execution: true, planner: true },
    { agent_type: 'newagent' as const, execution_model: 'historical-value', execution_mode: 'evaluate_only' as const, execution: false, planner: false },
  ])('shows the submitted $agent_type model only for collection ($execution_mode)', async scenario => {
    const row: FactoryRun = { ...scenario, run_id: 'model-view-run', batch_id: 'batch', status: 'completed', collector_data_state: 'retired', planner_config: { base_url: 'http://planner.invalid/v1', model: 'planner-id' } }
    const app = createSSRApp(FactoryRunDetailsDrawer, { modelValue: true, tab: 'runs', evaluation: false, runs: [row], folders: [], loadingReports: false, downloadingReport: '' })
    const passthrough = defineComponent({ setup: (_p, { slots }) => () => h('div', slots.default?.()) })
    for (const name of ['el-drawer', 'el-button', 'el-select', 'el-option', 'el-table']) app.component(name, passthrough)
    app.component('el-table-column', defineComponent({ props: ['type'], setup: (props, { slots }) => () => h('div', props.type === 'expand' ? slots.default?.({ row }) : []) }))
    for (const name of ['el-input', 'el-alert', 'el-progress', 'el-pagination']) app.component(name, defineComponent({ render: () => h('div') }))
    const html = await renderToString(app)
    expect(html.includes('执行模型：')).toBe(scenario.execution)
    expect(html.includes(scenario.execution_model)).toBe(scenario.execution)
    expect(html.includes('规划模型：')).toBe(scenario.planner)
  })
})


it('loads server pages only for the open tab and keeps selection and paging on refresh', async () => {
  invalidateSummaryCache()
  const envelope = (items: any[], total = 500) => ({items, latest: [], total, filtered_total: total, scope_counts: {total, active: 1, abnormal: 4}, offset: 0, limit: 10, has_more: true, counts_complete: true, index_state: 'ready'})
  const api = { runPage: vi.fn().mockResolvedValue(envelope(runs().slice(0, 10))), evaluationCases: vi.fn().mockResolvedValue(envelope([{case_key: 'one', evaluation_status: 'completed'}])), reportPage: vi.fn().mockResolvedValue(envelope([{key: 'legacy/report', folder: {index: 3, dir_name: 'legacy', files: []}, file: {name: 'report.json', size: 1}}], 30)), runDetail: vi.fn().mockResolvedValue({run_id: 'run-01', status: 'completed', execution_model: 'frozen'}) }
  const view = mount({diagnosticsApi: api})
  await nextTick(); expect(api.runPage).not.toHaveBeenCalled(); expect(api.reportPage).not.toHaveBeenCalled()
  view.props.modelValue = true; await nextTick(); await view.state.runList.refresh()
  expect(api.runPage).toHaveBeenCalledTimes(1); expect(api.reportPage).not.toHaveBeenCalled(); expect(api.evaluationCases).not.toHaveBeenCalled()
  view.state.runControls.page = 3; await nextTick(); await view.state.runList.refresh()
  expect(api.runPage).toHaveBeenLastCalledWith(expect.objectContaining({offset: 20, limit: 10}))
  expect(view.progress.selectedRun.value).toBe('run-54')
  view.state.expandRun({run_id: 'run-01'}, [{run_id: 'run-01'}]); await nextTick(); await Promise.resolve()
  expect(api.runDetail).toHaveBeenCalledWith('run-01')
  ;(view.props as any).tab = 'reports'; await nextTick(); await view.state.reportList.refresh()
  expect(api.reportPage).toHaveBeenCalledTimes(1); expect(view.state.pagedReports[0].folder.dir_name).toBe('legacy')
  ;(view.props as any).tab = 'cases'; await nextTick(); await view.state.caseList.refresh()
  expect(api.evaluationCases).toHaveBeenCalledWith('run-54', expect.objectContaining({offset: 0, limit: 10}))
  expect(view.state.runControls.page).toBe(3)
  view.props.modelValue = false; await nextTick(); await view.state.caseList.refresh(); expect(api.evaluationCases).toHaveBeenCalledTimes(1)
  view.app.unmount(); invalidateSummaryCache()
})
