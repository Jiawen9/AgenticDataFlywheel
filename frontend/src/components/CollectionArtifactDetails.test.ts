import { describe, expect, it } from 'vitest'
import { createRenderer, h, nextTick, shallowReactive, ssrContextKey } from 'vue'
import CollectionArtifactDetails from './CollectionArtifactDetails.vue'
import type { CollectionArtifact, FactoryRun } from '@/phoneFactoryApi'

function mount() {
  const items: CollectionArtifact[] = Array.from({ length: 55 }, (_, i) => ({ artifact_id: `artifact-${i}`, kind: 'trajectory',
    collection_case_id: `case-${Math.floor(i / 3)}`, phone_id: 'phone-one', repeat_index: i % 3 + 1, repeat_times: 3,
    source_trajectory_id: `trajectory-${i}`, execution_status: i < 5 ? 'failed' : 'completed', transfer_status: 'completed' }))
  const props = shallowReactive({ run: { run_id: 'run-one', status: 'running', collection_artifacts: items } as FactoryRun })
  const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
  const component = { ...CollectionArtifactDetails, render: () => h('div') }
  const app = renderer.createApp({ render: () => h(component, props) })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({})
  return { app, props, state: app._instance!.subTree.component!.setupState as Record<string, any> }
}

describe('per-attempt collection detail', () => {
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
})
