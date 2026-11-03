import { afterEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, createSSRApp, defineComponent, h, nextTick, shallowReactive, ssrContextKey } from 'vue'
import { renderToString } from '@vue/server-renderer'
import CollectionCaseGroups from './CollectionCaseGroups.vue'

const mounted: Array<{ unmount(): void }> = []
afterEach(() => { mounted.splice(0).forEach(app => app.unmount()); vi.useRealTimers() })
const flush = async () => { for (let i = 0; i < 5; i++) { await Promise.resolve(); await nextTick() } }
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
const row = (id: string) => ({ case_id: id, phone_id: 'phone-1', app: 'App', planned_count: 100, repeat_times: 100, completed_count: 20, failed_count: 1, active_count: 1, not_run_count: 78, received_count: 20, attempt_count: 22 })
function mount() {
  const api = { casePage: vi.fn(async () => ({ supported: true, items: Array.from({ length: 10 }, (_, i) => row(`case-${i}`)), total: 5000, offset: 0, limit: 10 })), caseInstances: vi.fn(async () => ({ items: Array.from({ length: 100 }, (_, i) => ({ repeat_index: i + 1, repeat_times: 100, status: 'completed', attempts: [{ execution_id: 'old', error: 'old failure' }], attempt_count: 2 })), total: 100 })) }
  const props = shallowReactive({ runId: 'run-a', api: api as any, active: false, refreshKey: 'one' })
  const unsupported = vi.fn()
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const component = { ...CollectionCaseGroups, render: () => h('div') }
  const app = renderer.createApp({ render: () => h(component, { ...props, onUnsupported: unsupported }) })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); mounted.push(app)
  return { app, api, props, unsupported, state: app._instance!.subTree.component!.setupState as Record<string, any> }
}

describe('server-paged case and phone groups', () => {
  it('shows the active retry while retaining the previous sealed result and its original download identity', async () => {
    const artifacts: any[] = []
    const app = createSSRApp(CollectionCaseGroups, { runId: 'root-run', api: {} as any, active: false })
    app.mixin({ created() {
      if (this.$options.__name === 'CollectionInstanceArtifacts') artifacts.push({ ...this.$props })
      if (this.$options.__name !== 'CollectionCaseGroups') return
      const state = this.$.setupState as any, first = row('case-0'), key = state.groupKey(first)
      state.result = { supported: true, items: [first], total: 1, offset: 0, limit: 10 }
      state.expanded = [key]
      state.instances = { [key]: { items: [{ repeat_index: 1, repeat_times: 100,
        status: 'running', execution_status: 'failed', execution_id: 'old-execution', artifact_id: 'old-artifact',
        error: 'old sealed failure', receipt_status: 'received', attempt_count: 2, attempts: [],
        active_attempt: { execution_id: 'new-execution', execution_status: 'running' },
        current_result: { execution_id: 'old-execution', artifact_id: 'old-artifact', execution_status: 'failed' },
      }], total: 1 } }
    } })
    const empty = defineComponent({ render: () => h('div') })
    for (const name of ['el-input', 'el-select', 'el-option', 'el-alert', 'el-pagination', 'el-button']) app.component(name, empty)
    app.directive('loading', {})
    const html = await renderToString(app)
    expect(html).toContain('续采中，保留上次结果。当前执行编号：new-execution')
    expect(html).toContain('上次结果执行编号：old-execution')
    expect(html).toMatch(/<details(?![^>]*open)[^>]*><summary[^>]*>查看上次结果原因<\/summary>/)
    expect(html).not.toContain('当前执行：old sealed failure')
    expect(artifacts).toHaveLength(1)
    expect(artifacts[0]).toMatchObject({ runId: 'root-run', executionId: 'old-execution', artifactId: 'old-artifact' })
  })
  it('renders only the server page and keeps repeat details and old failure contents closed by default', async () => {
    const api = { casePage: vi.fn(), caseInstances: vi.fn() }
    const app = createSSRApp(CollectionCaseGroups, { runId: 'run-a', api: api as any, active: false })
    app.mixin({ created() {
      if (this.$options.__name !== 'CollectionCaseGroups') return
      const state = this.$.setupState as any, first = row('case-0'), key = state.groupKey(first)
      state.result = { supported: true, items: [first], total: 5000, offset: 0, limit: 10 }
      state.expanded = [key]
      state.instances = { [key]: { items: [{ repeat_index: 1, repeat_times: 100, execution_status: 'completed', attempt_count: 2, attempts: [{ execution_id: 'old', execution_status: 'failed', error: 'historical failure' }] }], total: 1 } }
    } })
    const empty = defineComponent({ render: () => h('div') })
    for (const name of ['el-input', 'el-select', 'el-option', 'el-alert', 'el-pagination', 'el-button']) app.component(name, empty)
    app.directive('loading', {})
    const html = await renderToString(app)
    expect(html).toContain('共 5000 个用例 / 手机组')
    expect(html.match(/class="case-group"/g)).toHaveLength(1)
    expect(html).toMatch(/<details class="repeat-instance"[^>]*>/)
    expect(html).not.toMatch(/<details[^>]*open[^>]*class="repeat-instance"|<details[^>]*class="repeat-instance"[^>]*open/)
    expect(html).toMatch(/<details class="attempt-history"[^>]*>/)
    expect(html).not.toMatch(/<details[^>]*open[^>]*class="attempt-history"|<details[^>]*class="attempt-history"[^>]*open/)
    expect(api.caseInstances).not.toHaveBeenCalled()
  })
  it('loads only the requested page and expansion, preserving page and expanded groups across snapshots and drawer closes', async () => {
    const view = mount(); expect(view.api.casePage).not.toHaveBeenCalled()
    view.props.active = true; await flush()
    expect(view.state.rows).toHaveLength(10); expect(view.api.caseInstances).not.toHaveBeenCalled()
    view.state.controls.page = 3; await flush()
    expect(view.api.casePage.mock.calls.at(-1)?.[1]).toMatchObject({ offset: 20, limit: 10 })
    view.state.toggleGroup(view.state.rows[0], true); await flush()
    expect(view.api.caseInstances.mock.calls[0]?.slice(0, 3)).toEqual(['run-a', 'case-0', 'phone-1'])
    expect(view.state.instanceRows(view.state.rows[0])).toHaveLength(20)
    await view.state.refresh(); await flush()
    expect(view.state.controls.page).toBe(3); expect(view.state.expanded).toHaveLength(1)
    view.props.active = false; await flush(); view.props.active = true; await flush()
    expect(view.state.controls.page).toBe(3); expect(view.state.expanded).toHaveLength(1)
  })
  it('resets only filters and rejects a late page from a prior filter or run', async () => {
    const view = mount(); view.props.active = true; await flush()
    const late = deferred<any>(); view.api.casePage.mockReturnValueOnce(late.promise)
    view.state.controls.page = 4; await flush()
    const signal = view.api.casePage.mock.calls.at(-1)?.[2]
    view.state.controls.app = 'New App'; view.state.controls.phone = 'phone-2'; view.state.controls.query = 'needle'; await flush()
    expect(signal.aborted).toBe(true); expect(view.state.controls.page).toBe(1)
    expect(view.api.casePage.mock.calls.at(-1)?.[1]).toMatchObject({ app: 'New App', phone_id: 'phone-2', q: 'needle', offset: 0 })
    late.resolve({ supported: true, items: [row('stale')], total: 1 }); await flush()
    expect(view.state.rows[0].case_id).toBe('case-0')
    view.state.toggleGroup(view.state.rows[0], true); await flush()
    view.props.runId = 'run-b'; await flush(); expect(view.state.expanded).toEqual([])
  })
  it('signals unsupported versions without clearing records or silently fabricating an empty successful result', async () => {
    const view = mount(); view.api.casePage.mockResolvedValueOnce({ supported: false, items: [], total: 0, offset: 0, limit: 10 })
    view.props.active = true; await flush()
    expect(view.unsupported).toHaveBeenLastCalledWith(true); expect(view.state.result.supported).toBe(false)
  })
  it('retains the current page while the backend is preparing its grouped projection', async () => {
    const view = mount(); view.props.active = true; await flush()
    view.state.controls.page = 3; await flush()
    view.api.casePage.mockResolvedValueOnce({ supported: true, preparing: true, items: [], total: 0, offset: 20, limit: 10 } as any)
    await view.state.refresh(); await flush()
    expect(view.state.preparing).toBe(true); expect(view.state.rows).toHaveLength(10); expect(view.state.controls.page).toBe(3)
  })
  it('refreshes open details every five seconds without depending on diagnostic responses, and stops requests on close', async () => {
    vi.useFakeTimers()
    const view = mount(); view.props.active = true; await flush()
    expect(view.api.casePage).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(5000); expect(view.api.casePage).toHaveBeenCalledTimes(2)
    view.props.active = false; await flush(); await vi.advanceTimersByTimeAsync(10000)
    expect(view.api.casePage).toHaveBeenCalledTimes(2)
  })
})
