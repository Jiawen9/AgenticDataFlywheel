import { describe, expect, it } from 'vitest'
import type { ChecklistQualityEvaluation, QualityJob, QualityNodeScore, TaskQualityResult, TrajectoryQualityEvaluation, TrajectoryTreeNode, TreeOccurrence } from '@/types'
import { checklistRate, fusionScore, highestFusionTrajectories, nodeScoreLabel, preferredTrajectorySelection, qualityJobStageLabel, qualityJobProgressText, qualityModelProgressText, scoreText, trajectoryPath, treeTopology } from './qualityScores'

describe('quality score presentation', () => {
  const score = (q: number | null, scored_count = 2, total_count = 2): QualityNodeScore => ({ q, advantage: 99, scored_count, total_count, status: 'complete' })
  it('shows Q to three decimals with partial coverage and labels the root mean', () => {
    expect(nodeScoreLabel(score(0.345678))).toBe('Q 0.346')
    expect(nodeScoreLabel(score(0.345678, 2, 10), true)).toBe('任务均分 Q 0.346 · 2/10')
    expect(nodeScoreLabel(score(0, 1, 1))).toBe('Q 0.000')
  })
  it('does not manufacture zero for unscored, missing or invalid values', () => {
    expect(nodeScoreLabel(undefined)).toBe('Q 待评分')
    for (const value of [score(null), score(Number.NaN), score(5), score(0.5, 0, 5)]) expect(nodeScoreLabel(value)).toBe('Q 暂无有效评分')
    expect(scoreText(null)).toBe('未评测')
    expect(fusionScore({ global_score: 5 } as TrajectoryQualityEvaluation)).toBeNull()
    expect(fusionScore({ fusion: { valid: false, score: 1 } } as TrajectoryQualityEvaluation)).toBeNull()
  })
  it('does not equate an ended job with complete fusion results', () => {
    expect(qualityJobStageLabel({ status: 'succeeded', stage: 'succeeded' })).toBe('评分作业已结束')
  })
  it('separates rubric completion, checklist failures and fusion coverage', () => {
    const text = qualityJobProgressText({ completed_trajectories: 10, total_trajectories: 10, total_rubric_trajectories: 10, completed_rubric_trajectories: 10, total_checklist_trajectories: 10, completed_checklist_trajectories: 10, failed_checklist_trajectories: 2, fusion_eligible_count: 8, partial_results_available: true, percent: 100, status: 'failed' } as QualityJob)
    expect(text).toContain('Rubric 10/10'); expect(text).toContain('Checklist 已结束 10/10')
    expect(text).toContain('失败 2'); expect(text).toContain('融合有效 8/10'); expect(text).toContain('已保留部分结果')
  })
  it('only shows observed model event fields without fabricated timing or retry counts', () => {
    expect(qualityModelProgressText({ current_model_role: 'vision', current_model: 'deployed-id' } as QualityJob)).toBe('角色 vision · 模型 deployed-id')
    expect(qualityModelProgressText({ checklist_event: { kind: 'request_end', role: 'text', model: 'text-id', attempt: 2, elapsed_seconds: 1.25, timeout_seconds: 90 } } as QualityJob)).toContain('第 2 次请求 · 耗时 1.3 秒 · 超时预算 90 秒')
    expect(qualityModelProgressText({} as QualityJob)).toBe('')
  })
  it('uses explicit F validity and Checklist denominator without deriving a verdict', () => {
    expect(fusionScore({ fusion: { valid: true, score: 0.9 } } as TrajectoryQualityEvaluation)).toBe(.9)
    expect(checklistRate({ total: 3, passed: 2 } as ChecklistQualityEvaluation)).toBeCloseTo(2 / 3)
    expect(checklistRate({ total: 3, passed: 0 } as ChecklistQualityEvaluation)).toBe(0)
    expect(checklistRate({ total: 0, passed: 0 } as ChecklistQualityEvaluation)).toBeNull()
    expect(checklistRate(undefined)).toBeNull()
  })
  it.each([
    ['checklist_preparing', '准备 Checklist'], ['checklist_definition', '生成 Checklist 检查项'], ['checklist_evaluating', 'Checklist 评测中'], ['fusing', '融合评分与节点 Q'],
  ] as const)('describes active stage %s rather than marking it complete', (stage, label) => {
    expect(qualityJobStageLabel({ status: 'running', stage })).toBe(label)
  })
})

const treeNode = (id: number, terminal: string[] = [], children: TrajectoryTreeNode[] = []): TrajectoryTreeNode => ({ id, depth: id, label: 'click', action: {}, summary: '', actions_box: '', image: '', xml: '', reference_trajectory: '', reference_step: 1, occurrence_count: 0, occurrences: [], terminal_trajectories: terminal, children })
const evaluation = (score: number | null, valid = true) => ({ fusion: { valid, score }, global_score: 5 }) as TrajectoryQualityEvaluation
const quality = (evaluations: Record<string, TrajectoryQualityEvaluation>, extra: Partial<TaskQualityResult> = {}) => ({ task_id: 'task', evaluations, fusion_complete: true, ...extra }) as TaskQualityResult

describe('highest fused trajectory highlighting', () => {
  it('uses raw valid F, excludes foreign and unknown terminals, and never falls back to Rubric', () => {
    const tree = treeNode(0, [], [treeNode(1, ['low']), treeNode(2, ['high']), treeNode(3, ['invalid'])])
    const result = highestFusionTrajectories(tree, quality({ low: evaluation(.90001), high: evaluation(.90002), invalid: evaluation(1, false), foreign: evaluation(1) }, { fusion_order: ['foreign', 'invalid', 'low', 'high'] }), 'task')
    expect(result).toEqual({ trajectoryIds: ['high'], score: .90002, scoredCount: 2, totalCount: 3, partial: true })
    expect(highestFusionTrajectories(tree, quality({ low: { global_score: 5 } as TrajectoryQualityEvaluation }), 'task').trajectoryIds).toEqual([])
    expect(highestFusionTrajectories(tree, quality({ high: evaluation(1) }, { task_id: 'other' }), 'task').trajectoryIds).toEqual([])
    expect(highestFusionTrajectories({ ...tree, task_id: 'other' }, quality({ high: evaluation(1) }), 'task').trajectoryIds).toEqual([])
  })
  it('keeps tied maxima in frozen ranking order and falls back to stable source order', () => {
    const tree = treeNode(0, [], [treeNode(1, ['a', 'b']), treeNode(2, ['c'])])
    tree.source_trajectories = ['c', 'b', 'a'].map(trajectory => ({ trajectory, original_step_count: 1, tree_step_count: 1, ignored_incidental_step_count: 0, steps: [] }))
    const values = { a: evaluation(.8), b: evaluation(.8), c: evaluation(.8) }
    expect(highestFusionTrajectories(tree, quality(values, { fusion_order: ['b'], trajectory_order: ['a', 'c'] }), 'task').trajectoryIds).toEqual(['b', 'a', 'c'])
    expect(highestFusionTrajectories(tree, quality(values), 'task')).toEqual({ trajectoryIds: ['c', 'b', 'a'], score: .8, scoredCount: 3, totalCount: 3, partial: false })
    expect(highestFusionTrajectories(tree, quality(values, { result_state: 'partial' }), 'task').partial).toBe(true)
  })
  it('treats a valid zero as scored and invalid numbers as unavailable', () => {
    const tree = treeNode(0, ['zero', 'nan', 'missing'])
    expect(highestFusionTrajectories(tree, quality({ zero: evaluation(0), nan: evaluation(Number.NaN) }), 'task')).toEqual({ trajectoryIds: ['zero'], score: 0, scoredCount: 1, totalCount: 3, partial: true })
  })
  it('follows an actual path rather than occurrence labels or local Q maxima', () => {
    const shared = treeNode(1, ['short'], [treeNode(2, ['best', 'same-end']), treeNode(3, ['other'])])
    shared.occurrences = [{ trajectory: 'foreign' } as TreeOccurrence]
    const tree = treeNode(0, ['zero-step'], [shared])
    expect(trajectoryPath(tree, 'best')).toEqual({ nodeIds: [0, 1, 2], edgeIds: ['0-1', '1-2'], terminalNodeId: 2 })
    expect(trajectoryPath(tree, 'same-end')).toEqual(trajectoryPath(tree, 'best'))
    expect(trajectoryPath(tree, 'short')).toEqual({ nodeIds: [0, 1], edgeIds: ['0-1'], terminalNodeId: 1 })
    expect(trajectoryPath(tree, 'zero-step')).toEqual({ nodeIds: [0], edgeIds: [], terminalNodeId: 0 })
    expect(trajectoryPath(tree, 'foreign')).toEqual({ nodeIds: [], edgeIds: [], terminalNodeId: null })
  })
  it('selects the highlighted screenshot and terminal without losing old fallback behavior', () => {
    const node = treeNode(1, ['low', 'best'])
    node.occurrences = [{ trajectory: 'low' }, { trajectory: 'best' }] as TreeOccurrence[]
    expect(preferredTrajectorySelection(node, 'best')).toEqual({ occurrenceIndex: 1, terminalTrajectoryId: 'best' })
    expect(preferredTrajectorySelection(node, 'missing')).toEqual({ occurrenceIndex: 0, terminalTrajectoryId: 'low' })
  })
  it('keeps viewport identity independent of terminal choice, scores and source object identity', () => {
    const tree = treeNode(0, [], [treeNode(1, ['a'])])
    const clone = JSON.parse(JSON.stringify(tree)) as TrajectoryTreeNode
    clone.children[0]!.summary = 'new summary'
    clone.children[0]!.terminal_trajectories = ['b']
    expect(treeTopology(clone)).toBe(treeTopology(tree))
    clone.children.push(treeNode(2, ['c']))
    expect(treeTopology(clone)).not.toBe(treeTopology(tree))
  })
})
