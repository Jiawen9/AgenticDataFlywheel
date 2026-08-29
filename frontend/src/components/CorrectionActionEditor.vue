<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { CorrectionRow } from '@/types'
import { parseBBox, type BBox, type Point } from '@/utils/actionOverlay'

const props = defineProps<{ row: CorrectionRow; imageUrl: string; saving?: boolean }>()
const emit = defineEmits<{ save: [actions: string] }>()

type PickMode = 'single' | 'start' | 'end' | null
interface ActionForm {
  action: string
  x: number
  y: number
  sx: number
  sy: number
  ex: number
  ey: number
  text: string
  button: string
  status: string
}

const actionTypes = ['click', 'long_press', 'type', 'open', 'swipe', 'system_button', 'wait', 'terminate', 'answer']
const form = ref<ActionForm>(emptyForm())
const pickMode = ref<PickMode>(null)
const naturalWidth = ref(1080)
const naturalHeight = ref(2340)
const bbox = computed<BBox | null>(() => parseBBox(props.row.actions_box))
const actionType = computed(() => form.value.action)
const clickPoint = computed<Point | null>(() => normalized(form.value.x, form.value.y))
const swipePoints = computed<[Point, Point] | null>(() => {
  if (actionType.value !== 'swipe') return null
  return [normalized(form.value.sx, form.value.sy), normalized(form.value.ex, form.value.ey)]
})
const markerId = `correction-arrow-${Math.random().toString(36).slice(2)}`

function emptyForm(): ActionForm {
  return { action: 'click', x: 500, y: 500, sx: 250, sy: 500, ex: 750, ey: 500, text: '', button: 'back', status: 'success' }
}

function loadAction() {
  const next = emptyForm()
  try {
    const parsed = JSON.parse(props.row.actions) as Record<string, unknown>
    if (typeof parsed.action === 'string' && actionTypes.includes(parsed.action)) next.action = parsed.action
    if (Array.isArray(parsed.coordinate)) {
      next.x = Number(parsed.coordinate[0]) || 0
      next.y = Number(parsed.coordinate[1]) || 0
    }
    if (Array.isArray(parsed.start_coordinate)) {
      next.sx = Number(parsed.start_coordinate[0]) || 0
      next.sy = Number(parsed.start_coordinate[1]) || 0
    }
    if (Array.isArray(parsed.end_coordinate)) {
      next.ex = Number(parsed.end_coordinate[0]) || 0
      next.ey = Number(parsed.end_coordinate[1]) || 0
    }
    if (typeof parsed.text === 'string') next.text = parsed.text
    if (typeof parsed.button === 'string') next.button = parsed.button
    if (typeof parsed.status === 'string') next.status = parsed.status
  } catch {
    // Keep a valid click form for malformed legacy rows.
  }
  form.value = next
  pickMode.value = null
}

function normalized(x: number, y: number): Point {
  return { x: (Number(x) / 999) * naturalWidth.value, y: (Number(y) / 999) * naturalHeight.value }
}

function onImageLoad(event: Event) {
  const image = event.target as HTMLImageElement
  naturalWidth.value = image.naturalWidth || 1080
  naturalHeight.value = image.naturalHeight || 2340
}

function pickPoint(event: MouseEvent) {
  if (!pickMode.value) return
  const target = event.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  const x = Math.round(Math.max(0, Math.min(999, ((event.clientX - rect.left) / rect.width) * 999)))
  const y = Math.round(Math.max(0, Math.min(999, ((event.clientY - rect.top) / rect.height) * 999)))
  if (pickMode.value === 'single') {
    form.value.x = x
    form.value.y = y
  } else if (pickMode.value === 'start') {
    form.value.sx = x
    form.value.sy = y
  } else {
    form.value.ex = x
    form.value.ey = y
  }
  pickMode.value = null
}

function actionPayload(): Record<string, unknown> {
  const value: Record<string, unknown> = { action: form.value.action }
  if (['click', 'long_press'].includes(form.value.action)) value.coordinate = [clamp(form.value.x), clamp(form.value.y)]
  if (form.value.action === 'swipe') {
    value.start_coordinate = [clamp(form.value.sx), clamp(form.value.sy)]
    value.end_coordinate = [clamp(form.value.ex), clamp(form.value.ey)]
  }
  if (['type', 'open', 'answer'].includes(form.value.action)) value.text = form.value.text
  if (form.value.action === 'system_button') value.button = form.value.button
  if (form.value.action === 'terminate') value.status = form.value.status
  return value
}

function clamp(value: number) {
  return Math.max(0, Math.min(999, Math.round(Number(value) || 0)))
}

function save() {
  if (props.saving) return
  emit('save', JSON.stringify(actionPayload(), null, 2))
}

watch(() => [props.row.excel_row, props.row.actions], loadAction, { immediate: true })
</script>

<template>
  <section class="correction-editor">
    <div class="editor-image-shell">
      <div class="editor-image">
        <img :src="imageUrl" :alt="`第 ${row.step} 步截图`" @load="onImageLoad" />
        <svg class="editor-overlay" :viewBox="`0 0 ${naturalWidth} ${naturalHeight}`" preserveAspectRatio="none" aria-hidden="true">
          <defs><marker :id="markerId" markerWidth="12" markerHeight="12" refX="9" refY="4" orient="auto"><path d="M0,0 L0,8 L10,4 z" fill="#f43f5e" /></marker></defs>
          <rect v-if="bbox" :x="bbox.x1" :y="bbox.y1" :width="bbox.x2 - bbox.x1" :height="bbox.y2 - bbox.y1" fill="rgba(244,63,94,.14)" stroke="#f43f5e" stroke-width="8" rx="10" />
          <template v-if="['click', 'long_press'].includes(actionType) && clickPoint"><circle :cx="clickPoint.x" :cy="clickPoint.y" r="18" fill="none" stroke="#f43f5e" stroke-width="8" /><circle :cx="clickPoint.x" :cy="clickPoint.y" r="7" fill="#f43f5e" /></template>
          <line v-if="swipePoints" :x1="swipePoints[0].x" :y1="swipePoints[0].y" :x2="swipePoints[1].x" :y2="swipePoints[1].y" stroke="#f43f5e" stroke-width="12" stroke-linecap="round" :marker-end="`url(#${markerId})`" />
        </svg>
        <div v-if="pickMode" class="pick-surface" @click="pickPoint"><span>请在图片上点击{{ pickMode === 'single' ? '动作点' : pickMode === 'start' ? '滑动起点' : '滑动终点' }}</span></div>
      </div>
    </div>
    <div class="editor-form">
      <div class="editor-heading"><span>STEP {{ String(row.step).padStart(3, '0') }}</span><el-tag v-if="row.edited" type="warning">{{ row.edit_status }}</el-tag></div>
      <el-select v-model="form.action" class="action-select"><el-option v-for="type in actionTypes" :key="type" :label="type" :value="type" /></el-select>

      <div v-if="['click', 'long_press'].includes(actionType)" class="coordinate-grid">
        <el-input-number v-model="form.x" :min="0" :max="999" controls-position="right" /><el-input-number v-model="form.y" :min="0" :max="999" controls-position="right" />
        <el-button @click="pickMode = 'single'">图上取点</el-button>
      </div>
      <template v-else-if="actionType === 'swipe'">
        <div class="coordinate-grid"><el-input-number v-model="form.sx" :min="0" :max="999" controls-position="right" /><el-input-number v-model="form.sy" :min="0" :max="999" controls-position="right" /><el-button @click="pickMode = 'start'">取起点</el-button></div>
        <div class="coordinate-grid"><el-input-number v-model="form.ex" :min="0" :max="999" controls-position="right" /><el-input-number v-model="form.ey" :min="0" :max="999" controls-position="right" /><el-button @click="pickMode = 'end'">取终点</el-button></div>
      </template>
      <el-input v-else-if="['type', 'open', 'answer'].includes(actionType)" v-model="form.text" :placeholder="actionType === 'open' ? '应用名称' : '输入内容'" />
      <el-select v-else-if="actionType === 'system_button'" v-model="form.button"><el-option v-for="button in ['back', 'home', 'menu', 'enter']" :key="button" :label="button" :value="button" /></el-select>
      <el-select v-else-if="actionType === 'terminate'" v-model="form.status"><el-option label="success" value="success" /><el-option label="failure" value="failure" /></el-select>
      <el-alert v-else-if="actionType === 'wait'" title="wait 动作无需额外参数" type="info" :closable="false" />

      <div class="editor-hint">坐标使用 0–999 归一化值，与原始标注格式一致。</div>
      <el-button type="primary" :loading="props.saving" @click="save">保存动作</el-button>
    </div>
  </section>
</template>

<style scoped>
.correction-editor{display:grid;grid-template-columns:minmax(0,1fr);gap:18px;min-width:0}.editor-image-shell{display:grid;place-items:center;min-width:0;min-height:300px;border-radius:14px;background:#0b1220;padding:14px;overflow:hidden}.editor-image{position:relative;display:inline-block;max-width:100%;line-height:0}.editor-image img{display:block;width:auto;max-width:100%;max-height:46vh;border-radius:10px}.editor-overlay{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}.pick-surface{position:absolute;inset:0;z-index:2;display:grid;place-items:center;cursor:crosshair;background:rgba(15,23,42,.22);line-height:normal}.pick-surface span{padding:9px 12px;border-radius:8px;background:#0f172a;color:white;font-size:12px}.editor-form{display:grid;align-content:start;min-width:0;gap:12px;padding-top:16px;border-top:1px solid #334155}.editor-heading{display:flex;align-items:center;justify-content:space-between;color:#64748b;font-size:11px;font-weight:900;letter-spacing:.12em}.action-select{width:100%}.coordinate-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) auto;gap:6px}.coordinate-grid :deep(.el-input-number){width:100%;min-width:0}.editor-hint{color:#94a3b8;font-size:11px;line-height:1.5}@media(max-width:850px){.editor-image-shell{min-height:260px}.editor-image img{max-height:52vh}}
</style>
