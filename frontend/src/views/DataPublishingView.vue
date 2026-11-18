<script setup lang="ts">
import DisplayId from '@/components/DisplayId.vue'
import { useDisplayIdentifiers } from '@/composables/useDisplayIdentifiers'
import PipelineStatusBar from '@/components/PipelineStatusBar.vue'
import { usePipelineContext } from '@/composables/usePipelineContext'
import BatchPublishedNotice from '@/components/BatchPublishedNotice.vue'
import { useBatchLifecycle } from '@/composables/useBatchLifecycle'
import { activeBatchItems, type PublishedBatchEvent } from '@/utils/batchLifecycle'
import { notifyBatchesPublished } from '@/utils/batchLifecycle'
import { useRoute } from 'vue-router'

import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { CopyDocument, Download, Plus, Refresh, Search, Upload, View } from '@element-plus/icons-vue'
import { api, datasetReleaseExcelUrl, datasetReleaseTrainingUrl } from '@/api'
import { readPageApi, type ReleaseSummary } from '@/readPageApi'
import { useSummaryPage } from '@/composables/useSummaryPage'
import { invalidateSummaryCache } from '@/utils/summaryCache'
import DatasetUploadReceipts from '@/components/DatasetUploadReceipts.vue'
import DatasetFolderUploadProgress from '@/components/DatasetFolderUploadProgress.vue'
import { uploadDestinationPrefix, uploadRetryJobId } from '@/utils/datasetFolderUpload'
import ReleaseOverviewStatus from '@/components/ReleaseOverviewStatus.vue'
import ExternalDatasetImport from '@/components/ExternalDatasetImport.vue'
import { datasetUploadMessage } from '@/utils/datasetUploadMessage'
import type { DatasetRelease, DatasetReleaseCandidate, DatasetUploadJob, DatasetUploadStatus, DatasetUploadCapabilities } from '@/types'

const route = useRoute()
const { displayId } = useDisplayIdentifiers()
const pipelineContext = usePipelineContext({ stepId: 'publication' })
const managedBatchIds = ref<string[]>([])
const ACTIVE_UPLOAD_KEY = 'agentic-data-flywheel.active-internal-dataset-upload'
const capabilities = ref<DatasetUploadCapabilities>({ internal: { configured: false, reason: '正在检查云道S3上传配置' } })
const submitting = ref(false)
const uploadDialogVisible = ref(false)
const uploadRelease = ref<DatasetRelease | null>(null)
const destinationPrefix = ref('')
const trainingJsonl = ref(false)
const trainingOnly = ref(false)
const trainingDestinationPrefix = ref('')
const pairedTraining = computed(() => uploadRelease.value?.training_files?.filter(item => item.status === 'ready') || [])
const imagePrefixes = computed(() => [...new Set(pairedTraining.value.map(item => item.image_prefix).filter(Boolean))])
const normalizePrefix = (value: string | undefined) => (value || '').trim().replace(/\\/g, '/').split('/').filter(Boolean).join('/')
const retryUploadId = computed(() => Boolean(uploadRelease.value?.internal_upload?.training_jsonl) === trainingJsonl.value
  && Boolean(uploadRelease.value?.internal_upload?.training_only) === trainingOnly.value
  && normalizePrefix(uploadRelease.value?.internal_upload?.training_destination_prefix) === normalizePrefix(trainingDestinationPrefix.value)
  ? uploadRetryJobId(uploadRelease.value?.internal_upload, destinationPrefix.value) : undefined)
watch(trainingOnly, value => { if (value) trainingJsonl.value = true })
const uploadDestination = computed(() => retryUploadId.value && uploadRelease.value?.internal_upload?.destination
  ? uploadRelease.value.internal_upload.destination : capabilities.value.internal)
const pollError = ref('')
let pollEpoch = 0
let loadEpoch = 0
let detailEpoch = 0
let historyEpoch = 0
const candidates = ref<DatasetReleaseCandidate[]>([])
const releases = ref<ReleaseSummary[]>([])
const selectedSessionIds = ref<string[]>([])
const datasetName = ref('')
const loading = ref(true)
const creating = ref(false)
const pageError = ref('')
const searchText = ref('')
const statusFilter = ref<'all' | DatasetUploadStatus>('all')
const detailRelease = ref<DatasetRelease | null>(null)
const detailVisible = ref(false)
const activeUpload = ref<DatasetUploadJob | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | null = null
let disposed = false

const lifecycle = useBatchLifecycle({ currentBatch: () => '', onPublished, refreshChoices: () => loadData(true) })
const { notice: publishedNotice } = lifecycle
function onPublished(event: PublishedBatchEvent) {
  // External releases have no active editing workspace to retire on this page.
  const sourceKind = event.source_kind || releases.value.find(item => item.release_id === event.release_id)?.source_kind
  if (sourceKind === 'external_manual') return
  // Older tabs may omit origin metadata. An unrelated published batch has no
  // candidate or editing state to clear here, regardless of its ID spelling.
  if (!candidates.value.some(item => event.batch_ids.includes(item.batch_id || ''))) return
  const publishedIds = new Set(candidates.value.filter(item => event.batch_ids.includes(item.batch_id || '')).map(item => item.session_id))
  candidates.value = candidates.value.filter(item => !publishedIds.has(item.session_id))
  selectedSessionIds.value = selectedSessionIds.value.filter(id => !publishedIds.has(id))
  publishedNotice.value = event; loading.value = false
}
const readyCandidates = computed(() => candidates.value.filter(item => item.ready))
const selectedReadyCount = computed(() => selectedSessionIds.value.length)
const isExternalRelease = (item: DatasetRelease) => item.source_kind === 'external_manual'
const isPipelineRelease = (item: DatasetRelease) => Boolean((item as DatasetRelease & { pipeline_id?: string }).pipeline_id)
const releaseSource = (item: DatasetRelease) => isExternalRelease(item) ? '外部表格' : isPipelineRelease(item) ? 'Pipeline' : '专家纠偏'
const internalStatus = (item: DatasetRelease): DatasetUploadStatus => item.internal_upload?.status || 'not_uploaded'
const uploadedCount = computed(() => releases.value.filter(item => internalStatus(item) === 'succeeded').length)
const failedCount = computed(() => historyRead.counts.value?.abnormal ?? 0)
const uploadRunning = computed(() => activeUpload.value && ['queued', 'uploading'].includes(activeUpload.value.status))
const filteredReleases = computed(() => releases.value)
const historyPage = ref(1), historyLimit = ref(10), candidatePage = ref(1), candidateLimit = ref(10)
const candidateSearch = ref('')
const historyRead = useSummaryPage('/api/dataset-releases', computed(() => ({ offset: (historyPage.value - 1) * historyLimit.value, limit: historyLimit.value, search: searchText.value, status: statusFilter.value })), readPageApi.releases, { prefetch: true, persist: true })
const candidateRead = useSummaryPage('/api/dataset-releases/candidates', computed(() => ({ offset: (candidatePage.value - 1) * candidateLimit.value, limit: candidateLimit.value, search: candidateSearch.value })), readPageApi.candidates, { persist: false })
watch(historyRead.items, items => {
  releases.value = items; loading.value = false
  const running = items.find(item => ['queued', 'uploading'].includes(internalStatus(item)))
  if (running?.internal_upload?.job_id && !uploadRunning.value && !submitting.value) void pollUpload(running.internal_upload.job_id, ++pollEpoch)
}, { immediate: true })
watch(candidateRead.items, items => { candidates.value = activeBatchItems(items).filter(item => !managedBatchIds.value.includes(item.batch_id || '')) })
watch([searchText, statusFilter, historyLimit], () => { historyPage.value = 1 })
watch([candidateSearch, candidateLimit], () => { candidatePage.value = 1 })
const excelCount = (item: DatasetRelease) => (item as ReleaseSummary).excel_count ?? item.excel_paths?.length ?? 0
const trainingCount = (item: DatasetRelease) => (item as ReleaseSummary).training_count ?? item.training_files?.length ?? 0

function formatDate(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function formatBytes(value: number | null | undefined) {
  const bytes = Number(value || 0)
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

function statusText(status: DatasetUploadStatus | DatasetUploadJob['status']) {
  return ({ not_uploaded: '待上传', queued: '排队中', uploading: '上传中', succeeded: '已上传', failed: '失败', interrupted: '已中断' } as Record<string, string>)[status] || status
}

function statusType(status: DatasetUploadStatus) {
  if (status === 'succeeded') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'interrupted') return 'warning'
  if (status === 'uploading' || status === 'queued') return 'primary'
  return 'info'
}

async function loadData(silent = false) {
  if (pipelineContext.historyOnly.value) { loading.value = false; return }
  const epoch = ++loadEpoch
  pageError.value = ''
  if (!silent) loading.value = true
  // History renders independently of candidate validation or upload configuration.
  await Promise.allSettled([
    historyRead.refresh().finally(() => { if (epoch === loadEpoch) loading.value = false }),
    candidateRead.refresh(),
    api.datasetUploadCapabilities().then(value => { if (!disposed && epoch === loadEpoch) capabilities.value = value }).catch(() => { if (epoch === loadEpoch) capabilities.value = { internal: { configured: false, reason: '无法读取云道S3上传配置，请刷新重试' } } }),
    (typeof api.pipelineBindings === 'function' ? api.pipelineBindings() : Promise.resolve([])).then(values => {
      if (disposed || epoch !== loadEpoch) return
      managedBatchIds.value = values.map(item => item.batch_id).filter((id): id is string => Boolean(id))
      candidates.value = activeBatchItems(candidateRead.items.value).filter(item => !managedBatchIds.value.includes(item.batch_id || ''))
    }),
  ])
}

async function selectAll() {
  try {
    const selected = new Set(selectedSessionIds.value)
    for (let offset = 0; ; offset += 50) {
      const page = await readPageApi.candidates({ offset, limit: 50, search: candidateSearch.value })
      if (!page.counts_complete) throw new Error('候选摘要正在准备，请稍后重试全选')
      for (const item of activeBatchItems(page.items)) if (item.ready && !managedBatchIds.value.includes(item.batch_id || '')) selected.add(item.session_id)
      if (!page.has_more) break
    }
    selectedSessionIds.value = [...selected]
  } catch (cause) { ElMessage.error((cause as Error).message) }
}

async function createRelease() {
  if (pipelineContext.isPipelineRoute.value) return
  const name = datasetName.value.trim()
  if (!name) { ElMessage.warning('请输入数据集名称'); return }
  if (!selectedSessionIds.value.length) { ElMessage.warning('至少选择一个可发布批次'); return }
  creating.value = true
  try {
    const release = await api.createDatasetRelease(name, selectedSessionIds.value)
    notifyBatchesPublished({ batch_ids: release.batch_ids, release_id: release.release_id, published_at: release.created_at, source_kind: release.source_kind })
    if (disposed) return
    datasetName.value = ''
    selectedSessionIds.value = []
    invalidateSummaryCache()
    await loadData(true)
    detailRelease.value = releases.value.find(item => item.release_id === release.release_id) || release
    detailVisible.value = true
    ElMessage.success(`数据集 ${displayId('release', release.release_id)} 已发布`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    creating.value = false
  }
}

async function externalReleased(release: DatasetRelease) {
  const history = ++historyEpoch
  if (release.source_kind === 'external_manual' && release.batch_ids.length) {
    notifyBatchesPublished({ batch_ids: release.batch_ids, release_id: release.release_id,
      published_at: release.created_at, source_kind: release.source_kind })
  }
  if (disposed) return
  releases.value = [release, ...releases.value.filter(item => item.release_id !== release.release_id)]
  ++detailEpoch
  detailRelease.value = release
  detailVisible.value = true
  ElMessage.success('数据集 ' + displayId('release', release.release_id) + ' 已发布')
  try {
    const next = (await readPageApi.releases({ offset: (historyPage.value - 1) * historyLimit.value, limit: historyLimit.value, search: searchText.value, status: statusFilter.value })).items
    if (disposed || history !== historyEpoch) return
    releases.value = next.some(item => item.release_id === release.release_id) ? next : [release, ...next]
  } catch (cause) {
    if (!disposed && history === historyEpoch) pageError.value = '数据集已发布，刷新历史失败：' + (cause as Error).message
  }
}

function clearPoll() {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
}

async function pollUpload(jobId: string, epoch = pollEpoch) {
  clearPoll()
  if (typeof document !== 'undefined' && document.hidden) return
  try {
    const job = await api.datasetUploadJob(jobId)
    if (disposed || epoch !== pollEpoch) return
    if (job.mode !== 'internal') { localStorage.removeItem(ACTIVE_UPLOAD_KEY); return }
    pollError.value = ''
    activeUpload.value = job
    if (['queued', 'uploading'].includes(job.status)) {
      localStorage.setItem(ACTIVE_UPLOAD_KEY, jobId)
      pollTimer = setTimeout(() => void pollUpload(jobId, epoch), 5000)
      return
    }
    localStorage.removeItem(ACTIVE_UPLOAD_KEY)
    invalidateSummaryCache()
    await loadData(true)
    if (disposed || epoch !== pollEpoch) return
    if (job.status === 'succeeded') ElMessage.success(job.training_only ? '训练 JSONL 已上传到云道S3' : job.folder_upload && job.include_trajectories !== false ? '发布表格及所选完整轨迹已上传到云道S3' : '全部表格已上传到云道S3')
    else ElMessage.error(datasetUploadMessage(job.error) || '上传作业未成功完成')
  } catch (error) {
    if (disposed || epoch !== pollEpoch) return
    pollError.value = '读取上传进度失败：' + datasetUploadMessage((error as Error).message)
    // Keep the remembered job: a transient network error must not lose recovery.
    pollTimer = setTimeout(() => void pollUpload(jobId, epoch), 3000)
  }
}

async function startUpload(release: DatasetRelease, onlyTraining?: boolean) {
  if (uploadDisabled(release)) return
  const epoch = ++detailEpoch
  try { if ((release as ReleaseSummary).availability_checked === false) release = await api.datasetRelease(release.release_id) }
  catch (cause) { ElMessage.error((cause as Error).message); return }
  if (disposed || epoch !== detailEpoch) return
  uploadRelease.value = release
  destinationPrefix.value = uploadDestinationPrefix(release.internal_upload, capabilities.value)
  trainingOnly.value = onlyTraining ?? Boolean(release.internal_upload?.training_only)
  trainingJsonl.value = trainingOnly.value || (release.internal_upload ? Boolean(release.internal_upload.training_jsonl) : Boolean(release.training_files?.some(item => item.status === 'ready')))
  trainingDestinationPrefix.value = release.internal_upload?.training_destination_prefix || ''
  uploadDialogVisible.value = true
}

async function confirmUpload() {
  const release = uploadRelease.value
  if (!release || uploadDisabled(release)) return
  const folders = !isExternalRelease(release)
  submitting.value = true
  try {
    const job = await api.uploadDatasetRelease(release.release_id, 'internal', folders ? {
      include_trajectories: !trainingOnly.value, destination_prefix: destinationPrefix.value.trim(),
      ...(trainingJsonl.value ? { training_jsonl: true } : {}),
      ...(trainingOnly.value ? { training_only: true } : {}),
      ...(trainingJsonl.value && trainingDestinationPrefix.value.trim() ? { training_destination_prefix: trainingDestinationPrefix.value.trim() } : {}),
      ...(retryUploadId.value ? { retry_job_id: retryUploadId.value } : {}),
    } : undefined)
    localStorage.setItem(ACTIVE_UPLOAD_KEY, job.job_id)
    if (disposed) return
    uploadDialogVisible.value = false
    activeUpload.value = job
    void pollUpload(job.job_id, ++pollEpoch)
    invalidateSummaryCache()
    await loadData(true)
  } catch (error) {
    if (!disposed) ElMessage.error(datasetUploadMessage((error as Error).message))
  } finally {
    submitting.value = false
  }
}

function uploadDisabled(release: DatasetRelease) {
  return !capabilities.value.internal.configured || submitting.value || Boolean(uploadRunning.value)
    || (!isExternalRelease(release) && capabilities.value.internal.directory_upload !== true)
    || ['queued', 'uploading'].includes(internalStatus(release))
    || (isExternalRelease(release) && internalStatus(release) === 'succeeded')
    || !excelCount(release) || (isExternalRelease(release) && release.excel_paths?.some(item => item.available === false))
}

function uploadLabel(release: DatasetRelease) {
  const status = internalStatus(release)
  if (status === 'succeeded') return isExternalRelease(release) ? '已上传云道S3' : '云道S3上传／更换目标'
  if (['failed', 'interrupted'].includes(status)) return '重试云道S3上传'
  return '云道S3上传'
}

async function showDetails(release: DatasetRelease) {
  const epoch = ++detailEpoch
  try {
    const next = await api.datasetRelease(release.release_id)
    if (disposed || epoch !== detailEpoch) return
    detailRelease.value = next
    detailVisible.value = true
  } catch (error) { if (!disposed && epoch === detailEpoch) ElMessage.error((error as Error).message) }
}

async function copyS3(uri: string | null) {
  if (!uri) return
  try { await navigator.clipboard.writeText(uri); ElMessage.success('S3 地址已复制') }
  catch { ElMessage.error('复制失败，请在详情中手动复制') }
}

async function openBoundRelease(id: string) {
  if (!id || detailRelease.value?.release_id === id) return
  const token = ++detailEpoch
  try {
    const release = await api.datasetRelease(id)
    if (!disposed && token === detailEpoch) { detailRelease.value = release; detailVisible.value = true }
  } catch (cause) { if (!disposed && token === detailEpoch) pageError.value = (cause as Error).message }
}
watch(() => pipelineContext.releaseId.value || String(route.query.release_id || ''), id => { void openBoundRelease(id) })
function visibleUploads() { if (document.hidden) clearPoll(); else { const id = activeUpload.value && uploadRunning.value ? activeUpload.value.job_id : localStorage.getItem(ACTIVE_UPLOAD_KEY); if (id) void pollUpload(id) } }
onMounted(async () => {
  if (typeof document !== 'undefined') document.addEventListener('visibilitychange', visibleUploads)
  await pipelineContext.refresh()
  if (pipelineContext.historyOnly.value) { loading.value = false; return }
  await loadData()
  if (disposed) return
  await openBoundRelease(pipelineContext.releaseId.value || String(route.query.release_id || ''))
  const remembered = localStorage.getItem(ACTIVE_UPLOAD_KEY)
  if (remembered && !pollEpoch) void pollUpload(remembered, ++pollEpoch)
})

onBeforeUnmount(() => { if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', visibleUploads); disposed = true; ++pollEpoch; clearPoll() })
</script>

<template>
  <div class="page release-page">
    <PipelineStatusBar :context="pipelineContext" />
    <template v-if="!pipelineContext.historyOnly.value">
    <BatchPublishedNotice :notice="publishedNotice" />
    <header class="page-hero release-hero">
      <div><span class="eyebrow">DATASET RELEASE</span><h1>数据发布</h1><p>发布数据表，并将平台发布表包含的完整轨迹目录上传到云道S3。</p></div>
      <div class="release-actions"><ExternalDatasetImport @released="externalReleased" /><el-button :icon="Refresh" :loading="loading" @click="loadData()">刷新</el-button></div>
    </header>

    <section class="metrics" aria-label="发布统计">
      <div class="metric"><b>{{ historyRead.total.value }}</b><span>历史数据集</span></div>
      <div class="metric"><b>{{ readyCandidates.length }}</b><span>待发布会话</span></div>
      <div class="metric"><b>{{ uploadedCount }}</b><span>本页已上传云道S3</span></div>
      <div class="metric"><b>{{ failedCount }}</b><span>失败或中断</span></div>
    </section>

    <el-alert v-if="pageError" :title="pageError" type="error" :closable="false" show-icon />
    <el-alert v-if="!capabilities.internal.configured" :title="datasetUploadMessage(capabilities.internal.reason) || '云道S3上传尚未配置'" type="warning" :closable="false" show-icon />
    <el-alert v-else-if="capabilities.internal.directory_upload !== true" title="当前后端仅支持旧版表格上传，请同步支持完整轨迹目录上传的后端。" type="warning" :closable="false" show-icon />
    <el-alert v-if="pollError" :title="pollError" type="warning" :closable="false" show-icon />

    <section v-if="!pipelineContext.isPipelineRoute.value" class="panel create-panel" v-loading="loading">
      <div class="section-heading">
        <div><h2>创建数据集</h2><p>每个业务批次使用当前有效的“专家纠偏完整数据集”导出文件。</p></div>
        <div class="selection-actions"><el-button text @click="selectAll">全选可发布</el-button><el-button text @click="selectedSessionIds = []">取消选择</el-button></div>
      </div>
      <div class="create-form">
        <el-input v-model="datasetName" data-testid="internal-release-name" maxlength="120" show-word-limit placeholder="输入数据集名称，例如：爱奇艺 GUI 轨迹数据集 v1" />
        <el-button type="primary" :icon="Plus" :loading="creating" :disabled="!selectedReadyCount" @click="createRelease">创建并发布</el-button>
      </div>
      <el-input v-model="candidateSearch" clearable placeholder="搜索批次或会话" />
      <el-alert v-if="candidateRead.error.value" :title="candidateRead.error.value" type="warning" :closable="false" />
      <div v-if="candidates.length" class="candidate-list">
        <label v-for="candidate in candidates" :key="candidate.session_id" class="candidate" :class="{ disabled: !candidate.ready, selected: selectedSessionIds.includes(candidate.session_id) }">
          <el-checkbox v-model="selectedSessionIds" :value="candidate.session_id" :disabled="!candidate.ready" />
          <div class="candidate-main">
            <div class="candidate-title"><b><DisplayId v-if="candidate.batch_id" kind="batch" :id="candidate.batch_id" /><template v-else>未命名批次</template></b><el-tag v-if="candidate.ready" type="success" size="small">可发布</el-tag><el-tag v-else type="warning" size="small">缺少完整导出</el-tag></div>
            <span v-if="candidate.ready">{{ candidate.latest_excel.filename }} · {{ candidate.step_count }} 行 · 更新于 {{ formatDate(candidate.updated_at) }}</span>
            <span v-else class="candidate-error">{{ candidate.reason }}</span>
          </div>
          <div class="candidate-stats"><b>{{ candidate.task_count }}</b> 任务 · <b>{{ candidate.trajectory_count }}</b> 轨迹</div>
        </label>
      </div>
      <el-empty v-else-if="!loading" description="暂无可发布的业务批次" :image-size="72" />
    </section>

    <el-pagination v-model:current-page="candidatePage" v-model:page-size="candidateLimit" :page-sizes="[10,20,50]" :total="candidateRead.total.value" layout="total, sizes, prev, pager, next" />

    <section v-if="activeUpload" class="panel upload-progress">
      <div class="upload-progress__head"><div><span class="status-dot" :class="activeUpload.status" /><b>{{ statusText(activeUpload.status) }}</b><DisplayId kind="release" :id="activeUpload.release_id" /></div><strong v-if="!activeUpload.folder_upload">{{ activeUpload.percent }}%</strong></div>
      <DatasetFolderUploadProgress v-if="activeUpload.folder_upload" :key="activeUpload.job_id" :upload="activeUpload" :stage="activeUpload.stage" />
      <template v-else>
        <el-progress :percentage="activeUpload.percent" :status="activeUpload.status === 'failed' ? 'exception' : activeUpload.status === 'succeeded' ? 'success' : undefined" />
        <div class="upload-progress__meta"><span>已确认 {{ activeUpload.completed_files }}/{{ activeUpload.total_files }} 个 Excel</span><span>{{ formatBytes(activeUpload.completed_bytes) }}/{{ formatBytes(activeUpload.total_bytes) }}</span><span v-if="activeUpload.current_file" class="current-file" :title="activeUpload.current_file">当前文件：{{ activeUpload.current_file }}</span></div>
        <DatasetUploadReceipts :files="activeUpload.file_results" />
      </template>
      <el-alert v-if="activeUpload.error" :title="datasetUploadMessage(activeUpload.error)" type="error" :closable="false" />
      <p v-if="['failed', 'interrupted'].includes(activeUpload.status)" class="retry-hint">{{ activeUpload.folder_upload ? '使用同一目标重试将跳过已确认成功的目录，继续剩余传输。' : '重试将跳过已确认成功且校验值一致的文件，继续上传剩余表格。' }}</p>
    </section>

    <section class="panel history-panel">
      <div class="section-heading history-heading">
        <div><h2>历史发布</h2><p>发布记录只追加，不修改已有名称和内容。</p></div>
        <div class="filters"><el-input v-model="searchText" :prefix-icon="Search" clearable placeholder="搜索名称或发布 ID" /><el-select v-model="statusFilter" aria-label="上传状态筛选"><el-option label="全部状态" value="all" /><el-option label="待上传" value="not_uploaded" /><el-option label="上传中" value="uploading" /><el-option label="已上传" value="succeeded" /><el-option label="失败" value="failed" /><el-option label="已中断" value="interrupted" /></el-select></div>
      </div>
      <el-table v-if="filteredReleases.length" :data="filteredReleases" row-key="release_id" class="release-table">
        <el-table-column label="数据集" min-width="250"><template #default="{ row }"><div class="release-name"><b>{{ row.name }}</b><DisplayId kind="release" :id="row.release_id" /></div></template></el-table-column>
        <el-table-column label="来源" min-width="120"><template #default="{ row }"><el-tag :type="isExternalRelease(row) ? 'info' : 'success'">{{ releaseSource(row) }}</el-tag><small v-if="isExternalRelease(row)" class="release-progress">{{ row.external_import?.data_source || '人工采集' }}</small></template></el-table-column>
        <el-table-column label="发布时间" min-width="170"><template #default="{ row }">{{ formatDate(row.created_at) }}</template></el-table-column>
        <el-table-column label="规模" min-width="180"><template #default="{ row }">{{ excelCount(row) }} Excel · {{ row.trajectory_count }} 轨迹 · {{ row.step_count }} 步</template></el-table-column>
        <el-table-column label="训练 JSONL" min-width="135"><template #default="{ row }"><el-button v-if="trainingCount(row)" text type="primary" @click="showDetails(row)">查看 {{ trainingCount(row) }} 份配套文件</el-button><span v-else class="release-progress">暂无配套文件</span></template></el-table-column>
        <el-table-column label="云道S3" min-width="130"><template #default="{ row }"><el-tag :type="statusType(internalStatus(row))" effect="light">{{ statusText(internalStatus(row)) }}</el-tag><small v-if="row.internal_upload" class="release-progress">{{ row.internal_upload.folder_upload ? `${row.internal_upload.completed_items || 0}/${row.internal_upload.total_items || 0} 个目录` : `${row.internal_upload.completed_files}/${row.internal_upload.total_files} 个 Excel` }}</small></template></el-table-column>
        <el-table-column label="操作" width="380" fixed="right"><template #default="{ row }"><el-button text :icon="View" @click="showDetails(row)">详情</el-button><el-button v-if="excelCount(row)" text :icon="Download" tag="a" :href="datasetReleaseExcelUrl(row.release_id, 0)" target="_blank">下载 Excel</el-button><el-button text type="primary" :icon="Upload" :disabled="uploadDisabled(row)" @click="startUpload(row)">{{ uploadLabel(row) }}</el-button></template></el-table-column>
      </el-table>
      <el-empty v-else description="暂无匹配的发布记录" :image-size="80" />
      <el-alert v-if="historyRead.error.value" :title="historyRead.error.value" type="warning" :closable="false" />
      <el-alert v-if="historyRead.data.value && !historyRead.data.value.counts_complete" title="历史发布摘要正在准备，已有摘要仍可查看。" type="info" :closable="false" />
      <el-pagination v-model:current-page="historyPage" v-model:page-size="historyLimit" :page-sizes="[10,20,50]" :total="historyRead.total.value" layout="total, sizes, prev, pager, next" />
    </section>

    <el-dialog v-model="uploadDialogVisible" width="min(660px, 94vw)" title="上传到云道S3" :close-on-click-modal="!submitting" :close-on-press-escape="!submitting" destroy-on-close>
      <template v-if="uploadRelease">
        <p><b>{{ uploadRelease.name }}</b></p>
        <template v-if="!isExternalRelease(uploadRelease)">
          <p v-if="!trainingOnly" class="upload-scope">按冻结发布表包含的轨迹上传完整目录，不读取当前页面勾选。例如同一用例 10 条轨迹中发布 2 条，只上传这 2 条的完整文件。旧“完整数据集”表可能包含全部轨迹，实际范围会在后台定位后展示。</p>
          <el-form label-position="top" @submit.prevent="confirmUpload">
            <el-form-item v-if="pairedTraining.length" label="上传内容"><el-radio-group v-model="trainingOnly" data-testid="training-upload-scope" :disabled="submitting || capabilities.internal.training_pair_version !== 2"><el-radio-button :value="false">表格与完整轨迹</el-radio-button><el-radio-button :value="true">仅训练 JSONL</el-radio-button></el-radio-group><small v-if="capabilities.internal.training_pair_version !== 2">单独上传需要同步支持配套 JSONL 的后端。</small></el-form-item>
            <el-form-item v-if="!trainingOnly" :label="pairedTraining.length ? '发布表格目标前缀' : '云道目标前缀'"><el-input v-model="destinationPrefix" data-testid="upload-destination-prefix" :disabled="submitting" placeholder="例如 data/gui_data/flywheel_data/（空白表示 bucket 根目录）" /></el-form-item>
            <el-form-item v-if="!trainingOnly" label="训练文件"><el-switch v-model="trainingJsonl" data-testid="training-jsonl-switch" :disabled="submitting || (capabilities.internal.training_jsonl_version !== 1 && capabilities.internal.training_pair_version !== 2)" :active-text="pairedTraining.length ? '同时上传已生成的配套 JSONL' : '生成并上传训练 JSONL（旧导出）'" /><small v-if="capabilities.internal.training_jsonl_version !== 1 && capabilities.internal.training_pair_version !== 2">需要同步支持训练导出的后端。</small></el-form-item>
            <el-form-item v-if="trainingJsonl" label="JSONL 上传目录"><el-input v-model="trainingDestinationPrefix" data-testid="training-destination-prefix" :disabled="submitting" placeholder="bucket 内目录；留空使用目标前缀/批次编号/训练数据" /><small>独立设置 JSONL 文件的位置，不改变文件内的图片地址。多份表格分别放入 001、002 等子目录。</small></el-form-item>
          </el-form>
          <p class="upload-scope">Bucket：{{ uploadDestination.bucket || '后端配置' }} · Region：{{ uploadDestination.region || '后端配置' }}</p>
          <p v-if="!trainingOnly" class="upload-scope">表格目标：<code>s3://{{ uploadDestination.bucket || 'bucket' }}/{{ normalizePrefix(destinationPrefix) }}{{ normalizePrefix(destinationPrefix) ? '/' : '' }}&lt;批次编号&gt;/发布表格/</code></p>
          <p v-if="trainingJsonl" class="upload-scope">JSONL 目标：<code>s3://{{ uploadDestination.bucket || 'bucket' }}/{{ normalizePrefix(trainingDestinationPrefix) || [normalizePrefix(destinationPrefix), uploadRelease.batch_ids?.length === 1 ? uploadRelease.batch_ids[0] : uploadRelease.release_id, '训练数据'].filter(Boolean).join('/') }}/</code></p>
          <p v-for="prefix in imagePrefixes" :key="prefix" class="upload-scope">导出时确定的图片前缀：<code>{{ prefix }}</code></p>
          <p v-if="pairedTraining.length" class="upload-scope">上传配套文件，不重新转换内容。完整轨迹沿用上面的图片前缀，修改表格或 JSONL 上传目录不会改变图片地址。</p>
          <p v-if="trainingOnly" class="upload-scope">本次只上传 JSONL，不重复上传 Excel 或图片；请确认图片已上传，或随后按上述图片前缀上传。</p>
          <p v-else-if="trainingJsonl && !pairedTraining.length" class="upload-scope">旧导出仍在轨迹目录上传成功后生成 JSONL。缺框使用动作 JSON，无法转换的样本列入报告；原表与轨迹上传结果独立保留。</p>
          <p v-if="uploadRelease.internal_upload?.folder_upload && ['failed', 'interrupted'].includes(internalStatus(uploadRelease))" class="upload-scope">已填入上次目标。保持目标重试可跳过成功目录；更改目标会创建新上传任务。</p>
        </template>
        <p v-else class="upload-scope">外部导入记录沿用表格上传，不查找或上传外部轨迹目录。</p>
      </template>
      <template #footer><el-button :disabled="submitting" @click="uploadDialogVisible = false">取消</el-button><el-button data-testid="confirm-yundao-upload" type="primary" :loading="submitting" @click="confirmUpload">开始上传</el-button></template>
    </el-dialog>

    <el-dialog v-model="detailVisible" width="min(760px, 92vw)" title="数据集详情" destroy-on-close>
      <template v-if="detailRelease">
        <div class="detail-title"><h3>{{ detailRelease.name }}</h3><DisplayId kind="release" :id="detailRelease.release_id" /></div>
        <el-descriptions :column="2" border>
          <el-descriptions-item label="发布时间">{{ formatDate(detailRelease.created_at) }}</el-descriptions-item><el-descriptions-item label="云道S3上传"><el-tag :type="statusType(internalStatus(detailRelease))">{{ statusText(internalStatus(detailRelease)) }}</el-tag></el-descriptions-item>
          <el-descriptions-item label="数据来源">{{ releaseSource(detailRelease) }}</el-descriptions-item>
          <el-descriptions-item v-if="isPipelineRelease(detailRelease)" label="批次来源数">{{ detailRelease.batch_ids.length }}</el-descriptions-item>
          <el-descriptions-item v-else-if="!isExternalRelease(detailRelease)" label="会话来源数">{{ detailRelease.source_count }}</el-descriptions-item>
          <el-descriptions-item label="数据规模"><template v-if="!isExternalRelease(detailRelease)">{{ detailRelease.task_count }} 任务 / </template>{{ detailRelease.trajectory_count }} 轨迹 / {{ detailRelease.step_count }} 步</el-descriptions-item>
          <template v-if="isExternalRelease(detailRelease) && detailRelease.external_import">
            <el-descriptions-item label="来源名称">{{ detailRelease.external_import.data_source }}</el-descriptions-item>
            <el-descriptions-item label="数据日期">{{ detailRelease.external_import.data_date }}</el-descriptions-item>
            <el-descriptions-item label="工作表">{{ detailRelease.external_import.sheet_name }}</el-descriptions-item>
            <el-descriptions-item v-if="detailRelease.external_import.filename" label="来源文件">{{ detailRelease.external_import.filename }}</el-descriptions-item>
            <el-descriptions-item v-if="detailRelease.external_import.app" label="指定 App">{{ detailRelease.external_import.app }}</el-descriptions-item>
            <el-descriptions-item v-if="detailRelease.external_import.level1 || detailRelease.external_import.level2" label="指定场景">{{ [detailRelease.external_import.level1, detailRelease.external_import.level2].filter(Boolean).join(' / ') }}</el-descriptions-item>
          </template>
          <el-descriptions-item v-if="!isExternalRelease(detailRelease)" label="轨迹根目录（仅登记）" :span="2"><code>{{ detailRelease.trajectory_paths.join('\n') }}</code></el-descriptions-item>
          <el-descriptions-item v-if="!isExternalRelease(detailRelease)" label="历史模拟上传"><el-tag type="info">{{ statusText(detailRelease.upload_status) }}</el-tag><span v-if="detailRelease.upload_error">{{ detailRelease.upload_error }}</span></el-descriptions-item>
          <el-descriptions-item v-if="!isExternalRelease(detailRelease)" label="模拟 S3 地址"><code>{{ detailRelease.s3_uri || '无' }}</code><el-button v-if="detailRelease.s3_uri" text :icon="CopyDocument" @click="copyS3(detailRelease.s3_uri)">复制</el-button></el-descriptions-item>
        </el-descriptions>
        <template v-if="detailRelease.internal_upload">
          <h4>云道S3上传结果</h4>
          <p v-if="detailRelease.internal_upload.completed_at">任务结束时间：{{ formatDate(detailRelease.internal_upload.completed_at) }}</p>
          <el-alert v-if="detailRelease.internal_upload.error" :title="datasetUploadMessage(detailRelease.internal_upload.error)" type="error" :closable="false" />
          <DatasetFolderUploadProgress v-if="detailRelease.internal_upload.folder_upload" :key="detailRelease.internal_upload.job_id" :upload="detailRelease.internal_upload" />
          <DatasetUploadReceipts v-else :files="detailRelease.internal_upload.file_results" />
        </template>
        <ReleaseOverviewStatus v-if="detailVisible" :key="detailRelease.release_id" :release-id="detailRelease.release_id" />
        <template v-if="detailRelease.training_files?.length">
          <h4>配套训练 JSONL</h4>
          <div v-for="training in detailRelease.training_files" :key="training.index" class="training-detail">
            <b>表格 {{ training.index + 1 }} · {{ training.filename || '训练文件' }}</b>
            <span>样本 {{ training.included_count || 0 }} 条 · 排除 {{ training.excluded_count || 0 }} 条</span>
            <code v-if="training.image_prefix">图片前缀：{{ training.image_prefix }}</code>
            <p v-if="training.error" class="training-error">{{ training.error }}</p>
            <div class="training-links">
              <el-button v-if="training.status === 'ready'" text :icon="Download" tag="a" :href="datasetReleaseTrainingUrl(detailRelease.release_id, training.index)" target="_blank" :disabled="training.available === false">下载 JSONL</el-button>
              <el-button v-if="training.report_filename && training.report_sha256" text tag="a" :href="datasetReleaseTrainingUrl(detailRelease.release_id, training.index, 'report')" target="_blank">下载转换报告</el-button>
            </div>
          </div>
          <el-button v-if="detailRelease.training_files.some(item => item.status === 'ready')" data-testid="upload-training-only" type="primary" plain :disabled="uploadDisabled(detailRelease) || capabilities.internal.training_pair_version !== 2" @click="startUpload(detailRelease, true)">仅上传训练 JSONL</el-button>
        </template>
        <h4>{{ isExternalRelease(detailRelease) || isPipelineRelease(detailRelease) ? '发布 Excel' : '纠偏 Excel' }}</h4>
        <div v-for="(excel, index) in detailRelease.excel_paths" :key="excel.path" class="excel-detail"><div><b>{{ excel.filename }}</b><span>{{ excel.rows }} 行 · {{ excel.available ? '本地可用' : '本地缺失' }}</span></div><code v-if="!isExternalRelease(detailRelease)">{{ excel.path }}</code><small>SHA256 {{ excel.sha256 }}</small><el-button text :icon="Download" tag="a" :href="datasetReleaseExcelUrl(detailRelease.release_id, index)" target="_blank" :disabled="excel.available === false">下载</el-button></div>
      </template>
    </el-dialog>
    </template>
  </div>
</template>

<style scoped src="../styles/businessPaging.css"></style>
<style scoped>
:global(body:has(.release-page)){min-width:0}
.release-page,.create-panel,.history-panel{min-width:0}
@media(max-width:600px){.release-page{padding:20px 12px}.create-panel,.history-panel{padding:12px}.selection-actions,.candidate-title{flex-wrap:wrap}.candidate-stats{white-space:normal}.candidate-title{overflow-wrap:anywhere}.metrics{gap:8px}.metric{padding:12px 8px}}
.el-pagination { flex-wrap: wrap; row-gap: 8px; margin: 12px 0; }
.release-page { display: grid; grid-template-columns: minmax(0, 1fr); gap: 18px; }
.release-hero { align-items: flex-start; }
.release-hero .el-button { margin-top: 4px; }
.release-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.metric { display: grid; gap: 4px; padding: 16px 18px; border: 1px solid var(--line); border-radius: 12px; background: var(--panel); }
.metric b { color: var(--ink); font-size: 25px; font-weight: 650; }
.metric span { color: var(--muted); font-size: 13px; }
.panel { border: 1px solid var(--line); border-radius: 14px; background: var(--panel); }
.create-panel, .history-panel { padding: 20px; }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; }
.section-heading h2 { margin: 0; font-size: 18px; font-weight: 650; }
.section-heading p { margin: 5px 0 0; color: var(--muted); font-size: 13px; }
.selection-actions { display: flex; white-space: nowrap; }
.create-form { display: grid; grid-template-columns: minmax(260px, 1fr) auto; gap: 12px; margin-top: 18px; }
.candidate-list { display: grid; gap: 8px; margin-top: 16px; }
.candidate { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 12px; padding: 13px 14px; border: 1px solid var(--line); border-radius: 10px; cursor: pointer; transition: .18s ease; }
.candidate:hover, .candidate.selected { border-color: color-mix(in srgb, var(--accent) 55%, var(--line)); background: var(--accent-soft); }
.candidate.disabled { cursor: not-allowed; opacity: .68; background: #f8fafc; }
.candidate-main { min-width: 0; }
.candidate-title { display: flex; align-items: center; gap: 8px; }
.candidate-main > span { display: block; margin-top: 5px; overflow: hidden; color: var(--muted); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.candidate-main .candidate-error { color: #b45309; }
.candidate-stats { color: var(--muted); font-size: 12px; white-space: nowrap; }
.candidate-stats b { color: var(--ink); }
.upload-progress { padding: 16px 18px; }
.upload-progress__head, .upload-progress__head > div, .upload-progress__meta { display: flex; align-items: center; gap: 10px; }
.upload-progress__head { justify-content: space-between; margin-bottom: 10px; }
.upload-progress__head span { color: var(--muted); font-size: 12px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: #94a3b8; }
.status-dot.uploading, .status-dot.queued { background: #0ea5e9; box-shadow: 0 0 0 5px rgb(14 165 233 / 12%); }
.status-dot.succeeded { background: #10b981; }
.status-dot.failed, .status-dot.interrupted { background: #ef4444; }
.upload-progress__meta { margin-top: 8px; color: var(--muted); font-size: 12px; }
.current-file { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.release-progress { display: block; color: var(--muted); margin-top: 5px; }
.retry-hint { color: var(--muted); font-size: 13px; }
.upload-scope { color: var(--muted); line-height: 1.7; overflow-wrap: anywhere; }
.training-detail{display:grid;gap:7px;border:1px solid var(--line);border-radius:9px;padding:12px;margin-bottom:10px;font-size:13px}.training-detail code{overflow-wrap:anywhere}.training-links{display:flex;gap:12px;flex-wrap:wrap}.training-error{color:#b91c1c}
.history-heading { align-items: center; margin-bottom: 16px; }
.filters { display: grid; grid-template-columns: 240px 130px; gap: 10px; }
.release-name { display: grid; gap: 4px; }
.release-name code, .detail-title code { color: var(--muted); font-size: 12px; }
.detail-title { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.detail-title h3 { margin: 0; }
h4 { margin: 20px 0 10px; }
.excel-detail { position: relative; display: grid; gap: 6px; padding: 13px 100px 13px 14px; border: 1px solid var(--line); border-radius: 9px; }
.excel-detail > div { display: flex; justify-content: space-between; gap: 12px; }
.excel-detail span, .excel-detail small { color: var(--muted); font-size: 12px; }
.excel-detail code { overflow-wrap: anywhere; font-size: 12px; }
.excel-detail .el-button { position: absolute; top: 9px; right: 10px; }
@media (max-width: 980px) { .metrics { grid-template-columns: repeat(2, 1fr); } .candidate { grid-template-columns: auto minmax(0, 1fr); } .candidate-stats { grid-column: 2; } .history-heading { align-items: flex-start; flex-direction: column; } .filters { width: 100%; grid-template-columns: 1fr 130px; } }
@media (max-width: 640px) { .metrics { grid-template-columns: 1fr 1fr; } .create-form { grid-template-columns: 1fr; } .section-heading { flex-direction: column; } .filters { grid-template-columns: 1fr; } .upload-progress__meta { align-items: flex-start; flex-direction: column; } }
</style>
