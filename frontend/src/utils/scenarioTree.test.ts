import { describe, expect, it } from 'vitest'
import type { TaskGenerationTreeNode } from '@/types'
import { appConfigs, appNodes, canMove, editableTree, executionUnitCount, findNode, leaves, moveNode, nodePath, removeNode, reorderNode, selectionsFor, withInlineAddActions } from './scenarioTree'

const fixture = (): TaskGenerationTreeNode[] => [{
  id: 'scene', kind: 'scene', label: '视频娱乐', children: [{
    id: 'cap', kind: 'capability', label: '内容查找', children: [{
      id: 'leaf', kind: 'sub_capability', label: '搜索节目', description: '任务类型描述', children: [
        { id: 'app-a', kind: 'app', label: 'AppA', app: 'AppA', reference_example: 'A示例', use_resource_prior: true, resource_count: 2 },
        { id: 'app-b', kind: 'app', label: 'AppB', app: 'AppB', reference_example: 'B示例', use_resource_prior: false },
      ],
    }],
  }],
}]

describe('scenario tree editor', () => {
  it('counts L3 task types separately from L4 App execution units', () => {
    const tree = fixture()
    expect(leaves(tree)).toHaveLength(1)
    const selections = selectionsFor(tree, ['leaf'], { leaf: ['AppA', 'AppB'] })
    expect(executionUnitCount(selections)).toBe(2)
    expect(executionUnitCount(selections) * 5).toBe(10)
    expect(appNodes(leaves(tree)[0]!)).toHaveLength(2)
  })

  it('filters invalid apps without replacing an explicitly empty selection', () => {
    expect(selectionsFor(fixture(), ['leaf'], { leaf: ['AppB', 'unknown', 'AppB'] })).toEqual([{ node_id: 'leaf', apps: ['AppB'] }])
    expect(selectionsFor(fixture(), ['leaf'], { leaf: [] })).toEqual([{ node_id: 'leaf', apps: [] }])
  })

  it('serializes a detached four-level draft without changing saved data or UUID identity', () => {
    const saved = fixture()
    const draft = editableTree(saved)
    draft[0]!.label = '新场景'
    expect(saved[0]!.label).toBe('视频娱乐')
    expect(draft[0]!.id).toBe(saved[0]!.id)
    expect(nodePath(draft, 'app-a')).toEqual(['新场景', '内容查找', '搜索节目', 'AppA'])
    expect(draft[0]!.children![0]!.children![0]!.children![0]).toMatchObject({
      kind: 'app', label: 'AppA', app: 'AppA', reference_example: 'A示例', use_resource_prior: true,
    })
    expect(JSON.stringify(draft)).not.toContain('app_configs')
  })

  it('preserves per-App examples when changing a compatibility App list', () => {
    const configs = [
      { app: 'AppA', reference_example: 'A示例', use_resource_prior: true },
      { app: 'AppB', reference_example: 'B示例', use_resource_prior: false },
    ]
    expect(appConfigs(['AppB', 'AppC'], configs)).toEqual([
      { app: 'AppB', reference_example: 'B示例', use_resource_prior: false },
      { app: 'AppC', reference_example: '', use_resource_prior: false },
    ])
  })

  it('allows only one-level parent moves and preserves the moved node', () => {
    const tree = fixture()
    tree.push({ id: 'scene-2', kind: 'scene', label: '另一个场景', children: [] })
    const capability = findNode(tree, 'cap')!
    const app = findNode(tree, 'app-a')!
    expect(canMove(capability, findNode(tree, 'scene-2')!)).toBe(true)
    expect(canMove(app, findNode(tree, 'scene-2')!)).toBe(false)
    expect(moveNode(tree, 'cap', 'scene-2')).toBe(true)
    expect(nodePath(tree, 'app-a')).toEqual(['另一个场景', '内容查找', '搜索节目', 'AppA'])
  })

  it('reorders nodes only within the same level and parent branch', () => {
    const tree = fixture()
    tree[0]!.children!.push({ id: 'cap-2', kind: 'capability', label: '另一个能力', children: [] })
    expect(reorderNode(tree, 'cap-2', 'cap')).toBe(true)
    expect(tree[0]!.children!.map(node => node.id)).toEqual(['cap-2', 'cap'])
    expect(reorderNode(tree, 'app-a', 'cap')).toBe(false)
  })

  it('deletes a subtree and permits empty parents', () => {
    const tree = fixture()
    expect(removeNode(tree, 'cap')).toBe(true)
    expect(leaves(tree)).toHaveLength(0)
    expect(findNode(tree, 'scene')?.children).toEqual([])
    expect(findNode(tree, 'leaf')).toBeUndefined()
  })

  it('adds display-only actions below every editable level except App', () => {
    const display = withInlineAddActions(fixture())
    const scene = display[0]!
    const capability = scene.children![0]!
    const leaf = capability.children![0]!
    const app = leaf.children![0]!

    expect(scene.children!.at(-1)).toMatchObject({ kind: 'add_action', parent_id: 'scene', add_kind: 'capability', label: '＋ 新增二级场景' })
    expect(capability.children!.at(-1)).toMatchObject({ kind: 'add_action', parent_id: 'cap', add_kind: 'sub_capability', label: '＋ 新增任务类型' })
    expect(leaf.children!.at(-1)).toMatchObject({ kind: 'add_action', parent_id: 'leaf', add_kind: 'app', label: '＋ 新增 App' })
    expect(app.children).toBeUndefined()
    expect(leaves(fixture())).toHaveLength(1)
    expect(JSON.stringify(editableTree(fixture()))).not.toContain('add_action')
  })

  it('adds the correct inline action below empty parents without creating real nodes', () => {
    const empty: TaskGenerationTreeNode[] = [{ id: 'scene', kind: 'scene', label: '空场景', children: [] }]
    const display = withInlineAddActions(empty)
    expect(display[0]!.children).toEqual([{ id: '__add__:scene:capability', label: '＋ 新增二级场景', kind: 'add_action', parent_id: 'scene', add_kind: 'capability' }])
    expect(leaves(empty)).toHaveLength(0)
  })
})
