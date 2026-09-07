import type { TaskGenerationTreeNode } from '@/types'

export type ScenarioStudioMode = 'treeHome' | 'editor'

export interface ScenarioEditorState {
  editing: boolean
  dirty: boolean
  saving: boolean
  canUndo: boolean
  canRedo: boolean
  selectedSceneId: string
}

export function queryValue(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

export function scenarioStudioMode(query: Record<string, unknown>): ScenarioStudioMode {
  return queryValue(query.mode) === 'editor' || Boolean(queryValue(query.l1)) ? 'editor' : 'treeHome'
}

export function scenarioStudioInitialNodeId(query: Record<string, unknown>): string {
  return queryValue(query.l1)
}

export function sceneIdForNode(nodes: TaskGenerationTreeNode[], nodeId: string): string {
  const visit = (items: TaskGenerationTreeNode[], sceneId = ''): string => {
    for (const node of items) {
      const currentSceneId = node.kind === 'scene' ? node.id : sceneId
      if (node.id === nodeId) return currentSceneId
      const found = visit(node.children || [], currentSceneId)
      if (found) return found
    }
    return ''
  }
  return visit(nodes)
}

export function legacyScenarioStudioQuery(query: Record<string, unknown>): Record<string, string> {
  const l1 = scenarioStudioInitialNodeId(query)
  return l1 ? { mode: 'editor', l1 } : { mode: 'editor' }
}
