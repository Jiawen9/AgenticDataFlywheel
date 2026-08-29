<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { DataLine } from '@element-plus/icons-vue'
import { api, imageUrl } from '@/api'
import ActionImage from '@/components/ActionImage.vue'
import ReferenceTrajectoryTree from '@/components/ReferenceTrajectoryTree.vue'
import type { AuditStep, CorrectionRecommendation, DimensionEvaluation, QualityJob, QualityTaskSummary, TaskQualityResult, TrajectoryQualityEvaluation, TrajectoryTreeNode, TreeOccurrence, TreeRun, TreeRunTask } from '@/types'

const route = useRoute(), router = useRouter()
const runs = ref<TreeRun[]>([]), selectedRunId = ref(''), checkedTasks = ref<string[]>([])
const summaries = ref<Record<string, QualityTaskSummary>>({}), qualityJobs = ref<QualityJob[]>([])
const selectedTaskId = ref(''), tree = ref<TrajectoryTreeNode | null>(null), quality = ref<TaskQualityResult | null>(null)
const selectedNode = ref<TrajectoryTreeNode | null>(null), occurrenceIndex = ref(0), qualityTrajectory = ref('')
const loadingRuns = ref(true), loadingTree = ref(false), submitting = ref(false), auditVisible = ref(false)
const auditSelected = ref<(AuditStep & { trajectory: string }) | null>(null)
const correctionRecommendation = ref<CorrectionRecommendation | null>(null), polling = ref(false)
let pollTimer: number | undefined

const selectedRun = computed(() => runs.value.find((run) => run.run_id === selectedRunId.value))
const selectedTask = computed<TreeRunTask | undefined>(() => selectedRun.value?.tasks.find((task) => task.task_id === selectedTaskId.value))
const occurrence = computed(() => selectedNode.value?.occurrences?.[occurrenceIndex.value] ?? null)
const terminalIds = computed(() => selectedNode.value?.terminal_trajectories.filter((id) => quality.value?.evaluations[id]) ?? [])
const evaluation = computed<TrajectoryQualityEvaluation | null>(() => quality.value?.evaluations[qualityTrajectory.value] ?? null)
const ignoredSteps = computed(() => (tree.value?.source_trajectories ?? []).flatMap((item) => item.steps.filter((step) => !step.counted_in_tree).map((step) => ({ ...step, trajectory: item.trajectory }))))
const activeJobs = computed(() => qualityJobs.value.filter((job) => ['queued', 'running'].includes(job.status)).sort((a, b) => a.created_at.localeCompare(b.created_at)))
const visibleJobs = computed(() => [...activeJobs.value, ...qualityJobs.value.filter((job) => !['queued', 'running'].includes(job.status)).slice(0, 3)])
const jobRunning = computed(() => activeJobs.value.length > 0)
const desktopTree = computed<TrajectoryTreeNode | null>(() => {
  if (!tree.value) return null
  const occurrences: TreeOccurrence[] = []
  for (const item of tree.value.source_trajectories ?? []) {
    const first = item.steps[0]
    if (first) occurrences.push({ trajectory: item.trajectory, step: 0, excel_row: 0, image: first.image.replace(/[^\\/]+$/, 'initial_orch.jpg'), xml: first.xml.replace(/[^\\/]+$/, 'initial_orch_ui.xml'), action: { action: 'desktop' }, action_text: '', summary: '轨迹起始桌面', observation: '', actions_box: '', score: 5, reused: true, classification: null })
  }
  const first = occurrences[0]
  return { ...tree.value, label: '桌面', action: { action: 'desktop' }, summary: '所有轨迹的共同起点：设备桌面', image: first?.image ?? '', xml: first?.xml ?? '', reference_trajectory: first?.trajectory ?? '', reference_step: 0, occurrence_count: occurrences.length, occurrences }
})

function taskQuality(taskId: string) { return summaries.value[taskId] }
function top1Task(taskId: string) {
  if (correctionRecommendation.value?.status !== 'ready' || (correctionRecommendation.value.tree_run_id ?? correctionRecommendation.value.run_id) !== selectedRunId.value) return null
  return correctionRecommendation.value.tasks.find((task) => task.task_id === taskId) ?? null
}
function dimensions(value: TrajectoryQualityEvaluation | null): DimensionEvaluation[] {
  if (!value) return []
  return Array.isArray(value.dimension_global_scores) ? value.dimension_global_scores : Object.entries(value.dimension_global_scores ?? {}).map(([dimension_name, score]) => ({ dimension_name, score }))
}
async function loadRuns() {
  loadingRuns.value = true
  try { runs.value = await api.runs(); const requested = typeof route.query.run === 'string' ? route.query.run : ''; selectedRunId.value = runs.value.some((r) => r.run_id === requested) ? requested : runs.value[0]?.run_id || '' }
  catch (error) { ElMessage.error((error as Error).message) } finally { loadingRuns.value = false }
}
async function loadSummary() {
  if (!selectedRunId.value) return
  try { const result = await api.runQuality(selectedRunId.value); summaries.value = Object.fromEntries(result.tasks.map((item) => [item.task_id, item])) }
  catch (error) { ElMessage.error((error as Error).message) }
}
async function loadQualityJobs() {
  qualityJobs.value = await api.qualityJobs()
}
async function loadCorrectionRecommendation(runId = selectedRunId.value) {
  if (!runId) { correctionRecommendation.value = null; return }
  try { correctionRecommendation.value = await api.correctionRecommendation(runId) }
  catch { correctionRecommendation.value = null }
}
async function viewTree(taskId: string) {
  selectedTaskId.value = taskId; loadingTree.value = true
  try {
    tree.value = await api.tree(selectedRunId.value, taskId)
    quality.value = taskQuality(taskId)?.status === 'succeeded' ? await api.taskQuality(selectedRunId.value, taskId) : null
    selectedNode.value = desktopTree.value; occurrenceIndex.value = 0
  } catch (error) { ElMessage.error((error as Error).message) } finally { loadingTree.value = false }
}
async function submitQuality() {
  if (!checkedTasks.value.length) return ElMessage.warning('请至少选择一个任务')
  submitting.value = true
  try { await api.createQuality(selectedRunId.value, checkedTasks.value); await loadQualityJobs(); startPolling() }
  catch (error) { ElMessage.error((error as Error).message) } finally { submitting.value = false }
}
function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = undefined }
function startPolling() { stopPolling(); pollTimer = window.setInterval(() => void pollJobs(), 1200); void pollJobs() }
function jobLabel(job: QualityJob) {
  if (job.status === 'queued') return '排队等待'
  if (job.status === 'running') return '质检执行中'
  if (job.status === 'succeeded') return '质检完成'
  if (job.status === 'interrupted') return '服务重启中断'
  return '质检失败'
}
async function pollJobs() {
  if (polling.value) return
  polling.value = true
  const previous = new Map(qualityJobs.value.map((job) => [job.job_id, job.status]))
  try {
    await loadQualityJobs()
    const completed = qualityJobs.value.filter((job) => {
      const oldStatus = previous.get(job.job_id)
      return oldStatus && ['queued', 'running'].includes(oldStatus) && !['queued', 'running'].includes(job.status)
    })
    for (const job of completed) {
      if (job.status === 'succeeded') ElMessage.success(`轨迹质检完成：${job.run_id}`)
      else ElMessage.error(job.error || `${jobLabel(job)}：${job.job_id}`)
    }
    if (completed.some((job) => job.status === 'succeeded')) {
      await loadCorrectionRecommendation(selectedRunId.value)
      await loadSummary()
      if (selectedTaskId.value) await viewTree(selectedTaskId.value)
    }
    if (!activeJobs.value.length) stopPolling()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    polling.value = false
  }
}
function selectNode(node: TrajectoryTreeNode) { selectedNode.value = node; occurrenceIndex.value = 0; qualityTrajectory.value = node.terminal_trajectories.find((id) => quality.value?.evaluations[id]) ?? '' }
function selectAuditStep(row: AuditStep & { trajectory: string }) { auditSelected.value = row }
function toggleAll() { checkedTasks.value = checkedTasks.value.length === (selectedRun.value?.tasks.length ?? 0) ? [] : (selectedRun.value?.tasks.map((t) => t.task_id) ?? []) }
watch(selectedRunId, async (id) => { checkedTasks.value = []; selectedTaskId.value = ''; tree.value = quality.value = selectedNode.value = null; correctionRecommendation.value = null; await router.replace({ query: id ? { run: id } : {} }); if (id) await Promise.all([loadSummary(), loadCorrectionRecommendation(id)]) })
watch(terminalIds, (ids) => { qualityTrajectory.value = ids[0] ?? '' })
onMounted(async () => { await Promise.all([loadRuns(), loadQualityJobs()]); await loadCorrectionRecommendation(selectedRunId.value); if (activeJobs.value.length) startPolling() })
onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="page quality-page">
    <header class="page-hero"><div><span class="eyebrow">TRAJECTORY QUALITY</span><h1>轨迹质检</h1><p>选择已完成的建树任务集，批量评价轨迹；评分会显示在对应终点叶子上。</p></div><div class="run-selector"><label>已完成任务集</label><el-select v-model="selectedRunId" :loading="loadingRuns" placeholder="选择完成时间串" style="width:260px"><el-option v-for="run in runs" :key="run.run_id" :label="`${run.run_id} · ${run.task_count} 个任务`" :value="run.run_id" /></el-select></div></header>
    <el-empty v-if="!loadingRuns && !runs.length" description="还没有成功发布的建树任务集"><router-link to="/collection"><el-button type="primary">前往轨迹采集</el-button></router-link></el-empty>
    <template v-else-if="selectedRun">
      <section class="task-toolbar"><div><b>{{ selectedRun.task_count }} 个任务</b><span>{{ selectedRun.completed_at }} · {{ selectedRun.model_name }}</span></div><div><el-button @click="toggleAll">{{ checkedTasks.length === selectedRun.tasks.length ? '取消全选' : '全选' }}</el-button><el-button type="primary" :loading="submitting" :disabled="jobRunning" @click="submitQuality">提交轨迹质检（{{ checkedTasks.length }}）</el-button></div></section>
      <section v-if="visibleJobs.length" class="job-queue"><div class="job-queue__head"><div><b>质检任务队列</b><span>{{ activeJobs.length }} 个执行中 / 等待，页面切换后会从后端恢复</span></div><el-button text :loading="polling" @click="loadQualityJobs">刷新队列</el-button></div><article v-for="job in visibleJobs" :key="job.job_id" class="job-queue__item" :class="`job-queue__item--${job.status}`"><div class="job-queue__main"><b>{{ jobLabel(job) }}</b><span>{{ job.run_id }} · {{ job.current_task || '等待任务' }}<template v-if="job.current_trajectory"> · {{ job.current_trajectory }}</template></span><small>{{ job.job_id }}</small></div><el-progress :percentage="job.percent" :status="job.status === 'failed' ? 'exception' : job.status === 'succeeded' ? 'success' : undefined" /><small class="job-queue__count">{{ job.completed_trajectories }} / {{ job.total_trajectories || '—' }} 条轨迹</small><small v-if="job.error" class="job-queue__error">{{ job.error }}</small></article></section>
      <section class="task-list"><article v-for="task in selectedRun.tasks" :key="task.task_id" class="task-row"><el-checkbox v-model="checkedTasks" :value="task.task_id" /><div class="task-main"><b>{{ task.task_id }}</b><p>{{ task.goal }}</p></div><div class="metric"><span>轨迹 / 步骤</span><b>{{ task.trajectory_count }} / {{ task.original_step_count }}</b></div><div class="status"><el-tag :type="taskQuality(task.task_id)?.rubric_ready ? 'success' : 'info'">Rubric {{ taskQuality(task.task_id)?.rubric_ready ? '就绪' : '待生成' }}</el-tag><el-tag :type="taskQuality(task.task_id)?.status === 'succeeded' ? 'success' : 'info'">{{ taskQuality(task.task_id)?.status === 'succeeded' ? '已质检' : '未质检' }}</el-tag></div><div class="metric"><span>平均分 / 通过</span><b>{{ taskQuality(task.task_id)?.average_score?.toFixed(2) ?? '—' }} / {{ taskQuality(task.task_id)?.passed_count ?? '—' }}</b></div><div v-if="top1Task(task.task_id)" class="metric top1-metric"><span>Top-1 修正</span><b>{{ top1Task(task.task_id)?.trajectory_id }}</b><small>{{ top1Task(task.task_id)?.global_score.toFixed(4) }} 分 · {{ top1Task(task.task_id)?.step_count }} 步</small></div><el-button v-if="top1Task(task.task_id)" type="success" plain @click="router.push({ path: '/correction', query: { tree_run_id: selectedRunId } })">进入轨迹修正</el-button><el-button type="primary" plain @click="viewTree(task.task_id)">查看轨迹树</el-button></article></section>
      <template v-if="selectedTask"><section class="run-banner"><div><span>当前任务</span><b>{{ selectedTask.goal }}</b></div><el-button :icon="DataLine" @click="auditVisible = true">中间态审计（{{ ignoredSteps.length }}）</el-button></section><section class="stat-grid"><div><span>原始步骤</span><b>{{ tree?.original_step_count ?? selectedTask.original_step_count }}</b></div><div><span>入树步骤</span><b>{{ tree?.tree_step_count ?? selectedTask.tree_step_count }}</b></div><div><span>忽略步骤</span><b>{{ tree?.ignored_incidental_step_count ?? selectedTask.ignored_step_count }}</b></div><div><span>Action 节点</span><b>{{ selectedTask.action_node_count }}</b></div></section>
        <section v-loading="loadingTree" class="tree-workspace"><div class="tree-canvas"><ReferenceTrajectoryTree v-if="desktopTree" :root="desktopTree" :selected-id="selectedNode?.id" :quality="quality?.evaluations" @select="selectNode" /></div><aside class="node-panel"><el-empty v-if="!selectedNode" description="点击一个节点查看详情" :image-size="70" /><template v-else><div class="panel-head"><div><span>NODE {{ selectedNode.id }} · DEPTH {{ selectedNode.depth }}</span><h2>{{ selectedNode.label }}</h2></div><el-tag type="success">{{ selectedNode.occurrence_count }} occurrences</el-tag></div><p class="summary">{{ occurrence?.summary || selectedNode.summary }}</p><el-select v-if="selectedNode.occurrences.length" v-model="occurrenceIndex" style="width:100%"><el-option v-for="(item,index) in selectedNode.occurrences" :key="`${item.trajectory}-${item.step}`" :label="`${item.trajectory} · step ${item.step}`" :value="index" /></el-select><ActionImage v-if="occurrence" class="node-image" :image-url="imageUrl(occurrence.image)" :action="occurrence.action || selectedNode.action" :actions-box="occurrence.actions_box || selectedNode.actions_box" :show-overlay="selectedNode.id !== 0" color-tone="bright" />
          <div v-if="terminalIds.length" class="quality-card"><div class="quality-title"><b>终点质检结果</b><el-select v-if="terminalIds.length > 1" v-model="qualityTrajectory" size="small"><el-option v-for="id in terminalIds" :key="id" :label="id" :value="id" /></el-select></div><template v-if="evaluation"><div class="score"><strong>{{ evaluation.global_score.toFixed(2) }}</strong><span>/ 5</span><el-tag :type="evaluation.passed_threshold ? 'success' : 'danger'">{{ evaluation.passed_threshold ? '通过' : '未通过' }}</el-tag></div><div v-for="item in dimensions(evaluation)" :key="item.dimension_name" class="dimension"><b>{{ item.dimension_name }}</b><em>{{ item.score.toFixed(2) }}</em></div><el-collapse><el-collapse-item title="逐步评价"><div v-for="step in evaluation.step_evaluations" :key="step.step_id" class="step"><b>Step {{ step.step_id }}</b><small>{{ step.step_quality_summary }}</small><p v-for="item in step.dimension_scores" :key="item.dimension_name"><span>{{ item.dimension_name }} · {{ item.score }}</span>{{ item.rationale }}</p></div></el-collapse-item></el-collapse></template></div>
          <div class="node-data"><template v-if="selectedNode.id !== 0"><label>ACTION SUMMARY / THOUGHT</label><p>{{ occurrence?.summary || selectedNode.summary || '—' }}</p><label>OBSERVATION</label><p class="observation">{{ occurrence?.observation || selectedNode.observation || '该旧任务集未生成 observation' }}</p><label>ACTION</label><pre>{{ JSON.stringify(occurrence?.action || selectedNode.action, null, 2) }}</pre><template v-if="occurrence?.classification"><label>INTERMEDIATE STATE</label><p>{{ occurrence.classification.category }} · {{ occurrence.classification.confidence.toFixed(2) }}<br>{{ occurrence.classification.reason }}</p></template></template><template v-else><label>STATE</label><p>设备桌面 · 所有轨迹从这里开始</p></template><label>BBOX</label><code>{{ occurrence?.actions_box || selectedNode.actions_box || '—' }}</code></div></template></aside></section>
      </template>
    </template>
    <el-drawer v-model="auditVisible" title="中间态审计" size="72%"><div class="audit"><el-table :data="ignoredSteps" highlight-current-row @row-click="selectAuditStep"><el-table-column prop="trajectory" label="轨迹" /><el-table-column prop="step" label="Step" width="70" /><el-table-column prop="classification.category" label="类别" /><el-table-column prop="classification.confidence" label="置信度" /><el-table-column prop="summary" label="Action Summary" /></el-table><div v-if="auditSelected"><h3>{{ auditSelected.trajectory }} · step {{ auditSelected.step }}</h3><ActionImage :image-url="imageUrl(auditSelected.image)" :action="auditSelected.action" :actions-box="auditSelected.actions_box" /><p>{{ auditSelected.classification?.reason }}</p></div><el-empty v-else description="选择一个忽略步骤查看证据" /></div></el-drawer>
  </div>
</template>

<style scoped>
.node-data p{margin:0;color:#cbd5e1;font-size:12px;line-height:1.55}.node-data .observation{padding:9px;border-left:3px solid #22d3ee;border-radius:4px;background:#172338;color:#e0f2fe}
.audit{display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,.65fr);gap:20px}
.run-selector{display:grid;gap:7px}.run-selector label{color:#64748b;font-size:11px;font-weight:900}.task-toolbar,.task-row,.job-panel,.run-banner{display:flex;align-items:center;justify-content:space-between;gap:16px}.task-toolbar{margin:20px 0 12px}.task-toolbar>div:first-child,.job-panel>div:first-child{display:grid}.task-toolbar span,.job-panel span{color:var(--muted);font-size:12px}.task-list{display:grid;gap:9px}.task-row{padding:14px 16px;border:1px solid var(--line);border-radius:14px;background:#fff}.task-main{min-width:220px;flex:1}.task-main p{margin:4px 0 0;color:#64748b;font-size:12px}.metric{display:grid;min-width:110px}.metric span{color:#94a3b8;font-size:10px}.metric small{color:#0f766e;font-size:10px}.top1-metric b{color:#0f766e;font-size:12px}.status{display:flex;gap:6px}.job-panel{margin-bottom:14px;padding:14px 18px;border-radius:14px;background:#eff6ff;border:1px solid #bfdbfe}.job-panel .el-progress{flex:1}.job-panel--failed{background:#fff1f2}.run-banner{margin:22px 0 12px;padding:15px 18px;border:1px solid #99f6e4;border-radius:15px;background:#f0fdfa}.run-banner div{display:grid;gap:4px}.run-banner span{color:#0f766e;font-size:11px;font-weight:900}.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}.stat-grid>div{padding:12px 15px;border:1px solid var(--line);border-radius:12px;background:white}.stat-grid span{display:block;color:var(--muted);font-size:11px}.stat-grid b{font-size:21px}.tree-workspace{display:grid;grid-template-columns:minmax(0,1fr) 300px;height:calc(100vh - 280px);min-height:610px;overflow:hidden;border:1px solid var(--line);border-radius:18px}.tree-canvas{min-width:0;background:#0b1220}.node-panel{overflow:auto;padding:14px;border-left:1px solid #263750;background:#111827;color:#dce7f5}.panel-head,.quality-title{display:flex;justify-content:space-between;gap:8px}.panel-head span,.node-data label{color:#8297b1;font-size:10px;font-weight:900}.panel-head h2{margin:4px 0}.summary{color:#9fb1c8}.node-image{margin:10px 0}.node-image :deep(img){max-height:330px}.node-data{display:grid;gap:7px}.node-data pre{margin:0;padding:9px;overflow:auto;border-radius:8px;background:#09101d;color:#a7f3d0;font-size:10px}.node-data code{color:#67b7ff}.quality-card{margin:12px 0;padding:12px;border:1px solid #334155;border-radius:11px;background:#0b1324}.score{display:flex;align-items:baseline;gap:5px;margin:10px 0}.score strong{font-size:30px}.score .el-tag{margin-left:auto}.dimension{display:grid;grid-template-columns:1fr auto;gap:3px;padding:7px 0;border-top:1px solid #263750}.dimension em{color:#67e8f9;font-style:normal}.dimension p{grid-column:1/-1;margin:0;color:#94a3b8;font-size:11px}.step p{display:grid;color:#94a3b8;font-size:11px}.step p span{color:#dce7f5}@media(max-width:1100px){.task-row{flex-wrap:wrap}.tree-workspace{grid-template-columns:1fr;height:auto}.tree-canvas{height:650px}.stat-grid{grid-template-columns:repeat(2,1fr)}}
.job-queue{display:grid;gap:8px;margin-bottom:14px;padding:14px 16px;border:1px solid #bfdbfe;border-radius:14px;background:#eff6ff}.job-queue__head{display:flex;align-items:center;justify-content:space-between;gap:12px}.job-queue__head div{display:grid;gap:3px}.job-queue__head span{color:var(--muted);font-size:12px}.job-queue__item{display:grid;grid-template-columns:minmax(240px,1.2fr) minmax(180px,1fr) auto;align-items:center;gap:12px;padding:10px 12px;border:1px solid #dbeafe;border-radius:10px;background:#fff}.job-queue__main{display:grid;gap:3px;min-width:0}.job-queue__main>b{color:#0f172a}.job-queue__main span,.job-queue__main small,.job-queue__count{overflow:hidden;color:#64748b;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.job-queue__item .el-progress{min-width:140px}.job-queue__item--succeeded{border-color:#bbf7d0}.job-queue__item--failed,.job-queue__item--interrupted{border-color:#fecdd3;background:#fffafa}.job-queue__error{grid-column:1/-1;color:#be123c;font-size:11px;overflow-wrap:anywhere}
</style>
