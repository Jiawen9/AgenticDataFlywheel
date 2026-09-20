<script setup lang="ts">
import BatchPublishedNotice from '@/components/BatchPublishedNotice.vue'
import { useBatchLifecycle } from '@/composables/useBatchLifecycle'
import { activeBatchItems, eventMatchesRoute, withoutBatchQuery, type PublishedBatchEvent } from '@/utils/batchLifecycle'

import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { api, correctionDownloadUrl } from '@/api'
import type { CorrectionBatch, CorrectionRow } from '@/types'
import CorrectionWorkbench from '@/components/CorrectionWorkbench.vue'
import { resolveBatchSelection } from '@/utils/batchSelection'
import { useCorrectionWorkspace, type ActionDecision } from '@/composables/useCorrectionWorkspace'

const route = useRoute(), router = useRouter()
const batches = ref<CorrectionBatch[]>([])
const selectedBatchId = ref('')
const loading = ref(true)
const pageError = ref('')
const actionPrompt = ref(false)
let resolveDecision: ((decision: ActionDecision) => void) | null = null
let batchRequest = 0, routeSelectionRequest = 0
let initialized = false, choicesRequest = 0
let disposed = false
const ws = useCorrectionWorkspace(api, {
  error: (message) => ElMessage.error(message),
  actionDecision: () => new Promise((resolve) => { resolveDecision = resolve; actionPrompt.value = true }),
})
const { session, tasks, expandedTasks, openGroupId, activeGroup, activeRow, actionDraft, actionRevision, loadingGroup, groupError, savingCount, busy, hasUnsaved } = ws
const visibleExpandedTasks = ref<string[]>([])
const selectedBatch = computed(() => batches.value.find((batch) => batch.batch_id === selectedBatchId.value))
const trajectoryCount = computed(() => tasks.value.reduce((sum, task) => sum + task.trajectories.length, 0))
const editedCount = computed(() => tasks.value.reduce((sum, task) => sum + task.edited_row_count, 0))
const exportCount = computed(() => tasks.value.reduce((sum, task) => sum + task.export_count, 0))
const lifecycle = useBatchLifecycle({ currentBatch: () => selectedBatchId.value || String(route.query.batch_id ?? ''), onPublished, refreshChoices: loadChoices })
const { notice: publishedNotice } = lifecycle
function onPublished(event: PublishedBatchEvent) {
  const current = eventMatchesRoute(event, selectedBatchId.value, route.query)
  batches.value = batches.value.filter(batch => !event.batch_ids.includes(batch.batch_id))
  if (!current) return
  ++choicesRequest
  ++batchRequest; ++routeSelectionRequest; selectedBatchId.value = ''; ws.setSession(null); visibleExpandedTasks.value = []
  decideAction('cancel'); ElMessageBox.close(); loading.value = false; pageError.value = ''; publishedNotice.value = event
  void router.replace({ query: withoutBatchQuery(route.query) })
}
async function loadChoices() {
  const request = ++choicesRequest
  const [result, existing] = await Promise.all([api.correctionBatches(), api.correctionSessions()])
  if (disposed || request !== choicesRequest) return
  const merged = new Map(activeBatchItems(result.batches).map(batch => [batch.batch_id, batch]))
  for (const item of activeBatchItems(existing)) if (item.batch_id && !merged.has(item.batch_id)) merged.set(item.batch_id, { batch_id: item.batch_id, tree_run_id: item.tree_run_id, tree_completed_at: '', quality_completed_at: '', total_task_count: item.group_count, reviewed_task_count: 0, status: 'ready', is_default: false })
  batches.value = [...merged.values()]
  return result.default_batch_id ?? undefined
}

function decideAction(decision: ActionDecision) {
  actionPrompt.value = false
  resolveDecision?.(decision)
  resolveDecision = null
}

async function loadBatch(batchId: string) {
  const request = ++batchRequest
  selectedBatchId.value = batchId
  ws.setSession(null); visibleExpandedTasks.value = []; loading.value = true; pageError.value = ''
  try {
    if (!batchId) { pageError.value = '请先完成轨迹质检，再进入专家动作纠偏。'; return }
    const existing = (await api.correctionSessions()).find(item => item.batch_id === batchId)
    if (disposed || request !== batchRequest) return
    const saved = existing ? await api.correctionSession(existing.session_id) : await api.createCorrectionSession(batchId)
    if (!disposed && request === batchRequest) ws.setSession(saved)
  } catch (error) { if (!disposed && request === batchRequest) pageError.value = (error as Error).message }
  finally { if (!disposed && request === batchRequest) loading.value = false }
}
async function refreshBatch() {
  if (!await ws.prepareTransition()) return
  await loadBatch(selectedBatchId.value)
}
async function reviewGroup(groupId: string, decision: 'adopt' | 'discard') {
  if (await ws.reviewGroup(groupId, decision)) visibleExpandedTasks.value = [...expandedTasks.value]
}

const reviewFields: Record<string, string> = { actions: 'Action', actions_box: '动作框', sop: '操作说明', summary: 'Summary', thought: 'Thought', deleted: '删除状态' }
function reviewText(value: unknown) { return value === undefined || value === null ? '—' : typeof value === 'string' ? value : JSON.stringify(value) }
function currentReviewValue(stepKey: string, field: string) { return (activeGroup.value?.rows.find(row => row.step_key === stepKey) as unknown as Record<string, unknown> | undefined)?.[field] }
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

async function changeBatch(batchId: string) {
  if (batchId === selectedBatchId.value) return
  if (!await lifecycle.checkBatch(batchId)) return
  publishedNotice.value = null
  await router.replace({ query: { batch_id: batchId } })
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
  if (result && sessionId && session.value?.session_id === sessionId) {
    window.open(correctionDownloadUrl(sessionId, result.filename), '_blank', 'noopener')
    ElMessage.success('导出完成，文件也可从导出历史下载')
  }
}

function beforeUnload(event: BeforeUnloadEvent) {
  if (hasUnsaved.value || busy.value) { event.preventDefault(); event.returnValue = '' }
}
onBeforeRouteLeave(() => ws.prepareTransition())
onBeforeRouteUpdate((to, from) => publishedNotice.value && to.query.batch_id === undefined && to.query.tree_run_id === undefined ? true : to.query.batch_id !== from.query.batch_id || to.query.tree_run_id !== from.query.tree_run_id ? ws.prepareTransition() : true)
async function loadRouteBatch(defaultBatchId?: string) {
  const request = ++routeSelectionRequest
  ++batchRequest
  selectedBatchId.value = ''; ws.setSession(null); visibleExpandedTasks.value = []
  loading.value = true; pageError.value = ''
  try {
    if (typeof route.query.batch_id === 'string' && !await lifecycle.checkBatch(route.query.batch_id)) return
    const id = await resolveBatchSelection(batches.value.map(batch => batch.batch_id), {
      batchId: typeof route.query.batch_id === 'string' ? route.query.batch_id : undefined,
      legacyRunId: typeof route.query.tree_run_id === 'string' ? route.query.tree_run_id : undefined,
      defaultBatchId: defaultBatchId ?? batches.value.find(batch => batch.is_default)?.batch_id,
    }, api.treeRun)
    if (disposed || request !== routeSelectionRequest) return
    if (!await lifecycle.checkBatch(id)) return
    await loadBatch(id)
    if (!disposed && request === routeSelectionRequest && id && selectedBatchId.value === id) await router.replace({ query: { batch_id: id } })
  } catch (error) {
    if (!disposed && request === routeSelectionRequest) { pageError.value = (error as Error).message; loading.value = false }
  }
}
watch(() => [route.query.batch_id, route.query.tree_run_id], ([batchId, legacyRunId]) => {
  if ((publishedNotice.value && batchId === undefined && legacyRunId === undefined) || !initialized || (batchId === selectedBatchId.value && legacyRunId === undefined)) return
  publishedNotice.value = null
  void loadRouteBatch()
})
onMounted(async () => {
  window.addEventListener('beforeunload', beforeUnload)
  try {
    const preferred = typeof route.query.batch_id === 'string' ? route.query.batch_id : ''
    const active = await lifecycle.checkBatch(preferred)
    const defaultBatch = await loadChoices()
    if (disposed) return
    initialized = true
    if (active && !publishedNotice.value) await loadRouteBatch(defaultBatch)
  } catch (error) { if (!disposed) { pageError.value = (error as Error).message; loading.value = false } }
})
onBeforeUnmount(() => {
  disposed = true
  ++batchRequest; ++routeSelectionRequest
  decideAction('cancel')
  ws.setSession(null)
  window.removeEventListener('beforeunload', beforeUnload)
})
</script>

<template>
  <div class="page correction-page">
    <BatchPublishedNotice :notice="publishedNotice" />
    <header class="page-hero">
      <div><span class="eyebrow">EXPERT ACTION CORRECTION</span><h1>专家动作纠偏</h1><p>展开任务与轨迹，直接在截图上修正 Action。当前每个任务选择质检 Top-1。</p></div>
      <div class="hero-metrics"><div><b>{{ tasks.length }}</b><span>任务</span></div><div><b>{{ trajectoryCount }}</b><span>入选轨迹</span></div><div><b>{{ editedCount }}</b><span>已修改步骤</span></div></div>
    </header>

    <section class="toolbar">
      <label for="correction-batch">业务批次</label>
      <el-select id="correction-batch" :model-value="selectedBatchId" :loading="loading" :disabled="loading || busy || !batches.length" placeholder="选择已质检批次" @change="changeBatch">
        <el-option v-for="batch in batches" :key="batch.batch_id" :label="`${batch.batch_id} · 已质检 ${batch.reviewed_task_count}/${batch.total_task_count} 个任务`" :value="batch.batch_id" />
      </el-select>
      <el-button :disabled="busy || loading" @click="refreshBatch">刷新当前结果</el-button>
      <span class="save-status" role="status">{{ savingCount ? '正在保存…' : hasUnsaved ? '有未保存修改' : session ? '草稿已加载' : '' }}</span>
      <el-button type="success" :loading="savingCount > 0" :disabled="!session || busy || loading" @click="exportData">导出 SFT / RL / 原生数据（{{ exportCount }} 条）</el-button>
    </section>
    <el-alert v-if="session?.pending_review_count" :title="`上游数据已更新，保留的 ${session.pending_review_count} 项人工修改需要复核；导出仅包含不受影响的任务。`" type="warning" :closable="false" show-icon />
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
                    <el-tag v-if="trajectory.group.pending_review" type="warning">人工修改待复核</el-tag>
                    <span>{{ trajectory.group.active_row_count }}/{{ trajectory.group.row_count }} 步 · 已改 {{ trajectory.group.edited_row_count }} 步</span>
                    <el-button class="trajectory-export" size="small" :type="trajectory.group.export ? 'success' : 'default'" plain :disabled="busy || trajectory.group.pending_review" @click.stop="ws.toggleExport(trajectory.group)">{{ trajectory.group.export ? '取消导出' : '加入导出' }}</el-button>
                  </div>
                </template>
                <div v-if="openGroupId === trajectory.group.group_id" v-loading="loadingGroup" class="trajectory-detail">
                  <div v-if="loadingGroup" class="inline-loading" role="status">正在加载轨迹步骤…</div>
                  <div v-else-if="groupError" class="inline-error"><el-alert :title="groupError" type="error" :closable="false" /><el-button @click="ws.loadGroup(trajectory.group.group_id)">重新加载轨迹</el-button></div>
                  <div v-else-if="activeGroup?.pending_review" class="review-panel">
                    <el-alert title="对照当前步骤，决定是否采用上次人工修改" type="warning" :closable="false" />
                    <article v-for="item in activeGroup.pending_reviews" :key="item.step_key"><b>Step {{ item.step ?? item.baseline?.step ?? '—' }}</b><p>{{ item.reason }}</p><table><thead><tr><th>字段</th><th>上次基线</th><th>保留的人工修改</th><th>当前步骤</th></tr></thead><tbody><tr v-for="field in Object.keys(item.changes).filter(key => reviewFields[key])" :key="field"><th>{{ reviewFields[field] }}</th><td>{{ reviewText(item.baseline?.[field]) }}</td><td>{{ reviewText(item.changes[field]) }}</td><td>{{ reviewText(currentReviewValue(item.step_key, field)) }}</td></tr></tbody></table></article>
                    <p v-if="activeGroup.can_adopt_review === false">当前来源尚未就绪，或原步骤已不在当前入选轨迹中；暂时无法直接采用。</p>
                    <el-button type="primary" :disabled="busy || activeGroup.can_adopt_review === false" @click="reviewGroup(activeGroup.group_id, 'adopt')">采用保留的修改</el-button>
                    <el-button :disabled="busy" @click="reviewGroup(activeGroup.group_id, 'discard')">放弃保留的修改</el-button>
                  </div>
                  <CorrectionWorkbench v-if="activeGroup && session && !loadingGroup && !groupError" :session-id="session.session_id" :group="activeGroup" :row="activeRow" :revision="actionRevision" :saving="busy"
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
.review-panel{padding:16px;display:grid;gap:12px}.review-panel article{padding:12px;border:1px solid var(--line);border-radius:8px}.review-panel table{width:100%;table-layout:fixed;border-collapse:collapse;font-size:12px}.review-panel th,.review-panel td{padding:8px;text-align:left;white-space:pre-wrap;overflow-wrap:anywhere;vertical-align:top;border:1px solid var(--line)}
.toolbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:20px 0 10px;padding:14px 16px;border:1px solid var(--line);border-radius:12px;background:white}.toolbar label{font-size:12px;font-weight:800;color:#64748b}.toolbar .el-select{width:330px;max-width:100%}.save-status{margin-left:auto;color:#64748b;font-size:12px}.export-history summary{cursor:pointer;font-weight:700;padding:8px 0}.task-list{min-height:180px;margin-top:16px}.task-title{display:grid;grid-template-columns:190px minmax(240px,1fr) auto minmax(220px,auto);align-items:center;gap:14px;width:calc(100% - 36px);padding-right:16px}.task-title__id{font-weight:900;letter-spacing:.02em}.task-title__goal{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#334155}.task-title__stats{color:var(--muted);font-size:12px;text-align:right;white-space:nowrap}.trajectory-list{padding:10px 14px 18px 46px;min-height:80px}.trajectory-title{display:flex;align-items:center;gap:9px;width:calc(100% - 36px);padding-right:16px}.trajectory-title b{overflow:hidden;color:#334155;font-size:12px;text-overflow:ellipsis;white-space:nowrap}.trajectory-title span{color:var(--muted);font-size:12px;font-weight:400;white-space:nowrap}.trajectory-title .rank{color:#0f766e;font-weight:800}.trajectory-title .score{font-variant-numeric:tabular-nums}.trajectory-export{margin-left:auto;flex-shrink:0}.trajectory-detail{min-height:160px}.inline-loading,.inline-error{padding:24px;color:#64748b;font-size:13px}.inline-error{display:grid;gap:12px}.inline-error .el-button{justify-self:start}.export-history{margin-top:16px;padding:8px 16px;border:1px solid var(--line);border-radius:12px;background:white;font-size:12px;color:#64748b}.export-history a{display:flex;justify-content:space-between;gap:12px;padding:12px 0;color:#0f766e;overflow-wrap:anywhere}.export-history small{color:#64748b}
@media(max-width:1000px){.task-title{grid-template-columns:190px minmax(180px,1fr) auto}.task-title__stats{text-align:left}.trajectory-list{padding-left:0}.trajectory-title{flex-wrap:wrap;gap:8px}.trajectory-export{margin-left:0}}@media(max-width:650px){.save-status{margin-left:0}.toolbar .el-select{width:100%}.task-title{grid-template-columns:1fr auto;width:calc(100% - 36px)}.task-title__goal{grid-column:1 / -1}.task-title__stats{text-align:left;white-space:normal}.trajectory-title{width:calc(100% - 36px);padding-right:8px}.export-history a{flex-direction:column}}
</style>
