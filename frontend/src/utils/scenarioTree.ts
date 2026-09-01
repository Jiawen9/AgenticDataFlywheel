import type { TaskGenerationSelection, TaskGenerationTreeNode, TaskTypeAppConfig } from '@/types'

export interface TreeAddActionNode {
  id: string
  label: string
  kind: 'add_action'
  parent_id: string
  add_kind: 'capability' | 'sub_capability'
}

export type ScenarioTreeDisplayNode = Omit<TaskGenerationTreeNode, 'children'> & { children?: ScenarioTreeDisplayNode[] } | TreeAddActionNode

export function withInlineAddActions(nodes: TaskGenerationTreeNode[]): ScenarioTreeDisplayNode[] {
  return nodes.map(node => {
    if (node.kind === 'sub_capability') return { ...node }
    const children = withInlineAddActions(node.children || [])
    const addKind = node.kind === 'scene' ? 'capability' : 'sub_capability'
    const label = node.kind === 'scene' ? '＋ 新增一级能力' : '＋ 新增任务类型'
    children.push({ id: `__add__:${node.id}:${addKind}`, label, kind: 'add_action', parent_id: node.id, add_kind: addKind })
    return { ...node, children }
  })
}

export function leaves(nodes: TaskGenerationTreeNode[]): TaskGenerationTreeNode[] {
  return nodes.flatMap(node => node.kind === 'sub_capability' ? [node] : leaves(node.children || []))
}

export function findNode(nodes: TaskGenerationTreeNode[], id: string): TaskGenerationTreeNode | undefined {
  for (const node of nodes) {
    if (node.id === id) return node
    const found = findNode(node.children || [], id)
    if (found) return found
  }
}

export function nodePath(nodes: TaskGenerationTreeNode[], id: string, prefix: string[] = []): string[] {
  for (const node of nodes) {
    const path = [...prefix, node.label]
    if (node.id === id) return path
    const found = nodePath(node.children || [], id, path)
    if (found.length) return found
  }
  return []
}

export function editableTree(nodes: TaskGenerationTreeNode[]): TaskGenerationTreeNode[] {
  return nodes.map(node => ({
    id: node.id, kind: node.kind, label: node.label,
    ...(node.kind === 'sub_capability'
      ? { app_configs: (node.app_configs || []).map(({ app, reference_example, use_resource_prior }) => ({ app, reference_example, use_resource_prior })) }
      : { children: editableTree(node.children || []) }),
  }))
}

export function appConfigs(apps: string[], existing: TaskTypeAppConfig[]): TaskTypeAppConfig[] {
  return [...new Set(apps.map(app => app.trim()).filter(Boolean))].map(app =>
    existing.find(config => config.app === app) || { app, reference_example: '', use_resource_prior: false },
  )
}

export function selectionsFor(nodes: TaskGenerationTreeNode[], checked: string[], selectedApps: Record<string, string[]>): TaskGenerationSelection[] {
  return leaves(nodes).filter(node => checked.includes(node.id)).map(node => ({
    node_id: node.id,
    apps: [...new Set(selectedApps[node.id] || [])].filter(app => node.app_configs?.some(config => config.app === app)),
  }))
}

export function executionUnitCount(selections: TaskGenerationSelection[]): number {
  return selections.reduce((total, selection) => total + selection.apps.length, 0)
}

export function removeNode(nodes: TaskGenerationTreeNode[], id: string): boolean {
  const index = nodes.findIndex(node => node.id === id)
  if (index >= 0) { nodes.splice(index, 1); return true }
  return nodes.some(node => removeNode(node.children || [], id))
}
