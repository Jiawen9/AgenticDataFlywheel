import { describe, expect, it, vi } from 'vitest'
import { createRenderer, h, nextTick, shallowReactive, ssrContextKey } from 'vue'
import FactoryRunStorage from './FactoryRunStorage.vue'
import type { FactoryRun } from '@/phoneFactoryApi'
import type { RunStorageState } from '@/composables/useRunStorage'
const dialogs = vi.hoisted(() => ({ confirm: vi.fn(async () => true) }))
vi.mock('element-plus', () => ({ ElMessageBox: { confirm: dialogs.confirm } }))
function mount() {
  const runs: FactoryRun[] = Array.from({ length: 35 }, (_, i) => ({ run_id: `run-${i}`, batch_id: i < 20 ? 'batch-a' : 'batch-b', status: i % 2 ? 'failed' : 'running', transfer_status: 'failed' }))
  const start = vi.fn(async () => {}), state = { tasks: [], start, retry: vi.fn(), latest: vi.fn(), pending: () => false, archiveDownloadUrl: () => '/archive' } as unknown as RunStorageState
  const props = shallowReactive({ runs, state, focusedRun: '' })
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const component = { ...FactoryRunStorage, render: () => h('div') }
  const app = renderer.createApp({ render: () => h(component, props) }); app.provide(ssrContextKey, { modules: new Set() }); app.mount({})
  return { app, props, start, view: app._instance!.subTree.component!.setupState as Record<string, any> }
}
describe('run storage drawer', () => {
  it('selects the entire result batch across pagination with no quality or receipt gate', async () => {
    const value = mount(); value.view.filters.batch = 'batch-a'; await nextTick()
    expect(value.view.pageRuns).toHaveLength(10)
    value.view.selectBatch(); expect(value.view.selectedRuns).toHaveLength(20)
    value.view.filters.page = 2; value.props.runs = [...value.props.runs]; await nextTick()
    expect(value.view.filters.page).toBe(2); expect(value.view.selectedRuns).toHaveLength(20)
    await value.view.clean('retire')
    expect(dialogs.confirm).toHaveBeenCalledTimes(1)
    expect(dialogs.confirm.mock.calls[0]?.[0]).toContain('系统会先停止')
    expect(value.start.mock.calls[0]?.[0]).toHaveLength(20)
    expect(value.start.mock.calls[0]?.slice(1)).toEqual(['cleanup', 'retire'])
    value.app.unmount()
  })
  it('keeps a user-chosen page during polling and uses 10/20/50 page sizes', async () => {
    const value = mount(); value.view.filters.page = 3
    value.props.runs = [...value.props.runs]; await nextTick(); expect(value.view.filters.page).toBe(3)
    value.view.filters.size = 20; await nextTick(); expect(value.view.filters.page).toBe(1); expect(value.view.pageRuns).toHaveLength(20)
    value.view.filters.size = 50; await nextTick(); expect(value.view.pageRuns).toHaveLength(35)
    value.app.unmount()
  })
})
