import { afterEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, createSSRApp, defineComponent, h, nextTick, shallowReactive, ssrContextKey } from 'vue'
import * as Vue from 'vue'
import { readFileSync } from 'node:fs'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import { renderToString } from '@vue/server-renderer'
import type { TrajectoryQualityEvaluation } from '@/types'
import * as scoreHelpers from '@/utils/qualityScores'
import QualityTrajectoryScore from './QualityTrajectoryScore.vue'

// The Node test transform emits SSR-only components. Compile this same SFC's
// client template for real event/render tests without adding a DOM dependency.
const source = readFileSync(new URL('./QualityTrajectoryScore.vue', import.meta.url), 'utf8')
const script = compileScript(parse(source).descriptor, { id: 'quality-trajectory-score-test', inlineTemplate: true })
const compiled = ts.transpileModule(script.content, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } })
const componentModule: { default?: any } = {}
new Function('require', 'exports', compiled.outputText)((name: string) => {
  if (name === 'vue') return Vue
  if (name === '@/utils/qualityScores') return scoreHelpers
  throw new Error(`Unexpected import ${name}`)
}, componentModule)
const ClientScore = componentModule.default

type HostNode = { type: string; text: string; props: Record<string, any>; children: HostNode[]; parent: HostNode | null }
const element = (type: string, text = ''): HostNode => ({ type, text, props: {}, children: [], parent: null })
const detach = (node: HostNode) => {
  if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
  node.parent = null
}
const renderer = createRenderer<HostNode, HostNode>({
  createElement: type => element(type), createText: text => element('#text', text), createComment: () => element('#comment'),
  setText(node, text) { node.text = text },
  setElementText(node, text) { node.children.forEach(child => { child.parent = null }); node.children = []; node.text = text },
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
  patchProp(node, key, _before, after) { node.props[key] = after },
  insert(node, parent, anchor) { detach(node); node.parent = parent; const index = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(index < 0 ? parent.children.length : index, 0, node) },
  remove: detach,
})
const pass = defineComponent({ setup: (_props, { slots }) => () => h('section', slots.default?.()) })
const findAll = (root: HostNode, predicate: (node: HostNode) => boolean): HostNode[] => [root, ...root.children.flatMap(child => findAll(child, () => true))].filter(predicate)
const textOf = (root: HostNode): string => root.text + root.children.map(textOf).join('')
const evaluation = (id = 'trajectory-1'): TrajectoryQualityEvaluation => ({
  trajectory_id: id, global_score: 4.5, passed_threshold: true,
  dimension_global_scores: [{ dimension_name: '效率', score: 4.2, rationale: 'Rubric 理由' }], step_evaluations: [],
  fusion: { version: 1, lambda: .5, valid: true, score: .82, invalid_reasons: [] },
  checklist: { status: 'complete', total: 6, passed: 4, failed: 1, unchecked: 1, verdict: 'partial_success', items: Array.from({ length: 6 }, (_, index) => ({
    id: `c${index + 1}`, description: `检查项描述 ${index + 1}`, checked: index !== 5,
    status: index === 4 ? 'failed' : 'passed', reason: `详细理由 ${index + 1}`, evidence: { step: index + 1, text: `完整证据 ${index + 1}` },
  })) },
})
const mounted: ReturnType<typeof mount>[] = []
function mount(value = evaluation(), trajectoryKey?: string) {
  const state = shallowReactive<{ evaluation: TrajectoryQualityEvaluation | null; trajectoryKey?: string }>({ evaluation: value, trajectoryKey })
  const app = renderer.createApp({ setup: () => () => h(ClientScore, state) })
  app.component('el-collapse', pass); app.component('el-collapse-item', pass)
  app.provide(ssrContextKey, { modules: new Set() })
  const root = element('root'); app.mount(root)
  const buttons = () => findAll(root, node => node.type === 'button')
  const click = async (index: number) => { buttons()[index]!.props.onClick(); await nextTick() }
  const view = { state, root, app, buttons, click, text: () => textOf(root) }
  mounted.push(view); return view
}
afterEach(() => { mounted.splice(0).forEach(view => view.app.unmount()); vi.unstubAllGlobals() })

describe('Checklist always-visible summaries and evidence', () => {
  it('renders all six descriptions and Chinese results with evidence initially collapsed', () => {
    const view = mount()
    expect(view.buttons()).toHaveLength(6)
    for (let index = 1; index <= 6; index++) expect(view.text()).toContain(`检查项描述 ${index}`)
    expect(view.text()).toContain('部分成功')
    expect(view.text()).toContain('状态：已完成')
    expect(view.text()).toContain('通过'); expect(view.text()).toContain('未通过'); expect(view.text()).toContain('未检查')
    expect(view.text()).toContain('Rubric 理由')
    expect(view.text()).not.toContain('完整证据'); expect(view.text()).not.toContain('详细理由')
    expect(view.buttons().every(button => button.props['aria-expanded'] === false)).toBe(true)
  })

  it('expands one item at a time, links its controls, and keeps all summaries visible', async () => {
    const view = mount(); await view.click(0)
    expect(view.text()).toContain('详细理由 1'); expect(view.text()).toContain('完整证据 1')
    const targetId = view.buttons()[0]!.props['aria-controls']
    expect(findAll(view.root, node => node.props.id === targetId)).toHaveLength(1)
    await view.click(1)
    expect(view.text()).not.toContain('完整证据 1'); expect(view.text()).toContain('完整证据 2')
    expect(view.buttons()[0]!.props['aria-expanded']).toBe(false)
    expect(view.buttons()[1]!.props['aria-expanded']).toBe(true)
    expect(view.buttons()).toHaveLength(6)
    await view.click(1)
    expect(view.text()).not.toContain('完整证据 2')
  })

  it('retains the open item after polling and reordering, and closes when input identity changes', async () => {
    const view = mount(evaluation(), 'batch/task/tree/trajectory-1'); await view.click(0)
    const refreshed = evaluation(); refreshed.checklist!.items.reverse()
    view.state.evaluation = refreshed; await nextTick()
    expect(view.text()).toContain('完整证据 1')
    expect(view.buttons()[5]!.props['aria-expanded']).toBe(true)
    view.state.trajectoryKey = 'another-batch/task/tree/trajectory-1'; await nextTick()
    expect(view.text()).not.toContain('完整证据 1')
    await view.click(0)
    view.state.trajectoryKey = 'another-batch/task/new-tree/trajectory-1'; await nextTick()
    expect(view.buttons().every(button => button.props['aria-expanded'] === false)).toBe(true)
  })

  it('falls back to trajectory identity for older callers and drops an item removed by refresh', async () => {
    const view = mount(); await view.click(0)
    view.state.evaluation = evaluation('trajectory-2'); await nextTick()
    expect(view.text()).not.toContain('完整证据 1')
    await view.click(0)
    const refreshed = evaluation('trajectory-2'); refreshed.checklist!.items.shift()
    view.state.evaluation = refreshed; await nextTick()
    expect(view.buttons().every(button => button.props['aria-expanded'] === false)).toBe(true)
  })

  it('preserves unknown results, legacy result/text fields, and explicit unchecked precedence', async () => {
    const source = evaluation()
    source.checklist!.verdict = 'needs_review'
    source.checklist!.status = 'legacy_pending'
    source.checklist!.items = [
      { id: 'legacy', text: '旧格式检查项', result: 'passed' },
      { id: 'unchecked', description: '尚未检查', checked: false, status: 'passed', result: 'passed' },
      { id: 'future', description: '未来状态', result: 'needs_manual_review' },
      { id: 'unknown', description: '缺结果' },
    ]
    const view = mount(source)
    expect(view.text()).toContain('legacy_pending'); expect(view.text()).toContain('needs_review')
    expect(textOf(view.buttons()[0]!)).toContain('旧格式检查项'); expect(textOf(view.buttons()[0]!)).toContain('通过')
    expect(textOf(view.buttons()[1]!)).toContain('未检查')
    expect(textOf(view.buttons()[2]!)).toContain('needs_manual_review')
    expect(textOf(view.buttons()[3]!)).toContain('未知')
    await view.click(0); expect(view.text()).toContain('未提供证据')
  })

  it('keeps missing and failed Checklist results readable without inventing evidence or scores', async () => {
    const source = evaluation()
    source.fusion = { version: 1, lambda: .5, valid: false, score: null, invalid_reasons: ['checklist_error'] }
    source.checklist = { status: 'error', total: null, passed: null, failed: null, unchecked: null, items: [], error: '模型请求失败' }
    const view = mount(source)
    expect(view.text()).toContain('暂无有效评分'); expect(view.text()).toContain('模型请求失败')
    expect(view.text()).toContain('暂无检查项明细'); expect(view.text()).toContain('4.50 / 5')
    const historical = evaluation(); delete historical.checklist; delete historical.fusion
    view.state.evaluation = historical; await nextTick()
    expect(view.text()).toContain('Checklist 未评测')
    view.state.evaluation = null; await nextTick()
    expect(view.text()).toContain('本条轨迹待评分')
  })

  it('uses only supplied evidence and does not request or mutate data during interaction', async () => {
    const request = vi.fn(); vi.stubGlobal('fetch', request)
    const source = evaluation(), before = JSON.stringify(source)
    source.checklist!.items[0]!.evidence = { step: 1, text: '<img src=x onerror=alert(1)>' }
    const expected = JSON.stringify(source)
    const view = mount(source); await view.click(0)
    expect(view.text()).toContain('<img src=x onerror=alert(1)>')
    expect(findAll(view.root, node => node.type === 'img')).toHaveLength(0)
    await view.click(1); await view.click(1)
    expect(request).not.toHaveBeenCalled(); expect(JSON.stringify(source)).toBe(expected)
    expect(before).not.toBe(expected)
  })

  it('escapes descriptions in the real server-rendered template', async () => {
    const source = evaluation(); source.checklist!.items[0]!.description = '<script>bad()</script>'
    const app = createSSRApp(QualityTrajectoryScore, { evaluation: source })
    app.component('el-collapse', pass); app.component('el-collapse-item', pass)
    const html = await renderToString(app)
    expect(html).toContain('&lt;script&gt;bad()&lt;/script&gt;')
    expect(html).not.toContain('<script>bad()')
    expect(html.match(/class="checklist-description"/g)).toHaveLength(6)
  })
})
