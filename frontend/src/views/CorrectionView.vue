<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { api, correctionDownloadUrl } from '@/api'
import type { CorrectionBatch, CorrectionRow } from '@/types'
import CorrectionWorkbench from '@/components/CorrectionWorkbench.vue'
import { useCorrectionWorkspace, type ActionDecision } from '@/composables/useCorrectionWorkspace'

const route = useRoute(), router = useRouter()
const batches = ref<CorrectionBatch[]>([])
const selectedTreeRunId = ref('')
const loading = ref(true)
const pageError = ref('')
const actionPrompt = ref(false)
let resolveDecision: ((decision: ActionDecision) => void) | null = null
let batchRequest = 0
let disposed = false
const ws = useCorrectionWorkspace(api, {
  error: (message) => ElMessage.error(message),
  actionDecision: () => new Promise((resolve) => { resolveDecision = resolve; actionPrompt.value = true }),
})
const { session, tasks, expandedTasks, openGroupId, activeGroup, activeRow, actionDraft, actionRevision, loadingGroup, groupError, savingCount, busy, hasUnsaved } = ws
const visibleExpandedTasks = ref<string[]>([])
const selectedBatch = computed(() => batches.value.find((batch) => batch.tree_run_id === selectedTreeRunId.value))
const trajectoryCount = computed(() => tasks.value.reduce((sum, task) => sum + task.trajectories.length, 0))
const editedCount = computed(() => tasks.value.reduce((sum, task) => sum + task.edited_row_count, 0))
const exportCount = computed(() => tasks.value.reduce((sum, task) => sum + task.export_count, 0))

function decideAction(decision: ActionDecision) {
  actionPrompt.value = false
  resolveDecision?.(decision)
  resolveDecision = null
}

async function loadBatch(treeRunId: string) {
  const request = ++batchRequest
  selectedTreeRunId.value = treeRunId
  ws.setSession(null)
  visibleExpandedTasks.value = []
  loading.value = true
  pageError.value = ''
  try {
    if (!treeRunId) { pageError.value = '请先完成轨迹质检，再进入专家动作纠偏。'; return }
    const sessions = await api.correctionSessions()
    if (disposed || request !== batchRequest) return
    const existing = sessions.find((item) => item.tree_run_id === treeRunId)
    if (existing) {
      const saved = await api.correctionSession(existing.session_id)
      if (!disposed && request === batchRequest) ws.setSession(saved)
    } else {
      const recommendation = await api.correctionRecommendation(treeRunId)
      if (disposed || request !== batchRequest) return
      if (recommendation.status !== 'ready') { pageError.value = recommendation.message || '当前批次尚无可修正轨迹'; return }
      const created = await api.createCorrectionSession(treeRunId)
      if (!disposed && request === batchRequest) ws.setSession(created)
    }
  } catch (error) {
    if (!disposed && request === batchRequest) pageError.value = (error as Error).message
  } finally {
    if (!disposed && request === batchRequest) loading.value = false
  }
}

async function onTaskCollapse(next: string[]) {
  const current = visibleExpandedTasks.value
  const removed = current.find((taskId) => !next.includes(taskId))
  const added = next.find((taskId) => !current.includes(taskId))
  if (removed) await ws.toggleTask(removed)
  else if (added) await ws.toggleTask(added)
  visibleExpandedTasks.value = [...expandedTasks.value]
}

async function onTrajectoryCollapse(next: string | string[]) {
  const nextGroupId = Array.isArray(next) ? next[0] || '' : next
  const currentGroupId = openGroupId.value
  if (nextGroupId === currentGroupId) await ws.toggleTrajectory(currentGroupId)
  else if (nextGroupId) await ws.toggleTrajectory(nextGroupId)
}

async function changeBatch(treeRunId: string) {
  if (treeRunId === selectedTreeRunId.value) return
  await router.replace({ query: { ...route.query, tree_run_id: treeRunId } })
}

async function toggleDeleted(row: CorrectionRow) {
  await ws.toggleDeleted(row, async () => {
    try {
      await ElMessageBox.confirm(`确认删除第 ${row.step} 步吗？导出时不会包含该行。`, '删除步骤', { type: 'warning' })
      return true
    } catch { return false }
  })
}

async function exportData() {
  const sessionId = session.value?.session_id
  const result = await ws.exportData()
  if (result && sessionId) {
    window.open(correctionDownloadUrl(sessionId, result.filename), '_blank', 'noopener')
    ElMessage.success('导出完成，文件也可从导出历史下载')
  }
}

function beforeUnload(event: BeforeUnloadEvent) {
  if (hasUnsaved.value || busy.value) { event.preventDefault(); event.returnValue = '' }
}
onBeforeRouteLeave(() => ws.prepareTransition())
onBeforeRouteUpdate((to, from) => to.query.tree_run_id !== from.query.tree_run_id ? ws.prepareTransition() : true)
watch(() => route.query.tree_run_id, (value) => {
  const runId = typeof value === 'string' ? value : batches.value.find((batch) => batch.is_default)?.tree_run_id || batches.value[0]?.tree_run_id || ''
  if (runId !== selectedTreeRunId.value) void loadBatch(runId)
})
onMounted(async () => {
  window.addEventListener('beforeunload', beforeUnload)
  try {
    const result = await api.correctionBatches()
    if (disposed) return
    batches.value = result.batches
    const requested = typeof route.query.tree_run_id === 'string' ? route.query.tree_run_id : ''
    await loadBatch(requested || result.default_tree_run_id || batches.value[0]?.tree_run_id || '')
  } catch (error) { if (!disposed) { pageError.value = (error as Error).message; loading.value = false } }
})
onBeforeUnmount(() => {
  disposed = true
  ++batchRequest
  decideAction('cancel')
  ws.setSession(null)
  window.removeEventListener('beforeunload', beforeUnload)
})
</script>

<template>
  <div class="page correction-page">
    <header class="page-hero">
      <div><span class="eyebrow">EXPERT ACTION CORRECTION</span><h1>专家动作纠偏</h1><p>展开任务与轨迹，直接在截图上修正 Action。当前每个任务选择质检 Top-1。</p></div>
      <div class="hero-metrics"><div><b>{{ tasks.length }}</b><span>任务</span></div><div><b>{{ trajectoryCount }}</b><span>入选轨迹</span></div><div><b>{{ editedCount }}</b><span>已修改步骤</span></div></div>
    </header>

    <section class="toolbar">
      <label for="correction-batch">质检批次</label>
      <el-select id="correction-batch" :model-value="selectedTreeRunId" :loading="loading" :disabled="loading || busy || !batches.length" placeholder="选择已质检批次" @change="changeBatch">
        <el-option v-for="batch in batches" :key="batch.tree_run_id" :label="`${batch.tree_run_id} · 已质检 ${batch.reviewed_task_count}/${batch.total_task_count} 个任务`" :value="batch.tree_run_id" />
      </el-select>
      <span class="save-status" role="status">{{ savingCount ? '正在保存…' : hasUnsaved ? '有未保存修改' : session ? '草稿已加载' : '' }}</span>
      <el-button type="success" :loading="savingCount > 0" :disabled="!session || busy || loading" @click="exportData">导出 SFT / RL / 原生数据（{{ exportCount }} 条）</el-button>
    </section>
    <el-alert v-if="pageError" :title="pageError" type="warning" :closable="false" show-icon />
    <el-alert v-else-if="selectedBatch && selectedBatch.reviewed_task_count < selectedBatch.total_task_count" title="该批次仅部分任务完成质检，当前展示可修正的任务。" type="info" :closable="false" show-icon />

    <section v-loading="loading" class="task-list" aria-label="修正任务列表">
      <el-empty v-if="!loading && !tasks.length" :description="pageError ? '暂无可修正任务' : '该草稿没有入选轨迹'" :image-size="90" />
      <el-collapse v-else :model-value="visibleExpandedTasks" @change="onTaskCollapse">
        <el-collapse-item v-for="task in tasks" :key="task.task_id" :name="task.task_id">
          <template #title>
            <div class="task-title">
              <div class="task-title__id">{{ task.task_id }}</div>
              <div class="task-title__goal" :title="task.goal">{{ task.goal }}</div>
              <el-tag type="success" round>已选轨迹</el-tag>
              <span class="task-title__stats">{{ task.trajectories.length }} 轨迹 · 已改 {{ task.edited_row_count }} 步 · 导出 {{ task.export_count }}/{{ task.trajectories.length }}</span>
            </div>
          </template>
          <div class="trajectory-list">
            <el-collapse :model-value="openGroupId" accordion @change="onTrajectoryCollapse">
              <el-collapse-item v-for="trajectory in task.trajectories" :key="trajectory.group.group_id" :name="trajectory.group.group_id">
                <template #title>
                  <div class="trajectory-title">
                    <el-icon><Refresh /></el-icon>
                    <b>{{ trajectory.trajectory_id }}</b>
                    <span class="rank">Top-{{ trajectory.rank }}</span>
                    <span class="score">{{ trajectory.global_score.toFixed(4) }} 分</span>
                    <el-tag size="small" :type="trajectory.passed_threshold ? 'success' : 'warning'">{{ trajectory.passed_threshold ? '质检通过' : '质检未通过' }}</el-tag>
                    <span>{{ trajectory.group.active_row_count }}/{{ trajectory.group.row_count }} 步 · 已改 {{ trajectory.group.edited_row_count }} 步</span>
                    <el-button class="trajectory-export" size="small" :type="trajectory.group.export ? 'success' : 'default'" plain :disabled="busy" @click.stop="ws.toggleExport(trajectory.group)">{{ trajectory.group.export ? '取消导出' : '加入导出' }}</el-button>
                  </div>
                </template>
                <div v-if="openGroupId === trajectory.group.group_id" v-loading="loadingGroup" class="trajectory-detail">
                  <div v-if="loadingGroup" class="inline-loading" role="status">正在加载轨迹步骤…</div>
                  <div v-else-if="groupError" class="inline-error"><el-alert :title="groupError" type="error" :closable="false" /><el-button @click="ws.loadGroup(trajectory.group.group_id)">重新加载轨迹</el-button></div>
                  <CorrectionWorkbench v-else-if="activeGroup && session" :session-id="session.session_id" :group="activeGroup" :row="activeRow" :revision="actionRevision" :saving="busy"
                    @select="ws.chooseRow" @delete="toggleDeleted" @action="ws.saveAction" @draft="actionDraft = $event" />
                </div>
              </el-collapse-item>
            </el-collapse>
          </div>
        </el-collapse-item>
      </el-collapse>
    </section>

    <details v-if="session?.exports.length" class="export-history"><summary>导出历史（{{ session.exports.length }}）</summary><a v-for="item in session.exports" :key="item.export_id" :href="correctionDownloadUrl(session.session_id, item.filename)" target="_blank" rel="noreferrer"><span>{{ item.filename }}</span><small>{{ item.created_at }} · {{ Object.entries(item.sheets).map(([name, count]) => `${name} ${count}`).join(' / ') }}</small></a></details>
    <el-dialog :model-value="actionPrompt" title="动作尚未保存" width="min(440px, 92vw)" :close-on-click-modal="false" :before-close="() => decideAction('cancel')">
      <p>当前步骤的 Action 有修改，请选择如何处理。</p>
      <template #footer><el-button @click="decideAction('cancel')">取消</el-button><el-button @click="decideAction('discard')">放弃修改</el-button><el-button type="primary" @click="decideAction('save')">保存后继续</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:20px 0 10px;padding:14px 16px;border:1px solid var(--line);border-radius:12px;background:white}.toolbar label{font-size:12px;font-weight:800;color:#64748b}.toolbar .el-select{width:330px;max-width:100%}.save-status{margin-left:auto;color:#64748b;font-size:12px}.export-history summary{cursor:pointer;font-weight:700;padding:8px 0}.task-list{min-height:180px;margin-top:16px}.task-title{display:grid;grid-template-columns:190px minmax(240px,1fr) auto minmax(220px,auto);align-items:center;gap:14px;width:calc(100% - 36px);padding-right:16px}.task-title__id{font-weight:900;letter-spacing:.02em}.task-title__goal{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#334155}.task-title__stats{color:var(--muted);font-size:12px;text-align:right;white-space:nowrap}.trajectory-list{padding:10px 14px 18px 46px;min-height:80px}.trajectory-title{display:flex;align-items:center;gap:9px;width:calc(100% - 36px);padding-right:16px}.trajectory-title b{overflow:hidden;color:#334155;font-size:12px;text-overflow:ellipsis;white-space:nowrap}.trajectory-title span{color:var(--muted);font-size:12px;font-weight:400;white-space:nowrap}.trajectory-title .rank{color:#0f766e;font-weight:800}.trajectory-title .score{font-variant-numeric:tabular-nums}.trajectory-export{margin-left:auto;flex-shrink:0}.trajectory-detail{min-height:160px}.inline-loading,.inline-error{padding:24px;color:#64748b;font-size:13px}.inline-error{display:grid;gap:12px}.inline-error .el-button{justify-self:start}.export-history{margin-top:16px;padding:8px 16px;border:1px solid var(--line);border-radius:12px;background:white;font-size:12px;color:#64748b}.export-history a{display:flex;justify-content:space-between;gap:12px;padding:12px 0;color:#0f766e;overflow-wrap:anywhere}.export-history small{color:#64748b}
@media(max-width:1000px){.task-title{grid-template-columns:190px minmax(180px,1fr) auto}.task-title__stats{text-align:left}.trajectory-list{padding-left:0}.trajectory-title{flex-wrap:wrap;gap:8px}.trajectory-export{margin-left:0}}@media(max-width:650px){.save-status{margin-left:0}.toolbar .el-select{width:100%}.task-title{grid-template-columns:1fr auto;width:calc(100% - 36px)}.task-title__goal{grid-column:1 / -1}.task-title__stats{text-align:left;white-space:normal}.trajectory-title{width:calc(100% - 36px);padding-right:8px}.export-history a{flex-direction:column}}
</style>
