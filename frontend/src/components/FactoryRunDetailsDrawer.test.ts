import { describe, expect, it, vi } from 'vitest'
import { createRenderer, createSSRApp, defineComponent, h, nextTick, ref, shallowReactive, ssrContextKey } from 'vue'
import { renderToString } from '@vue/server-renderer'
import FactoryRunDetailsDrawer from './FactoryRunDetailsDrawer.vue'
import type { FactoryRun } from '@/phoneFactoryApi'
import type { EvaluationProgress } from '@/composables/useEvaluationProgress'

const runs = (): FactoryRun[] => Array.from({ length: 55 }, (_, i) => ({ run_id: `run-${String(i).padStart(2, '0')}`, status: i < 5 ? 'running' : 'completed', created_at: `2026-10-29T00:${String(i).padStart(2, '0')}:00` }))
const makeProgress = () => {
  const selectedRun = ref('run-54')
  return { selectedRun, results: ref({ summary: { total: 55 }, cases: Array.from({ length: 55 }, (_, i) => ({ case_key: `key-${i}`, case_id: `case-${i}`, phone_id: 'phone', collection_status: 'completed', evaluation_status: i < 5 ? 'running' : 'completed', errors: [] })) }), busy: ref(false), resultError: ref(''), cancelling: ref(false), downloading: ref(''), canCancel: ref(false), resultMetadata: ref({}), selectRun: (id: string) => { selectedRun.value = id }, cancelRun: vi.fn(), download: vi.fn() } as unknown as EvaluationProgress
}
function mount() {
  const progress = makeProgress()
  const props = shallowReactive({ modelValue: false, tab: 'runs' as const, evaluation: true, runs: runs(), progress, folders: [{ index: 1, dir_name: 'legacy', files: Array.from({ length: 55 }, (_, i) => ({ name: `report-${i}.xlsx`, size: 1 })) }], loadingReports: false, downloadingReport: '', runFilter: 'all' as const, focusedRun: '' })
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
})
