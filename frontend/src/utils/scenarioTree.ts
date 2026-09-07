import type { TaskGenerationSelection, TaskGenerationTreeNode, TaskTypeAppConfig } from '@/types'

export interface TreeAddActionNode {
  id: string
  label: string
  kind: 'add_action'
  parent_id: string
  add_kind: 'capability' | 'sub_capability' | 'app'
}

export type ScenarioTreeDisplayNode = Omit<TaskGenerationTreeNode, 'children'> & { children?: ScenarioTreeDisplayNode[] } | TreeAddActionNode

export const NODE_LABELS = {
  scene: '场景',
  capability: '二级场景',
  sub_capability: '任务类型',
  app: 'App',
} as const

export const NODE_LEVELS = {
  scene: 'L1',
  capability: 'L2',
  sub_capability: 'L3',
  app: 'L4',
} as const

export function appNodes(taskType: TaskGenerationTreeNode): TaskGenerationTreeNode[] {
  const children = (taskType.children || []).filter(node => node.kind === 'app')
  if (children.length) return children
  return (taskType.app_configs || []).map(config => ({
    id: config.id || `legacy-app:${taskType.id}:${config.app}`,
    kind: 'app' as const,
    label: config.app,
    app: config.app,
    reference_example: config.reference_example,
    use_resource_prior: config.use_resource_prior,
    control_prior_available: config.control_prior_available,
    resource_count: config.resource_count,
    description: '',
  }))
}

export function normalizeTree(nodes: TaskGenerationTreeNode[]): TaskGenerationTreeNode[] {
  return nodes.map(node => {
    const base = { ...node, description: node.description || '' }
    if (node.kind === 'sub_capability') {
      return { ...base, app_configs: undefined, children: appNodes(node).map(app => ({ ...app, children: undefined })) }
    }
    if (node.kind === 'app') return { ...base, app: node.app || node.label, children: undefined }
    return { ...base, children: normalizeTree(node.children || []) }
  })
}

export function withInlineAddActions(nodes: TaskGenerationTreeNode[]): ScenarioTreeDisplayNode[] {
  return nodes.map(node => {
    if (node.kind === 'app') return { ...node }
    const children = withInlineAddActions(node.children || [])
    const addKind = node.kind === 'scene' ? 'capability' : node.kind === 'capability' ? 'sub_capability' : 'app'
    const label = node.kind === 'scene' ? '＋ 新增二级场景' : node.kind === 'capability' ? '＋ 新增任务类型' : '＋ 新增 App'
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

export function parentOf(nodes: TaskGenerationTreeNode[], id: string): TaskGenerationTreeNode | undefined {
  for (const node of nodes) {
    if ((node.children || []).some(child => child.id === id)) return node
    const found = parentOf(node.children || [], id)
    if (found) return found
  }
}

export function editableTree(nodes: TaskGenerationTreeNode[]): TaskGenerationTreeNode[] {
  return nodes.map(node => {
    const base: TaskGenerationTreeNode = { id: node.id, kind: node.kind, label: node.label, description: node.description || '' }
    if (node.kind === 'app') {
      return { ...base, app: node.label, reference_example: node.reference_example || '', use_resource_prior: Boolean(node.use_resource_prior) }
    }
    return { ...base, children: editableTree(node.children || []) }
  })
}

export function appConfigs(apps: string[], existing: TaskTypeAppConfig[]): TaskTypeAppConfig[] {
  return [...new Set(apps.map(app => app.trim()).filter(Boolean))].map(app =>
    existing.find(config => config.app === app) || { app, reference_example: '', use_resource_prior: false },
  )
}

export function selectionsFor(nodes: TaskGenerationTreeNode[], checked: string[], selectedApps: Record<string, string[]>): TaskGenerationSelection[] {
  return leaves(nodes).filter(node => checked.includes(node.id)).map(node => ({
    node_id: node.id,
    apps: [...new Set(selectedApps[node.id] || [])].filter(app => appNodes(node).some(config => (config.app || config.label) === app)),
  }))
}

export function executionUnitCount(selections: TaskGenerationSelection[]): number {
  return selections.reduce((total, selection) => total + selection.apps.length, 0)
}

export function descendantIds(node: TaskGenerationTreeNode): string[] {
  return (node.children || []).flatMap(child => [child.id, ...descendantIds(child)])
}

export function canMove(node: TaskGenerationTreeNode, target: TaskGenerationTreeNode): boolean {
  if (node.id === target.id || descendantIds(node).includes(target.id)) return false
  return (node.kind === 'capability' && target.kind === 'scene')
    || (node.kind === 'sub_capability' && target.kind === 'capability')
    || (node.kind === 'app' && target.kind === 'sub_capability')
}

export function moveNode(nodes: TaskGenerationTreeNode[], nodeId: string, targetId: string): boolean {
  const node = findNode(nodes, nodeId)
  const target = findNode(nodes, targetId)
  if (!node || !target || !canMove(node, target)) return false
  if (!removeNode(nodes, nodeId)) return false
  target.children ||= []
  target.children.push(node)
  return true
}

/** Reorder a node before another node in the same level and parent branch. */
export function reorderNode(nodes: TaskGenerationTreeNode[], nodeId: string, targetId: string): boolean {
  const node = findNode(nodes, nodeId)
  const target = findNode(nodes, targetId)
  if (!node || !target || node.id === target.id || node.kind !== target.kind) return false

  const sourceParent = parentOf(nodes, nodeId)
  const targetParent = parentOf(nodes, targetId)
  const source = sourceParent?.children || nodes
  const destination = targetParent?.children || nodes
  if (source !== destination) return false

  const sourceIndex = source.findIndex(item => item.id === nodeId)
  if (sourceIndex < 0) return false
  const [moved] = source.splice(sourceIndex, 1)
  const targetIndex = source.findIndex(item => item.id === targetId)
  source.splice(targetIndex < 0 ? source.length : targetIndex, 0, moved!)
  return true
}

export function removeNode(nodes: TaskGenerationTreeNode[], id: string): boolean {
  const index = nodes.findIndex(node => node.id === id)
  if (index >= 0) { nodes.splice(index, 1); return true }
  return nodes.some(node => removeNode(node.children || [], id))
}

export function searchNodes(nodes: TaskGenerationTreeNode[], text: string): TaskGenerationTreeNode[] {
  const keyword = text.trim().toLowerCase()
  if (!keyword) return []
  const result: TaskGenerationTreeNode[] = []
  const visit = (items: TaskGenerationTreeNode[]) => items.forEach(node => {
    if (node.label.toLowerCase().includes(keyword)) result.push(node)
    visit(node.children || [])
  })
  visit(nodes)
  return result
}
