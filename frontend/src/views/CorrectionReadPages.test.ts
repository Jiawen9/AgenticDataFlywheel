import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, h, nextTick, ref, ssrContextKey } from 'vue'
import { invalidateSummaryCache } from '@/utils/summaryCache'

const calls = vi.hoisted(() => ({ sessions: vi.fn(), groups: vi.fn(), session: vi.fn(), cot: vi.fn(), cotJobs: vi.fn(), correctionBatches: vi.fn(), createCorrectionCotJob: vi.fn(), correctionSession: vi.fn(), correctionSessionCot: vi.fn(), createCorrectionSession: vi.fn() }))
const route = { query: {} as Record<string, string> }
vi.mock('@/readPageApi', () => ({ readPageApi: calls }))
vi.mock('@/api', () => ({ api: calls, correctionDownloadUrl: () => '' }))
vi.mock('vue-router', () => ({ useRoute: () => route, useRouter: () => ({ replace: vi.fn() }), onBeforeRouteUpdate: vi.fn(), onBeforeRouteLeave: vi.fn() }))
vi.mock('@/composables/useDisplayIdentifiers', () => ({ useDisplayIdentifiers: () => ({ displayId: (_: string, id: string) => id }) }))
vi.mock('@/composables/useBatchLifecycle', () => ({ useBatchLifecycle: () => ({ notice: ref(null), checkBatch: async () => true }) }))
vi.mock('@/composables/usePipelineContext', () => ({ usePipelineContext: () => ({
  historyOnly: ref(false), isPipelineRoute: ref(false), managed: ref(false), readOnly: ref(false), error: ref(''), sessionId: ref(''), pipeline: ref(null), refresh: async () => {},
}) }))
vi.mock('element-plus', async original => ({ ...await original<object>(), ElMessage: { warning: vi.fn(), error: vi.fn(), success: vi.fn() }, ElMessageBox: { confirm: vi.fn(async () => {}), close: vi.fn() } }))
import CotGenerationView from './CotGenerationView.vue'
import CorrectionView from './CorrectionView.vue'

const apps: Array<{ unmount(): void }> = []
function mount(component: object) {
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const app = renderer.createApp({ render: () => h({ ...component, render: () => h('div') }) })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({}); apps.push(app)
  return app._instance!.subTree.component!.setupState as Record<string, any>
}
const flush = async () => { for (let n = 0; n < 20; n++) { await Promise.resolve(); await nextTick() } }
const page = (items: unknown[], total = items.length) => ({ items, latest: [], total, filtered_total: total, scope_counts: { total, active: 0, abnormal: 0 }, offset: 0, limit: 10, has_more: false, counts_complete: true, index_state: 'ready' })
const header = { session_id: 'session-one', batch_id: 'batch-one', storage_revision: 7, group_count: 20, cot_row_count: 80, cot_generated_count: 4, eligible_cot_row_count: 80, groups: [], selection: { tasks: [] } }
const group = { group_id: 'group-one', meta_task: 'tr-one', task_id: 't-one', cot_row_count: 4, pending_review: false }
beforeEach(() => {
  vi.useFakeTimers(); vi.clearAllMocks(); invalidateSummaryCache(); route.query = {}
  vi.stubGlobal('window', { addEventListener: vi.fn(), removeEventListener: vi.fn(), setTimeout, clearTimeout })
  vi.stubGlobal('document', { hidden: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })
  calls.sessions.mockResolvedValue(page([header])); calls.correctionBatches.mockResolvedValue(page([{ batch_id: 'batch-one' }]))
  calls.session.mockResolvedValue(header); calls.groups.mockResolvedValue(page([group], 20)); calls.cotJobs.mockResolvedValue(page([]))
  calls.cot.mockResolvedValue({ session_id: 'session-one', storage_revision: 7, groups: [{ group_id: 'group-one', trajectory_id: 'tr-one', rows: [{ excel_row: 2, action: '{"action":"wait"}', summary: 'old', thought: '' }] }] })
  calls.createCorrectionCotJob.mockResolvedValue({ session_id: 'session-one', job_id: 'job-one', status: 'queued' })
})
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('lazy correction and COT pages', () => {
  it('opening either page fetches only lists, not session bodies or COT rows', async () => {
    mount(CorrectionView); mount(CotGenerationView); await flush()
    expect(calls.session).not.toHaveBeenCalled()
    expect(calls.groups).not.toHaveBeenCalled()
    expect(calls.cot).not.toHaveBeenCalled()
    expect(calls.createCorrectionSession).not.toHaveBeenCalled()
  })
  it('a COT deep link loads metadata once and rows only after expanding one group', async () => {
    route.query = { session_id: 'session-one' }
    const state = mount(CotGenerationView); await flush()
    expect(calls.groups).toHaveBeenCalledTimes(1)
    expect(calls.cot).not.toHaveBeenCalled()
    await state.loadCotGroup('group-one'); await flush()
    expect(calls.cot).toHaveBeenCalledExactlyOnceWith('session-one', 'group-one')
    expect(state.activeRow.excel_row).toBe(2)
    expect(calls.correctionSessionCot).not.toHaveBeenCalled()
  })
  it('bulk generation covers the frozen session, not only the visible page', async () => {
    route.query = { session_id: 'session-one' }
    const state = mount(CotGenerationView); await flush()
    await state.generateAll()
    expect(calls.createCorrectionCotJob).toHaveBeenCalledWith('session-one', undefined, undefined, { generateBBox: true, forceOverwrite: true, expectedRevision: 7 })
  })
  it('a late previous-group result cannot overwrite the selected group', async () => {
    route.query = { session_id: 'session-one' }
    const state = mount(CotGenerationView); await flush()
    let done!: (value: unknown) => void
    calls.cot.mockReturnValueOnce(new Promise(resolve => { done = resolve }))
    const first = state.loadCotGroup('old')
    calls.cot.mockResolvedValueOnce({ session_id: 'session-one', groups: [{ group_id: 'new', rows: [{ excel_row: 9 }] }] })
    await state.loadCotGroup('new')
    done({ session_id: 'session-one', groups: [{ group_id: 'old', rows: [{ excel_row: 3 }] }] }); await first; await flush()
    expect(state.selectedGroupId).toBe('new'); expect(state.activeRow.excel_row).toBe(9)
  })
  it('refresh does not overwrite a manual text draft', async () => {
    route.query = { session_id: 'session-one' }
    const state = mount(CotGenerationView); await flush(); await state.loadCotGroup('group-one')
    state.editingField = 'summary'; state.textDraft = 'unsaved manual correction'
    const count = calls.groups.mock.calls.length
    await state.refreshCot()
    expect(calls.groups).toHaveBeenCalledTimes(count)
    expect(state.textDraft).toBe('unsaved manual correction')
  })
})
