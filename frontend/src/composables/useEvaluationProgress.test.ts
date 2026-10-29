import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, defineComponent, h, nextTick, ref, ssrContextKey } from 'vue'
import { useEvaluationProgress, type EvaluationProgress } from './useEvaluationProgress'
import type { FactoryRun } from '@/phoneFactoryApi'
import FactoryRunDetailsDrawer from '@/components/FactoryRunDetailsDrawer.vue'

const mocks = vi.hoisted(() => ({ evaluations: vi.fn(), cancel: vi.fn(), confirm: vi.fn(), start: vi.fn(), registrations: 0, poll: undefined as undefined | ((signal: AbortSignal, current: () => boolean) => Promise<void>) }))
vi.mock('@/phoneFactoryApi', () => ({ modelIterationApi: { evaluations: mocks.evaluations, cancelEvaluation: mocks.cancel } }))
vi.mock('element-plus', () => ({ ElMessageBox: { confirm: mocks.confirm } }))
vi.mock('./useFactoryPolling', () => ({ useFactoryPolling: (work: typeof mocks.poll) => { mocks.registrations++; mocks.poll = work; return { start: mocks.start } } }))
const flush = async () => { await nextTick(); await Promise.resolve(); await nextTick() }
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }

function mount() {
  const runs = ref<FactoryRun[]>([{ run_id: 'old', status: 'running', created_at: '2026-10-01' }, { run_id: 'new', status: 'running', created_at: '2026-10-02' }])
  const opened = ref(false), tab = ref<'runs' | 'cases' | 'reports'>('runs'), changed = vi.fn()
  let progress!: EvaluationProgress
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const drawer = { ...FactoryRunDetailsDrawer, render: () => h('div') }
  const app = renderer.createApp(defineComponent({ setup() {
    progress = useEvaluationProgress(runs, changed)
    return () => opened.value ? h(drawer, { modelValue: true, tab: tab.value, runs: runs.value, progress, folders: [], evaluation: true, loadingReports: false, downloadingReport: '' }) : h('div')
  } }))
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({})
  return { app, runs, opened, tab, changed, progress }
}

beforeEach(() => { vi.clearAllMocks(); mocks.registrations = 0 })

describe('page-owned evaluation progress', () => {
  it('owns exactly one result poller through drawer mounts, closes and tab switches', async () => {
    const view = mount()
    expect(view.progress.selectedRun.value).toBe('new')
    for (const tab of ['runs', 'cases', 'reports'] as const) {
      view.opened.value = true; view.tab.value = tab; await flush()
      view.opened.value = false; await flush()
    }
    expect(mocks.registrations).toBe(1)
    const result = { run_id: 'new', summary: { total: 9 }, cases: [] }
    mocks.evaluations.mockResolvedValueOnce(result)
    await mocks.poll!(new AbortController().signal, () => true)
    expect(view.progress.results.value).toEqual(result)
    expect(view.opened.value).toBe(false)
    view.app.unmount()
  })
  it('retains a freshly-created selection while list snapshots catch up and rejects A→B→A late responses', async () => {
    const view = mount()
    view.progress.selectRun('just-created'); view.runs.value = [...view.runs.value]; await flush()
    expect(view.progress.selectedRun.value).toBe('just-created')
    view.progress.selectRun('old')
    const pending = deferred<any>(); mocks.evaluations.mockReturnValueOnce(pending.promise)
    const request = mocks.poll!(new AbortController().signal, () => true)
    view.progress.selectRun('new'); view.progress.selectRun('old')
    pending.resolve({ run_id: 'old', summary: { total: 100 }, cases: [] }); await request
    expect(view.progress.results.value).toBeNull()
    expect(mocks.start).toHaveBeenCalled()
    view.app.unmount()
  })
  it('does not apply results after the page is unmounted', async () => {
    const view = mount(), pending = deferred<any>()
    mocks.evaluations.mockReturnValueOnce(pending.promise)
    const request = mocks.poll!(new AbortController().signal, () => true)
    view.app.unmount(); pending.resolve({ run_id: 'new', summary: { total: 1 }, cases: [] }); await request
    expect(view.progress.results.value).toBeNull()
  })
})
