<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import type { FactoryRun } from '@/phoneFactoryApi'
import type { RunStorageState } from '@/composables/useRunStorage'
import { storageBytes, storagePhase, storageOperationDone } from '@/composables/useRunStorage'
import { factoryRunId, newestFactoryRuns } from '@/utils/factoryHistory'
import { collectorDataLabel, collectorDataUnavailable, runDisplayStatus, runStatus } from '@/utils/phoneFactoryRuns'
import DisplayId from './DisplayId.vue'
import { useDisplayIdentifiers } from '@/composables/useDisplayIdentifiers'

const props = defineProps<{ runs: FactoryRun[]; state: RunStorageState; focusedRun?: string }>()
const { displayId, matches } = useDisplayIdentifiers()
const filters = reactive({ query: '', batch: '', page: 1, size: 10 })
const selectedIds = ref<string[]>([]), actionError = ref(''), confirming = ref(false), operationPage = ref(1)
const batches = computed(() => [...new Set(props.runs.map(run => run.batch_id).filter((id): id is string => !!id))])
const ordered = computed(() => newestFactoryRuns(props.runs))
const filtered = computed(() => ordered.value.filter(run => (!filters.batch || run.batch_id === filters.batch)
  && (matches(filters.query, 'run', factoryRunId(run), '', run.filename || '', runDisplayStatus(run)) || matches(filters.query, 'batch', run.batch_id))))
const pageRuns = computed(() => filtered.value.slice((filters.page - 1) * filters.size, filters.page * filters.size))
const selectedRuns = computed(() => props.runs.filter(run => selectedIds.value.includes(factoryRunId(run))))
const operationRows = computed(() => [...props.state.tasks].reverse())
const pageOperations = computed(() => operationRows.value.slice((operationPage.value - 1) * 10, operationPage.value * 10))
watch(() => [filters.query, filters.batch, filters.size], () => { filters.page = 1 }, { flush: 'sync' })
watch(() => filtered.value.length, count => { filters.page = Math.max(1, Math.min(filters.page, Math.ceil(count / filters.size))) }, { flush: 'sync' })
watch(() => props.focusedRun, id => {
  if (!id) return
  filters.query = ''; filters.batch = ''; selectedIds.value = [id]
  const index = ordered.value.findIndex(run => factoryRunId(run) === id)
  filters.page = Math.max(1, Math.floor(index / filters.size) + 1)
}, { immediate: true })
function toggle(id: string, checked: boolean) { selectedIds.value = checked ? [...new Set([...selectedIds.value, id])] : selectedIds.value.filter(value => value !== id) }
function selectBatch() { if (filters.batch) selectedIds.value = props.runs.filter(run => run.batch_id === filters.batch).map(factoryRunId) }
function selectPage() { selectedIds.value = [...new Set([...selectedIds.value, ...pageRuns.value.map(factoryRunId)])] }
function scan(runId?: string) { return props.state.start(runId ? [runId] : selectedRuns.value.map(factoryRunId), 'scan') }
async function clean(mode: 'retire' | 'cache') {
  const chosen = [...selectedRuns.value], ids = chosen.map(factoryRunId)
  if (!ids.length || confirming.value) return
  confirming.value = true; actionError.value = ''
  const dependencies = [...new Set(ids.flatMap(id => props.state.latest(id, 'scan')?.result?.dependent_run_ids || []))]
  const names = ids.slice(0, 6).map(id => displayId('run', id)).join('、') + (ids.length > 6 ? `等 ${ids.length} 个运行` : '')
  const detail = mode === 'retire'
    ? `将删除采集端 ${names} 的运行数据，包含所属续采分段、轨迹、截图、副本、过程日志及归档。系统会先停止所选运行及使用这些原件的评测任务，再释放文件并删除。${dependencies.length ? `已知受影响运行：${dependencies.map(id => displayId('run', id)).join('、')}。` : ''}平台已收到的数据、外部原件、最终报告和简要记录保留。清理后不能继续本轮或使用已删原件评测。`
    : `将清理 ${names} 的可再生成归档和临时缓存；保留原始轨迹及续采能力。`
  try {
    await ElMessageBox.confirm(detail, mode === 'retire' ? '清理采集端运行数据' : '清理可再生成缓存', { type: 'warning', confirmButtonText: mode === 'retire' ? '确认停止相关任务并清理' : '确认清理缓存', cancelButtonText: '取消' })
    await props.state.start(ids, 'cleanup', mode)
  } catch (cause) { if (cause !== 'cancel' && cause !== 'close') actionError.value = (cause as Error).message || String(cause) }
  finally { confirming.value = false }
}
const kindLabel = (kind: string, mode?: string | null) => kind === 'scan' ? '统计空间' : kind === 'archive' ? '生成完整归档' : mode === 'cache' ? '清理缓存' : '清理运行数据'
</script>

<template>
  <section class="run-storage" data-testid="run-storage">
    <p class="storage-hint">清理采集端 .http_runs 中的运行数据，平台已接收的数据保留。统计空间是可选操作，不必等待统计或回传完成才能清理。</p>
    <div class="storage-toolbar">
      <el-input v-model="filters.query" clearable placeholder="搜索运行、结果批次或文件名" aria-label="搜索存储运行" />
      <el-select v-model="filters.batch" clearable filterable placeholder="按结果批次筛选" aria-label="存储结果批次"><el-option v-for="id in batches" :key="id" :value="id" :label="displayId('batch', id)" /></el-select>
      <el-button :disabled="!filters.batch" @click="selectBatch">选择整个结果批次</el-button>
    </div>
    <div class="storage-toolbar">
      <el-button @click="selectPage">选择本页</el-button><el-button :disabled="!selectedIds.length" @click="selectedIds = []">清除选择</el-button><span>已选 {{ selectedRuns.length }} 个运行</span>
      <el-button :disabled="!selectedRuns.length" @click="scan()">统计空间</el-button>
      <el-button :disabled="!selectedRuns.length || confirming" @click="clean('cache')">清理缓存</el-button>
      <el-button type="danger" :loading="confirming" :disabled="!selectedRuns.length" data-testid="cleanup-selected-runs" @click="clean('retire')">清理所选运行数据</el-button>
    </div>
    <el-alert v-if="actionError" :title="actionError" type="error" :closable="false" />
    <el-table :data="pageRuns" :row-key="factoryRunId" border>
      <el-table-column width="48"><template #default="{ row }"><el-checkbox :model-value="selectedIds.includes(factoryRunId(row))" :aria-label="`选择运行 ${factoryRunId(row)}`" @update:model-value="toggle(factoryRunId(row), Boolean($event))" /></template></el-table-column>
      <el-table-column label="运行 / 结果批次" min-width="180"><template #default="{ row }"><DisplayId kind="run" :id="factoryRunId(row)" /><small><DisplayId kind="batch" :id="row.batch_id" /></small></template></el-table-column>
      <el-table-column label="执行 / 采集端数据" min-width="160"><template #default="{ row }">{{ runDisplayStatus(row) }}<small>{{ collectorDataLabel(row.collector_data_state) }}</small></template></el-table-column>
      <el-table-column label="最近统计" min-width="150"><template #default="{ row }">{{ storageBytes(state.latest(factoryRunId(row), 'scan')?.result?.summary?.bytes) }}<small v-if="state.latest(factoryRunId(row), 'scan')?.result?.updated_at">{{ state.latest(factoryRunId(row), 'scan')?.result?.updated_at?.slice(0, 19).replace('T', ' ') }}</small></template></el-table-column>
      <el-table-column label="完整归档" min-width="200"><template #default="{ row }"><span>{{ runStatus(row.archive_status || 'not_requested') }}</span><div><el-button link :disabled="collectorDataUnavailable(row) || state.pending(factoryRunId(row))" @click="state.start([factoryRunId(row)], 'archive')">生成完整归档</el-button><a v-if="row.archive_status === 'ready' && !collectorDataUnavailable(row)" :href="state.archiveDownloadUrl(factoryRunId(row))" :download="`${factoryRunId(row)}.zip`">下载 ZIP</a></div></template></el-table-column>
    </el-table>
    <el-pagination v-model:current-page="filters.page" v-model:page-size="filters.size" :page-sizes="[10, 20, 50]" :total="filtered.length" layout="total, sizes, prev, pager, next" />
    <h3>后台操作</h3><p class="storage-hint">关闭抽屉后后台继续执行。已受理请求在刷新后恢复查询；失败时重试同一操作，只处理剩余内容。</p>
    <el-table :data="pageOperations" row-key="requestId" border data-testid="storage-operations">
      <el-table-column type="expand"><template #default="{ row }"><div class="operation-detail"><p v-if="row.error" class="error-text">{{ row.error }}</p><p v-for="issue in row.result?.errors || []" :key="`${issue.path}:${issue.error}`" class="error-text">{{ issue.path }}：{{ issue.error }}</p><p v-for="item in row.result?.items || []" :key="item.path">{{ item.path }} · {{ storageBytes(item.bytes) }} · {{ item.files ?? '—' }} 个文件</p><p v-if="row.result?.execution_ids?.length">包含执行：{{ row.result.execution_ids.join('、') }}</p><p v-if="row.result?.dependent_run_ids?.length">受影响任务：{{ row.result.dependent_run_ids.join('、') }}</p></div></template></el-table-column>
      <el-table-column label="运行" min-width="150"><template #default="{ row }"><DisplayId kind="run" :id="row.runId" /></template></el-table-column>
      <el-table-column label="操作" min-width="130"><template #default="{ row }">{{ kindLabel(row.kind, row.mode) }}</template></el-table-column>
      <el-table-column label="进度" min-width="180"><template #default="{ row }">{{ row.result ? storagePhase(row.result) : row.rejected ? '请求未受理' : '等待受理确认' }}<small v-if="row.kind === 'cleanup'">已删除 {{ row.result?.summary?.deleted_files ?? 0 }} 个文件 · {{ storageBytes(row.result?.summary?.deleted_bytes ?? 0) }}</small><small v-if="row.error" class="error-text">{{ row.error }}</small></template></el-table-column>
      <el-table-column label="操作" width="120"><template #default="{ row }"><el-button v-if="row.rejected || row.result?.status === 'partial' || row.result?.status === 'failed' || (!row.result && !row.sending)" link :loading="row.sending" @click="state.retry(row)">重试同一操作</el-button><span v-else-if="row.result && storageOperationDone(row.result.status)">已结束</span></template></el-table-column>
    </el-table>
    <el-pagination v-model:current-page="operationPage" :page-size="10" :total="operationRows.length" layout="total, prev, pager, next" />
  </section>
</template>

<style scoped>
.storage-toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0}.storage-toolbar>.el-input{flex:1;min-width:180px}.storage-toolbar>.el-select{width:210px}.storage-hint,small{font-size:12px;color:var(--muted);line-height:1.6}small{display:block;overflow-wrap:anywhere}.operation-detail{padding:10px 18px;max-height:280px;overflow:auto;overflow-wrap:anywhere}.error-text{color:#b45309}.el-pagination{justify-content:flex-end;margin:14px 0;flex-wrap:wrap}.run-storage a{color:var(--accent-deep);font-size:13px;margin-left:8px}h3{font-size:15px;margin-top:24px}@media(max-width:760px){.storage-toolbar>.el-select{width:100%}.storage-toolbar>.el-button{margin-left:0}.el-pagination{justify-content:flex-start}}
</style>
