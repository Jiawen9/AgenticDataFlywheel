<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Download, Refresh, Search, VideoPlay, View } from '@element-plus/icons-vue'
import type { ModelPublishingState, TrainingMixtureState, TrainingValidationMetricGroup, TrainingValidationReport } from '@/types'
import { emptyModelPublishingState, MODEL_PUBLISHING_EVENT, readModelPublishingState } from '@/utils/modelPublishingStore'
import { emptyTrainingMixtureState, readTrainingMixtureState, TRAINING_MIXTURE_EVENT } from '@/utils/trainingMixtureStore'
import {
  createTrainingValidationReport,
  eligibleTrainingSessions,
  readTrainingValidationState,
  TRAINING_VALIDATION_EVENT,
  VALIDATION_COLUMNS,
} from '@/utils/trainingValidationStore'

const modelState = ref<ModelPublishingState>(emptyModelPublishingState())
const mixtureState = ref<TrainingMixtureState>(emptyTrainingMixtureState())
const reports = ref<TrainingValidationReport[]>([])
const selectedMonitorId = ref('')
const selectedArtifactId = ref('')
const expandedReportId = ref('')
const historySearch = ref('')
const validating = ref(false)

const sessions = computed(() => eligibleTrainingSessions(modelState.value, mixtureState.value))
const selectedSession = computed(() => sessions.value.find(item => item.monitor.monitor_id === selectedMonitorId.value) || null)
const checkpoints = computed(() => selectedSession.value?.checkpoints || [])
const canStart = computed(() => Boolean(selectedSession.value && selectedArtifactId.value && !validating.value))
const filteredReports = computed(() => {
  const keyword = historySearch.value.trim().toLowerCase()
  if (!keyword) return reports.value
  return reports.value.filter(report => [report.report_id, report.training_name, report.trained_model, report.checkpoint_version, report.dataset_name].some(value => value.toLowerCase().includes(keyword)))
})

const metricLabels: Array<[keyof TrainingValidationMetricGroup, string]> = [
  ['teacher', 'Teacher'],
  ['student_before', 'Student 训练前'],
  ['student_after', 'Student 训练后'],
  ['improvement', '提升（绝对值）'],
]

function loadState() {
  modelState.value = readModelPublishingState()
  mixtureState.value = readTrainingMixtureState()
  reports.value = readTrainingValidationState().reports
}
function formatDate(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}
function formatMetric(value: number, improvement = false) {
  const text = value.toFixed(4)
  return improvement && value > 0 ? `+${text}` : text
}
function toggleReport(reportId: string) { expandedReportId.value = expandedReportId.value === reportId ? '' : reportId }
function startValidation() {
  const session = selectedSession.value
  if (!session || !selectedArtifactId.value) { ElMessage.warning('请选择训练完成会话和 checkpoint'); return }
  validating.value = true
  try {
    const report = createTrainingValidationReport(session, selectedArtifactId.value)
    reports.value = readTrainingValidationState().reports
    expandedReportId.value = report.report_id
    ElMessage.success('训练有效性验证演示报告已生成')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    validating.value = false
  }
}
function downloadReport() { ElMessage.info('当前为前端演示，等待后端报告下载服务接入') }

watch(selectedMonitorId, () => { selectedArtifactId.value = '' })
onMounted(() => {
  loadState()
  window.addEventListener(MODEL_PUBLISHING_EVENT, loadState)
  window.addEventListener(TRAINING_MIXTURE_EVENT, loadState)
  window.addEventListener(TRAINING_VALIDATION_EVENT, loadState)
  window.addEventListener('storage', loadState)
})
onBeforeUnmount(() => {
  window.removeEventListener(MODEL_PUBLISHING_EVENT, loadState)
  window.removeEventListener(TRAINING_MIXTURE_EVENT, loadState)
  window.removeEventListener(TRAINING_VALIDATION_EVENT, loadState)
  window.removeEventListener('storage', loadState)
})
</script>

<template>
  <div class="page validation-page">
    <header class="page-hero validation-hero">
      <div><span class="eyebrow">TRAINING VALIDATION</span><h1>训练有效性验证</h1><p>选择训练完成会话与模型 checkpoint，对比训练前后表现并形成验证报告。</p></div>
      <el-button :icon="Refresh" @click="loadState">刷新资源</el-button>
    </header>

    <el-alert title="当前使用固定指标生成前端演示报告，不会执行真实评测，也不会创建或下载报告文件。" type="info" :closable="false" show-icon />

    <section class="panel validation-config">
      <div class="section-heading"><div><h2>新建有效性验证</h2><p>检测到非失败模型产物的训练会话才可用于验证。</p></div><el-tag type="info">演示数据</el-tag></div>
      <div class="config-layout">
        <el-form label-position="top" class="selection-form" @submit.prevent="startValidation">
          <el-form-item label="选择训练完成会话" required>
            <el-select v-model="selectedMonitorId" filterable placeholder="选择训练会话">
              <el-option v-for="session in sessions" :key="session.monitor.monitor_id" :value="session.monitor.monitor_id" :label="`${session.monitor.training_name} · ${session.monitor.target_model}`">
                <div class="session-option"><b>{{ session.monitor.training_name }}</b><span>{{ session.monitor.target_model }} · {{ formatDate(session.monitor.started_at) }}</span></div>
              </el-option>
            </el-select>
          </el-form-item>

          <div class="readonly-grid" aria-label="训练与配比信息">
            <el-form-item label="配比数据集"><el-input :model-value="selectedSession?.mixtureJob.dataset_name || ''" readonly /></el-form-item>
            <el-form-item label="Pass@K 参数"><el-input :model-value="selectedSession ? `K = ${selectedSession.mixtureJob.pass_k}` : ''" readonly /></el-form-item>
            <el-form-item label="数据配比结果"><el-input :model-value="selectedSession?.mixtureJob.result_path || ''" readonly /></el-form-item>
          </div>

          <el-form-item label="选择模型 Checkpoint" required>
            <el-select v-model="selectedArtifactId" filterable :disabled="!selectedSession" placeholder="选择当前会话的 checkpoint">
              <el-option v-for="checkpoint in checkpoints" :key="checkpoint.artifact_id" :value="checkpoint.artifact_id" :label="`${checkpoint.model_version} · ${checkpoint.filename}`">
                <div class="session-option"><b>{{ checkpoint.model_version }}</b><span>{{ checkpoint.filename }} · {{ formatDate(checkpoint.detected_at) }}</span></div>
              </el-option>
            </el-select>
          </el-form-item>
          <el-button native-type="submit" type="primary" size="large" :icon="VideoPlay" :loading="validating" :disabled="!canStart">开始训练有效性验证</el-button>
        </el-form>
      </div>
      <el-empty v-if="!sessions.length" description="暂无包含可用模型 checkpoint 的训练完成会话" :image-size="70" />
    </section>

    <section class="panel report-history">
      <div class="section-heading history-heading">
        <div><h2>训练有效性验证报告</h2><p>报告保存在浏览器本地状态中，可展开查看完整演示指标。</p></div>
        <el-input v-model="historySearch" :prefix-icon="Search" clearable placeholder="搜索训练、模型、checkpoint 或数据集" />
      </div>

      <div v-if="filteredReports.length" class="report-list">
        <article v-for="report in filteredReports" :key="report.report_id" class="report-card">
          <div class="report-row">
            <div class="report-primary"><b>{{ report.training_name }}</b><code>{{ report.report_id }}</code></div>
            <div><span>训练模型</span><b>{{ report.trained_model }}</b><small>{{ report.optimization_algorithm || '—' }}</small></div>
            <div><span>Checkpoint</span><b>{{ report.checkpoint_version }}</b><small>{{ report.checkpoint_filename }}</small></div>
            <div><span>数据集</span><b>{{ report.dataset_name }}</b><small>Pass@{{ report.pass_k }}</small></div>
            <div><span>生成时间</span><b>{{ formatDate(report.created_at) }}</b><small>前端演示报告</small></div>
            <div class="report-actions"><el-button :icon="View" @click="toggleReport(report.report_id)">{{ expandedReportId === report.report_id ? '收起结果' : '查看结果' }}</el-button><el-button :icon="Download" @click="downloadReport">下载报告</el-button></div>
          </div>

          <div v-if="expandedReportId === report.report_id" class="report-result">
            <el-alert title="以下为固定演示数据，未执行真实模型验证。" type="warning" :closable="false" show-icon />
            <section class="result-section">
              <h3>数据分布</h3>
              <div class="table-scroll"><table class="metric-table"><thead><tr><th>难度等级 / 数据类型</th><th v-for="column in VALIDATION_COLUMNS" :key="column">{{ column }}</th></tr></thead><tbody><tr><th>样本量</th><td v-for="(value, index) in report.sample_counts" :key="index"><b>{{ value }}</b></td></tr></tbody></table></div>
            </section>
            <section class="result-section">
              <h3>开启采样测试 <small>Pass@K</small></h3>
              <div class="table-scroll"><table class="metric-table"><thead><tr><th>模型</th><th v-for="column in VALIDATION_COLUMNS" :key="column">{{ column }}</th></tr></thead><tbody><tr v-for="([key, label]) in metricLabels" :key="key" :class="{ improvement: key === 'improvement' }"><th>{{ label }}</th><td v-for="(value, index) in report.pass_at_k[key]" :key="index" :class="key === 'improvement' ? (value >= 0 ? 'positive' : 'negative') : ''">{{ formatMetric(value, key === 'improvement') }}</td></tr></tbody></table></div>
            </section>
            <section class="result-section">
              <h3>关闭采样测试 <small>正确率</small></h3>
              <div class="table-scroll"><table class="metric-table"><thead><tr><th>模型</th><th v-for="column in VALIDATION_COLUMNS" :key="column">{{ column }}</th></tr></thead><tbody><tr v-for="([key, label]) in metricLabels" :key="key" :class="{ improvement: key === 'improvement' }"><th>{{ label }}</th><td v-for="(value, index) in report.accuracy[key]" :key="index" :class="key === 'improvement' ? (value >= 0 ? 'positive' : 'negative') : ''">{{ formatMetric(value, key === 'improvement') }}</td></tr></tbody></table></div>
            </section>
          </div>
        </article>
      </div>
      <el-empty v-else description="暂无训练有效性验证报告" :image-size="78" />
    </section>
  </div>
</template>

<style scoped>
.validation-page { display: grid; gap: 18px; }.validation-hero { align-items: flex-start; }.validation-hero .el-button { margin-top: 4px; }
.panel { padding: 21px; border: 1px solid var(--line); border-radius: 14px; background: var(--panel); }.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 18px; }.section-heading h2 { margin: 0; font-size: 19px; }.section-heading p { margin: 5px 0 0; color: var(--muted); font-size: 13px; }
.config-layout { width: min(760px,100%); }.selection-form :deep(.el-select) { width: 100%; }.session-option { display: grid; line-height: 1.3; }.session-option span { color: var(--muted); font-size: 11px; }
.readonly-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 0 14px; }.readonly-grid :deep(.el-input__wrapper) { background: #f8fafc; box-shadow: 0 0 0 1px #dbe3ef inset; }.readonly-grid :deep(.el-input__inner) { color: var(--ink); cursor: default; }
.validation-config>.el-empty { padding: 20px 0 0; }.history-heading { align-items: center; }.history-heading>.el-input { width: min(370px,100%); }
.report-list { display: grid; gap: 12px; }.report-card { overflow: hidden; border: 1px solid var(--line); border-radius: 11px; }.report-row { display: grid; grid-template-columns: minmax(180px,1.1fr) minmax(145px,.8fr) minmax(175px,1fr) minmax(145px,.8fr) minmax(165px,.9fr) auto; align-items: center; gap: 14px; padding: 14px 16px; }.report-row>div { display: grid; gap: 3px; min-width: 0; }.report-row span,.report-row small,.report-primary code { overflow: hidden; color: var(--muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }.report-row b { overflow: hidden; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }.report-actions { display: flex!important; grid-template-columns: auto auto; }
.report-result { display: grid; gap: 20px; padding: 18px; border-top: 1px solid var(--line); background: #f8fafc; }.result-section h3 { margin: 0 0 9px; font-size: 15px; }.result-section h3 small { margin-left: 6px; color: var(--muted); font-size: 12px; font-weight: 500; }.table-scroll { overflow-x: auto; border: 1px solid #dbe3ef; border-radius: 8px; background: white; }.metric-table { width: 100%; min-width: 1220px; border-collapse: collapse; font-size: 12px; }.metric-table th,.metric-table td { min-width: 105px; padding: 10px 9px; border-right: 1px solid #e5eaf1; border-bottom: 1px solid #e5eaf1; text-align: center; white-space: nowrap; }.metric-table th:first-child { position: sticky; left: 0; z-index: 1; min-width: 155px; background: #f8fafc; text-align: left; }.metric-table thead th { background: #eef2ff; color: #3730a3; font-weight: 650; }.metric-table tr:last-child>* { border-bottom: 0; }.metric-table th:last-child,.metric-table td:last-child { border-right: 0; }.metric-table .improvement { font-weight: 650; }.positive { color: #15803d; }.negative { color: #dc2626; }
@media (max-width: 1050px) { .report-row { grid-template-columns: 1fr 1fr; }.report-actions { justify-content: flex-start; } }
@media (max-width: 720px) { .readonly-grid { grid-template-columns: 1fr; }.history-heading { align-items: flex-start; flex-direction: column; }.history-heading>.el-input { width: 100%; }.report-row { grid-template-columns: 1fr; }.report-actions { flex-wrap: wrap; } }
</style>
