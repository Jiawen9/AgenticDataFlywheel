<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, Refresh, Search, Setting } from '@element-plus/icons-vue'
import { api, ApiError, sceneTreeDownloadUrl, taskGenerationDownloadUrl } from '@/api'
import type { KnowledgeBaseSummary, TaskGenerationTreeNode } from '@/types'
import { executionUnitCount, leaves, normalizeTree, selectionsFor } from '@/utils/scenarioTree'
import { generationJobOptionLabel, isGenerationActive, jobLabels, reconcileApps, stageLabel, unitLabel, type AppSelection } from '@/utils/taskGeneration'
import { useTaskGenerationReview } from '@/composables/useTaskGenerationReview'
import TaskGenerationScope from '@/components/TaskGenerationScope.vue'
import TaskGenerationResults from '@/components/TaskGenerationResults.vue'
import CollectionBatchSubmission from '@/components/CollectionBatchSubmission.vue'

const mode = ref<'new' | 'records'>('new')
const tree = ref<TaskGenerationTreeNode[]>([])
const version = ref('')
const knowledgeBases = ref<KnowledgeBaseSummary[]>([])
const warnings = ref<string[]>([])
const selectedApps = ref<AppSelection>({})
const generateN = ref(5)
const resourceLoading = ref(false)
const resourceError = ref('')
const resourceOpen = ref(false)
const replacing = ref(false)
const submitting = ref(false)
const jobQuery = ref('')
const jobStatus = ref('')
const selectedGenerationJobId = ref('')
let disposed = false
const kbNames = { scene_tree: '场景树', control_prior: '操控先验', resource_prior: '资源先验' }
const review = useTaskGenerationReview(api, {
  error: message => ElMessage.error(message),
  decide: async () => {
    try {
      await ElMessageBox.confirm('当前任务文本尚未保存。保存后继续，或放弃修改；关闭此提示则留在当前位置。', '未保存的任务修改', {
        confirmButtonText: '保存后继续', cancelButtonText: '放弃修改', distinguishCancelAndClose: true, closeOnClickModal: false, type: 'warning',
      })
      return 'save'
    } catch (action) { return action === 'cancel' ? 'discard' : 'cancel' }
  },
  confirmDelete: async row => {
    try {
      await ElMessageBox.confirm(`确定${row.deleted ? '恢复' : '删除'}这条任务${row.dependency_group_id ? '及同组关联任务' : ''}吗？删除不会移除历史文件。`, '确认操作', { type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消' })
      return true
    } catch { return false }
  },
})
const { jobs, selectedJob, results, errors, listError, detailError, loading: detailLoading, refreshing, busy, editingId, editingText, dirty } = review
const selectedTypes = computed(() => leaves(tree.value).filter(node => selectedApps.value[node.id]?.length))
const selections = computed(() => selectionsFor(tree.value, selectedTypes.value.map(node => node.id), selectedApps.value))
const ready = computed(() => Boolean(version.value && !resourceError.value && !resourceLoading.value && !replacing.value && !submitting.value && executionUnitCount(selections.value) > 0 && Number.isInteger(generateN.value) && generateN.value >= 1 && generateN.value <= 20))
const validResources = computed(() => knowledgeBases.value.filter(item => item.valid).length)
const filteredJobs = computed(() => jobs.value.filter(job => (!jobStatus.value || job.status === jobStatus.value) && [job.job_id, job.created_at, jobLabels[job.status]].join(' ').toLowerCase().includes(jobQuery.value.trim().toLowerCase())))
const activeJob = computed(() => selectedJob.value && isGenerationActive(selectedJob.value) ? selectedJob.value : null)
const collectionTaskCount = computed(() => results.value.filter(row => !row.deleted).length)

async function refreshResources() {
  if (resourceLoading.value) return
  resourceLoading.value = true
  try {
    const [bases, payload] = await Promise.all([api.taskGenerationKnowledgeBases(), api.taskGenerationTree()])
    if (disposed) return
    const next = normalizeTree(payload.scenes)
    const reconciled = reconcileApps(next, selectedApps.value)
    tree.value = next; version.value = payload.version; warnings.value = payload.warnings || []; knowledgeBases.value = bases
    selectedApps.value = reconciled.selected; resourceError.value = ''
    if (reconciled.removed) ElMessage.warning(`资源已更新，移除了 ${reconciled.removed} 个失效的类型/App 选择，请确认生成范围。`)
  } catch (error) { if (!disposed) resourceError.value = (error as Error).message }
  finally { resourceLoading.value = false }
}
async function replaceKnowledgeBase(kind: KnowledgeBaseSummary['kind'], event: Event) {
  const input = event.target as HTMLInputElement, file = input.files?.[0]
  if (!file || replacing.value) return
  try {
    await ElMessageBox.confirm(`替换${kbNames[kind]}将发布新的知识库版本，已有作业仍使用提交时的快照。`, '替换知识库', { type: 'warning', confirmButtonText: '替换', cancelButtonText: '取消' })
    replacing.value = true
    await api.replaceTaskGenerationKnowledgeBase(kind, file, version.value || undefined)
    await refreshResources()
    if (!disposed) ElMessage.success('知识库新版本已发布')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close' && !disposed) {
      ElMessage.error((error as Error).message)
      if (error instanceof ApiError && error.status === 409) await refreshResources()
    }
  } finally { replacing.value = false; input.value = '' }
}
async function switchMode(next: 'new' | 'records') {
  if (mode.value === next || submitting.value || !(await review.protect())) return
  mode.value = next
}
async function selectGenerationJob(jobId: string) {
  const previousId = selectedJob.value?.job_id || ''
  const job = jobs.value.find(item => item.job_id === jobId)
  if (!job || !(await review.selectJob(job))) selectedGenerationJobId.value = previousId
}
async function submit() {
  if (!ready.value || !(await review.protect()) || !ready.value) return
  submitting.value = true
  try {
    const job = await api.createTaskGeneration(selections.value, generateN.value, version.value)
    if (disposed) return
    mode.value = 'records'; await review.acceptJob(job); ElMessage.success('任务生成作业已提交')
  } catch (error) {
    if (!disposed) {
      if (error instanceof ApiError && error.status === 409) {
        await refreshResources(); ElMessage.warning('知识库版本已变化，已重新加载。请确认生成范围后再次提交。')
      } else ElMessage.error((error as Error).message)
    }
  } finally { submitting.value = false }
}
async function exportResults() {
  const result = await review.exportResults()
  if (!result || disposed) return
  const link = document.createElement('a')
  link.href = taskGenerationDownloadUrl(result.jobId, result.exported.filename); link.download = result.exported.filename; link.click()
}
function beforeUnload(event: BeforeUnloadEvent) { if (dirty.value || busy.value || submitting.value) { event.preventDefault(); event.returnValue = '' } }
watch(() => selectedJob.value?.job_id, jobId => { selectedGenerationJobId.value = jobId || '' }, { immediate: true, flush: 'sync' })
onBeforeRouteLeave(async () => !submitting.value && await review.protect())
onMounted(() => { void refreshResources(); void review.refreshJobs(); window.addEventListener('beforeunload', beforeUnload) })
onBeforeUnmount(() => { disposed = true; review.dispose(); window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <div class="page task-generation-page">
    <header class="generation-header"><div><span class="eyebrow">任务池 / TASK GENERATION</span><h1>任务生成</h1><p>从场景能力中选择任务类型与 App，生成、审核并导出任务。</p></div><el-button :icon="Setting" @click="resourceOpen = true">管理生成资源</el-button></header>
    <div class="generation-tabs" role="tablist" aria-label="任务生成工作区"><button role="tab" :aria-selected="mode === 'new'" :class="{ active: mode === 'new' }" @click="switchMode('new')">新建生成</button><button role="tab" :aria-selected="mode === 'records'" :class="{ active: mode === 'records' }" @click="switchMode('records')">生成记录 <span>{{ jobs.length }}</span><i v-if="jobs.some(isGenerationActive)" aria-label="存在活动作业"></i></button><span v-if="dirty" class="dirty-note">有未保存的任务修改</span></div>
    <section v-show="mode === 'new'" class="generation-panel" aria-label="新建生成">
      <div class="resource-strip"><span>知识库 <b>{{ version ? version.slice(0, 8) : '未加载' }}</b></span><span :class="{ 'resource-warning': validResources < 3 }">{{ resourceLoading ? '正在加载…' : `${validResources} / 3 项资源可用` }}</span><el-button v-if="warnings.length" text type="warning" @click="resourceOpen = true">{{ warnings.length }} 项提示</el-button><el-button text :icon="Refresh" :loading="resourceLoading" :disabled="replacing || submitting" @click="refreshResources">刷新资源</el-button></div>
      <el-alert v-if="resourceError" :title="resourceError" type="error" :closable="false"><el-button text @click="refreshResources">重试加载资源</el-button></el-alert>
      <TaskGenerationScope :tree="tree" v-model:selected="selectedApps" v-model:count="generateN" :disabled="resourceLoading || replacing || submitting" :submitting="submitting" :ready="ready" @submit="submit" />
    </section>
    <section v-show="mode === 'records'" class="generation-panel" aria-label="生成记录">
      <div class="records-toolbar"><h2>生成记录</h2><el-button text :icon="Refresh" :loading="refreshing" :disabled="busy" @click="review.refreshJobs">刷新记录</el-button></div>
      <el-alert v-if="listError" :title="listError" type="error" :closable="false"><el-button text @click="review.refreshJobs">重试加载记录</el-button></el-alert>
      <div class="collection-job-selector">
        <div><b>生成批次任务</b><span>选择要审核并提交轨迹采集的生成作业</span></div>
        <el-select v-model="selectedGenerationJobId" filterable placeholder="选择生成批次任务" aria-label="选择生成批次任务" :disabled="busy || refreshing || submitting" @change="selectGenerationJob">
          <el-option v-for="job in jobs" :key="job.job_id" :label="generationJobOptionLabel(job)" :value="job.job_id" />
        </el-select>
      </div>
      <CollectionBatchSubmission v-if="selectedJob" :key="selectedJob.job_id" :job="selectedJob" :result-count="collectionTaskCount" :busy="busy || detailLoading || refreshing || submitting" :protect="review.protect" :run-protected="review.runExternalAction" />
      <div class="records-layout">
        <aside class="job-directory"><el-input v-model="jobQuery" clearable :prefix-icon="Search" placeholder="搜索时间、状态或作业 ID" aria-label="搜索生成记录" /><el-select v-model="jobStatus" clearable placeholder="全部状态" aria-label="筛选作业状态"><el-option v-for="(label, value) in jobLabels" :key="value" :label="label" :value="value" /></el-select><div class="job-list"><button v-for="job in filteredJobs" :key="job.job_id" :data-job-id="job.job_id" :class="{ active: selectedJob?.job_id === job.job_id }" :disabled="busy" @click="review.selectJob(job)"><div><strong>{{ job.created_at.slice(0, 16).replace('T', ' ') }}</strong><span :class="`status-${job.status}`">{{ jobLabels[job.status] }}</span></div><p>{{ job.completed_items }} / {{ job.total_items }} 个执行单元 · {{ job.result_count }} 条结果</p><small>{{ job.job_id.slice(0, 12) }}</small></button><el-empty v-if="!filteredJobs.length" :description="jobs.length ? '没有匹配的记录' : '暂无生成记录'" :image-size="50" /></div></aside>
        <div class="job-detail" :aria-busy="detailLoading">
          <el-empty v-if="!selectedJob" description="提交生成后，在这里查看进度与审核结果" :image-size="70" />
          <template v-else><header class="job-heading"><div><h2>{{ jobLabels[selectedJob.status] }}</h2><p>{{ selectedJob.created_at.slice(0, 19).replace('T', ' ') }} · 作业 {{ selectedJob.job_id }}</p></div><el-tag type="info">快照 {{ selectedJob.knowledge_base_version?.slice(0, 8) || '历史版本' }}</el-tag></header><p class="job-summary">{{ selectedJob.total_items }} 个执行单元<span v-if="selectedJob.expected_main_tasks != null"> · 预计 {{ selectedJob.expected_main_tasks }} 条主任务</span> · {{ selectedJob.result_count }} 条生成结果（含前置任务）</p>
            <div v-if="activeJob" class="job-progress"><div><b>{{ stageLabel(activeJob.stage) }}</b><span>{{ activeJob.completed_items }} / {{ activeJob.total_items }}</span></div><el-progress :percentage="activeJob.percent" /><p>{{ unitLabel(activeJob, activeJob.current_item) }}</p><small>作业完成后显示可审核结果，切换页面不影响后台执行。</small></div>
            <details v-if="errors.length || selectedJob.error || selectedJob.warnings?.length" class="job-issues"><summary>{{ errors.length }} 项错误 · {{ selectedJob.warnings?.length || 0 }} 项提示</summary><article v-for="(error, index) in errors" :key="index"><strong>{{ unitLabel(selectedJob, error.item_id) }}<span v-if="error.stage"> · {{ stageLabel(error.stage) }}</span></strong><p>{{ error.error }}</p><small v-if="error.item_id">执行单元 {{ error.item_id }}</small></article><p v-if="selectedJob.error">{{ selectedJob.error }}</p><p v-for="warning in selectedJob.warnings" :key="warning">{{ warning }}</p></details>
            <el-alert v-if="detailError" :title="detailError" type="error" :closable="false"><el-button text @click="review.selectJob(selectedJob!, true)">重试加载结果</el-button></el-alert>
            <el-skeleton v-else-if="detailLoading" :rows="5" animated />
            <TaskGenerationResults v-else-if="results.length" :key="selectedJob.job_id" :results="results" :editing-id="editingId" v-model:text="editingText" :busy="busy" :protect="review.protect" @edit="review.startEdit" @delete="review.toggleDeleted" @save="review.saveEdit" @cancel="review.cancelEdit" @export="exportResults" />
            <el-empty v-else-if="!activeJob" description="此作业没有可审核结果，请查看错误与提示" :image-size="65" />
          </template>
        </div>
      </div>
    </section>
    <el-drawer v-model="resourceOpen" title="管理生成资源" size="min(520px, 100vw)" class="generation-resource-drawer">
      <p class="drawer-intro">当前版本 {{ version || '未加载' }}。替换只影响未来作业，历史作业继续使用原快照。</p><a v-if="version" :href="sceneTreeDownloadUrl()" download class="resource-download"><el-icon><Download /></el-icon>下载当前场景树 Excel</a>
      <article v-for="item in knowledgeBases" :key="item.kind" class="resource-item"><header><h3>{{ kbNames[item.kind] }}</h3><el-tag :type="item.valid ? 'success' : 'danger'" size="small">{{ item.valid ? '可用' : '未就绪' }}</el-tag></header><p>{{ item.filename }} · {{ item.rows ?? 0 }} 行</p><p v-if="item.sheets?.length">Sheets：{{ item.sheets.join('、') }}</p><p v-if="item.error" class="resource-warning">{{ item.error }}</p><label class="resource-upload">替换 Excel<input type="file" accept=".xlsx,.xlsm" :aria-label="`替换${kbNames[item.kind]}`" :disabled="replacing || resourceLoading || submitting" @change="replaceKnowledgeBase(item.kind, $event)" /></label></article>
      <details v-if="warnings.length" class="resource-warnings"><summary>{{ warnings.length }} 项资源提示</summary><p v-for="warning in warnings" :key="warning">{{ warning }}</p></details>
      <el-alert v-if="resourceError" :title="resourceError" type="error" :closable="false" /><el-button text :loading="resourceLoading" :disabled="replacing || submitting" @click="refreshResources">刷新资源状态</el-button>
    </el-drawer>
  </div>
</template>

<style scoped>
/* The legacy shell has a 620px body minimum; only this responsive page opts out. */
:global(body:has(.task-generation-page)){min-width:0}
  .task-generation-page{width:min(1700px,100%);margin:0 auto;min-width:0}.generation-header{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:24px}.generation-header h1{font-size:28px;margin:8px 0}.generation-header p{font-size:13px;color:var(--muted);margin:0;line-height:1.7}.generation-tabs{display:flex;gap:24px;align-items:center;border-bottom:1px solid var(--line);margin-bottom:22px}.generation-tabs>button{position:relative;padding:12px 2px;border:0;border-bottom:2px solid transparent;background:transparent;font-size:14px;color:var(--muted);cursor:pointer}.generation-tabs>button.active{color:var(--accent-deep);border-bottom-color:var(--accent-deep);font-weight:650}.generation-tabs button span{font-size:11px;margin-left:6px}.generation-tabs i{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--accent-deep);margin-left:6px}.dirty-note{font-size:12px;color:#92611e}.generation-panel{padding:22px;border:1px solid var(--line);border-radius:12px;background:#fff;min-width:0}.resource-strip{display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding-bottom:16px;margin-bottom:22px;border-bottom:1px solid var(--line);font-size:12px;color:var(--muted)}.resource-strip b{color:var(--ink);font-weight:500;margin-left:6px}.resource-strip .el-button:last-child{margin-left:auto}.resource-warning{color:#9a6423!important}.records-toolbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}.collection-job-selector{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:14px 16px;border:1px solid var(--line);border-radius:9px;background:#fbfcfc}.collection-job-selector>div{display:grid;gap:4px}.collection-job-selector b{font-size:13px}.collection-job-selector span{color:var(--muted);font-size:11px}.collection-job-selector>.el-select{width:min(600px,60%)}h2{font-size:16px;margin:0}.records-layout{display:grid;grid-template-columns:265px minmax(0,1fr);gap:24px}.job-directory{min-width:0;border-right:1px solid var(--line);padding-right:18px}.job-directory>.el-select{width:100%;margin-top:8px}.job-list{max-height:680px;overflow:auto;margin-top:14px}.job-list>button{width:100%;border:1px solid transparent;border-radius:8px;background:transparent;padding:12px;text-align:left;cursor:pointer;margin-bottom:7px}.job-list>button.active{border-color:var(--line);background:#f4f8f7}.job-list>button:hover{background:#f7f9fa}.job-list>button>div{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font-size:12px}.job-list strong{font-weight:550}.job-list p,.job-list small{font-size:11px;color:var(--muted);line-height:1.6;margin:7px 0 0}.status-failed,.status-interrupted{color:#b4533c}.status-partial{color:#9a6423}.status-running,.status-succeeded{color:var(--accent-deep)}.job-detail{min-width:0}.job-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap}.job-heading p,.job-summary{font-size:12px;color:var(--muted);line-height:1.6;overflow-wrap:anywhere}.job-progress{padding:16px;border:1px solid var(--line);border-radius:9px;margin:16px 0;background:#f8fafb}.job-progress>div:first-child{display:flex;justify-content:space-between;gap:12px;margin-bottom:10px;font-size:13px}.job-progress p{font-size:13px;overflow-wrap:anywhere}.job-progress small{font-size:12px;color:var(--muted)}.job-issues{padding:12px 14px;border:1px solid #eadbcb;border-radius:8px;margin:15px 0;color:#885623;font-size:12px}.job-issues summary,.resource-warnings summary{cursor:pointer}.job-issues article{padding-top:12px;border-bottom:1px solid var(--line)}.job-issues p,.job-issues strong{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7}.job-issues small{display:block;margin-bottom:10px}.el-alert{margin-bottom:15px}.drawer-intro,.resource-item p,.resource-warnings p{font-size:12px;color:var(--muted);line-height:1.8;overflow-wrap:anywhere}.resource-download{display:inline-flex;align-items:center;gap:6px;color:var(--accent-deep);font-size:13px;text-decoration:none}.resource-item{margin:18px 0;padding:16px;border:1px solid var(--line);border-radius:9px}.resource-item header{display:flex;align-items:center;justify-content:space-between}.resource-item h3{font-size:14px;margin:0}.resource-upload{display:inline-flex;position:relative;font-size:12px;color:var(--accent-deep);padding:6px 10px;border:1px solid var(--line);border-radius:5px;cursor:pointer}.resource-upload input{position:absolute;inset:0;opacity:0;width:100%;cursor:pointer}.resource-upload:has(input:disabled){opacity:.5;cursor:not-allowed}.resource-warnings{font-size:13px;margin:18px 0}@media(max-width:1100px){.records-layout{grid-template-columns:220px minmax(0,1fr);gap:16px}}@media(max-width:700px){.generation-header{align-items:flex-start;flex-direction:column;gap:12px;margin-bottom:16px}.generation-header h1{font-size:24px}.generation-tabs{gap:20px;flex-wrap:wrap}.generation-panel{padding:14px}.resource-strip{gap:8px 12px}.collection-job-selector{align-items:stretch;flex-direction:column}.collection-job-selector>.el-select{width:100%}.records-layout{grid-template-columns:1fr}.job-directory{border-right:0;border-bottom:1px solid var(--line);padding:0 0 15px}.job-list{max-height:240px}.job-heading{gap:5px}}
</style>
