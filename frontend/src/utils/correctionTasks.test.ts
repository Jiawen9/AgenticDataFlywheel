import { describe, expect, it } from 'vitest'
import type { CorrectionGroupSummary, CorrectionRecommendation } from '@/types'
import { correctionTasks } from './correctionTasks'

describe('saved correction ranking', () => {
  const groups = ['t1', 't2'].map(id => ({ group_id: id, meta_task: id, edited_row_count: 0, export: false })) as CorrectionGroupSummary[]
  const selection = (): CorrectionRecommendation => ({ status: 'ready', tasks: [
    { task_id: 'case', goal: '', trajectory_id: 't1', global_score: 5, fusion_score: .3, ranking_basis: 'fusion', passed_threshold: true, trajectory_count: 2, step_count: 2 },
    { task_id: 'case', goal: '', trajectory_id: 't2', global_score: 3, fusion_score: .9, ranking_basis: 'fusion', passed_threshold: false, trajectory_count: 2, step_count: 3 },
  ] })
  it('ranks fused candidates by saved F while retaining G and the saved selection', () => {
    const value = selection(), before = JSON.stringify(value)
    const result = correctionTasks(value, groups)
    expect(result[0]?.trajectories.map(item => item.trajectory_id)).toEqual(['t2', 't1'])
    expect(result[0]?.trajectories[0]).toMatchObject({ fusion_score: .9, global_score: 3, rank: 1 })
    expect(JSON.stringify(value)).toBe(before)
  })
  it('preserves historical Rubric ranking without introducing inferred F', () => {
    const value = selection()
    value.tasks.forEach(item => { delete item.fusion_score; delete item.ranking_basis })
    expect(correctionTasks(value, groups)[0]?.trajectories.map(item => item.trajectory_id)).toEqual(['t1', 't2'])
  })
})
