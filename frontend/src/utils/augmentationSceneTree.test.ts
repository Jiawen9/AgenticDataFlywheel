import { describe, expect, it } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import type { AugmentationSeed, TaskGenerationTree, TaskGenerationTreeNode } from '@/types'
import AugmentationSceneTree from '@/components/AugmentationSceneTree.vue'
import {
  augmentationNodeState,
  associateAugmentationSeeds,
  buildAugmentationSceneTree,
  expandFocusedAugmentationSeed,
  expandNewAugmentationSeeds,
  indexAugmentationSceneTree,
  visibleAugmentationScenes,
} from './augmentationSceneTree'

function fixture(): TaskGenerationTree {
  function scene(suffix: string): TaskGenerationTreeNode {
    return {
      id: `scene-${suffix}`, kind: 'scene', label: '相同场景名称', children: [{
        id: `cap-${suffix}`, kind: 'capability', label: '相同能力名称', children: [{
          id: `task-${suffix}`, kind: 'sub_capability', label: '相同任务类型', children: [
            { id: `app-${suffix}`, kind: 'app', label: 'AppA', reference_example: '示例', use_resource_prior: true },
            { id: `other-app-${suffix}`, kind: 'app', label: 'AppB', use_resource_prior: false },
          ],
        }],
      }],
    }
  }
  return { version: 'snapshot-version', scenes: [scene('a'), scene('b')], leaf_count: 2, execution_unit_count: 4, warnings: [] }
}

function seed(overrides: Partial<AugmentationSeed> = {}): AugmentationSeed {
  return {
    seed_id: 'seed-a', source_row: 2, task: '失败用例', app: 'AppA', scene: '相同场景名称', capability: '相同能力名称', sub_capability: '相同任务类型',
    classification_source: 'excel', classification_status: 'classified', mapping_status: 'matched',
    node_path_ids: ['scene-a', 'cap-a', 'task-a', 'app-a'], generation_status: 'waiting', result_count: 0,
    ...overrides,
  }
}

describe('augmentation scene associations', () => {
  it('associates all four levels by stable IDs without illuminating namesakes', () => {
    const tree = fixture()
    const before = JSON.stringify(tree)
    const model = buildAugmentationSceneTree(tree, [seed()])
    for (const id of ['scene-a', 'cap-a', 'task-a', 'app-a']) expect(model.associations.get(id)?.seedIds).toEqual(['seed-a'])
    for (const id of ['scene-b', 'cap-b', 'task-b', 'app-b', 'other-app-a']) expect(model.associations.has(id)).toBe(false)
    expect(model.matchedPaths.get('seed-a')).toEqual(seed().node_path_ids)
    expect(JSON.stringify(tree)).toBe(before)
  })

  it('counts each seed once and keeps simultaneous aggregate generation states', () => {
    const model = buildAugmentationSceneTree(fixture(), [
      seed({ generation_status: 'generating' }),
      seed({ generation_status: 'succeeded' }),
      seed({ seed_id: 'waiting' }),
      seed({ seed_id: 'running', generation_status: 'generating' }),
      seed({ seed_id: 'partial', generation_status: 'partial' }),
      seed({ seed_id: 'failed', generation_status: 'failed' }),
      seed({ seed_id: 'skipped', generation_status: 'skipped' }),
    ])
    expect(model.totalSeeds).toBe(6)
    expect(model.associations.get('scene-a')).toEqual({
      seedIds: ['seed-a', 'waiting', 'running', 'partial', 'failed', 'skipped'],
      states: { waiting: 1, generating: 1, completed: 1, exception: 3 },
    })
    expect(augmentationNodeState('partial')).toBe('exception')
  })

  it('reuses the immutable snapshot index when seed statuses change', () => {
    const index = indexAugmentationSceneTree(fixture())
    const first = associateAugmentationSeeds(index, [seed()])
    const second = associateAugmentationSeeds(index, [seed({ generation_status: 'succeeded' })])
    expect(second.scenes).toBe(first.scenes)
    expect(second.paths).toBe(first.paths)
    expect(second.nodes).toBe(first.nodes)
    expect(first.associations.get('app-a')?.states.waiting).toBe(1)
    expect(second.associations.get('app-a')?.states.completed).toBe(1)
  })

  it('rejects partial, crossed, missing and unresolved paths even when labels look correct', () => {
    const seeds = [
      seed({ seed_id: 'partial', node_path_ids: ['scene-a', 'cap-a', 'task-a'] }),
      seed({ seed_id: 'crossed', node_path_ids: ['scene-b', 'cap-a', 'task-a', 'app-a'] }),
      seed({ seed_id: 'missing', node_path_ids: ['scene-a', 'cap-a', 'task-a', 'missing-app'] }),
      ...(['pending', 'unclassified', 'not_found', 'classification_failed'] as const).map(status => seed({ seed_id: status, mapping_status: status })),
    ]
    const model = buildAugmentationSceneTree(fixture(), seeds)
    expect(model.totalSeeds).toBe(7)
    expect(model.matchedPaths.size).toBe(0)
    expect(model.associations.size).toBe(0)
  })

  it('accepts a legacy App projection through the existing normalization utility', () => {
    const tree = fixture()
    const task = tree.scenes[0]!.children![0]!.children![0]!
    task.children = undefined
    task.app_configs = [{ id: 'app-a', app: 'AppA', reference_example: '保留示例', use_resource_prior: true }]
    const model = buildAugmentationSceneTree(tree, [seed()])
    expect(model.matchedPaths.size).toBe(1)
    expect(model.scenes[0]!.children![0]!.children![0]!.children![0]).toMatchObject({
      id: 'app-a', kind: 'app', reference_example: '保留示例', use_resource_prior: true,
    })
  })

  it('expands only associated ancestors initially and preserves manual collapse across polls', () => {
    const model = buildAugmentationSceneTree(fixture(), [seed()])
    const initial = expandNewAugmentationSeeds(model, { expanded: new Set(), seenMatchedSeeds: new Set() })
    expect([...initial.expanded]).toEqual(['scene-a', 'cap-a', 'task-a'])
    initial.expanded.delete('cap-a')
    const polled = buildAugmentationSceneTree(fixture(), [seed({ generation_status: 'succeeded', result_count: 10 })])
    const next = expandNewAugmentationSeeds(polled, initial)
    expect([...next.expanded]).toEqual(['scene-a', 'task-a'])
    expect(next.seenMatchedSeeds.size).toBe(1)
  })

  it('opens a newly matched case after classification and opens new cases once', () => {
    const pending = buildAugmentationSceneTree(fixture(), [seed({ mapping_status: 'pending', node_path_ids: [] })])
    const initial = expandNewAugmentationSeeds(pending, { expanded: new Set(), seenMatchedSeeds: new Set() })
    expect(initial.expanded.size).toBe(0)
    const matched = buildAugmentationSceneTree(fixture(), [seed()])
    const next = expandNewAugmentationSeeds(matched, initial)
    expect(next.expanded.has('task-a')).toBe(true)
    next.expanded.clear()
    const newCase = buildAugmentationSceneTree(fixture(), [seed(), seed({ seed_id: 'second' })])
    expect([...expandNewAugmentationSeeds(newCase, next).expanded]).toEqual(['scene-a', 'cap-a', 'task-a'])
  })

  it('focus explicitly reveals the matched ancestors without changing other expansion choices', () => {
    const model = buildAugmentationSceneTree(fixture(), [seed()])
    const expanded = new Set(['scene-b'])
    expect([...expandFocusedAugmentationSeed(model, expanded, 'seed-a')]).toEqual(['scene-b', 'scene-a', 'cap-a', 'task-a'])
    expect([...expanded]).toEqual(['scene-b'])
    expect([...expandFocusedAugmentationSeed(model, expanded, 'unknown')]).toEqual(['scene-b'])
  })

  it('prunes unrelated descendants at every level without changing the snapshot or counts', () => {
    const tree = fixture()
    tree.scenes[0]!.children!.push({ id: 'unrelated-cap', kind: 'capability', label: '相同能力名称', children: [] })
    tree.scenes[0]!.children![0]!.children!.push({ id: 'unrelated-task', kind: 'sub_capability', label: '相同任务类型', children: [] })
    const model = buildAugmentationSceneTree(tree, [seed(), seed({ seed_id: 'second' })])
    const originalTree = JSON.stringify(tree)
    const originalScenes = JSON.stringify(model.scenes)
    const visible = visibleAugmentationScenes(model, 'related')
    expect(visible.map(node => node.id)).toEqual(['scene-a'])
    expect(visible[0]!.children!.map(node => node.id)).toEqual(['cap-a'])
    expect(visible[0]!.children![0]!.children!.map(node => node.id)).toEqual(['task-a'])
    expect(visible[0]!.children![0]!.children![0]!.children!.map(node => node.id)).toEqual(['app-a'])
    expect(model.associations.get('scene-a')?.seedIds).toEqual(['seed-a', 'second'])
    expect(model.nodes.has('other-app-a')).toBe(true)
    expect(visibleAugmentationScenes(model, 'all')).toBe(model.scenes)
    expect(JSON.stringify(model.scenes)).toBe(originalScenes)
    expect(JSON.stringify(tree)).toBe(originalTree)
  })

  it('shows no guessed path for incomplete associations while retaining the full snapshot option', async () => {
    const seeds = [seed({ mapping_status: 'classification_failed' }), seed({ seed_id: 'partial', node_path_ids: ['scene-a', 'cap-a'] })]
    const model = buildAugmentationSceneTree(fixture(), seeds)
    expect(visibleAugmentationScenes(model, 'related')).toEqual([])
    expect(visibleAugmentationScenes(model, 'all')).toHaveLength(2)
    const html = await renderToString(createSSRApp(AugmentationSceneTree, { tree: fixture(), seeds }))
    expect(html).toContain('暂无完整匹配的关联场景')
    expect(html).toContain('全部场景')
    expect(html).not.toContain('data-node-id=')
  })

  it('renders only related paths by default with independent selection and state badges', async () => {
    const html = await renderToString(createSSRApp(AugmentationSceneTree, {
      tree: fixture(), seeds: [seed()], selectedNodeId: 'scene-a', focusedSeedId: 'seed-a',
    }))
    expect(html).toContain('data-node-id="scene-a"')
    expect(html).not.toContain('data-node-id="scene-b"')
    expect(html).toContain('data-node-id="app-a"')
    expect(html).not.toContain('data-node-id="cap-b"')
    expect(html).not.toContain('data-node-id="app-b"')
    expect(html).not.toContain('data-node-id="other-app-a"')
    expect(html).toContain('level-scene related selected focused')
    expect(html).toContain('level-app related focused')
    expect(html).toContain('level-capability related focused')
    expect(html).toContain('level-sub_capability related focused')
    expect(html).toContain('related focused association-children')
    expect(html).toContain('aria-pressed="true"')
    expect(html).toContain('aria-expanded="true"')
    expect(html).toContain('等待 1')
    expect(html).toMatch(/<button[^>]*aria-pressed="true"[^>]*>仅关联场景<\/button>/)
    expect(html).toMatch(/<button[^>]*aria-pressed="false"[^>]*>全部场景<\/button>/)
  })
})
