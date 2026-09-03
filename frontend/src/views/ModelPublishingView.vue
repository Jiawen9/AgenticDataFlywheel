<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Refresh, Search, View } from '@element-plus/icons-vue'
import type { ModelArtifact, ModelPublishingState, ModelTrainingMonitor } from '@/types'
import { emptyModelPublishingState, MODEL_PUBLISHING_EVENT, readModelPublishingState } from '@/utils/modelPublishingStore'

const state = ref<ModelPublishingState>(emptyModelPublishingState())
const monitorSearch = ref('')
const artifactSearch = ref('')
const releaseFilter = ref<'all' | ModelArtifact['release_status']>('all')
const selectedArtifact = ref<ModelArtifact | null>(null)
const detailVisible = ref(false)
const selectedMonitor = ref<ModelTrainingMonitor | null>(null)
const monitorDetailVisible = ref(false)

const filteredMonitors = computed(() => {
  const keyword = monitorSearch.value.trim().toLowerCase()
  if (!keyword) return state.value.monitors
  return state.value.monitors.filter(item => [item.training_name, item.target_model, item.optimization_algorithm || '', item.training_job || '', item.result_directory, item.monitor_id].some(value => value.toLowerCase().includes(keyword)))
})
const filteredArtifacts = computed(() => {
  const keyword = artifactSearch.value.trim().toLowerCase()
  return state.value.artifacts.filter((item) => {
    const textMatches = !keyword || [item.training_name, item.model_version, item.filename, item.artifact_id].some(value => value.toLowerCase().includes(keyword))
    return textMatches && (releaseFilter.value === 'all' || item.release_status === releaseFilter.value)
  })
})
const publishedCount = computed(() => state.value.artifacts.filter(item => item.release_status === 'published').length)

function reloadState() { state.value = readModelPublishingState() }
function formatDate(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}
function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}
function validationText(status: ModelArtifact['validation_status']) { return { pending: '待校验', passed: '校验通过', failed: '校验失败' }[status] }
function releaseText(status: ModelArtifact['release_status']) { return { detected: '已检测', publishing: '发布中', published: '已发布', failed: '发布失败' }[status] }
function releaseType(status: ModelArtifact['release_status']) {
  if (status === 'published') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'publishing') return 'primary'
  return 'info'
}
function showArtifact(item: ModelArtifact) { selectedArtifact.value = item; detailVisible.value = true }
function showMonitor(item: ModelTrainingMonitor) { selectedMonitor.value = item; monitorDetailVisible.value = true }

onMounted(() => {
  reloadState()
  window.addEventListener(MODEL_PUBLISHING_EVENT, reloadState)
  window.addEventListener('storage', reloadState)
})
onBeforeUnmount(() => {
  window.removeEventListener(MODEL_PUBLISHING_EVENT, reloadState)
  window.removeEventListener('storage', reloadState)
})
</script>

<template>
  <div class="page model-publishing-page">
    <header class="page-hero publishing-hero">
      <div><span class="eyebrow">MODEL RELEASE</span><h1>模型发布</h1><p>持续观察训练平台的模型结果目录，并管理检测到的模型版本及发布记录。</p></div>
      <el-button :icon="Refresh" @click="reloadState">刷新状态</el-button>
    </header>

    <el-alert title="当前尚未接入后端文件监测服务。页面只保存并展示拉起训练产生的监测任务，不会读取远程目录或生成模拟模型结果。" type="info" :closable="false" show-icon />

    <section class="metrics" aria-label="模型发布统计">
      <div class="metric"><b>{{ state.monitors.length }}</b><span>监测任务</span></div>
      <div class="metric"><b>{{ state.monitors.length }}</b><span>持续监测中</span></div>
      <div class="metric"><b>{{ state.artifacts.length }}</b><span>检测到模型</span></div>
      <div class="metric"><b>{{ publishedCount }}</b><span>已发布模型</span></div>
    </section>

    <section class="panel monitor-panel">
      <div class="section-heading">
        <div><h2>模型结果监测</h2><p>每次拉起训练都会形成一条独立监测任务。</p></div>
        <el-input v-model="monitorSearch" :prefix-icon="Search" clearable placeholder="搜索训练、模型、目录或监测 ID" />
      </div>
      <el-table v-if="filteredMonitors.length" :data="filteredMonitors" row-key="monitor_id">
        <el-table-column label="训练任务" min-width="220"><template #default="{ row }"><div class="primary-cell"><b>{{ row.training_name }}</b><code>{{ row.monitor_id }}</code></div></template></el-table-column>
        <el-table-column prop="target_model" label="目标模型" min-width="160" />
        <el-table-column label="优化算法" width="115"><template #default="{ row }">{{ row.optimization_algorithm || '—' }}</template></el-table-column>
        <el-table-column label="训练作业" min-width="250" show-overflow-tooltip><template #default="{ row }">{{ row.training_job || '旧版任务' }}</template></el-table-column>
        <el-table-column label="资源" width="145"><template #default="{ row }">{{ row.cards_per_node ? `${row.cards_per_node} 卡 × ${row.instance_count} 实例` : '—' }}</template></el-table-column>
        <el-table-column label="监测目录" min-width="230"><template #default="{ row }"><code class="path-cell" :title="row.result_directory">{{ row.result_directory }}</code></template></el-table-column>
        <el-table-column label="启动时间" min-width="170"><template #default="{ row }">{{ formatDate(row.started_at) }}</template></el-table-column>
        <el-table-column label="状态" width="130"><template #default><el-tag type="primary" effect="light"><span class="live-dot" />持续监测中</el-tag></template></el-table-column>
        <el-table-column label="操作" width="90" fixed="right"><template #default="{ row }"><el-button text :icon="View" @click="showMonitor(row)">详情</el-button></template></el-table-column>
      </el-table>
      <el-empty v-else description="暂无监测任务，请先前往“模型训练 → 拉起训练”" :image-size="76" />
    </section>

    <section class="panel history-panel">
      <div class="section-heading history-heading">
        <div><h2>历史模型发布</h2><p>后端检测到模型结果后，将在这里形成可追溯的版本记录。</p></div>
        <div class="filters"><el-input v-model="artifactSearch" :prefix-icon="Search" clearable placeholder="搜索版本、文件或训练任务" /><el-select v-model="releaseFilter"><el-option label="全部状态" value="all" /><el-option label="已检测" value="detected" /><el-option label="发布中" value="publishing" /><el-option label="已发布" value="published" /><el-option label="发布失败" value="failed" /></el-select></div>
      </div>
      <el-table v-if="filteredArtifacts.length" :data="filteredArtifacts" row-key="artifact_id">
        <el-table-column label="模型版本" min-width="210"><template #default="{ row }"><div class="primary-cell"><b>{{ row.model_version }}</b><code>{{ row.artifact_id }}</code></div></template></el-table-column>
        <el-table-column prop="filename" label="结果文件" min-width="220" />
        <el-table-column label="文件大小" width="120"><template #default="{ row }">{{ formatBytes(row.file_size) }}</template></el-table-column>
        <el-table-column prop="training_name" label="来源训练" min-width="190" />
        <el-table-column label="检测时间" min-width="170"><template #default="{ row }">{{ formatDate(row.detected_at) }}</template></el-table-column>
        <el-table-column label="校验" width="110"><template #default="{ row }">{{ validationText(row.validation_status) }}</template></el-table-column>
        <el-table-column label="发布状态" width="115"><template #default="{ row }"><el-tag :type="releaseType(row.release_status)">{{ releaseText(row.release_status) }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="100" fixed="right"><template #default="{ row }"><el-button text :icon="View" @click="showArtifact(row)">详情</el-button></template></el-table-column>
      </el-table>
      <div v-else class="artifact-empty"><el-empty description="尚未检测到模型结果" :image-size="82" /><p>等待后端文件监测服务接入后，模型版本会自动出现在此处。</p></div>
    </section>

    <el-dialog v-model="monitorDetailVisible" title="训练监测详情" width="min(780px, 92vw)">
      <el-descriptions v-if="selectedMonitor" :column="2" border>
        <el-descriptions-item label="训练名称">{{ selectedMonitor.training_name }}</el-descriptions-item><el-descriptions-item label="监测 ID">{{ selectedMonitor.monitor_id }}</el-descriptions-item>
        <el-descriptions-item label="目标模型">{{ selectedMonitor.target_model }}</el-descriptions-item><el-descriptions-item label="模型 ID">{{ selectedMonitor.model_artifact_id || '旧版任务未记录' }}</el-descriptions-item>
        <el-descriptions-item label="优化算法">{{ selectedMonitor.optimization_algorithm || '旧版任务未记录' }}</el-descriptions-item>
        <el-descriptions-item label="训练作业" :span="2">{{ selectedMonitor.training_job || '旧版任务未记录' }}</el-descriptions-item>
        <el-descriptions-item label="单节点卡数">{{ selectedMonitor.cards_per_node ?? '—' }}</el-descriptions-item><el-descriptions-item label="实例数">{{ selectedMonitor.instance_count ?? '—' }}</el-descriptions-item>
        <el-descriptions-item label="监测目录" :span="2">{{ selectedMonitor.result_directory }}</el-descriptions-item><el-descriptions-item label="启动时间">{{ formatDate(selectedMonitor.started_at) }}</el-descriptions-item><el-descriptions-item label="状态"><el-tag type="primary">持续监测</el-tag></el-descriptions-item>
      </el-descriptions>
    </el-dialog>

    <el-dialog v-model="detailVisible" title="模型结果详情" width="min(720px, 92vw)">
      <el-descriptions v-if="selectedArtifact" :column="2" border>
        <el-descriptions-item label="模型版本">{{ selectedArtifact.model_version }}</el-descriptions-item><el-descriptions-item label="来源训练">{{ selectedArtifact.training_name }}</el-descriptions-item>
        <el-descriptions-item label="结果文件" :span="2">{{ selectedArtifact.filename }}</el-descriptions-item><el-descriptions-item label="文件路径" :span="2">{{ selectedArtifact.path || '—' }}</el-descriptions-item>
        <el-descriptions-item label="文件大小">{{ formatBytes(selectedArtifact.file_size) }}</el-descriptions-item><el-descriptions-item label="检测时间">{{ formatDate(selectedArtifact.detected_at) }}</el-descriptions-item>
        <el-descriptions-item label="SHA256" :span="2">{{ selectedArtifact.sha256 || '—' }}</el-descriptions-item><el-descriptions-item label="发布地址" :span="2">{{ selectedArtifact.registry_uri || '尚未发布' }}</el-descriptions-item>
      </el-descriptions>
    </el-dialog>
  </div>
</template>

<style scoped>
.model-publishing-page { display: grid; gap: 18px; }
.publishing-hero { align-items: flex-start; }
.publishing-hero .el-button { margin-top: 4px; }
.metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.metric { display: grid; gap: 4px; padding: 16px 18px; border: 1px solid var(--line); border-radius: 12px; background: var(--panel); }
.metric b { color: var(--ink); font-size: 25px; font-weight: 650; }
.metric span { color: var(--muted); font-size: 13px; }
.panel { padding: 20px; border: 1px solid var(--line); border-radius: 14px; background: var(--panel); }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 18px; margin-bottom: 16px; }
.section-heading h2 { margin: 0; font-size: 18px; font-weight: 650; }
.section-heading p { margin: 5px 0 0; color: var(--muted); font-size: 13px; }
.section-heading > .el-input { width: min(360px, 100%); }
.primary-cell { display: grid; gap: 4px; }
.primary-cell code { color: var(--muted); font-size: 11px; }
.path-cell { display: block; overflow: hidden; color: #475569; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.live-dot { display: inline-block; width: 6px; height: 6px; margin-right: 5px; border-radius: 50%; background: #3b82f6; box-shadow: 0 0 0 4px rgb(59 130 246 / 12%); }
.filters { display: grid; grid-template-columns: 250px 130px; gap: 10px; }
.artifact-empty { padding: 8px 0 20px; text-align: center; }
.artifact-empty :deep(.el-empty) { padding-bottom: 0; }
.artifact-empty p { margin: -4px 0 0; color: var(--muted); font-size: 13px; }
@media (max-width: 900px) { .metrics { grid-template-columns: 1fr 1fr; } .section-heading { align-items: flex-start; flex-direction: column; } .section-heading > .el-input, .filters { width: 100%; } .filters { grid-template-columns: 1fr 140px; } }
@media (max-width: 600px) { .filters { grid-template-columns: 1fr; } }
</style>
