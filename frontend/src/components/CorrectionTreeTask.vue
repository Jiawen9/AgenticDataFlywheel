<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { readPageApi } from '@/readPageApi'
import type { useCorrectionWorkspace } from '@/composables/useCorrectionWorkspace'
import type { CorrectionRow, CorrectionTaskItem, TrajectoryTreeNode } from '@/types'
import { correctionNodeDecision, findTargetRow, firstTreeTarget, nodeForRow, targetsForGroup, type CorrectionTaskTree, type CorrectionTreeTarget } from '@/utils/correctionTree'
import CorrectionWorkbench from './CorrectionWorkbench.vue'
import ReferenceTrajectoryTree from './ReferenceTrajectoryTree.vue'

const props = defineProps<{
  task: CorrectionTaskItem
  workspace: ReturnType<typeof useCorrectionWorkspace>
  trajectoryId: string
  step?: number
  readOnly: boolean
  opd: boolean
  displayTrajectory: (id: string) => string
}>()
const ws = props.workspace
const { session, activeGroup, activeRow, actionRevision, actionDraft, loadingGroup, busy } = ws
const tree = shallowRef<CorrectionTaskTree | null>(null)
const loading = ref(true)
const treeError = ref('')
const currentGroupId = ref('')
const selectedNodeId = ref<number>()
const focusNodeId = ref<number>()
const selectedStep = ref<number>()
const pendingNodeId = ref<number>()
const panelOpen = ref(false)
const listOpen = ref(false)
const notice = ref('')
const transition = ref(false)
const closeButton = ref<HTMLButtonElement | null>(null)
const canvas = ref<HTMLElement | null>(null)
let loadId = 0
let returnFocus: HTMLElement | SVGElement | undefined

const trajectories = computed(() => props.task.trajectories)
const trajectory = computed(() => trajectories.value.find(item => item.group.group_id === currentGroupId.value))
const previousStepIndex = computed(() => activeGroup.value?.rows.findIndex(row => row.excel_row === activeRow.value?.excel_row) ?? -1)
const targetsAt = (id: number | undefined) => id === undefined ? [] : tree.value?.targets[String(id)] ?? []
const pendingTargets = computed(() => targetsAt(pendingNodeId.value).filter(target => !target.deleted))
const panelTargets = computed(() => targetsAt(selectedNodeId.value).filter(target => !target.deleted))
const editedNodes = computed(() => Object.fromEntries(Object.entries(tree.value?.targets ?? {}).map(([id, targets]) => [
  id, { total: targets.length, edited: targets.filter(target => target.edited && !target.deleted).length },
])))
const targetValue = (target: CorrectionTreeTarget) => target.step_key
const targetLabel = (target: CorrectionTreeTarget) =>
  `${props.displayTrajectory(target.trajectory_id)} · 第 ${target.step} 步${target.edited ? ' · 已修正' : ''}`

function locate(node: number | undefined, step: number | undefined) {
  selectedNodeId.value = node
  selectedStep.value = step
}

function updateSelectionFromRow(row: CorrectionRow | null, groupId: string) {
  currentGroupId.value = groupId
  locate(nodeForRow(tree.value, groupId, row), row?.step)
}

async function loadTree(initialize: boolean) {
  const current = session.value
  const request = ++loadId
  const taskId = props.task.task_id
  if (!current) return
  if (initialize || !tree.value) loading.value = true
  treeError.value = ''
  try {
    const result = await readPageApi.correctionTaskTree(current.session_id, taskId, current.storage_revision)
    if (request !== loadId || session.value !== current || props.task.task_id !== taskId) return
    if (result.session_id !== current.session_id || result.task_id !== taskId) throw new Error('未能读取此任务的轨迹树')
    if (current.storage_revision !== undefined && result.storage_revision < current.storage_revision) return
    tree.value = result
    if (!result.tree) {
      treeError.value = result.reason || '此任务没有可用的轨迹树'
      return
    }
    if (!initialize) return
    const preferred = trajectories.value.find(item => item.trajectory_id === props.trajectoryId) ?? trajectories.value[0]
    if (!preferred) return
    const groupId = preferred.group.group_id
    currentGroupId.value = groupId
    const candidates = targetsForGroup(result, groupId).filter(({ target }) => !target.deleted)
    const explicit = props.step === undefined ? undefined : candidates.filter(({ target }) => target.step === props.step)
    if (explicit?.length === 1) {
      const match = explicit[0]!
      locate(match.nodeId, match.target.step)
    } else {
      const initial = firstTreeTarget(result, groupId)
      if (props.step === undefined && initial && result.key_node_ids.includes(initial.nodeId)) {
        locate(initial.nodeId, initial.target.step)
      } else {
        await ws.loadGroup(groupId)
        if (request !== loadId || session.value !== current) return
        const requestedRows = activeGroup.value?.rows.filter(row => row.step === props.step && !row.deleted) ?? []
        const row = requestedRows.length === 1 ? requestedRows[0]! : activeRow.value
        updateSelectionFromRow(row, groupId)
      }
    }
    if (request === loadId) focusNodeId.value = selectedNodeId.value
  } catch (cause) {
    if (request === loadId && session.value === current) treeError.value = (cause as Error).message
  } finally {
    if (request === loadId) loading.value = false
  }
}

function rememberFocus() {
  const focused = document.activeElement
  if (focused instanceof HTMLElement || focused instanceof SVGElement) returnFocus = focused
}

async function showPanel() {
  if (panelOpen.value) return
  rememberFocus()
  panelOpen.value = true
  await nextTick()
  closeButton.value?.focus({ preventScroll: true })
}

async function openTarget(nodeId: number, target: CorrectionTreeTarget) {
  if (transition.value || busy.value) return
  transition.value = true
  try {
    if (!await ws.focusStep(target.group_id, group => findTargetRow(group, target))) return
    pendingNodeId.value = undefined
    currentGroupId.value = target.group_id
    locate(nodeId, target.step)
    notice.value = ''
    await showPanel()
  } finally { transition.value = false }
}

function selectNode(node: TrajectoryTreeNode) {
  if (transition.value || busy.value || loading.value) return
  const { target, choices } = correctionNodeDecision(targetsAt(node.id), currentGroupId.value)
  pendingNodeId.value = undefined
  notice.value = ''
  if (target) void openTarget(node.id, target)
  else if (choices.length) pendingNodeId.value = node.id
  else notice.value = node.id === tree.value?.tree?.id ? '桌面是轨迹起点。请选择一个动作。' : '此节点没有可修正步骤。可打开轨迹与步骤查看。'
}

function changeNodeTarget(stepKey: string, nodeId: number | undefined) {
  if (nodeId === undefined) return
  const target = targetsAt(nodeId).find(item => targetValue(item) === stepKey && !item.deleted)
  if (target) void openTarget(nodeId, target)
}

async function selectTrajectory(groupId: string, open = panelOpen.value, requestedStep?: number) {
  if (transition.value || busy.value || !groupId) return
  transition.value = true
  try {
    const preferred = tree.value && firstTreeTarget(tree.value, groupId)
    const selected = await ws.focusStep(groupId, group => {
      if (requestedStep !== undefined) {
        const matchingSteps = group.rows.filter(row => row.step === requestedStep && !row.deleted)
        if (matchingSteps.length === 1) return matchingSteps[0]
      }
      if (tree.value && preferred && tree.value.key_node_ids.includes(preferred.nodeId)) {
        const row = findTargetRow(group, preferred.target)
        if (row) return row
      }
      return group.rows.find(row => !row.deleted)
    })
    if (!selected) return
    pendingNodeId.value = undefined
    notice.value = ''
    updateSelectionFromRow(activeRow.value, groupId)
    if (open) await showPanel()
    else focusNodeId.value = selectedNodeId.value
  } finally { transition.value = false }
}

async function moveStep(direction: -1 | 1) {
  const row = activeGroup.value?.rows[previousStepIndex.value + direction]
  if (!row || transition.value || busy.value) return
  transition.value = true
  try {
    await ws.chooseRow(row)
    if (activeRow.value?.excel_row === row.excel_row) {
      pendingNodeId.value = undefined
      updateSelectionFromRow(activeRow.value, currentGroupId.value)
    }
  } finally { transition.value = false }
}

async function hidePanel() {
  panelOpen.value = false
  await nextTick()
  const selected = canvas.value?.querySelector<SVGElement>('.reference-node.selected')
  if (returnFocus?.isConnected && returnFocus !== document.body) returnFocus.focus({ preventScroll: true })
  else selected?.focus({ preventScroll: true })
}

async function closePanel() {
  if (!transition.value && !busy.value && await ws.prepareTransition()) await hidePanel()
}

async function saveAction(actions: string) {
  if (busy.value || transition.value) return
  if (await ws.saveAction(actions)) await hidePanel()
}

async function toggleList() {
  if (transition.value || !await ws.prepareTransition()) return
  if (panelOpen.value) await hidePanel()
  pendingNodeId.value = undefined
  listOpen.value = !listOpen.value
  if (!listOpen.value && activeGroup.value && trajectories.value.some(item => item.group.group_id === activeGroup.value?.group_id)) {
    updateSelectionFromRow(activeRow.value, activeGroup.value.group_id)
  }
}

watch(() => [session.value, props.trajectoryId, props.step] as const, () => {
  panelOpen.value = false
  listOpen.value = false
  pendingNodeId.value = undefined
  focusNodeId.value = undefined
  locate(undefined, undefined)
  void loadTree(true)
}, { immediate: true })
watch(ws.treeRefreshVersion, () => { if (tree.value?.tree) void loadTree(false) })
onBeforeUnmount(() => { ++loadId })
</script>

<template>
  <section class="correction-tree-task" aria-label="任务轨迹修正">
    <div v-if="tree?.tree" class="correction-tree-toolbar">
      <div class="correction-trajectory-controls">
        <el-select :model-value="currentGroupId" :disabled="busy || transition || loading" aria-label="查看轨迹" @change="selectTrajectory($event)">
          <el-option v-for="item in trajectories" :key="item.group.group_id" :label="`${displayTrajectory(item.trajectory_id)} · Top-${item.rank}`" :value="item.group.group_id" />
        </el-select>
        <el-button v-if="!listOpen && selectedStep !== undefined" :disabled="busy || transition" @click="selectTrajectory(currentGroupId, true, selectedStep)">打开第 {{ selectedStep }} 步</el-button>
        <el-button v-if="!listOpen && trajectory" plain :type="trajectory.group.export ? 'success' : 'default'"
          :disabled="readOnly || busy || transition || trajectory.group.pending_review" @click="ws.toggleExport(trajectory.group)">
          {{ trajectory.group.export ? '取消导出' : '加入导出' }}
        </el-button>
        <span v-if="opd && trajectory?.group.opd_cutoff_step != null" class="correction-cutoff">OPD 导出至第 {{ trajectory.group.opd_cutoff_step }} 步</span>
      </div>
      <el-button plain :disabled="busy || transition" @click="toggleList">{{ listOpen ? '返回轨迹树' : '轨迹与步骤' }}</el-button>
    </div>

    <p v-if="loading && !tree?.tree" class="correction-tree-status" role="status">正在加载本任务的轨迹树…</p>
    <div v-if="treeError" class="correction-tree-status">
      <span role="status">{{ treeError }}</span>
      <el-button link :disabled="loading || busy" @click="loadTree(!tree?.tree)">重新读取轨迹树</el-button>
    </div>

    <div v-if="tree?.tree" v-show="!listOpen" class="correction-tree-layout" :class="{ 'has-editor': panelOpen }">
      <div class="correction-tree-scene">
        <div v-if="pendingNodeId !== undefined" class="correction-node-choice">
          <label for="correction-select-occurrence">选择本次要修正的轨迹</label>
          <el-select id="correction-select-occurrence" :model-value="''" aria-label="选择本次要修正的轨迹" :disabled="busy || transition" @change="changeNodeTarget($event, pendingNodeId)">
            <el-option v-for="target in pendingTargets" :key="targetValue(target)" :label="targetLabel(target)" :value="targetValue(target)" />
          </el-select>
          <el-button text @click="pendingNodeId = undefined">取消</el-button>
        </div>
        <div v-if="notice" class="correction-tree-status" role="status">{{ notice }}</div>
        <div ref="canvas" class="correction-tree-canvas">
          <ReferenceTrajectoryTree :root="tree.tree" :selected-id="selectedNodeId" :focus-id="focusNodeId" :highlight-trajectory-id="trajectory?.trajectory_id" :node-scores="tree.node_scores" :key-node-ids="tree.key_node_ids" :edited-nodes="editedNodes" @select="selectNode">
            <template #hint>◆ 关键步骤　✓ 已修正　Q 为原始分支评分</template>
          </ReferenceTrajectoryTree>
        </div>
      </div>

      <aside v-if="panelOpen" class="correction-drawer" aria-label="修正动作" @keydown.esc.stop.prevent="closePanel">
        <header class="correction-drawer-head">
          <div class="correction-drawer-context">
            <el-select v-if="panelTargets.length > 1" :model-value="activeRow?.step_key" aria-label="节点对应轨迹" :disabled="busy || transition" @change="changeNodeTarget($event, selectedNodeId)">
              <el-option v-for="target in panelTargets" :key="targetValue(target)" :label="targetLabel(target)" :value="targetValue(target)" />
            </el-select>
            <b v-else-if="trajectory">{{ displayTrajectory(trajectory.trajectory_id) }}</b>
            <span v-if="activeRow && activeGroup">第 {{ activeRow.step }} 步 · {{ previousStepIndex + 1 }}/{{ activeGroup.rows.length }}</span>
          </div>
          <button ref="closeButton" class="correction-close" type="button" title="收起修正面板" aria-label="收起修正面板" :disabled="busy || transition" @click="closePanel">×</button>
        </header>
        <div class="correction-drawer-scroll">
          <div class="correction-step-nav">
            <el-button :disabled="busy || transition || loadingGroup || previousStepIndex <= 0" @click="moveStep(-1)">上一步</el-button>
            <el-button :disabled="busy || transition || loadingGroup || previousStepIndex < 0 || previousStepIndex >= (activeGroup?.rows.length ?? 0) - 1" @click="moveStep(1)">下一步</el-button>
          </div>
          <div v-if="loadingGroup" class="correction-tree-status" role="status">正在加载此步骤…</div>
          <div v-else-if="ws.groupError.value" class="correction-tree-status" role="status">{{ ws.groupError.value }}</div>
          <template v-else-if="activeGroup && session">
            <el-alert v-if="activeGroup.pending_review" type="warning" :closable="false" title="此轨迹有待复核的人工修改">
              <el-button text :disabled="busy || transition" @click="toggleList">打开轨迹与步骤复核</el-button>
            </el-alert>
            <CorrectionWorkbench editor-only :session-id="session.session_id" :group="activeGroup" :row="activeRow" :revision="actionRevision" :storage-revision="session.storage_revision" :saving="busy" :read-only="readOnly" @action="saveAction" @draft="actionDraft = $event" />
          </template>
        </div>
      </aside>
    </div>
    <slot v-if="listOpen || (!loading && !tree?.tree)" name="list" />
  </section>
</template>

<style scoped>
.correction-tree-task { display: grid; min-width: 0; gap: 10px; padding: 8px 0 10px; }
.correction-tree-toolbar, .correction-trajectory-controls, .correction-node-choice,
.correction-drawer-context, .correction-step-nav { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; min-width: 0; }
.correction-tree-toolbar, .correction-drawer-head { justify-content: space-between; }
.correction-tree-toolbar .el-select, .correction-node-choice .el-select { width: 208px; max-width: 100%; }
.correction-cutoff { color: #0f766e; font-size: 12px; font-weight: 700; }
.correction-tree-status { padding: 4px 10px; color: #475569; }
.correction-node-choice { padding: 8px 10px; background: #f0fdfa; color: #134e4a; border-radius: 8px; }
.correction-tree-layout { display: grid; gap: 12px; min-width: 0; }
.correction-tree-layout.has-editor { grid-template-columns: minmax(0, 1fr) minmax(420px, 44%); align-items: start; }
.correction-tree-scene { display: grid; gap: 6px; min-width: 0; }
.correction-tree-canvas { height: clamp(365px, 65vh, 700px); border: 1px solid #263649; border-radius: 12px; overflow: hidden; }
.correction-drawer { display: flex; flex-direction: column; min-width: 0; height: clamp(365px, 65vh, 700px); overflow: hidden; border: 1px solid #cbd5e1; border-radius: 12px; background: #fff; box-shadow: 0 10px 24px rgba(15, 23, 42, .12); animation: slide-in .2s ease-out; }
.correction-drawer-scroll { display: grid; gap: 10px; min-height: 0; padding: 10px; overflow: auto; }
.correction-drawer-head { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-bottom: 1px solid #e2e8f0; color: #1e293b; }
.correction-drawer-head .el-select { width: 218px; max-width: 100%; }
.correction-drawer-context span { font-size: 12px; color: #475569; }
.correction-close { border: 0; border-radius: 6px; background: #f1f5f9; color: #1e293b; width: 32px; height: 32px; font-size: 22px; line-height: 1; cursor: pointer; }
.correction-step-nav :deep(.el-button + .el-button), .correction-tree-toolbar :deep(.el-button + .el-button) { margin-left: 0; }
@keyframes slide-in { from { transform: translateX(12px); opacity: .3; } }
@media (max-width: 1120px) {
  :global(body:has(.correction-drawer)) { overflow: hidden; }
  .correction-tree-layout.has-editor { grid-template-columns: minmax(0, 1fr); }
  .correction-drawer { position: fixed; inset: 0; z-index: 1800; height: 100dvh; border: 0; border-radius: 0; }
  .correction-drawer-scroll { flex: 1; overscroll-behavior: contain; }
}
@media (prefers-reduced-motion: reduce) { .correction-drawer { animation: none; } }
</style>
