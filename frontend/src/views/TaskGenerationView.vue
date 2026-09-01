<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, Refresh } from '@element-plus/icons-vue'
import ScenarioTreeWorkbench from '@/components/ScenarioTreeWorkbench.vue'
import { api, taskGenerationDownloadUrl } from '@/api'
import type { TaskGenerationJob, TaskGenerationResult } from '@/types'

const jobs = ref<TaskGenerationJob[]>([])
const leafCount = ref(0)
const selectedJob = ref<TaskGenerationJob | null>(null)
const results = ref<TaskGenerationResult[]>([])
const resultErrors = ref<TaskGenerationJob['errors']>([])
const editingId = ref<string | null>(null)
const editingText = ref('')
let pollTimer: number | undefined
let disposed = false
let selectionEpoch = 0
const taskJobs = computed(() => jobs.value.filter(job => job.kind === 'task_generation'))
const isActive = (job: TaskGenerationJob) => ['queued', 'running'].includes(job.status)
const activeJob = computed(() => selectedJob.value && isActive(selectedJob.value) ? selectedJob.value : null)
const activeResults = computed(() => results.value.filter(item => !item.deleted))

async function selectJob(job: TaskGenerationJob) {
  const epoch = ++selectionEpoch
  selectedJob.value = job
  results.value = []
  resultErrors.value = job.errors
  editingId.value = null
  if (!isActive(job)) {
    try {
      const payload = await api.taskGenerationResults(job.job_id)
      if (disposed || epoch !== selectionEpoch) return
      results.value = payload.results
      resultErrors.value = payload.errors
    } catch (error) { if (!disposed) ElMessage.error((error as Error).message) }
  }
  schedulePoll()
}

function schedulePoll() {
  if (pollTimer) window.clearTimeout(pollTimer)
  if (!disposed && taskJobs.value.some(isActive)) pollTimer = window.setTimeout(() => void refreshJobs(), 1500)
}

async function refreshJobs() {
  try {
    const values = await api.taskGenerationJobs()
    if (disposed) return
    jobs.value = values
    if (!selectedJob.value) {
      const first = taskJobs.value.find(isActive) || taskJobs.value[0]
      if (first) await selectJob(first)
    } else {
      const updated = values.find(job => job.job_id === selectedJob.value?.job_id)
      if (updated) {
        const finished = isActive(selectedJob.value) && !isActive(updated)
        selectedJob.value = updated
        if (finished) await selectJob(updated)
      }
    }
  } catch (error) { if (!disposed) ElMessage.error((error as Error).message) }
  finally { schedulePoll() }
}

async function created(job: TaskGenerationJob) {
  jobs.value = [job, ...jobs.value.filter(item => item.job_id !== job.job_id)]
  await selectJob(job)
}

function startEdit(item: TaskGenerationResult) {
  editingId.value = item.result_id
  editingText.value = item.task
}

async function saveEdit(item: TaskGenerationResult) {
  const jobId = selectedJob.value?.job_id
  if (!jobId) return
  try {
    const updated = await api.patchTaskGenerationResult(jobId, item.result_id, { task: editingText.value })
    if (selectedJob.value?.job_id !== jobId) return
    results.value = results.value.map(value => value.result_id === updated.result_id ? updated : value)
    editingId.value = null
  } catch (error) { ElMessage.error((error as Error).message) }
}

async function toggleDeleted(item: TaskGenerationResult) {
  const jobId = selectedJob.value?.job_id
  if (!jobId) return
  const action = item.deleted ? '恢复' : '删除'
  try {
    await ElMessageBox.confirm(`确定${action}这条任务${item.pre_dependency === 'weak' || item.pre_dependency === 'pre_node' ? '及其前置任务' : ''}吗？`, '确认操作', { type: 'warning' })
    const updated = await api.patchTaskGenerationResult(jobId, item.result_id, { deleted: !item.deleted })
    if (selectedJob.value?.job_id !== jobId) return
    results.value = results.value.map(value => value.result_id === updated.result_id || (updated.dependency_group_id && value.dependency_group_id === updated.dependency_group_id) ? { ...value, deleted: updated.deleted } : value)
  } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error((error as Error).message) }
}

async function exportResults() {
  const jobId = selectedJob.value?.job_id
  if (!jobId) return
  try {
    const exported = await api.taskGenerationExport(jobId)
    const link = document.createElement('a')
    link.href = taskGenerationDownloadUrl(jobId, exported.filename)
    link.download = exported.filename
    link.click()
  } catch (error) { ElMessage.error((error as Error).message) }
}

onMounted(() => void refreshJobs())
onBeforeUnmount(() => { disposed = true; selectionEpoch++; if (pollTimer) window.clearTimeout(pollTimer) })
</script>

<template>
  <div class="page task-generation-page">
    <header class="page-hero"><div><span class="eyebrow">TASK GENERATION</span><h1>任务生成</h1><p>从场景出发，维护任务类型与适用 App，批量生成可审核、可导出的 GUI Agent 任务。</p></div><div class="hero-metrics"><div><b>{{ leafCount }}</b><span>任务类型</span></div><div><b>{{ activeResults.length }}</b><span>当前结果</span></div><div><b>{{ taskJobs.length }}</b><span>历史作业</span></div></div></header>
    <ScenarioTreeWorkbench @created="created" @count-change="leafCount = $event" />
    <section class="jobs-card">
      <div class="section-title"><div><span class="eyebrow">JOB QUEUE</span><h2>作业与结果</h2></div><div><el-button :icon="Refresh" text @click="refreshJobs">刷新作业</el-button><el-button v-if="selectedJob && results.length" type="primary" plain :icon="Download" @click="exportResults">导出当前结果</el-button></div></div>
      <div v-if="taskJobs.length" class="job-tabs"><button v-for="job in taskJobs" :key="job.job_id" :class="{ active: selectedJob?.job_id === job.job_id }" @click="selectJob(job)"><b>{{ job.created_at.slice(0, 16).replace('T', ' ') }}</b><span>{{ job.status }} · {{ job.result_count }} 条</span></button></div>
      <p v-if="selectedJob" class="job-meta">知识库版本 {{ selectedJob.knowledge_base_version?.slice(0, 8) || '历史版本' }} · {{ selectedJob.total_items }} 个执行单元<span v-if="selectedJob.expected_main_tasks != null"> · 预计主任务 {{ selectedJob.expected_main_tasks }} 条</span></p>
      <div v-if="activeJob" class="progress-card"><div><b>{{ activeJob.stage }}</b><span>{{ activeJob.current_item || '等待执行' }} · {{ activeJob.completed_items }} / {{ activeJob.total_items }}</span></div><el-progress :percentage="activeJob.percent" /></div>
      <el-alert v-for="error in resultErrors" :key="`${error.item_id}-${error.stage}-${error.error}`" type="error" :title="`${error.item_id || '任务'}${error.stage ? ` · ${error.stage}` : ''}`" :description="error.error" :closable="false" show-icon />
      <el-alert v-if="selectedJob?.error && !resultErrors.length" type="error" :title="selectedJob.error" :closable="false" />
      <el-empty v-if="!selectedJob" description="提交作业后在这里查看结果" />
      <el-empty v-else-if="!results.length && !activeJob" description="作业没有生成可审核结果" />
      <el-table v-else-if="results.length" :data="results" stripe class="result-table">
        <el-table-column label="状态" width="92"><template #default="{ row }"><el-tag v-if="row.deleted" type="info">已删除</el-tag><el-tag v-else-if="row.pre_dependency === 'strong'" type="danger">强依赖</el-tag><el-tag v-else-if="row.pre_dependency === 'pre_node'" type="warning">前置任务</el-tag><el-tag v-else-if="row.pre_dependency === 'weak'" type="success">弱依赖</el-tag><el-tag v-else type="info">无依赖</el-tag></template></el-table-column>
        <el-table-column prop="app" label="App" width="110" />
        <el-table-column label="场景能力" min-width="230"><template #default="{ row }"><b>{{ row.scene }}</b><span class="cell-sub">{{ row.capability }} / {{ row.sub_capability }}</span></template></el-table-column>
        <el-table-column label="任务" min-width="420"><template #default="{ row }"><el-input v-if="editingId === row.result_id" v-model="editingText" type="textarea" :rows="2" /><span v-else :class="{ deleted: row.deleted }">{{ row.task }}</span></template></el-table-column>
        <el-table-column label="操作" width="160" fixed="right"><template #default="{ row }"><template v-if="editingId === row.result_id"><el-button link type="primary" @click="saveEdit(row)">保存</el-button><el-button link @click="editingId = null">取消</el-button></template><template v-else><el-button link type="primary" :disabled="row.deleted" @click="startEdit(row)">编辑</el-button><el-button link :type="row.deleted ? 'success' : 'danger'" @click="toggleDeleted(row)">{{ row.deleted ? '恢复' : '删除' }}</el-button></template></template></el-table-column>
      </el-table>
    </section>
  </div>
</template>

<style scoped>
.task-generation-page{width:min(1680px,100%);margin:0 auto}.jobs-card{margin-top:20px;padding:22px;border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.82);box-shadow:0 12px 35px rgba(15,23,42,.045)}.section-title{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:14px;flex-wrap:wrap}.section-title h2{margin:4px 0 0;font-size:21px}.job-tabs{display:flex;gap:8px;overflow-x:auto;padding-bottom:12px}.job-tabs button{display:grid;gap:4px;min-width:170px;padding:10px 12px;border:1px solid var(--line);border-radius:11px;background:#fff;color:var(--ink);text-align:left;cursor:pointer}.job-tabs button.active{border-color:#5eead4;background:#f0fdfa}.job-tabs span,.job-meta{color:var(--muted);font-size:11px}.progress-card{display:flex;align-items:center;gap:20px;margin-bottom:12px;padding:13px 15px;border:1px solid #bfdbfe;border-radius:12px;background:#eff6ff}.progress-card>div{display:grid;gap:4px;min-width:220px}.progress-card span{color:var(--muted);font-size:12px}.progress-card .el-progress{flex:1}.result-table{margin-top:12px}.cell-sub{display:block;margin-top:4px;color:var(--muted);font-size:11px}.deleted{color:#94a3b8;text-decoration:line-through}.el-alert{margin:7px 0}@media(max-width:1050px){.progress-card{align-items:stretch;flex-direction:column;gap:8px}.progress-card .el-progress{width:100%}}
</style>
