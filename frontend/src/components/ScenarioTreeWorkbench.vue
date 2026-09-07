<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { api } from '@/api'
import type { TaskGenerationTree, TaskGenerationTreeNode } from '@/types'
import ScenarioAppBindingPopover from '@/components/ScenarioAppBindingPopover.vue'
import ScenarioColumn, { type ScenarioNodeCommand } from '@/components/ScenarioColumn.vue'
import type { ScenarioEditorState } from '@/utils/scenarioStudio'
import { appNodes, canMove, descendantIds, editableTree, findNode, moveNode, NODE_LABELS, NODE_LEVELS, nodePath, normalizeTree, parentOf, removeNode, reorderNode } from '@/utils/scenarioTree'

type EditableKind = 'scene' | 'capability' | 'sub_capability'
interface DetailDraft {
  id: string
  kind: TaskGenerationTreeNode['kind']
  label: string
  description: string
  reference_example: string
  use_resource_prior: boolean
}

const props = defineProps<{
  initialTree: TaskGenerationTreeNode[]
  initialVersion: string
  initialNodeId?: string
}>()
const emit = defineEmits<{
  (event: 'tree-saved', payload: TaskGenerationTree): void
  (event: 'state-change', state: ScenarioEditorState): void
  (event: 'selection-change', sceneId: string): void
}>()

const savedTree = ref<TaskGenerationTreeNode[]>(normalizeTree(props.initialTree))
const draft = ref<TaskGenerationTreeNode[]>([])
const version = ref(props.initialVersion)
const selectedL1 = ref('')
const selectedL2 = ref('')
const selectedL3 = ref('')
const selectedApp = ref('')
const editing = ref(false)
const saving = ref(false)
const failure = ref('')
const addingKind = ref<TaskGenerationTreeNode['kind'] | ''>('')
const addingParentId = ref('')
const addText = ref('')
const renameId = ref('')
const renameText = ref('')
const draggedId = ref('')
const dropTargetId = ref('')
const undoStack = ref<string[]>([])
const redoStack = ref<string[]>([])
const detailVisible = ref(false)
const detailDraft = ref<DetailDraft | null>(null)
const moveVisible = ref(false)
const moveNodeId = ref('')
const moveTargetId = ref('')
const highlightId = ref('')
let highlightTimer: ReturnType<typeof setTimeout> | undefined

const realTree = computed(() => editing.value ? draft.value : savedTree.value)
const sceneNodes = computed(() => realTree.value.filter(node => node.kind === 'scene'))
const selectedScene = computed(() => findNode(realTree.value, selectedL1.value))
const selectedCapability = computed(() => selectedScene.value?.children?.find(node => node.id === selectedL2.value && node.kind === 'capability'))
const selectedSubCapability = computed(() => selectedCapability.value?.children?.find(node => node.id === selectedL3.value && node.kind === 'sub_capability'))
const selectedAppNode = computed(() => selectedSubCapability.value?.children?.find(node => node.id === selectedApp.value && node.kind === 'app'))
const capabilityNodes = computed(() => selectedScene.value?.children?.filter(node => node.kind === 'capability') || [])
const subCapabilityNodes = computed(() => selectedCapability.value?.children?.filter(node => node.kind === 'sub_capability') || [])
const selectedApps = computed(() => selectedSubCapability.value ? appNodes(selectedSubCapability.value).map(node => node.label) : [])
const dirty = computed(() => editing.value && JSON.stringify(editableTree(draft.value)) !== JSON.stringify(editableTree(savedTree.value)))
const activeNode = computed(() => selectedAppNode.value || selectedSubCapability.value || selectedCapability.value || selectedScene.value)
const pathNodes = computed(() => {
  const target = activeNode.value
  if (!target) return []
  const result: TaskGenerationTreeNode[] = []
  let current: TaskGenerationTreeNode | undefined = target
  while (current) {
    result.unshift(current)
    current = parentOf(realTree.value, current.id)
  }
  return result
})
const appLibrary = computed(() => {
  const byLabel = new Map<string, TaskGenerationTreeNode>()
  const collect = (nodes: TaskGenerationTreeNode[]) => nodes.forEach(node => {
    if (node.kind === 'sub_capability') {
      appNodes(node).forEach(app => {
        if (app.label && !byLabel.has(app.label)) byLabel.set(app.label, app)
      })
    }
    collect(node.children || [])
  })
  collect(savedTree.value)
  collect(draft.value)
  return [...byLabel.values()].sort((left, right) => left.label.localeCompare(right.label, 'zh-CN'))
})
const movingNode = computed(() => findNode(draft.value, moveNodeId.value))
const moveCandidates = computed(() => {
  const node = movingNode.value
  if (!node) return []
  const result: TaskGenerationTreeNode[] = []
  const visit = (nodes: TaskGenerationTreeNode[]) => nodes.forEach(item => {
    if (canMove(node, item)) result.push(item)
    visit(item.children || [])
  })
  visit(draft.value)
  return result
})
const detailTitle = computed(() => detailDraft.value ? `编辑${NODE_LABELS[detailDraft.value.kind]}` : '编辑节点详情')

function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T }
function snapshot() {
  if (editing.value) {
    undoStack.value.push(JSON.stringify(editableTree(draft.value)))
    redoStack.value = []
  }
}
function emitState() {
  emit('state-change', {
    editing: editing.value,
    dirty: dirty.value,
    saving: saving.value,
    canUndo: undoStack.value.length > 0,
    canRedo: redoStack.value.length > 0,
    selectedSceneId: selectedL1.value,
  })
}
function initializeFromProps() {
  savedTree.value = normalizeTree(props.initialTree)
  version.value = props.initialVersion
  syncSelection()
  if (props.initialNodeId) focusNode(props.initialNodeId)
  emitState()
}
function syncSelection() {
  const scene = sceneNodes.value.find(node => node.id === selectedL1.value) || sceneNodes.value[0]
  selectedL1.value = scene?.id || ''
  const capability = scene?.children?.find(node => node.id === selectedL2.value && node.kind === 'capability')
  selectedL2.value = capability?.id || ''
  const sub = capability?.children?.find(node => node.id === selectedL3.value && node.kind === 'sub_capability')
  selectedL3.value = sub?.id || ''
  const app = sub?.children?.find(node => node.id === selectedApp.value && node.kind === 'app')
  selectedApp.value = app?.id || ''
}
function selectNode(node: TaskGenerationTreeNode) {
  const chain: TaskGenerationTreeNode[] = []
  let current: TaskGenerationTreeNode | undefined = findNode(realTree.value, node.id)
  while (current) {
    chain.unshift(current)
    current = parentOf(realTree.value, current.id)
  }
  selectedL1.value = chain.find(item => item.kind === 'scene')?.id || ''
  selectedL2.value = chain.find(item => item.kind === 'capability')?.id || ''
  selectedL3.value = chain.find(item => item.kind === 'sub_capability')?.id || ''
  selectedApp.value = chain.find(item => item.kind === 'app')?.id || ''
}
function selectScene(node: TaskGenerationTreeNode) {
  selectedL1.value = node.id
  selectedL2.value = ''
  selectedL3.value = ''
  selectedApp.value = ''
}
function selectCapability(node: TaskGenerationTreeNode) {
  selectedL2.value = node.id
  selectedL3.value = ''
  selectedApp.value = ''
}
function selectSubCapability(node: TaskGenerationTreeNode) {
  selectedL3.value = node.id
  selectedApp.value = ''
}
function selectApp(node: TaskGenerationTreeNode) { selectedApp.value = node.id }

function openAppBinding() {
  if (!editing.value) beginEdit()
}

function bindApps(labels: string[]) {
  const target = selectedSubCapability.value
  if (!editing.value || !target) return
  const current = appNodes(target)
  const currentLabels = current.map(node => node.label)
  if (JSON.stringify(currentLabels) === JSON.stringify(labels)) return
  const next = labels.map(label => {
    const source = current.find(node => node.label === label) || appLibrary.value.find(node => node.label === label)
    if (source) {
      const app = clone(source)
      app.kind = 'app'
      app.label = label
      app.app = label
      app.children = undefined
      return app
    }
    return { id: crypto.randomUUID(), kind: 'app' as const, label, app: label, description: '', reference_example: '', use_resource_prior: false }
  })
  snapshot()
  target.children = next
  target.app_configs = undefined
  if (!labels.includes(selectedAppNode.value?.label || '')) selectedApp.value = ''
  syncSelection()
}

function restore(serialized: string) {
  draft.value = normalizeTree(JSON.parse(serialized) as TaskGenerationTreeNode[])
  syncSelection()
}
function undo() {
  const previous = undoStack.value.pop()
  if (!previous) return
  redoStack.value.push(JSON.stringify(editableTree(draft.value)))
  restore(previous)
}
function redo() {
  const next = redoStack.value.pop()
  if (!next) return
  undoStack.value.push(JSON.stringify(editableTree(draft.value)))
  restore(next)
}
function applyTree(payload: TaskGenerationTree) {
  savedTree.value = normalizeTree(payload.scenes)
  version.value = payload.version
  syncSelection()
}
function beginEdit() {
  draft.value = clone(savedTree.value)
  editing.value = true
  undoStack.value = []
  redoStack.value = []
  syncSelection()
}
async function discardChanges(): Promise<boolean> {
  if (dirty.value) {
    try {
      await ElMessageBox.confirm('有尚未保存的场景树修改，确定放弃吗？', '未保存的修改', { type: 'warning', confirmButtonText: '放弃修改', cancelButtonText: '继续编辑' })
    } catch { return false }
  }
  editing.value = false
  draft.value = []
  undoStack.value = []
  redoStack.value = []
  addingKind.value = ''
  renameId.value = ''
  detailVisible.value = false
  syncSelection()
  return true
}
async function save() {
  if (!dirty.value) return
  saving.value = true
  failure.value = ''
  try {
    const payload = await api.saveTaskGenerationTree(editableTree(draft.value), version.value)
    applyTree(payload)
    editing.value = false
    draft.value = []
    undoStack.value = []
    redoStack.value = []
    emit('tree-saved', payload)
    ElMessage.success('场景树已保存并发布新版本')
  } catch (error) {
    failure.value = (error as Error).message
    ElMessage.error(failure.value)
  } finally { saving.value = false }
}

function siblingNodes(parent: TaskGenerationTreeNode | undefined) {
  if (!parent) return draft.value
  return (parent.children ||= [])
}
function uniqueLabel(parent: TaskGenerationTreeNode | undefined, kind: TaskGenerationTreeNode['kind']) {
  const siblings = siblingNodes(parent)
  const base = kind === 'scene' ? '新一级场景' : kind === 'capability' ? '新能力' : '新子能力'
  let label = base
  let suffix = 2
  while (siblings.some(node => node.label === label)) label = `${base}${suffix++}`
  return label
}
function startAdd(parent: TaskGenerationTreeNode | undefined, kind: EditableKind) {
  if (!editing.value) return
  addingKind.value = kind
  addingParentId.value = parent?.id || ''
  addText.value = ''
  void nextTick(() => document.querySelector<HTMLInputElement>('.inline-creator-input, .scene-creator-input')?.focus())
}
function requestAdd(parent: TaskGenerationTreeNode | undefined, kind: EditableKind) {
  if (!editing.value) beginEdit()
  startAdd(parent, kind)
}
function commitAdd() {
  const kind = addingKind.value as EditableKind
  if (!kind || !editing.value) return
  const parent = addingParentId.value ? findNode(draft.value, addingParentId.value) : undefined
  const validParent = kind === 'scene'
    ? !parent
    : (kind === 'capability' && parent?.kind === 'scene')
      || (kind === 'sub_capability' && parent?.kind === 'capability')
  if (!validParent) { cancelAdd(); return }
  const requested = addText.value.trim()
  const siblings = siblingNodes(parent)
  if (requested && siblings.some(node => node.label === requested)) {
    ElMessage.warning('同级节点名称不能重复')
    return
  }
  snapshot()
  const label = requested || uniqueLabel(parent, kind)
  const node: TaskGenerationTreeNode = { id: crypto.randomUUID(), kind, label, description: '', children: [] }
  siblings.push(node)
  addingKind.value = ''
  addingParentId.value = ''
  addText.value = ''
  selectNode(node)
}
function cancelAdd() {
  addingKind.value = ''
  addingParentId.value = ''
  addText.value = ''
}
function beginRename(node: TaskGenerationTreeNode) {
  if (!editing.value) return
  renameId.value = node.id
  renameText.value = node.label
  void nextTick(() => {
    const input = [...document.querySelectorAll<HTMLInputElement>('.inline-rename-input')].find(item => item.value === node.label)
    input?.focus()
    input?.select()
  })
}
function commitRename(node: TaskGenerationTreeNode) {
  if (renameId.value !== node.id) return
  const requested = renameText.value.trim()
  if (!requested) { ElMessage.warning('名称不能为空'); return }
  const siblings = siblingNodes(parentOf(draft.value, node.id))
  if (siblings.some(item => item.id !== node.id && item.label === requested)) { ElMessage.warning('同级节点名称不能重复'); return }
  if (requested !== node.label) {
    snapshot()
    node.label = requested
    if (node.kind === 'app') node.app = requested
  }
  renameId.value = ''
  renameText.value = ''
}
function cancelRename() { renameId.value = ''; renameText.value = '' }

function openDetails(node: TaskGenerationTreeNode) {
  detailDraft.value = {
    id: node.id,
    kind: node.kind,
    label: node.label,
    description: node.description || '',
    reference_example: node.reference_example || '',
    use_resource_prior: Boolean(node.use_resource_prior),
  }
  detailVisible.value = true
}
function saveDetails() {
  const details = detailDraft.value
  const node = details ? findNode(draft.value, details.id) : undefined
  if (!details || !node || !editing.value) return
  const siblings = siblingNodes(parentOf(draft.value, node.id))
  const label = details.label.trim()
  if (!label) { ElMessage.warning('名称不能为空'); return }
  if (siblings.some(item => item.id !== node.id && item.label === label)) { ElMessage.warning('同级节点名称不能重复'); return }
  const changed = node.label !== label
    || (node.description || '') !== details.description
    || (node.kind === 'app' && ((node.reference_example || '') !== details.reference_example || Boolean(node.use_resource_prior) !== details.use_resource_prior))
  if (!changed) { detailVisible.value = false; return }
  snapshot()
  node.label = label
  node.description = details.description
  if (node.kind === 'app') {
    node.app = label
    node.reference_example = details.reference_example
    node.use_resource_prior = details.use_resource_prior
  }
  detailVisible.value = false
}

function handleCommand(node: TaskGenerationTreeNode, command: ScenarioNodeCommand) {
  if (command === 'details') { openDetails(node); return }
  if (!editing.value) return
  if (command === 'rename') beginRename(node)
  else if (command === 'add' && node.kind === 'scene') startAdd(node, 'capability')
  else if (command === 'add' && node.kind === 'capability') startAdd(node, 'sub_capability')
  else if (command === 'bind' && node.kind === 'sub_capability') selectSubCapability(node)
  else if (command === 'move') openMove(node)
  else if (command === 'delete' || command === 'unbind') void deleteNode(node)
}
function handleColumnCommand(payload: { node: TaskGenerationTreeNode; command: ScenarioNodeCommand }) {
  handleCommand(payload.node, payload.command)
}
function countByKind(node: TaskGenerationTreeNode, kind: TaskGenerationTreeNode['kind']): number {
  return (node.children || []).reduce((count, child) => count + (child.kind === kind ? 1 : 0) + countByKind(child, kind), 0)
}
async function deleteNode(node: TaskGenerationTreeNode) {
  if (!editing.value) return
  const descendantCount = descendantIds(node).length
  const message = node.kind === 'app'
    ? `取消绑定“${node.label}”？App 本身不会被删除。`
    : `删除“${node.label}”将移除 ${descendantCount} 个后代节点${node.kind === 'capability' ? `（${countByKind(node, 'sub_capability')} 个子能力、${countByKind(node, 'app')} 个 App 绑定）` : ''}。历史作业和先验记录不会删除。`
  try {
    await ElMessageBox.confirm(message, node.kind === 'app' ? '取消绑定 App' : '删除整棵子树', { type: 'warning', confirmButtonText: node.kind === 'app' ? '取消绑定' : '确认删除', cancelButtonText: '取消' })
  } catch { return }
  const parent = parentOf(draft.value, node.id)
  snapshot()
  removeNode(draft.value, node.id)
  if (parent) selectNode(parent)
  else syncSelection()
}
function openMove(node: TaskGenerationTreeNode) {
  if (!editing.value || node.kind === 'scene') return
  moveNodeId.value = node.id
  moveTargetId.value = ''
  moveVisible.value = true
}
function confirmMove() {
  if (!moveNodeId.value || !moveTargetId.value) return
  const node = findNode(draft.value, moveNodeId.value)
  if (!node) return
  snapshot()
  if (moveNode(draft.value, moveNodeId.value, moveTargetId.value)) {
    moveVisible.value = false
    selectNode(node)
    ElMessage.success('节点已移动，保存后生效')
  }
}

function sameParent(node: TaskGenerationTreeNode, target: TaskGenerationTreeNode) {
  const left = parentOf(draft.value, node.id)
  const right = parentOf(draft.value, target.id)
  return (left?.id || '') === (right?.id || '') && node.kind === target.kind
}
function handleDragStart(node: TaskGenerationTreeNode) { if (editing.value) draggedId.value = node.id }
function handleDragOver(node: TaskGenerationTreeNode) {
  const moving = findNode(draft.value, draggedId.value)
  dropTargetId.value = moving && (sameParent(moving, node) || canMove(moving, node)) ? node.id : ''
}
function handleDrop(node: TaskGenerationTreeNode) {
  const moving = findNode(draft.value, draggedId.value)
  if (!moving) return
  if (sameParent(moving, node)) {
    snapshot()
    reorderNode(draft.value, moving.id, node.id)
    selectNode(moving)
  } else if (canMove(moving, node)) {
    snapshot()
    moveNode(draft.value, moving.id, node.id)
    selectNode(moving)
  } else ElMessage.warning('无法移动到该位置，请保持 L1 → L2 → L3 → App 层级')
  draggedId.value = ''
  dropTargetId.value = ''
}

function focusNode(target: string | TaskGenerationTreeNode) {
  const node = typeof target === 'string' ? findNode(realTree.value, target) : target
  if (!node) return
  selectNode(node)
  highlightId.value = node.id
  if (highlightTimer) clearTimeout(highlightTimer)
  highlightTimer = setTimeout(() => { highlightId.value = '' }, 1300)
}
watch(() => props.initialTree, () => {
  if (!editing.value) initializeFromProps()
}, { deep: true })
watch(() => props.initialVersion, next => {
  if (!editing.value) version.value = next
})
watch(() => props.initialNodeId, next => {
  if (!editing.value && next) focusNode(next)
})
watch([editing, dirty, saving, selectedL1, undoStack, redoStack], emitState, { deep: true })
watch(selectedL1, sceneId => emit('selection-change', sceneId), { immediate: true })
onMounted(initializeFromProps)
onBeforeUnmount(() => {
  if (highlightTimer) clearTimeout(highlightTimer)
})

defineExpose({ beginEdit, undo, redo, save, discardChanges, focusNode })
</script>

<template>
  <section class="scenario-editor" v-loading="saving">
    <el-alert v-if="failure" :title="failure" type="error" :closable="false" show-icon />

    <nav class="scene-strip" aria-label="一级场景">
      <div class="scene-tabs">
        <div v-for="scene in sceneNodes" :key="scene.id" class="scene-tab-wrap">
          <button
            type="button"
            class="scene-tab"
            :class="{ active: selectedL1 === scene.id, 'search-hit': highlightId === scene.id }"
            :draggable="editing"
            @click="selectScene(scene)"
            @dblclick.stop="beginRename(scene)"
            @dragstart="handleDragStart(scene)"
            @dragover.prevent="handleDragOver(scene)"
            @drop.prevent="handleDrop(scene)"
          >
            <input v-if="renameId === scene.id" :value="renameText" class="scene-rename-input" autofocus @click.stop @input="renameText = ($event.target as HTMLInputElement).value" @keydown.enter.prevent="commitRename(scene)" @keydown.esc.prevent="cancelRename" @blur="commitRename(scene)" />
            <span v-else>{{ scene.label }}</span>
          </button>
          <el-dropdown v-if="editing" trigger="click" @command="handleCommand(scene, $event as ScenarioNodeCommand)">
            <button type="button" class="scene-more" aria-label="场景操作" @click.stop>···</button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="rename">重命名</el-dropdown-item>
                <el-dropdown-item command="details">编辑详情</el-dropdown-item>
                <el-dropdown-item command="add">添加能力</el-dropdown-item>
                <el-dropdown-item divided command="delete">删除整棵子树</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
        <div v-if="addingKind === 'scene'" class="scene-creator">
          <input :value="addText" class="scene-creator-input" autofocus placeholder="输入场景名称" maxlength="200" @input="addText = ($event.target as HTMLInputElement).value" @keydown.enter.prevent="commitAdd" @keydown.esc.prevent="cancelAdd" />
          <button type="button" @click="cancelAdd">×</button>
        </div>
        <button v-if="editing && addingKind !== 'scene'" type="button" class="scene-add" @click="startAdd(undefined, 'scene')"><el-icon><Plus /></el-icon>新增一级场景</button>
        <button v-if="editing && addingKind === 'scene'" type="button" class="scene-add scene-add--muted" @click="cancelAdd">取消</button>
      </div>
    </nav>

    <div class="path-bar">
      <span class="path-root">场景体系</span>
      <template v-for="(node, index) in pathNodes" :key="node.id">
        <span class="path-separator">›</span>
        <button type="button" :class="{ current: index === pathNodes.length - 1 }" @click="selectNode(node)">{{ node.label }}</button>
      </template>
      <span v-if="!pathNodes.length" class="path-placeholder">选择一个场景开始浏览</span>
    </div>

    <main class="column-browser">
      <ScenarioColumn
        title="能力"
        kind="capability"
        :nodes="capabilityNodes"
        :selected-id="selectedL2"
        :editing="editing"
        :adding-kind="addingKind"
        :adding-parent-id="addingParentId"
        :add-parent-id="selectedL1"
        :add-text="addText"
        :rename-id="renameId"
        :rename-text="renameText"
        :drop-target-id="dropTargetId"
        :highlight-id="highlightId"
        add-label="添加能力"
        empty-title="当前场景还没有能力"
        empty-description="添加第一个能力，开始搭建场景体系"
        :add-disabled="!selectedScene"
        @select="selectCapability"
        @command="handleColumnCommand"
        @start-add="requestAdd(selectedScene, 'capability')"
        @update-add-text="addText = $event"
        @commit-add="commitAdd"
        @cancel-add="cancelAdd"
        @start-rename="beginRename"
        @update-rename-text="renameText = $event"
        @commit-rename="commitRename"
        @cancel-rename="cancelRename"
        @drag-start="handleDragStart"
        @drag-over="handleDragOver"
        @drop="handleDrop"
      />
      <ScenarioColumn
        title="子能力"
        kind="sub_capability"
        :nodes="subCapabilityNodes"
        :selected-id="selectedL3"
        :editing="editing"
        :adding-kind="addingKind"
        :adding-parent-id="addingParentId"
        :add-parent-id="selectedL2"
        :add-text="addText"
        :rename-id="renameId"
        :rename-text="renameText"
        :drop-target-id="dropTargetId"
        :highlight-id="highlightId"
        add-label="添加子能力"
        empty-title="当前能力还没有子能力"
        empty-description="创建子能力，定义可生成的任务类型"
        :add-disabled="!selectedCapability"
        @select="selectSubCapability"
        @command="handleColumnCommand"
        @start-add="requestAdd(selectedCapability, 'sub_capability')"
        @update-add-text="addText = $event"
        @commit-add="commitAdd"
        @cancel-add="cancelAdd"
        @start-rename="beginRename"
        @update-rename-text="renameText = $event"
        @commit-rename="commitRename"
        @cancel-rename="cancelRename"
        @drag-start="handleDragStart"
        @drag-over="handleDragOver"
        @drop="handleDrop"
      />
      <ScenarioColumn
        title="App"
        kind="app"
        :nodes="selectedSubCapability ? appNodes(selectedSubCapability) : []"
        :selected-id="selectedApp"
        :editing="editing"
        :adding-kind="addingKind"
        :adding-parent-id="addingParentId"
        :add-parent-id="selectedL3"
        :add-text="addText"
        :rename-id="renameId"
        :rename-text="renameText"
        :drop-target-id="dropTargetId"
        :highlight-id="highlightId"
        add-label="新增 App"
        empty-title="当前子能力还没有 App"
        empty-description="新增或绑定 App 后，任务生成才会有执行范围"
        :show-footer-add="false"
        @select="selectApp"
        @command="handleColumnCommand"
        @start-rename="beginRename"
        @update-rename-text="renameText = $event"
        @commit-rename="commitRename"
        @cancel-rename="cancelRename"
        @drag-start="handleDragStart"
        @drag-over="handleDragOver"
        @drop="handleDrop"
      >
        <template #footer-extra>
          <ScenarioAppBindingPopover :apps="appLibrary" :selected-apps="selectedApps" :disabled="!selectedSubCapability" @open="openAppBinding" @confirm="bindApps" />
        </template>
      </ScenarioColumn>
    </main>

    <p v-if="!editing" class="studio-tip">进入编辑模式后可双击名称快速重命名；App 可在“新增 App”入口中绑定已有 App 或直接创建。</p>
  </section>

  <el-drawer v-model="detailVisible" :title="detailTitle" size="min(420px, 92vw)" destroy-on-close>
    <template v-if="detailDraft">
      <div class="detail-form">
        <div class="detail-path">{{ nodePath(realTree, detailDraft.id).join(' › ') }}</div>
        <label>名称</label>
        <el-input v-model="detailDraft.label" :disabled="!editing" maxlength="200" />
        <label>描述</label>
        <el-input v-model="detailDraft.description" :disabled="!editing" type="textarea" :rows="6" maxlength="20000" />
        <template v-if="detailDraft.kind === 'app'">
          <label>参考示例</label>
          <el-input v-model="detailDraft.reference_example" :disabled="!editing" type="textarea" :rows="6" maxlength="20000" />
          <div class="detail-switch"><span>使用资源先验</span><el-switch v-model="detailDraft.use_resource_prior" :disabled="!editing" /></div>
        </template>
      </div>
    </template>
    <template #footer>
      <el-button @click="detailVisible = false">取消</el-button>
      <el-button v-if="editing" type="primary" @click="saveDetails">完成</el-button>
    </template>
  </el-drawer>

  <el-dialog v-model="moveVisible" title="移动节点" width="min(520px, 92vw)">
    <p class="dialog-copy">选择合法的新父节点，节点身份和子树会保持不变。</p>
    <el-select v-model="moveTargetId" filterable placeholder="选择新父节点" style="width:100%">
      <el-option v-for="node in moveCandidates" :key="node.id" :value="node.id" :label="`${NODE_LEVELS[node.kind]} · ${nodePath(realTree, node.id).join(' / ')}`" />
    </el-select>
    <template #footer><el-button @click="moveVisible = false">取消</el-button><el-button type="primary" :disabled="!moveTargetId" @click="confirmMove">移动</el-button></template>
  </el-dialog>
</template>

<style scoped>
.scenario-editor{position:relative;margin-top:18px;padding:0 0 18px;border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.84);box-shadow:0 12px 35px rgba(15,23,42,.035);overflow:visible}.studio-header{position:relative;display:flex;align-items:flex-end;justify-content:space-between;gap:24px;padding:23px 24px 18px;border-bottom:1px solid var(--line)}.studio-heading h2{margin:5px 0 4px;color:#182535;font-size:22px;letter-spacing:-.025em}.studio-heading p{margin:0;color:var(--muted);font-size:12px}.studio-actions{display:flex;align-items:center;justify-content:flex-end;gap:4px;flex-wrap:wrap}.studio-meta{margin-right:7px;color:#94a3b8;font-size:10px;white-space:nowrap}.studio-search{width:235px}.saved-state{font-size:11px}.search-results-popover{position:absolute;z-index:10;top:78px;right:170px;width:min(420px,calc(100% - 48px));max-height:410px;overflow:auto;padding:9px;border:1px solid #cfdde1;border-radius:12px;background:#fff;box-shadow:0 18px 40px rgba(15,23,42,.14)}.search-results-head{display:flex;justify-content:space-between;padding:6px 8px 9px;color:#334155;font-size:12px}.search-results-head span{color:#94a3b8;font-size:10px}.search-result{display:grid;gap:4px;width:100%;padding:9px 8px;border:0;border-radius:7px;background:transparent;color:#64748b;text-align:left;cursor:pointer}.search-result:hover{background:#f8fafc}.search-result>span:last-child{overflow:hidden;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.search-result-title{display:flex;align-items:center;gap:7px;color:#334155}.search-result-title small{color:#0f766e;font-size:9px;font-weight:900}.search-no-result{padding:20px;color:#94a3b8;font-size:11px;text-align:center}.el-alert{margin:14px 20px 0}.scene-strip{padding:0 20px;border-bottom:1px solid var(--line)}.scene-tabs{display:flex;align-items:center;gap:3px;min-width:0;overflow-x:auto;scrollbar-width:thin}.scene-tab-wrap{display:flex;align-items:center;flex:0 0 auto}.scene-tab{position:relative;display:inline-flex;align-items:center;min-height:51px;padding:0 13px;border:0;border-bottom:2px solid transparent;background:transparent;color:#64748b;font-size:13px;cursor:pointer;white-space:nowrap}.scene-tab:hover{color:#334155;background:#fafdfd}.scene-tab.active{border-bottom-color:var(--accent);color:#1e293b;font-weight:800}.scene-tab.search-hit{animation:search-hit 1.2s ease}.scene-more{align-self:center;margin-left:-8px;padding:3px;border:0;background:transparent;color:#94a3b8;font-size:12px;cursor:pointer}.scene-more:hover{color:var(--accent-deep)}.scene-add{display:inline-flex;align-items:center;gap:4px;flex:0 0 auto;margin-left:7px;padding:6px 8px;border:0;border-radius:6px;background:transparent;color:var(--accent-deep);font-size:11px;cursor:pointer}.scene-add:hover{background:#f0fdfa}.scene-add--muted{color:#94a3b8}.scene-creator{display:flex;align-items:center;gap:3px;flex:0 0 auto;margin-left:7px;padding:5px 5px 5px 9px;border:1px solid #9bd8d0;border-radius:7px;background:#fff}.scene-creator-input{width:115px;border:0;outline:0;color:#334155;font-size:12px}.scene-creator button{border:0;background:transparent;color:#94a3b8;font-size:17px;cursor:pointer}.scene-rename-input{width:90px;border:0;border-bottom:1px solid var(--accent);outline:0;background:transparent;color:inherit;font:inherit}.path-bar{display:flex;align-items:center;gap:8px;min-height:53px;padding:0 24px;color:#64748b;font-size:12px;white-space:nowrap;overflow-x:auto}.path-root{color:#94a3b8;font-size:11px}.path-separator{color:#cbd5e1;font-size:17px}.path-bar button{padding:4px 5px;border:0;border-radius:5px;background:transparent;color:#64748b;font-size:12px;cursor:pointer}.path-bar button:hover{background:#f1f5f9;color:#334155}.path-bar button.current{color:#1e293b;font-weight:800}.path-placeholder{color:#c0cbd0;font-size:11px}.column-browser{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:0 18px;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#fbfdfd}.column-browser>.scenario-column+ .scenario-column{border-left:1px solid var(--line)}.studio-tip{margin:13px 24px 0;color:#94a3b8;font-size:10px}.detail-form{display:grid;gap:8px}.detail-path{margin-bottom:6px;padding-bottom:10px;border-bottom:1px solid var(--line);color:#94a3b8;font-size:11px;line-height:1.6}.detail-form label{margin-top:8px;color:#64748b;font-size:11px;font-weight:800}.detail-switch{display:flex;align-items:center;justify-content:space-between;margin-top:12px;color:#475569;font-size:12px}.dialog-copy{color:var(--muted);font-size:12px}@keyframes search-hit{0%,100%{box-shadow:none}35%{box-shadow:0 0 0 4px rgba(20,184,166,.2)}}@media(max-width:1180px){.studio-header{align-items:flex-start;flex-direction:column}.studio-actions{justify-content:flex-start}.search-results-popover{top:146px;right:24px}.studio-meta{display:none}}@media(max-width:780px){.studio-header{padding:19px 16px 15px}.studio-heading h2{font-size:19px}.studio-actions{width:100%;align-items:stretch}.studio-search{width:100%}.studio-actions :deep(.el-button){margin-left:0}.scene-strip{padding:0 12px}.path-bar{padding:0 16px}.column-browser{display:block;margin:0 12px}.column-browser>.scenario-column+ .scenario-column{border-top:1px solid var(--line);border-left:0}.scenario-column{min-height:320px}.column-list{min-height:190px}.search-results-popover{right:16px;width:calc(100% - 32px)}}
</style>
