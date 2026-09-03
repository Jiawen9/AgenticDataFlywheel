<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { FolderOpened, Promotion, Refresh, Search, VideoPlay, View } from '@element-plus/icons-vue'
import type { ModelPublishingState, ModelTrainingMonitor, TrainingMixtureJob } from '@/types'
import {
  createTrainingMonitor,
  emptyModelPublishingState,
  MODEL_PUBLISHING_EVENT,
  readModelPublishingState,
} from '@/utils/modelPublishingStore'
import { readTrainingMixtureState, TRAINING_MIXTURE_EVENT } from '@/utils/trainingMixtureStore'

type OptimizationAlgorithm = 'CHORD' | 'OPSD' | 'GKD' | 'OPD-RL'
const OPTIMIZATION_ALGORITHMS: OptimizationAlgorithm[] = ['CHORD', 'OPSD', 'GKD', 'OPD-RL']
const TRAINING_JOBS: Record<OptimizationAlgorithm, string[]> = {
  CHORD: [],
  OPSD: ['opd-rl_vla-qwen3vl-8b_teacher-qwen3vl-32b'],
  GKD: ['gkd_vla-qwen3vl-8b_teacher-qwen3vl-32b'],
  'OPD-RL': [],
}

const router = useRouter()
const modelState = ref<ModelPublishingState>(emptyModelPublishingState())
const mixtureJobs = ref<TrainingMixtureJob[]>([])
const latestMonitor = ref<ModelTrainingMonitor | null>(null)
const selectedMonitor = ref<ModelTrainingMonitor | null>(null)
const detailVisible = ref(false)
const historySearch = ref('')
const submitting = ref(false)

const form = reactive({
  modelArtifactId: '',
  trainingName: '',
  optimizationAlgorithm: 'GKD' as OptimizationAlgorithm,
  trainingJob: TRAINING_JOBS.GKD[0],
  mixtureJobId: '',
  resultDirectory: '/training-platform/model-results/',
  cardsPerNode: '8',
  instanceCount: '1',
})

const publishedModels = computed(() => modelState.value.artifacts.filter(item => item.release_status === 'published'))
const availableDatasets = computed(() => mixtureJobs.value.filter(item => item.status === 'succeeded'))
const availableTrainingJobs = computed(() => TRAINING_JOBS[form.optimizationAlgorithm])
const selectedDataset = computed(() => availableDatasets.value.find(item => item.job_id === form.mixtureJobId) || null)
const cardsError = computed(() => integerError(form.cardsPerNode, 1, 16, '单节点卡数'))
const instancesError = computed(() => integerError(form.instanceCount, 1, 128, '实例数'))
const canStart = computed(() => Boolean(
  form.modelArtifactId
  && form.trainingName.trim()
  && form.trainingJob
  && selectedDataset.value
  && form.resultDirectory.trim()
  && !cardsError.value
  && !instancesError.value
  && !submitting.value,
))
const filteredMonitors = computed(() => {
  const keyword = historySearch.value.trim().toLowerCase()
  if (!keyword) return modelState.value.monitors
  return modelState.value.monitors.filter(item => [
    item.monitor_id,
    item.training_name,
    item.target_model,
    item.optimization_algorithm || '',
    item.training_job || '',
    item.result_directory,
  ].some(value => value.toLowerCase().includes(keyword)))
})

function sanitizeInteger(value: string) { return value.replace(/\D/g, '') }
function integerError(value: string, min: number, max: number, label: string) {
  if (!value) return `请输入${label}`
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= min && parsed <= max ? '' : `${label}必须是 ${min}～${max} 的整数`
}
function formatDate(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}
function loadResources() {
  modelState.value = readModelPublishingState()
  mixtureJobs.value = readTrainingMixtureState().jobs
}
function showMonitor(monitor: ModelTrainingMonitor) {
  selectedMonitor.value = monitor
  detailVisible.value = true
}
function startTraining() {
  const model = publishedModels.value.find(item => item.artifact_id === form.modelArtifactId)
  const dataset = selectedDataset.value
  if (!model || !dataset) { ElMessage.warning('请选择有效的已发布模型和训练数据集'); return }
  if (cardsError.value || instancesError.value) { ElMessage.warning(cardsError.value || instancesError.value); return }
  submitting.value = true
  try {
    latestMonitor.value = createTrainingMonitor({
      trainingName: form.trainingName,
      targetModel: model.model_version,
      resultDirectory: form.resultDirectory,
      modelArtifactId: model.artifact_id,
      optimizationAlgorithm: form.optimizationAlgorithm,
      trainingJob: form.trainingJob,
      mixtureJobId: dataset.job_id,
      trainDatasetPath: dataset.train_dataset_path,
      evalDatasetPath: dataset.eval_dataset_path,
      cardsPerNode: Number(form.cardsPerNode),
      instanceCount: Number(form.instanceCount),
    })
    loadResources()
    ElMessage.success('训练任务已拉起，并已创建模型结果监测任务')
    form.trainingName = ''
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    submitting.value = false
  }
}

watch(() => form.optimizationAlgorithm, () => {
  form.trainingJob = availableTrainingJobs.value[0] || ''
})

onMounted(() => {
  loadResources()
  window.addEventListener(MODEL_PUBLISHING_EVENT, loadResources)
  window.addEventListener(TRAINING_MIXTURE_EVENT, loadResources)
  window.addEventListener('storage', loadResources)
})
onBeforeUnmount(() => {
  window.removeEventListener(MODEL_PUBLISHING_EVENT, loadResources)
  window.removeEventListener(TRAINING_MIXTURE_EVENT, loadResources)
  window.removeEventListener('storage', loadResources)
})
</script>

<template>
  <div class="page training-launch-page">
    <header class="page-hero training-hero">
      <div><span class="eyebrow">MODEL TRAINING</span><h1>拉起训练</h1><p>选择已发布模型和训练数据集，配置训练作业及资源后创建训练监测任务。</p></div>
      <el-button :icon="Refresh" @click="loadResources">刷新资源</el-button>
    </header>

    <el-alert title="当前为前端流程演示：配置会保存在浏览器中，但不会真正提交训练平台、读取数据集或访问输出目录。" type="info" :closable="false" show-icon />

    <section class="panel configuration-panel">
      <div class="section-heading">
        <div><h2>创建训练任务</h2><p>完成配置后，模型发布模块会持续等待输出目录中的模型结果。</p></div>
        <el-tag type="info">前端演示</el-tag>
      </div>

      <el-form label-position="top" class="training-form" @submit.prevent="startTraining">
        <div class="form-column">
          <el-form-item label="训练名称" required><el-input v-model="form.trainingName" maxlength="80" show-word-limit placeholder="例如：GUI Agent 爱奇艺能力增训 v1" /></el-form-item>
          <el-form-item label="选择模型" required>
            <el-select v-model="form.modelArtifactId" filterable placeholder="选择模型发布中的已发布模型">
              <el-option v-for="model in publishedModels" :key="model.artifact_id" :value="model.artifact_id" :label="`${model.model_version} · ${model.filename}`" />
            </el-select>
            <span v-if="!publishedModels.length" class="field-warning">暂无已发布模型，请先前往模型发布页面。</span>
          </el-form-item>
          <el-form-item label="选择优化算法" required>
            <el-select v-model="form.optimizationAlgorithm"><el-option v-for="algorithm in OPTIMIZATION_ALGORITHMS" :key="algorithm" :label="algorithm" :value="algorithm" /></el-select>
          </el-form-item>
          <el-form-item label="选择训练作业" required>
            <el-select v-model="form.trainingJob" :disabled="!availableTrainingJobs.length" :placeholder="availableTrainingJobs.length ? '选择当前算法下的训练作业' : '当前优化算法暂无训练作业'"><el-option v-for="job in availableTrainingJobs" :key="job" :label="job" :value="job" /></el-select>
          </el-form-item>
          <el-form-item label="选择训练数据集" required>
            <el-select v-model="form.mixtureJobId" filterable placeholder="选择已完成的数据配比结果">
              <el-option v-for="job in availableDatasets" :key="job.job_id" :value="job.job_id" :label="`${job.dataset_name} · ${job.teacher_model_name} → ${job.student_model_name}`">
                <div class="dataset-option"><b>{{ job.dataset_name }}</b><span>{{ job.teacher_model_name }} → {{ job.student_model_name }} · {{ formatDate(job.completed_at) }}</span></div>
              </el-option>
            </el-select>
          </el-form-item>
          <el-form-item label="输入训练输出目录" required>
            <el-input v-model="form.resultDirectory" maxlength="300" placeholder="其他训练平台输出模型文件的目录"><template #prefix><el-icon><FolderOpened /></el-icon></template></el-input>
          </el-form-item>
        </div>

        <div class="form-column resource-column">
          <div class="resource-fields">
            <el-form-item label="单节点卡数" required :error="cardsError"><el-input v-model="form.cardsPerNode" inputmode="numeric" placeholder="1～16" @input="form.cardsPerNode = sanitizeInteger($event)" /></el-form-item>
            <el-form-item label="实例数" required :error="instancesError"><el-input v-model="form.instanceCount" inputmode="numeric" placeholder="1～128" @input="form.instanceCount = sanitizeInteger($event)" /></el-form-item>
          </div>
        </div>

        <div class="form-actions">
          <el-button native-type="submit" type="primary" size="large" :loading="submitting" :disabled="!canStart"><el-icon><VideoPlay /></el-icon>拉起训练并监测</el-button>
        </div>
      </el-form>
    </section>

    <section v-if="latestMonitor" class="latest-monitor">
      <div><span>本次训练已登记</span><b>{{ latestMonitor.training_name }}</b><code>{{ latestMonitor.monitor_id }}</code></div>
      <div><span>目标模型</span><b>{{ latestMonitor.target_model }}</b></div>
      <div><span>资源</span><b>{{ latestMonitor.cards_per_node }} 卡/节点 × {{ latestMonitor.instance_count }} 实例</b></div>
      <el-button :icon="Promotion" @click="router.push('/model-publishing')">查看模型发布监测</el-button>
    </section>

    <section class="panel history-panel">
      <div class="section-heading history-heading">
        <div><h2>历史拉起训练任务</h2><p>展示本地保存的训练配置和持续监测任务。</p></div>
        <el-input v-model="historySearch" :prefix-icon="Search" clearable placeholder="搜索训练、模型、作业或目录" />
      </div>
      <el-table v-if="filteredMonitors.length" :data="filteredMonitors" row-key="monitor_id">
        <el-table-column label="训练任务" min-width="210"><template #default="{ row }"><div class="primary-cell"><b>{{ row.training_name }}</b><code>{{ row.monitor_id }}</code></div></template></el-table-column>
        <el-table-column prop="target_model" label="模型" min-width="170" />
        <el-table-column label="优化算法" width="115"><template #default="{ row }">{{ row.optimization_algorithm || '—' }}</template></el-table-column>
        <el-table-column label="训练作业" min-width="260" show-overflow-tooltip><template #default="{ row }">{{ row.training_job || '旧版任务' }}</template></el-table-column>
        <el-table-column label="资源" width="150"><template #default="{ row }">{{ row.cards_per_node ? `${row.cards_per_node} 卡 × ${row.instance_count} 实例` : '—' }}</template></el-table-column>
        <el-table-column label="输出目录" min-width="230" show-overflow-tooltip prop="result_directory" />
        <el-table-column label="时间" min-width="170"><template #default="{ row }">{{ formatDate(row.started_at) }}</template></el-table-column>
        <el-table-column label="状态" width="115"><template #default><el-tag type="primary">持续监测</el-tag></template></el-table-column>
        <el-table-column label="操作" width="90" fixed="right"><template #default="{ row }"><el-button text :icon="View" @click="showMonitor(row)">详情</el-button></template></el-table-column>
      </el-table>
      <el-empty v-else description="暂无拉起训练任务" :image-size="76" />
    </section>

    <el-dialog v-model="detailVisible" title="训练任务详情" width="min(780px, 92vw)">
      <el-descriptions v-if="selectedMonitor" :column="2" border>
        <el-descriptions-item label="训练名称">{{ selectedMonitor.training_name }}</el-descriptions-item><el-descriptions-item label="监测 ID">{{ selectedMonitor.monitor_id }}</el-descriptions-item>
        <el-descriptions-item label="选择模型">{{ selectedMonitor.target_model }}</el-descriptions-item><el-descriptions-item label="模型 ID">{{ selectedMonitor.model_artifact_id || '旧版任务未记录' }}</el-descriptions-item>
        <el-descriptions-item label="优化算法">{{ selectedMonitor.optimization_algorithm || '旧版任务未记录' }}</el-descriptions-item>
        <el-descriptions-item label="训练作业" :span="2">{{ selectedMonitor.training_job || '旧版任务未记录' }}</el-descriptions-item>
        <el-descriptions-item label="单节点卡数">{{ selectedMonitor.cards_per_node ?? '—' }}</el-descriptions-item><el-descriptions-item label="实例数">{{ selectedMonitor.instance_count ?? '—' }}</el-descriptions-item>
        <el-descriptions-item label="训练输出目录" :span="2">{{ selectedMonitor.result_directory }}</el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ formatDate(selectedMonitor.started_at) }}</el-descriptions-item><el-descriptions-item label="状态"><el-tag type="primary">持续监测</el-tag></el-descriptions-item>
      </el-descriptions>
    </el-dialog>
  </div>
</template>

<style scoped>
.training-launch-page { display: grid; gap: 18px; }
.training-hero { align-items: flex-start; }.training-hero .el-button { margin-top: 4px; }
.panel { padding: 22px; border: 1px solid var(--line); border-radius: 14px; background: var(--panel); }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 20px; }.section-heading h2 { margin: 0; font-size: 19px; }.section-heading p { margin: 5px 0 0; color: var(--muted); font-size: 13px; }
.training-form { display: grid; grid-template-columns: minmax(280px, 34%) 180px minmax(0, 1fr); align-items: start; gap: 4px 32px; }.form-column { min-width: 0; }.training-form :deep(.el-select) { width: 100%; }
.resource-fields { display: grid; gap: 4px; }.resource-fields :deep(.el-input) { width: 132px; }
.field-warning { display: block; margin-top: 5px; color: #b45309; font-size: 12px; }
.dataset-option { display: grid; line-height: 1.3; }.dataset-option span { color: var(--muted); font-size: 11px; }
.form-actions { grid-column: 1/-1; display: flex; align-items: center; gap: 6px; padding-top: 6px; }
.latest-monitor { display: grid; grid-template-columns: minmax(0,1.2fr) minmax(0,1fr) minmax(180px,.7fr) auto; align-items: center; gap: 20px; padding: 16px 20px; border: 1px solid #bbf7d0; border-radius: 12px; background: #f0fdf4; }.latest-monitor>div { display: grid; gap: 3px; min-width: 0; }.latest-monitor span { color: #15803d; font-size: 11px; }.latest-monitor b,.latest-monitor code { overflow: hidden; color: #166534; text-overflow: ellipsis; white-space: nowrap; }.latest-monitor code { font-size: 11px; }
.history-heading { align-items: center; }.history-heading>.el-input { width: min(350px,100%); }.primary-cell { display: grid; gap: 4px; }.primary-cell code { color: var(--muted); font-size: 11px; }
@media (max-width: 1050px) { .training-form { grid-template-columns: minmax(260px, 42%) 170px minmax(0,1fr); gap: 4px 20px; }.latest-monitor { grid-template-columns: 1fr 1fr; } }
@media (max-width: 760px) { .training-form { grid-template-columns: 1fr; }.resource-fields { grid-template-columns: 1fr 1fr; gap: 12px; }.form-actions { align-items: flex-start; flex-direction: column; }.latest-monitor { grid-template-columns: 1fr; }.history-heading { align-items: flex-start; flex-direction: column; }.history-heading>.el-input { width: 100%; } }
</style>
