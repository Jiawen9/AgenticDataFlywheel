<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArrowLeft, DocumentChecked, Edit, RefreshLeft, RefreshRight, Search } from '@element-plus/icons-vue'
import { api } from '@/api'
import type { TaskGenerationTree, TaskGenerationTreeNode } from '@/types'
import ScenarioEditor from '@/components/ScenarioEditor.vue'
import ScenarioTreeHome from '@/components/ScenarioTreeHome.vue'
import type { ScenarioEditorState, ScenarioStudioMode } from '@/utils/scenarioStudio'
import { sceneIdForNode, scenarioStudioInitialNodeId, scenarioStudioMode } from '@/utils/scenarioStudio'
import { findNode, leaves, NODE_LEVELS, nodePath, normalizeTree, searchNodes } from '@/utils/scenarioTree'
import { onBeforeRouteLeave, useRoute } from 'vue-router'

interface EditorHandle {
  beginEdit: () => void
  undo: () => void
  redo: () => void
  save: () => Promise<void> | undefined
  discardChanges: () => Promise<boolean>
  focusNode: (target: string | TaskGenerationTreeNode) => void
}

const route = useRoute()
const tree = ref<TaskGenerationTreeNode[]>([])
const version = ref('')
const loading = ref(false)
const failure = ref('')
const viewMode = ref<ScenarioStudioMode>(scenarioStudioMode(route.query))
const editorInitialNodeId = ref(scenarioStudioInitialNodeId(route.query))
const searchText = ref('')
const editor = ref<EditorHandle | null>(null)
const editorState = ref<ScenarioEditorState>({ editing: false, dirty: false, saving: false, canUndo: false, canRedo: false, selectedSceneId: '' })

const sceneNodes = computed(() => tree.value.filter(node => node.kind === 'scene'))
const currentScene = computed(() => findNode(tree.value, editorState.value.selectedSceneId || resolveSceneId(editorInitialNodeId.value)) || sceneNodes.value[0])
const searchResults = computed(() => searchNodes(tree.value, searchText.value).slice(0, 40))
const treeMeta = computed(() => `${sceneNodes.value.length} 个场景 · ${leaves(tree.value).length} 个任务类型`)

function resolveSceneId(nodeId: string): string {
  if (!nodeId) return ''
  const sceneId = sceneIdForNode(tree.value, nodeId)
  if (sceneId) return sceneId
  const node = findNode(tree.value, nodeId)
  return node?.kind === 'scene' ? node.id : ''
}

async function refresh() {
  loading.value = true
  failure.value = ''
  try {
    const payload = await api.taskGenerationTree()
    tree.value = normalizeTree(payload.scenes)
    version.value = payload.version
  } catch (error) {
    failure.value = (error as Error).message
  } finally {
    loading.value = false
  }
}

async function enterEditor(nodeId = '') {
  const requestedId = nodeId || sceneNodes.value[0]?.id || ''
  const targetSceneId = resolveSceneId(requestedId) || sceneNodes.value[0]?.id || ''
  const targetId = findNode(tree.value, requestedId) ? requestedId : targetSceneId
  // An empty saved tree must still be able to open the existing editor.
  if ((!targetId || !targetSceneId) && sceneNodes.value.length) return
  editorInitialNodeId.value = targetId
  searchText.value = ''
  viewMode.value = 'editor'
  await nextTick()
  if (targetId) editor.value?.focusNode(targetId)
}

async function focusSearchResult(node: TaskGenerationTreeNode) {
  await enterEditor(node.id)
  editor.value?.focusNode(node.id)
}

async function discardEditorChanges() {
  if (editorState.value.saving) return false
  return editor.value ? editor.value.discardChanges() : true
}

async function backToTree() {
  if (!await discardEditorChanges()) return
  searchText.value = ''
  viewMode.value = 'treeHome'
  editorInitialNodeId.value = ''
}

async function cancelEditing() {
  await discardEditorChanges()
}

function beginEditing() { editor.value?.beginEdit() }
function undo() { editor.value?.undo() }
function redo() { editor.value?.redo() }
function save() { void editor.value?.save() }
function updateEditorState(state: ScenarioEditorState) { editorState.value = state }
function handleTreeSaved(payload: TaskGenerationTree) {
  tree.value = normalizeTree(payload.scenes)
  version.value = payload.version
}

function beforeUnload(event: BeforeUnloadEvent) {
  if (editorState.value.dirty || editorState.value.saving) {
    event.preventDefault()
    event.returnValue = ''
  }
}

onBeforeRouteLeave(async () => {
  if (editorState.value.saving) return false
  return discardEditorChanges()
})
onMounted(() => {
  document.body.classList.add('scenario-studio-responsive')
  void refresh()
  window.addEventListener('beforeunload', beforeUnload)
})
onBeforeUnmount(() => {
  document.body.classList.remove('scenario-studio-responsive')
  window.removeEventListener('beforeunload', beforeUnload)
})
</script>

<template>
  <div class="page scenario-studio-page" :class="{ 'is-tree-home': viewMode === 'treeHome' }">
    <section class="studio-shell">
      <header class="studio-shell-header">
        <div class="studio-shell-title">
          <span class="eyebrow">SCENARIO STUDIO</span>
          <h1>GUI操控场景树</h1>
          <div v-if="viewMode === 'editor'" class="studio-context">
            <button type="button" @click="backToTree"><el-icon><ArrowLeft /></el-icon>能力树</button>
            <span>›</span>
            <strong>{{ currentScene?.label || '场景编辑器' }}</strong>
          </div>
        </div>
        <div class="studio-shell-actions">
          <span v-if="viewMode === 'editor'" class="studio-meta">{{ treeMeta }}</span>
          <el-input v-model="searchText" class="studio-search" clearable :prefix-icon="Search" placeholder="搜索场景、能力或 App" @keydown.esc="searchText = ''" />
          <template v-if="viewMode === 'editor'">
            <el-button text :icon="RefreshLeft" :disabled="!editorState.editing || !editorState.canUndo" @click="undo">Undo</el-button>
            <el-button text :icon="RefreshRight" :disabled="!editorState.editing || !editorState.canRedo" @click="redo">Redo</el-button>
            <el-tag :type="editorState.dirty ? 'warning' : 'success'"><span class="saved-state">{{ editorState.dirty ? '● 有未保存修改' : '✓ 已保存' }}</span></el-tag>
            <el-button v-if="!editorState.editing" :icon="Edit" @click="beginEditing">编辑</el-button>
            <template v-else>
              <el-button :icon="ArrowLeft" @click="cancelEditing">取消编辑</el-button>
              <el-button type="primary" :icon="DocumentChecked" :disabled="!editorState.dirty || editorState.saving" @click="save">保存</el-button>
            </template>
          </template>
        </div>
        <div v-if="searchText.trim()" class="search-results-popover">
          <div class="search-results-head"><strong>搜索结果</strong><span>{{ searchResults.length }} 项</span></div>
          <button v-for="node in searchResults" :key="node.id" type="button" class="search-result" @click="focusSearchResult(node)">
            <span class="search-result-title"><b>{{ node.label }}</b><small>{{ NODE_LEVELS[node.kind] }}</small></span>
            <span>{{ nodePath(tree, node.id).join(' › ') }}</span>
          </button>
          <div v-if="!searchResults.length" class="search-no-result">没有找到匹配节点</div>
        </div>
      </header>

      <div v-if="viewMode === 'editor' && failure" class="studio-load-error">
        <strong>场景树暂时无法加载</strong><span>{{ failure }}</span><el-button text type="primary" @click="refresh">重试</el-button>
      </div>

      <Transition name="studio-mode" mode="out-in">
        <ScenarioTreeHome v-if="viewMode === 'treeHome'" key="tree-home" :tree="tree" :loading="loading" :error="failure" @open-editor="enterEditor" @retry="refresh" />
        <ScenarioEditor v-else-if="viewMode === 'editor' && !loading && !failure" key="editor" ref="editor" :tree="tree" :version="version" :initial-node-id="editorInitialNodeId" @state-change="updateEditorState" @tree-saved="handleTreeSaved" />
        <div v-else key="studio-loading" class="studio-editor-placeholder" v-loading="loading"><span v-if="!loading">无法加载场景树</span></div>
      </Transition>
    </section>
  </div>
</template>

<style scoped>
.scenario-studio-page.is-tree-home { background: #faf9f5; }
.is-tree-home .studio-shell { border: 0; border-radius: 9px; background: #faf9f5; box-shadow: none; }
.is-tree-home .studio-shell-header { align-items: center; padding: 20px 26px; border-bottom-color: #d9dbd1; }
.is-tree-home .studio-shell-title h1 { color: #2d3832; }
.is-tree-home .studio-shell-title .eyebrow { color: #496c55; letter-spacing: .23em; }
.is-tree-home .studio-search :deep(.el-input__wrapper) { background: #fcfbf8; box-shadow: 0 0 0 1px #d2d8ca inset; }
@media (max-width: 780px) {
  .is-tree-home .studio-shell-header { align-items: flex-start; padding: 18px 16px; }
}
:global(body.scenario-studio-responsive){min-width:0}
.scenario-studio-page{width:min(1800px,100%);min-height:100vh;margin:0 auto;padding-top:20px;background:radial-gradient(circle at 94% 0%,rgba(20,184,166,.08),transparent 23%),radial-gradient(circle at 2% 28%,rgba(14,165,233,.05),transparent 20%)}.studio-shell{position:relative;overflow:visible;border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.8);box-shadow:0 12px 35px rgba(15,23,42,.035)}.studio-shell-header{position:relative;display:flex;align-items:flex-end;justify-content:space-between;gap:24px;padding:24px 26px 18px;border-bottom:1px solid var(--line)}.studio-shell-title h1{margin:6px 0 0;color:#182535;font-size:23px;letter-spacing:-.035em}.studio-context{display:flex;align-items:center;gap:7px;margin-top:13px;color:#94a3b8;font-size:11px}.studio-context button{display:inline-flex;align-items:center;gap:3px;padding:3px 5px;border:0;border-radius:5px;background:transparent;color:#64748b;font-size:11px;cursor:pointer}.studio-context button:hover{background:#f1f5f9;color:#0f766e}.studio-context strong{color:#334155}.studio-shell-actions{display:flex;align-items:center;justify-content:flex-end;gap:4px;flex-wrap:wrap}.studio-meta{margin-right:7px;color:#94a3b8;font-size:10px;white-space:nowrap}.studio-search{width:235px}.saved-state{font-size:11px}.search-results-popover{position:absolute;top:83px;right:26px;z-index:20;width:min(420px,calc(100% - 52px));max-height:410px;overflow:auto;padding:9px;border:1px solid #cfdde1;border-radius:12px;background:#fff;box-shadow:0 18px 40px rgba(15,23,42,.14)}.search-results-head{display:flex;justify-content:space-between;padding:6px 8px 9px;color:#334155;font-size:12px}.search-results-head span{color:#94a3b8;font-size:10px}.search-result{display:grid;gap:4px;width:100%;padding:9px 8px;border:0;border-radius:7px;background:transparent;color:#64748b;text-align:left;cursor:pointer}.search-result:hover{background:#f8fafc}.search-result>span:last-child{overflow:hidden;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.search-result-title{display:flex;align-items:center;gap:7px;color:#334155}.search-result-title small{color:#0f766e;font-size:9px;font-weight:900}.search-no-result{padding:20px;color:#94a3b8;font-size:11px;text-align:center}.studio-load-error{display:flex;align-items:center;gap:12px;margin:16px 22px 0;padding:12px 15px;border:1px solid #fecdd3;border-radius:10px;background:#fff1f2}.studio-load-error strong{color:#9f1239;font-size:12px}.studio-load-error span{flex:1;color:#be123c;font-size:11px}.studio-editor-placeholder{min-height:420px}.studio-mode-enter-active,.studio-mode-leave-active{transition:opacity .2s ease,transform .2s ease}.studio-mode-enter-from,.studio-mode-leave-to{opacity:0;transform:translateY(7px)}@media(max-width:1180px){.studio-shell-header{align-items:flex-start;flex-direction:column}.studio-shell-actions{justify-content:flex-start}.studio-meta{display:none}.search-results-popover{top:151px;right:26px}}@media(max-width:780px){.scenario-studio-page{padding:12px 0 22px}.studio-shell-header{padding:20px 16px 16px}.studio-shell-title h1{font-size:20px}.studio-shell-actions{width:100%;align-items:stretch}.studio-search{width:100%}.studio-shell-actions :deep(.el-button){margin-left:0}.studio-load-error{margin-inline:16px;align-items:flex-start;flex-wrap:wrap}.studio-load-error span{min-width:100%}.search-results-popover{right:16px;width:calc(100% - 32px)}}
</style>
