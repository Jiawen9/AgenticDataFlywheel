import { describe, expect, it } from 'vitest'
import type { AugmentationSeed, TaskGenerationResult } from '@/types'
import { augmentationResultGroupId, groupAugmentationResults } from './augmentationReview'

const result = (id: string, extra: Partial<TaskGenerationResult> = {}): TaskGenerationResult => ({
  result_id: id, task: id, app: 'App', scene: '场景', capability: '能力', sub_capability: '任务类型', deleted: false, ...extra,
})
describe('augmentation review source groups', () => {
  it('groups ten variants under one source and retains original order and objects', () => {
    const rows = Array.from({ length: 10 }, (_, i) => result(String(i), { seed_id: 'one', source_task: '失败任务' }))
    const before = JSON.stringify(rows)
    const groups = groupAugmentationResults(rows, [])
    expect(groups).toHaveLength(1)
    expect(groups[0]).toMatchObject({ id: 'seed:one', sourceTask: '失败任务', activeCount: 10 })
    expect(groups[0]!.results.map(row => row.result_id)).toEqual(rows.map(row => row.result_id))
    expect(groups[0]!.results[0]).toBe(rows[0])
    expect(JSON.stringify(rows)).toBe(before)
  })
  it('does not merge identical failure texts with different seeds or source rows', () => {
    const rows = [
      result('a', { seed_id: 'a', source_row: 2, source_task: '相同任务' }),
      result('b', { seed_id: 'b', source_row: 2, source_task: '相同任务' }),
      result('c', { source_row: 2, source_task: '相同任务' }),
      result('d', { source_row: 3, source_task: '相同任务' }),
    ]
    expect(groupAugmentationResults(rows, []).map(group => group.id)).toEqual(['seed:a', 'seed:b', 'row:2', 'row:3'])
  })
  it('supports valid historical row numbers and never guesses an unidentified source', () => {
    const rows = [result('a', { source_row: '2' as unknown as number }), result('b', { source_row: 2 })]
    expect(groupAugmentationResults(rows, [])).toHaveLength(1)
    for (const value of [undefined, null, '', ' ', '2.5', 0, -1, true, Infinity, 2.5]) {
      const row = result('legacy', { source_row: value as number, '源失败任务': '保留旧来源' })
      expect(augmentationResultGroupId(row)).toBe('result:legacy')
      expect(groupAugmentationResults([row], [])[0]).toMatchObject({ incompleteSource: true, sourceTask: '保留旧来源' })
    }
    expect(groupAugmentationResults([result('x'), result('y')], [])).toHaveLength(2)
  })
  it('uses preview metadata and keeps fully deleted groups recoverable', () => {
    const rows = [result('a', { seed_id: 'one', source_task: '旧记录', deleted: true }), result('b', { seed_id: 'one', deleted: true })]
    const seeds = [{ seed_id: 'one', source_row: 4, task: '真实源失败任务', app: '源 App' } as AugmentationSeed]
    expect(groupAugmentationResults(rows, seeds)[0]).toMatchObject({ sourceRow: 4, sourceTask: '真实源失败任务', app: '源 App', activeCount: 0, results: rows })
    rows[1]!.deleted = false
    expect(groupAugmentationResults(rows, seeds)[0]!.activeCount).toBe(1)
  })
  it('regroups refreshed records without changing parent identity after text edits', () => {
    const first = [result('a', { seed_id: 'one' }), result('b', { seed_id: 'two' }), result('c', { seed_id: 'one' })]
    expect(groupAugmentationResults(first, []).map(group => group.id)).toEqual(['seed:one', 'seed:two'])
    const next = first.map(row => ({ ...row, task: '已人工修改' }))
    expect(groupAugmentationResults(next, []).map(group => group.id)).toEqual(['seed:one', 'seed:two'])
    expect(groupAugmentationResults(next, [])[0]!.results.map(row => row.result_id)).toEqual(['a', 'c'])
  })
})

