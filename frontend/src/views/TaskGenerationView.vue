<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Download, MagicStick, Refresh, Upload } from '@element-plus/icons-vue'
import { api, taskGenerationDownloadUrl } from '@/api'
import type { TaskGenerationInput, TaskGenerationJob, TaskGenerationPreview, TaskGenerationSource } from '@/types'

type Mode = 'knowledge' | 'flywheel'

const ACTIVE_JOBS_KEY = 'task-generation-active-jobs'
const mode = ref<Mode>('knowledge')
const source = ref<TaskGenerationSource | null>(null)
const sourceLoading = ref(true)
const jobs = ref<TaskGenerationJob[]>([])
const knowledgeJob = ref<TaskGenerationJob | null>(null)
const sceneJob = ref<TaskGenerationJob | null>(null)
const variantJob = ref<TaskGenerationJob | null>(null)
const knowledgePreview = ref<TaskGenerationPreview | null>(null)
const scenePreview = ref<TaskGenerationPreview | null>(null)
const variantPreview = ref<TaskGenerationPreview | null>(null)
const input = ref<TaskGenerationInput | null>(null)
const uploading = ref(false)
const submitting = ref(false)
const refreshing = ref(false)
let pollTimer: number | undefined

const knowledgeParams = reactive({
  app: null as string | null,
  scene: null as string | null,
  capability: null as string | null,
  sub_capability: null as string | null,
  generate_per_sub_capability: 5,
})
const generateN = ref(10)

const currentPreview = computed(() => mode.value === 'knowledge' ? knowledgePreview.value : variantPreview.value)
const sceneRows = computed(() => scenePreview.value?.rows || [])
const previewColumns = computed(() => {
  const rows = currentPreview.value?.rows || []
  const preferred = ['任务', 'task', '生成的变体任务', 'app', '涉及APP', 'scene', 'capability', 'sub_capability', 'pre_dependency', 'status', '源失败任务', '用例编号', '审核状态']
  const all = new Set<string>()
  rows.forEach((row) => Object.keys(row).forEach((key) => all.add(key)))
  return [...preferred.filter((key) => all.has(key)), ...[...all].filter((key) => !preferred.includes(key))].slice(0, 12)
})
const activeRows = computed(() => currentPreview.value?.rows || [])
const sourceReady = computed(() => source.value?.ready === true)

function saveActiveJobs() {
  localStorage.setItem(ACTIVE_JOBS_KEY, JSON.stringify({
    knowledge: knowledgeJob.value?.job_id || null,
    scene: sceneJob.value?.job_id || null,
    variant: variantJob.value?.job_id || null,
    input: input.value?.input_id || null,
  }))
}

function readActiveJobs(): Record<string, string | null> {
  try {
    return JSON.parse(localStorage.getItem(ACTIVE_JOBS_KEY) || '{}') as Record<string, string | null>
  } catch {
    return {}
  }
}

function jobText(job: TaskGenerationJob | null) {
  if (!job) return ''
  const labels: Record<string, string> = {
    queued: '排队中', preparing: '准备中', generating: '模型生成中', scene_matching: '场景匹配中',
    writing: '保存结果中', succeeded: '已完成', failed: '失败', interrupted: '已中断',
  }
  return labels[job.stage] || labels[job.status] || job.stage
}

function jobProgressStatus(job: TaskGenerationJob | null) {
  if (!job) return undefined
  if (job.status === 'failed' || job.status === 'interrupted') return 'exception' as const
  if (job.status === 'succeeded') return 'success' as const
  return undefined
}

function display(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function formatBytes(value: number) {
  if (!value) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

async function loadSource() {
  sourceLoading.value = true
  try {
    source.value = await api.taskGenerationSource()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    sourceLoading.value = false
  }
}

async function loadPreview(job: TaskGenerationJob, target: 'knowledge' | 'scene' | 'variant') {
  if (job.status !== 'succeeded') return
  try {
    const preview = await api.taskGenerationPreview(job.job_id)
    if (target === 'knowledge') knowledgePreview.value = preview
    if (target === 'scene') scenePreview.value = preview
    if (target === 'variant') variantPreview.value = preview
  } catch (error) {
    ElMessage.error(`结果预览失败：${(error as Error).message}`)
  }
}

async function refreshJob(target: 'knowledge' | 'scene' | 'variant') {
  const current = target === 'knowledge' ? knowledgeJob.value : target === 'scene' ? sceneJob.value : variantJob.value
  if (!current) return
  try {
    const latest = await api.taskGenerationJob(current.job_id)
    if (target === 'knowledge') knowledgeJob.value = latest
    if (target === 'scene') sceneJob.value = latest
    if (target === 'variant') variantJob.value = latest
    await loadPreview(latest, target)
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function pollJobs() {
  await Promise.all([refreshJob('knowledge'), refreshJob('scene'), refreshJob('variant')])
  const pending = [knowledgeJob.value, sceneJob.value, variantJob.value].some((job) => job?.status === 'queued' || job?.status === 'running')
  if (pending) schedulePoll()
  else saveActiveJobs()
}

function schedulePoll(delay = 1200) {
  if (pollTimer) window.clearTimeout(pollTimer)
  pollTimer = window.setTimeout(() => void pollJobs(), delay)
}

async function loadJobs() {
  try {
    jobs.value = await api.taskGenerationJobs()
    const active = readActiveJobs()
    const byId = new Map(jobs.value.map((job) => [job.job_id, job]))
    const restoredKnowledge = active.knowledge ? byId.get(active.knowledge) : undefined
    const restoredScene = active.scene ? byId.get(active.scene) : undefined
    const restoredVariant = active.variant ? byId.get(active.variant) : undefined
    if (restoredKnowledge) knowledgeJob.value = restoredKnowledge
    if (restoredScene) sceneJob.value = restoredScene
    if (restoredVariant) variantJob.value = restoredVariant
    if (active.input) {
      // The input metadata remains in the job record, so a refresh can still
      // show the selected source even though the browser does not hold the file.
      const sourceJob = restoredScene || restoredVariant
      if (sourceJob?.input_id) input.value = {
        input_id: sourceJob.input_id,
        original_filename: '已上传失败用例文件',
        size_bytes: 0,
        created_at: sourceJob.created_at,
        status: 'ready',
      }
    }
    await Promise.all([
      loadPreviewIfPresent(knowledgeJob.value, 'knowledge'),
      loadPreviewIfPresent(sceneJob.value, 'scene'),
      loadPreviewIfPresent(variantJob.value, 'variant'),
    ])
    if ([knowledgeJob.value, sceneJob.value, variantJob.value].some((job) => job?.status === 'queued' || job?.status === 'running')) schedulePoll()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function loadPreviewIfPresent(job: TaskGenerationJob | null, target: 'knowledge' | 'scene' | 'variant') {
  if (job) await loadPreview(job, target)
}

async function refreshAll() {
  refreshing.value = true
  try {
    await Promise.all([loadSource(), loadJobs()])
    ElMessage.success('任务生成状态已刷新')
  } finally {
    refreshing.value = false
  }
}

async function submitKnowledge() {
  if (!sourceReady.value) return ElMessage.warning('三份 KnowledgeBase 未准备好')
  submitting.value = true
  try {
    knowledgePreview.value = null
    knowledgeJob.value = await api.createKnowledgeGeneration(knowledgeParams)
    saveActiveJobs()
    ElMessage.success('知识库任务生成作业已提交')
    schedulePoll(0)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    submitting.value = false
  }
}

async function uploadInput(event: Event) {
  const element = event.target as HTMLInputElement
  const file = element.files?.[0]
  element.value = ''
  if (!file) return
  if (!file.name.toLowerCase().endsWith('.xlsx')) return ElMessage.warning('只支持 .xlsx 文件')
  uploading.value = true
  try {
    input.value = await api.uploadFlywheelInput(file)
    sceneJob.value = null
    variantJob.value = null
    scenePreview.value = null
    variantPreview.value = null
    saveActiveJobs()
    ElMessage.success(`已上传，发现 ${input.value.failed_count || 0} 条失败用例`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    uploading.value = false
  }
}

async function submitSceneMatch() {
  if (!input.value) return ElMessage.warning('请先上传失败用例 Excel')
  submitting.value = true
  try {
    scenePreview.value = null
    sceneJob.value = await api.createSceneMatchGeneration(input.value.input_id)
    saveActiveJobs()
    ElMessage.success('场景匹配作业已提交')
    schedulePoll(0)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    submitting.value = false
  }
}

async function submitVariants() {
  if (!sceneJob.value || sceneJob.value.status !== 'succeeded') return ElMessage.warning('请先完成场景匹配')
  submitting.value = true
  try {
    variantPreview.value = null
    variantJob.value = await api.createVariantGeneration(sceneJob.value.job_id, generateN.value)
    saveActiveJobs()
    ElMessage.success('变体任务生成作业已提交')
    schedulePoll(0)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadSource(), loadJobs()])
})

onBeforeUnmount(() => {
  if (pollTimer) window.clearTimeout(pollTimer)
})
</script>

<template>
  <div class="page task-generation-page">
    <header class="page-hero">
      <div>
        <span class="eyebrow">TASK GENERATION</span>
        <h1>任务生成</h1>
        <p>从场景与 App 先验知识生成训练任务，或将失败用例扩写为新的变体任务。模型配置只在后端环境中生效。</p>
      </div>
      <div class="hero-actions">
        <el-button :icon="Refresh" :loading="refreshing" @click="refreshAll">刷新状态</el-button>
      </div>
    </header>

    <section class="source-card" :class="{ 'source-card--ready': sourceReady }">
      <div class="source-card__heading">
        <div><span class="section-kicker">DATA SOURCE</span><h2>KnowledgeBase 数据源</h2></div>
        <el-tag :type="sourceReady ? 'success' : 'danger'">{{ sourceLoading ? '检查中' : sourceReady ? '可用' : '不可用' }}</el-tag>
      </div>
      <div class="source-card__files" v-loading="sourceLoading">
        <div v-for="file in source?.files || []" :key="file.name" class="source-file">
          <div class="source-file__icon"><el-icon><MagicStick /></el-icon></div>
          <div><b>{{ file.name }}</b><span>{{ file.exists ? `${file.rows} 行 · ${file.sheets.length} 个 Sheet · ${formatBytes(file.size_bytes)}` : '文件不存在' }}</span></div>
          <el-tag size="small" :type="file.exists && !file.error ? 'success' : 'danger'">{{ file.exists && !file.error ? '正常' : '检查失败' }}</el-tag>
        </div>
      </div>
      <el-alert v-if="source?.errors.length" :title="source.errors.join('；')" type="error" :closable="false" show-icon />
    </section>

    <el-tabs v-model="mode" class="generation-tabs">
      <el-tab-pane label="知识库生成" name="knowledge">
        <section class="control-card">
          <div class="section-heading"><div><span class="section-kicker">KNOWLEDGE FLOW</span><h2>按场景节点生成任务</h2></div><el-tag type="info">模型与并发由 .env 管理</el-tag></div>
          <div class="control-grid">
            <label>目标 App<el-select v-model="knowledgeParams.app" clearable filterable placeholder="全部 App"><el-option v-for="item in source?.filters.apps || []" :key="item" :label="item" :value="item" /></el-select></label>
            <label>场景<el-select v-model="knowledgeParams.scene" clearable filterable placeholder="全部场景"><el-option v-for="item in source?.filters.scenes || []" :key="item" :label="item" :value="item" /></el-select></label>
            <label>一级能力<el-select v-model="knowledgeParams.capability" clearable filterable placeholder="全部能力"><el-option v-for="item in source?.filters.capabilities || []" :key="item" :label="item" :value="item" /></el-select></label>
            <label>二级能力<el-select v-model="knowledgeParams.sub_capability" clearable filterable placeholder="全部子能力"><el-option v-for="item in source?.filters.sub_capabilities || []" :key="item" :label="item" :value="item" /></el-select></label>
            <label>每个子能力生成数量<el-input-number v-model="knowledgeParams.generate_per_sub_capability" :min="1" :max="100" /></label>
          </div>
          <div class="control-footer"><span>留空筛选项表示生成全部匹配的场景节点。</span><el-button type="primary" :icon="MagicStick" :loading="submitting" :disabled="!sourceReady" @click="submitKnowledge">开始生成任务</el-button></div>
        </section>

        <section v-if="knowledgeJob" class="job-card" :class="`job-card--${knowledgeJob.status}`">
          <div class="job-card__heading"><div><b>{{ jobText(knowledgeJob) }}</b><span>{{ knowledgeJob.current || '任务生成作业' }}</span></div><strong>{{ knowledgeJob.percent }}%</strong></div>
          <el-progress :percentage="knowledgeJob.percent" :status="jobProgressStatus(knowledgeJob)" :show-text="false" />
          <p v-if="knowledgeJob.total">场景节点 {{ knowledgeJob.completed }} / {{ knowledgeJob.total }}</p>
          <p v-if="knowledgeJob.error" class="job-error">{{ knowledgeJob.error }}</p>
        </section>

        <section v-if="knowledgePreview" class="result-card">
          <div class="section-heading"><div><span class="section-kicker">RESULT PREVIEW</span><h2>生成结果 <small>{{ knowledgePreview.total }} 条</small></h2></div><div class="download-actions"><a :href="taskGenerationDownloadUrl(knowledgePreview.job.job_id, 'json')" target="_blank"><el-button link :icon="Download">下载 JSON</el-button></a><a :href="taskGenerationDownloadUrl(knowledgePreview.job.job_id, 'xlsx')" target="_blank"><el-button type="primary" plain :icon="Download">下载 Excel</el-button></a></div></div>
          <el-table :data="activeRows" max-height="560" empty-text="没有生成结果"><el-table-column v-for="column in previewColumns" :key="column" :prop="column" :label="column" min-width="150" show-overflow-tooltip><template #default="scope">{{ display(scope.row[column]) }}</template></el-table-column></el-table>
        </section>
      </el-tab-pane>

      <el-tab-pane label="失败用例扩写" name="flywheel">
        <section class="control-card">
          <div class="section-heading"><div><span class="section-kicker">FLYWHEEL FLOW</span><h2>失败用例 → 场景匹配 → 变体任务</h2></div><el-tag type="warning">原始 Excel 不会被修改</el-tag></div>
          <div class="flywheel-upload">
            <div class="upload-box"><input id="flywheel-input" type="file" accept=".xlsx" :disabled="uploading" @change="uploadInput"><label for="flywheel-input"><el-icon><Upload /></el-icon><b>{{ uploading ? '上传并校验中…' : '选择失败用例 Excel' }}</b><span>仅支持 .xlsx，后端会保存到模块专属目录</span></label></div>
            <div v-if="input" class="uploaded-file"><el-tag type="success">已上传</el-tag><div><b>{{ input.original_filename }}</b><span>{{ input.failed_count || 0 }} 条失败用例 · {{ formatBytes(input.size_bytes) }}</span></div></div>
          </div>
          <div class="control-footer"><span>首个工作表必须包含：任务结果、任务、涉及APP。</span><el-button type="primary" :loading="submitting" :disabled="!input" @click="submitSceneMatch">第一步：执行场景匹配</el-button></div>
        </section>

        <section v-if="sceneJob" class="job-card" :class="`job-card--${sceneJob.status}`">
          <div class="job-card__heading"><div><b>{{ jobText(sceneJob) }}</b><span>{{ sceneJob.current || '场景匹配作业' }}</span></div><strong>{{ sceneJob.percent }}%</strong></div>
          <el-progress :percentage="sceneJob.percent" :status="jobProgressStatus(sceneJob)" :show-text="false" />
          <p v-if="sceneJob.total">失败用例 {{ sceneJob.completed }} / {{ sceneJob.total }}</p><p v-if="sceneJob.error" class="job-error">{{ sceneJob.error }}</p>
        </section>

        <section v-if="scenePreview" class="result-card">
          <div class="section-heading"><div><span class="section-kicker">STEP 1 RESULT</span><h2>场景匹配结果 <small>{{ scenePreview.total }} 条</small></h2></div><a :href="taskGenerationDownloadUrl(scenePreview.job.job_id, 'xlsx')" target="_blank"><el-button link :icon="Download">下载匹配表</el-button></a></div>
          <el-table :data="sceneRows" max-height="360"><el-table-column v-for="column in ['app', 'task', 'scene', 'capability', 'sub_capability']" :key="column" :prop="column" :label="column" min-width="150" show-overflow-tooltip><template #default="scope">{{ display(scope.row[column]) }}</template></el-table-column></el-table>
          <div class="variant-controls"><label>每条失败用例生成变体数量<el-input-number v-model="generateN" :min="1" :max="100" /></label><el-button type="primary" :loading="submitting" @click="submitVariants">第二步：开始生成变体任务</el-button></div>
        </section>

        <section v-if="variantJob" class="job-card" :class="`job-card--${variantJob.status}`">
          <div class="job-card__heading"><div><b>{{ jobText(variantJob) }}</b><span>{{ variantJob.current || '变体任务生成作业' }}</span></div><strong>{{ variantJob.percent }}%</strong></div>
          <el-progress :percentage="variantJob.percent" :status="jobProgressStatus(variantJob)" :show-text="false" />
          <p v-if="variantJob.total">失败用例 {{ variantJob.completed }} / {{ variantJob.total }}</p><p v-if="variantJob.error" class="job-error">{{ variantJob.error }}</p>
        </section>

        <section v-if="variantPreview" class="result-card">
          <div class="section-heading"><div><span class="section-kicker">RESULT PREVIEW</span><h2>变体任务结果 <small>{{ variantPreview.total }} 条</small></h2></div><div class="download-actions"><a :href="taskGenerationDownloadUrl(variantPreview.job.job_id, 'json')" target="_blank"><el-button link :icon="Download">下载 JSON</el-button></a><a :href="taskGenerationDownloadUrl(variantPreview.job.job_id, 'xlsx')" target="_blank"><el-button type="primary" plain :icon="Download">下载 Excel</el-button></a></div></div>
          <el-table :data="activeRows" max-height="560"><el-table-column v-for="column in previewColumns" :key="column" :prop="column" :label="column" min-width="150" show-overflow-tooltip><template #default="scope">{{ display(scope.row[column]) }}</template></el-table-column></el-table>
        </section>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.hero-actions{display:flex;align-items:center}.section-kicker{color:var(--accent-deep);font-size:10px;font-weight:950;letter-spacing:.16em}.source-card,.control-card,.result-card,.job-card{margin-top:18px;padding:18px 20px;border:1px solid var(--line);border-radius:16px;background:rgba(255,255,255,.86);box-shadow:0 10px 28px rgba(15,23,42,.04)}.source-card{border-color:#fecaca;background:#fffafa}.source-card--ready{border-color:#99f6e4;background:#f0fdfa}.source-card__heading,.section-heading,.job-card__heading,.control-footer,.uploaded-file,.variant-controls{display:flex;align-items:center;justify-content:space-between;gap:16px}.source-card h2,.control-card h2,.result-card h2{margin:4px 0 0;font-size:18px}.source-card h2 small,.result-card h2 small{color:var(--muted);font-size:12px;font-weight:500}.source-card__files{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:15px 0}.source-file{display:flex;align-items:center;gap:10px;min-width:0;padding:11px;border:1px solid #dbeafe;border-radius:11px;background:white}.source-file__icon{display:grid;place-items:center;flex:0 0 30px;height:30px;border-radius:8px;background:#ccfbf1;color:#0f766e}.source-file>div:nth-child(2){display:grid;gap:3px;min-width:0;flex:1}.source-file b,.source-file span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.source-file b{font-size:12px}.source-file span,.source-location{color:var(--muted);font-size:11px}.source-location{display:block;margin-top:11px}.generation-tabs{margin-top:22px}.control-card{background:white}.control-grid{display:grid;grid-template-columns:repeat(5,minmax(150px,1fr));gap:12px;margin-top:18px}.control-grid label,.variant-controls label{display:grid;gap:7px;color:var(--muted);font-size:12px;font-weight:700}.control-grid .el-select,.control-grid .el-input-number{width:100%}.control-footer{margin-top:20px;color:var(--muted);font-size:12px}.job-card{border-color:#bfdbfe;background:#eff6ff}.job-card--succeeded{border-color:#99f6e4;background:#f0fdfa}.job-card--failed,.job-card--interrupted{border-color:#fecaca;background:#fff1f2}.job-card__heading b{display:block}.job-card__heading span{display:block;margin-top:4px;color:var(--muted);font-size:12px}.job-card__heading strong{font-size:20px}.job-card p{margin:9px 0 0;color:var(--muted);font-size:12px}.job-error{color:#be123c!important;overflow-wrap:anywhere}.download-actions{display:flex;gap:8px}.result-card{overflow:hidden}.result-card :deep(.el-table){margin-top:15px}.flywheel-upload{display:flex;align-items:center;gap:14px;margin-top:18px}.upload-box{position:relative;min-width:300px}.upload-box input{position:absolute;inset:0;z-index:1;width:100%;height:100%;opacity:0;cursor:pointer}.upload-box label{display:grid;justify-items:center;gap:6px;padding:22px;border:1px dashed #5eead4;border-radius:13px;background:#f0fdfa;color:#0f766e;text-align:center;cursor:pointer}.upload-box label span{color:var(--muted);font-size:11px;font-weight:400}.uploaded-file{justify-content:flex-start;padding:13px 16px;border:1px solid #bbf7d0;border-radius:12px;background:#f0fdf4}.uploaded-file div{display:grid;gap:4px}.uploaded-file span{color:var(--muted);font-size:12px}.variant-controls{justify-content:flex-end;margin-top:18px;padding-top:16px;border-top:1px solid var(--line)}
@media(max-width:1150px){.control-grid{grid-template-columns:repeat(3,minmax(160px,1fr))}.source-card__files{grid-template-columns:1fr}.source-file{max-width:none}}@media(max-width:760px){.page-hero,.control-footer,.section-heading,.variant-controls,.flywheel-upload{align-items:stretch;flex-direction:column}.control-grid{grid-template-columns:1fr}.control-footer .el-button,.variant-controls .el-button{width:100%}.upload-box{width:100%}}
</style>
