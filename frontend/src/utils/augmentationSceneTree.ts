import type { AugmentationSeed, TaskGenerationTree, TaskGenerationTreeNode } from '@/types'
import { normalizeTree } from './scenarioTree'

export const AUGMENTATION_STATE_LABELS = {
  waiting: '等待',
  generating: '生成中',
  completed: '完成',
  exception: '异常',
} as const

export type AugmentationNodeState = keyof typeof AUGMENTATION_STATE_LABELS

export interface AugmentationNodeAssociation {
  seedIds: string[]
  states: Record<AugmentationNodeState, number>
}

export interface AugmentationSceneTreeIndex {
  scenes: TaskGenerationTreeNode[]
  paths: Map<string, string[]>
  nodes: Map<string, TaskGenerationTreeNode>
}

export interface AugmentationSceneTreeModel extends AugmentationSceneTreeIndex {
  associations: Map<string, AugmentationNodeAssociation>
  matchedPaths: Map<string, string[]>
  totalSeeds: number
}

export interface AugmentationTreeExpansion {
  expanded: Set<string>
  seenMatchedSeeds: Set<string>
}

export type AugmentationTreeDisplayMode = 'related' | 'all'

/** Prune only the rendered branches; counts and focus still use the full snapshot. */
export function visibleAugmentationScenes(model: AugmentationSceneTreeModel, mode: AugmentationTreeDisplayMode): TaskGenerationTreeNode[] {
  if (mode === 'all') return model.scenes
  const related = (nodes: TaskGenerationTreeNode[]): TaskGenerationTreeNode[] => nodes
    .filter(node => model.associations.has(node.id))
    .map(node => node.children?.length ? { ...node, children: related(node.children) } : node)
  return related(model.scenes)
}

const levels: TaskGenerationTreeNode['kind'][] = ['scene', 'capability', 'sub_capability', 'app']

export function augmentationNodeState(status: AugmentationSeed['generation_status']): AugmentationNodeState {
  if (status === 'waiting' || status === 'generating') return status
  return status === 'succeeded' ? 'completed' : 'exception'
}

/** The immutable job snapshot is normalized once, independently of seed polls. */
export function indexAugmentationSceneTree(tree: TaskGenerationTree): AugmentationSceneTreeIndex {
  const scenes = normalizeTree(tree.scenes)
  const paths = new Map<string, string[]>()
  const nodes = new Map<string, TaskGenerationTreeNode>()
  function visit(items: TaskGenerationTreeNode[], ancestors: string[] = []) {
    for (const node of items) {
      const path = [...ancestors, node.id]
      paths.set(node.id, path)
      nodes.set(node.id, node)
      visit(node.children || [], path)
    }
  }
  visit(scenes)
  return { scenes, paths, nodes }
}

/** Only a complete, ordered path from the job's snapshot is an association. */
export function associateAugmentationSeeds(index: AugmentationSceneTreeIndex, seeds: AugmentationSeed[]): AugmentationSceneTreeModel {
  const { paths, nodes } = index
  const associations = new Map<string, AugmentationNodeAssociation>()
  const matchedPaths = new Map<string, string[]>()
  // A repeated seed record is an update, not another failed case.
  const uniqueSeeds = new Map(seeds.map(seed => [seed.seed_id, seed]))
  for (const seed of uniqueSeeds.values()) {
    if (seed.mapping_status !== 'matched' || seed.node_path_ids.length !== levels.length) continue
    const expected = paths.get(seed.node_path_ids[3]!)
    if (!expected || expected.length !== levels.length) continue
    if (!expected.every((id, depth) => id === seed.node_path_ids[depth] && nodes.get(id)?.kind === levels[depth])) continue
    matchedPaths.set(seed.seed_id, expected)
    for (const id of expected) {
      const association = associations.get(id) || {
        seedIds: [],
        states: { waiting: 0, generating: 0, completed: 0, exception: 0 },
      }
      association.seedIds.push(seed.seed_id)
      association.states[augmentationNodeState(seed.generation_status)] += 1
      associations.set(id, association)
    }
  }
  return { ...index, associations, matchedPaths, totalSeeds: uniqueSeeds.size }
}

export function buildAugmentationSceneTree(tree: TaskGenerationTree, seeds: AugmentationSeed[]): AugmentationSceneTreeModel {
  return associateAugmentationSeeds(indexAugmentationSceneTree(tree), seeds)
}

/** Open newly mapped seeds once; a status poll must not undo a user's collapse. */
export function expandNewAugmentationSeeds(model: AugmentationSceneTreeModel, previous: AugmentationTreeExpansion): AugmentationTreeExpansion {
  const expanded = new Set([...previous.expanded].filter(id => model.paths.has(id)))
  const seenMatchedSeeds = new Set(previous.seenMatchedSeeds)
  for (const [seedId, path] of model.matchedPaths) {
    if (seenMatchedSeeds.has(seedId)) continue
    path.slice(0, -1).forEach(id => expanded.add(id))
    seenMatchedSeeds.add(seedId)
  }
  return { expanded, seenMatchedSeeds }
}

export function expandFocusedAugmentationSeed(model: AugmentationSceneTreeModel, expanded: Set<string>, seedId?: string): Set<string> {
  const next = new Set(expanded)
  if (seedId) model.matchedPaths.get(seedId)?.slice(0, -1).forEach(id => next.add(id))
  return next
}
