<script setup lang="ts">
import { computed, reactive, ref, watch, type ComputedRef } from 'vue'
import type { FactoryRun, ReportFolder, ReportFile } from '@/phoneFactoryApi'
import type { EvaluationProgress } from '@/composables/useEvaluationProgress'
import DisplayId from './DisplayId.vue'
import CollectionRunProgress from './CollectionRunProgress.vue'
import CollectionArtifactDetails from './CollectionArtifactDetails.vue'
import { runStatus, collectionProgressText, hasCollectionProgress } from '@/utils/phoneFactoryRuns'
import { useDisplayIdentifiers } from '@/composables/useDisplayIdentifiers'
import { agentLabel } from '@/utils/collectionAgents'
import { runDisplayStatus, runMessage } from '@/utils/phoneFactoryRuns'
import { collectionTaskSource } from '@/utils/collectionResults'
import { evaluationModeLabel, evaluationSourceLabel, evaluationStatus, collectionStatus, verdictLabel, checklistSourceLabel } from '@/utils/modelIteration'
import { caseErrors, evaluationCaseAbnormal, evaluationCaseActive, factoryRunAbnormal, factoryRunActive, factoryRunErrors, factoryRunId, flatReportFiles, newestFactoryRuns } from '@/utils/factoryHistory'

export type FactoryDetailsTab = 'runs' | 'cases' | 'reports'
const props = defineProps<{
  modelValue: boolean; tab: FactoryDetailsTab; evaluation: boolean; runs: FactoryRun[]
  progress?: EvaluationProgress; folders: ReportFolder[]; runError?: string; reportError?: string
  loadingReports: boolean; downloadingReport: string; runFilter?: 'all' | 'active' | 'abnormal'; focusedRun?: string; scopeKey?: string
}>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]; 'update:tab': [value: FactoryDetailsTab]
  'update:runFilter': [value: 'all' | 'active' | 'abnormal']
  refreshReports: []; downloadReport: [folder: ReportFolder, file: ReportFile]
}>()
const { displayId, matches } = useDisplayIdentifiers()
const runControls = reactive({ query: '', status: 'all', page: 1, size: 10 })
const caseControls = reactive({ query: '', status: 'all', page: 1, size: 10 })
const reportControls = reactive({ query: '', page: 1, size: 10 })
const expandedRuns = ref<string[]>([])
const orderedRuns = computed(() => newestFactoryRuns(props.runs))
const selectedRun = computed({ get: () => props.progress?.selectedRun.value || '', set: value => props.progress?.selectRun(value) })
const results = computed(() => props.progress?.results.value)
const progressBusy = computed(() => props.progress?.busy.value || false)
const filteredRuns = computed(() => orderedRuns.value.filter(run =>
  (runControls.status === 'all' || (runControls.status === 'active' ? factoryRunActive(run) : factoryRunAbnormal(run)))
  && (matches(runControls.query, 'run', factoryRunId(run), '', run.filename || '', runDisplayStatus(run), agentLabel(run.agent_type), runMessage(run), collectionProgressText(run))
    || matches(runControls.query, 'batch', run.batch_id) || matches(runControls.query, 'batch', collectionTaskSource(run)))))
const filteredCases = computed(() => (results.value?.cases || []).filter(row =>
  (caseControls.status === 'all' || (caseControls.status === 'active' ? evaluationCaseActive(row) : caseControls.status === 'abnormal' ? evaluationCaseAbnormal(row) : row.evaluation_status === 'completed'))
  && [row.case_id, row.case_key, row.phone_id, row.failure_reason, verdictLabel(row.verdict), ...caseErrors(row)].join(' ').toLocaleLowerCase().includes(caseControls.query.trim().toLocaleLowerCase())))
const reportFiles = computed(() => flatReportFiles(props.folders))
const filteredReports = computed(() => reportFiles.value.filter(row => matches(reportControls.query, 'run', row.folder.run_id, '', row.folder.dir_name, row.file.name, `第 ${row.folder.index} 轮`, agentLabel(row.folder.agent_type))))
function paginate<T>(rows: ComputedRef<T[]>, controls: { query: string; page: number; size: number }) {
  watch(() => [controls.query, controls.size], () => { controls.page = 1 }, { flush: 'sync' })
  watch(() => rows.value.length, length => { controls.page = Math.max(1, Math.min(controls.page, Math.ceil(length / controls.size))) }, { flush: 'sync' })
  return computed(() => rows.value.slice((controls.page - 1) * controls.size, controls.page * controls.size))
}
const pagedRuns = paginate(filteredRuns, runControls), pagedCases = paginate(filteredCases, caseControls), pagedReports = paginate(filteredReports, reportControls)
watch(() => props.runFilter, value => { runControls.status = value || 'all' }, { immediate: true })
watch(() => runControls.status, value => { runControls.page = 1; emit('update:runFilter', value as 'all' | 'active' | 'abnormal') })
watch(() => caseControls.status, () => { caseControls.page = 1 })
watch(selectedRun, () => { caseControls.page = 1 })
watch(() => props.scopeKey, () => { runControls.page = 1; expandedRuns.value = [] })
watch([() => props.focusedRun, () => props.modelValue], ([id, opened]) => {
  if (!id || !opened) return
  runControls.query = ''; runControls.status = 'all'
  const index = filteredRuns.value.findIndex(run => factoryRunId(run) === id)
  runControls.page = Math.max(1, Math.floor(index / runControls.size) + 1)
  expandedRuns.value = [id]
}, { immediate: true })
const reportTime = (value?: number) => value != null && Number.isFinite(value) ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'
function showCases(runId: string) {
  if (progressBusy.value) return
  props.progress?.selectRun(runId); emit('update:tab', 'cases')
}
</script>

<template>
  <el-drawer :model-value="modelValue" :title="evaluation ? '评估运行详情' : '采集运行详情'" size="min(1200px, 92vw)" class="factory-details-drawer" @update:model-value="emit('update:modelValue', $event)">
    <div class="detail-tabs" role="tablist" aria-label="运行详情分类">
      <button role="tab" :aria-selected="tab === 'runs'" :class="{ active: tab === 'runs' }" @click="emit('update:tab', 'runs')">运行记录</button>
      <button v-if="evaluation" role="tab" :aria-selected="tab === 'cases'" :class="{ active: tab === 'cases' }" @click="emit('update:tab', 'cases')">用例明细</button>
      <button v-if="evaluation" role="tab" :aria-selected="tab === 'reports'" :class="{ active: tab === 'reports' }" @click="emit('update:tab', 'reports')">报告</button>
    </div>
    <section v-show="tab === 'runs'" data-testid="run-details-tab">
      <div class="detail-toolbar"><el-input v-model="runControls.query" clearable placeholder="搜索编号、文件名或结果" aria-label="搜索运行" /><el-select v-model="runControls.status" aria-label="运行状态筛选"><el-option label="全部状态" value="all" /><el-option label="活动运行" value="active" /><el-option label="异常运行" value="abnormal" /></el-select></div>
      <el-alert v-if="runError" :title="runError" type="warning" :closable="false" />
      <el-table :data="pagedRuns" :row-key="factoryRunId" :expand-row-keys="expandedRuns" border data-testid="factory-runs" @expand-change="(_row: FactoryRun, expanded: FactoryRun[]) => expandedRuns = expanded.map(factoryRunId)">
        <el-table-column type="expand"><template #default="{ row }"><div class="expanded-record"><CollectionRunProgress :run="row" /><CollectionArtifactDetails :run="row" /><p v-if="!evaluation">每任务采集次数：{{ row.repeat_times ?? 1 }}</p><p v-if="row.filename">文件：{{ row.filename }}</p><p v-for="message in factoryRunErrors(row)" :key="message" class="error-text">{{ message }}</p><p v-if="!factoryRunErrors(row).length">{{ runMessage(row) || '暂无错误记录' }}</p><p v-if="evaluation">{{ evaluationModeLabel(row.execution_mode) }} · {{ row.evaluation_profile_id || '历史配置' }} · {{ evaluationSourceLabel(row) }}</p><template v-if="row.agent_type === 'pipelineagent'"><p>执行模型：{{ row.execution_model || '沿用采集端配置' }} · VLA 接口：{{ row.vla || '沿用采集端配置' }}</p><p>规划模型：{{ row.planner_config?.model || '沿用采集端配置' }} · 规划接口：{{ row.planner_config?.base_url || '沿用采集端配置' }}</p></template></div></template></el-table-column>
        <el-table-column v-if="!evaluation" label="任务来源" min-width="150"><template #default="{ row }"><DisplayId kind="batch" :id="collectionTaskSource(row)" /></template></el-table-column>
        <el-table-column label="结果批次" min-width="150"><template #default="{ row }"><DisplayId kind="batch" :id="row.batch_id" /><small v-if="row.batch_status === 'published'"> · 已发布</small></template></el-table-column>
        <el-table-column label="运行编号" min-width="150"><template #default="{ row }"><DisplayId kind="run" :id="factoryRunId(row)" /></template></el-table-column>
        <el-table-column label="采集引擎" min-width="110"><template #default="{ row }">{{ row.execution_mode === 'evaluate_only' ? (row.source?.agent_type ? agentLabel(row.source.agent_type) : '已有轨迹') : agentLabel(row.agent_type) }}</template></el-table-column>
        <el-table-column label="时间" min-width="155"><template #default="{ row }">{{ row.created_at?.slice(0, 19).replace('T', ' ') || '—' }}</template></el-table-column>
        <el-table-column label="状态" width="105"><template #default="{ row }">{{ runDisplayStatus(row) }}</template></el-table-column>
        <el-table-column label="采集 / 回传" min-width="150"><template #default="{ row }"><template v-if="row.collection_summary && hasCollectionProgress(row)">{{ runStatus(row.collection_status || 'pending') }} / {{ runStatus(row.transfer_status || 'waiting') }}<small class="transfer-count">已回传 {{ row.collection_summary.transferred_trajectory_count ?? 0 }}/{{ row.collection_summary.sealed_trajectory_count }} 条</small></template><template v-else>—</template></template></el-table-column>
        <el-table-column label="结果" min-width="170" show-overflow-tooltip><template #default="{ row }">{{ runMessage(row) }}</template></el-table-column>
        <el-table-column label="操作" min-width="200"><template #default="{ row }"><el-button v-if="evaluation" link :disabled="progressBusy" @click="showCases(row.run_id)">查看用例</el-button><slot name="run-actions" :run="row" /></template></el-table-column>
      </el-table>
      <el-pagination v-model:current-page="runControls.page" v-model:page-size="runControls.size" :page-sizes="[10, 20, 50]" :total="filteredRuns.length" layout="total, sizes, prev, pager, next" />
    </section>
    <section v-if="evaluation && progress" v-show="tab === 'cases'" data-testid="case-details-tab">
      <div class="detail-toolbar"><el-select v-model="selectedRun" filterable :disabled="progressBusy" placeholder="选择评估运行" aria-label="明细评估运行"><el-option v-for="run in orderedRuns" :key="run.run_id" :value="run.run_id" :label="`${displayId('run', run.run_id)} · ${run.evaluation_profile_id || '历史规则'}`" /></el-select><el-button v-if="progress.canCancel.value" type="danger" plain :loading="progress.cancelling.value" :disabled="!!progress.downloading.value" @click="progress.cancelRun">停止本次运行</el-button></div>
      <el-alert v-if="progress.resultError.value" :title="progress.resultError.value" type="warning" :closable="false" />
      <div class="detail-toolbar"><el-input v-model="caseControls.query" clearable placeholder="搜索用例、手机或原因" aria-label="搜索用例" /><el-select v-model="caseControls.status" aria-label="用例状态筛选"><el-option label="全部状态" value="all" /><el-option label="进行中" value="active" /><el-option label="评测完成" value="completed" /><el-option label="异常或未通过" value="abnormal" /></el-select></div>
      <p class="detail-hint" v-if="results">共 {{ results.summary.total }} 条轨迹 · 评测完成 {{ results.summary.evaluation_completed }} · 评测异常 {{ results.summary.evaluation_failed }}</p>
      <el-table :data="pagedCases" row-key="case_key" border data-testid="evaluation-cases">
        <el-table-column type="expand"><template #default="{ row }"><div class="expanded-record"><p v-for="message in caseErrors(row)" :key="message" class="error-text">{{ message }}</p><p v-if="!caseErrors(row).length">暂无错误记录</p><p>用例身份：{{ row.case_key }}</p></div></template></el-table-column>
        <el-table-column prop="phone_id" label="来源手机" min-width="130" show-overflow-tooltip />
        <el-table-column prop="case_id" label="用例" min-width="130" show-overflow-tooltip />
        <el-table-column v-if="progress.resultMetadata.value.execution_mode !== 'evaluate_only'" label="采集状态" min-width="100"><template #default="{ row }">{{ collectionStatus(row.collection_status) }}</template></el-table-column>
        <el-table-column label="评测状态" min-width="100"><template #default="{ row }">{{ evaluationStatus(row.evaluation_status) }}</template></el-table-column>
        <el-table-column label="清单来源" min-width="130"><template #default="{ row }">{{ checklistSourceLabel(row.checklist_source) }}</template></el-table-column>
        <el-table-column label="结论" min-width="100"><template #default="{ row }">{{ verdictLabel(row.verdict) }}</template></el-table-column>
        <el-table-column prop="failure_reason" label="原因" min-width="170" show-overflow-tooltip />
        <el-table-column prop="attempt_count" label="尝试次数" width="90" />
        <el-table-column label="报告" width="110"><template #default="{ row }"><el-button v-if="row.report_file_id" link :loading="progress.downloading.value === row.case_key" :disabled="progressBusy && progress.downloading.value !== row.case_key" @click="progress.download(row)">下载 JSON</el-button></template></el-table-column>
      </el-table>
      <el-pagination v-model:current-page="caseControls.page" v-model:page-size="caseControls.size" :page-sizes="[10, 20, 50]" :total="filteredCases.length" layout="total, sizes, prev, pager, next" />
    </section>
    <section v-if="evaluation" v-show="tab === 'reports'" data-testid="evaluation-reports">
      <div class="detail-toolbar"><el-input v-model="reportControls.query" clearable placeholder="搜索轮次、运行编号或报告文件" aria-label="搜索报告" /><el-button :loading="loadingReports" @click="emit('refreshReports')">刷新报告</el-button></div>
      <el-alert v-if="reportError" :title="reportError" type="warning" :closable="false" />
      <p class="detail-hint">{{ folders.length }} 轮 · {{ reportFiles.length }} 个报告文件</p>
      <el-table :data="pagedReports" row-key="key" border>
        <el-table-column label="轮次" width="90"><template #default="{ row }">第 {{ row.folder.index }} 轮</template></el-table-column>
        <el-table-column label="运行 / 目录" min-width="170" show-overflow-tooltip><template #default="{ row }"><DisplayId v-if="row.folder.run_id" kind="run" :id="row.folder.run_id" /><span v-else>{{ row.folder.dir_name }}</span></template></el-table-column>
        <el-table-column prop="file.name" label="报告文件" min-width="200" show-overflow-tooltip />
        <el-table-column label="生成时间" min-width="170"><template #default="{ row }">{{ reportTime(row.file.modified ?? row.folder.modified) }}</template></el-table-column>
        <el-table-column label="操作" width="110"><template #default="{ row }"><el-button link type="primary" :loading="downloadingReport === `${row.folder.run_id || row.folder.dir_name}/${row.file.file_id || row.file.name}`" @click="emit('downloadReport', row.folder, row.file)">下载报告</el-button></template></el-table-column>
      </el-table>
      <el-pagination v-model:current-page="reportControls.page" v-model:page-size="reportControls.size" :page-sizes="[10, 20, 50]" :total="filteredReports.length" layout="total, sizes, prev, pager, next" />
    </section>
  </el-drawer>
</template>

<style scoped>
.transfer-count{display:block;margin-top:4px;color:var(--muted);font-size:12px}
.detail-tabs{position:sticky;top:0;z-index:2;display:flex;gap:6px;padding:0 0 14px;background:white;border-bottom:1px solid var(--line);margin-bottom:18px}.detail-tabs button{padding:9px 16px;background:transparent;border:0;border-radius:7px;color:var(--muted);cursor:pointer;font:inherit}.detail-tabs button.active{background:#e6f6f0;color:var(--accent-deep);font-weight:600}.detail-toolbar{display:flex;gap:12px;margin-bottom:14px;align-items:center}.detail-toolbar>.el-input{flex:1}.detail-toolbar>.el-select{width:230px}.detail-toolbar>.el-button{flex-shrink:0}.expanded-record{padding:10px 22px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}.error-text{color:#b45309}.detail-hint{color:var(--muted);font-size:12px}.el-pagination{margin-top:18px;justify-content:flex-end;flex-wrap:wrap;gap:6px}.el-alert{margin-bottom:14px}:global(.factory-details-drawer .el-drawer__header){margin-bottom:0;padding-bottom:14px;border-bottom:1px solid var(--line)}@media(max-width:760px){:global(.factory-details-drawer){width:100vw!important}.detail-toolbar{flex-wrap:wrap}.detail-toolbar>.el-select{width:100%}.detail-tabs button{padding:9px 12px}.el-pagination{justify-content:flex-start}:global(.factory-details-drawer .el-drawer__body){padding:14px}}
</style>
