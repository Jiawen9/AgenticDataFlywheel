<script setup lang="ts">
import { computed } from 'vue'
import { ChatDotRound, Connection, Headset, Location, Reading, Share, ShoppingCart, Ticket, Tools, VideoCamera, VideoPlay, Monitor } from '@element-plus/icons-vue'
import type { TaskGenerationTreeNode } from '@/types'
import type { CapabilityTreePosition, ScenarioDomainSummary } from '@/utils/scenarioOverview'
import { capabilityNodeHeight, scenarioIcon, scenarioSpotlight } from '@/utils/scenarioOverview'

const props = defineProps<{
  node: TaskGenerationTreeNode
  ordinal: number
  domain: ScenarioDomainSummary
  position: CapabilityTreePosition
  nodeWidth: number
  canvasWidth: number
  canvasHeight: number
  compact?: boolean
  active?: boolean
  dimmed?: boolean
}>()
const emit = defineEmits<{
  (event: 'hover', nodeId: string): void
  (event: 'leave', nodeId: string): void
  (event: 'select', nodeId: string): void
}>()
const preview = computed(() => scenarioSpotlight(props.domain, 3))
const icons = { video: VideoPlay, shortVideo: VideoCamera, music: Headset, shopping: ShoppingCart, tools: Tools, game: Monitor, network: Connection, social: ChatDotRound, reading: Reading, travel: Location, ticket: Ticket, tree: Share }
const sceneIcon = computed(() => icons[scenarioIcon(props.node.label)])
const cardWidth = computed(() => props.compact ? props.nodeWidth : props.nodeWidth + (props.active ? 24 : 0))
const cardHeight = computed(() => capabilityNodeHeight(props.domain, props.active))
const placement = computed(() => {
  const halfWidth = cardWidth.value / 2
  const halfHeight = cardHeight.value / 2
  return {
    left: Math.max(halfWidth + 4, Math.min(props.position.x, props.canvasWidth - halfWidth - 4)) + 'px',
    top: Math.max(halfHeight + 4, Math.min(props.position.y, props.canvasHeight - halfHeight - 4)) + 'px',
    width: cardWidth.value + 'px',
    height: cardHeight.value + 'px',
  }
})
function pointerEnter(event: PointerEvent) {
  if (event.pointerType === 'mouse') emit('hover', props.node.id)
}
function pointerLeave(event: PointerEvent) {
  if (event.pointerType === 'mouse') emit('leave', props.node.id)
}
function keyboardFocus(event: FocusEvent) {
  if ((event.currentTarget as HTMLElement).matches(':focus-visible')) emit('hover', props.node.id)
}
</script>

<template>
  <button type="button" class="capability-tree-node" :data-scene-node="props.node.id" :class="{ 'is-active': props.active, 'is-dimmed': props.dimmed, 'is-compact': props.compact, 'is-right': !props.compact && props.position.x > props.canvasWidth / 2 }" :style="placement" :aria-label="`${props.node.label}，${props.domain.capabilityCount} 个能力，进入编辑工作台`" :aria-expanded="Boolean(props.active)" @pointerenter="pointerEnter" @pointerleave="pointerLeave" @focus="keyboardFocus" @blur="emit('leave', props.node.id)" @click="emit('select', props.node.id)">
    <svg class="node-outline" :viewBox="`0 0 ${cardWidth} ${cardHeight}`" aria-hidden="true">
      <rect class="tree-node-base" x="1" y="1" :width="cardWidth - 2" :height="cardHeight - 2" rx="6" />
    </svg>
      <span class="node-ordinal" aria-hidden="true">{{ String(props.ordinal).padStart(2, '0') }}</span>
      <span class="node-content">
        <span class="node-heading"><el-icon class="node-icon" aria-hidden="true"><component :is="sceneIcon" /></el-icon><span class="node-title" :title="props.node.label">{{ props.node.label }}</span><span class="node-arrow" aria-hidden="true">↗</span></span>
        <template v-if="props.active && preview">
          <span class="node-detail">
            <span class="detail-label">代表性能力</span>
            <span v-if="preview.visibleCapabilities.length" class="detail-list">
              <span v-for="capability in preview.visibleCapabilities" :key="capability.id" class="detail-item" :title="capability.label">{{ capability.label }}</span>
            </span>
            <span v-else class="detail-empty">暂无能力</span>
            <span v-if="preview.remainingCapabilityCount" class="detail-more">+ {{ preview.remainingCapabilityCount }} 个能力</span>
          </span>
          <span class="node-meta">{{ preview.capabilityCount }} 个能力 · {{ preview.subCapabilityCount }} 个子能力</span>
        </template>
        <span v-else class="node-count">{{ props.domain.capabilityCount }} 个能力</span>
      </span>
  </button>
</template>

<style scoped>
.capability-tree-node { position: absolute; z-index: 1; padding: 13px 15px; border: 0; background: transparent; overflow: visible; transform: translate(-50%, -50%); outline: none; cursor: pointer; transition: top .2s ease, width .2s ease, height .2s ease, opacity .2s ease; }
.node-outline { position: absolute; inset: 0; z-index: -1; width: 100%; height: 100%; overflow: visible; pointer-events: none; }
.capability-tree-node.is-active { z-index: 3; }
.capability-tree-node.is-dimmed { opacity: .85; }
.tree-node-base { fill: #fcfbf8; stroke: #babfb3; stroke-width: 1; transition: fill .2s ease, stroke .2s ease; }
.is-active .tree-node-base, .capability-tree-node:focus-visible .tree-node-base { fill: #f2f5ed; stroke: #496c55; stroke-width: 1.3; }
.capability-tree-node:focus-visible { outline: 2px solid #496c55; outline-offset: 3px; border-radius: 6px; }
.node-ordinal { position: absolute; top: 50%; left: -28px; transform: translateY(-50%); color: #899080; font-size: 10px; font-variant-numeric: tabular-nums; letter-spacing: .05em; }
.is-right .node-ordinal { left: auto; right: -28px; }
.capability-tree-node.is-compact { transition: opacity .2s ease; }
.is-compact .node-ordinal { top: 6px; left: auto; right: 8px; transform: none; font-size: 8px; }
.node-content { display: flex; flex-direction: column; gap: 5px; height: 100%; font-family: inherit; color: var(--atlas-ink, #2d3832); text-align: left; }
.node-heading { display: flex; align-items: center; gap: 10px; }
.node-icon { flex: none; font-size: 22px; color: #62735c; }
.node-title { display: -webkit-box; flex: 1; min-width: 0; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; font-family: var(--atlas-serif); font-size: 18px; font-weight: 500; line-height: 21px; overflow-wrap: anywhere; }
.node-arrow { flex: none; color: #7a8876; font-size: 14px; }
.node-count { padding-left: 32px; color: #747e6e; font-size: 11px; line-height: 14px; }
.node-detail { display: grid; gap: 5px; padding-top: 8px; margin-top: 3px; border-top: 1px solid #d8dfd0; }
.detail-label { color: #6b7d63; font-size: 10px; line-height: 13px; }
.detail-list { display: grid; gap: 4px; }
.detail-item { overflow: hidden; color: #4b5d44; font-size: 12px; line-height: 16px; text-overflow: ellipsis; white-space: nowrap; }
.detail-item::before { display: inline-block; width: 3px; height: 3px; margin: 0 7px 3px 0; border-radius: 50%; background: #94a589; content: ''; }
.detail-empty, .detail-more { color: #707d66; font-size: 11px; line-height: 15px; }
.node-meta { margin-top: auto; padding-top: 6px; border-top: 1px solid #d8dfd0; color: #68795f; font-size: 11px; line-height: 15px; }
.is-active .node-content { animation: node-reveal .2s ease both; }
@keyframes node-reveal { from { opacity: .55; transform: translateY(3px); } to { opacity: 1; transform: translateY(0); } }
@media (prefers-reduced-motion: reduce) {
  .capability-tree-node, .tree-node-base { transition: none; }
  .is-active .node-content { animation: none; }
}
</style>
