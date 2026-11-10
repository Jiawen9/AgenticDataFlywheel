<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { CorrectionRow, CoordinateInfo } from '@/types'
import { parseBBox, type BBox, type Point } from '@/utils/actionOverlay'

import { useCoordinateInfo } from '@/composables/useCoordinateInfo'
import { changesCoordinates, coordinateExtent, emptyActionForm, fromImageFraction, parseActionForm, serializeActionForm, toImagePoint } from '@/utils/correctionCoordinates'

const props = defineProps<{ row: CorrectionRow; imageUrl: string; saving?: boolean; readOnly?: boolean; sessionId?: string; storageRevision?: number; coordinateInfo?: CoordinateInfo }>()
const emit = defineEmits<{ save: [actions: string]; draft: [actions: string | null] }>()

const actionTypes = ['click', 'long_press', 'type', 'open', 'swipe', 'system_button', 'wait', 'terminate', 'answer']
const form = ref(emptyActionForm())
const originalForm = ref(emptyActionForm())
const coordinates = useCoordinateInfo(() => props.coordinateInfo || !props.sessionId ? undefined : ({
  kind: 'correction', session_id: props.sessionId, excel_row: props.row.excel_row, expected_revision: props.storageRevision,
}), () => props.row.agent_type)
const coordinateInfo = computed(() => props.coordinateInfo ?? coordinates.coordinateInfo.value)
const canPickCoordinates = computed(() => Boolean(coordinateExtent(coordinateInfo.value)) && !coordinates.conflict.value)
const coordinateChanged = computed(() => changesCoordinates(form.value, originalForm.value))
const draftValue = computed(() => serializeActionForm(props.row.actions, originalForm.value, form.value))
const saveBlocked = computed(() => coordinateChanged.value && !canPickCoordinates.value)
const swipeDragging = ref(false)
const swipeStart = ref<Point | null>(null)
const swipeDragPoint = ref<Point | null>(null)
const naturalWidth = ref(1080)
const naturalHeight = ref(2340)
const imageShell = ref<HTMLElement | null>(null)
const canvasSize = ref({ width: 1, height: 1 })
let resizeObserver: ResizeObserver | null = null
const bbox = computed<BBox | null>(() => parseBBox(props.row.actions_box))
const originalBbox = computed<BBox | null>(() => parseBBox(props.row.original_actions_box))
const bboxText = computed(() => bbox.value ? `[${bbox.value.x1}, ${bbox.value.y1}, ${bbox.value.x2}, ${bbox.value.y2}]` : '未标框')
const originalBboxText = computed(() => originalBbox.value ? `[${originalBbox.value.x1}, ${originalBbox.value.y1}, ${originalBbox.value.x2}, ${originalBbox.value.y2}]` : '未标框')
const actionType = computed(() => form.value.action)
const clickPoint = computed<Point | null>(() => project(form.value.x, form.value.y))
const swipePoints = computed<[Point, Point] | null>(() => {
  if (actionType.value !== 'swipe') return null
  const start = swipeStart.value || { x: form.value.sx, y: form.value.sy }
  const end = swipeDragPoint.value || { x: form.value.ex, y: form.value.ey }
  const a = project(start.x, start.y), b = project(end.x, end.y)
  return a && b ? [a, b] : null
})
const coordinateBadge = computed(() => {
  if (['click', 'long_press'].includes(actionType.value)) return `坐标 ${form.value.x}, ${form.value.y}`
  if (actionType.value === 'swipe') return `起点 ${form.value.sx}, ${form.value.sy} · 终点 ${form.value.ex}, ${form.value.ey}`
  return ''
})
const imageHint = computed(() => {
  if (!canPickCoordinates.value) return coordinateInfo.value.reason || '坐标信息不足，暂不可取点；可编辑文本及其他非坐标字段'
  if (['click', 'long_press'].includes(actionType.value)) return '点击图片设置动作点'
  if (actionType.value === 'swipe') return '按住并拖动设置滑动起点和终点'
  return '当前动作无需在图片上取点'
})
const markerId = `correction-arrow-${Math.random().toString(36).slice(2)}`

function loadAction() {
  const next = parseActionForm(props.row.actions)
  originalForm.value = { ...next }
  form.value = next
  swipeDragging.value = false
  swipeStart.value = null
  swipeDragPoint.value = null
}

function project(x: number, y: number): Point | null {
  return toImagePoint({ x, y }, { x: naturalWidth.value, y: naturalHeight.value }, coordinateInfo.value)
}

function onImageLoad(event: Event) {
  const image = event.target as HTMLImageElement
  naturalWidth.value = image.naturalWidth || 1080
  naturalHeight.value = image.naturalHeight || 2340
  updateCanvasSize()
}

function updateCanvasSize() {
  const shell = imageShell.value
  if (!shell) return
  const availableWidth = Math.max(1, shell.clientWidth - 24)
  const availableHeight = Math.max(1, shell.clientHeight - 24)
  const scale = Math.min(availableWidth / naturalWidth.value, availableHeight / naturalHeight.value)
  canvasSize.value = {
    width: Math.max(1, Math.round(naturalWidth.value * scale)),
    height: Math.max(1, Math.round(naturalHeight.value * scale)),
  }
}

function pointFromEvent(event: MouseEvent | PointerEvent): Point | null {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  if (rect.width <= 0 || rect.height <= 0) return null
  return fromImageFraction({ x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height }, coordinateInfo.value)
}

function handleImageClick(event: MouseEvent) {
  if (props.saving || props.readOnly || !canPickCoordinates.value || !['click', 'long_press'].includes(actionType.value)) return
  const point = pointFromEvent(event)
  if (!point) return
  form.value.x = point.x
  form.value.y = point.y
}

function startSwipe(event: PointerEvent) {
  if (props.saving || props.readOnly || !canPickCoordinates.value || actionType.value !== 'swipe') return
  event.preventDefault()
  const point = pointFromEvent(event)
  if (!point) return
  swipeDragging.value = true
  swipeStart.value = point
  swipeDragPoint.value = point
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}

function moveSwipe(event: PointerEvent) {
  if (!swipeDragging.value || actionType.value !== 'swipe') return
  swipeDragPoint.value = pointFromEvent(event)
}

function finishSwipe(event: PointerEvent) {
  if (!swipeDragging.value || !swipeStart.value || actionType.value !== 'swipe') return
  const end = pointFromEvent(event)
  if (!end) return
  form.value.sx = swipeStart.value.x
  form.value.sy = swipeStart.value.y
  form.value.ex = end.x
  form.value.ey = end.y
  swipeDragging.value = false
  swipeStart.value = null
  swipeDragPoint.value = null
  const target = event.currentTarget as HTMLElement
  if (target.hasPointerCapture(event.pointerId)) target.releasePointerCapture(event.pointerId)
}

function save() {
  if (props.saving || props.readOnly || saveBlocked.value) return
  emit('save', draftValue.value)
}

watch(() => [props.row.excel_row, props.row.actions] as const, (value, previous) => {
  if (previous && value[0] === previous[0]) {
    const draft = serializeActionForm(previous[1], originalForm.value, form.value)
    if (draft !== previous[1] && draft !== value[1]) {
      // A late save/SOP refresh must not discard a newer local action draft.
      originalForm.value = parseActionForm(value[1])
      return
    }
  }
  loadAction()
}, { immediate: true })
watch(actionType, () => {
  swipeDragging.value = false
  swipeStart.value = null
  swipeDragPoint.value = null
})
watch(draftValue, (value) => emit('draft', value === props.row.actions ? null : value), { immediate: true, flush: 'post' })

onMounted(() => {
  resizeObserver = new ResizeObserver(updateCanvasSize)
  if (imageShell.value) resizeObserver.observe(imageShell.value)
  void nextTick(updateCanvasSize)
})
onBeforeUnmount(() => resizeObserver?.disconnect())
</script>

<template>
  <section class="correction-editor">
    <div ref="imageShell" class="editor-image-shell">
      <div class="editor-image" :style="{ width: `${canvasSize.width}px`, height: `${canvasSize.height}px` }" :class="{ 'is-clickable': canPickCoordinates && ['click', 'long_press'].includes(actionType), 'is-draggable': canPickCoordinates && actionType === 'swipe' }"
        @click="handleImageClick" @pointerdown="startSwipe" @pointermove="moveSwipe" @pointerup="finishSwipe" @pointercancel="finishSwipe">
        <img :src="imageUrl" :alt="`第 ${row.step} 步截图`" @load="onImageLoad" />
        <svg class="editor-overlay" :viewBox="`0 0 ${naturalWidth} ${naturalHeight}`" preserveAspectRatio="none" aria-hidden="true">
          <defs><marker :id="markerId" markerWidth="12" markerHeight="12" refX="9" refY="4" orient="auto"><path d="M0,0 L0,8 L10,4 z" fill="#f43f5e" /></marker></defs>
          <rect v-if="bbox" :x="bbox.x1" :y="bbox.y1" :width="bbox.x2 - bbox.x1" :height="bbox.y2 - bbox.y1" fill="rgba(244,63,94,.14)" stroke="#f43f5e" stroke-width="8" rx="10" />
          <template v-if="['click', 'long_press'].includes(actionType) && clickPoint"><circle :cx="clickPoint.x" :cy="clickPoint.y" r="18" fill="none" stroke="#f43f5e" stroke-width="8" /><circle :cx="clickPoint.x" :cy="clickPoint.y" r="7" fill="#f43f5e" /></template>
          <line v-if="swipePoints" :x1="swipePoints[0].x" :y1="swipePoints[0].y" :x2="swipePoints[1].x" :y2="swipePoints[1].y" stroke="#f43f5e" stroke-width="12" stroke-linecap="round" :marker-end="`url(#${markerId})`" />
        </svg>
        <div v-if="coordinateBadge" class="coordinate-badge" aria-live="polite">{{ coordinateBadge }}</div>
        <div class="image-hint">{{ imageHint }}</div>
      </div>
    </div>
    <fieldset class="editor-form" :disabled="props.saving || props.readOnly">
      <div class="editor-heading"><span>STEP {{ String(row.step).padStart(3, '0') }}</span><el-tag v-if="row.edited" type="warning">{{ row.edit_status }}</el-tag></div>
      <label class="field-label" for="correction-action-type">修正动作</label>
      <el-select id="correction-action-type" v-model="form.action" class="action-select" :disabled="props.saving || props.readOnly" aria-label="动作类型"><el-option v-for="type in actionTypes" :key="type" :label="type" :value="type" /></el-select>
      <div v-if="['click', 'long_press', 'swipe'].includes(actionType)" class="interaction-hint">{{ imageHint }}<span v-if="coordinateBadge">{{ coordinateBadge }}</span></div>
      <el-input v-else-if="['type', 'open', 'answer'].includes(actionType)" v-model="form.text" :placeholder="actionType === 'open' ? '应用名称' : '输入内容'" />
      <el-select v-else-if="actionType === 'system_button'" v-model="form.button" :disabled="props.saving || props.readOnly"><el-option v-for="button in ['back', 'home', 'menu', 'enter']" :key="button" :label="button" :value="button" /></el-select>
      <el-select v-else-if="actionType === 'terminate'" v-model="form.status" :disabled="props.saving || props.readOnly"><el-option label="success" value="success" /><el-option label="failure" value="failure" /></el-select>
      <el-alert v-else-if="actionType === 'wait'" title="wait 动作无需额外参数" type="info" :closable="false" />

      <div class="bbox-status">
        <div><span>当前 bbox</span><code>{{ bboxText }}</code></div>
        <div><span>原始 bbox</span><code>{{ originalBboxText }}</code></div>
        <small v-if="row.bbox_edited">当前框为专家修改后的结果，将随导出文件保存。</small>
      </div>

      <div v-if="coordinates.error.value" class="editor-hint" role="status">{{ coordinates.error.value }}</div>
      <div class="editor-hint">{{ coordinateInfo.space === 'pixel' ? '坐标使用源截图像素（' + coordinateInfo.width + ' × ' + coordinateInfo.height + '）' : coordinateInfo.space === 'normalized' ? '坐标使用 0–999 归一化值' : '保留原值，坐标待确认' }}；图片上的修改需要点击保存动作后写入。</div>
      <el-button class="save-action" type="primary" :loading="props.saving" :disabled="props.readOnly || saveBlocked" @click="save">保存动作</el-button>
    </fieldset>
  </section>
</template>

<style scoped>
.editor-form{margin:0;padding:0;border:0}
.correction-editor{display:grid;grid-template-columns:minmax(0,1fr) 184px;gap:12px;min-width:0;padding:12px;border:1px solid var(--line);border-radius:14px;background:#f8fafc}.editor-image-shell{display:grid;place-items:center;min-width:0;height:clamp(420px,70vh,720px);padding:12px;border-radius:12px;background:#0b1220;overflow:hidden}.editor-image{position:relative;display:block;line-height:0}.editor-image img{display:block;width:100%;height:100%;border-radius:10px;object-fit:contain}.editor-image.is-clickable{cursor:crosshair}.editor-image.is-draggable{cursor:grab;touch-action:none}.editor-image.is-draggable:active{cursor:grabbing}.editor-overlay{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}.coordinate-badge{position:absolute;top:10px;right:10px;z-index:2;max-width:calc(100% - 20px);padding:6px 9px;border:1px solid rgba(255,255,255,.2);border-radius:7px;background:rgba(2,6,23,.84);color:white;font-size:11px;line-height:1.3;white-space:nowrap}.image-hint{position:absolute;right:10px;bottom:10px;left:10px;z-index:2;padding:6px 9px;border-radius:7px;background:rgba(2,6,23,.72);color:#e2e8f0;font-size:11px;line-height:1.35;text-align:center;pointer-events:none}.editor-form{display:grid;align-content:start;min-width:0;gap:12px;height:100%;padding:14px;border:1px solid var(--line);border-radius:12px;background:white;color:#334155}.editor-heading{display:flex;align-items:center;justify-content:space-between;color:#64748b;font-size:11px;font-weight:900;letter-spacing:.12em}.field-label{color:#64748b;font-size:12px;font-weight:800}.action-select{width:100%}.interaction-hint{display:grid;gap:4px;padding:9px 10px;border-radius:8px;background:#f0fdfa;color:#0f766e;font-size:11px;line-height:1.45}.interaction-hint span{color:#334155;font-variant-numeric:tabular-nums}.editor-hint{color:#64748b;font-size:11px;line-height:1.5}.save-action{width:100%}@media(max-width:850px){.correction-editor{grid-template-columns:1fr}.editor-image-shell{height:clamp(360px,62vh,620px)}.editor-form{height:auto}}
.bbox-status{display:grid;gap:6px;padding:9px 10px;border:1px solid #dbe3ed;border-radius:8px;background:#f8fafc}.bbox-status>div{display:flex;justify-content:space-between;gap:8px;align-items:baseline;min-width:0}.bbox-status span{color:#64748b;font-size:11px}.bbox-status code{overflow:hidden;color:#0f766e;font:11px ui-monospace,SFMono-Regular,Consolas,monospace;text-overflow:ellipsis;white-space:nowrap}.bbox-status small{color:#0f766e;font-size:10px;line-height:1.4}
</style>
