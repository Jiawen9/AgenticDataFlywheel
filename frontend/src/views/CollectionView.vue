<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Check, Clock, Refresh, Select } from '@element-plus/icons-vue'
import { api } from '@/api'
import type { BuildJob, TaskSummary, TrajectoryRecord, TrajectorySummary } from '@/types'
import TrajectoryExplorer from '@/components/TrajectoryExplorer.vue'

const ACTIVE_JOB_KEY = 'trajectory-tree-active-job'
const tasks = ref<TaskSummary[]>([])
const selected = ref<string[]>([])
const expandedTasks = ref<string[]>([])
const expandedTrajectories = reactive<Record<string, string>>({})
const taskData = reactive<Record<string, TrajectorySummary[]>>({})
const trajectoryData = reactive<Record<string, TrajectoryRecord>>({})
const loadingTasks = reactive<Record<string, boolean>>({})
const loadingTrajectories = reactive<Record<string, boolean>>({})
const loading = ref(true)
const submitting = ref(false)
const job = ref<BuildJob | null>(null)
let pollTimer: number | undefined

const eligibleTasks = computed(() => tasks.value.filter((task) => task.annotated))
const allSelected = computed(
  () => eligibleTasks.value.length > 0 && eligibleTasks.value.every((task) => selected.value.includes(task.task_id)),
)
const stageText = computed(() => {
  const labels: Record<string, string> = {
    queued: '等待执行', classifying: 'Qwen 中间态分类', building: '构建轨迹树',
    publishing: '发布任务集', succeeded: '构建完成', failed: '构建失败', interrupted: '作业已中断',
  }
  return labels[job.value?.stage || ''] || job.value?.stage || ''
})

async function loadTasks() {
  loading.value = true
  try { tasks.value = await api.tasks() }
  catch (error) { ElMessage.error((error as Error).message) }
  finally { loading.value = false }
}

async function loadTask(taskId: string) {
  if (taskData[taskId] || loadingTasks[taskId]) return
  loadingTasks[taskId] = true
  try { taskData[taskId] = (await api.trajectories(taskId)).trajectories }
  catch (error) { ElMessage.error((error as Error).message) }
  finally { loadingTasks[taskId] = false }
}

function onTaskExpand(values: string[]) {
  values.forEach((taskId) => void loadTask(taskId))
}

function trajectoryKey(taskId: string, trajectoryId: string) {
  return `${taskId}/${trajectoryId}`
}

async function onTrajectoryExpand(taskId: string, trajectoryId: string) {
  if (!trajectoryId) return
  const key = trajectoryKey(taskId, trajectoryId)
  if (trajectoryData[key] || loadingTrajectories[key]) return
  loadingTrajectories[key] = true
  try { trajectoryData[key] = await api.trajectory(taskId, trajectoryId) }
  catch (error) { ElMessage.error((error as Error).message) }
  finally { loadingTrajectories[key] = false }
}

function toggleSelectAll() {
  selected.value = allSelected.value ? [] : eligibleTasks.value.map((task) => task.task_id)
}

async function submitBuild() {
  if (!selected.value.length) return ElMessage.warning('请至少选择一个已预处理任务')
  submitting.value = true
  try {
    job.value = await api.createBuild(selected.value)
    localStorage.setItem(ACTIVE_JOB_KEY, job.value.job_id)
    ElMessage.success('后台建树作业已提交')
    schedulePoll(0)
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { submitting.value = false }
}

function schedulePoll(delay = 1000) {
  if (pollTimer) window.clearTimeout(pollTimer)
  pollTimer = window.setTimeout(() => void pollJob(), delay)
}

async function pollJob() {
  const jobId = job.value?.job_id || localStorage.getItem(ACTIVE_JOB_KEY)
  if (!jobId) return
  try {
    job.value = await api.build(jobId)
    if (job.value.status === 'queued' || job.value.status === 'running') schedulePoll()
    else {
      localStorage.removeItem(ACTIVE_JOB_KEY)
      if (job.value.status === 'succeeded') ElMessage.success(`任务集 ${job.value.run_id} 已生成`)
      else ElMessage.error(job.value.error || '建树作业未成功完成')
    }
  } catch (error) {
    localStorage.removeItem(ACTIVE_JOB_KEY)
    ElMessage.error((error as Error).message)
  }
}

onMounted(async () => {
  await loadTasks()
  if (localStorage.getItem(ACTIVE_JOB_KEY)) await pollJob()
})
onBeforeUnmount(() => { if (pollTimer) window.clearTimeout(pollTimer) })
</script>

<template>
  <div class="page collection-page">
    <header class="page-hero">
      <div>
        <span class="eyebrow">TRAJECTORY COLLECTION</span>
        <h1>轨迹采集</h1>
        <p>浏览原始步骤、检查动作标框，并将选中任务提交为可质检的轨迹树任务集。</p>
      </div>
      <div class="hero-metrics">
        <div><b>{{ tasks.length }}</b><span>任务</span></div>
        <div><b>{{ tasks.reduce((sum, item) => sum + item.trajectory_count, 0) }}</b><span>轨迹</span></div>
        <div><b>{{ tasks.reduce((sum, item) => sum + item.step_count, 0) }}</b><span>步骤</span></div>
      </div>
    </header>

    <section class="toolbar-card">
      <div class="selection-summary">
        <el-button :icon="Select" @click="toggleSelectAll">{{ allSelected ? '取消全选' : '全选可用任务' }}</el-button>
        <span>已选择 <b>{{ selected.length }}</b> / {{ eligibleTasks.length }} 个任务</span>
      </div>
      <el-button type="primary" size="large" :loading="submitting" :disabled="!selected.length" @click="submitBuild">
        提交轨迹树构建
      </el-button>
    </section>

    <section v-if="job" class="job-card" :class="`job-card--${job.status}`">
      <div class="job-card__icon"><el-icon><Check v-if="job.status === 'succeeded'" /><Clock v-else /></el-icon></div>
      <div class="job-card__body">
        <div class="job-card__heading">
          <div><b>{{ stageText }}</b><span v-if="job.current_task">当前：{{ job.current_task }}</span></div>
          <span>{{ job.percent }}%</span>
        </div>
        <el-progress :percentage="job.percent" :status="job.status === 'failed' ? 'exception' : job.status === 'succeeded' ? 'success' : undefined" :show-text="false" />
        <p v-if="job.stage === 'summarizing_trajectories'">轨迹摘要 {{ job.summarized_trajectories || 0 }} / {{ job.total_trajectories || 0 }} · observation 已完成 {{ job.classified_steps }} 步</p>
        <p v-else-if="job.total_steps">分类与 observation {{ job.classified_steps }} / {{ job.total_steps }} · 任务 {{ job.task_index }} / {{ job.total_tasks }}</p>
        <p v-if="job.error" class="job-card__error">{{ job.error }}</p>
        <router-link v-if="job.run_id" :to="{ path: '/quality', query: { run: job.run_id } }">进入任务集 {{ job.run_id }} →</router-link>
      </div>
    </section>

    <section v-loading="loading" class="task-list">
      <el-empty v-if="!loading && !tasks.length" description="未发现轨迹任务" />
      <el-collapse v-else v-model="expandedTasks" @change="onTaskExpand">
        <el-collapse-item v-for="task in tasks" :key="task.task_id" :name="task.task_id">
          <template #title>
            <div class="task-title">
              <el-checkbox v-model="selected" :value="task.task_id" :disabled="!task.annotated" @click.stop />
              <div class="task-title__id">{{ task.task_id }}</div>
              <div class="task-title__goal">{{ task.goal }}</div>
              <el-tag :type="task.annotated ? 'success' : 'warning'" round>
                {{ task.annotated ? '已预处理' : '待预处理' }}
              </el-tag>
              <span>{{ task.trajectory_count }} 轨迹 · {{ task.step_count }} 步</span>
            </div>
          </template>
          <el-alert v-if="task.warning" :title="task.warning" type="warning" :closable="false" show-icon />
          <div v-loading="loadingTasks[task.task_id]" class="trajectory-list">
            <el-empty v-if="!loadingTasks[task.task_id] && !taskData[task.task_id]?.length" description="暂无预处理轨迹" />
            <el-collapse
              v-else
              v-model="expandedTrajectories[task.task_id]"
              accordion
              @change="(trajectoryId: string) => onTrajectoryExpand(task.task_id, trajectoryId)"
            >
              <el-collapse-item
                v-for="trajectory in taskData[task.task_id]"
                :key="trajectory.trajectory_id"
                :name="trajectory.trajectory_id"
              >
                <template #title>
                  <div class="trajectory-title"><el-icon><Refresh /></el-icon><b>{{ trajectory.trajectory_id }}</b><span>{{ trajectory.step_count }} steps</span></div>
                </template>
                <div v-loading="loadingTrajectories[trajectoryKey(task.task_id, trajectory.trajectory_id)]" class="trajectory-detail">
                  <TrajectoryExplorer
                    v-if="trajectoryData[trajectoryKey(task.task_id, trajectory.trajectory_id)]"
                    :task-id="task.task_id"
                    :trajectory="trajectoryData[trajectoryKey(task.task_id, trajectory.trajectory_id)]"
                  />
                </div>
              </el-collapse-item>
            </el-collapse>
          </div>
        </el-collapse-item>
      </el-collapse>
    </section>
  </div>
</template>

<style scoped>
.toolbar-card { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin: 22px 0 16px; padding: 16px 18px; border: 1px solid var(--line); border-radius: 16px; background: rgba(255,255,255,.86); }
.selection-summary { display: flex; align-items: center; gap: 16px; color: var(--muted); }
.selection-summary b { color: var(--accent-deep); }
.job-card { display: flex; gap: 16px; margin-bottom: 16px; padding: 18px; border: 1px solid #bae6fd; border-radius: 16px; background: #f0f9ff; }
.job-card--succeeded { border-color: #99f6e4; background: #f0fdfa; }
.job-card--failed, .job-card--interrupted { border-color: #fecaca; background: #fef2f2; }
.job-card__icon { display: grid; place-items: center; flex: 0 0 44px; height: 44px; border-radius: 12px; background: #0f172a; color: white; font-size: 20px; }
.job-card__body { flex: 1; min-width: 0; }
.job-card__heading { display: flex; justify-content: space-between; margin-bottom: 10px; }
.job-card__heading div { display: flex; gap: 12px; align-items: baseline; }
.job-card__heading span, .job-card p { color: var(--muted); font-size: 13px; }
.job-card p { margin: 8px 0 0; }.job-card__error { color: #b91c1c !important; }.job-card a { display: inline-block; margin-top: 9px; color: var(--accent-deep); font-weight: 800; }
.task-list { min-height: 180px; }
.task-title { display: grid; grid-template-columns: 32px 190px minmax(240px,1fr) auto 140px; align-items: center; gap: 14px; width: calc(100% - 36px); padding-right: 16px; }
.task-title__id { font-weight: 900; letter-spacing: .02em; }.task-title__goal { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #334155; }.task-title > span:last-child { color: var(--muted); font-size: 12px; text-align: right; }
.trajectory-list { padding: 10px 14px 18px 46px; min-height: 80px; }.trajectory-title { display: flex; align-items: center; gap: 9px; }.trajectory-title span { color: var(--muted); font-size: 12px; font-weight: 400; }
.trajectory-detail { min-height: 160px; }
@media (max-width: 900px) { .task-title { grid-template-columns: 28px 1fr auto; }.task-title__goal, .task-title > span:last-child { display: none; }.toolbar-card { align-items: stretch; flex-direction: column; }.trajectory-list { padding-left: 0; } }
</style>
