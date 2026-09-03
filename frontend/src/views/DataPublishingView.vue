<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument, Download, Plus, Refresh, Search, Upload, View } from '@element-plus/icons-vue'
import { api, datasetReleaseExcelUrl } from '@/api'
import type { DatasetRelease, DatasetReleaseCandidate, DatasetUploadJob, DatasetUploadStatus } from '@/types'

const ACTIVE_UPLOAD_KEY = 'agentic-data-flywheel.active-dataset-upload'
const candidates = ref<DatasetReleaseCandidate[]>([])
const releases = ref<DatasetRelease[]>([])
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

const readyCandidates = computed(() => candidates.value.filter(item => item.ready))
const selectedReadyCount = computed(() => selectedSessionIds.value.filter(id => readyCandidates.value.some(item => item.session_id === id)).length)
const uploadedCount = computed(() => releases.value.filter(item => item.upload_status === 'succeeded').length)
const failedCount = computed(() => releases.value.filter(item => ['failed', 'interrupted'].includes(item.upload_status)).length)
const uploadRunning = computed(() => activeUpload.value && ['queued', 'uploading'].includes(activeUpload.value.status))
const filteredReleases = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  return releases.value.filter((item) => {
    const matchesText = !keyword || item.name.toLowerCase().includes(keyword) || item.release_id.toLowerCase().includes(keyword)
    const matchesStatus = statusFilter.value === 'all' || item.upload_status === statusFilter.value
    return matchesText && matchesStatus
  })
})

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
  if (!silent) loading.value = true
  pageError.value = ''
  try {
    const [nextCandidates, nextReleases] = await Promise.all([api.datasetReleaseCandidates(), api.datasetReleases()])
    if (disposed) return
    candidates.value = nextCandidates
    releases.value = nextReleases
    const available = new Set(nextCandidates.filter(item => item.ready).map(item => item.session_id))
    selectedSessionIds.value = selectedSessionIds.value.filter(id => available.has(id))
  } catch (error) {
    if (!disposed) pageError.value = (error as Error).message
  } finally {
    if (!disposed && !silent) loading.value = false
  }
}

function selectAll() {
  selectedSessionIds.value = readyCandidates.value.map(item => item.session_id)
}

async function createRelease() {
  const name = datasetName.value.trim()
  if (!name) { ElMessage.warning('请输入数据集名称'); return }
  if (!selectedSessionIds.value.length) { ElMessage.warning('至少选择一个可发布会话'); return }
  creating.value = true
  try {
    const release = await api.createDatasetRelease(name, selectedSessionIds.value)
    datasetName.value = ''
    selectedSessionIds.value = []
    await loadData(true)
    detailRelease.value = releases.value.find(item => item.release_id === release.release_id) || release
    detailVisible.value = true
    ElMessage.success(`数据集 ${release.release_id} 已发布`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    creating.value = false
  }
}

function clearPoll() {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
}

async function pollUpload(jobId: string) {
  clearPoll()
  try {
    const job = await api.datasetUploadJob(jobId)
    if (disposed) return
    activeUpload.value = job
    if (['queued', 'uploading'].includes(job.status)) {
      localStorage.setItem(ACTIVE_UPLOAD_KEY, jobId)
      pollTimer = setTimeout(() => void pollUpload(jobId), 800)
      return
    }
    localStorage.removeItem(ACTIVE_UPLOAD_KEY)
    await loadData(true)
    if (job.status === 'succeeded') ElMessage.success('数据集已模拟上传到训练环境')
    else ElMessage.error(job.error || '上传作业未成功完成')
  } catch (error) {
    localStorage.removeItem(ACTIVE_UPLOAD_KEY)
    ElMessage.error((error as Error).message)
  }
}

async function startUpload(release: DatasetRelease) {
  if (!release.local_available) { ElMessage.error('本地源文件不完整，无法上传'); return }
  if (uploadRunning.value) { ElMessage.warning('已有数据集正在上传'); return }
  try {
    if (release.upload_status === 'succeeded') await ElMessageBox.confirm('该数据集已经上传过，是否重新执行模拟上传？', '重新上传', { type: 'warning' })
    const job = await api.uploadDatasetRelease(release.release_id)
    activeUpload.value = job
    localStorage.setItem(ACTIVE_UPLOAD_KEY, job.job_id)
    void pollUpload(job.job_id)
    await loadData(true)
  } catch (error) {
    if ((error as Error).message !== 'cancel') ElMessage.error((error as Error).message)
  }
}

async function showDetails(release: DatasetRelease) {
  try { detailRelease.value = await api.datasetRelease(release.release_id); detailVisible.value = true }
  catch (error) { ElMessage.error((error as Error).message) }
}

async function copyS3(uri: string | null) {
  if (!uri) return
  try { await navigator.clipboard.writeText(uri); ElMessage.success('S3 地址已复制') }
  catch { ElMessage.error('复制失败，请在详情中手动复制') }
}

onMounted(async () => {
  await loadData()
  if (disposed) return
  const remembered = localStorage.getItem(ACTIVE_UPLOAD_KEY)
  const running = releases.value.find(item => ['queued', 'uploading'].includes(item.upload_status) && item.upload_job_id)
  const jobId = remembered || running?.upload_job_id
  if (jobId) void pollUpload(jobId)
})

onBeforeUnmount(() => { disposed = true; clearPoll() })
</script>

<template>
  <div class="page release-page">
    <header class="page-hero release-hero">
      <div><span class="eyebrow">DATASET RELEASE</span><h1>数据发布</h1><p>将专家纠偏后的完整 Excel 与原始轨迹根目录登记为不可变数据集，并上传到训练环境。</p></div>
      <el-button :icon="Refresh" :loading="loading" @click="loadData()">刷新</el-button>
    </header>

    <section class="metrics" aria-label="发布统计">
      <div class="metric"><b>{{ releases.length }}</b><span>历史数据集</span></div>
      <div class="metric"><b>{{ readyCandidates.length }}</b><span>待发布会话</span></div>
      <div class="metric"><b>{{ uploadedCount }}</b><span>已上传训练环境</span></div>
      <div class="metric"><b>{{ failedCount }}</b><span>失败或中断</span></div>
    </section>

    <el-alert v-if="pageError" :title="pageError" type="error" :closable="false" show-icon />

    <section class="panel create-panel" v-loading="loading">
      <div class="section-heading">
        <div><h2>创建数据集</h2><p>每个会话使用最新一份“专家纠偏完整数据集”导出文件。</p></div>
        <div class="selection-actions"><el-button text @click="selectAll">全选可发布</el-button><el-button text @click="selectedSessionIds = []">取消选择</el-button></div>
      </div>
      <div class="create-form">
        <el-input v-model="datasetName" maxlength="120" show-word-limit placeholder="输入数据集名称，例如：爱奇艺 GUI 轨迹数据集 v1" />
        <el-button type="primary" :icon="Plus" :loading="creating" :disabled="!selectedReadyCount" @click="createRelease">创建并发布</el-button>
      </div>
      <div v-if="candidates.length" class="candidate-list">
        <label v-for="candidate in candidates" :key="candidate.session_id" class="candidate" :class="{ disabled: !candidate.ready, selected: selectedSessionIds.includes(candidate.session_id) }">
          <el-checkbox v-model="selectedSessionIds" :value="candidate.session_id" :disabled="!candidate.ready" />
          <div class="candidate-main">
            <div class="candidate-title"><b>{{ candidate.tree_run_id || '未命名纠偏批次' }}</b><el-tag v-if="candidate.ready" type="success" size="small">可发布</el-tag><el-tag v-else type="warning" size="small">缺少完整导出</el-tag></div>
            <span v-if="candidate.ready">{{ candidate.latest_excel.filename }} · {{ candidate.step_count }} 行 · 更新于 {{ formatDate(candidate.updated_at) }}</span>
            <span v-else class="candidate-error">{{ candidate.reason }}</span>
          </div>
          <div class="candidate-stats"><b>{{ candidate.task_count }}</b> 任务 · <b>{{ candidate.trajectory_count }}</b> 轨迹</div>
        </label>
      </div>
      <el-empty v-else-if="!loading" description="暂无未发布的纠偏会话" :image-size="72" />
    </section>

    <section v-if="activeUpload" class="panel upload-progress">
      <div class="upload-progress__head"><div><span class="status-dot" :class="activeUpload.status" /><b>{{ statusText(activeUpload.status) }}</b><span>{{ activeUpload.release_id }}</span></div><strong>{{ activeUpload.percent }}%</strong></div>
      <el-progress :percentage="activeUpload.percent" :status="activeUpload.status === 'failed' ? 'exception' : activeUpload.status === 'succeeded' ? 'success' : undefined" />
      <div class="upload-progress__meta"><span>{{ activeUpload.completed_files }}/{{ activeUpload.total_files }} 个文件</span><span>{{ formatBytes(activeUpload.completed_bytes) }}/{{ formatBytes(activeUpload.total_bytes) }}</span><span class="current-file" :title="activeUpload.current_file || ''">{{ activeUpload.current_file || activeUpload.error || activeUpload.s3_uri }}</span></div>
    </section>

    <section class="panel history-panel">
      <div class="section-heading history-heading">
        <div><h2>历史发布</h2><p>发布记录只追加，不修改已有名称和内容。</p></div>
        <div class="filters"><el-input v-model="searchText" :prefix-icon="Search" clearable placeholder="搜索名称或发布 ID" /><el-select v-model="statusFilter" aria-label="上传状态筛选"><el-option label="全部状态" value="all" /><el-option label="待上传" value="not_uploaded" /><el-option label="上传中" value="uploading" /><el-option label="已上传" value="succeeded" /><el-option label="失败" value="failed" /><el-option label="已中断" value="interrupted" /></el-select></div>
      </div>
      <el-table v-if="filteredReleases.length" :data="filteredReleases" row-key="release_id" class="release-table">
        <el-table-column label="数据集" min-width="250"><template #default="{ row }"><div class="release-name"><b>{{ row.name }}</b><code>{{ row.release_id }}</code></div></template></el-table-column>
        <el-table-column label="发布时间" min-width="170"><template #default="{ row }">{{ formatDate(row.created_at) }}</template></el-table-column>
        <el-table-column label="规模" min-width="180"><template #default="{ row }">{{ row.excel_paths.length }} Excel · {{ row.trajectory_count }} 轨迹 · {{ row.step_count }} 步</template></el-table-column>
        <el-table-column label="训练环境" min-width="130"><template #default="{ row }"><el-tag :type="statusType(row.upload_status)" effect="light">{{ statusText(row.upload_status) }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="330" fixed="right"><template #default="{ row }"><el-button text :icon="View" @click="showDetails(row)">详情</el-button><el-button v-if="row.excel_paths.length" text :icon="Download" tag="a" :href="datasetReleaseExcelUrl(row.release_id, 0)" target="_blank">下载 Excel</el-button><el-button text type="primary" :icon="Upload" :disabled="Boolean(uploadRunning) || !row.local_available" @click="startUpload(row)">{{ row.upload_status === 'not_uploaded' ? '上传训练环境' : '重新上传' }}</el-button><el-button v-if="row.s3_uri" text :icon="CopyDocument" @click="copyS3(row.s3_uri)">复制地址</el-button></template></el-table-column>
      </el-table>
      <el-empty v-else description="暂无匹配的发布记录" :image-size="80" />
    </section>

    <el-dialog v-model="detailVisible" width="min(760px, 92vw)" title="数据集详情" destroy-on-close>
      <template v-if="detailRelease">
        <div class="detail-title"><h3>{{ detailRelease.name }}</h3><code>{{ detailRelease.release_id }}</code></div>
        <el-descriptions :column="2" border>
          <el-descriptions-item label="发布时间">{{ formatDate(detailRelease.created_at) }}</el-descriptions-item><el-descriptions-item label="上传状态"><el-tag :type="statusType(detailRelease.upload_status)">{{ statusText(detailRelease.upload_status) }}</el-tag></el-descriptions-item>
          <el-descriptions-item label="会话来源数">{{ detailRelease.source_count }}</el-descriptions-item><el-descriptions-item label="数据规模">{{ detailRelease.task_count }} 任务 / {{ detailRelease.trajectory_count }} 轨迹 / {{ detailRelease.step_count }} 步</el-descriptions-item>
          <el-descriptions-item label="轨迹根目录" :span="2"><code>{{ detailRelease.trajectory_paths.join('\n') }}</code></el-descriptions-item><el-descriptions-item label="S3 地址" :span="2"><code>{{ detailRelease.s3_uri || '尚未上传' }}</code></el-descriptions-item>
        </el-descriptions>
        <h4>纠偏 Excel</h4>
        <div v-for="(excel, index) in detailRelease.excel_paths" :key="excel.path" class="excel-detail"><div><b>{{ excel.filename }}</b><span>{{ excel.rows }} 行 · {{ excel.available ? '本地可用' : '本地缺失' }}</span></div><code>{{ excel.path }}</code><small>SHA256 {{ excel.sha256 }}</small><el-button text :icon="Download" tag="a" :href="datasetReleaseExcelUrl(detailRelease.release_id, index)" target="_blank" :disabled="excel.available === false">下载</el-button></div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.release-page { display: grid; gap: 18px; }
.release-hero { align-items: flex-start; }
.release-hero .el-button { margin-top: 4px; }
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
