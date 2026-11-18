import type { CorrectionGroup, CorrectionRow, QualityNodeScore, TrajectoryTreeNode } from '@/types'

export interface CorrectionTreeTarget {
  group_id: string
  trajectory_id: string
  step: number
  step_key: string
  excel_row: number
  edited: boolean
  deleted: boolean
}

export interface CorrectionTaskTree {
  session_id: string
  task_id: string
  storage_revision: number
  tree: TrajectoryTreeNode | null
  reason: string | null
  node_scores: Record<string, QualityNodeScore>
  tree_version: string | null
  quality_version: string | null
  tree_hash: string | null
  targets: Record<string, CorrectionTreeTarget[]>
  key_node_ids: number[]
}

export function findTargetRow(group: CorrectionGroup, target: CorrectionTreeTarget) {
  if (group.group_id !== target.group_id) return undefined
  return group.rows.find(row => !row.deleted && row.step_key === target.step_key && row.excel_row === target.excel_row)
}

export function targetsForGroup(tree: CorrectionTaskTree, groupId: string) {
  return Object.entries(tree.targets)
    .flatMap(([nodeId, targets]) => targets.filter(target => target.group_id === groupId).map(target => ({ nodeId: Number(nodeId), target })))
    .sort((a, b) => a.target.excel_row - b.target.excel_row)
}

export function firstTreeTarget(tree: CorrectionTaskTree, groupId: string) {
  const targets = targetsForGroup(tree, groupId).filter(({ target }) => !target.deleted)
  return targets.find(({ nodeId }) => tree.key_node_ids.includes(nodeId)) ?? targets[0]
}

export function nodeForRow(tree: CorrectionTaskTree | null, groupId: string, row: CorrectionRow | null) {
  if (!tree || !row?.step_key) return undefined
  return targetsForGroup(tree, groupId)
    .find(({ target }) => target.step_key === row.step_key && target.excel_row === row.excel_row)?.nodeId
}

export function correctionNodeDecision(targets: CorrectionTreeTarget[], groupId: string) {
  const available = targets.filter(target => !target.deleted)
  const current = available.filter(target => target.group_id === groupId)
  if (current.length === 1) return { target: current[0], choices: [] }
  if (available.length === 1) return { target: available[0], choices: [] }
  return { target: undefined, choices: available }
}
