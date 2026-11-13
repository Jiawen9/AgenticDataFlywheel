import { describe, expect, it, vi } from 'vitest'
import { createRenderer, defineComponent, h, nextTick, ref } from 'vue'
import * as Vue from 'vue'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import { ScriptTarget, transpileModule } from 'typescript'
import type { TaskQualityResult, TrajectoryTreeNode, TreeOccurrence } from '@/types'
import QualityView from './QualityView.vue'
import componentSource from './QualityView.vue?raw'

const fixture = vi.hoisted(() => ({ flow: {} as Record<string, unknown> }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }), useRouter: () => ({ replace: vi.fn(), push: vi.fn() }) }))
vi.mock('@/api', () => ({ api: { qualityJobs: vi.fn(async () => []) }, imageUrl: (path: string) => path, treeRunScope: () => ({}) }))
vi.mock('@/composables/useBatchQualityWorkspace', () => ({ useBatchQualityWorkspace: () => fixture.flow }))
vi.mock('@/composables/usePipelineContext', () => ({ usePipelineContext: () => ({ readOnly: ref(false), isPipelineRoute: ref(false), managed: ref(false), historyOnly: ref(false), pipeline: ref(null), refresh: vi.fn() }) }))
vi.mock('@/composables/useBatchLifecycle', () => ({ useBatchLifecycle: () => ({ notice: ref(null), checkBatch: vi.fn(async () => true) }) }))
vi.mock('@/composables/useDisplayIdentifiers', () => ({ useDisplayIdentifiers: () => ({ displayId: (_kind: string, id: string) => id, matches: () => true }) }))
vi.mock('@/composables/useCoordinateInfo', () => ({ treeCoordinateSource: () => undefined }))
vi.mock('@/components/PipelineStatusBar.vue', () => ({ default: { render: () => null } }))
vi.mock('@/components/BatchPublishedNotice.vue', () => ({ default: { render: () => null } }))
vi.mock('@/components/QualityFusionSettings.vue', () => ({ default: { render: () => null } }))
vi.mock('@/components/DisplayId.vue', () => ({ default: defineComponent({ props: ['id'], setup: props => () => h('span', String(props.id ?? '')) }) }))
vi.mock('@/components/ReferenceTrajectoryTree.vue', () => ({ default: defineComponent({
  props: ['root', 'selectedId', 'highlightTrajectoryId'], emits: ['select'],
  setup: (props, { emit }) => () => h('test-tree', { selected: props.selectedId, highlighted: props.highlightTrajectoryId,
    onPick: (node: TrajectoryTreeNode) => emit('select', node) }),
}) }))
vi.mock('@/components/ActionImage.vue', () => ({ default: defineComponent({ props: ['imageUrl'], setup: props => () => h('test-image', { path: props.imageUrl }) }) }))
vi.mock('@/components/QualityTrajectoryScore.vue', () => ({ default: defineComponent({ props: ['evaluation'], setup: props => () => h('test-score', { trajectory: props.evaluation?.trajectory_id }) }) }))

// Exercise the page's actual setup, watchers and client template. Only visual
// child components and network-backed workspace are replaced with small ports.
const descriptor = parse(componentSource).descriptor
const bindings = compileScript(descriptor, { id: 'quality-view-highlight-test' }).bindings
const code = compile(descriptor.template!.content, { mode: 'function', prefixIdentifiers: true,
  bindingMetadata: bindings, expressionPlugins: ['typescript'] }).code
const render = new Function('Vue', transpileModule(code, { compilerOptions: { target: ScriptTarget.ES2022 } }).outputText)(Vue)
const ClientView = { ...QualityView, render }

interface HostNode {
  type: string; props: Record<string, unknown>; children: HostNode[]; parent: HostNode | null; text: string
  checked: boolean; events: Record<string, unknown>
  addEventListener: (event: string, handler: unknown) => void
  removeEventListener: (event: string) => void
}
function host(type: string): HostNode {
  const node: HostNode = { type, props: {}, children: [], parent: null, text: '', checked: false, events: {},
    addEventListener: (event, handler) => { node.events[event] = handler },
    removeEventListener: event => { delete node.events[event] } }
  return node
}
const renderer = createRenderer<HostNode, HostNode>({
  patchProp: (node, key, _old, value) => { node.props[key] = value },
  insert: (node, parent, anchor = null) => {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
    node.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) parent.children.push(node); else parent.children.splice(index, 0, node)
  },
  remove: node => { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1) },
  createElement: tag => host(tag), createText: text => ({ ...host('#text'), text }), createComment: text => ({ ...host('#comment'), text }),
  setText: (node, text) => { node.text = text }, setElementText: (node, text) => { node.text = text; node.children = [] },
  parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
})
function all(root: HostNode, check: (node: HostNode) => boolean): HostNode[] { return [...(check(root) ? [root] : []), ...root.children.flatMap(child => all(child, check))] }
const flush = async () => { for (let i = 0; i < 4; ++i) { await Promise.resolve(); await nextTick() } }
const node = (id: number, children: TrajectoryTreeNode[] = []): TrajectoryTreeNode => ({ id, depth: id, label: 'click', action: {}, summary: '', actions_box: '', image: '', xml: '', reference_trajectory: '', reference_step: 1, occurrence_count: 2, occurrences: [], terminal_trajectories: [], children })
function mount(tied = false) {
  const terminal = node(1)
  terminal.terminal_trajectories = ['low', 'best']
  terminal.occurrences = ['low', 'best'].map(trajectory => ({ trajectory, step: 1, image: `${trajectory}.jpg`, summary: trajectory, action: {}, xml: '', actions_box: '' })) as TreeOccurrence[]
  const tree = ref(node(0, [terminal]))
  const quality = ref({ task_id: 'task', evaluations: Object.fromEntries(['low', 'best'].map(id => [id, { trajectory_id: id, global_score: 4,
    fusion: { valid: true, score: id === 'best' || tied ? .9 : .4 } }])), fusion_order: ['best', 'low'], fusion_complete: true } as TaskQualityResult)
  const task = { task_id: 'task', goal: '检查目标页面', trajectory_count: 2, original_step_count: 2, tree_step_count: 2, ignored_step_count: 0, action_node_count: 1 }
  fixture.flow = { batches: ref([{ batch_id: 'batch', task_count: 1 }]), batchId: ref('batch'), currentTree: ref({ run_id: 'batch', task_count: 1, tasks: [task] }),
    summaries: ref({ task: { status: 'succeeded', rubric_ready: true, fusion_complete: true } }), recommendation: ref(null),
    selectedTaskId: ref('task'), tree, quality, loadingBatches: ref(false), loadingTree: ref(false), counts: ref({ completed: 1, pending: 0, stale: 0 }),
    readyTasks: ref([task]), error: ref(''), loadBatches: vi.fn(), refreshChoices: vi.fn(), dispose: vi.fn() }
  const app = renderer.createApp(ClientView)
  const pass = defineComponent({ setup: (_props, { slots, attrs }) => () => h('div', attrs, slots.default?.()) })
  for (const name of ['button', 'alert', 'empty', 'tag', 'checkbox', 'progress', 'option']) app.component('el-' + name, pass)
  app.component('el-select', defineComponent({ props: ['modelValue'], emits: ['update:modelValue', 'change'],
    setup: (props, { emit, slots, attrs }) => () => h('test-select', { ...attrs, value: props.modelValue, onPick: (value: unknown) => {
      emit('update:modelValue', value); emit('change', value)
    } }, slots.default?.()) }))
  for (const name of ['el-drawer', 'el-table', 'el-table-column']) app.component(name, { render: () => null })
  app.component('router-link', pass)
  app.directive('loading', {}); app.provide(Symbol.for('v-scx'), {})
  const container = host('container'); app.mount(container)
  const one = (type: string) => all(container, item => item.type === type)[0]!
  const pickNode = () => (one('test-tree').props.onPick as (node: TrajectoryTreeNode) => void)(tree.value.children[0]!)
  return { app, container, tree, quality, one, pickNode }
}

describe('QualityView highlight wiring', () => {
  it('prefers the highlighted shared-node evidence, then preserves manual selection during scoring refresh', async () => {
    const page = mount()
    try {
      await flush(); expect(page.one('test-tree').props.highlighted).toBe('best')
      page.pickNode(); await flush()
      expect(page.one('test-image').props.path).toBe('best.jpg')
      expect(page.one('test-score').props.trajectory).toBe('best')
      const occurrence = all(page.container, item => item.type === 'test-select' && item.props.value === 1)[0]!
      ;(occurrence.props.onPick as (value: unknown) => void)(0)
      const terminal = all(page.container, item => item.type === 'test-select' && item.props.value === 'best')[0]!
      ;(terminal.props.onPick as (value: unknown) => void)('low')
      await flush()
      const refreshed = JSON.parse(JSON.stringify(page.tree.value)) as TrajectoryTreeNode
      refreshed.children[0]!.occurrences.reverse()
      page.tree.value = refreshed
      page.quality.value = JSON.parse(JSON.stringify(page.quality.value)) as TaskQualityResult
      await flush()
      expect(page.one('test-tree').props.selected).toBe(1)
      expect(page.one('test-tree').props.highlighted).toBe('best')
      expect(page.one('test-image').props.path).toBe('low.jpg')
      expect(page.one('test-score').props.trajectory).toBe('low')
      expect(all(page.container, item => item.type === 'test-select' && item.props.value === 1)).toHaveLength(1)
    } finally { page.app.unmount() }
  })
  it('switches a tied highlight explicitly and toggles emphasis without clearing the selected evidence', async () => {
    const page = mount(true)
    try {
      await flush(); page.pickNode(); await flush()
      const choice = all(page.container, item => item.type === 'test-select' && item.props.class === 'highlight-choice')[0]!
      ;(choice.props.onPick as (value: unknown) => void)('low')
      await flush()
      expect(page.one('test-tree').props.highlighted).toBe('low')
      expect(page.one('test-tree').props.selected).toBe(1)
      expect(page.one('test-image').props.path).toBe('low.jpg')
      expect(page.one('test-score').props.trajectory).toBe('low')
      const toggle = all(page.container, item => item.type === 'input' && item.props.type === 'checkbox')[0]!
      ;(toggle.props['onUpdate:modelValue'] as (value: boolean) => void)(false)
      await flush()
      expect(page.one('test-tree').props.highlighted).toBe('')
      expect(page.one('test-image').props.path).toBe('low.jpg')
      ;(toggle.props['onUpdate:modelValue'] as (value: boolean) => void)(true)
      await flush()
      expect(page.one('test-tree').props.highlighted).toBe('low')
      expect(page.one('test-score').props.trajectory).toBe('low')
    } finally { page.app.unmount() }
  })
})
