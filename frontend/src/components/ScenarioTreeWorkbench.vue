<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { ElMessage, ElMessageBox, ElTree } from 'element-plus'
import { Download, Edit, Plus, Refresh, Upload } from '@element-plus/icons-vue'
import { api, sceneTreeDownloadUrl } from '@/api'
import type { KnowledgeBaseSummary, TaskGenerationJob, TaskGenerationTree, TaskGenerationTreeNode } from '@/types'
import { appConfigs, editableTree, executionUnitCount, findNode, leaves, nodePath, removeNode, selectionsFor, withInlineAddActions, type ScenarioTreeDisplayNode, type TreeAddActionNode } from '@/utils/scenarioTree'

const emit = defineEmits<{ created: [job: TaskGenerationJob]; countChange: [count: number] }>()
const treeRef = ref<InstanceType<typeof ElTree>>()
const tree = ref<TaskGenerationTreeNode[]>([])
const draft = ref<TaskGenerationTreeNode[]>([])
const version = ref('')
const knowledgeBases = ref<KnowledgeBaseSummary[]>([])
const warnings = ref<string[]>([])
const focusedId = ref('')
const checkedIds = ref<string[]>([])
const selectedApps = ref<Record<string, string[]>>({})
const filterText = ref('')
const editing = ref(false)
const loading = ref(false)
const saving = ref(false)
const submitting = ref(false)
const failure = ref('')
const generateN = ref(5)
const realTree = computed(() => editing.value ? draft.value : tree.value)
const displayTree = computed<ScenarioTreeDisplayNode[]>(() => editing.value ? withInlineAddActions(draft.value) : tree.value)
const focused = computed(() => findNode(realTree.value, focusedId.value))
const dirty = computed(() => editing.value && JSON.stringify(editableTree(draft.value)) !== JSON.stringify(editableTree(tree.value)))
const allLeaves = computed(() => leaves(tree.value))
const selectedLeaves = computed(() => allLeaves.value.filter(node => checkedIds.value.includes(node.id)))
const selections = computed(() => selectionsFor(tree.value, checkedIds.value, selectedApps.value))
const unitCount = computed(() => executionUnitCount(selections.value))
const appOptions = computed(() => [...new Set(leaves(realTree.value).flatMap(node => (node.app_configs || []).map(config => config.app)))])
const ready = computed(() => version.value && !editing.value && !loading.value && knowledgeBases.value.every(item => item.valid) && unitCount.value > 0 && selections.value.every(item => item.apps.length > 0))
const kbNames = { scene_tree: '场景树', control_prior: '操控先验', resource_prior: '资源先验' }
const kindNames = { scene: '场景', capability: '一级能力', sub_capability: '任务类型' }

function applyTree(payload: TaskGenerationTree) {
  tree.value = payload.scenes
  version.value = payload.version
  warnings.value = payload.warnings
  checkedIds.value = []
  selectedApps.value = {}
  treeRef.value?.setCheckedKeys([])
  if (!findNode(tree.value, focusedId.value)) focusedId.value = tree.value[0]?.id || ''
  emit('countChange', payload.leaf_count)
  void nextTick(() => treeRef.value?.filter(filterText.value))
}

async function refresh() {
  if (editing.value) return
  loading.value = true
  failure.value = ''
  try {
    knowledgeBases.value = await api.taskGenerationKnowledgeBases()
    applyTree(await api.taskGenerationTree())
  } catch (error) { failure.value = (error as Error).message }
  finally { loading.value = false }
}

async function replaceKnowledgeBase(kind: KnowledgeBaseSummary['kind'], event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file || editing.value) return
  try {
    await ElMessageBox.confirm(`上传将替换当前${kbNames[kind]}并发布新版本，旧版本和历史作业将保留。确定继续吗？`, '替换知识库', { type: 'warning' })
    loading.value = true
    await api.replaceTaskGenerationKnowledgeBase(kind, file, version.value || undefined)
    await refresh()
    ElMessage.success('知识库新版本已发布')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error((error as Error).message)
  } finally { input.value = ''; loading.value = false }
}

function beginEdit() {
  draft.value = JSON.parse(JSON.stringify(tree.value))
  editing.value = true
  failure.value = ''
  void nextTick(() => treeRef.value?.filter(filterText.value))
}

async function discardAllowed(): Promise<boolean> {
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm('有尚未保存的场景树修改，确定放弃吗？', '未保存的修改', { type: 'warning', confirmButtonText: '放弃修改', cancelButtonText: '继续编辑' })
    return true
  } catch { return false }
}

async function cancelEdit() {
  if (!(await discardAllowed())) return
  editing.value = false
  draft.value = []
  failure.value = ''
  await nextTick()
  treeRef.value?.setCheckedKeys(checkedIds.value)
  treeRef.value?.filter(filterText.value)
}

async function save() {
  saving.value = true
  failure.value = ''
  try {
    const payload = await api.saveTaskGenerationTree(editableTree(draft.value), version.value)
    editing.value = false
    draft.value = []
    applyTree(payload)
    knowledgeBases.value = await api.taskGenerationKnowledgeBases()
    ElMessage.success('场景树已保存，新作业将使用此版本')
  } catch (error) {
    failure.value = (error as Error).message
    ElMessage.error(failure.value)
  } finally { saving.value = false }
}

function addNode(parent?: TaskGenerationTreeNode) {
  const kind = !parent ? 'scene' : parent.kind === 'scene' ? 'capability' : 'sub_capability'
  const siblings = parent ? (parent.children ||= []) : draft.value
  const base = `新${kindNames[kind]}`
  let label = base
  let suffix = 2
  while (siblings.some(node => node.label === label)) label = `${base}${suffix++}`
  const node: TaskGenerationTreeNode = { id: crypto.randomUUID(), kind, label, ...(kind === 'sub_capability' ? { app_configs: [] } : { children: [] }) }
  siblings.push(node)
  filterText.value = ''
  focusedId.value = node.id
  void nextTick(() => {
    treeRef.value?.filter('')
    if (parent) { const parentNode = treeRef.value?.getNode(parent.id); if (parentNode) parentNode.expanded = true }
    treeRef.value?.setCurrentKey(node.id)
  })
}

function isAddAction(node: ScenarioTreeDisplayNode): node is TreeAddActionNode {
  return node.kind === 'add_action'
}

function activateAddAction(node: TreeAddActionNode) {
  const parent = findNode(draft.value, node.parent_id)
  if (parent) addNode(parent)
}

function handleTreeNodeClick(node: ScenarioTreeDisplayNode) {
  if (isAddAction(node)) {
    activateAddAction(node)
    return
  }
  focusedId.value = node.id
}

async function deleteFocused() {
  const node = focused.value
  if (!node) return
  const count = leaves([node]).length
  try {
    await ElMessageBox.confirm(`删除“${node.label}”将移除 ${count} 个任务类型。保存后影响未来作业，历史任务和先验记录不会删除。`, '删除节点', { type: 'warning' })
    removeNode(draft.value, node.id)
    focusedId.value = ''
  } catch { /* Confirmation cancelled. */ }
}

function setApps(apps: string[]) {
  if (focused.value) focused.value.app_configs = appConfigs(apps, focused.value.app_configs || [])
}

function filterNode(value: string, data: object) {
  const node = data as ScenarioTreeDisplayNode
  if (!value.trim()) return true
  if (isAddAction(node)) return false
  const realNode = findNode(realTree.value, node.id)
  if (!realNode) return false
  const text = [...nodePath(realTree.value, realNode.id), ...leaves([realNode]).flatMap(leaf => [leaf.label, ...(leaf.app_configs || []).map(config => config.app)])].join(' ').toLowerCase()
  return text.includes(value.trim().toLowerCase())
}

function checkable(data: object) {
  const node = data as TaskGenerationTreeNode
  return !leaves([node]).some(leaf => leaf.app_configs?.length)
}

function onCheck(_node: object, info: { checkedNodes: object[] }) {
  const selected = (info.checkedNodes as TaskGenerationTreeNode[]).filter(node => node.kind === 'sub_capability' && node.app_configs?.length)
  const next: Record<string, string[]> = {}
  for (const node of selected) next[node.id] = selectedApps.value[node.id] ?? node.app_configs!.map(config => config.app)
  selectedApps.value = next
  checkedIds.value = selected.map(node => node.id)
}

async function submit() {
  if (!ready.value) return
  submitting.value = true
  try {
    emit('created', await api.createTaskGeneration(selections.value, generateN.value, version.value))
    ElMessage.success('任务生成作业已提交')
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { submitting.value = false }
}

function beforeUnload(event: BeforeUnloadEvent) {
  if (dirty.value) { event.preventDefault(); event.returnValue = '' }
}
onBeforeRouteLeave(() => saving.value || submitting.value ? false : discardAllowed())
onMounted(() => { void refresh(); window.addEventListener('beforeunload', beforeUnload) })
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))
</script>

<template>
  <section class="kb-card">
    <div class="section-heading"><div><span class="eyebrow">KNOWLEDGE BASE</span><h2>知识库与版本</h2></div><div class="toolbar"><el-tag v-if="version" type="info">版本 {{ version.slice(0, 8) }}</el-tag><el-button :icon="Refresh" :disabled="editing || loading" @click="refresh">刷新</el-button><a v-if="version" :href="sceneTreeDownloadUrl()" download class="download-link"><el-icon><Download /></el-icon>下载已保存场景树</a></div></div>
    <div class="kb-grid"><article v-for="item in knowledgeBases" :key="item.kind"><div><el-tag :type="item.valid ? 'success' : 'danger'" size="small">{{ item.valid ? '可用' : '未就绪' }}</el-tag><strong>{{ kbNames[item.kind] }}</strong></div><p>{{ item.filename }} · {{ item.rows ?? 0 }} 行</p><small v-if="item.error">{{ item.error }}</small><label class="upload" :class="{ disabled: editing || loading }"><el-icon><Upload /></el-icon>替换 Excel<input type="file" accept=".xlsx,.xlsm" :disabled="editing || loading" @change="replaceKnowledgeBase(item.kind, $event)" /></label></article></div>
  </section>
  <el-alert v-if="failure" :title="failure" type="error" :closable="false" show-icon class="notice" />
  <el-alert v-for="warning in warnings" :key="warning" :title="warning" type="warning" :closable="false" class="notice" />
  <section class="workbench" v-loading="loading || saving">
    <div class="section-heading"><div><span class="eyebrow">SCENARIO COVERAGE</span><h2>场景能力 · 任务类型</h2><p>场景 → 一级能力 → 任务类型。点击查看详情，勾选用于生成。</p></div><div class="toolbar"><template v-if="editing"><el-tag :type="dirty ? 'warning' : 'info'">{{ dirty ? '有未保存修改' : '编辑模式' }}</el-tag><el-button @click="addNode()" :icon="Plus">新增场景</el-button><el-button @click="cancelEdit">取消</el-button><el-button type="primary" :disabled="!dirty" @click="save">保存场景树</el-button></template><el-button v-else :icon="Edit" :disabled="!version || submitting" @click="beginEdit">编辑场景树</el-button></div></div>
    <el-alert v-if="editing" title="编辑模式：保存后才影响新作业。修改期间暂停提交生成；先验状态在保存后刷新。" type="info" :closable="false" class="notice" />
    <div class="editor-layout">
      <div class="tree-pane">
        <el-input v-model="filterText" clearable placeholder="搜索场景、能力、任务类型或 App" @input="treeRef?.filter(filterText)" />
        <el-tree ref="treeRef" :key="editing ? 'edit' : 'browse'" class="scenario-tree" :data="displayTree" node-key="id" :show-checkbox="!editing" highlight-current :expand-on-click-node="false" :filter-node-method="filterNode" :props="{ label: 'label', children: 'children', disabled: checkable }" :current-node-key="focusedId" @node-click="handleTreeNodeClick" @check="onCheck">
          <template #default="{ data }"><button v-if="isAddAction(data)" type="button" class="tree-add-action" :aria-label="data.label" @click.stop="activateAddAction(data)"><el-icon><Plus /></el-icon><span>{{ data.label.replace('＋ ', '') }}</span></button><span v-else class="tree-label">{{ data.label }}<small v-if="data.kind === 'sub_capability'">{{ data.app_configs?.length ? `${data.app_configs.length} 个 App` : '未配置 App' }}</small></span></template>
        </el-tree>
        <el-empty v-if="!displayTree.length" description="暂无场景，可进入编辑模式新增" :image-size="70" />
      </div>
      <div class="detail-pane">
        <template v-if="focused">
          <div class="detail-title"><el-tag>{{ kindNames[focused.kind] }}</el-tag><span>{{ nodePath(realTree, focused.id).join(' / ') }}</span></div>
          <template v-if="editing">
            <label class="field-label">{{ kindNames[focused.kind] }}名称</label><el-input v-model="focused.label" maxlength="200" placeholder="请输入节点名称" aria-label="节点名称" />
            <div class="node-actions"><el-button v-if="focused.kind !== 'sub_capability'" :icon="Plus" @click="addNode(focused)">{{ focused.kind === 'scene' ? '新增一级能力' : '新增任务类型' }}</el-button><el-button type="danger" plain @click="deleteFocused">删除此节点</el-button></div>
          </template>
          <h3 v-else>{{ focused.label }}</h3>
          <template v-if="focused.kind === 'sub_capability'">
            <label class="field-label">适用 App</label>
            <el-select v-if="editing" :model-value="(focused.app_configs || []).map(config => config.app)" multiple filterable allow-create default-first-option placeholder="选择或输入 App 名称后按回车" aria-label="适用 App" @update:model-value="setApps"><el-option v-for="app in appOptions" :key="app" :value="app" :label="app" /></el-select>
            <p v-if="!focused.app_configs?.length" class="empty-note">尚未配置 App，此任务类型暂不可生成。</p>
            <article v-for="config in focused.app_configs" :key="config.app" class="app-config">
              <div class="app-heading"><strong>{{ config.app }}</strong><el-tag :type="config.control_prior_available ? 'success' : 'warning'" size="small">{{ config.control_prior_available ? '操控先验已匹配' : '缺少操控先验 · 仍可生成' }}</el-tag></div>
              <div class="resource-row"><label>使用资源先验</label><el-switch v-model="config.use_resource_prior" :disabled="!editing" :aria-label="`${config.app} 使用资源先验`" /><small>资源 {{ config.resource_count ?? 0 }} 条</small></div>
              <small v-if="config.use_resource_prior && !config.resource_count" class="warning">该 App 暂无资源数据，生成时将使用空资源先验。</small>
              <label class="field-label">参考示例</label><el-input v-if="editing" v-model="config.reference_example" type="textarea" :rows="3" maxlength="20000" :aria-label="`${config.app} 参考示例`" placeholder="填写该 App 下的任务参考示例（可选）" /><p v-else class="example">{{ config.reference_example || '未配置参考示例' }}</p>
            </article>
          </template>
          <p v-else class="empty-note">包含 {{ leaves([focused]).length }} 个任务类型。展开子节点查看适用 App 和生成配置。</p>
        </template>
        <el-empty v-else description="点击左侧节点查看或编辑配置" :image-size="80" />
      </div>
    </div>
  </section>
  <section class="generation-card">
    <div class="section-heading"><div><span class="eyebrow">GENERATION CONTROL</span><h2>生成范围</h2><p>按任务类型选择 App；每个选中 App 独立生成指定数量。</p></div><el-tag>已选 {{ selectedLeaves.length }} / {{ allLeaves.length }} 个任务类型</el-tag></div>
    <div class="generation-layout">
      <div class="selected-types"><p v-if="!selectedLeaves.length" class="empty-note">在上方勾选任务类型或整个场景，默认选中其全部适用 App。</p><article v-for="node in selectedLeaves" :key="node.id"><strong>{{ nodePath(tree, node.id).join(' / ') }}</strong><el-checkbox-group v-model="selectedApps[node.id]" :disabled="editing || submitting"><el-checkbox v-for="config in node.app_configs" :key="config.app" :value="config.app">{{ config.app }}</el-checkbox></el-checkbox-group><small v-if="!selectedApps[node.id]?.length" class="warning">至少选择一个 App，或在树中取消此任务类型。</small></article></div>
      <aside class="submit-panel"><label class="field-label">每个任务类型 / App 生成数量</label><el-input-number v-model="generateN" :min="1" :max="20" :disabled="editing || submitting" /><div class="estimate"><strong>{{ unitCount * generateN }}</strong><span>条预计主任务</span></div><p>{{ selectedLeaves.length }} 个任务类型 · {{ unitCount }} 个类型/App 执行单元 × {{ generateN }} 条<br />弱依赖前置任务另计。</p><el-button type="primary" size="large" :disabled="!ready" :loading="submitting" @click="submit">提交任务生成</el-button><small v-if="editing" class="warning">请先保存或取消场景树编辑。</small></aside>
    </div>
  </section>
</template>

<style scoped>
.kb-card,.workbench,.generation-card{margin-top:20px;padding:22px;border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.86);box-shadow:0 12px 35px rgba(15,23,42,.04)}
.section-heading,.toolbar,.app-heading,.detail-title,.resource-row{display:flex;align-items:center;gap:12px}.section-heading{justify-content:space-between;flex-wrap:wrap;margin-bottom:18px}.section-heading h2{font-size:21px;margin:5px 0}.section-heading p,.submit-panel p{font-size:12px;color:var(--muted);line-height:1.7;margin:5px 0 0}.toolbar{flex-wrap:wrap}.kb-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.kb-grid article{padding:14px;border:1px solid var(--line);border-radius:12px;background:#fff}.kb-grid article>div{display:flex;align-items:center;gap:8px}.kb-grid p{font-size:12px;color:var(--muted)}.kb-grid small{display:block;color:#b91c1c}.upload,.download-link{display:inline-flex;gap:5px;align-items:center;font-size:12px;color:var(--accent-deep);cursor:pointer;text-decoration:none}.upload input{display:none}.upload.disabled{opacity:.45;cursor:not-allowed}.notice{margin:12px 0}.editor-layout{display:grid;grid-template-columns:minmax(290px,.9fr) minmax(0,1.1fr);min-height:380px}.tree-pane{padding-right:20px;border-right:1px solid var(--line);min-width:0}.scenario-tree{margin-top:14px;max-height:600px;overflow:auto}.tree-label{display:flex;align-items:center;gap:10px;font-size:13px}.tree-label small{font-size:11px;color:#94a3b8}.tree-add-action{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border:1px solid var(--line);border-radius:5px;background:transparent;color:var(--accent-deep);font-size:12px;line-height:1.5;cursor:pointer}.tree-add-action:hover{border-color:var(--accent);background:transparent;color:var(--accent-deep)}.detail-pane{padding-left:24px;min-width:0;max-height:665px;overflow:auto}.detail-title{font-size:12px;color:var(--muted);flex-wrap:wrap;margin-bottom:16px}.detail-pane h3{font-size:22px;margin:8px 0 20px}.field-label{display:block;font-size:12px;font-weight:700;margin:12px 0 8px;color:var(--muted)}.node-actions{display:flex;gap:8px;margin:12px 0 22px}.app-config{border:1px solid var(--line);border-radius:12px;padding:15px;margin-top:12px;background:#fff}.app-heading{justify-content:space-between;flex-wrap:wrap}.resource-row{margin-top:12px;font-size:12px}.resource-row small{color:var(--muted)}.example{white-space:pre-wrap;font-size:13px;line-height:1.8;margin:4px 0;overflow-wrap:anywhere}.empty-note{font-size:13px;color:var(--muted);line-height:1.8}.warning{color:#b45309;font-size:11px}.generation-layout{display:grid;grid-template-columns:minmax(0,1fr) 285px;gap:24px}.selected-types{max-height:390px;overflow:auto}.selected-types article{padding:13px 0;border-bottom:1px solid var(--line)}.selected-types strong{font-size:13px}.submit-panel{padding:0 0 0 22px;border-left:1px solid var(--line);display:flex;align-items:flex-start;flex-direction:column;gap:10px}.submit-panel .field-label{margin-top:0}.submit-panel .el-button{width:100%;margin-top:6px}.estimate{display:flex;gap:10px;align-items:baseline}.estimate strong{font-size:34px;line-height:1.1;color:var(--accent-deep)}.estimate span{font-size:12px;color:var(--muted)}
@media(max-width:1050px){.kb-grid{grid-template-columns:1fr}.editor-layout{grid-template-columns:1fr}.tree-pane{border-right:0;padding:0 0 18px}.scenario-tree{max-height:360px}.detail-pane{border-top:1px solid var(--line);padding:20px 0 0;max-height:none}.generation-layout{grid-template-columns:1fr}.submit-panel{border-left:0;border-top:1px solid var(--line);padding:20px 0 0}.section-heading{gap:12px}.selected-types{max-height:300px}}
</style>
