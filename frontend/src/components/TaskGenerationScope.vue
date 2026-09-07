<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Search } from '@element-plus/icons-vue'
import type { TaskGenerationTreeNode } from '@/types'
import { appNodes, executionUnitCount, findNode, leaves, nodePath, selectionsFor } from '@/utils/scenarioTree'
import { generationCandidates, generationDirectory, selectCandidates, type AppSelection } from '@/utils/taskGeneration'

const props = defineProps<{ tree: TaskGenerationTreeNode[]; selected: AppSelection; count: number; disabled: boolean; submitting: boolean; ready: boolean }>()
const emit = defineEmits<{ 'update:selected': [value: AppSelection]; 'update:count': [value: number]; submit: [] }>()
const scope = ref('')
const query = ref('')
const directory = computed(() => generationDirectory(props.tree))
const candidates = computed(() => generationCandidates(props.tree, scope.value, query.value))
const chosen = computed(() => leaves(props.tree).filter(node => props.selected[node.id]?.length))
const units = computed(() => executionUnitCount(selectionsFor(props.tree, chosen.value.map(n => n.id), props.selected)))
const scopeLabel = computed(() => scope.value ? nodePath(props.tree, scope.value).join(' / ') : '全部场景')
watch(() => props.tree, () => { if (scope.value && !findNode(props.tree, scope.value)) scope.value = '' })
const names = (node: TaskGenerationTreeNode) => appNodes(node).map(app => app.app || app.label)
function setApps(node: TaskGenerationTreeNode, apps: string[]) { emit('update:selected', { ...props.selected, [node.id]: apps }) }
</script>

<template>
  <div class="generation-scope" :aria-busy="disabled">
    <section class="scope-directory">
      <h2>场景目录</h2>
      <button class="all-scenes" :class="{ active: !scope }" @click="scope = ''">全部场景</button>
      <el-tree :data="directory" :current-node-key="scope" node-key="id" highlight-current default-expand-all :expand-on-click-node="false" :props="{ label: 'label', children: 'children' }" @node-click="(node: TaskGenerationTreeNode) => scope = node.id">
        <template #default="{ data }"><span class="directory-label" :title="data.label"><small>{{ data.kind === 'scene' ? 'L1' : 'L2' }}</small>{{ data.label }}</span></template>
      </el-tree>
    </section>
    <section class="scope-candidates">
      <header><div><h2>任务类型与 App</h2><p>{{ scopeLabel }}</p></div><span class="muted">{{ candidates.length }} 个类型</span></header>
      <el-input v-model="query" clearable :prefix-icon="Search" placeholder="搜索场景、任务类型或 App" aria-label="搜索任务类型" />
      <div class="candidate-actions"><el-button text :disabled="disabled || !candidates.length" @click="emit('update:selected', selectCandidates(selected, candidates))">全选当前结果</el-button><span>筛选不改变已选范围</span></div>
      <div class="candidate-list">
        <article v-for="node in candidates" :key="node.id" class="candidate" :data-type-id="node.id">
          <el-checkbox :disabled="disabled || !names(node).length" :model-value="Boolean(names(node).length && selected[node.id]?.length === names(node).length)" :indeterminate="Boolean(selected[node.id]?.length && selected[node.id]!.length < names(node).length)" @update:model-value="setApps(node, $event ? names(node) : [])"><strong>{{ node.label }}</strong></el-checkbox>
          <p class="type-path">{{ nodePath(tree, node.id).slice(0, -1).join(' / ') }}</p>
          <el-checkbox-group :model-value="selected[node.id] || []" :disabled="disabled" @update:model-value="setApps(node, $event as string[])"><el-checkbox v-for="app in appNodes(node)" :key="app.id" :value="app.app || app.label">{{ app.app || app.label }}</el-checkbox></el-checkbox-group>
          <span v-if="!names(node).length" class="unavailable">未配置 App，暂不可生成</span>
        </article>
        <el-empty v-if="!candidates.length" :description="tree.length ? '当前范围没有匹配的任务类型' : '场景树暂无任务类型'" :image-size="60" />
      </div>
    </section>
    <aside class="scope-summary">
      <header><h2>已选范围 <small>{{ chosen.length }}</small></h2><el-button text :disabled="disabled || !chosen.length" @click="emit('update:selected', {})">清空</el-button></header>
      <div class="selected-list">
        <div v-for="node in chosen" :key="node.id" class="selected-type"><strong :title="nodePath(tree, node.id).join(' / ')">{{ node.label }}</strong><p>{{ nodePath(tree, node.id).slice(0, -1).join(' / ') }}</p><div><el-tag v-for="app in selected[node.id]" :key="app" :closable="!disabled" type="info" @close="setApps(node, (selected[node.id] || []).filter(name => name !== app))">{{ app }}</el-tag></div></div>
        <p v-if="!chosen.length" class="muted">从左侧选择场景，勾选任务类型及适用 App。</p>
      </div>
      <div class="submit-settings"><label for="generation-count">每个类型 / App 生成数量</label><el-input-number id="generation-count" :model-value="count" :min="1" :max="20" :precision="0" :disabled="disabled" @update:model-value="emit('update:count', $event ?? 5)" /><div class="estimate"><strong>{{ units * count }}</strong><span>条预计主任务</span></div><p>{{ chosen.length }} 个任务类型 · {{ units }} 个执行单元 × {{ count }} 条<br />弱依赖前置任务另计。</p><el-button type="primary" :loading="submitting" :disabled="!ready || disabled" @click="emit('submit')">提交任务生成</el-button></div>
    </aside>
  </div>
</template>

<style scoped>
.generation-scope{display:grid;grid-template-columns:220px minmax(0,1fr) 270px;gap:24px;align-items:start}.generation-scope>*{min-width:0}h2{font-size:15px;margin:0}h2 small{font-size:12px;color:var(--muted);margin-left:5px}header{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:15px}p{margin:6px 0;color:var(--muted);font-size:12px;line-height:1.6}.muted{font-size:12px;color:var(--muted)}.scope-directory{border-right:1px solid var(--line);padding-right:16px}.scope-directory h2{margin-bottom:18px}.all-scenes{display:block;width:100%;padding:10px 12px;text-align:left;border:0;border-radius:6px;background:transparent;cursor:pointer;color:var(--ink)}.all-scenes.active{background:var(--el-color-primary-light-9);color:var(--accent-deep)}.scope-directory :deep(.el-tree){background:transparent;max-height:560px;overflow:auto;margin-top:6px}.directory-label{display:flex;align-items:center;gap:7px;overflow:hidden;text-overflow:ellipsis;font-size:12px}.directory-label small{font-size:9px;color:var(--muted)}.candidate-actions{display:flex;align-items:center;justify-content:space-between;margin:7px 0;font-size:11px;color:var(--muted);gap:8px}.candidate-list{display:grid;gap:10px;max-height:580px;overflow:auto;padding-right:4px}.candidate{padding:13px 15px;border:1px solid var(--line);border-radius:9px;background:#fff}.candidate :deep(.el-checkbox){height:auto;min-height:28px;max-width:100%}.candidate :deep(.el-checkbox__label){white-space:normal;overflow-wrap:anywhere}.type-path{margin-left:24px}.candidate :deep(.el-checkbox-group){display:flex;flex-wrap:wrap;margin:7px 0 0 24px}.unavailable{display:block;margin:7px 0 0 24px;font-size:12px;color:#8a6324}.scope-summary{position:sticky;top:20px;padding:17px;border:1px solid var(--line);border-radius:10px;background:#fafbfc}.selected-list{max-height:260px;overflow:auto}.selected-type{padding:10px 0;border-bottom:1px solid var(--line)}.selected-type strong{font-size:13px;overflow-wrap:anywhere}.selected-type p{font-size:11px}.selected-type>div{display:flex;flex-wrap:wrap;gap:5px}.selected-type :deep(.el-tag){max-width:100%;height:auto;min-height:24px;white-space:normal;overflow-wrap:anywhere}.submit-settings{display:grid;gap:10px;margin-top:18px}.submit-settings label{font-size:12px;color:var(--muted)}.estimate{display:flex;align-items:baseline;gap:7px}.estimate strong{font-size:30px;color:var(--accent-deep);font-weight:650}.estimate span{font-size:12px;color:var(--muted)}@media(max-width:1200px){.generation-scope{grid-template-columns:190px minmax(0,1fr);gap:18px}.scope-summary{grid-column:1/-1;position:static;display:grid;grid-template-columns:1fr 250px;gap:0 24px}.scope-summary header{grid-column:1}.selected-list{grid-column:1;grid-row:2}.submit-settings{grid-column:2;grid-row:1/3;margin-top:0}}@media(max-width:700px){.generation-scope{grid-template-columns:1fr}.scope-directory{border-right:0;border-bottom:1px solid var(--line);padding:0 0 14px}.scope-directory :deep(.el-tree){max-height:180px}.scope-summary{display:block}.candidate-list{max-height:none;padding-right:0}.submit-settings{margin-top:16px}.candidate-actions{flex-wrap:wrap}}
</style>
