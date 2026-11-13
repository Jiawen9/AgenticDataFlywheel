import { describe, expect, it } from 'vitest'
import { createRenderer, defineComponent, h, nextTick, ref } from 'vue'
import * as Vue from 'vue'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import type { QualityNodeScore, TrajectoryTreeNode } from '@/types'
import ReferenceTrajectoryTree from './ReferenceTrajectoryTree.vue'
import componentSource from './ReferenceTrajectoryTree.vue?raw'

// Vitest loads Vue SFCs as SSR modules. Mount the same setup and actual client
// template on a small renderer so viewport events can be checked without a DOM.
const descriptor = parse(componentSource).descriptor
const bindings = compileScript(descriptor, { id: 'reference-tree-test' }).bindings
const render = new Function('Vue', compile(descriptor.template!.content, {
  mode: 'function', prefixIdentifiers: true, bindingMetadata: bindings,
}).code)(Vue)
const ClientTree = { ...ReferenceTrajectoryTree, render }

interface HostNode {
  type: string
  props: Record<string, unknown>
  children: HostNode[]
  parent: HostNode | null
  text: string
  clientHeight: number
  getBoundingClientRect: () => { left: number; top: number; width: number; height: number }
}
const host = (type: string): HostNode => ({ type, props: {}, children: [], parent: null, text: '', clientHeight: 610, getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 610 }) })
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
const node = (id: number, terminal: string[] = [], children: TrajectoryTreeNode[] = []): TrajectoryTreeNode => ({ id, depth: id, label: 'click', action: {}, summary: '', actions_box: '', image: '', xml: '', reference_trajectory: '', reference_step: 1, occurrence_count: 1, occurrences: [], terminal_trajectories: terminal, children })
function findAll(root: HostNode, predicate: (node: HostNode) => boolean): HostNode[] {
  return [...(predicate(root) ? [root] : []), ...root.children.flatMap(child => findAll(child, predicate))]
}
const hasClass = (node: HostNode, name: string) => String(node.props.class ?? '').split(' ').includes(name)
const flush = async () => { await nextTick(); await nextTick() }

describe('reference tree highlight and viewport', () => {
  it('preserves zoom across score/highlight/equivalent tree refreshes and resets only for changed topology', async () => {
    const tree = ref(node(0, [], [node(1, ['short'], [node(2, ['best', 'shared']), node(3, ['other'])])]))
    const selected = ref(2), highlight = ref('best'), scores = ref<Record<string, QualityNodeScore>>({})
    const container = host('container')
    const app = renderer.createApp(defineComponent({ setup: () => () => h(ClientTree, { root: tree.value, selectedId: selected.value, highlightTrajectoryId: highlight.value, nodeScores: scores.value }) }))
    app.provide(Symbol.for('v-scx'), {})
    app.mount(container)
    try {
      await flush()
      const scene = () => findAll(container, item => item.type === 'g' && 'transform' in item.props)[0]!.props.transform
      const edges = () => findAll(container, item => hasClass(item, 'reference-edge--highlighted'))
      const nodes = () => findAll(container, item => hasClass(item, 'reference-node--highlighted'))
      expect(edges()).toHaveLength(2); expect(nodes()).toHaveLength(3)
      expect(nodes().some(item => hasClass(item, 'selected'))).toBe(true)
      const zoom = findAll(container, item => item.type === 'button' && item.props.title === '放大')[0]!
      ;(zoom.props.onClick as () => void)()
      await flush()
      const zoomed = scene()
      expect(zoomed).toContain('scale(1.2)')
      scores.value = { '2': { q: .91, advantage: null, scored_count: 1, total_count: 1, status: 'complete' } }
      highlight.value = 'short'
      tree.value = JSON.parse(JSON.stringify(tree.value)) as TrajectoryTreeNode
      await flush()
      expect(scene()).toBe(zoomed)
      expect(edges()).toHaveLength(1); expect(nodes()).toHaveLength(2)
      highlight.value = 'shared'; await flush()
      expect(scene()).toBe(zoomed); expect(edges()).toHaveLength(2)
      tree.value = node(0, ['root-terminal'], [node(4, ['new'])])
      highlight.value = 'root-terminal'; await flush()
      expect(scene()).toContain('scale(1)')
      expect(edges()).toHaveLength(0); expect(nodes()).toHaveLength(1)
      highlight.value = 'missing'; await flush()
      expect(nodes()).toHaveLength(0)
    } finally { app.unmount() }
  })
})
