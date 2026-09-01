import { describe, expect, it } from 'vitest'
import type { TaskGenerationTreeNode } from '@/types'
import { appConfigs, editableTree, executionUnitCount, findNode, leaves, nodePath, removeNode, selectionsFor, withInlineAddActions } from './scenarioTree'

const fixture = (): TaskGenerationTreeNode[] => [{ id: 'scene', kind: 'scene', label: '视频娱乐', children: [{ id: 'cap', kind: 'capability', label: '内容查找', children: [{ id: 'leaf', kind: 'sub_capability', label: '搜索节目', app_configs: [
  { app: 'AppA', reference_example: 'A示例', use_resource_prior: true, resource_count: 2 },
  { app: 'AppB', reference_example: 'B示例', use_resource_prior: false },
] }] }] }]

describe('scenario tree editor', () => {
  it('counts task types separately from app execution units', () => {
    const tree = fixture()
    expect(leaves(tree)).toHaveLength(1)
    const selections = selectionsFor(tree, ['leaf'], { leaf: ['AppA', 'AppB'] })
    expect(executionUnitCount(selections)).toBe(2)
    expect(executionUnitCount(selections) * 5).toBe(10)
  })

  it('filters invalid apps without replacing an explicitly empty selection', () => {
    expect(selectionsFor(fixture(), ['leaf'], { leaf: ['AppB', 'unknown', 'AppB'] })).toEqual([{ node_id: 'leaf', apps: ['AppB'] }])
    expect(selectionsFor(fixture(), ['leaf'], { leaf: [] })).toEqual([{ node_id: 'leaf', apps: [] }])
  })

  it('edits a detached draft without changing saved data or UUID identity', () => {
    const saved = fixture()
    const draft = editableTree(saved)
    draft[0]!.label = '新场景'
    expect(saved[0]!.label).toBe('视频娱乐')
    expect(draft[0]!.id).toBe(saved[0]!.id)
    expect(nodePath(draft, 'leaf')).toEqual(['新场景', '内容查找', '搜索节目'])
    expect(leaves(draft)[0]!.app_configs![0]!.resource_count).toBeUndefined()
  })

  it('preserves per-app examples when adding or removing app configurations', () => {
    const configs = leaves(fixture())[0]!.app_configs!
    const changed = appConfigs(['AppB', 'AppC'], configs)
    expect(changed).toEqual([{ app: 'AppB', reference_example: 'B示例', use_resource_prior: false }, { app: 'AppC', reference_example: '', use_resource_prior: false }])
    expect(configs).toHaveLength(2)
  })

  it('deletes a subtree and permits empty parents', () => {
    const tree = fixture()
    expect(removeNode(tree, 'cap')).toBe(true)
    expect(leaves(tree)).toHaveLength(0)
    expect(findNode(tree, 'scene')?.children).toEqual([])
    expect(findNode(tree, 'leaf')).toBeUndefined()
  })

  it('adds display-only actions below scenes and capabilities', () => {
    const tree = fixture()
    const display = withInlineAddActions(tree)
    const scene = display[0]!
    const capability = scene.children![0]!
    const leaf = capability.children![0]!
    const sceneAction = scene.children![1]!
    const capabilityAction = capability.children![1]!

    expect(sceneAction).toMatchObject({ kind: 'add_action', parent_id: 'scene', add_kind: 'capability', label: '＋ 新增一级能力' })
    expect(capabilityAction).toMatchObject({ kind: 'add_action', parent_id: 'cap', add_kind: 'sub_capability', label: '＋ 新增任务类型' })
    expect(leaf.kind).toBe('sub_capability')
    expect(leaves(tree)).toHaveLength(1)
    expect(JSON.stringify(editableTree(tree))).not.toContain('add_action')
    expect(selectionsFor(tree, ['leaf'], { leaf: ['AppA'] })).toEqual([{ node_id: 'leaf', apps: ['AppA'] }])
  })

  it('adds the inline action below empty parents without creating leaves', () => {
    const empty: TaskGenerationTreeNode[] = [{ id: 'scene', kind: 'scene', label: '空场景', children: [] }]
    const display = withInlineAddActions(empty)
    expect(display[0]!.children).toEqual([{ id: '__add__:scene:capability', label: '＋ 新增一级能力', kind: 'add_action', parent_id: 'scene', add_kind: 'capability' }])
    expect(leaves(empty)).toHaveLength(0)
  })
})
