import { describe, expect, it } from 'vitest'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import type { TrajectoryQualityEvaluation, TrajectoryTreeNode } from '@/types'
import ReferenceTrajectoryTree from './ReferenceTrajectoryTree.vue'
import QualityTrajectoryScore from './QualityTrajectoryScore.vue'

const node = (id: number, terminal: string[] = []): TrajectoryTreeNode => ({ id, depth: id, label: id ? 'click' : '桌面', action: {}, summary: '', actions_box: '', image: '', xml: '', reference_trajectory: 't1', reference_step: id, occurrence_count: 2, occurrences: [], terminal_trajectories: terminal, children: [] })
const evaluated = (id: string, score: number): TrajectoryQualityEvaluation => ({ trajectory_id: id, global_score: 4.5, passed_threshold: true, dimension_global_scores: [{ dimension_name: '效率', score: 4 }], step_evaluations: [{ step_id: 1, step_quality_summary: '原有逐步评价', dimension_scores: [] }], fusion: { version: 1, lambda: .5, valid: true, score, invalid_reasons: [] }, checklist: { status: 'succeeded', definition_digest: 'rules-digest', source: 'profile', total: 3, passed: 2, failed: 0, unchecked: 1, verdict: 'partial_success', items: [{ id: 'c1', description: '目标证据', status: 'passed', evidence: ['step1'] }] } })
async function renderScore(evaluation?: TrajectoryQualityEvaluation) {
  const app = createSSRApp(QualityTrajectoryScore, { evaluation })
  const pass = defineComponent({ setup: (_props, { slots }) => () => h('div', slots.default?.()) })
  for (const tag of ['el-collapse', 'el-collapse-item']) app.component(tag, pass)
  return renderToString(app)
}

describe('fusion result and tree display', () => {
  it('displays Q on every step and root, not the sibling advantage or terminal G', async () => {
    const root = node(0), terminal = node(1, ['t1', 't2'])
    root.children.push(terminal)
    const html = await renderToString(createSSRApp(ReferenceTrajectoryTree, { root, nodeScores: { '0': { q: .8, advantage: 41, scored_count: 2, total_count: 2, status: 'complete' }, '1': { q: .7, advantage: 42, scored_count: 1, total_count: 2, status: 'partial' } } }))
    expect(html).toContain('任务均分 Q 0.800')
    expect(html).toContain('Q 0.700 · 1/2')
    expect(html).toContain('2 条轨迹在此结束，点击分别查看')
    expect(html).not.toContain('41.000')
    expect(html).not.toContain('/ 5')
  })
  it('keeps each terminal trajectory F distinct from the shared-node average', async () => {
    const first = await renderScore(evaluated('t1', .701)), second = await renderScore(evaluated('t2', .899))
    expect(first).toContain('0.701'); expect(first).not.toContain('0.899')
    expect(second).toContain('0.899'); expect(second).not.toContain('0.701')
    for (const html of [first, second]) {
      expect(html).toContain('Rubric 总分 G')
      expect(html).toContain('4.50 / 5')
      expect(html).toContain('2 / 3 · 66.7%')
      expect(html).toContain('partial_success')
      expect(html).toContain('原有逐步评价')
      expect(html).toContain('目标证据')
    }
  })
  it('leaves historical Rubric-only evaluation visibly unscored for fusion', async () => {
    const historical = evaluated('old', .99)
    delete historical.fusion; delete historical.checklist
    const html = await renderScore(historical)
    expect(html).toContain('未评测')
    expect(html).toContain('Checklist 未评测')
    expect(html).not.toContain('0.000')
    expect(await renderScore()).toContain('本条轨迹待评分')
  })
})
