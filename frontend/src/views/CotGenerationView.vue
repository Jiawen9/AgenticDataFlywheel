<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCheck, Document, Refresh } from '@element-plus/icons-vue'
import { api, correctionDownloadUrl } from '@/api'
import ActionImage from '@/components/ActionImage.vue'
import { parseBBox, type BBox } from '@/utils/actionOverlay'
import type { CorrectionCotJob, CorrectionCotResponse, CorrectionCotRow, CorrectionSession } from '@/types'

type EditableTextField = 'thought' | 'summary'

const sessions = ref<CorrectionSession[]>([])
const selectedSessionId = ref('')
const sessionCot = ref<CorrectionCotResponse | null>(null)
const selectedGroupId = ref('')
const selectedRowKey = ref('')
const loading = ref(true)
const loadingCot = ref(false)
const saving = ref(false)
const exporting = ref(false)
const error = ref('')
const activeJob = ref<CorrectionCotJob | null>(null)
const bboxEditing = ref(false)
const editingField = ref<EditableTextField | null>(null)
const textDraft = ref('')
let timer: number | null = null

const groups = computed(() => sessionCot.value?.groups ?? [])
const activeGroup = computed(() => groups.value.find((group) => group.group_id === selectedGroupId.value) ?? groups.value[0] ?? null)
const activeRow = computed<CorrectionCotRow | null>(() => activeGroup.value?.rows.find((row) => `${row.excel_row}` === selectedRowKey.value) ?? activeGroup.value?.rows[0] ?? null)
const editedRows = computed(() => groups.value.reduce((total, group) => total + group.rows.length, 0))
const generatedRows = computed(() => groups.value.reduce((total, group) => total + group.rows.filter((row) => row.status === 'generated').length, 0))
const selectedSession = computed(() => sessions.value.find((item) => item.session_id === selectedSessionId.value) ?? null)
const jobRunning = computed(() => Boolean(activeJob.value && ['queued', 'running'].includes(activeJob.value.status)))
const parsedAction = computed<Record<string, unknown>>(() => {
  try {
    const value = JSON.parse(activeRow.value?.action || '')
    return value && typeof value === 'object' ? value : {}
  } catch { return {} }
})
const compactAction = computed(() => {
  const value = activeRow.value?.action || ''
  try { return JSON.stringify(JSON.parse(value)) } catch { return value.replace(/\s+/g, ' ').trim() }
})
const actionType = computed(() => typeof parsedAction.value.action === 'string' ? parsedAction.value.action : '')
const canEditBBox = computed(() => ['click', 'long_press', 'swipe'].includes(actionType.value))
const bboxText = computed(() => {
  const box = parseBBox(activeRow.value?.actions_box || '')
  return box ? `[${box.x1}, ${box.y1}, ${box.x2}, ${box.y2}]` : '未标框'
})
const currentFieldValue = computed(() => {
  if (!activeRow.value || !editingField.value) return ''
  return String(activeRow.value[editingField.value] || '')
})
const textDirty = computed(() => editingField.value !== null && textDraft.value !== currentFieldValue.value)
const hasUnsaved = computed(() => bboxEditing.value || textDirty.value)

function stopPolling() {
  if (timer !== null) { window.clearInterval(timer); timer = null }
}

function resetLocalEditing() {
  bboxEditing.value = false
  editingField.value = null
  textDraft.value = ''
}

function selectRow(groupId: string, row: CorrectionCotRow) {
  if (hasUnsaved.value) {
    ElMessage.warning('请先保存或取消当前修改')
    return
  }
  selectedGroupId.value = groupId
  selectedRowKey.value = String(row.excel_row)
  resetLocalEditing()
}

async function refreshCot() {
  if (!selectedSessionId.value) { sessionCot.value = null; return }
  loadingCot.value = true; error.value = ''
  const previousGroupId = selectedGroupId.value
  const previousRowKey = selectedRowKey.value
  try {
    sessionCot.value = await api.correctionSessionCot(selectedSessionId.value)
    const group = groups.value.find((item) => item.group_id === previousGroupId) ?? groups.value[0]
    const row = group?.rows.find((item) => String(item.excel_row) === previousRowKey) ?? group?.rows[0] ?? null
    selectedGroupId.value = group?.group_id ?? ''
    selectedRowKey.value = row ? String(row.excel_row) : ''
    resetLocalEditing()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loadingCot.value = false
  }
}

async function pollJob() {
  if (!activeJob.value) return
  try {
    const next = await api.correctionCotJob(activeJob.value.job_id)
    activeJob.value = next
    if (next.status === 'succeeded') {
      stopPolling()
      await refreshCot()
      ElMessage.success(next.generate_bbox ? 'bbox 与 COT 批量生成完成' : 'COT 重新生成完成')
    } else if (next.status === 'failed' || next.status === 'interrupted') {
      stopPolling()
    }
  } catch (cause) {
    stopPolling()
    error.value = cause instanceof Error ? cause.message : String(cause)
  }
}

function startPolling(job: CorrectionCotJob) {
  activeJob.value = job
  stopPolling()
  if (job.status === 'queued' || job.status === 'running') {
    timer = window.setInterval(() => void pollJob(), 1200)
  }
}

async function loadSessions() {
  loading.value = true; error.value = ''
  try {
    sessions.value = await api.correctionSessions()
    if (!selectedSessionId.value || !sessions.value.some((item) => item.session_id === selectedSessionId.value)) {
      selectedSessionId.value = sessions.value[0]?.session_id ?? ''
    }
    const jobs = selectedSessionId.value ? await api.correctionCotJobs() : []
    const existing = jobs.find((job) => job.session_id === selectedSessionId.value && (job.status === 'queued' || job.status === 'running'))
    if (existing) startPolling(existing)
    await refreshCot()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
}

function serializeBBox(row: CorrectionCotRow, box: BBox): string {
  let action: Record<string, unknown> = {}
  try { action = JSON.parse(row.action) as Record<string, unknown> } catch { return '' }
  const kind = String(action.action || '').toLowerCase()
  const tagged = `[${box.x1},${box.y1},${box.x2},${box.y2}]`
  if (kind === 'swipe') {
    const start = Array.isArray(action.start_coordinate) ? action.start_coordinate : [0, 0]
    const end = Array.isArray(action.end_coordinate) ? action.end_coordinate : [0, 0]
    const dx = Number(end[0]) - Number(start[0])
    const dy = Number(end[1]) - Number(start[1])
    const direction = Math.abs(dx) > Math.abs(dy) ? (dx < 0 ? 'left' : 'right') : (dy < 0 ? 'up' : 'down')
    return `swipe_screen(bbox=<bbox>${tagged}</bbox>, direction=${direction})`
  }
  if (['click', 'long_press'].includes(kind)) return `${kind}(bbox=<bbox>${tagged}</bbox>)`
  return ''
}

async function saveBBox(value: [number, number, number, number]) {
  const row = activeRow.value
  if (!row || !selectedSessionId.value) return
  const box: BBox = { x1: value[0], y1: value[1], x2: value[2], y2: value[3] }
  saving.value = true; error.value = ''
  try {
    await api.patchCorrectionRow(selectedSessionId.value, row.excel_row, { actions_box: serializeBBox(row, box) })
    bboxEditing.value = false
    await refreshCot()
    ElMessage.success('bbox 已保存到纠偏草稿')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
    throw cause
  } finally {
    saving.value = false
  }
}

function beginTextEdit(field: EditableTextField) {
  if (!activeRow.value || bboxEditing.value) return
  editingField.value = field
  textDraft.value = String(activeRow.value[field] || '')
}

function cancelTextEdit() {
  editingField.value = null
  textDraft.value = ''
}

async function saveTextField(field: EditableTextField) {
  const row = activeRow.value
  if (!row || !selectedSessionId.value || editingField.value !== field) return
  saving.value = true; error.value = ''
  try {
    await api.patchCorrectionRow(selectedSessionId.value, row.excel_row, { [field]: textDraft.value })
    cancelTextEdit()
    await refreshCot()
    ElMessage.success(field === 'thought' ? 'Thought 已保存' : 'Summary 已保存')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    saving.value = false
  }
}

async function regenerateCot() {
  const row = activeRow.value
  if (!row || !selectedSessionId.value || saving.value || hasUnsaved.value) return
  saving.value = true; error.value = ''
  try {
    startPolling(await api.createCorrectionCotJob(
      selectedSessionId.value,
      [selectedGroupId.value],
      [row.excel_row],
      { generateBBox: false },
    ))
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    saving.value = false
  }
}

async function generateAll() {
  if (!selectedSessionId.value || saving.value || hasUnsaved.value) return
  const rows = groups.value.flatMap((group) => group.rows)
  try {
    await ElMessageBox.confirm(
      `将重新生成 ${rows.length} 个步骤的 bbox、thought 和 summary；已有人工修改会被覆盖。`,
      '确认批量生成',
      { type: 'warning', confirmButtonText: '确认覆盖生成', cancelButtonText: '取消' },
    )
  } catch { return }
  saving.value = true; error.value = ''
  try {
    startPolling(await api.createCorrectionCotJob(selectedSessionId.value, undefined, undefined, { generateBBox: true, forceOverwrite: true }))
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    saving.value = false
  }
}

async function exportDataset() {
  if (!selectedSessionId.value || hasUnsaved.value || jobRunning.value || exporting.value) return
  exporting.value = true; error.value = ''
  try {
    const result = await api.correctionDatasetExport(selectedSessionId.value)
    window.open(correctionDownloadUrl(selectedSessionId.value, result.filename), '_blank', 'noopener')
    ElMessage.success(`完整数据集已导出，替换 ${result.summary?.changed_rows ?? 0} 个步骤`)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    exporting.value = false
  }
}

watch(selectedSessionId, () => {
  activeJob.value = null
  stopPolling()
  resetLocalEditing()
  void refreshCot()
})
onMounted(() => { void loadSessions() })
onBeforeUnmount(() => { stopPolling() })
</script>

<template>
  <div class="page cot-page">
    <header class="page-hero cot-hero">
      <div><span class="eyebrow">CORRECTED TRAJECTORY COT</span><h1>COT 生成</h1><p>批量生成后直接采用，单步可继续调整 bbox、Thought 和 Summary。</p></div>
      <div class="hero-metrics"><div><b>{{ editedRows }}</b><span>纠偏步骤</span></div><div><b>{{ generatedRows }}</b><span>模型生成</span></div></div>
    </header>
    <section class="toolbar">
      <div class="toolbar-field"><span>纠偏会话</span><el-select v-model="selectedSessionId" :loading="loading" :disabled="loading || !sessions.length || hasUnsaved || jobRunning" placeholder="选择纠偏会话"><el-option v-for="item in sessions" :key="item.session_id" :label="`${item.session_id} · ${item.tree_run_id}`" :value="item.session_id" /></el-select></div>
      <div class="toolbar-stats"><span>任务 {{ selectedSession?.group_count ?? 0 }}</span><span>修改步骤 {{ editedRows }}</span></div>
      <el-button type="primary" :loading="saving && jobRunning" :disabled="!selectedSessionId || !editedRows || jobRunning || hasUnsaved" @click="generateAll">批量生成 bbox + COT</el-button>
      <el-button type="success" :loading="exporting" :disabled="!selectedSessionId || !editedRows || jobRunning || hasUnsaved || saving" @click="exportDataset">导出数据集</el-button>
      <el-button :icon="Refresh" text :disabled="loadingCot || jobRunning || hasUnsaved" @click="refreshCot">刷新</el-button>
    </section>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <section v-if="activeJob" class="job-status">
      <div><b>{{ activeJob.status === 'succeeded' ? '生成完成' : activeJob.status === 'failed' ? '生成失败' : activeJob.stage === 'generating_bbox' ? '正在生成 bbox' : '正在生成 COT' }}</b><span v-if="activeJob.current_trajectory">{{ activeJob.current_trajectory }} · Step {{ activeJob.current_step }}</span><span v-if="activeJob.generate_bbox">bbox {{ activeJob.completed_bbox ?? 0 }}/{{ activeJob.total_steps }}</span><span>COT {{ activeJob.completed_cot ?? activeJob.completed_steps }}/{{ activeJob.total_steps }}</span><em v-if="activeJob.error">{{ activeJob.error }}</em></div>
      <el-progress :percentage="activeJob.percent" :status="activeJob.status === 'failed' ? 'exception' : activeJob.status === 'succeeded' ? 'success' : undefined" />
    </section>
    <el-empty v-if="!loading && !sessions.length" description="暂无纠偏会话，请先完成专家动作纠偏" :image-size="90" />
    <section v-else v-loading="loading || loadingCot" class="workspace">
      <aside class="step-panel">
        <div class="panel-title"><b>修改步骤</b><span>{{ editedRows }} 步</span></div>
        <div v-for="group in groups" :key="group.group_id" class="trajectory-group">
          <div class="trajectory-heading"><el-icon><Document /></el-icon><b>{{ group.trajectory_id }}</b></div>
          <button v-for="row in group.rows" :key="row.excel_row" class="step-item" :class="{ active: activeRow?.excel_row === row.excel_row }" :title="`${row.trajectory_id}-step${row.step}`" @click="selectRow(group.group_id, row)"><span>{{ row.trajectory_id }}-step{{ row.step }}</span><span class="step-check">✓</span></button>
        </div>
      </aside>
      <section class="image-panel">
        <div class="panel-title"><div><b>步骤截图</b><span v-if="activeRow">{{ activeRow.trajectory_id }} · Step {{ activeRow.step }}</span></div><el-tag v-if="bboxEditing" type="warning" size="small">bbox 编辑中</el-tag></div>
        <div class="image-stage">
          <ActionImage
            v-if="activeRow"
            :key="`${activeRow.excel_row}:${activeRow.actions_box}`"
            :image-url="activeRow.image_url"
            :action="parsedAction"
            :actions-box="activeRow.actions_box"
            :alt="`${activeRow.trajectory_id} Step ${activeRow.step}`"
            :editable="canEditBBox && !jobRunning && !saving"
            :show-edit-trigger="true"
            :on-save-bbox="saveBBox"
            @editing-change="bboxEditing = $event"
          />
          <div v-else class="empty-image">选择左侧步骤查看截图</div>
        </div>
      </section>
      <aside class="detail-panel">
        <div class="panel-title"><b>步骤详情</b><el-icon v-if="activeRow" class="done"><CircleCheck /></el-icon></div>
        <el-empty v-if="!activeRow" description="选择一个步骤" :image-size="70" />
        <template v-else>
          <div class="identity"><span>任务名</span><b>{{ activeRow.task_id }}</b><p>{{ activeGroup?.task }}</p><div class="identity-meta">{{ activeRow.trajectory_id }} · Step {{ activeRow.step }}</div></div>
          <div class="field-card"><label>Action（专家纠偏结果）</label><code class="action-readonly" :title="compactAction">{{ compactAction }}</code><div class="original-value"><span>原始 Action</span><code :title="activeRow.original_action || '暂无'">{{ activeRow.original_action || '暂无' }}</code></div></div>
          <div class="bbox-card"><div class="card-head"><label>当前 bbox</label><code>{{ bboxText }}</code></div><div class="source-line">来源：{{ activeRow.bbox_source || 'original' }}</div><div class="original-value"><span>原始 bbox</span><code>{{ activeRow.original_actions_box || '未标框' }}</code></div></div>
          <div class="compare-card">
            <div><label>旧 Thought</label><p>{{ activeRow.original_thought || '暂无' }}</p></div>
            <div class="editable-result"><div class="result-head"><label>新 Thought</label><el-button v-if="editingField !== 'thought'" link type="primary" :disabled="saving || jobRunning || bboxEditing || editingField !== null" @click="beginTextEdit('thought')">编辑</el-button></div><template v-if="editingField === 'thought'"><el-input v-model="textDraft" type="textarea" :rows="4" /><div class="inline-actions"><el-button size="small" @click="cancelTextEdit">取消</el-button><el-button size="small" type="primary" :loading="saving" :disabled="!textDirty" @click="saveTextField('thought')">保存</el-button></div></template><p v-else>{{ activeRow.thought || '暂无' }}</p></div>
            <div><label>旧 Summary</label><p>{{ activeRow.original_summary || '暂无' }}</p></div>
            <div class="editable-result"><div class="result-head"><label>新 Summary</label><el-button v-if="editingField !== 'summary'" link type="primary" :disabled="saving || jobRunning || bboxEditing || editingField !== null" @click="beginTextEdit('summary')">编辑</el-button></div><template v-if="editingField === 'summary'"><el-input v-model="textDraft" type="textarea" :rows="4" /><div class="inline-actions"><el-button size="small" @click="cancelTextEdit">取消</el-button><el-button size="small" type="primary" :loading="saving" :disabled="!textDirty" @click="saveTextField('summary')">保存</el-button></div></template><p v-else>{{ activeRow.summary || '暂无' }}</p></div>
          </div>
          <div class="history-card"><label>History</label><pre>{{ activeRow.history || 'Empty' }}</pre></div>
          <div class="detail-actions"><el-button type="primary" :loading="saving || jobRunning" :disabled="hasUnsaved || jobRunning" @click="regenerateCot">重新生成 COT</el-button></div>
        </template>
      </aside>
    </section>
  </div>
</template>

<style scoped>
.cot-hero{margin-bottom:10px}.toolbar{display:flex;align-items:end;gap:18px;flex-wrap:wrap;margin:12px 0 14px;padding:14px 16px;border:1px solid var(--line);border-radius:14px;background:#fff}.toolbar-field{display:grid;gap:6px}.toolbar-field>span,.toolbar label,.field-card label,.bbox-card label,.compare-card label,.history-card label{font-size:11px;font-weight:800;letter-spacing:.05em;color:#64748b}.toolbar-field .el-select{width:360px;max-width:100%}.toolbar-stats{display:flex;gap:14px;padding-bottom:9px;color:#64748b;font-size:12px}.toolbar>.el-button--primary{margin-left:auto}.job-status{display:grid;gap:10px;margin:0 0 14px;padding:12px 16px;border:1px solid var(--line);border-radius:12px;background:#fff}.job-status>div{display:flex;gap:12px;flex-wrap:wrap;align-items:center;color:#64748b;font-size:12px}.job-status b{color:#0f172a}.job-status em{color:#dc2626;font-style:normal}.workspace{display:grid;grid-template-columns:236px minmax(330px,1fr) minmax(360px,.95fr);gap:14px;min-height:650px}.step-panel,.image-panel,.detail-panel{min-width:0;border:1px solid var(--line);border-radius:14px;background:#fff;overflow:hidden}.step-panel{overflow:auto}.panel-title{display:flex;justify-content:space-between;align-items:center;gap:8px;min-height:48px;padding:0 14px;border-bottom:1px solid #e5e7eb;color:#0f172a;font-size:13px}.panel-title span{margin-left:8px;color:#94a3b8;font-size:11px;font-weight:400}.trajectory-group{padding:10px 8px;border-bottom:1px solid #eef2f7}.trajectory-heading{display:grid;grid-template-columns:auto 1fr;gap:6px 8px;align-items:center;padding:5px 7px;color:#334155;font-size:12px}.trajectory-heading small{grid-column:2;overflow:hidden;color:#94a3b8;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.step-item{display:grid;grid-template-columns:1fr auto;gap:3px 8px;width:100%;margin:2px 0;padding:9px 8px;border:1px solid transparent;border-radius:9px;background:transparent;color:#475569;text-align:left;cursor:pointer}.step-item:hover{background:#f0fdfa}.step-item.active{border-color:#5eead4;background:#ecfdf5;box-shadow:0 0 0 2px rgba(20,184,166,.08)}.step-item span{font-size:11px;font-weight:800}.step-item strong{color:#0f766e;font-size:10px}.step-item small{grid-column:1/-1;overflow:hidden;color:#94a3b8;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.image-panel{display:grid;grid-template-rows:auto 1fr}.image-stage{position:relative;display:grid;place-items:center;min-height:600px;padding:16px;background:#0b1220;overflow:hidden;touch-action:none}.image-stage img{display:block;max-width:100%;max-height:100%;object-fit:contain}.overlay{position:absolute;width:calc(100% - 32px);height:calc(100% - 32px);pointer-events:none}.bbox{fill:rgba(20,184,166,.18);stroke:#14b8a6;stroke-width:8}.bbox-handle{fill:#fff;stroke:#0f766e;stroke-width:5;pointer-events:auto}.action-point{fill:#f59e0b;stroke:#fff;stroke-width:8}.swipe-line{stroke:#f59e0b;stroke-width:12;stroke-linecap:round}.image-caption{position:absolute;right:12px;bottom:12px;left:12px;padding:8px;border-radius:8px;background:rgba(2,6,23,.78);color:#e2e8f0;font-size:11px;text-align:center;pointer-events:none}.empty-image{color:#94a3b8;font-size:13px}.detail-panel{overflow:auto}.identity,.field-card,.bbox-card,.compare-card,.history-card{margin:12px;padding:11px;border:1px solid #e5e7eb;border-radius:10px;background:#f8fafc}.identity{display:grid;gap:5px}.identity span,.identity label{color:#64748b;font-size:10px;font-weight:800}.identity b{color:#0f766e;font-size:12px}.identity p{margin:0;color:#334155;font-size:12px;line-height:1.5}.field-card,.bbox-card,.history-card{display:grid;gap:8px}.field-card :deep(textarea){font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px}.bbox-card .card-head{display:flex;justify-content:space-between;gap:8px;align-items:center}.bbox-card code{color:#0f766e;font-size:11px}.bbox-fields{display:grid;grid-template-columns:1fr 1fr;gap:7px}.bbox-fields :deep(.el-input-number){width:100%}.muted{color:#94a3b8!important}.compare-card{display:grid;gap:10px}.compare-card>div{padding-bottom:8px;border-bottom:1px dashed #dbe3ed}.compare-card>div:last-child{padding-bottom:0;border-bottom:0}.compare-card p{margin:4px 0 0;color:#334155;font-size:12px;line-height:1.6;white-space:pre-wrap}.history-card pre{max-height:120px;margin:0;overflow:auto;color:#475569;font:11px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap}.detail-actions{position:sticky;bottom:0;padding:12px;background:linear-gradient(transparent,#fff 18%)}.detail-actions .el-button{width:100%}.done{color:#16a34a}@media(max-width:1150px){.workspace{grid-template-columns:210px minmax(300px,1fr)}.detail-panel{grid-column:1/-1}.image-stage{min-height:520px}}@media(max-width:700px){.toolbar-field,.toolbar-field .el-select{width:100%}.toolbar>.el-button--primary{margin-left:0}.workspace{grid-template-columns:1fr}.step-panel{max-height:300px}.image-stage{min-height:430px}.detail-panel{grid-column:auto}}
.identity-meta{color:#94a3b8;font-size:11px}.original-value{display:grid;gap:3px;padding:7px 8px;border-radius:7px;background:#fff;color:#64748b;font-size:10px}.original-value code{overflow:auto;color:#64748b;font:10px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap}.image-frame{position:relative;display:inline-block;max-width:100%;max-height:100%;line-height:0}.image-frame img{display:block;max-width:100%;max-height:568px;width:auto;height:auto;object-fit:contain}.image-frame .overlay{position:absolute;inset:0;width:100%;height:100%}
.action-readonly{display:block;max-width:100%;padding:8px 10px;border:1px solid #dbe3ed;border-radius:8px;background:#fff;color:#334155;font:11px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.original-value code{display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.bbox-help{padding:8px 9px;border-radius:8px;background:#ecfdf5;color:#0f766e;font-size:11px;line-height:1.5}
.trajectory-heading small{display:none}.step-item{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center}.step-item span:first-child{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.step-check{display:grid;place-items:center;width:18px;height:18px;border-radius:50%;background:#dcfce7;color:#15803d;font-size:12px;font-weight:900}.workspace{grid-template-columns:220px minmax(280px,360px) minmax(560px,1fr)}.image-stage{height:clamp(420px,68vh,680px);min-height:420px;padding:10px;background:transparent;overflow:auto}.image-stage :deep(.action-image){min-height:0}.image-stage :deep(.action-image img){max-height:620px;border-radius:10px}.source-line{color:#64748b;font-size:10px}.result-head{display:flex;align-items:center;justify-content:space-between;gap:10px}.editable-result :deep(textarea){font-size:12px;line-height:1.55}.inline-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:8px}.detail-actions{display:flex}.detail-actions .el-button{width:100%}@media(max-width:1150px){.workspace{grid-template-columns:210px minmax(280px,360px)}.detail-panel{grid-column:1/-1}.image-stage{height:clamp(420px,65vh,620px);min-height:420px}}@media(max-width:700px){.workspace{grid-template-columns:1fr}.image-stage{height:auto;min-height:360px}.detail-panel{grid-column:auto}}
</style>
