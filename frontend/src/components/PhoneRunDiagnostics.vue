<script setup lang="ts">
import { computed } from 'vue'
import type { FactoryRun } from '@/phoneFactoryApi'
import { diagnosisAge, type CollectionDiagnosticsState } from '@/composables/useCollectionDiagnostics'
import { diagnosisConfidence, diagnosisPhase, diagnosisStatus, phoneDiagnosisText, resumeStatus } from '@/utils/collectionDiagnostics'
import { factoryRunId, newestFactoryRuns } from '@/utils/factoryHistory'
import { useDisplayIdentifiers } from '@/composables/useDisplayIdentifiers'

const props = defineProps<{ state: CollectionDiagnosticsState; runs: FactoryRun[]; allowResume: boolean; detailed?: boolean }>()
const emit = defineEmits<{ details: [phoneId: string] }>()
const { displayId } = useDisplayIdentifiers()
const selected = computed({ get: () => props.state.selectedRunId.value, set: id => props.state.selectRun(id) })
const data = computed(() => props.state.snapshot.value)
const diagnosticErrors = computed(() => [...new Set([data.value?.persistence_error, data.value?.receipt_storage?.error, data.value?.control_error, data.value?.sync_error].filter(Boolean))] as string[])
const ordered = computed(() => newestFactoryRuns(props.runs))
const age = (time?: string) => diagnosisAge(time, props.state.now.value)
function blocked(phone: string) {
  const item = props.state.operationFor(phone)
  return !!item && ['previewing', 'confirming', 'submitting'].includes(item.phase)
}
</script>

<template>
  <section class="phone-diagnostics" data-testid="phone-run-diagnostics">
    <div class="diagnosis-heading"><h3>{{ detailed ? '手机诊断详情' : '运行诊断' }}</h3><el-button link :loading="state.loading.value" @click="state.refresh()">刷新诊断</el-button></div>
    <el-select v-model="selected" filterable placeholder="选择运行查看各手机状态" aria-label="诊断运行" class="diagnosis-run-select">
      <el-option v-for="run in ordered" :key="factoryRunId(run)" :value="factoryRunId(run)" :label="`${displayId('run', factoryRunId(run))} · ${run.filename || diagnosisStatus(run.status)}`" />
    </el-select>
    <p v-if="!selected" class="hint">暂无运行。采集后可在此查看每部手机的进度和异常原因。</p>
    <el-alert v-if="state.error.value" :title="state.error.value" type="warning" :closable="false" />
    <el-alert v-if="data?.supported === false" :title="data.message || '当前采集端暂不支持运行诊断与续跑；原记录、下载和操作保持可用。'" type="info" :closable="false" />
    <template v-if="data?.supported">
      <p class="hint">{{ data.summary.total }} 部手机 · 正常 {{ data.summary.healthy }} · 需关注 {{ data.summary.warning }} · 异常 {{ data.summary.error }}<span v-if="state.stale.value" class="stale"> · 数据超过 20 秒未更新，显示上次快照</span></p>
      <el-alert v-for="message in diagnosticErrors" :key="message" :title="message" type="warning" :closable="false" />
      <div v-for="phone in data.phones" :key="phone.phone_id" class="diagnosis-phone" :data-phone="phone.phone_id">
        <div class="phone-line"><strong>{{ phone.phone_id }}</strong><span>{{ diagnosisStatus(phone.status) }}</span><span>{{ diagnosisPhase(phone.phase) }}<template v-if="phone.model_role || phone.model_call?.role"> · {{ phone.model_role || phone.model_call?.role }}</template></span><span>{{ diagnosisConfidence(phone.confidence) }}</span></div>
        <p class="phone-reason" :title="phoneDiagnosisText(phone)">{{ phoneDiagnosisText(phone) }}</p>
        <p v-if="phone.case_id" class="hint">{{ phone.app || '' }} · {{ phone.case_id }}<template v-if="phone.repeat_index"> · 第 {{ phone.repeat_index }} / {{ phone.repeat_times || 1 }} 次</template></p>
        <p class="hint">最近活动：{{ age(phone.last_activity_at) == null ? '未提供' : `${Math.floor(age(phone.last_activity_at)!)} 秒前` }}<span v-if="phone.stale || (age(phone.observed_at) ?? Infinity) > 20" class="stale"> · 阶段与模型信息超过 20 秒未更新或尚未提供</span></p>
        <p v-if="phone.model_call" class="hint">模型：{{ phone.model_call.model || '未提供' }} · 当前请求 {{ ((phone.model_call.elapsed_ms || 0) / 1000).toFixed(1) }} 秒 · 重试 {{ phone.model_call.retry_count ?? Math.max(0, (phone.model_call.attempt || 0) - 1) }} 次</p>
        <div class="phone-actions"><el-button v-if="!detailed" link type="primary" @click="emit('details', phone.phone_id)">查看诊断</el-button><el-button v-if="allowResume && phone.can_resume !== false" link type="primary" :disabled="blocked(phone.phone_id)" @click="state.continuePhones([phone.phone_id])">{{ state.operationFor(phone.phone_id)?.phase === 'waiting' ? '重试 / 确认续跑状态' : '继续该手机' }}</el-button><span role="status">{{ resumeStatus(state.operationFor(phone.phone_id), phone.phone_id) }}</span></div>
        <p v-if="state.operationFor(phone.phone_id)?.error" class="stale">{{ state.operationFor(phone.phone_id)?.error }}</p>
        <details v-if="detailed"><summary>完整诊断与请求记录</summary><p class="full-message">{{ phoneDiagnosisText(phone) }}</p><p v-if="phone.suggestion">{{ phone.suggestion }}</p><dl><dt>判断依据</dt><dd>{{ diagnosisConfidence(phone.confidence) }}</dd><dt>原因代码</dt><dd>{{ phone.reason_code || '未提供' }}</dd><dt>执行编号</dt><dd>{{ phone.execution_id || '未提供' }}</dd><dt>最近观察时间</dt><dd>{{ phone.observed_at || '未提供' }}</dd><dt>最近活动时间</dt><dd>{{ phone.last_activity_at || '未提供' }}</dd><dt>阶段耗时</dt><dd>{{ phone.phase_elapsed_seconds ?? phone.elapsed_seconds ?? age(phone.phase_started_at) ?? '未提供' }} 秒</dd><template v-if="phone.model_call"><dt>请求接口</dt><dd>{{ phone.model_call.endpoint || '未提供' }}</dd><dt>请求超时</dt><dd>{{ phone.model_call.timeout_seconds ?? '未提供' }} 秒</dd><dt>HTTP 状态</dt><dd>{{ phone.model_call.status_code ?? '未提供' }}</dd></template><dt v-if="state.operationFor(phone.phone_id)">续跑请求</dt><dd v-if="state.operationFor(phone.phone_id)">{{ state.operationFor(phone.phone_id)?.requestId }}</dd></dl><p v-for="(evidence, index) in phone.evidence || []" :key="index" class="full-message">{{ evidence.source }}：{{ evidence.detail }}</p><p v-if="phone.model_call?.error" class="full-message">{{ phone.model_call.error }}</p><p v-if="phone.persistence_error" class="full-message">{{ phone.persistence_error }}</p></details>
      </div>
      <p class="hint">诊断每 5 秒独立同步。续跑保留当前运行和批次，已完成的实例不重复执行。</p>
    </template>
  </section>
</template>

<style scoped>
.phone-diagnostics{margin-top:18px;border-top:1px solid var(--line);padding-top:14px;min-width:0}.diagnosis-heading,.phone-line,.phone-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.diagnosis-heading{justify-content:space-between}.diagnosis-heading h3{font-size:14px;margin:0 0 10px}.diagnosis-run-select{width:100%;margin-bottom:8px}.diagnosis-phone{padding:12px 0;border-top:1px solid var(--line);font-size:12px}.phone-line strong{overflow-wrap:anywhere}.phone-line>span{color:var(--muted)}.phone-reason{white-space:nowrap;text-overflow:ellipsis;overflow:hidden;margin:7px 0}.hint{color:var(--muted);font-size:12px;line-height:1.6}.stale{color:#b45309}.full-message,dd{overflow-wrap:anywhere;white-space:pre-wrap}details{margin-top:10px}summary{cursor:pointer;color:var(--muted)}dl{display:grid;grid-template-columns:95px minmax(0,1fr);gap:6px}dd{margin:0}
</style>
