<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { type CollectionCaseGroup, type CollectionCasePage, type CollectionInstancePage, type createFactoryApi } from '@/phoneFactoryApi'
import { diagnosisStatus } from '@/utils/collectionDiagnostics'
import CollectionInstanceArtifacts from './CollectionInstanceArtifacts.vue'

const props = defineProps<{ runId: string; api: ReturnType<typeof createFactoryApi>; active: boolean; phoneIds?: string[]; focusedPhone?: string }>()
const emit = defineEmits<{ unsupported: [value: boolean] }>()
const controls = reactive({ app: '', phone: '', status: '', query: '', page: 1, size: 10 })
const result = ref<CollectionCasePage>(), loading = ref(false), error = ref('')
const preparing = ref(false)
const expanded = ref<string[]>([])
const instances = ref<Record<string, CollectionInstancePage>>({})
const instanceErrors = ref<Record<string, string>>({}), instanceBusy = ref<Record<string, boolean>>({})
const instancePages = reactive<Record<string, number>>({})
let generation = 0, controller: AbortController | undefined, pending = false
let refreshTimer: ReturnType<typeof setInterval> | undefined
const instanceControllers = new Map<string, AbortController>()
const groupKey = (row: CollectionCaseGroup) => JSON.stringify([row.case_id, row.phone_id])
const rows = computed(() => result.value?.items || [])
const instanceRows = (row: CollectionCaseGroup) => {
  const key = groupKey(row), page = instancePages[key] || 1
  return (instances.value[key]?.items || []).slice((page - 1) * 20, page * 20)
}
const diagnosticText = (row: CollectionCaseGroup) => typeof row.latest_diagnostic === 'string' ? row.latest_diagnostic : row.latest_diagnostic?.message || row.latest_diagnostic?.reason_code || ''
const countText = (counts: Record<string, number>) => `计划 ${counts.planned_count ?? 0} · 完成 ${counts.completed_count ?? 0} · 失败 ${counts.failed_count ?? 0} · 未执行 ${counts.not_run_count ?? 0} · 进行中 ${counts.active_count ?? 0} · 已接收 ${counts.received_count ?? 0}`

async function refresh(replace = false) {
  if (!props.active || !props.runId || (pending && !replace)) return
  const epoch = ++generation, id = props.runId
  controller?.abort(); const request = controller = new AbortController()
  loading.value = true; pending = true
  const timer = setTimeout(() => request.abort(), 15000)
  try {
    const value = await props.api.casePage(id, { app: controls.app.trim(), phone_id: controls.phone, status: controls.status,
      q: controls.query.trim(), offset: (controls.page - 1) * controls.size, limit: controls.size }, request.signal)
    if (epoch !== generation || request.signal.aborted || id !== props.runId) return
    preparing.value = !!value.preparing
    if (value.preparing && result.value?.supported) { error.value = ''; return }
    result.value = value; error.value = ''; emit('unsupported', value.supported === false)
    const last = Math.max(1, Math.ceil(value.total / controls.size))
    if (controls.page > last) controls.page = last
    for (const row of value.items) if (expanded.value.includes(groupKey(row))) void loadInstances(row)
  } catch (cause) {
    if (epoch !== generation || id !== props.runId) return
    const issue = cause as Error & { status?: number }
    if ([404, 405, 501].includes(issue.status || 0)) {
      result.value = { supported: false, items: [], total: 0, offset: 0, limit: controls.size }; emit('unsupported', true)
    } else error.value = request.signal.aborted ? '用例明细读取超时，保留上次结果。' : `用例明细暂不可用：${issue.message}`
  } finally { clearTimeout(timer); if (epoch === generation) { loading.value = false; pending = false } }
}
async function loadInstances(row: CollectionCaseGroup) {
  const key = groupKey(row), id = props.runId
  instanceControllers.get(key)?.abort()
  const request = new AbortController(); instanceControllers.set(key, request)
  instanceBusy.value = { ...instanceBusy.value, [key]: true }
  const timer = setTimeout(() => request.abort(), 15000)
  try {
    const value = await props.api.caseInstances(id, row.case_id, row.phone_id, request.signal)
    if (request.signal.aborted || id !== props.runId || instanceControllers.get(key) !== request) return
    if (value.preparing) { instanceErrors.value = { ...instanceErrors.value, [key]: '实例汇总准备中，保留上次结果。' }; return }
    instances.value = { ...instances.value, [key]: value }; instanceErrors.value = { ...instanceErrors.value, [key]: '' }
    instancePages[key] = Math.max(1, Math.min(instancePages[key] || 1, Math.ceil(value.items.length / 20)))
  } catch (cause) {
    if (id === props.runId && instanceControllers.get(key) === request) instanceErrors.value = { ...instanceErrors.value, [key]: request.signal.aborted ? '实例读取超时，请重试。' : (cause as Error).message }
  } finally {
    clearTimeout(timer)
    if (instanceControllers.get(key) === request) { instanceBusy.value = { ...instanceBusy.value, [key]: false }; instanceControllers.delete(key) }
  }
}
function toggleGroup(row: CollectionCaseGroup, open: boolean) {
  const key = groupKey(row)
  if (open) { if (!expanded.value.includes(key)) expanded.value.push(key); if (!instances.value[key] && !instanceBusy.value[key]) void loadInstances(row) }
  else expanded.value = expanded.value.filter(item => item !== key)
}
watch(() => props.focusedPhone, phone => { if (phone) controls.phone = phone }, { immediate: true })
watch([() => controls.app, () => controls.phone, () => controls.status, () => controls.query, () => controls.size], () => { controls.page = 1 }, { flush: 'sync' })
watch(() => props.runId, () => {
  generation++; controller?.abort(); pending = false; result.value = undefined; error.value = ''; preparing.value = false; controls.page = 1
  expanded.value = []; instances.value = {}; instanceErrors.value = {}; instanceControllers.forEach(request => request.abort()); instanceControllers.clear()
}, { flush: 'sync' })
watch([() => props.runId, () => props.active, () => controls.app, () => controls.phone, () => controls.status, () => controls.query, () => controls.page, () => controls.size], () => {
  if (props.active) void refresh(true)
  else { generation++; controller?.abort(); pending = false; loading.value = false }
}, { immediate: true })
// Detail reads are independent of diagnostic/probe failures, and do no work
// while this tab is closed. The page's summary poller remains mounted.
onMounted(() => { refreshTimer = setInterval(() => void refresh(), 5000) })
onBeforeUnmount(() => { clearInterval(refreshTimer); generation++; controller?.abort(); instanceControllers.forEach(request => request.abort()); instanceControllers.clear() })
</script>

<template>
  <section data-testid="collection-case-groups">
    <div class="case-filters"><el-input v-model="controls.app" clearable placeholder="按 App 筛选" aria-label="用例 App 筛选" /><el-select v-model="controls.phone" clearable filterable placeholder="全部手机" aria-label="用例手机筛选"><el-option v-for="phone in phoneIds || []" :key="phone" :label="phone" :value="phone" /></el-select><el-select v-model="controls.status" clearable placeholder="全部执行状态" aria-label="采集用例状态"><el-option value="running" label="执行中" /><el-option value="completed" label="已完成" /><el-option value="failed" label="失败" /><el-option value="not_run" label="未执行" /></el-select><el-input v-model="controls.query" clearable placeholder="搜索用例或任务" aria-label="搜索分组用例" /></div>
    <el-alert v-if="error" :title="error" type="warning" :closable="false" />
    <el-alert v-if="preparing" title="用例汇总准备中，已有结果与筛选保持不变。" type="info" :closable="false" />
    <el-alert v-if="result?.supported === false" :title="result.message || '当前采集端不支持分组用例查询，请使用运行记录中的原有回传明细。'" type="info" :closable="false" />
    <p v-if="!runId" class="hint">请选择运行。</p>
    <template v-else-if="result?.supported">
      <p class="hint">共 {{ result.total }} 个用例 / 手机组。统计以当前执行为准；历史失败保留在尝试记录中。</p>
      <p v-if="result.scope_counts && Object.keys(result.scope_counts).length" class="hint">当前 App / 手机范围：{{ countText(result.scope_counts) }}<br v-if="controls.query || controls.status" /><span v-if="(controls.query || controls.status) && result.filtered_counts">筛选后：{{ countText(result.filtered_counts) }}</span></p>
      <p v-if="!rows.length && !loading" class="hint">当前筛选没有匹配用例。</p>
      <div v-loading="loading" class="case-groups">
        <details v-for="row in rows" :key="groupKey(row)" :open="expanded.includes(groupKey(row))" class="case-group" @toggle="toggleGroup(row, ($event.target as HTMLDetailsElement).open)">
          <summary><span class="case-title">{{ row.case_id }} · {{ row.app || '未提供 App' }} · {{ row.phone_id }}</span><span class="case-task">{{ row.task }}</span><span class="counts">计划 {{ row.planned_count }} · 完成 {{ row.completed_count }} · 失败 {{ row.failed_count }} · 未执行 {{ row.not_run_count }} · 进行中 {{ row.active_count }} · 已接收 {{ row.received_count }}</span><span v-if="diagnosticText(row)" class="case-cause" :title="diagnosticText(row)">{{ diagnosticText(row) }}</span></summary>
          <div class="instance-list"><p v-if="instanceBusy[groupKey(row)]" class="hint">正在读取重复实例…</p><p v-if="instanceErrors[groupKey(row)]" class="warning">{{ instanceErrors[groupKey(row)] }} <el-button link @click="loadInstances(row)">重试</el-button></p>
            <details v-for="instance in instanceRows(row)" :key="instance.repeat_index" class="repeat-instance">
              <summary>第 {{ instance.repeat_index }} / {{ instance.repeat_times || row.repeat_times }} 次 <span>{{ diagnosisStatus(instance.active_attempt?.execution_status || instance.active_attempt?.status || instance.status || instance.execution_status) }} · {{ diagnosisStatus(instance.receipt_status) }}</span><span class="hint"> · {{ instance.attempt_count }} 次尝试</span></summary>
              <p v-if="instance.active_attempt" class="hint">续采中，保留上次结果。当前执行编号：{{ instance.active_attempt.execution_id || '尚未分配' }}</p>
              <details v-if="instance.active_attempt && instance.error"><summary>查看上次结果原因</summary><p class="full-error">{{ instance.error }}</p></details>
              <p v-else-if="instance.error" class="full-error">当前执行：{{ instance.error }}</p>
              <p class="hint">{{ instance.active_attempt ? '上次结果执行编号' : '当前执行编号' }}：{{ instance.execution_id || '尚未分配' }}</p>
              <CollectionInstanceArtifacts v-if="instance.artifact_id" :api="api" :run-id="runId" :artifact-id="instance.artifact_id" :execution-id="instance.execution_id" />
              <details v-if="instance.attempts?.length" class="attempt-history"><summary>历史尝试（{{ instance.attempts.length }}）</summary><div v-for="(attempt, index) in instance.attempts" :key="attempt.execution_id || attempt.attempt_id || index" class="attempt"><p>{{ attempt.execution_id || attempt.attempt_id || `尝试 ${index + 1}` }} · {{ diagnosisStatus(attempt.execution_status || attempt.status) }}</p><p v-if="attempt.created_at" class="hint">{{ attempt.created_at }}</p><CollectionInstanceArtifacts v-if="attempt.artifact_id" :api="api" :run-id="runId" :artifact-id="attempt.artifact_id" :execution-id="attempt.execution_id" /><details v-if="attempt.error || attempt.message"><summary>查看本次原因</summary><p class="full-error">{{ attempt.error || attempt.message }}</p></details></div></details>
            </details>
            <el-pagination v-if="(instances[groupKey(row)]?.items.length || 0) > 20" :current-page="instancePages[groupKey(row)] || 1" :page-size="20" :total="instances[groupKey(row)]?.items.length || 0" layout="prev, pager, next" @update:current-page="instancePages[groupKey(row)] = $event" />
          </div>
        </details>
      </div>
      <el-pagination v-model:current-page="controls.page" v-model:page-size="controls.size" :page-sizes="[10, 20, 50]" :total="result.total" layout="total, sizes, prev, pager, next" />
    </template>
  </section>
</template>

<style scoped>
.case-filters{display:grid;grid-template-columns:1fr 1fr 1fr 1.3fr;gap:10px;margin:15px 0}.case-group{border:1px solid var(--line);border-radius:8px;margin:10px 0}.case-group>summary{padding:13px;cursor:pointer;font-size:13px}.case-title{font-weight:650}.case-task,.counts,.case-cause{display:block;margin:7px 0 0 16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.case-task,.counts,.hint{color:var(--muted)}.case-cause,.warning{color:#b45309}.instance-list{padding:0 14px 14px}.repeat-instance{border-top:1px solid var(--line);padding:10px 0;font-size:12px}.repeat-instance>summary{cursor:pointer}.repeat-instance>summary>span{margin-left:10px}.attempt-history{margin:10px 0 0 18px}.attempt{border-left:2px solid var(--line);padding-left:12px;margin:12px 0}.attempt>p{overflow-wrap:anywhere}.full-error{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.hint{font-size:12px;line-height:1.5}.el-pagination{margin-top:14px}@media(max-width:760px){.case-filters{grid-template-columns:1fr 1fr}.counts{white-space:normal;line-height:1.6}}
</style>
