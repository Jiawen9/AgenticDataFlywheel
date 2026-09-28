<script setup lang="ts">
import PipelineStatusBar from '@/components/PipelineStatusBar.vue'
import RolloutImportDialog from '@/components/RolloutImportDialog.vue'
import type { RolloutImportResult } from '@/rolloutImportApi'
import { usePipelineContext } from '@/composables/usePipelineContext'
import BatchPublishedNotice from '@/components/BatchPublishedNotice.vue'
import { useBatchLifecycle } from '@/composables/useBatchLifecycle'
import { eventMatchesRoute, withoutBatchQuery, type PublishedBatchEvent } from '@/utils/batchLifecycle'

import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, Check, Clock, Download, Refresh, Select } from '@element-plus/icons-vue'
import { api, stageArtifactDownloadUrl } from '@/api'
import type { StageArtifact, TrajectoryStep } from '@/types'
import TrajectoryExplorer from '@/components/TrajectoryExplorer.vue'
import { useTrajectoryPreprocessing } from '@/composables/useTrajectoryPreprocessing'

const route = useRoute(), router = useRouter()
const editors = new Map<string, InstanceType<typeof TrajectoryExplorer>>()
async function protectEditors() {
  for (const editor of editors.values()) if (!(await editor.protect())) return false
  return true
}
const pipelineBatchId = ref(String(route.query.batch_id || ''))
const pipelineContext = usePipelineContext({ batchId: pipelineBatchId, stepId: 'preprocessing' })
const pipelineReadOnly = pipelineContext.readOnly
const flow = useTrajectoryPreprocessing(api, protectEditors, {
  readOnly: () => pipelineReadOnly.value,
  historyOnly: () => pipelineContext.historyOnly.value,
  boundJobs: () => pipelineContext.pipeline.value ? {
    preprocessing: pipelineContext.pipeline.value.steps.find(step => step.id === 'preprocessing')?.job_ids || [],
    tree: pipelineContext.pipeline.value.steps.find(step => step.id === 'tree')?.job_ids || [],
  } : pipelineContext.isPipelineRoute.value ? { preprocessing: [], tree: [] } : null,
})
const { batches, batchId, selectedBatch, scope, error, busy, processing, building,
  sourceTasks, sourceRuns, loadingSources, sourceError,
  loadingBatches, loading, submitting, tasks, selectedTasks, expandedTasks, expandedTrajectories,
  taskData, trajectoryData, loadingTasks, loadingTrajectories, preprocessingJob, buildJob } = flow
watch(batchId, id => { pipelineBatchId.value = id })
watch(() => pipelineContext.pipeline.value?.steps.map(step => `${step.id}:${step.status}:${step.job_ids.join(',')}`).join('|'), () => {
  if (batchId.value && pipelineContext.pipeline.value?.batch_id === batchId.value) void flow.loadBatches()
})
watch(pipelineContext.historyOnly, value => { if (value) void flow.selectBatch('') })
const lifecycle = useBatchLifecycle({ currentBatch: () => batchId.value || String(route.query.batch_id ?? route.query.collection_batch_id ?? ''), onPublished, refreshChoices: () => flow.loadBatches() })
const { notice: publishedNotice } = lifecycle
function onPublished(event: PublishedBatchEvent) {
  const current = eventMatchesRoute(event, batchId.value, route.query)
  flow.retireBatches(event.batch_ids, current)
  if (!current) return
  publishedNotice.value = event; editors.clear(); ElMessageBox.close()
  void router.replace({ query: withoutBatchQuery(route.query) })
}
const mounted = ref(false)
const rolloutImportVisible = ref(false)
let disposed = false
const treeCounts = computed(() => {
  const completed = tasks.value.filter(task => task.tree_status === 'succeeded').length
  const stale = tasks.value.filter(task => ['stale', 'invalidated'].includes(task.tree_status || '')).length
  return { completed, stale, pending: tasks.value.length - completed - stale }
})
const eligibleTasks = computed(() => tasks.value.filter(task => task.annotated))
const allSelected = computed(() => eligibleTasks.value.length > 0 && eligibleTasks.value.every(task => selectedTasks.value.includes(task.task_id)))
const labels: Record<string, string> = {
  queued: '等待执行', pending: '待开始', not_started: '待开始', waiting: '等待轨迹', collecting: '采集中',
  ready: '轨迹已就绪', partial: '部分轨迹已就绪', complete: '采集完成', completed: '已完成', running: '处理中',
  scanning: '检查原始轨迹', converting: '转换轨迹表', annotating: '生成与复核动作框', publishing: '保存阶段文件',
  succeeded: '已完成', failed: '失败', error: '采集异常', interrupted: '已中断', classifying: '生成 Observation 与中间态判断',
  building: '构建轨迹树', summarizing_trajectories: '生成轨迹摘要',
  dispatching: '下发中',
}
const statusText = (status: string) => labels[status] || status || '待开始'
const kinds: Record<string, string> = { collection: '采集批次', collection_batch: '采集批次', imported: '导入批次', import: '导入批次', existing_trajectories: '已有轨迹', rollout_import: 'Rollout 导入', task_generation: '任务生成', augmentation: '任务扩增' }
const kindText = (kind: string) => kinds[kind] || kind
const percentage = (value: number) => Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0
const collecting = computed(() => preprocessingJob.value?.stage === 'scanning' && !preprocessingJob.value.total_steps)
const sourceGroups = computed(() => sourceTasks.value.map(task => ({
  ...task,
  trajectories: sourceRuns.value.flatMap(run => run.trajectories.filter(trajectory => trajectory.task_id === task.task_id).map(trajectory => ({ ...trajectory, status: run.status }))),
  attempts: sourceRuns.value.filter(run => Object.values(run.batch_tasks).some(value => value.task_id === task.task_id)),
})))
const timeText = (value?: string | null) => value ? value.slice(0, 19).replace('T', ' ') : '采集时间未记录'
const stageFiles = computed(() => {
  const latest = new Map<string, StageArtifact>()
  for (const artifact of [...(selectedBatch.value?.artifacts || []), ...(preprocessingJob.value?.artifacts || [])]) {
    if (!['01_conversion', '02_annotation'].includes(artifact.stage)) continue
    const previous = latest.get(artifact.stage)
    if (!previous || artifact.version > previous.version) latest.set(artifact.stage, artifact)
  }
  return [...latest.values()].sort((a, b) => a.stage.localeCompare(b.stage))
})
function registerEditor(key: string, value: unknown) {
  if (value) editors.set(key, value as InstanceType<typeof TrajectoryExplorer>)
  else editors.delete(key)
}
async function chooseBatch(id: string) {
  if (pipelineContext.isPipelineRoute.value) return
  if (!await lifecycle.checkBatch(id)) return
  publishedNotice.value = null
  pipelineBatchId.value = id; await nextTick(); await pipelineContext.refresh()
  if (await flow.selectBatch(id)) await router.replace({ query: { ...route.query, batch_id: id || undefined, collection_batch_id: undefined } })
}
async function openRolloutImport() {
  if (pipelineContext.isPipelineRoute.value || pipelineReadOnly.value || !await flow.guard() || disposed) return
  rolloutImportVisible.value = true
}
async function importedRollout(result: RolloutImportResult) {
  if (disposed || pipelineContext.isPipelineRoute.value) return
  await flow.loadBatches()
  if (disposed || pipelineContext.isPipelineRoute.value) return
  await chooseBatch(result.batch_id)
  if (!disposed) ElMessage.success('原始轨迹已登记，可点击“开始预处理”，或在 Pipeline 中选择此批次。')
}
function toggleSelectAll() { selectedTasks.value = allSelected.value ? [] : eligibleTasks.value.map(task => task.task_id) }
async function toggleTask(name: string | number) {
  const id = String(name)
  await flow.expandTasks(expandedTasks.value.includes(id) ? expandedTasks.value.filter(item => item !== id) : [...expandedTasks.value, id])
  return false
}
async function toggleTrajectory(taskId: string, name: string | number) {
  const id = String(name)
  await flow.expandTrajectory(taskId, expandedTrajectories[taskId] === id ? '' : id)
  return false
}
async function submitBuild() { if (await flow.submitBuild()) ElMessage.success('后台建树作业已提交') }
function beforeUnload(event: BeforeUnloadEvent) {
  if ([...editors.values()].some(editor => editor.isEditing)) { event.preventDefault(); event.returnValue = '' }
}
watch(() => route.query.batch_id ?? route.query.collection_batch_id, async value => {
  if (!mounted.value || (publishedNotice.value && value === undefined)) return
  const requested = typeof value === 'string' ? value : ''
  if (requested === batchId.value) return
  if (!await lifecycle.checkBatch(requested)) return
  publishedNotice.value = null
  if (!(await flow.selectBatch(requested))) await router.replace({ query: { ...route.query, batch_id: batchId.value || undefined, collection_batch_id: undefined } })
})
onBeforeRouteLeave(() => flow.guard())
onMounted(async () => {
  document.body.classList.add('preprocessing-responsive')
  await pipelineContext.refresh()
  const requested = route.query.batch_id ?? route.query.collection_batch_id
  try {
    const active = typeof requested !== 'string' || await lifecycle.checkBatch(requested)
    await flow.loadBatches(active && typeof requested === 'string' ? requested : undefined)
  } catch (cause) { error.value = (cause as Error).message }
  if (disposed) return
  mounted.value = true
  window.addEventListener('beforeunload', beforeUnload)
})
onBeforeUnmount(() => { disposed = true; flow.dispose(); window.removeEventListener('beforeunload', beforeUnload); document.body.classList.remove('preprocessing-responsive') })
</script>

<template>
  <div class="page collection-page">
    <PipelineStatusBar :context="pipelineContext" />
    <template v-if="!pipelineContext.historyOnly.value">
    <BatchPublishedNotice :notice="publishedNotice" />
    <header class="page-hero">
      <div>
        <span class="eyebrow">TRAJECTORY PREPROCESSING</span>
        <h1>轨迹预处理与建树</h1>
        <p>选择一个批次，转换轨迹、检查动作标框，再提交轨迹树构建。</p>
      </div>
      <div class="hero-metrics">
        <div><b>{{ batches.length }}</b><span>批次</span></div>
        <div><b>{{ selectedBatch?.ready_trajectory_count || 0 }}</b><span>当前就绪轨迹</span></div>
        <div><b>{{ selectedBatch?.ready_step_count || 0 }}</b><span>当前就绪步骤</span></div>
      </div>
    </header>

    <section class="batch-directory" aria-label="预处理批次">
      <div class="section-heading"><h2>轨迹批次</h2><div><el-button v-if="!pipelineContext.isPipelineRoute.value && !pipelineReadOnly" :disabled="busy" data-testid="open-rollout-import" @click="openRolloutImport">导入已有 Rollout</el-button><el-button :icon="Refresh" :loading="loadingBatches" :disabled="busy" @click="flow.loadBatches()">刷新批次</el-button></div></div>
      <el-empty v-if="!loadingBatches && !batches.length" description="暂无轨迹批次，请先采集或登记原始轨迹" :image-size="70" />
      <div v-loading="loadingBatches" class="batch-list">
        <section v-for="batch in batches" :key="batch.batch_id" class="batch-item" :class="{ selected: batchId === batch.batch_id }">
          <button class="batch-toggle" type="button" :aria-expanded="batchId === batch.batch_id" :aria-label="'选择批次 ' + batch.batch_id" :disabled="busy || pipelineContext.isPipelineRoute.value" @click="chooseBatch(batchId === batch.batch_id ? '' : batch.batch_id)">
            <span class="batch-identity"><strong>{{ batch.label || batch.batch_id }}</strong><code>{{ batch.batch_id }}</code></span>
            <span class="batch-kind">{{ kindText(batch.kind) }}</span>
            <span class="batch-counts">{{ batch.task_count }} 任务 · {{ batch.ready_trajectory_count }} 就绪轨迹 · {{ batch.ready_step_count }} 步</span>
            <span class="batch-status" :class="batch.preprocessing_status">{{ statusText(batch.preprocessing_status) }}</span>
            <el-icon :class="{ rotated: batchId === batch.batch_id }"><ArrowDown /></el-icon>
          </button>
          <div v-if="batchId === batch.batch_id" class="batch-detail">
            <div class="batch-actions">
              <span>采集状态：{{ statusText(batch.collection_status) }}</span>
              <el-button v-if="preprocessingJob && ['failed', 'interrupted'].includes(preprocessingJob.status)" type="primary" :loading="submitting" :disabled="pipelineReadOnly || busy || processing || building" @click="flow.start(true)">重试预处理</el-button>
              <el-button v-if="preprocessingJob && ['failed', 'interrupted'].includes(preprocessingJob.status)" :loading="submitting" :disabled="pipelineReadOnly || busy || processing || building || !batch.can_start" @click="flow.start()">开始新预处理</el-button>
              <el-button v-else type="primary" :loading="submitting" :disabled="pipelineReadOnly || busy || processing || building || !batch.can_start" @click="flow.start()">{{ processing ? '预处理中' : '开始预处理' }}</el-button>
            </div>
            <p v-if="batch.reason" class="batch-reason">{{ batch.reason }}</p>
            <div v-loading="loadingSources" v-if="batch.kind !== 'existing_trajectories'" class="source-preview">
              <p v-if="sourceError" class="job-error">{{ sourceError }}</p>
              <details v-if="sourceGroups.length" :open="!scope" :key="batchId">
                <summary>源任务与采集轨迹 · {{ sourceGroups.length }} 个任务</summary>
                <div class="source-task-list">
                  <details v-for="source in sourceGroups" :key="source.task_id" class="source-task">
                    <summary><b>{{ source.app || 'App 未记录' }}</b><span>{{ source.task }}</span><small>{{ source.trajectories.length }} 轨迹</small></summary>
                    <p class="source-ident">用例 {{ source.collection_case_id }} · 任务 {{ source.task_id }}</p>
                    <ul v-if="source.trajectories.length"><li v-for="trajectory in source.trajectories" :key="trajectory.collection_run_id + '/' + trajectory.relative_dir"><b>{{ trajectory.source_trajectory_id }}</b><span>{{ timeText(trajectory.collected_at) }} · {{ statusText(trajectory.status) }}</span></li></ul>
                    <p v-else class="source-ident">暂无已就绪原始轨迹</p>
                    <div v-for="attempt in source.attempts" :key="attempt.collection_run_id" class="source-attempt">
                      <span>采集 {{ timeText(attempt.created_at) }} · {{ statusText(attempt.status) }}</span>
                      <p v-if="attempt.dispatch_error" class="job-error">{{ attempt.dispatch_error }}</p>
                      <p v-for="(failure, index) in attempt.errors.filter(value => !value.collection_case_id || value.collection_case_id === source.collection_case_id)" :key="index" class="job-error">{{ failure.error }}</p>
                    </div>
                  </details>
                </div>
              </details>
            </div>
            <div v-if="stageFiles.length" class="stage-downloads" aria-label="阶段文件">
              <div v-for="artifact in stageFiles" :key="artifact.stage">
                <b>{{ artifact.stage === '01_conversion' ? '轨迹转换' : '动作标框' }}</b>
                <a v-for="file in artifact.files" :key="file.name" :href="stageArtifactDownloadUrl(artifact, file.name)" :download="file.name"><el-icon><Download /></el-icon>{{ file.kind === 'excel' ? 'Excel' : 'JSON' }}</a>
                <small :title="artifact.version">{{ artifact.version }}</small>
              </div>
            </div>
          </div>
        </section>
      </div>
    </section>

    <el-alert v-if="error" class="page-error" :title="error" type="error" :closable="false" show-icon />
    <section v-if="preprocessingJob" class="job-card" :class="'job-card--' + preprocessingJob.status" aria-label="预处理进度">
      <div class="job-card__icon"><el-icon><Check v-if="preprocessingJob.status === 'succeeded'" /><Clock v-else /></el-icon></div>
      <div class="job-card__body">
        <div class="job-card__heading"><b>{{ statusText(preprocessingJob.stage) }}</b><span>{{ collecting ? '正在统计步骤' : percentage(preprocessingJob.percent) + '%' }}</span></div>
        <el-progress :percentage="collecting ? 100 : percentage(preprocessingJob.percent)" :indeterminate="collecting" :status="preprocessingJob.status === 'failed' || preprocessingJob.status === 'interrupted' ? 'exception' : preprocessingJob.status === 'succeeded' ? 'success' : undefined" :show-text="false" />
        <p><template v-if="!collecting">步骤 {{ preprocessingJob.completed_steps }} / {{ preprocessingJob.total_steps }}</template><template v-if="preprocessingJob.current_trajectory"> · {{ preprocessingJob.current_trajectory }}</template><template v-if="preprocessingJob.current_step"> · step {{ preprocessingJob.current_step }}</template></p>
        <p v-if="preprocessingJob.current_task">当前任务：{{ preprocessingJob.current_task }}</p>
        <p v-if="preprocessingJob.error" class="job-error">{{ preprocessingJob.error }}</p>
        <small>作业 {{ preprocessingJob.job_id }}</small>
      </div>
    </section>

    <template v-if="selectedBatch">
      <div class="workspace-heading"><div><h2>任务与轨迹</h2><p>当前批次 {{ batchId }} · 建树已完成 {{ treeCounts.completed }} / 待处理 {{ treeCounts.pending }} / 已失效 {{ treeCounts.stale }}</p></div><el-button :disabled="busy" @click="flow.loadBatches()">刷新当前结果</el-button></div>
      <el-empty v-if="!scope && !processing" description="完成预处理后，可查看步骤、编辑动作框并建树" :image-size="80" />
      <template v-if="scope">
        <section class="toolbar-card">
          <div class="selection-summary"><el-button :icon="Select" :disabled="pipelineReadOnly || busy || processing || building" @click="toggleSelectAll">{{ allSelected ? '取消全选' : '全选可用任务' }}</el-button><span>已选择 <b>{{ selectedTasks.length }}</b> / {{ eligibleTasks.length }} 个任务</span></div>
          <el-button type="primary" :loading="submitting" :disabled="pipelineReadOnly || busy || !selectedTasks.length || processing || building" @click="submitBuild">提交轨迹树构建</el-button>
        </section>
        <section v-if="buildJob" class="job-card" :class="'job-card--' + buildJob.status" aria-label="建树进度">
          <div class="job-card__body">
            <div class="job-card__heading"><b>{{ statusText(buildJob.stage) }}</b><span>{{ percentage(buildJob.percent) }}%</span></div>
            <el-progress :percentage="percentage(buildJob.percent)" :status="buildJob.status === 'failed' ? 'exception' : buildJob.status === 'succeeded' ? 'success' : undefined" :show-text="false" />
            <p>分类与 Observation {{ buildJob.classified_steps }} / {{ buildJob.total_steps }}<span v-if="buildJob.current_task"> · {{ buildJob.current_task }}</span></p>
            <p v-if="buildJob.stage === 'summarizing_trajectories'">轨迹摘要 {{ buildJob.summarized_trajectories || 0 }} / {{ buildJob.total_trajectories || 0 }}</p>
            <p v-if="buildJob.error" class="job-error">{{ buildJob.error }}</p>
            <router-link v-if="buildJob.status === 'succeeded'" :to="{ path: '/quality', query: { ...route.query, batch_id: batchId, ...(pipelineContext.isPipelineRoute.value ? { step_id: 'quality' } : {}) } }">进入本批次质检 →</router-link>
          </div>
        </section>
        <section v-loading="loading" class="task-list">
          <el-empty v-if="!loading && !tasks.length" description="该批次没有可查看的预处理任务" />
          <el-collapse v-else :model-value="expandedTasks" :before-collapse="toggleTask">
            <el-collapse-item v-for="task in tasks" :key="task.task_id" :name="task.task_id">
              <template #title><div class="task-title">
                <el-checkbox v-model="selectedTasks" :value="task.task_id" :disabled="pipelineReadOnly || !task.annotated || busy || processing || building" @click.stop />
                <b>{{ task.task_id }}</b><span class="task-goal" :title="task.goal">{{ task.goal }}</span><el-tag :type="task.annotated ? 'success' : 'warning'">{{ task.annotated ? '已预处理' : '待预处理' }}</el-tag><el-tag v-if="task.tree_status" :type="task.tree_status === 'succeeded' ? 'success' : 'warning'">{{ task.tree_status === 'succeeded' ? '已建树' : ['stale', 'invalidated'].includes(task.tree_status) ? '建树已失效' : '待建树' }}</el-tag><small>{{ task.trajectory_count }} 轨迹 · {{ task.step_count }} 步</small>
              </div></template>
              <el-alert v-if="task.warning" :title="task.warning" type="warning" :closable="false" show-icon />
              <div v-loading="loadingTasks[task.task_id]" class="trajectory-list">
                <el-empty v-if="!loadingTasks[task.task_id] && !taskData[task.task_id]?.length" description="暂无预处理轨迹" :image-size="60" />
                <el-collapse v-else :model-value="expandedTrajectories[task.task_id] || ''" accordion :before-collapse="(id: string | number) => toggleTrajectory(task.task_id, id)">
                  <el-collapse-item v-for="trajectory in taskData[task.task_id]" :key="trajectory.trajectory_id" :name="trajectory.trajectory_id">
                    <template #title><div class="trajectory-title"><el-icon><Refresh /></el-icon><b :title="trajectory.trajectory_id">{{ trajectory.source_trajectory_id || trajectory.trajectory_id }}</b><span>{{ trajectory.step_count }} 步 · {{ timeText(trajectory.collected_at) }}</span></div></template>
                    <div v-loading="loadingTrajectories[flow.key(task.task_id, trajectory.trajectory_id)]" class="trajectory-detail">
                      <TrajectoryExplorer v-if="trajectoryData[flow.key(task.task_id, trajectory.trajectory_id)]"
                        :ref="value => registerEditor(flow.key(task.task_id, trajectory.trajectory_id), value)"
                        :task-id="task.task_id" :trajectory="trajectoryData[flow.key(task.task_id, trajectory.trajectory_id)]" :scope="scope" :disabled="pipelineReadOnly || busy || processing || building"
                        :save-bbox="(step: TrajectoryStep, bbox: [number, number, number, number]) => flow.saveBBox(task.task_id, trajectory.trajectory_id, step, bbox)" />
                    </div>
                  </el-collapse-item>
                </el-collapse>
              </div>
            </el-collapse-item>
          </el-collapse>
        </section>
      </template>
    </template>
    <el-empty v-else-if="batches.length" description="选择一个批次开始预处理或查看结果" :image-size="80" />
    </template>
    <RolloutImportDialog v-model="rolloutImportVisible" @imported="importedRollout" />
  </div>
</template>

<style scoped>
.source-preview{font-size:12px;margin-top:12px}.source-preview summary{cursor:pointer;color:var(--accent-deep);line-height:1.7}.source-task-list{max-height:220px;overflow:auto;margin-top:8px}.source-task{padding:9px 0;border-top:1px solid var(--line)}.source-task>summary{display:flex;gap:8px;align-items:baseline}.source-task>summary span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink)}.source-task>summary small{white-space:nowrap}.source-ident,.source-attempt{color:var(--muted);font-size:11px;overflow-wrap:anywhere;margin:7px 0}.source-task ul{padding-left:18px}.source-task li{margin:6px 0;overflow-wrap:anywhere}.source-task li span{display:block;color:var(--muted);font-size:11px}
:global(body.preprocessing-responsive){min-width:0}
@media(max-width:600px){.collection-page{padding:20px 12px}.collection-page .page-hero{gap:16px}.collection-page .page-hero h1{font-size:25px}.collection-page .page-hero p{font-size:13px}.collection-page .hero-metrics{gap:6px}.collection-page .hero-metrics div{min-width:0;padding:10px 8px}.collection-page .hero-metrics span{font-size:10px}}
.collection-page{min-width:0}.section-heading,.workspace-heading{display:flex;align-items:center;justify-content:space-between;gap:14px}.section-heading h2,.workspace-heading h2{font-size:16px;margin:0}.batch-directory{margin-top:24px;padding:18px;border:1px solid var(--line);border-radius:16px;background:rgba(255,255,255,.86)}.section-heading{margin-bottom:12px}.batch-list{max-height:400px;overflow:auto}.batch-item+.batch-item{border-top:1px solid var(--line)}.batch-toggle{width:100%;display:grid;grid-template-columns:minmax(180px,1fr) 88px minmax(170px,auto) 90px 20px;gap:14px;align-items:center;padding:14px 10px;border:0;background:transparent;text-align:left;color:var(--ink);cursor:pointer}.batch-toggle:hover,.batch-item.selected{background:#f0fdfa}.batch-toggle:disabled{cursor:wait}.batch-toggle:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}.batch-identity{display:grid;gap:4px;min-width:0}.batch-identity strong{font-size:14px}.batch-identity code{font-size:11px;color:var(--muted);overflow-wrap:anywhere}.batch-kind,.batch-counts{font-size:12px;color:var(--muted)}.batch-status{font-size:12px;color:var(--accent-deep)}.batch-status.failed,.batch-status.interrupted{color:#b91c1c}.rotated{transform:rotate(180deg)}.batch-detail{padding:0 12px 16px}.batch-actions{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:12px;color:var(--muted)}.batch-reason{font-size:12px;color:var(--muted);margin:9px 0;line-height:1.6;overflow-wrap:anywhere}.stage-downloads{display:grid;gap:9px;margin-top:14px}.stage-downloads>div{display:flex;align-items:center;gap:14px;flex-wrap:wrap;font-size:12px}.stage-downloads a{display:inline-flex;gap:4px;align-items:center;color:var(--accent-deep)}.stage-downloads small{color:var(--muted);font-size:10px;overflow-wrap:anywhere}.page-error{margin-top:16px}.workspace-heading{margin:22px 0 8px}.workspace-heading p{font-size:12px;color:var(--muted);line-height:1.7;overflow-wrap:anywhere}.workspace-heading code{font-size:10px}.toolbar-card{display:flex;justify-content:space-between;align-items:center;gap:16px;margin:16px 0;padding:16px 18px;border:1px solid var(--line);border-radius:14px;background:#fff}.selection-summary{display:flex;align-items:center;gap:16px;color:var(--muted);font-size:13px}.selection-summary b{color:var(--accent-deep)}.job-card{display:flex;gap:16px;margin-top:16px;padding:18px;border:1px solid #bae6fd;border-radius:14px;background:#f0f9ff}.job-card--succeeded{border-color:#99f6e4;background:#f0fdfa}.job-card--failed,.job-card--interrupted{border-color:#fecaca;background:#fef2f2}.job-card__icon{display:grid;place-items:center;flex:0 0 38px;height:38px;border-radius:10px;background:var(--accent-deep);color:white;font-size:20px}.job-card__body{flex:1;min-width:0}.job-card__heading{display:flex;justify-content:space-between;gap:10px;margin-bottom:10px}.job-card p,.job-card small,.job-card__heading span{color:var(--muted);font-size:12px;overflow-wrap:anywhere}.job-card p{margin:8px 0}.job-error{color:#b91c1c!important}.job-card a{display:inline-block;margin-top:9px;color:var(--accent-deep)}.task-list{min-height:180px;margin-top:16px}.task-title{display:grid;grid-template-columns:28px 180px minmax(100px,1fr) auto 105px;align-items:center;gap:12px;width:calc(100% - 36px);padding-right:12px}.task-title>b{font-size:12px;overflow-wrap:anywhere}.task-goal{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#334155;font-size:13px}.task-title small{color:var(--muted);font-size:11px}.trajectory-list{padding:10px 12px 18px 36px;min-height:80px}.trajectory-title{display:flex;align-items:center;gap:9px;min-width:0}.trajectory-title b{font-size:12px;overflow-wrap:anywhere}.trajectory-title span{color:var(--muted);font-size:12px;font-weight:400}.trajectory-detail{min-height:160px}
@media(max-width:1100px){.batch-toggle{grid-template-columns:minmax(140px,1fr) 80px 90px 18px;gap:10px}.batch-counts{grid-column:1/-1;grid-row:2}.task-title{grid-template-columns:28px minmax(120px,1fr) auto}.task-goal,.task-title small{display:none}.trajectory-list{padding-left:0}}
@media(max-width:600px){.batch-directory{padding:12px}.batch-toggle{grid-template-columns:minmax(0,1fr) 70px 18px;padding:12px 4px}.batch-kind{display:none}.batch-counts{font-size:11px}.batch-status{font-size:11px}.batch-actions{flex-wrap:wrap}.stage-downloads>div{gap:9px}.stage-downloads small{flex-basis:100%}.toolbar-card,.workspace-heading,.selection-summary{align-items:flex-start;flex-direction:column}.toolbar-card{padding:12px;gap:12px}.job-card{padding:12px;gap:9px}.job-card__icon{display:none}.task-title{gap:6px;width:calc(100% - 20px);padding-right:4px;grid-template-columns:24px minmax(100px,1fr) auto}.trajectory-title{gap:5px;flex-wrap:wrap}.trajectory-list{padding-right:0}.workspace-heading p{font-size:11px}}
</style>
