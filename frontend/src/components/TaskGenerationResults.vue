<script setup lang="ts">
import { computed, ref } from 'vue'
import type { TaskGenerationResult } from '@/types'
import { filterResultGroups, groupGenerationResults, resultCounts, resultPath, resultTypeKey, type ResultFilters } from '@/utils/taskGeneration'
import TaskGenerationResultRow from './TaskGenerationResultRow.vue'
const props = defineProps<{ results: TaskGenerationResult[]; editingId: string | null; text: string; busy: boolean; protect: () => Promise<boolean> }>()
const emit = defineEmits<{ edit: [row: TaskGenerationResult]; delete: [row: TaskGenerationResult]; save: []; cancel: []; 'update:text': [value: string]; export: [] }>()
const filters = ref<ResultFilters>({ app: '', type: '', dependency: '', showDeleted: false })
const expanded = ref<string[]>([])
const groups = computed(() => groupGenerationResults(props.results))
const visible = computed(() => filterResultGroups(groups.value, filters.value))
const counts = computed(() => resultCounts(props.results))
const apps = computed(() => [...new Set(groups.value.map(group => group.main.app))])
const types = computed(() => [...new Map(groups.value.map(group => [resultTypeKey(group.main), resultPath(group.main)])).entries()])
async function changeFilter<K extends keyof ResultFilters>(key: K, value: ResultFilters[K]) { if (await props.protect()) filters.value = { ...filters.value, [key]: value } }
async function toggleGroup(id: string) {
  if (expanded.value.includes(id)) { if (await props.protect()) expanded.value = expanded.value.filter(value => value !== id) }
  else expanded.value = [...expanded.value, id]
}
</script>
<template>
  <section class="generation-results">
    <header><div><h3>任务审核</h3><p>未删除：{{ counts.main }} 条主任务 · {{ counts.prerequisites }} 条前置任务<span v-if="counts.strong"> · {{ counts.strong }} 条强依赖</span></p></div><el-button type="primary" plain :disabled="busy || counts.main + counts.prerequisites === 0" @click="emit('export')">导出全部未删除结果</el-button></header>
    <p class="export-note">导出包含全部未删除任务及其前置任务，不受下方筛选影响；强依赖仍按原规则导出。</p>
    <div class="result-filters">
      <el-select :model-value="filters.app" clearable placeholder="全部 App" aria-label="筛选 App" :disabled="busy" @update:model-value="changeFilter('app', $event || '')"><el-option v-for="app in apps" :key="app" :label="app" :value="app" /></el-select>
      <el-select :model-value="filters.type" clearable filterable placeholder="全部任务类型" aria-label="筛选任务类型" :disabled="busy" @update:model-value="changeFilter('type', $event || '')"><el-option v-for="[key, label] in types" :key="key" :label="label" :value="key" /></el-select>
      <el-select :model-value="filters.dependency" clearable placeholder="全部依赖类型" aria-label="筛选依赖类型" :disabled="busy" @update:model-value="changeFilter('dependency', $event || '')"><el-option label="无依赖" value="zero" /><el-option label="弱依赖" value="weak" /><el-option label="强依赖" value="strong" /><el-option label="前置任务（历史未关联）" value="pre_node" /><el-option label="依赖判定异常" value="error" /></el-select>
      <el-checkbox :model-value="filters.showDeleted" :disabled="busy" @update:model-value="changeFilter('showDeleted', Boolean($event))">显示已删除</el-checkbox>
    </div>
    <p class="visible-count">当前显示 {{ visible.length }} 个任务组</p>
    <article v-for="group in visible" :key="group.main.result_id" class="result-group">
      <p v-if="group.orphan" class="orphan-note">历史前置任务未关联到主任务，保留独立展示。</p>
      <p v-else-if="group.main.pre_task_uuid && !group.prerequisites.length" class="orphan-note">关联前置任务缺失，导出时将由服务端校验。</p>
      <TaskGenerationResultRow :row="group.main" :editing-id="editingId" :text="text" :busy="busy" @edit="emit('edit', $event)" @delete="emit('delete', $event)" @save="emit('save')" @cancel="emit('cancel')" @update:text="emit('update:text', $event)" />
      <template v-if="group.prerequisites.length"><button class="pre-toggle" :aria-expanded="expanded.includes(group.main.result_id)" :disabled="busy" @click="toggleGroup(group.main.result_id)">{{ expanded.includes(group.main.result_id) ? '收起' : '展开' }} {{ group.prerequisites.length }} 条前置任务 <span>与主任务成组删除 / 恢复</span></button><div v-if="expanded.includes(group.main.result_id)" class="prerequisites"><TaskGenerationResultRow v-for="row in group.prerequisites" :key="row.result_id" :row="row" :editing-id="editingId" :text="text" :busy="busy" @edit="emit('edit', $event)" @delete="emit('delete', $event)" @save="emit('save')" @cancel="emit('cancel')" @update:text="emit('update:text', $event)" /></div></template>
    </article>
    <el-empty v-if="!visible.length" description="当前筛选条件下没有任务" :image-size="65" />
  </section>
</template>
<style scoped>
header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:22px 0 8px}h3{font-size:15px;margin:0}header p,.export-note,.visible-count{color:var(--muted);font-size:12px;line-height:1.6;margin:6px 0}.export-note{margin-bottom:14px}.result-filters{display:flex;gap:8px;flex-wrap:wrap}.result-filters .el-select{width:160px}.result-filters .el-select:nth-child(2){width:240px}.visible-count{margin:12px 0}.result-group{border:1px solid var(--line);border-radius:9px;margin:10px 0;overflow:hidden;background:#fff}.pre-toggle{display:flex;align-items:center;gap:15px;width:100%;padding:10px 16px;border:0;border-top:1px solid var(--line);background:#fafbfc;color:var(--accent-deep);font-size:12px;cursor:pointer;text-align:left}.pre-toggle span{font-size:11px;color:var(--muted)}.prerequisites{margin:0 16px 12px;border-left:2px solid var(--line)}.orphan-note{margin:12px 16px 0;color:#9a6423;font-size:12px}@media(max-width:700px){.result-filters .el-select,.result-filters .el-select:nth-child(2){width:100%}.pre-toggle{flex-wrap:wrap;gap:4px}}
</style>
