<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { modelIterationApi, phoneFactoryApi, type CollectionArtifact, type FactoryRun } from '@/phoneFactoryApi'
import { runStatus } from '@/utils/phoneFactoryRuns'

const props = defineProps<{ run: FactoryRun }>()
const controls = reactive({ query: '', status: 'all', page: 1, size: 10 })
const remote = computed(() => props.run.receipt_policy_version === 2)
const runId = computed(() => props.run.collection_run_id || props.run.run_id || '')
const api = computed(() => props.run.execution_mode === 'collect_and_evaluate' ? modelIterationApi : phoneFactoryApi)
const remoteRows = ref<CollectionArtifact[]>([]), remoteTotal = ref(0), loading = ref(false), loadError = ref('')
let generation = 0, controller: AbortController | undefined, pending = false
const selectedArtifact = ref(''), fileRows = ref<Array<{ path: string; size: number; sha256: string }>>([])
const fileLoading = ref(false), fileError = ref(''), fileControls = reactive({ query: '', page: 1, size: 10 })
let fileGeneration = 0, fileController: AbortController | undefined
const filteredFiles = computed(() => fileRows.value.filter(file => file.path.toLocaleLowerCase().includes(fileControls.query.trim().toLocaleLowerCase())))
const pageFiles = computed(() => filteredFiles.value.slice((fileControls.page - 1) * fileControls.size, fileControls.page * fileControls.size))
const rows = computed(() => (props.run.collection_artifacts || []).filter(row => {
  const abnormal = !!row.error || !!row.transfer_error || row.transfer_status === 'failed' || ['failed', 'timeout', 'interrupted'].includes(row.execution_status || '')
  return (controls.status === 'all' || (controls.status === 'abnormal' ? abnormal : row.transfer_status === 'completed'))
    && [row.collection_case_id, row.phone_id, row.expanded_case_id, row.source_trajectory_id, row.error, row.transfer_error]
      .join(' ').toLocaleLowerCase().includes(controls.query.trim().toLocaleLowerCase())
}))
const pageRows = computed(() => remote.value ? remoteRows.value : rows.value.slice((controls.page - 1) * controls.size, controls.page * controls.size))
const total = computed(() => remote.value ? remoteTotal.value : rows.value.length)
watch([() => controls.query, () => controls.status, () => controls.size, () => props.run.collection_run_id || props.run.run_id], () => { controls.page = 1 }, { flush: 'sync' })
watch(() => rows.value.length, length => { if (!remote.value) controls.page = Math.max(1, Math.min(controls.page, Math.ceil(length / controls.size))) }, { flush: 'sync' })

async function refresh(replace = false) {
  if (!remote.value || !runId.value || props.run.execution_mode === 'evaluate_only') return
  if (pending && !replace) return
  const epoch = ++generation, id = runId.value
  controller?.abort()
  const request = controller = new AbortController()
  pending = true; loading.value = true; loadError.value = ''
  try {
    const result = await api.value.artifactPage(id, { offset: (controls.page - 1) * controls.size,
      limit: controls.size, search: controls.query.trim(), status: controls.status === 'all' ? '' : controls.status }, request.signal)
    if (epoch !== generation || request.signal.aborted || id !== runId.value) return
    remoteRows.value = result.items; remoteTotal.value = result.total
    const last = Math.max(1, Math.ceil(result.total / controls.size))
    if (controls.page > last) controls.page = last
  } catch (error) {
    if (epoch === generation && !request.signal.aborted) loadError.value = error instanceof Error ? error.message : '读取回传明细失败'
  } finally {
    if (epoch === generation) { pending = false; loading.value = false }
  }
}
watch(runId, () => { generation++; controller?.abort(); pending = false; remoteRows.value = []; remoteTotal.value = props.run.collection_artifact_count || 0; closeFiles() }, { flush: 'sync' })
watch([remote, runId, () => controls.query, () => controls.status, () => controls.page, () => controls.size], () => void refresh(true), { immediate: true })
// Parent polling owns the clock. Refreshing its run snapshot retains this page;
// closing the drawer disposes only this detail request, not parent polling.
watch(() => props.run, () => void refresh())
onBeforeUnmount(() => { generation++; controller?.abort(); pending = false; closeFiles() })
watch([() => fileControls.query, () => fileControls.size], () => { fileControls.page = 1 })

function receiptText(row: CollectionArtifact) {
  return ({ received: '已接收', quarantined: '已隔离', retry_wait: '等待重试', downloading: '下载中', verified: '校验完成', pending: '等待接收' } as Record<string, string>)[row.receipt_status || '']
    || runStatus(row.transfer_error ? 'failed' : row.transfer_status || 'pending')
}
function issueText(code: string) {
  return ({ preprocessing_evidence_invalid: '预处理所需证据不完整', evaluation_evidence_invalid: '评测所需证据不完整',
    image_unavailable: '部分截图不可用', image_evidence_missing: '没有可读截图', unsafe_evidence_reference: '证据引用路径无效',
    no_file_evidence: '没有文件证据', ui_xml_missing: '缺少 UI XML', ui_xml_invalid: 'UI XML 无效' } as Record<string, string>)[code] || code
}
function closeFiles() { fileGeneration++; fileController?.abort(); selectedArtifact.value = ''; fileRows.value = []; fileError.value = ''; fileLoading.value = false }
async function openFiles(row: CollectionArtifact) {
  if (row.receipt_status !== 'received' || row.kind === 'outcome') return
  closeFiles()
  selectedArtifact.value = row.artifact_id; fileControls.query = ''; fileControls.page = 1
  const epoch = fileGeneration, id = runId.value, request = fileController = new AbortController()
  fileLoading.value = true
  try {
    const result = await api.value.artifactDetail(id, row.artifact_id, request.signal)
    if (epoch !== fileGeneration || request.signal.aborted || id !== runId.value) return
    fileRows.value = result.files
  } catch (error) {
    if (epoch === fileGeneration && !request.signal.aborted) fileError.value = error instanceof Error ? error.message : '读取原件清单失败'
  } finally { if (epoch === fileGeneration) fileLoading.value = false }
}
function fileUrl(path: string) { return api.value.artifactFileUrl(runId.value, selectedArtifact.value, path) }
</script>

<template>
  <section v-if="(remote || run.collection_artifacts) && run.execution_mode !== 'evaluate_only'" class="artifact-details" data-testid="collection-artifact-details">
    <h4>轨迹与回传明细</h4>
    <p class="hint">原件接收与用途资格分别记录。缺少部分证据的原件仍会保留；无文件结果仅记录执行结局。</p>
    <div class="artifact-toolbar"><el-input v-model="controls.query" clearable placeholder="搜索用例、手机、轨迹或错误" aria-label="搜索采集轨迹" /><el-select v-model="controls.status" aria-label="轨迹回传筛选"><el-option value="all" label="全部条目" /><el-option value="abnormal" label="异常或警告" /><el-option value="completed" label="原件已接收" /><el-option v-if="remote" value="retry_wait" label="等待重试" /><el-option v-if="remote" value="quarantined" label="已隔离" /></el-select></div>
    <p v-if="loadError" class="detail-error" role="alert">{{ loadError }} <el-button link @click="refresh(true)">重试读取</el-button></p>
    <el-table v-loading="loading" :data="pageRows" row-key="artifact_id" border>
      <el-table-column type="expand"><template #default="{ row }"><div class="artifact-full"><p>扩展用例：{{ row.expanded_case_id || '等待清单' }}</p><p>轨迹编号：{{ row.source_trajectory_id || '等待清单' }}</p><p v-if="row.error">采集：{{ row.error }}</p><p v-if="row.transfer_error">回传：{{ row.transfer_error }}</p><p v-for="(issue, index) in row.eligibility?.issues || []" :key="index">{{ issueText(issue.code) }}{{ issue.path ? `：${issue.path}` : '' }}{{ issue.step ? `（步骤 ${issue.step}）` : '' }}</p></div></template></el-table-column>
      <el-table-column prop="collection_case_id" label="原用例" min-width="110" show-overflow-tooltip />
      <el-table-column prop="phone_id" label="手机" min-width="150" show-overflow-tooltip />
      <el-table-column label="采集次数" width="90"><template #default="{ row }">{{ row.repeat_index ? `${row.repeat_index} / ${row.repeat_times}` : '等待清单' }}</template></el-table-column>
      <el-table-column label="内容" width="100"><template #default="{ row }">{{ row.kind === 'outcome' ? '无文件结果' : row.kind === 'diagnostic' ? '诊断证据' : '采集轨迹' }}</template></el-table-column>
      <el-table-column label="采集 / 回传" min-width="150"><template #default="{ row }">{{ runStatus(row.execution_status || 'pending') }} / {{ receiptText(row) }}</template></el-table-column>
      <el-table-column v-if="remote" label="用途资格" min-width="170"><template #default="{ row }"><span v-if="row.eligibility">{{ row.eligibility.evaluation ? '可评测' : '不可评测' }} / {{ row.eligibility.preprocessing ? '可预处理' : '不可预处理' }}</span><span v-else>等待校验</span></template></el-table-column>
      <el-table-column label="错误" min-width="160" show-overflow-tooltip><template #default="{ row }">{{ row.transfer_error || row.error || '—' }}</template></el-table-column>
      <el-table-column v-if="remote" label="原件" width="100"><template #default="{ row }"><el-button v-if="row.receipt_status === 'received' && row.kind !== 'outcome'" link @click="openFiles(row)">查看原件</el-button><span v-else>{{ row.kind === 'outcome' ? '无文件' : '待接收' }}</span></template></el-table-column>
    </el-table>
    <el-pagination v-model:current-page="controls.page" v-model:page-size="controls.size" :page-sizes="[10, 20, 50]" :total="total" layout="total, sizes, prev, pager, next" />
    <section v-if="selectedArtifact" v-loading="fileLoading" class="source-files" aria-label="原件文件清单">
      <div class="source-heading"><strong>原件：{{ selectedArtifact }}</strong><el-button link @click="closeFiles">关闭原件清单</el-button></div>
      <p v-if="fileError" role="alert" class="detail-error">{{ fileError }}</p>
      <el-input v-model="fileControls.query" clearable placeholder="搜索文件名" aria-label="搜索原件文件" />
      <el-table :data="pageFiles" row-key="path" max-height="360"><el-table-column prop="path" label="文件" show-overflow-tooltip /><el-table-column prop="size" label="字节" width="110" /><el-table-column label="下载" width="80"><template #default="{ row }"><a :href="fileUrl(row.path)" download>下载</a></template></el-table-column></el-table>
      <el-pagination v-model:current-page="fileControls.page" v-model:page-size="fileControls.size" :page-sizes="[10, 20, 50]" :total="filteredFiles.length" layout="total, sizes, prev, pager, next" />
    </section>
  </section>
</template>

<style scoped>
.artifact-details{margin-top:12px}.artifact-details h4{margin:6px 0}.hint{font-size:12px;color:var(--muted)}.detail-error{color:var(--el-color-danger)}.artifact-toolbar{display:flex;gap:8px;margin:10px 0}.artifact-toolbar>.el-input{flex:1}.artifact-toolbar>.el-select{width:150px}.artifact-full{padding:10px 18px;white-space:pre-wrap;overflow-wrap:anywhere}.artifact-details .el-pagination{margin-top:10px;max-width:100%;overflow-x:auto}.source-files{margin-top:16px;border-top:1px solid var(--el-border-color);padding-top:10px}.source-heading{display:flex;justify-content:space-between;gap:8px;overflow-wrap:anywhere}.source-heading strong{min-width:0}@media(max-width:760px){.artifact-toolbar,.source-heading{flex-wrap:wrap}.artifact-toolbar>.el-select{width:100%}}
</style>
