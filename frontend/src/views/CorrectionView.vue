<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, correctionAssetUrl, correctionDownloadUrl } from '@/api'
import type { CorrectionBatch, CorrectionGroup, CorrectionGroupSummary, CorrectionRecommendation, CorrectionRow, CorrectionSession } from '@/types'
import CorrectionActionEditor from '@/components/CorrectionActionEditor.vue'

const route = useRoute(), router = useRouter()
const batches = ref<CorrectionBatch[]>([])
const selectedTreeRunId = ref('')
const recommendation = ref<CorrectionRecommendation | null>(null)
const sessionHistory = ref<CorrectionSession[]>([])
const currentSession = ref<CorrectionSession | null>(null)
const groups = ref<CorrectionGroupSummary[]>([])
const activeGroup = ref<CorrectionGroup | null>(null)
const activeRowId = ref<number | null>(null)
const sopDraft = ref('')
const loadingBatches = ref(false)
const loadingContent = ref(false)
const loadingSession = ref(false)
const loadingGroup = ref(false)
const busy = ref(false)
const savingActionRow = ref<number | null>(null)
const savingSopRow = ref<number | null>(null)
const savingDeleteRow = ref<number | null>(null)
const savingExportGroup = ref<string | null>(null)
const batchLoadToken = ref(0)
const groupCache = ref<Record<string, CorrectionGroup>>({})

const activeRow = computed<CorrectionRow | null>(() => activeGroup.value?.rows.find((row) => row.excel_row === activeRowId.value) ?? null)
const activeRows = computed(() => activeGroup.value?.rows.filter((row) => !row.deleted) ?? [])
const editedCount = computed(() => groups.value.reduce((sum, group) => sum + group.edited_row_count, 0))
const selectedBatch = computed(() => batches.value.find((batch) => batch.tree_run_id === selectedTreeRunId.value) ?? null)
const batchSessions = computed(() => sessionHistory.value.filter((session) => session.tree_run_id === selectedTreeRunId.value))
const existingBatchSession = computed(() => batchSessions.value[0] ?? null)
const currentFlowStep = computed(() => currentSession.value ? 4 : recommendation.value?.status === 'ready' ? 3 : 2)

function flowNodeClass(step: number) {
  return {
    'flow-node--active': currentFlowStep.value === step,
    'flow-node--done': currentFlowStep.value > step,
  }
}

function setCurrentSession(session: CorrectionSession) {
  currentSession.value = session
  groups.value = session.groups
  activeGroup.value = null
  activeRowId.value = null
  sopDraft.value = ''
  groupCache.value = {}
}

async function loadBatches() {
  loadingBatches.value = true
  try {
    const result = await api.correctionBatches()
    batches.value = result.batches
    const requested = typeof route.query.tree_run_id === 'string' ? route.query.tree_run_id : ''
    selectedTreeRunId.value = batches.value.some((batch) => batch.tree_run_id === requested)
      ? requested
      : result.default_tree_run_id || batches.value[0]?.tree_run_id || ''
  } finally {
    loadingBatches.value = false
  }
}

async function loadSessions() {
  sessionHistory.value = await api.correctionSessions()
}

function selectionForGroup(group: CorrectionGroupSummary | CorrectionGroup) {
  return currentSession.value?.selection.tasks.find((task) => task.trajectory_id === group.meta_task) ?? null
}

async function loadRecommendation() {
  if (!selectedTreeRunId.value) {
    recommendation.value = { status: 'blocked', message: '请先完成一个批次的轨迹质检，再进入专家动作纠偏', tasks: [] }
    return recommendation.value
  }
  try {
    recommendation.value = await api.correctionRecommendation(selectedTreeRunId.value)
  } catch (error) {
    recommendation.value = { status: 'blocked', message: (error as Error).message, tasks: [] }
  }
  return recommendation.value
}

function resetWorkspace() {
  currentSession.value = null
  groups.value = []
  activeGroup.value = null
  activeRowId.value = null
  sopDraft.value = ''
  groupCache.value = {}
}

async function loadSelectedBatch(refreshSessions = true) {
  const token = ++batchLoadToken.value
  resetWorkspace()
  recommendation.value = null
  loadingContent.value = true
  try {
    const [result] = await Promise.all([
      loadRecommendation(),
      refreshSessions ? loadSessions() : Promise.resolve(),
    ])
    if (token !== batchLoadToken.value || result.status !== 'ready') return
    const existing = existingBatchSession.value
    if (existing) {
      await openSession(existing.session_id)
    } else {
      await createSession()
    }
  } finally {
    if (token === batchLoadToken.value) loadingContent.value = false
  }
}

async function switchBatch(treeRunId: string) {
  if (!treeRunId) return
  selectedTreeRunId.value = treeRunId
  await router.replace({ query: { tree_run_id: treeRunId } })
  await loadSelectedBatch()
}

async function openSession(sessionId: string) {
  if (!sessionId) return
  loadingSession.value = true
  try {
    const session = await api.correctionSession(sessionId)
    if (session.tree_run_id !== selectedTreeRunId.value) return
    setCurrentSession(session)
    const first = groups.value[0]
    if (first) await openGroup(first.group_id)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loadingSession.value = false
  }
}

async function createSession() {
  const current = recommendation.value
  if (!current || current.status !== 'ready' || !selectedTreeRunId.value) return ElMessage.warning(current?.message || '请先完成轨迹质检')
  loadingSession.value = true
  try {
    const session = await api.createCorrectionSession(selectedTreeRunId.value)
    if (session.tree_run_id !== selectedTreeRunId.value) return
    setCurrentSession(session)
    const first = groups.value[0]
    await Promise.all([
      loadSessions(),
      first ? openGroup(first.group_id) : Promise.resolve(),
    ])
  } catch (error) {
    ElMessage.error((error as Error).message)
    await loadRecommendation()
  } finally {
    loadingSession.value = false
  }
}

async function openGroup(groupId: string) {
  if (!currentSession.value) return
  const cacheKey = `${currentSession.value.session_id}:${groupId}`
  const cached = groupCache.value[cacheKey]
  if (cached) {
    activeGroup.value = cached
    const first = cached.rows.find((row) => !row.deleted) ?? cached.rows[0]
    selectRow(first ?? null)
    return
  }
  loadingGroup.value = true
  try {
    const group = await api.correctionGroup(currentSession.value.session_id, groupId)
    groupCache.value[cacheKey] = group
    activeGroup.value = group
    const first = activeRows.value[0] ?? activeGroup.value.rows[0]
    selectRow(first ?? null)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loadingGroup.value = false
  }
}

function selectRow(row: CorrectionRow | null) {
  activeRowId.value = row?.excel_row ?? null
  sopDraft.value = row?.sop ?? ''
}

function rowClassName({ row }: { row: CorrectionRow }) {
  return row.deleted ? 'is-deleted' : ''
}

function onStepRowClick(row: CorrectionRow) {
  selectRow(row)
}

function updateGroupSummary(value: CorrectionGroupSummary) {
  groups.value = groups.value.map((group) => group.group_id === value.group_id ? value : group)
  if (currentSession.value) currentSession.value.groups = groups.value
}

function cacheActiveGroup(group: CorrectionGroup) {
  if (!currentSession.value) return
  groupCache.value[`${currentSession.value.session_id}:${group.group_id}`] = group
}

async function saveAction(actions: string) {
  if (!currentSession.value || !activeRow.value) return
  const rowId = activeRow.value.excel_row
  savingActionRow.value = rowId
  try {
    const result = await api.patchCorrectionRow(currentSession.value.session_id, rowId, { actions })
    updateGroupSummary(result.group)
    if (activeGroup.value) {
      const updated = { ...activeGroup.value, ...result.group, rows: activeGroup.value.rows.map((row) => row.excel_row === result.row.excel_row ? result.row : row) }
      activeGroup.value = updated
      cacheActiveGroup(updated)
    }
    ElMessage.success(`第 ${result.row.step} 步动作已保存`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    savingActionRow.value = null
  }
}

async function saveSop() {
  if (!currentSession.value || !activeRow.value || sopDraft.value === activeRow.value.sop) return
  const rowId = activeRow.value.excel_row
  savingSopRow.value = rowId
  try {
    const result = await api.patchCorrectionRow(currentSession.value.session_id, rowId, { sop: sopDraft.value })
    updateGroupSummary(result.group)
    if (activeGroup.value) {
      const updated = { ...activeGroup.value, ...result.group, rows: activeGroup.value.rows.map((row) => row.excel_row === result.row.excel_row ? result.row : row) }
      activeGroup.value = updated
      cacheActiveGroup(updated)
    }
    ElMessage.success(`第 ${result.row.step} 步 SOP 已保存`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    savingSopRow.value = null
  }
}

async function toggleExport(group: CorrectionGroupSummary) {
  if (!currentSession.value) return
  savingExportGroup.value = group.group_id
  try {
    const updated = await api.patchCorrectionExport(currentSession.value.session_id, group.group_id, !group.export)
    updateGroupSummary(updated)
    if (activeGroup.value?.group_id === updated.group_id) {
      const next = { ...activeGroup.value, ...updated }
      activeGroup.value = next
      cacheActiveGroup(next)
    }
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    savingExportGroup.value = null
  }
}

async function toggleDeleted(row: CorrectionRow) {
  if (!currentSession.value) return
  if (!row.deleted) {
    try {
      await ElMessageBox.confirm(`确认删除第 ${row.step} 步吗？导出时不会包含该行。`, '删除步骤', { type: 'warning' })
    } catch {
      return
    }
  }
  savingDeleteRow.value = row.excel_row
  try {
    const result = await api.patchCorrectionRow(currentSession.value.session_id, row.excel_row, { deleted: !row.deleted })
    updateGroupSummary(result.group)
    if (activeGroup.value) {
      const updated = { ...activeGroup.value, ...result.group, rows: activeGroup.value.rows.map((item) => item.excel_row === result.row.excel_row ? result.row : item) }
      activeGroup.value = updated
      cacheActiveGroup(updated)
    }
    selectRow(result.row)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    savingDeleteRow.value = null
  }
}

async function exportData() {
  if (!currentSession.value) return
  busy.value = true
  try {
    const result = await api.correctionExport(currentSession.value.session_id)
    currentSession.value.exports.unshift(result)
    window.open(correctionDownloadUrl(currentSession.value.session_id, result.filename), '_blank')
    ElMessage.success(`导出完成：${result.filename}`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  try {
    await Promise.all([loadBatches(), loadSessions()])
    await loadSelectedBatch(false)
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
})
</script>

<template>
  <div class="page correction-page">
    <header class="page-hero">
      <div><span class="eyebrow">EXPERT ACTION CORRECTION</span><h1>专家动作纠偏</h1><p>基于 human8.0.py 的 Excel 精修工作台：查看截图、修正 Action/SOP、删除异常步骤，并按 SFT/RL/原生数据分流导出。</p></div>
      <div class="hero-metrics"><div><b>{{ currentSession?.group_count ?? 0 }}</b><span>任务组</span></div><div><b>{{ currentSession?.row_count ?? 0 }}</b><span>步骤</span></div><div><b>{{ editedCount }}</b><span>已编辑</span></div></div>
    </header>

    <section class="flow-card">
      <div class="flow-head">
        <div><span class="eyebrow">QUALITY-GATED CORRECTION</span><h2>按质检批次修正 Top-1</h2><p>每次成功建树形成一个批次；选择已质检批次后，每个已完成质检的任务只保留 global_score 最高的一条轨迹。</p></div>
        <el-tag v-if="loadingContent || loadingSession" type="warning" effect="plain">正在准备修正工作区…</el-tag>
        <el-tag v-else-if="currentSession" type="success" effect="plain">已自动加载，可直接编辑</el-tag>
      </div>
      <div class="flow-line"><div class="flow-node" :class="flowNodeClass(1)"><b>1</b><span>轨迹树构建</span></div><i>→</i><div class="flow-node" :class="flowNodeClass(2)"><b>2</b><span>轨迹质检</span></div><i>→</i><div class="flow-node" :class="flowNodeClass(3)"><b>3</b><span>Top-1 选择</span></div><i>→</i><div class="flow-node" :class="flowNodeClass(4)"><b>4</b><span>人工修正</span></div></div>
      <div class="batch-picker"><label>可修正批次</label><el-select v-model="selectedTreeRunId" :loading="loadingBatches" :disabled="loadingBatches || !batches.length" placeholder="选择已质检批次" style="width:330px" @change="switchBatch"><el-option v-for="batch in batches" :key="batch.tree_run_id" :label="`${batch.tree_run_id} · 已质检 ${batch.reviewed_task_count}/${batch.total_task_count} 个任务`" :value="batch.tree_run_id" /></el-select><span v-if="selectedBatch">建树：{{ selectedBatch.tree_completed_at }} · 质检：{{ selectedBatch.quality_completed_at }}</span></div>
      <el-alert v-if="recommendation?.status === 'blocked'" :title="recommendation.message || '请先完成轨迹质检，再进入专家动作纠偏'" type="warning" :closable="false" />
      <template v-else-if="recommendation?.status === 'ready'">
        <div class="run-meta"><span>轨迹树批次：<code>{{ recommendation.tree_run_id }}</code></span><span>建树完成：{{ recommendation.tree_completed_at }}</span><span>质检完成：{{ recommendation.quality_completed_at }}</span><span>已质检任务：{{ recommendation.reviewed_task_count }}/{{ recommendation.total_task_count }}</span></div>
        <el-alert v-if="recommendation.reviewed_task_count !== recommendation.total_task_count" title="该批次还有部分任务未完成质检，当前仅展示已质检任务。" type="info" :closable="false" />
        <div class="top1-list"><div v-for="task in recommendation.tasks" :key="task.task_id" class="top1-item"><div><b>{{ task.task_id }}</b><span>{{ task.goal }}</span></div><div class="top1-score"><small>Top-1 · {{ task.trajectory_id }}</small><strong>{{ task.global_score.toFixed(4) }}</strong><el-tag size="small" :type="task.passed_threshold ? 'success' : 'warning'">{{ task.passed_threshold ? '已通过' : '未通过' }}</el-tag><small>{{ task.step_count }} 步 / 原始 {{ task.trajectory_count }} 条</small></div></div></div>
      </template>
      <div v-if="existingBatchSession && !currentSession" class="draft-hint">该批次已有修正草稿，数据准备完成后将自动恢复。</div>
    </section>

    <section class="session-toolbar"><div><b>{{ currentSession ? `Top-1 草稿 ${currentSession.session_id}` : '修正工作区' }}</b><span v-if="currentSession">轨迹树批次：{{ currentSession.tree_run_id }} · 最后保存：{{ currentSession.updated_at }} · 原文件不会被覆盖</span><span v-else>选择批次后自动加载 Top-1；修正草稿只保存在会话文件中。</span></div><el-button v-if="currentSession" type="success" :loading="busy" @click="exportData">导出 SFT / RL / 原生数据</el-button></section>
    <section class="correction-layout">
      <aside class="group-panel">
        <div class="panel-title"><b>Top-1 任务组</b><span>{{ groups.length }}</span></div>
        <div v-if="loadingContent && !groups.length" class="panel-skeleton"><i></i><i></i><i></i></div>
        <el-empty v-else-if="!groups.length" :description="recommendation?.status === 'blocked' ? '完成质检后可进入修正' : '等待批次数据'" :image-size="80" />
        <template v-else>
        <button v-for="group in groups" :key="group.group_id" type="button" class="group-item" :class="{ active: group.group_id === activeGroup?.group_id }" @click="openGroup(group.group_id)">
          <div class="group-item__heading"><strong :title="group.task">{{ group.task }}</strong><el-tag size="small" :type="group.export ? 'success' : 'info'">{{ group.export ? '导出' : '待确认' }}</el-tag></div>
          <p :title="group.meta_task">{{ group.meta_task }}</p>
          <div class="group-item__meta"><span>{{ selectionForGroup(group)?.global_score.toFixed(4) ?? '—' }} 分</span><span>{{ group.active_row_count }}/{{ group.row_count }} 步</span><span v-if="group.edited_row_count">{{ group.edited_row_count }} 改</span></div>
          <el-button size="small" text :loading="savingExportGroup === group.group_id" @click.stop="toggleExport(group)">{{ group.export ? '设为丢弃' : '加入导出' }}</el-button>
        </button>
        </template>
      </aside>
      <section v-loading="loadingGroup" class="step-panel">
        <div v-if="activeGroup" class="group-header"><div><span>质检 Top-1 · {{ selectionForGroup(activeGroup)?.global_score.toFixed(4) ?? '—' }} 分</span><h2>{{ activeGroup.meta_task }}</h2><p>主任务：{{ activeGroup.task }}</p></div><el-tag :type="activeGroup.export ? 'success' : 'info'">{{ activeGroup.export ? '当前会导出' : '当前会丢弃' }}</el-tag></div>
        <el-table v-if="activeGroup" :data="activeGroup.rows" height="calc(100vh - 375px)" highlight-current-row :row-class-name="rowClassName" @row-click="onStepRowClick"><el-table-column prop="step" label="Step" width="70" /><el-table-column label="Action" min-width="200"><template #default="scope"><code class="action-text">{{ scope.row.actions }}</code></template></el-table-column><el-table-column prop="summary" label="Action Summary" min-width="180" show-overflow-tooltip /><el-table-column prop="sop" label="SOP" min-width="150" show-overflow-tooltip /><el-table-column label="状态" width="112"><template #default="scope"><el-tag v-if="scope.row.deleted" type="danger" size="small">已删除</el-tag><el-tag v-else-if="scope.row.edited" type="warning" size="small">{{ scope.row.edit_status }}</el-tag><span v-else>—</span></template></el-table-column><el-table-column label="操作" width="76" fixed="right"><template #default="scope"><el-button link type="danger" size="small" :loading="savingDeleteRow === scope.row.excel_row" @click.stop="toggleDeleted(scope.row)">{{ scope.row.deleted ? '恢复' : '删除' }}</el-button></template></el-table-column></el-table>
        <el-empty v-else :description="loadingContent || loadingSession ? '正在准备步骤列表…' : '选择左侧任务组后查看步骤'" :image-size="100" />
      </section>
      <aside class="detail-panel">
        <template v-if="activeRow && currentSession">
          <CorrectionActionEditor :row="activeRow" :image-url="correctionAssetUrl(currentSession.session_id, activeRow.image)" :saving="savingActionRow === activeRow.excel_row" @save="saveAction" />
          <div class="sop-editor"><label>SOP（离开输入框自动保存）</label><el-input v-model="sopDraft" type="textarea" :rows="4" placeholder="输入该步骤的 SOP" :disabled="savingSopRow === activeRow.excel_row" @blur="saveSop" /><el-button size="small" :loading="savingSopRow === activeRow.excel_row" @click="saveSop">保存 SOP</el-button></div>
          <div class="row-meta"><label>模型/人工质检字段</label><p>task_manual_result：{{ activeRow.task_manual_result || '—' }}</p><p>micro：{{ activeRow.micro_manual || activeRow.micro_pred || '—' }} · macro：{{ activeRow.macro_manual || activeRow.macro_pred || '—' }}</p><label>原始截图路径</label><code>{{ activeRow.image }}</code></div>
        </template>
        <div v-else class="detail-empty"><b>交互修正区</b><span>{{ loadingContent || loadingSession ? '正在准备图片和 Action 编辑器…' : '选择一个步骤后，在这里查看截图并修改 Action。' }}</span></div>
      </aside>
    </section>
    <section v-if="currentSession?.exports.length" class="export-history"><div class="panel-title"><b>导出历史</b><span>保留草稿与历史文件</span></div><a v-for="item in currentSession.exports" :key="item.export_id" :href="correctionDownloadUrl(currentSession.session_id, item.filename)" target="_blank" rel="noreferrer"><span>{{ item.filename }}</span><small>{{ item.created_at }} · {{ Object.entries(item.sheets).map(([name, count]) => `${name} ${count}`).join(' / ') }}</small></a></section>
  </div>
</template>

<style scoped>
.source-card,.session-toolbar{margin:20px 0 14px;padding:16px 18px;border:1px solid var(--line);border-radius:16px;background:rgba(255,255,255,.86)}.source-controls{display:flex;align-items:end;gap:12px;flex-wrap:wrap}.control-block{display:grid;gap:6px}.control-block label,.sop-editor label,.row-meta label{color:#64748b;font-size:11px;font-weight:850}.control-block small{float:right;margin-left:18px;color:#94a3b8}.upload-button{display:inline-flex;align-items:center;justify-content:center;min-height:32px;padding:0 15px;border:1px solid #99f6e4;border-radius:6px;background:#f0fdfa;color:#0f766e;font-size:13px;font-weight:800;cursor:pointer}.upload-button input{display:none}.session-picker{margin-left:auto}.source-hint{margin-top:12px;color:#64748b;font-size:11px}.source-hint code{color:#0f766e}.session-toolbar{display:flex;align-items:center;justify-content:space-between;gap:14px}.session-toolbar div{display:grid;gap:4px}.session-toolbar span{color:#64748b;font-size:12px}.draft-hint{margin-top:12px;color:#0f766e;font-size:12px}.correction-layout{display:grid;grid-template-columns:minmax(220px,230px) minmax(480px,1fr) minmax(560px,620px);gap:12px;min-height:calc(100vh - 345px);align-items:stretch}.group-panel,.step-panel,.detail-panel{min-width:0;border:1px solid var(--line);border-radius:16px;background:white;overflow:hidden}.group-panel{padding:12px;background:#f8fafc;overflow:auto}.panel-title{display:flex;align-items:center;justify-content:space-between;padding:5px 4px 12px;color:#0f172a}.panel-title span{color:#94a3b8;font-size:12px}.group-item{display:block;width:100%;margin-bottom:8px;padding:10px;border:1px solid #e2e8f0;border-radius:12px;background:white;text-align:left;color:#0f172a;cursor:pointer}.group-item:hover,.group-item.active{border-color:#5eead4;background:#f0fdfa}.group-item__heading{display:flex;align-items:center;justify-content:space-between;gap:6px}.group-item__heading strong,.group-item p{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.group-item p{margin:7px 0;color:#475569;font-size:12px;line-height:1.35}.group-item__meta{display:flex;justify-content:space-between;gap:5px;color:#94a3b8;font-size:10px}.group-item .el-button{padding:4px 0}.step-panel{display:flex;flex-direction:column;min-height:calc(100vh - 345px)}.group-header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:16px;border-bottom:1px solid var(--line)}.group-header span{color:#0f766e;font-size:11px;font-weight:900}.group-header h2{margin:5px 0 4px;font-size:20px}.group-header p{margin:0;color:#64748b;font-size:12px}.action-text{display:block;overflow:hidden;color:#334155;font-size:10px;line-height:1.3;white-space:normal;word-break:break-all}.detail-panel{padding:12px;background:#111827;color:#e2e8f0;overflow:auto;min-height:0;max-height:calc(100vh - 345px)}.detail-empty{display:grid;place-items:center;align-content:center;gap:9px;min-height:calc(100vh - 385px);padding:24px;text-align:center;color:#94a3b8;font-size:12px;line-height:1.5}.detail-empty b{color:#e2e8f0;font-size:15px}.panel-skeleton{display:grid;gap:10px;padding:8px 0}.panel-skeleton i{display:block;height:84px;border-radius:12px;background:linear-gradient(90deg,#e2e8f0 25%,#f8fafc 50%,#e2e8f0 75%);background-size:200% 100%;animation:correction-shimmer 1.3s infinite}.sop-editor{display:grid;gap:8px;margin-top:14px;padding-top:14px;border-top:1px solid #334155}.sop-editor label,.row-meta label{color:#94a3b8}.row-meta{display:grid;gap:6px;margin-top:14px;padding-top:14px;border-top:1px solid #334155;color:#cbd5e1;font-size:11px;line-height:1.45}.row-meta p{margin:0}.row-meta code{overflow-wrap:anywhere;color:#67e8f9}.export-history{display:grid;gap:8px;margin-top:14px;padding:14px 16px;border:1px solid var(--line);border-radius:16px;background:white}.export-history a{display:flex;justify-content:space-between;gap:14px;padding:10px;border-radius:9px;background:#f8fafc;color:#0f766e;font-size:12px}.export-history small{color:#64748b;text-align:right}.step-panel :deep(.is-deleted){opacity:.48;background:#fff1f2}.step-panel :deep(.el-table__row){cursor:pointer}@keyframes correction-shimmer{to{background-position:-200% 0}}@media(max-width:1240px){.correction-layout{grid-template-columns:minmax(210px,220px) minmax(440px,1fr) minmax(520px,560px);overflow-x:auto;padding-bottom:6px}.detail-panel{grid-column:auto;max-height:none}}@media(max-width:900px){.session-picker{margin-left:0}.correction-layout{grid-template-columns:1fr;overflow-x:visible}.group-panel{max-height:330px}.step-panel{min-height:500px}.detail-panel{min-height:500px;max-height:none}.detail-empty{min-height:420px}}
.flow-card{margin:20px 0 14px;padding:18px;border:1px solid #99f6e4;border-radius:16px;background:linear-gradient(135deg,#f0fdfa,#f8fafc)}.flow-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}.flow-head h2{margin:6px 0 4px;font-size:22px}.flow-head p{margin:0;color:#64748b;font-size:12px}.flow-line{display:flex;align-items:center;gap:10px;margin:18px 0;padding:12px;border-radius:12px;background:rgba(255,255,255,.75)}.flow-line i{color:#94a3b8;font-style:normal;font-size:18px}.flow-node{display:flex;align-items:center;gap:7px;color:#475569;font-size:12px;font-weight:800}.flow-node b{display:grid;place-items:center;width:24px;height:24px;border-radius:50%;background:#cbd5e1;color:#334155;font-size:11px}.flow-node--active{color:#0f766e}.flow-node--active b{background:#5eead4;color:#134e4a}.flow-node--done{color:#0f766e}.flow-node--done b{background:#ccfbf1;color:#0f766e}.run-meta{display:flex;flex-wrap:wrap;gap:18px;margin:12px 0;color:#64748b;font-size:11px}.run-meta code{color:#0f766e}.top1-list{display:grid;gap:8px}.top1-item{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:10px 12px;border:1px solid #ccfbf1;border-radius:10px;background:rgba(255,255,255,.8)}.top1-item>div:first-child{display:grid;gap:3px;min-width:0}.top1-item span{overflow:hidden;color:#64748b;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.top1-score{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end}.top1-score small{color:#64748b;font-size:10px}.top1-score strong{color:#0f766e;font-size:20px}.flow-card .session-picker{display:flex;align-items:center;gap:10px;margin-top:14px}.flow-card .session-picker label{color:#64748b;font-size:11px;font-weight:850}@media(max-width:700px){.flow-head{display:grid}.flow-line{overflow:auto}.flow-line i{flex:0 0 auto}.flow-node{white-space:nowrap}.top1-item{align-items:flex-start;display:grid}.top1-score{justify-content:flex-start}.flow-card .session-picker{align-items:flex-start;display:grid}}
.batch-picker{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:16px 0 12px}.batch-picker label{color:#64748b;font-size:11px;font-weight:850}.batch-picker>span{color:#64748b;font-size:11px}.batch-picker :deep(.el-select){max-width:100%}
</style>
