<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Cpu, EditPen, Refresh, Search, Setting, VideoPlay, View } from '@element-plus/icons-vue'
import { api } from '@/api'
import type { DatasetRelease, ModelArtifact, TrainingMixtureJob } from '@/types'
import { readModelPublishingState } from '@/utils/modelPublishingStore'
import { createTrainingMixtureJob, readTrainingMixtureState, updateTrainingMixtureJob } from '@/utils/trainingMixtureStore'

const datasets = ref<DatasetRelease[]>([])
const models = ref<ModelArtifact[]>([])
const jobs = ref<TrainingMixtureJob[]>([])
const loading = ref(true)
const pageError = ref('')
const teacherDialog = ref(false)
const historySearch = ref('')
const historyStatus = ref<'all' | TrainingMixtureJob['status']>('all')
const activeJob = ref<TrainingMixtureJob | null>(null)
const selectedJob = ref<TrainingMixtureJob | null>(null)
const detailVisible = ref(false)
let timer: ReturnType<typeof setTimeout> | null = null

const form = reactive({
  datasetReleaseId: '',
  studentArtifactId: '',
  teacherModelName: '',
  teacherModelPath: '',
  maxWorkers: '4',
  passK: '4',
  outputDirectory: '/training-platform/data-mixture-results/',
})
const teacherDraft = reactive({ name: '', path: '' })
const publishedModels = computed(() => models.value.filter(item => item.release_status === 'published'))
const maxWorkersError = computed(() => integerFieldError(form.maxWorkers, 1, 64, 'max_workers'))
const passKError = computed(() => integerFieldError(form.passK, 1, 20, 'K 值'))
const canStart = computed(() => Boolean(
  form.datasetReleaseId
  && form.studentArtifactId
  && form.teacherModelName.trim()
  && form.teacherModelPath.trim()
  && form.outputDirectory.trim()
  && !maxWorkersError.value
  && !passKError.value
  && !activeJob.value,
))
const succeededCount = computed(() => jobs.value.filter(item => item.status === 'succeeded').length)
const filteredJobs = computed(() => {
  const keyword = historySearch.value.trim().toLowerCase()
  return jobs.value.filter((job) => {
    const matchesText = !keyword || [job.job_id, job.dataset_name, job.student_model_name, job.teacher_model_name, job.result_filename].some(value => value.toLowerCase().includes(keyword))
    return matchesText && (historyStatus.value === 'all' || job.status === historyStatus.value)
  })
})

function formatDate(value: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}
function statusText(status: TrainingMixtureJob['status']) { return { running: '运行中', succeeded: '已完成', failed: '失败', interrupted: '已中断' }[status] }
function statusType(status: TrainingMixtureJob['status']) {
  if (status === 'succeeded') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'interrupted') return 'warning'
  return 'primary'
}
function stageText(stage: TrainingMixtureJob['stage']) {
  return { student_pass_at_k: 'Student 模型 Pass@K', teacher_pass_at_k: 'Teacher 模型 Pass@K', writing_result: '生成结果 Excel', completed: '执行完成', interrupted: '执行中断' }[stage]
}

function sanitizeIntegerInput(value: string) {
  return value.replace(/\D/g, '')
}

function integerFieldError(value: string, min: number, max: number, label: string) {
  if (!value) return `请输入${label}`
  const parsed = Number(value)
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) return `${label}必须是 ${min}～${max} 的整数`
  return ''
}

function reloadLocalState() {
  const modelState = readModelPublishingState()
  models.value = modelState.artifacts
  jobs.value = readTrainingMixtureState().jobs
  activeJob.value = jobs.value.find(item => item.status === 'running') || null
}

async function loadPage() {
  loading.value = true
  pageError.value = ''
  try {
    datasets.value = await api.datasetReleases()
    reloadLocalState()
    if (activeJob.value) scheduleProgress()
  } catch (error) {
    pageError.value = (error as Error).message
    reloadLocalState()
  } finally { loading.value = false }
}

function openTeacherConfig() {
  teacherDraft.name = form.teacherModelName
  teacherDraft.path = form.teacherModelPath
  teacherDialog.value = true
}
function saveTeacherConfig() {
  if (!teacherDraft.name.trim() || !teacherDraft.path.trim()) { ElMessage.warning('请填写 Teacher 模型名称和路径'); return }
  form.teacherModelName = teacherDraft.name.trim()
  form.teacherModelPath = teacherDraft.path.trim()
  teacherDialog.value = false
}
function stopTimer() { if (timer) clearTimeout(timer); timer = null }
function scheduleProgress() {
  stopTimer()
  if (!activeJob.value || activeJob.value.status !== 'running') return
  timer = setTimeout(() => {
    const job = activeJob.value
    if (!job || job.status !== 'running') return
    job.progress = Math.min(100, job.progress + 5)
    if (job.progress < 45) job.stage = 'student_pass_at_k'
    else if (job.progress < 90) job.stage = 'teacher_pass_at_k'
    else if (job.progress < 100) job.stage = 'writing_result'
    else {
      job.status = 'succeeded'
      job.stage = 'completed'
      job.completed_at = new Date().toISOString()
    }
    updateTrainingMixtureJob(job)
    jobs.value = readTrainingMixtureState().jobs
    if (job.status === 'succeeded') {
      activeJob.value = null
      ElMessage.success('前端演示任务已完成，结果文件信息已加入历史记录')
    } else scheduleProgress()
  }, 500)
}

function startMixture() {
  const dataset = datasets.value.find(item => item.release_id === form.datasetReleaseId)
  const student = publishedModels.value.find(item => item.artifact_id === form.studentArtifactId)
  if (!dataset || !student) { ElMessage.warning('请选择有效的发布数据集和已发布模型'); return }
  if (maxWorkersError.value || passKError.value) {
    ElMessage.warning(maxWorkersError.value || passKError.value)
    return
  }
  try {
    const job = createTrainingMixtureJob({
      datasetReleaseId: dataset.release_id,
      datasetName: dataset.name,
      studentArtifactId: student.artifact_id,
      studentModelName: student.model_version,
      teacherModelName: form.teacherModelName,
      teacherModelPath: form.teacherModelPath,
      maxWorkers: Number(form.maxWorkers),
      passK: Number(form.passK),
      outputDirectory: form.outputDirectory,
    })
    activeJob.value = job
    jobs.value = readTrainingMixtureState().jobs
    scheduleProgress()
  } catch (error) { ElMessage.error((error as Error).message) }
}
function showDetails(job: TrainingMixtureJob) { selectedJob.value = job; detailVisible.value = true }

onMounted(loadPage)
onBeforeUnmount(stopTimer)
</script>

<template>
  <div class="page mixture-page">
    <header class="page-hero mixture-hero">
      <div><span class="eyebrow">TRAINING DATA MIXTURE</span><h1>训练数据配比</h1><p>在发布数据集上对比 Student 与 Teacher 模型的 Pass@K 表现，并形成可追溯的配比结果。</p></div>
      <el-button :icon="Refresh" :loading="loading" @click="loadPage">刷新资源</el-button>
    </header>

    <el-alert title="当前为前端流程演示：进度和结果文件信息会保存在浏览器中，但不会真正执行模型推理、访问模型路径或创建 Excel。" type="info" :closable="false" show-icon />
    <el-alert v-if="pageError" :title="`数据集接口加载失败：${pageError}`" type="warning" :closable="false" show-icon />

    <section class="metrics">
      <div class="metric"><b>{{ datasets.length }}</b><span>发布数据集</span></div><div class="metric"><b>{{ publishedModels.length }}</b><span>已发布模型</span></div><div class="metric"><b>{{ jobs.length }}</b><span>历史配比任务</span></div><div class="metric"><b>{{ succeededCount }}</b><span>已完成</span></div>
    </section>

    <section class="panel configuration-panel" v-loading="loading">
      <div class="section-heading"><div><h2>新建数据配比任务</h2><p>同一数据集分别运行 Student 和 Teacher 的 Pass@K。</p></div><el-tag v-if="activeJob" type="primary">已有任务运行中</el-tag></div>
      <el-form label-position="top" class="configuration-grid" @submit.prevent="startMixture">
        <div class="resource-column">
          <el-form-item label="选择数据集" required>
            <el-select v-model="form.datasetReleaseId" filterable placeholder="选择发布的数据集"><el-option v-for="dataset in datasets" :key="dataset.release_id" :value="dataset.release_id" :label="`${dataset.name} · ${dataset.release_id}`"><div class="select-option"><b>{{ dataset.name }}</b><span>{{ dataset.release_id }} · {{ dataset.trajectory_count }} 轨迹</span></div></el-option></el-select>
          </el-form-item>
          <el-form-item label="Student 模型（已发布）" required>
            <el-select v-model="form.studentArtifactId" filterable placeholder="选择发布的模型"><el-option v-for="model in publishedModels" :key="model.artifact_id" :value="model.artifact_id" :label="`${model.model_version} · ${model.filename}`" /></el-select>
            <span v-if="!publishedModels.length" class="field-warning">模型发布模块中暂无“已发布”模型。</span>
          </el-form-item>
          <el-form-item label="Teacher 模型" required>
            <button type="button" class="teacher-card" @click="openTeacherConfig"><el-icon><Cpu /></el-icon><span v-if="form.teacherModelName"><b>{{ form.teacherModelName }}</b><small>{{ form.teacherModelPath }}</small></span><span v-else><b>配置 Teacher 模型</b></span><el-icon><EditPen /></el-icon></button>
          </el-form-item>
          <el-form-item label="结果保存目录" required><el-input v-model="form.outputDirectory" placeholder="配比结果 Excel 保存目录" /></el-form-item>
        </div>
        <div class="parameter-column">
          <el-form-item label="max_workers" required :error="maxWorkersError">
            <el-input v-model="form.maxWorkers" class="parameter-input" inputmode="numeric" placeholder="1～64" @input="form.maxWorkers = sanitizeIntegerInput($event)" />
          </el-form-item>
          <el-form-item label="Pass@K 的 K 值" required :error="passKError">
            <el-input v-model="form.passK" class="parameter-input" inputmode="numeric" placeholder="1～20" @input="form.passK = sanitizeIntegerInput($event)" />
          </el-form-item>
        </div>
        <div class="start-row"><el-button native-type="submit" type="primary" size="large" :icon="VideoPlay" :disabled="!canStart">启动数据配比</el-button><span>文件名自动使用：Teacher模型名-Student模型名-数据集名.xlsx</span></div>
      </el-form>
    </section>

    <section v-if="activeJob" class="panel active-panel">
      <div class="active-head"><div><span class="running-dot" /><b>{{ stageText(activeJob.stage) }}</b><code>{{ activeJob.job_id }}</code></div><strong>{{ activeJob.progress }}%</strong></div>
      <el-progress :percentage="activeJob.progress" />
      <p>{{ activeJob.dataset_name }} · Student {{ activeJob.student_model_name }} · Teacher {{ activeJob.teacher_model_name }} · Pass@{{ activeJob.pass_k }}</p>
    </section>

    <section class="panel history-panel">
      <div class="section-heading history-heading"><div><h2>历史数据配比结果</h2><p>保留任务参数、执行过程、结果文件名和保存目录。</p></div><div class="filters"><el-input v-model="historySearch" :prefix-icon="Search" clearable placeholder="搜索任务、数据集或模型" /><el-select v-model="historyStatus"><el-option label="全部状态" value="all" /><el-option label="运行中" value="running" /><el-option label="已完成" value="succeeded" /><el-option label="失败" value="failed" /><el-option label="已中断" value="interrupted" /></el-select></div></div>
      <el-table v-if="filteredJobs.length" :data="filteredJobs" row-key="job_id">
        <el-table-column label="数据集" min-width="210"><template #default="{ row }"><div class="primary-cell"><b>{{ row.dataset_name }}</b><code>{{ row.job_id }}</code></div></template></el-table-column>
        <el-table-column label="模型组合" min-width="220"><template #default="{ row }"><span>{{ row.teacher_model_name }} → {{ row.student_model_name }}</span></template></el-table-column>
        <el-table-column label="参数" width="150"><template #default="{ row }">workers {{ row.max_workers }} · Pass@{{ row.pass_k }}</template></el-table-column>
        <el-table-column label="当前阶段" min-width="170"><template #default="{ row }">{{ stageText(row.stage) }}</template></el-table-column>
        <el-table-column label="结果文件" min-width="260" show-overflow-tooltip prop="result_filename" />
        <el-table-column label="状态" width="110"><template #default="{ row }"><el-tag :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="95" fixed="right"><template #default="{ row }"><el-button text :icon="View" @click="showDetails(row)">详情</el-button></template></el-table-column>
      </el-table>
      <el-empty v-else description="暂无数据配比任务" :image-size="78" />
    </section>

    <el-dialog v-model="teacherDialog" title="配置 Teacher 模型" width="min(560px, 92vw)">
      <el-form label-position="top"><el-form-item label="模型名称" required><el-input v-model="teacherDraft.name" placeholder="例如：qwen3-vl-72b-teacher" /></el-form-item><el-form-item label="模型路径" required><el-input v-model="teacherDraft.path" placeholder="例如：/models/qwen3-vl-72b/" /></el-form-item></el-form>
      <template #footer><el-button @click="teacherDialog = false">取消</el-button><el-button type="primary" :icon="Setting" @click="saveTeacherConfig">保存配置</el-button></template>
    </el-dialog>

    <el-dialog v-model="detailVisible" title="数据配比任务详情" width="min(760px, 92vw)">
      <el-descriptions v-if="selectedJob" :column="2" border>
        <el-descriptions-item label="任务 ID">{{ selectedJob.job_id }}</el-descriptions-item><el-descriptions-item label="状态"><el-tag :type="statusType(selectedJob.status)">{{ statusText(selectedJob.status) }}</el-tag></el-descriptions-item>
        <el-descriptions-item label="数据集">{{ selectedJob.dataset_name }}</el-descriptions-item><el-descriptions-item label="发布 ID">{{ selectedJob.dataset_release_id }}</el-descriptions-item>
        <el-descriptions-item label="Student 模型">{{ selectedJob.student_model_name }}</el-descriptions-item><el-descriptions-item label="Teacher 模型">{{ selectedJob.teacher_model_name }}</el-descriptions-item>
        <el-descriptions-item label="Teacher 路径" :span="2">{{ selectedJob.teacher_model_path }}</el-descriptions-item><el-descriptions-item label="参数">max_workers={{ selectedJob.max_workers }}, Pass@{{ selectedJob.pass_k }}</el-descriptions-item><el-descriptions-item label="创建时间">{{ formatDate(selectedJob.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="结果文件" :span="2">{{ selectedJob.result_filename }}</el-descriptions-item><el-descriptions-item label="结果路径" :span="2">{{ selectedJob.result_path }}</el-descriptions-item>
      </el-descriptions>
    </el-dialog>
  </div>
</template>

<style scoped>
.mixture-page { display: grid; gap: 18px; }
.mixture-hero { align-items: flex-start; }.mixture-hero .el-button { margin-top: 4px; }
.metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.metric { display: grid; gap: 4px; padding: 16px 18px; border: 1px solid var(--line); border-radius: 12px; background: var(--panel); }.metric b { font-size: 25px; font-weight: 650; }.metric span { color: var(--muted); font-size: 13px; }
.panel { padding: 20px; border: 1px solid var(--line); border-radius: 14px; background: var(--panel); }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 18px; }.section-heading h2 { margin: 0; font-size: 18px; }.section-heading p { margin: 5px 0 0; color: var(--muted); font-size: 13px; }
.configuration-grid { display: grid; grid-template-columns: minmax(280px, 34%) 180px minmax(0, 1fr); align-items: start; gap: 4px 32px; }.resource-column,.parameter-column { display: grid; align-content: start; }.configuration-grid :deep(.el-select) { width: 100%; }.parameter-column { justify-items: start; }.parameter-column :deep(.el-form-item) { width: 100%; }.parameter-input { width: 132px; }
.select-option { display: grid; line-height: 1.25; }.select-option span { color: var(--muted); font-size: 11px; }
.field-warning { margin-top: 5px; color: #b45309; font-size: 12px; }
.teacher-card { display: grid; grid-template-columns: auto minmax(0,1fr) auto; align-items: center; gap: 9px; width: 100%; min-height: 40px; padding: 5px 10px; border: 1px solid var(--line); border-radius: 6px; background: white; color: var(--ink); cursor: pointer; text-align: left; }.teacher-card:hover { border-color: var(--accent); }.teacher-card span { display: grid; min-width: 0; }.teacher-card b { font-size: 13px; font-weight: 600; }.teacher-card small { overflow: hidden; color: var(--muted); font-size: 11px; line-height: 1.25; text-overflow: ellipsis; white-space: nowrap; }
.start-row { grid-column: 1/-1; display: flex; align-items: center; gap: 14px; padding-top: 5px; }.start-row span { color: var(--muted); font-size: 12px; }
.active-head,.active-head>div { display: flex; align-items: center; gap: 10px; }.active-head { justify-content: space-between; margin-bottom: 10px; }.active-head code,.active-panel p { color: var(--muted); font-size: 12px; }.running-dot { width: 8px; height: 8px; border-radius: 50%; background: #3b82f6; box-shadow: 0 0 0 5px rgb(59 130 246 / 12%); }.active-panel p { margin: 8px 0 0; }
.history-heading { align-items: center; }.filters { display: grid; grid-template-columns: 250px 130px; gap: 10px; }.primary-cell { display: grid; gap: 4px; }.primary-cell code { color: var(--muted); font-size: 11px; }
@media (max-width: 1050px) { .configuration-grid { grid-template-columns: minmax(260px, 42%) 170px minmax(0,1fr); gap: 4px 20px; } .metrics { grid-template-columns: repeat(2,1fr); } .history-heading { align-items: flex-start; flex-direction: column; }.filters { width: 100%; } }
@media (max-width: 680px) { .configuration-grid { grid-template-columns: 1fr; }.parameter-column { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }.start-row { align-items: flex-start; flex-direction: column; }.filters { grid-template-columns: 1fr; } }
</style>
