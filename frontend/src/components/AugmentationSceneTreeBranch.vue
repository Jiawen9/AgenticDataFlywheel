<script setup lang="ts">
import { computed } from 'vue'
import type { TaskGenerationTreeNode } from '@/types'
import { NODE_LEVELS } from '@/utils/scenarioTree'
import { AUGMENTATION_STATE_LABELS, type AugmentationNodeAssociation, type AugmentationNodeState } from '@/utils/augmentationSceneTree'

const props = defineProps<{
  node: TaskGenerationTreeNode
  associations: Map<string, AugmentationNodeAssociation>
  expanded: Set<string>
  selectedNodeId?: string
  focusedPathIds: Set<string>
}>()
const emit = defineEmits<{ select: [nodeId: string]; toggle: [nodeId: string] }>()
const association = computed(() => props.associations.get(props.node.id))
const hasChildren = computed(() => Boolean(props.node.children?.length))
const badges = computed(() => (Object.keys(AUGMENTATION_STATE_LABELS) as AugmentationNodeState[])
  .filter(state => association.value?.states[state])
  .map(state => ({ state, label: AUGMENTATION_STATE_LABELS[state], count: association.value!.states[state] })))
</script>

<template>
  <li class="augmentation-branch" :class="{ related: Boolean(association), focused: props.focusedPathIds.has(props.node.id), 'has-children': hasChildren && props.expanded.has(props.node.id) }">
    <div
      class="association-card"
      :class="[
        `level-${props.node.kind}`,
        { related: Boolean(association), selected: props.selectedNodeId === props.node.id, focused: props.focusedPathIds.has(props.node.id) },
      ]"
    >
      <button
        type="button"
        class="association-select"
        :data-node-id="props.node.id"
        :aria-pressed="props.selectedNodeId === props.node.id"
        @click="emit('select', props.node.id)"
      >
        <span class="association-heading"><span class="level-tag">{{ NODE_LEVELS[props.node.kind] }}</span><strong>{{ props.node.label }}</strong></span>
        <span class="association-count">{{ association?.seedIds.length || 0 }} 个关联用例</span>
        <span v-if="badges.length" class="association-badges">
          <span v-for="badge in badges" :key="badge.state" class="association-badge" :class="`state-${badge.state}`">{{ badge.label }} {{ badge.count }}</span>
        </span>
      </button>
      <button
        v-if="hasChildren"
        type="button"
        class="association-toggle"
        :aria-label="`${props.expanded.has(props.node.id) ? '折叠' : '展开'}${props.node.label}`"
        :aria-expanded="props.expanded.has(props.node.id)"
        @click.stop="emit('toggle', props.node.id)"
      ><span aria-hidden="true">{{ props.expanded.has(props.node.id) ? '−' : '+' }}</span></button>
    </div>
    <ul v-if="hasChildren && props.expanded.has(props.node.id)" class="association-children" :class="{ related: Boolean(association), focused: props.focusedPathIds.has(props.node.id) }" :aria-label="`${props.node.label}的子节点`">
      <AugmentationSceneTreeBranch
        v-for="child in props.node.children"
        :key="child.id"
        :node="child"
        :associations="props.associations"
        :expanded="props.expanded"
        :selected-node-id="props.selectedNodeId"
        :focused-path-ids="props.focusedPathIds"
        @select="emit('select', $event)"
        @toggle="emit('toggle', $event)"
      />
    </ul>
  </li>
</template>

<style scoped>
.augmentation-branch { position: relative; display: flex; align-items: flex-start; gap: 12px; min-width: max-content; list-style: none; }
.association-card { position: relative; flex: 0 0 160px; width: 160px; border: 1px solid #e2e8f0; border-radius: 11px; background: #f8fafc; color: #64748b; transition: border-color .16s, box-shadow .16s; }
.association-card.related { border-color: #8bd8ce; background: #f8fffd; color: #1e293b; }
.association-card.selected { border-color: var(--accent-deep, #0f766e); box-shadow: 0 0 0 3px rgba(20, 184, 166, .22); }
.association-card.focused { outline: 2px dashed #60a5fa; outline-offset: 3px; }
.association-select { display: grid; gap: 5px; width: 100%; min-height: 64px; padding: 9px 32px 9px 10px; border: 0; border-radius: inherit; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.association-select:hover { background: rgba(255,255,255,.65); }
.association-select:focus-visible, .association-toggle:focus-visible { outline: 2px solid var(--accent-deep, #0f766e); outline-offset: 3px; }
.association-heading { display: flex; align-items: baseline; gap: 6px; min-width: 0; }
.association-heading strong { font-size: 13px; line-height: 1.5; overflow-wrap: anywhere; }
.level-scene .association-heading strong { font-size: 14px; }
.level-app .association-heading strong { font-size: 12px; }
.level-tag { flex-shrink: 0; color: #94a3b8; font-size: 10px; font-weight: 800; letter-spacing: .05em; }
.related .level-tag { color: #0f766e; }
.association-count { color: #94a3b8; font-size: 11px; }
.related .association-count { color: #64748b; }
.association-toggle { position: absolute; top: 8px; right: 6px; display: grid; place-items: center; width: 23px; height: 23px; padding: 0; border: 1px solid #dbe3ed; border-radius: 6px; background: #fff; color: #64748b; font-size: 16px; cursor: pointer; }
.association-toggle:hover { border-color: #8bd8ce; color: #0f766e; }
.association-badges { display: flex; flex-wrap: wrap; gap: 4px; }
.association-badge { padding: 2px 5px; border-radius: 4px; font-size: 10px; font-weight: 500; line-height: 1.5; white-space: nowrap; }
.state-waiting { color: #64748b; background: #eaf0f5; }
.state-generating { color: #1d4ed8; background: #dbeafe; }
.state-completed { color: #0f766e; background: #ccfbf1; }
.state-exception { color: #b45309; background: #fef3c7; }
.association-children { display: grid; gap: 10px; margin: 0; padding: 0 0 0 12px; border-left: 1px solid #dbe3ed; }
.association-children.related { border-left-color: #8bd8ce; }
.has-children > .association-card::after { content: ''; position: absolute; top: 30px; left: 100%; width: 13px; border-top: 1px solid #dbe3ed; }
.has-children > .association-card.related::after { border-color: #8bd8ce; }
.association-children > .augmentation-branch::before { content: ''; position: absolute; top: 30px; right: 100%; width: 13px; border-top: 1px solid #dbe3ed; }
.association-children > .augmentation-branch.related::before { border-color: #8bd8ce; }
.association-children.focused, .has-children > .association-card.focused::after, .association-children > .augmentation-branch.focused::before { border-color: #60a5fa; }
@media (prefers-reduced-motion: reduce) { .association-card { transition: none; } }
</style>
