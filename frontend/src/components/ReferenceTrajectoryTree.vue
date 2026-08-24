<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { TrajectoryTreeNode } from '@/types'

const props = defineProps<{
  root: TrajectoryTreeNode
  selectedId?: number
}>()

const emit = defineEmits<{
  select: [node: TrajectoryTreeNode]
}>()

interface LayoutNode {
  node: TrajectoryTreeNode
  x: number
  y: number
  actionClass: 'root' | 'click' | 'swipe' | 'other'
}

interface LayoutEdge {
  id: string
  path: string
  branch: boolean
}

const svg = ref<SVGSVGElement | null>(null)
const viewport = ref({ x: 0, y: 0, scale: 1 })
const dragging = ref(false)
let lastPointer = { x: 0, y: 0 }

const layout = computed(() => {
  const nodes: LayoutNode[] = []
  const edges: LayoutEdge[] = []
  const positions = new Map<number, { x: number; y: number }>()
  let leaf = 0

  const walk = (node: TrajectoryTreeNode, depth = 0): number => {
    const childYs = node.children.map((child) => walk(child, depth + 1))
    const y = childYs.length
      ? childYs.reduce((sum, value) => sum + value, 0) / childYs.length
      : leaf++ * 86 + 60
    positions.set(node.id, { x: depth * 210 + 70, y })
    return y
  }
  walk(props.root)

  const collect = (node: TrajectoryTreeNode) => {
    const point = positions.get(node.id)!
    const label = node.label.toLowerCase()
    nodes.push({
      node,
      ...point,
      actionClass: node.id === 0
        ? 'root'
        : label === 'click'
          ? 'click'
          : label.includes('swipe')
            ? 'swipe'
            : 'other',
    })
    for (const child of node.children) {
      const childPoint = positions.get(child.id)!
      edges.push({
        id: `${node.id}-${child.id}`,
        path: `M ${point.x + 18},${point.y} C ${point.x + 95},${point.y} ${childPoint.x - 95},${childPoint.y} ${childPoint.x - 18},${childPoint.y}`,
        branch: node.children.length > 1,
      })
      collect(child)
    }
  }
  collect(props.root)
  return { nodes, edges, leafCount: Math.max(leaf, 1) }
})

const sceneTransform = computed(
  () => `translate(${viewport.value.x} ${viewport.value.y}) scale(${viewport.value.scale})`,
)

function resetView() {
  const root = layout.value.nodes.find((item) => item.node.id === props.root.id)
  const height = svg.value?.clientHeight || 610
  if (!root) return
  viewport.value = { x: 0, y: height / 2 - root.y, scale: 1 }
}

function zoomBy(factor: number, clientX?: number, clientY?: number) {
  const element = svg.value
  if (!element) return
  const rect = element.getBoundingClientRect()
  const anchorX = clientX === undefined ? rect.width / 2 : clientX - rect.left
  const anchorY = clientY === undefined ? rect.height / 2 : clientY - rect.top
  const oldScale = viewport.value.scale
  const nextScale = Math.max(0.15, Math.min(3, oldScale * factor))
  const sceneX = (anchorX - viewport.value.x) / oldScale
  const sceneY = (anchorY - viewport.value.y) / oldScale
  viewport.value = {
    x: anchorX - sceneX * nextScale,
    y: anchorY - sceneY * nextScale,
    scale: nextScale,
  }
}

function onWheel(event: WheelEvent) {
  zoomBy(event.deltaY < 0 ? 1.12 : 0.89, event.clientX, event.clientY)
}

function onPointerDown(event: PointerEvent) {
  if ((event.target as Element).closest('.reference-node')) return
  dragging.value = true
  lastPointer = { x: event.clientX, y: event.clientY }
  svg.value?.setPointerCapture(event.pointerId)
}

function onPointerMove(event: PointerEvent) {
  if (!dragging.value) return
  viewport.value = {
    ...viewport.value,
    x: viewport.value.x + event.clientX - lastPointer.x,
    y: viewport.value.y + event.clientY - lastPointer.y,
  }
  lastPointer = { x: event.clientX, y: event.clientY }
}

function stopDragging(event?: PointerEvent) {
  dragging.value = false
  if (event && svg.value?.hasPointerCapture(event.pointerId)) {
    svg.value.releasePointerCapture(event.pointerId)
  }
}

function selectNode(node: TrajectoryTreeNode) {
  emit('select', node)
}

watch(() => props.root, async () => {
  await nextTick()
  resetView()
})

onMounted(() => {
  void nextTick(resetView)
  window.addEventListener('resize', resetView)
})
onBeforeUnmount(() => window.removeEventListener('resize', resetView))
</script>

<template>
  <div class="reference-tree">
    <svg
      ref="svg"
      class="reference-tree__svg"
      :class="{ 'is-dragging': dragging }"
      @wheel.prevent="onWheel"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="stopDragging"
      @pointercancel="stopDragging"
    >
      <g :transform="sceneTransform">
        <path
          v-for="edge in layout.edges"
          :key="edge.id"
          :d="edge.path"
          class="reference-edge"
          :class="{ 'reference-edge--branch': edge.branch }"
        />
        <g
          v-for="item in layout.nodes"
          :key="item.node.id"
          class="reference-node"
          :class="{ selected: selectedId === item.node.id }"
          :transform="`translate(${item.x}, ${item.y})`"
          role="button"
          tabindex="0"
          @click.stop="selectNode(item.node)"
          @keydown.enter.stop="selectNode(item.node)"
        >
          <circle class="page-ring" r="27" />
          <circle :class="['action-dot', `action-dot--${item.actionClass}`]" r="20" />
          <text class="node-count" text-anchor="middle" y="4">
            {{ item.node.occurrence_count }}
          </text>
          <text class="node-action" x="33" y="-5">{{ item.node.label }}</text>
          <text class="node-summary" x="33" y="12">
            {{ item.node.summary.slice(0, 17) }}{{ item.node.summary.length > 17 ? '…' : '' }}
          </text>
          <circle
            v-if="item.node.terminal_trajectories.length"
            class="terminal"
            cx="20"
            cy="-20"
            r="5"
          />
        </g>
      </g>
    </svg>

    <div class="tree-controls">
      <button type="button" title="放大" @click="zoomBy(1.2)">＋</button>
      <button type="button" title="缩小" @click="zoomBy(0.84)">－</button>
      <button type="button" class="tree-controls__reset" @click="resetView">初始视角</button>
    </div>
    <div class="tree-hint">滚轮缩放 · 拖动画布 · 点击节点查看详情</div>
  </div>
</template>

<style scoped>
.reference-tree { position: relative; width: 100%; height: 100%; overflow: hidden; background-color: #0b1220; background-image: radial-gradient(#24344d 1px, transparent 1px); background-size: 24px 24px; }
.reference-tree__svg { display: block; width: 100%; height: 100%; cursor: grab; touch-action: none; user-select: none; }
.reference-tree__svg.is-dragging { cursor: grabbing; }
.reference-edge { fill: none; stroke: #58708f; stroke-width: 2; }
.reference-edge--branch { stroke: #ef8354; }
.reference-node { cursor: pointer; outline: none; }
.page-ring { fill: #111c30; stroke: #30435e; stroke-width: 4; pointer-events: none; }
.action-dot { stroke: #dce7f5; stroke-width: 1.5; transition: stroke-width .16s ease, filter .16s ease; }
.action-dot--root { fill: #8b5cf6; }
.action-dot--click { fill: #2f80ed; }
.action-dot--swipe { fill: #f2994a; }
.action-dot--other { fill: #27ae60; }
.reference-node:hover .action-dot { stroke: #67b7ff; stroke-width: 3; }
.reference-node.selected .action-dot { stroke: #ffd166; stroke-width: 4; filter: drop-shadow(0 0 5px rgba(255,209,102,.8)); }
.reference-node text { fill: #dce7f5; font-size: 12px; pointer-events: none; }
.reference-node .node-count { font-weight: 750; }
.reference-node .node-action { font-weight: 700; }
.reference-node .node-summary { fill: #91a6c0; font-size: 10px; }
.terminal { fill: #ffd166; stroke: #0b1220; stroke-width: 1; }
.tree-controls { position: absolute; top: 14px; left: 14px; z-index: 4; display: flex; overflow: hidden; border: 1px solid #58708f; border-radius: 6px; background: rgba(17,24,39,.92); box-shadow: 0 6px 18px rgba(0,0,0,.3); }
.tree-controls button { min-width: 34px; height: 32px; padding: 0 9px; border: 0; border-right: 1px solid #354158; background: transparent; color: #dce7f5; cursor: pointer; }
.tree-controls button:last-child { border-right: 0; }
.tree-controls button:hover { background: #263149; color: #67b7ff; }
.tree-controls__reset { font-size: 11px; }
.tree-hint { position: absolute; bottom: 12px; left: 14px; padding: 7px 10px; border-radius: 5px; background: rgba(17,24,39,.86); color: #8297b1; font-size: 12px; pointer-events: none; }
</style>
