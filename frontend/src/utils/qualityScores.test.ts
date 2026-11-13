import { describe, expect, it } from 'vitest'
import type { ChecklistQualityEvaluation, QualityNodeScore, TrajectoryQualityEvaluation } from '@/types'
import { checklistRate, fusionScore, nodeScoreLabel, qualityJobStageLabel, scoreText } from './qualityScores'

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
  it('uses explicit F validity and Checklist denominator without deriving a verdict', () => {
    expect(fusionScore({ fusion: { valid: true, score: 0.9 } } as TrajectoryQualityEvaluation)).toBe(.9)
    expect(checklistRate({ total: 3, passed: 2 } as ChecklistQualityEvaluation)).toBeCloseTo(2 / 3)
    expect(checklistRate({ total: 3, passed: 0 } as ChecklistQualityEvaluation)).toBe(0)
    expect(checklistRate({ total: 0, passed: 0 } as ChecklistQualityEvaluation)).toBeNull()
    expect(checklistRate(undefined)).toBeNull()
  })
  it.each([
    ['checklist_preparing', '准备 Checklist'], ['checklist_evaluating', 'Checklist 评测中'], ['fusing', '融合评分与节点 Q'],
  ] as const)('describes active stage %s rather than marking it complete', (stage, label) => {
    expect(qualityJobStageLabel({ status: 'running', stage })).toBe(label)
  })
})
