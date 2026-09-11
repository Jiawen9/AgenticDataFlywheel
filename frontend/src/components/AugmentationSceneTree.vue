<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import type { AugmentationSeed, TaskGenerationTree } from '@/types'
import AugmentationSceneTreeBranch from './AugmentationSceneTreeBranch.vue'
import { associateAugmentationSeeds, indexAugmentationSceneTree, expandFocusedAugmentationSeed, expandNewAugmentationSeeds, visibleAugmentationScenes, type AugmentationTreeDisplayMode, type AugmentationTreeExpansion } from '@/utils/augmentationSceneTree'

const props = defineProps<{
  tree: TaskGenerationTree
  seeds: AugmentationSeed[]
  selectedNodeId?: string
  focusedSeedId?: string
  focusRevision?: number
  beforeHideSelection?: () => Promise<boolean>
}>()
const emit = defineEmits<{ select: [nodeId: string] }>()
const viewport = ref<HTMLElement>()
// Preview polling may replace the tree object while retaining the same pinned
// snapshot. The parent keys this component by job, so the version is enough.
const snapshotIndex = shallowRef(indexAugmentationSceneTree(props.tree))
watch(() => props.tree.version, () => { snapshotIndex.value = indexAugmentationSceneTree(props.tree) })
const model = computed(() => associateAugmentationSeeds(snapshotIndex.value, props.seeds))
const displayMode = ref<AugmentationTreeDisplayMode>('related')
const changingMode = ref(false)
const visibleScenes = computed(() => visibleAugmentationScenes(model.value, displayMode.value))
let disposed = false
onBeforeUnmount(() => { disposed = true })
const expansion = ref<AugmentationTreeExpansion>({ expanded: new Set(), seenMatchedSeeds: new Set() })
watch(model, value => { expansion.value = expandNewAugmentationSeeds(value, expansion.value) }, { immediate: true })

const focusedPath = computed(() => props.focusedSeedId ? model.value.matchedPaths.get(props.focusedSeedId) : undefined)
const focusedNodeId = computed(() => focusedPath.value?.at(-1))
const focusedPathIds = computed(() => new Set(focusedPath.value || []))
// Track only the focused identity/path, not the replacement objects from polls.
watch(() => JSON.stringify([props.focusedSeedId, props.focusRevision, focusedPath.value]), async () => {
  expansion.value = { ...expansion.value, expanded: expandFocusedAugmentationSeed(model.value, expansion.value.expanded, props.focusedSeedId) }
  const targetId = focusedNodeId.value
  if (!targetId) return
  await nextTick()
  if (targetId !== focusedNodeId.value) return
  const target = [...(viewport.value?.querySelectorAll<HTMLButtonElement>('[data-node-id]') || [])]
    .find(element => element.dataset.nodeId === targetId)
  target?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
}, { immediate: true, flush: 'post' })

function toggle(nodeId: string) {
  const expanded = new Set(expansion.value.expanded)
  if (expanded.has(nodeId)) expanded.delete(nodeId)
  else expanded.add(nodeId)
  expansion.value = { ...expansion.value, expanded }
}

async function changeDisplayMode(mode: AugmentationTreeDisplayMode) {
  if (changingMode.value || mode === displayMode.value) return
  changingMode.value = true
  try {
    if (mode === 'related' && props.selectedNodeId && !model.value.associations.has(props.selectedNodeId)) {
      // The parent protects unsaved edits and clears the hidden node's filter.
      if (!props.beforeHideSelection || !(await props.beforeHideSelection())) return
    }
    if (!disposed) displayMode.value = mode
  } finally {
    if (!disposed) changingMode.value = false
  }
}
</script>

<template>
  <section class="augmentation-scene-tree" aria-label="失败用例关联场景树">
    <header class="association-tree-header">
      <span>已关联 <strong>{{ model.matchedPaths.size }}</strong> / {{ model.totalSeeds }} 个失败用例</span>
      <div class="association-display-mode" role="group" aria-label="场景显示范围">
        <button type="button" :aria-pressed="displayMode === 'related'" :disabled="changingMode" @click="changeDisplayMode('related')">仅关联场景</button>
        <button type="button" :aria-pressed="displayMode === 'all'" :disabled="changingMode" @click="changeDisplayMode('all')">全部场景</button>
      </div>
      <span class="association-legend"><i aria-hidden="true" />关联分支 <i class="selected-marker" aria-hidden="true" />当前选择</span>
    </header>
    <div ref="viewport" class="association-tree-viewport" tabindex="0" aria-label="场景树，可横向滚动">
      <ul v-if="visibleScenes.length" class="association-tree-roots" aria-label="一级场景">
        <AugmentationSceneTreeBranch
          v-for="scene in visibleScenes"
          :key="scene.id"
          :node="scene"
          :associations="model.associations"
          :expanded="expansion.expanded"
          :selected-node-id="props.selectedNodeId"
          :focused-path-ids="focusedPathIds"
          @select="emit('select', $event)"
          @toggle="toggle"
        />
      </ul>
      <p v-else class="association-tree-empty">{{ model.scenes.length ? '暂无完整匹配的关联场景，可切换“全部场景”查看快照。' : '该作业的场景树暂无节点。' }}</p>
    </div>
    <p class="association-tree-hint">点击节点筛选关联用例，使用 + / − 展开或折叠分支。{{ model.matchedPaths.size ? '' : '完整匹配后会自动点亮关联路径。' }}</p>
  </section>
</template>

<style scoped>
.augmentation-scene-tree { min-width: 0; overflow: hidden; border: 1px solid var(--line, #dbe3ed); border-radius: 12px; background: #fbfdfd; }
.association-tree-header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; padding: 10px 12px; border-bottom: 1px solid var(--line, #dbe3ed); color: #64748b; font-size: 12px; }
.association-tree-header strong { color: #0f766e; }
.association-display-mode { display: inline-flex; padding: 2px; border: 1px solid var(--line, #dbe3ed); border-radius: 7px; background: #f1f5f9; }
.association-display-mode button { padding: 5px 8px; border: 0; border-radius: 5px; color: #64748b; background: transparent; font: inherit; font-size: 11px; cursor: pointer; }
.association-display-mode button[aria-pressed="true"] { color: var(--accent-deep, #0f766e); background: #fff; box-shadow: 0 1px 3px #0f172a10; }
.association-display-mode button:focus-visible { outline: 2px solid var(--accent, #14b8a6); outline-offset: 1px; }
.association-display-mode button:disabled { cursor: wait; opacity: .65; }
.association-legend { display: inline-flex; align-items: center; gap: 7px; color: #64748b; font-size: 11px; }
.association-legend i { width: 10px; height: 10px; border: 1px solid #8bd8ce; border-radius: 3px; background: #f0fdfa; }
.association-legend .selected-marker { margin-left: 6px; border: 2px solid #0f766e; box-shadow: 0 0 0 2px rgba(20,184,166,.15); }
.association-tree-viewport { max-height: 300px; overflow: auto; overscroll-behavior: contain; scrollbar-width: thin; }
.association-tree-viewport:focus-visible { outline: 2px solid var(--accent, #14b8a6); outline-offset: -2px; }
.association-tree-roots { display: grid; gap: 12px; width: max-content; min-width: calc(100% - 28px); margin: 0; padding: 14px; list-style: none; }
.association-tree-hint { margin: 0; padding: 8px 12px; border-top: 1px solid var(--line, #dbe3ed); color: #64748b; font-size: 11px; line-height: 1.7; }
.association-tree-empty { margin: 0; padding: 24px 16px; color: #64748b; font-size: 12px; text-align: center; line-height: 1.7; }
@media (max-width: 780px) { .association-tree-header { align-items: flex-start; }.association-legend { width: 100%; } }
</style>
