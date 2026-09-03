<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ActionPayload } from '@/types'
import {
  actionKind,
  fallbackSwipePoints,
  normalizedPoint,
  parseBBox,
  swipeDirection,
  type BBox,
  type Point,
} from '@/utils/actionOverlay'

const props = withDefaults(
  defineProps<{
    imageUrl: string
    action: ActionPayload
    actionsBox?: string
    alt?: string
    showOverlay?: boolean
    editable?: boolean
    showEditTrigger?: boolean
    colorTone?: 'deep' | 'bright'
    onSaveBbox?: (bbox: [number, number, number, number]) => Promise<void>
  }>(),
  { actionsBox: '', alt: '轨迹步骤截图', showOverlay: true, editable: false, showEditTrigger: true, colorTone: 'deep' },
)
const emit = defineEmits<{ 'editing-change': [editing: boolean] }>()

const naturalWidth = ref(1080)
const naturalHeight = ref(2340)
const failed = ref(false)
const editing = ref(false)
const drawing = ref(false)
const saving = ref(false)
const dragStart = ref<Point | null>(null)
const draftBBox = ref<BBox | null>(null)
const markerId = `arrow-${Math.random().toString(36).slice(2)}`
const kind = computed(() => actionKind(props.action))
const bbox = computed(() => parseBBox(props.actionsBox))
const clickPoint = computed(() =>
  normalizedPoint(props.action.coordinate, naturalWidth.value, naturalHeight.value),
)
const swipePoints = computed<[Point, Point] | null>(() => {
  const start = normalizedPoint(props.action.start_coordinate, naturalWidth.value, naturalHeight.value)
  const end = normalizedPoint(props.action.end_coordinate, naturalWidth.value, naturalHeight.value)
  if (start && end) return [start, end]
  return bbox.value ? fallbackSwipePoints(bbox.value, swipeDirection(props.actionsBox)) : null
})
const color = computed(() => {
  if (props.colorTone === 'bright') {
    if (kind.value === 'swipe') return '#38bdf8'
    if (kind.value === 'long_press') return '#fbbf24'
    if (kind.value === 'type') return '#c084fc'
    return '#fb7185'
  }
  if (kind.value === 'swipe') return '#075985'
  if (kind.value === 'long_press') return '#b45309'
  if (kind.value === 'type') return '#7e22ce'
  return '#b91c1c'
})
const labelX = computed(() => Math.max(12, bbox.value?.x1 ?? 16))
const labelY = computed(() => Math.max(82, (bbox.value?.y1 ?? 100) - 18))

watch(() => props.imageUrl, () => {
  failed.value = false
  editing.value = false
  drawing.value = false
  emit('editing-change', false)
})

function beginEditing() {
  const current = bbox.value
  draftBBox.value = current ? { ...current } : null
  editing.value = true
  emit('editing-change', true)
}

function cancelEditing() {
  editing.value = false
  drawing.value = false
  dragStart.value = null
  draftBBox.value = null
  emit('editing-change', false)
}

function eventPoint(event: PointerEvent): Point {
  const target = event.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  return {
    x: Math.round(Math.max(0, Math.min(naturalWidth.value, ((event.clientX - rect.left) / rect.width) * naturalWidth.value))),
    y: Math.round(Math.max(0, Math.min(naturalHeight.value, ((event.clientY - rect.top) / rect.height) * naturalHeight.value))),
  }
}

function startDrawing(event: PointerEvent) {
  const point = eventPoint(event)
  dragStart.value = point
  draftBBox.value = { x1: point.x, y1: point.y, x2: point.x, y2: point.y }
  drawing.value = true
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}

function draw(event: PointerEvent) {
  if (!drawing.value || !dragStart.value) return
  const point = eventPoint(event)
  draftBBox.value = {
    x1: Math.min(dragStart.value.x, point.x),
    y1: Math.min(dragStart.value.y, point.y),
    x2: Math.max(dragStart.value.x, point.x),
    y2: Math.max(dragStart.value.y, point.y),
  }
}

function finishDrawing(event: PointerEvent) {
  if (!drawing.value) return
  draw(event)
  drawing.value = false
  const target = event.currentTarget as HTMLElement
  if (target.hasPointerCapture(event.pointerId)) target.releasePointerCapture(event.pointerId)
}

async function saveDrawing() {
  const value = draftBBox.value
  if (!value || value.x2 <= value.x1 || value.y2 <= value.y1 || !props.onSaveBbox) return
  saving.value = true
  try {
    await props.onSaveBbox([value.x1, value.y1, value.x2, value.y2])
    cancelEditing()
  } catch {
    // Parent displays the persistence error; keep edit mode open for retry.
  } finally {
    saving.value = false
  }
}

defineExpose({ beginEditing })

function onLoad(event: Event) {
  const image = event.target as HTMLImageElement
  naturalWidth.value = image.naturalWidth || 1080
  naturalHeight.value = image.naturalHeight || 2340
  failed.value = false
}
</script>

<template>
  <div class="action-image">
    <div v-if="!failed" class="action-image__canvas">
      <img :src="imageUrl" :alt="alt" @load="onLoad" @error="failed = true" />
      <svg
        v-if="showOverlay"
        class="action-image__overlay"
        :viewBox="`0 0 ${naturalWidth} ${naturalHeight}`"
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <defs>
          <marker :id="markerId" markerWidth="12" markerHeight="12" refX="9" refY="4" orient="auto">
            <path d="M0,0 L0,8 L10,4 z" :fill="color" />
          </marker>
        </defs>
        <rect
          v-if="bbox"
          :x="bbox.x1"
          :y="bbox.y1"
          :width="bbox.x2 - bbox.x1"
          :height="bbox.y2 - bbox.y1"
          fill="rgba(15, 23, 42, .14)"
          :stroke="color"
          stroke-width="8"
          rx="10"
        />
        <template v-if="(kind === 'click' || kind === 'long_press') && clickPoint">
          <circle :cx="clickPoint.x" :cy="clickPoint.y" r="19" fill="none" :stroke="color" stroke-width="8" />
          <circle :cx="clickPoint.x" :cy="clickPoint.y" r="7" :fill="color" />
          <circle
            v-if="kind === 'long_press'"
            :cx="clickPoint.x"
            :cy="clickPoint.y"
            r="34"
            fill="none"
            :stroke="color"
            stroke-width="5"
            stroke-dasharray="12 9"
          />
        </template>
        <line
          v-if="kind === 'swipe' && swipePoints"
          :x1="swipePoints[0].x"
          :y1="swipePoints[0].y"
          :x2="swipePoints[1].x"
          :y2="swipePoints[1].y"
          :stroke="color"
          stroke-width="12"
          stroke-linecap="round"
          :marker-end="`url(#${markerId})`"
        />
        <g :transform="`translate(${labelX}, ${labelY})`">
          <rect x="0" y="-74" :width="Math.max(250, kind.length * 56 + 68)" height="92" rx="16" fill="#020617" opacity=".96" />
          <text x="26" y="0" fill="white" font-size="64" font-weight="800">{{ kind }}</text>
        </g>
      </svg>
      <div v-if="editable && (showEditTrigger || editing)" class="action-image__toolbar">
        <button v-if="!editing && showEditTrigger" type="button" @click="beginEditing">修改 bbox</button>
        <template v-else>
          <button type="button" :disabled="saving" @click="cancelEditing">取消</button>
          <button
            type="button"
            class="is-primary"
            :disabled="!draftBBox || draftBBox.x2 <= draftBBox.x1 || draftBBox.y2 <= draftBBox.y1 || saving"
            @click="saveDrawing"
          >{{ saving ? '保存中…' : '保存 bbox' }}</button>
        </template>
      </div>
      <div
        v-if="editing"
        class="action-image__edit-surface"
        @pointerdown="startDrawing"
        @pointermove="draw"
        @pointerup="finishDrawing"
        @pointercancel="finishDrawing"
      >
        <svg :viewBox="`0 0 ${naturalWidth} ${naturalHeight}`" preserveAspectRatio="none">
          <rect
            v-if="draftBBox"
            :x="draftBBox.x1"
            :y="draftBBox.y1"
            :width="draftBBox.x2 - draftBBox.x1"
            :height="draftBBox.y2 - draftBBox.y1"
            fill="rgba(34, 197, 94, .18)"
            stroke="#22c55e"
            stroke-width="10"
            stroke-dasharray="20 12"
          />
        </svg>
        <span>按住鼠标拖动，重新框选动作区域</span>
      </div>
    </div>
    <el-empty v-else description="截图加载失败" :image-size="72" />
  </div>
</template>

<style scoped>
.action-image { display: grid; place-items: center; width: 100%; min-height: 240px; }
.action-image__canvas { position: relative; display: inline-block; max-width: 100%; line-height: 0; }
.action-image img { display: block; width: auto; max-width: 100%; max-height: 68vh; border-radius: 14px; box-shadow: 0 16px 48px rgba(15, 23, 42, .16); }
.action-image__overlay { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
.action-image__toolbar { position: absolute; top: 10px; right: 10px; z-index: 5; display: flex; gap: 6px; line-height: normal; }
.action-image__toolbar button { padding: 7px 10px; border: 1px solid rgba(255,255,255,.48); border-radius: 7px; background: rgba(2,6,23,.86); color: white; font-size: 12px; font-weight: 750; cursor: pointer; box-shadow: 0 4px 12px rgba(0,0,0,.22); }
.action-image__toolbar button.is-primary { border-color: #4ade80; background: #15803d; }
.action-image__toolbar button:disabled { cursor: not-allowed; opacity: .55; }
.action-image__edit-surface { position: absolute; inset: 0; z-index: 4; overflow: hidden; border-radius: 14px; cursor: crosshair; touch-action: none; }
.action-image__edit-surface svg { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
.action-image__edit-surface span { position: absolute; right: 10px; bottom: 10px; padding: 6px 9px; border-radius: 6px; background: rgba(2,6,23,.82); color: white; font-size: 11px; line-height: normal; pointer-events: none; }
</style>
