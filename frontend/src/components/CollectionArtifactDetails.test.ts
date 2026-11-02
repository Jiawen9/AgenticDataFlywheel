import { afterEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, h, nextTick, shallowReactive, ssrContextKey } from 'vue'
import CollectionArtifactDetails from './CollectionArtifactDetails.vue'
import { modelIterationApi, phoneFactoryApi, type CollectionArtifact, type FactoryRun } from '@/phoneFactoryApi'

function mount(overrides: Partial<FactoryRun> = {}) {
  const items: CollectionArtifact[] = Array.from({ length: 55 }, (_, i) => ({ artifact_id: `artifact-${i}`, kind: 'trajectory',
    collection_case_id: `case-${Math.floor(i / 3)}`, phone_id: 'phone-one', repeat_index: i % 3 + 1, repeat_times: 3,
    source_trajectory_id: `trajectory-${i}`, execution_status: i < 5 ? 'failed' : 'completed', transfer_status: 'completed' }))
  const props = shallowReactive({ run: { run_id: 'run-one', status: 'running', collection_artifacts: items, ...overrides } as FactoryRun })
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const component = { ...CollectionArtifactDetails, render: () => h('div') }
  const app = renderer.createApp({ render: () => h(component, props) })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({})
  return { app, props, state: app._instance!.subTree.component!.setupState as Record<string, any> }
}

describe('per-attempt collection detail', () => {
  afterEach(() => vi.restoreAllMocks())
  it('pages independent repeats and preserves the page during progress refreshes', async () => {
    const view = mount()
    expect(view.state.pageRows).toHaveLength(10)
    expect(view.state.pageRows.slice(0, 3).map((row: CollectionArtifact) => row.repeat_index)).toEqual([1, 2, 3])
    view.state.controls.page = 3
    view.props.run = { ...view.props.run, collection_artifacts: [...view.props.run.collection_artifacts!] }
    await nextTick(); expect(view.state.controls.page).toBe(3)
    view.state.controls.size = 20; await nextTick()
    expect(view.state.controls.page).toBe(1); expect(view.state.pageRows).toHaveLength(20)
    view.state.controls.size = 50; await nextTick(); expect(view.state.pageRows).toHaveLength(50)
    view.app.unmount()
  })
  it('filters anomalies and searches original cases, phones and trajectory identities without changing the run', async () => {
    const view = mount()
    view.state.controls.status = 'abnormal'; await nextTick(); expect(view.state.rows).toHaveLength(5)
    view.props.run = { ...view.props.run, collection_artifacts: [...view.props.run.collection_artifacts!, { artifact_id: 'retry', kind: 'trajectory', execution_status: 'completed', transfer_status: 'pending', transfer_error: 'download disconnected' }] }
    await nextTick(); expect(view.state.rows).toHaveLength(6)
    view.state.controls.query = 'download disconnected'; await nextTick(); expect(view.state.rows[0].artifact_id).toBe('retry')
    view.state.controls.status = 'all'; view.state.controls.query = 'case-0'; await nextTick()
    expect(view.state.rows).toHaveLength(3)
    view.state.controls.query = 'trajectory-54'; await nextTick()
    expect(view.state.rows).toHaveLength(1); expect(view.state.rows[0].repeat_index).toBe(1)
    expect(view.props.run.run_id).toBe('run-one')
    view.app.unmount()
  })

  async function settle() { await Promise.resolve(); await nextTick(); await Promise.resolve(); await nextTick() }
  function result(id: string, total = 55) { return { items: [{ artifact_id: id, kind: 'trajectory' as const, receipt_status: 'received', transfer_status: 'completed', eligibility: { version: 1, evaluation: true, preprocessing: false, issues: [] } }], total, next_offset: 1, has_more: total > 1 } }

  it('uses server pagination and preserves the page while polling refreshes the run', async () => {
    const get = vi.spyOn(phoneFactoryApi, 'artifactPage').mockImplementation(async (_id, options) => result(`offset-${options?.offset}`))
    const view = mount({ receipt_policy_version: 2, collection_artifact_count: 55, collection_artifacts: [] })
    await settle()
    expect(view.state.total).toBe(55)
    view.state.controls.page = 3; await settle()
    expect(get.mock.lastCall?.[1]).toMatchObject({ offset: 20, limit: 10 })
    view.props.run = { ...view.props.run, collection_summary: undefined }; await settle()
    expect(view.state.controls.page).toBe(3)
    expect(get.mock.lastCall?.[1]).toMatchObject({ offset: 20, limit: 10 })
    view.state.controls.size = 20; await settle()
    expect(view.state.controls.page).toBe(1)
    expect(get.mock.lastCall?.[1]).toMatchObject({ offset: 0, limit: 20 })
    view.state.controls.query = 'phone-two'; view.state.controls.status = 'quarantined'; await settle()
    expect(get.mock.lastCall?.[1]).toMatchObject({ search: 'phone-two', status: 'quarantined' })
    view.app.unmount()
  })

  it('aborts and discards a previous run response without resetting a new selection', async () => {
    const requests: Array<{ resolve: (value: ReturnType<typeof result>) => void; signal?: AbortSignal }> = []
    vi.spyOn(phoneFactoryApi, 'artifactPage').mockImplementation((_id, _options, signal) => new Promise(resolve => { requests.push({ resolve, signal }) }))
    const view = mount({ receipt_policy_version: 2, collection_artifacts: [] })
    await settle()
    view.props.run = { ...view.props.run, run_id: 'run-two' }; await settle()
    expect(requests[0]!.signal?.aborted).toBe(true)
    requests[1]!.resolve(result('new-result', 1)); await settle()
    requests[0]!.resolve(result('late-old-result', 50)); await settle()
    expect(view.state.pageRows[0].artifact_id).toBe('new-result')
    expect(view.state.total).toBe(1)
    view.app.unmount()
    expect(requests[1]!.signal?.aborted).toBe(true)
  })

  it('does not start another detail request on every parent refresh while one is pending', async () => {
    let resolve!: (value: ReturnType<typeof result>) => void
    const get = vi.spyOn(phoneFactoryApi, 'artifactPage').mockImplementation(() => new Promise(done => { resolve = done }))
    const view = mount({ receipt_policy_version: 2, collection_artifacts: [] }); await settle()
    view.props.run = { ...view.props.run }; await settle()
    expect(get).toHaveBeenCalledTimes(1)
    resolve(result('saved')); await settle()
    view.props.run = { ...view.props.run }; await settle()
    expect(get).toHaveBeenCalledTimes(2)
    view.app.unmount()
  })

  it('pages declared original files and never requests files for an outcome', async () => {
    vi.spyOn(modelIterationApi, 'artifactPage').mockResolvedValue(result('case-file'))
    const detail = vi.spyOn(modelIterationApi, 'artifactDetail').mockResolvedValue({ artifact_id: 'case-file', status: 'received',
      manifest: {}, eligibility: undefined, files: Array.from({ length: 55 }, (_, index) => ({ path: `case/trajectory/step${index}.jpg`, size: 100, sha256: 'a'.repeat(64) })), error: undefined })
    const url = vi.spyOn(modelIterationApi, 'artifactFileUrl').mockReturnValue('/api/model-iter/file')
    const view = mount({ receipt_policy_version: 2, execution_mode: 'collect_and_evaluate', collection_artifacts: [] }); await settle()
    await view.state.openFiles({ artifact_id: 'nothing', kind: 'outcome', receipt_status: 'received' }); await settle()
    expect(detail).not.toHaveBeenCalled()
    await view.state.openFiles(result('case-file').items[0]); await settle()
    expect(view.state.pageFiles).toHaveLength(10)
    view.state.fileControls.page = 2; await settle()
    expect(view.state.pageFiles[0].path).toContain('step10')
    expect(view.state.fileUrl(view.state.pageFiles[0].path)).toBe('/api/model-iter/file')
    expect(url).toHaveBeenCalledWith('run-one', 'case-file', 'case/trajectory/step10.jpg')
    view.state.fileControls.query = 'step54'; await settle()
    expect(view.state.pageFiles).toHaveLength(1)
    view.props.run = { ...view.props.run, run_id: 'other' }; await settle()
    expect(view.state.selectedArtifact).toBe('')
    expect(view.state.pageFiles).toHaveLength(0)
    view.app.unmount()
  })
})
