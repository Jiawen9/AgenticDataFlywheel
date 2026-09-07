import { describe, expect, it } from 'vitest'
import type { TaskGenerationTreeNode } from '@/types'
import { legacyScenarioStudioQuery, sceneIdForNode, scenarioStudioInitialNodeId, scenarioStudioMode } from './scenarioStudio'

describe('scenario studio mode routing', () => {
  it('opens the canonical route in tree home mode by default', () => {
    expect(scenarioStudioMode({})).toBe('treeHome')
    expect(scenarioStudioInitialNodeId({})).toBe('')
  })

  it('recognizes editor deep links and their selected node', () => {
    expect(scenarioStudioMode({ mode: 'editor', l1: 'scene-1' })).toBe('editor')
    expect(scenarioStudioMode({ l1: 'scene-1' })).toBe('editor')
    expect(scenarioStudioInitialNodeId({ mode: 'editor', l1: 'scene-1' })).toBe('scene-1')
  })

  it('preserves the legacy editor route as a canonical Studio query', () => {
    expect(legacyScenarioStudioQuery({})).toEqual({ mode: 'editor' })
    expect(legacyScenarioStudioQuery({ l1: 'scene-1' })).toEqual({ mode: 'editor', l1: 'scene-1' })
  })

  it('resolves a search result to its ancestor L1 scene', () => {
    const tree: TaskGenerationTreeNode[] = [{
      id: 'scene-1', kind: 'scene', label: '场景', children: [{
        id: 'cap-1', kind: 'capability', label: '能力', children: [{
          id: 'sub-1', kind: 'sub_capability', label: '子能力', children: [{ id: 'app-1', kind: 'app', label: 'App' }],
        }],
      }],
    }]
    expect(sceneIdForNode(tree, 'scene-1')).toBe('scene-1')
    expect(sceneIdForNode(tree, 'cap-1')).toBe('scene-1')
    expect(sceneIdForNode(tree, 'sub-1')).toBe('scene-1')
    expect(sceneIdForNode(tree, 'app-1')).toBe('scene-1')
    expect(sceneIdForNode(tree, 'missing')).toBe('')
  })
})
