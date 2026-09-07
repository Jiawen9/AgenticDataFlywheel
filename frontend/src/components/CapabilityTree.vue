<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { TaskGenerationTreeNode } from '@/types'
import type { CapabilityTreePosition } from '@/utils/scenarioOverview'
import { capabilityTreeGeometry, scenarioDomains } from '@/utils/scenarioOverview'
import CapabilityTreeNode from '@/components/CapabilityTreeNode.vue'

const props = defineProps<{ tree: TaskGenerationTreeNode[] }>()
const emit = defineEmits<{
  (event: 'hover-l1', nodeId: string): void
  (event: 'select-l1', nodeId: string): void
}>()
const canvas = ref<HTMLDivElement | null>(null)
const width = ref(1000)
const hoveredL1Id = ref('')
const domains = computed(() => scenarioDomains(props.tree))
const geometry = computed(() => capabilityTreeGeometry(domains.value, width.value, hoveredL1Id.value))
let observer: ResizeObserver | undefined

// Expanding content can replace the element under the pointer before a leave event.
// Reconcile against the next actual mouse position so a preview cannot get stuck.
function reconcilePointer(event: PointerEvent) {
  if (event.pointerType !== 'mouse' || !hoveredL1Id.value) return
  const target = event.target instanceof Element ? event.target.closest('[data-scene-node]') : null
  if (!target || !canvas.value?.contains(target) || target.getAttribute('data-scene-node') !== hoveredL1Id.value) updateHover('')
}
function resetPreview() { updateHover('') }
onMounted(() => {
  if (!canvas.value) return
  width.value = canvas.value.clientWidth
  observer = new ResizeObserver(entries => {
    const entry = entries[0]
    if (entry) width.value = entry.contentRect.width
  })
  observer.observe(canvas.value)
  window.addEventListener('pointermove', reconcilePointer, { passive: true })
  window.addEventListener('blur', resetPreview)
})
onBeforeUnmount(() => {
  observer?.disconnect()
  window.removeEventListener('pointermove', reconcilePointer)
  window.removeEventListener('blur', resetPreview)
})

function updateHover(nodeId: string) {
  hoveredL1Id.value = nodeId
  emit('hover-l1', nodeId)
}
function clearHover(nodeId: string) {
  if (hoveredL1Id.value === nodeId) updateHover('')
}
function selectNode(nodeId: string) {
  updateHover('')
  emit('select-l1', nodeId)
}
function branchPath(position: CapabilityTreePosition) {
  const { center, compact, nodeWidth } = geometry.value
  if (compact) return `M ${center.x} ${center.y + 30} H 12 V ${position.y - 8} Q 12 ${position.y}, 20 ${position.y} H ${position.x - nodeWidth / 2}`
  const left = position.x < center.x
  const start = center.x + (left ? -72 : 72)
  const cardWidth = nodeWidth + (hoveredL1Id.value === position.id ? 24 : 0)
  const end = position.x + (left ? cardWidth / 2 : -cardWidth / 2)
  const trunk = (start + end) / 2
  const direction = left ? -1 : 1
  const vertical = Math.sign(position.y - center.y)
  const radius = Math.min(18, Math.abs(end - trunk) / 2, Math.abs(position.y - center.y) / 2)
  return `M ${start} ${center.y} H ${trunk} V ${position.y - vertical * radius} Q ${trunk} ${position.y}, ${trunk + direction * radius} ${position.y} H ${end}`
}
</script>

<template>
  <div ref="canvas" class="capability-tree" :class="{ 'is-compact': geometry.compact }" :style="{ height: geometry.height + 'px' }" role="group" aria-label="GUI Agent 场景能力树" @pointerleave="updateHover('')">
    <svg class="tree-connections" :viewBox="`0 0 ${geometry.width} ${geometry.height}`" aria-hidden="true">
      <path v-for="position in geometry.positions" :key="position.id" class="tree-branch" :class="{ 'is-active': hoveredL1Id === position.id, 'is-dimmed': Boolean(hoveredL1Id) && hoveredL1Id !== position.id }" :d="branchPath(position)" />
      <g class="tree-root" :transform="`translate(${geometry.center.x} ${geometry.center.y})`">
        <rect x="-72" :y="geometry.compact ? -30 : -50" width="144" :height="geometry.compact ? 60 : 100" rx="5" />
        <rect v-if="!geometry.compact" class="root-inset" x="-67" y="-45" width="134" height="90" rx="3" />
        <g v-if="!geometry.compact" class="root-symbol" transform="translate(-13 -27)">
          <path d="M13 7V12M3 12H23M3 12V17M23 12V17" /><rect x="9" y="0" width="8" height="7" rx="2" /><rect x="-1" y="17" width="8" height="7" rx="2" /><rect x="19" y="17" width="8" height="7" rx="2" />
        </g>
        <text class="root-label" x="0" :y="geometry.compact ? -2 : 17">GUI Agent</text>
        <text class="root-caption" x="0" :y="geometry.compact ? 16 : 33">场景能力体系</text>
      </g>
    </svg>
    <CapabilityTreeNode v-for="(domain, index) in domains" :key="domain.scene.id" :node="domain.scene" :domain="domain" :ordinal="index + 1" :position="geometry.positions[index]!" :node-width="geometry.nodeWidth" :canvas-width="geometry.width" :canvas-height="geometry.height" :compact="geometry.compact" :active="hoveredL1Id === domain.scene.id" :dimmed="Boolean(hoveredL1Id) && hoveredL1Id !== domain.scene.id" @hover="updateHover" @leave="clearHover" @select="selectNode" />
  </div>
</template>

<style scoped>
.capability-tree { position: relative; width: 100%; min-width: 0; margin-top: 2px; isolation: isolate; }
.tree-connections { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
.tree-branch { fill: none; stroke: #b6bcb0; stroke-width: 1.2; stroke-linecap: round; transition: stroke .2s ease, opacity .2s ease; }
.tree-branch.is-active { stroke: #3d6555; stroke-width: 1.7; }
.tree-branch.is-dimmed { opacity: .72; }
.tree-root>rect { fill: #f8f8f1; stroke: #9eaf9f; stroke-width: 1; }
.tree-root>.root-inset { fill: none; stroke: #d5ddcf; stroke-width: .8; }
.root-label { fill: #304d3d; text-anchor: middle; font: 500 18px var(--atlas-serif); }
.root-caption { fill: #65715f; text-anchor: middle; font-size: 11px; }
.root-symbol { fill: #fcfbf8; stroke: #597b62; stroke-width: 1.2; }
.root-symbol path { fill: none; }
@media (prefers-reduced-motion: reduce) { .tree-branch { transition: none; } }
</style>
