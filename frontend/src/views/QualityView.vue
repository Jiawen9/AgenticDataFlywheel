<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { DataLine, View } from '@element-plus/icons-vue'
import { api, imageUrl } from '@/api'
import ActionImage from '@/components/ActionImage.vue'
import ReferenceTrajectoryTree from '@/components/ReferenceTrajectoryTree.vue'
import type {
  AuditStep,
  TrajectoryTreeNode,
  TreeOccurrence,
  TreeRun,
  TreeRunTask,
} from '@/types'

const route = useRoute()
const router = useRouter()
const runs = ref<TreeRun[]>([])
const selectedRunId = ref('')
const selectedTaskId = ref('')
const tree = ref<TrajectoryTreeNode | null>(null)
const selectedNode = ref<TrajectoryTreeNode | null>(null)
const selectedOccurrenceIndex = ref(0)
const loadingRuns = ref(true)
const loadingTree = ref(false)
const auditVisible = ref(false)
const auditSelected = ref<(AuditStep & { trajectory: string }) | null>(null)

const selectedRun = computed(() => runs.value.find((run) => run.run_id === selectedRunId.value))
const selectedTask = computed<TreeRunTask | undefined>(() =>
  selectedRun.value?.tasks.find((task) => task.task_id === selectedTaskId.value),
)
const selectedOccurrence = computed<TreeOccurrence | null>(() =>
  selectedNode.value?.occurrences?.[selectedOccurrenceIndex.value] ?? null,
)
const ignoredSteps = computed(() => {
  const result: Array<AuditStep & { trajectory: string }> = []
  for (const trajectory of tree.value?.source_trajectories ?? []) {
    for (const step of trajectory.steps) {
      if (!step.counted_in_tree) result.push({ ...step, trajectory: trajectory.trajectory })
    }
  }
  return result
})
const desktopTree = computed<TrajectoryTreeNode | null>(() => {
  if (!tree.value) return null
  const occurrences: TreeOccurrence[] = []
  for (const trajectory of tree.value.source_trajectories ?? []) {
    const first = trajectory.steps[0]
    if (first) {
      occurrences.push({
        trajectory: trajectory.trajectory,
        step: 0,
        excel_row: 0,
        image: first.image.replace(/[^\\/]+$/, 'initial_orch.jpg'),
        xml: first.xml.replace(/[^\\/]+$/, 'initial_orch_ui.xml'),
        action: { action: 'desktop' },
        action_text: '',
        summary: '轨迹起始桌面',
        actions_box: '',
        score: 5,
        reused: true,
        classification: null,
      })
    }
  }
  const first = occurrences[0]
  return {
    ...tree.value,
    label: '桌面',
    action: { action: 'desktop' },
    summary: '所有轨迹的共同起点：设备桌面',
    image: first?.image ?? '',
    xml: first?.xml ?? '',
    reference_trajectory: first?.trajectory ?? '',
    reference_step: 0,
    occurrence_count: occurrences.length,
    occurrences,
  }
})

function formatTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'medium' }).format(new Date(value))
}

async function loadRuns() {
  loadingRuns.value = true
  try {
    runs.value = await api.runs()
    const requested = typeof route.query.run === 'string' ? route.query.run : ''
    selectedRunId.value = runs.value.some((run) => run.run_id === requested) ? requested : runs.value[0]?.run_id || ''
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { loadingRuns.value = false }
}

async function loadTree() {
  if (!selectedRunId.value || !selectedTaskId.value) { tree.value = null; return }
  loadingTree.value = true
  try {
    tree.value = await api.tree(selectedRunId.value, selectedTaskId.value)
    selectedNode.value = desktopTree.value
    selectedOccurrenceIndex.value = 0
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { loadingTree.value = false }
}

function onNodeClick(node: TrajectoryTreeNode) {
  selectedNode.value = node
  selectedOccurrenceIndex.value = 0
}

function selectAuditStep(row: AuditStep & { trajectory: string }) {
  auditSelected.value = row
}

watch(selectedRunId, (runId) => {
  const run = runs.value.find((item) => item.run_id === runId)
  selectedTaskId.value = run?.tasks[0]?.task_id || ''
  void router.replace({ query: runId ? { run: runId } : {} })
})
watch(selectedTaskId, () => void loadTree())
onMounted(loadRuns)
</script>

<template>
  <div class="page quality-page">
    <header class="page-hero">
      <div>
        <span class="eyebrow">TRAJECTORY QUALITY</span>
        <h1>轨迹质检</h1>
        <p>从不可变任务集中选择一棵轨迹树，沿分支检查 action、截图和中间态审计证据。</p>
      </div>
      <div class="run-selectors">
        <label>任务集</label>
        <el-select v-model="selectedRunId" :loading="loadingRuns" placeholder="选择完成时间串" style="width: 230px">
          <el-option v-for="run in runs" :key="run.run_id" :label="`${run.run_id} · ${run.task_count} 任务`" :value="run.run_id" />
        </el-select>
        <label>任务</label>
        <el-select v-model="selectedTaskId" placeholder="选择任务" style="width: 230px">
          <el-option v-for="task in selectedRun?.tasks || []" :key="task.task_id" :label="task.task_id" :value="task.task_id" />
        </el-select>
      </div>
    </header>

    <el-empty v-if="!loadingRuns && !runs.length" description="还没有成功发布的任务集，请先到轨迹采集页提交建树">
      <router-link to="/collection"><el-button type="primary">前往轨迹采集</el-button></router-link>
    </el-empty>

    <template v-else-if="selectedRun && selectedTask">
      <section class="run-banner">
        <div><span>任务目标</span><b>{{ selectedTask.goal }}</b></div>
        <p>{{ formatTime(selectedRun.completed_at) }} · {{ selectedRun.model_name }}</p>
      </section>
      <section class="stat-grid">
        <div><span>原始步骤</span><b>{{ tree?.original_step_count ?? selectedTask.original_step_count }}</b></div>
        <div><span>入树步骤</span><b>{{ tree?.tree_step_count ?? selectedTask.tree_step_count }}</b></div>
        <div><span>忽略步骤</span><b>{{ tree?.ignored_incidental_step_count ?? selectedTask.ignored_step_count }}</b></div>
        <div><span>Action 节点</span><b>{{ selectedTask.action_node_count }}</b></div>
        <el-button :icon="DataLine" @click="auditVisible = true">中间态审计（{{ ignoredSteps.length }}）</el-button>
      </section>

      <section v-loading="loadingTree" class="tree-workspace">
        <div class="tree-canvas">
          <ReferenceTrajectoryTree
            v-if="desktopTree"
            :root="desktopTree"
            :selected-id="selectedNode?.id"
            @select="onNodeClick"
          />
        </div>
        <aside class="node-panel">
          <el-empty v-if="!selectedNode" description="点击一个节点查看详情" :image-size="76" />
          <template v-else>
            <div class="node-panel__header">
              <div><span>NODE {{ selectedNode.id }} · DEPTH {{ selectedNode.depth }}</span><h2>{{ selectedNode.label }}</h2></div>
              <el-tag type="success" round>{{ selectedNode.occurrence_count }} occurrences</el-tag>
            </div>
            <p class="node-summary">{{ selectedOccurrence?.summary || selectedNode.summary }}</p>
            <el-select v-if="selectedNode.occurrences.length" v-model="selectedOccurrenceIndex" style="width: 100%">
              <el-option
                v-for="(occurrence, index) in selectedNode.occurrences"
                :key="`${occurrence.trajectory}-${occurrence.step}`"
                :label="`${occurrence.trajectory} · step ${occurrence.step}${occurrence.reused ? ' · reused' : ''}`"
                :value="index"
              />
            </el-select>
            <ActionImage
              v-if="selectedOccurrence"
              class="node-image"
              :image-url="imageUrl(selectedOccurrence.image)"
              :action="selectedOccurrence.action || selectedNode.action"
              :actions-box="selectedOccurrence.actions_box || selectedNode.actions_box"
              :show-overlay="selectedNode.id !== 0"
              color-tone="bright"
            />
            <div class="node-data">
              <template v-if="selectedNode.id !== 0">
                <label>ACTION</label><pre>{{ JSON.stringify(selectedOccurrence?.action || selectedNode.action, null, 2) }}</pre>
              </template>
              <template v-else>
                <label>STATE</label><p>设备桌面 · 所有轨迹从这里开始</p>
              </template>
              <label>BBOX</label><code>{{ selectedOccurrence?.actions_box || selectedNode.actions_box || '—' }}</code>
              <template v-if="selectedOccurrence?.classification">
                <label>CLASSIFICATION</label>
                <p>{{ selectedOccurrence.classification.category }} · {{ selectedOccurrence.classification.confidence }}</p>
              </template>
            </div>
          </template>
        </aside>
      </section>
    </template>

    <el-drawer v-model="auditVisible" title="中间态审计" size="72%">
      <div class="audit-layout">
        <el-table :data="ignoredSteps" height="calc(100vh - 150px)" highlight-current-row @row-click="selectAuditStep">
          <el-table-column prop="trajectory" label="轨迹" min-width="170" />
          <el-table-column prop="step" label="Step" width="70" />
          <el-table-column prop="classification.category" label="类别" width="150" />
          <el-table-column prop="classification.confidence" label="置信度" width="90" />
          <el-table-column prop="summary" label="Action Summary" min-width="220" show-overflow-tooltip />
          <el-table-column label="查看" width="74"><template #default><el-button :icon="View" circle /></template></el-table-column>
        </el-table>
        <div v-if="auditSelected" class="audit-detail">
          <h3>{{ auditSelected.trajectory }} · step {{ auditSelected.step }}</h3>
          <ActionImage :image-url="imageUrl(auditSelected.image)" :action="auditSelected.action" :actions-box="auditSelected.actions_box" />
          <p>{{ auditSelected.classification?.reason }}</p>
          <pre>{{ JSON.stringify(auditSelected.action, null, 2) }}</pre>
        </div>
        <el-empty v-else description="选择一个忽略步骤查看证据" />
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.run-selectors { display: grid; grid-template-columns: auto 230px auto 230px; align-items: center; gap: 10px; }.run-selectors label { color: #94a3b8; font-size: 11px; font-weight: 900; letter-spacing: .1em; }
.run-banner { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin: 22px 0 14px; padding: 16px 20px; border: 1px solid #99f6e4; border-radius: 16px; background: linear-gradient(110deg,#f0fdfa,#ecfeff); }.run-banner div { display: grid; gap: 5px; }.run-banner span { color: #0f766e; font-size: 11px; font-weight: 900; letter-spacing: .12em; }.run-banner b { color: #134e4a; }.run-banner p { color: var(--muted); font-size: 12px; white-space: nowrap; }
.stat-grid { display: grid; grid-template-columns: repeat(4, minmax(110px, 1fr)) auto; gap: 12px; margin-bottom: 14px; }.stat-grid > div { padding: 13px 16px; border: 1px solid var(--line); border-radius: 13px; background: white; }.stat-grid span { display: block; color: var(--muted); font-size: 11px; }.stat-grid b { display: block; margin-top: 4px; color: var(--ink); font-size: 23px; }.stat-grid .el-button { height: 100%; }
.tree-workspace { display: grid; grid-template-columns: minmax(0, 1fr) 320px; height: calc(100vh - 315px); min-height: 610px; overflow: hidden; border: 1px solid var(--line); border-radius: 18px; background: white; }
.tree-canvas { position: relative; min-width: 0; overflow: hidden; background: #0b1220; }
.node-panel { overflow: auto; padding: 16px; border-left: 1px solid #263750; background: #111827; color: #dce7f5; }.node-panel__header { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }.node-panel__header span { color: #8297b1; font-size: 10px; font-weight: 900; letter-spacing: .1em; }.node-panel__header h2 { margin: 4px 0 0; }.node-summary { color: #9fb1c8; line-height: 1.55; }.node-image { margin: 12px 0; }.node-image :deep(.action-image img) { max-height: 390px; }.node-data { display: grid; gap: 8px; }.node-data label { margin-top: 7px; color: #8297b1; font-size: 10px; font-weight: 900; letter-spacing: .12em; }.node-data pre, .audit-detail pre { margin: 0; padding: 10px; overflow: auto; border: 1px solid #263750; border-radius: 8px; background: #09101d; color: #a7f3d0; font-size: 10px; }.node-data code { color: #67b7ff; overflow-wrap: anywhere; }
:deep(.node-panel .el-select__wrapper) { background: #172338; box-shadow: 0 0 0 1px #30435e inset; }
:deep(.node-panel .el-select__selected-item) { color: #dce7f5; }
.audit-layout { display: grid; grid-template-columns: minmax(480px,1fr) minmax(310px,.72fr); gap: 20px; }.audit-detail { min-width: 0; }.audit-detail h3 { margin-top: 0; }.audit-detail p { color: #475569; line-height: 1.6; }
@media (max-width: 1200px) { .tree-workspace { grid-template-columns: 1fr; height: auto; }.tree-canvas { height: 650px; }.node-panel { border-top: 1px solid var(--line); border-left: 0; }.stat-grid { grid-template-columns: repeat(2,1fr); }.run-selectors { grid-template-columns: auto 1fr; } }
</style>
