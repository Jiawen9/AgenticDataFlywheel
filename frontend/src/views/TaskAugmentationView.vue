<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, Upload } from '@element-plus/icons-vue'
import { api, taskGenerationDownloadUrl } from '@/api'
import type { TaskGenerationJob, TaskGenerationResult } from '@/types'

const jobs = ref<TaskGenerationJob[]>([])
const selectedFile = ref<File | null>(null)
const generateN = ref(10)
const submitting = ref(false)
const selectedJob = ref<TaskGenerationJob | null>(null)
const results = ref<TaskGenerationResult[]>([])
const resultErrors = ref<TaskGenerationJob['errors']>([])
const editingId = ref<string | null>(null)
const editingText = ref('')
const fileInput = ref<HTMLInputElement>()
let pollTimer: number | undefined

const augmentationJobs = computed(() => jobs.value.filter((item) => item.kind === 'augmentation'))
const activeJob = computed(() => selectedJob.value && ['queued', 'running'].includes(selectedJob.value.status) ? selectedJob.value : null)
const activeResults = computed(() => results.value.filter((item) => !item.deleted))

function chooseFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  if (!/\.(xlsx|xlsm)$/i.test(file.name)) {
    ElMessage.error('只支持 .xlsx 或 .xlsm 文件')
    return
  }
  selectedFile.value = file
}

function statusText(status: TaskGenerationJob['status']) {
  return { queued: '排队中', running: '执行中', succeeded: '已完成', partial: '部分完成', failed: '失败', interrupted: '已中断' }[status]
}

async function loadJobs() {
  try {
    jobs.value = await api.taskGenerationJobs()
    const current = augmentationJobs.value.find((job) => ['queued', 'running'].includes(job.status)) || augmentationJobs.value[0]
    if (current) await selectJob(current)
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function submit() {
  if (!selectedFile.value) return ElMessage.warning('请先选择种子任务 Excel')
  submitting.value = true
  try {
    const job = await api.createAugmentation(selectedFile.value, generateN.value)
    jobs.value = [job, ...jobs.value.filter((item) => item.job_id !== job.job_id)]
    selectedFile.value = null
    if (fileInput.value) fileInput.value.value = ''
    await selectJob(job)
    ElMessage.success('任务扩增作业已提交')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    submitting.value = false
  }
}

async function selectJob(job: TaskGenerationJob) {
  selectedJob.value = job
  if (!['queued', 'running'].includes(job.status)) {
    try {
      const payload = await api.taskGenerationResults(job.job_id)
      results.value = payload.results
      resultErrors.value = payload.errors
    } catch (error) {
      ElMessage.error((error as Error).message)
    }
  }
  if (['queued', 'running'].includes(job.status)) schedulePoll(0)
}

async function poll() {
  if (!selectedJob.value) return
  try {
    const updated = await api.taskGenerationJob(selectedJob.value.job_id)
    selectedJob.value = updated
    jobs.value = jobs.value.map((job) => job.job_id === updated.job_id ? updated : job)
    if (['queued', 'running'].includes(updated.status)) schedulePoll()
    else {
      const payload = await api.taskGenerationResults(updated.job_id)
      results.value = payload.results
      resultErrors.value = payload.errors
      if (updated.status === 'succeeded') ElMessage.success('任务扩增完成')
      else if (updated.status === 'partial') ElMessage.warning('任务扩增部分完成，请查看错误项')
      else ElMessage.error(updated.error || '任务扩增未完成')
    }
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

function schedulePoll(delay = 1200) {
  if (pollTimer) window.clearTimeout(pollTimer)
  pollTimer = window.setTimeout(() => void poll(), delay)
}

function startEdit(item: TaskGenerationResult) {
  editingId.value = item.result_id
  editingText.value = item.task
}

async function saveEdit(item: TaskGenerationResult) {
  if (!selectedJob.value) return
  try {
    const updated = await api.patchTaskGenerationResult(selectedJob.value.job_id, item.result_id, { task: editingText.value })
    results.value = results.value.map((value) => value.result_id === updated.result_id ? updated : value)
    editingId.value = null
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function toggleDeleted(item: TaskGenerationResult) {
  if (!selectedJob.value) return
  try {
    await ElMessageBox.confirm(`确定${item.deleted ? '恢复' : '删除'}这条变体任务吗？`, '确认操作', { type: 'warning' })
    const updated = await api.patchTaskGenerationResult(selectedJob.value.job_id, item.result_id, { deleted: !item.deleted })
    results.value = results.value.map((value) => value.result_id === updated.result_id ? updated : value)
  } catch {
    // Cancelled confirmation is intentionally silent.
  }
}

async function exportResults() {
  if (!selectedJob.value) return
  try {
    const exported = await api.taskGenerationExport(selectedJob.value.job_id)
    const link = document.createElement('a')
    link.href = taskGenerationDownloadUrl(selectedJob.value.job_id, exported.filename)
    link.download = exported.filename
    link.click()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

onMounted(loadJobs)
onBeforeUnmount(() => { if (pollTimer) window.clearTimeout(pollTimer) })
</script>

<template>
  <div class="page augmentation-page">
    <header class="page-hero">
      <div><span class="eyebrow">TASK AUGMENTATION</span><h1>任务扩增</h1><p>上传失败任务种子，自动匹配场景能力并生成符合 App 先验的变体任务。</p></div>
      <div class="hero-metrics"><div><b>{{ activeResults.length }}</b><span>当前变体</span></div><div><b>{{ augmentationJobs.length }}</b><span>扩增作业</span></div><div><b>{{ selectedJob?.generate_n ?? generateN }}</b><span>每种子</span></div></div>
    </header>

    <section class="upload-card">
      <div class="upload-copy"><span class="eyebrow">FAILED SEED WORKBOOK</span><h2>上传种子任务</h2><p>支持含有“任务、涉及APP”的原始表；如果包含“任务结果”列，只处理结果不是 TRUE 的任务。也支持已包含 app/task/scene/capability/sub_capability 的“新场景匹配”表。</p></div>
      <div class="upload-actions"><label class="file-picker"><el-icon><Upload /></el-icon><span>{{ selectedFile?.name || '选择 Excel 文件' }}</span><input ref="fileInput" type="file" accept=".xlsx,.xlsm" @change="chooseFile" /></label><div class="count-control"><span>每个种子生成</span><el-input-number v-model="generateN" :min="1" :max="20" /></div><el-button type="primary" size="large" :loading="submitting" @click="submit">提交任务扩增</el-button></div>
    </section>

    <section class="jobs-card">
      <div class="section-title"><div><span class="eyebrow">AUGMENTATION QUEUE</span><h2>作业与变体审核</h2></div><el-button v-if="selectedJob && results.length" type="primary" plain :icon="Download" @click="exportResults">导出当前结果</el-button></div>
      <div v-if="augmentationJobs.length" class="job-tabs"><button v-for="job in augmentationJobs.slice(0, 8)" :key="job.job_id" :class="{ active: selectedJob?.job_id === job.job_id }" @click="selectJob(job)"><b>{{ job.created_at.slice(0, 16).replace('T', ' ') }}</b><span>{{ statusText(job.status) }} · {{ job.result_count }} 条</span></button></div>
      <div v-if="activeJob" class="progress-card"><div><b>{{ activeJob.stage }}</b><span>{{ activeJob.current_item || '等待执行' }} · {{ activeJob.completed_items }} / {{ activeJob.total_items || '待读取' }}</span></div><el-progress :percentage="activeJob.percent" /></div>
      <el-alert v-for="error in resultErrors" :key="`${error.item_id}-${error.stage}-${error.error}`" type="error" :title="`${error.item_id || '种子'}${error.stage ? ` · ${error.stage}` : ''}`" :description="error.error" :closable="false" show-icon />
      <el-empty v-if="!selectedJob" description="上传种子文件后在这里查看结果" />
      <el-empty v-else-if="!results.length && !activeJob" description="作业没有生成可审核结果" />
      <el-table v-else-if="results.length" :data="results" stripe class="result-table">
        <el-table-column prop="用例编号" label="用例编号" width="180" />
        <el-table-column label="场景能力" min-width="220"><template #default="{ row }"><b>{{ row.scene }}</b><span class="cell-sub">{{ row.capability }} / {{ row.sub_capability }}</span></template></el-table-column>
        <el-table-column label="源失败任务" min-width="230"><template #default="{ row }"><span>{{ row.source_task }}</span></template></el-table-column>
        <el-table-column label="生成变体" min-width="360"><template #default="{ row }"><el-input v-if="editingId === row.result_id" v-model="editingText" type="textarea" :rows="2" /><span v-else :class="{ deleted: row.deleted }">{{ row.task }}</span></template></el-table-column>
        <el-table-column label="审核" width="150"><template #default="{ row }"><el-tag :type="row.deleted ? 'info' : 'warning'">{{ row.deleted ? '已删除' : row['审核状态'] || '待人工Review' }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="150" fixed="right"><template #default="{ row }"><template v-if="editingId === row.result_id"><el-button link type="primary" @click="saveEdit(row)">保存</el-button><el-button link @click="editingId = null">取消</el-button></template><template v-else><el-button link type="primary" :disabled="row.deleted" @click="startEdit(row)">编辑</el-button><el-button link :type="row.deleted ? 'success' : 'danger'" @click="toggleDeleted(row)">{{ row.deleted ? '恢复' : '删除' }}</el-button></template></template></el-table-column>
      </el-table>
    </section>
  </div>
</template>

<style scoped>
.augmentation-page{width:min(1680px,100%);margin:0 auto}.upload-card,.jobs-card{border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.84);box-shadow:0 12px 35px rgba(15,23,42,.045)}.upload-card{display:flex;align-items:center;justify-content:space-between;gap:28px;margin:20px 0;padding:22px}.upload-copy{max-width:760px}.upload-copy h2{margin:5px 0 8px;font-size:22px}.upload-copy p{margin:0;color:var(--muted);font-size:13px;line-height:1.75}.upload-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;justify-content:flex-end}.file-picker{display:flex;align-items:center;gap:8px;max-width:250px;padding:11px 14px;border:1px dashed #94a3b8;border-radius:11px;color:#475569;font-size:12px;font-weight:700;cursor:pointer}.file-picker span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.file-picker input{display:none}.count-control{display:grid;gap:4px;color:var(--muted);font-size:11px;font-weight:800}.jobs-card{padding:18px 20px}.section-title{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:14px}.section-title h2{margin:4px 0 0;font-size:20px}.job-tabs{display:flex;gap:8px;overflow-x:auto;padding-bottom:12px}.job-tabs button{display:grid;gap:4px;min-width:170px;padding:10px 12px;border:1px solid var(--line);border-radius:11px;background:#fff;color:var(--ink);text-align:left;cursor:pointer}.job-tabs button.active{border-color:#5eead4;background:#f0fdfa}.job-tabs span{color:var(--muted);font-size:11px}.progress-card{display:flex;align-items:center;gap:20px;margin-bottom:12px;padding:13px 15px;border:1px solid #bfdbfe;border-radius:12px;background:#eff6ff}.progress-card>div{display:grid;gap:4px;min-width:240px}.progress-card span{color:var(--muted);font-size:12px}.progress-card .el-progress{flex:1}.result-table{margin-top:12px}.cell-sub{display:block;margin-top:4px;color:var(--muted);font-size:11px}.deleted{color:#94a3b8;text-decoration:line-through}.el-alert{margin:7px 0}@media(max-width:1100px){.upload-card{align-items:stretch;flex-direction:column}.upload-actions{justify-content:flex-start}.progress-card{align-items:stretch;flex-direction:column;gap:8px}.progress-card .el-progress{width:100%}}
</style>
