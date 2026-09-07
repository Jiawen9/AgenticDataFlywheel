import { describe, expect, it } from 'vitest'
import type { TaskGenerationJob, TaskGenerationResult, TaskGenerationTreeNode } from '@/types'
import { dependencyLabel, filterResultGroups, generationCandidates, generationDirectory, groupGenerationResults, reconcileApps, resultCounts, resultTypeKey, selectCandidates, unitLabel } from './taskGeneration'
import { executionUnitCount, selectionsFor } from './scenarioTree'

const tree: TaskGenerationTreeNode[] = [
  { id: 's1', kind: 'scene', label: '媒体', children: [{ id: 'c1', kind: 'capability', label: '查找', children: [
    { id: 't1', kind: 'sub_capability', label: '搜索视频', children: ['甲', '乙'].map(app => ({ id: app, kind: 'app', app, label: app })) },
    { id: 't2', kind: 'sub_capability', label: '空类型', children: [] },
  ] }] },
  { id: 's2', kind: 'scene', label: '出行', children: [{ id: 'c2', kind: 'capability', label: '导航', children: [
    { id: 't3', kind: 'sub_capability', label: '找路线', children: [{ id: '丙', kind: 'app', label: '丙' }] },
  ] }] },
]
const row = (id: string, extra: Partial<TaskGenerationResult> = {}): TaskGenerationResult => ({ result_id: id, task_uuid: id, app: '甲', scene: '媒体', capability: '查找', sub_capability: '搜索视频', task: id, deleted: false, pre_dependency: 'zero', ...extra })

describe('task generation selection and review selectors', () => {
  it('renders only L1/L2 directory and links the current scene/capability scope to L3 candidates', () => {
    expect(generationDirectory(tree)[0]!.children![0]!.children).toEqual([])
    expect(generationCandidates(tree, 's1', '').map(n => n.id)).toEqual(['t1', 't2'])
    expect(generationCandidates(tree, 'c2', '').map(n => n.id)).toEqual(['t3'])
    expect(generationCandidates(tree, 'gone', '')).toEqual([])
  })
  it('searches paths and App names, intersects scope without mutating the tree', () => {
    expect(generationCandidates(tree, '', ' 乙 ').map(n => n.id)).toEqual(['t1'])
    expect(generationCandidates(tree, '', '导航').map(n => n.id)).toEqual(['t3'])
    expect(generationCandidates(tree, 's1', '导航')).toEqual([])
    expect(generationCandidates(tree, '', '')).toHaveLength(3)
  })
  it('selects only filtered candidates and retains cross-scene selections', () => {
    const initial = { t3: ['丙'] }
    const selected = selectCandidates(initial, generationCandidates(tree, '', '乙'))
    expect(selected).toEqual({ t1: ['甲', '乙'], t3: ['丙'] })
    expect(initial).toEqual({ t3: ['丙'] })
    expect(executionUnitCount(selectionsFor(tree, Object.keys(selected), selected)) * 5).toBe(15)
  })
  it('preserves valid partial App choices on refresh and reports invalid execution units', () => {
    expect(reconcileApps(tree, { t1: ['乙'], t3: ['丙'] })).toEqual({ selected: { t1: ['乙'], t3: ['丙'] }, removed: 0 })
    expect(reconcileApps(tree, { t1: ['甲', '不存在'], gone: ['甲'], t2: [] })).toEqual({ selected: { t1: ['甲'] }, removed: 2 })
    expect(reconcileApps([], { t1: ['甲'] })).toEqual({ selected: {}, removed: 1 })
  })
  it('groups prerequisites without losing independent legacy rows or changing main order', () => {
    const rows = [row('pre', { pre_dependency: 'pre_node', dependency_group_id: 'g' }), row('main', { pre_dependency: 'weak', pre_task_uuid: 'pre', dependency_group_id: 'g' }), row('zero'), row('orphan', { pre_dependency: 'pre_node' })]
    const grouped = groupGenerationResults(rows)
    expect(grouped.map(g => g.main.result_id)).toEqual(['main', 'zero', 'orphan'])
    expect(grouped[0]!.prerequisites.map(r => r.result_id)).toEqual(['pre'])
    expect(grouped[2]!.orphan).toBe(true)
    expect(rows.map(r => r.result_id)).toEqual(['pre', 'main', 'zero', 'orphan'])
  })
  it('filters by main task but keeps prerequisites with different classification', () => {
    const rows = [row('pre', { app: '乙', sub_capability: '登录', pre_dependency: 'pre_node' }), row('main', { pre_dependency: 'weak', pre_task_uuid: 'pre' }), row('deleted', { deleted: true })]
    const groups = groupGenerationResults(rows)
    const filter = { app: '甲', type: resultTypeKey(rows[1]!), dependency: 'weak', showDeleted: false }
    expect(filterResultGroups(groups, filter)[0]!.prerequisites).toHaveLength(1)
    expect(filterResultGroups(groups, { app: '', type: '', dependency: '', showDeleted: true })).toHaveLength(2)
  })
  it('keeps export counts independent of display filters, including strong dependencies', () => {
    const rows = [row('strong', { pre_dependency: 'strong' }), row('pre', { pre_dependency: 'pre_node' }), row('deleted', { deleted: true })]
    expect(resultCounts(rows)).toEqual({ main: 1, prerequisites: 1, strong: 1 })
    expect(resultCounts([])).toEqual({ main: 0, prerequisites: 0, strong: 0 })
  })
  it('does not label dependency errors as successful zero-dependency results', () => {
    const broken = row('broken', { dependency_error: '模型异常' })
    expect(dependencyLabel(broken)).toBe('依赖判定异常')
    expect(filterResultGroups(groupGenerationResults([broken]), { app: '', type: '', dependency: 'zero', showDeleted: false })).toHaveLength(0)
  })
  it('maps progress exclusively from job snapshot with legacy ID fallback', () => {
    const job = { execution_units: [{ execution_unit_id: 'unit', scene: '旧场景', capability: '旧能力', sub_capability: '旧类型', app: '甲' }] } as TaskGenerationJob
    expect(unitLabel(job, 'unit')).toBe('旧场景 / 旧能力 / 旧类型 · 甲')
    expect(unitLabel(job, 'legacy')).toBe('legacy')
    expect(unitLabel({} as TaskGenerationJob, 'unit')).toBe('unit')
  })
})
