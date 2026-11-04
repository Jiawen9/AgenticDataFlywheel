<script setup lang="ts">
import { computed, reactive } from 'vue'
import type { FactoryRun, DeviceConfigRevision } from '@/phoneFactoryApi'
import { diagnosisAge, type CollectionDiagnosticsState } from '@/composables/useCollectionDiagnostics'
import { diagnosisConfidence, diagnosisPhase, diagnosisStatus, phoneDiagnosisText, resumeStatus } from '@/utils/collectionDiagnostics'
import { factoryRunId, newestFactoryRuns } from '@/utils/factoryHistory'
import { useDisplayIdentifiers } from '@/composables/useDisplayIdentifiers'

const props = defineProps<{ state: CollectionDiagnosticsState; runs: FactoryRun[]; allowResume: boolean; detailed?: boolean }>()
const emit = defineEmits<{ details: [phoneId: string] }>()
const { displayId } = useDisplayIdentifiers()
const selected = computed({ get: () => props.state.selectedRunId.value, set: id => props.state.selectRun(id) })
const data = computed(() => props.state.snapshot.value)
const deviceRevisionSupported = computed(() => data.value?.collection_resume?.device_config_revision === 1)
const ownershipLabel = (value?: string) => ({ free: '已释放', busy: '占用中', unknown: '待核验' })[value || 'unknown'] || '待核验'
function visibleOwners(phone: string) { return (props.state.ownershipFor(phone)?.owners || []).filter(owner => props.detailed || owner.availability !== 'free' || owner.supported_actions?.includes('reconcile')) }
const diagnosticErrors = computed(() => [...new Set([data.value?.persistence_error, data.value?.receipt_storage?.error, data.value?.receipt_storage?.notification_error, data.value?.control_error, data.value?.sync_error].filter(Boolean))] as string[])
const ordered = computed(() => newestFactoryRuns(props.runs))
const age = (time?: string) => diagnosisAge(time, props.state.now.value)
function blocked(phone: string) {
  const item = props.state.operationFor(phone)
  return !!item && ['previewing', 'confirming', 'submitting'].includes(item.phase)
}
const revisions = reactive<Record<string, { enabled: boolean; adb: string; apps: string; error: string }>>({})
function revisionFor(phone: string) { const key = `${selected.value}:${phone}`; return revisions[key] ||= { enabled: false, adb: '', apps: '', error: '' } }
async function continuePhone(phone: string) {
  const draft = revisionFor(phone); draft.error = ''
  let revision: DeviceConfigRevision | undefined
  if (draft.enabled) {
    if (!deviceRevisionSupported.value) { draft.error = '采集端尚未声明设备修订能力，请升级后再修订'; return }
    try {
      const apps = draft.apps.trim() ? JSON.parse(draft.apps) : undefined
      if (apps && (Array.isArray(apps) || typeof apps !== 'object' || Object.values(apps).some(v => typeof v !== 'string'))) throw new Error('App 映射必须是名称到包名的 JSON 对象')
      revision = { ...(draft.adb.trim() ? { adb_path: draft.adb.trim() } : {}), ...(apps ? { app_list: apps } : {}) }
      if (!Object.keys(revision).length) throw new Error('请填写至少一项设备修订，或取消修订选项')
    } catch (cause) { draft.error = (cause as Error).message; return }
  }
  await props.state.continuePhones([phone], revision)
}
function controlBusy(phone: string) { const value = props.state.controlFor(phone); return !!value && (value.sending || !['completed', 'conflict', 'failed', 'rejected'].includes(value.result?.status || '')) }
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
        <div class="phone-actions"><el-button v-if="!detailed" link type="primary" @click="emit('details', phone.phone_id)">查看诊断</el-button><el-button v-if="allowResume && phone.can_resume !== false" link type="primary" :disabled="blocked(phone.phone_id)" @click="continuePhone(phone.phone_id)">{{ state.operationFor(phone.phone_id)?.phase === 'waiting' ? '重试 / 确认续跑状态' : '继续该手机' }}</el-button><span role="status">{{ resumeStatus(state.operationFor(phone.phone_id), phone.phone_id) }}</span></div>
        <div v-if="state.phoneControlSupported.value" class="ownership-panel">
          <p class="hint">当前占用：{{ ({ free: '空闲', busy: '已确认占用', unknown: '尚无法确认' })[state.ownershipFor(phone.phone_id)?.availability || 'unknown'] }}<span v-if="state.ownershipFor(phone.phone_id)?.stale" class="stale"> · 核验已过期</span><el-button link type="primary" @click="state.checkOwnership(phone.phone_id)">核对占用</el-button></p>
          <p v-if="state.ownershipFor(phone.phone_id)?.sync_error" class="stale">{{ state.ownershipFor(phone.phone_id)?.sync_error }}</p>
          <div v-for="owner in visibleOwners(phone.phone_id)" :key="owner.execution_id" class="owner-line">
            <span>{{ owner.availability === 'free' ? '已退出的历史执行' : '当前拥有者' }}：{{ owner.execution_id }} · {{ ownershipLabel(owner.availability) }}</span>
            <el-button v-if="owner.supported_actions?.includes('reconcile')" link :disabled="controlBusy(phone.phone_id) || state.ownershipFor(phone.phone_id)?.stale" @click="state.controlPhone(phone.phone_id, 'reconcile', owner)">对账释放</el-button>
            <el-button v-if="owner.supported_actions?.includes('stop')" link type="danger" :disabled="controlBusy(phone.phone_id) || state.ownershipFor(phone.phone_id)?.stale" @click="state.controlPhone(phone.phone_id, 'stop', owner)">停止该手机</el-button>
            <span v-if="detailed" class="hint">父 PID {{ owner.parent?.pid ?? '未提供' }} · 子 PID {{ owner.child?.pid ?? '未提供' }} · {{ owner.reason_code || '' }}</span>
          </div>
          <p v-if="state.controlFor(phone.phone_id)" role="status" class="hint">手机控制：{{ state.controlFor(phone.phone_id)?.sending ? '正在提交' : diagnosisStatus(state.controlFor(phone.phone_id)?.result?.status || 'uncertain') }} {{ state.controlFor(phone.phone_id)?.error || '' }}</p>
        </div>
        <details v-if="allowResume && state.selectedRun.value?.agent_type === 'pipelineagent' && deviceRevisionSupported" class="device-revision"><summary>修订设备配置（可选）</summary><label><input v-model="revisionFor(phone.phone_id).enabled" type="checkbox" /> 本次续采使用以下设备修订</label><el-input v-model="revisionFor(phone.phone_id).adb" placeholder="ADB 可执行文件绝对路径；留空沿用冻结值" aria-label="修订 ADB 路径" /><el-input v-model="revisionFor(phone.phone_id).apps" type="textarea" placeholder='App 映射 JSON，例如 {"应用名称":"com.example.app"}；留空沿用冻结值' aria-label="修订 App 映射" /><p class="hint">仅作用于新尝试，模型和任务配置保持原冻结值。</p><p v-if="revisionFor(phone.phone_id).error" class="stale">{{ revisionFor(phone.phone_id).error }}</p></details>
        <p v-if="detailed && allowResume && state.selectedRun.value?.agent_type === 'pipelineagent' && !deviceRevisionSupported" class="hint">采集端尚未声明设备配置修订能力；升级后可在同批次续采中修订 ADB 或 App 映射。</p>
        <p v-if="state.operationFor(phone.phone_id)?.error" class="stale">{{ state.operationFor(phone.phone_id)?.error }}</p>
        <details v-if="detailed"><summary>完整诊断与请求记录</summary><p class="full-message">{{ phoneDiagnosisText(phone) }}</p><p v-if="phone.suggestion">{{ phone.suggestion }}</p><dl><dt>判断依据</dt><dd>{{ diagnosisConfidence(phone.confidence) }}</dd><template v-if="phone.io_error"><dt>文件操作</dt><dd>{{ phone.io_error.operation || '未提供' }}</dd><dt>文件路径</dt><dd>{{ phone.io_error.path || '未提供' }}</dd><dt>系统错误码</dt><dd>WinError {{ phone.io_error.winerror ?? '未提供' }} · errno {{ phone.io_error.errno ?? '未提供' }}</dd></template><dt>原因代码</dt><dd>{{ phone.reason_code || '未提供' }}</dd><dt>执行编号</dt><dd>{{ phone.execution_id || '未提供' }}</dd><dt>最近观察时间</dt><dd>{{ phone.observed_at || '未提供' }}</dd><dt>最近活动时间</dt><dd>{{ phone.last_activity_at || '未提供' }}</dd><dt>阶段耗时</dt><dd>{{ phone.phase_elapsed_seconds ?? phone.elapsed_seconds ?? age(phone.phase_started_at) ?? '未提供' }} 秒</dd><template v-if="phone.model_call"><dt>请求接口</dt><dd>{{ phone.model_call.endpoint || '未提供' }}</dd><dt>请求超时</dt><dd>{{ phone.model_call.timeout_seconds ?? '未提供' }} 秒</dd><dt>HTTP 状态</dt><dd>{{ phone.model_call.status_code ?? '未提供' }}</dd></template><dt v-if="state.operationFor(phone.phone_id)">续跑请求</dt><dd v-if="state.operationFor(phone.phone_id)">{{ state.operationFor(phone.phone_id)?.requestId }}</dd></dl><p v-for="(evidence, index) in phone.evidence || []" :key="index" class="full-message">{{ evidence.source }}：{{ evidence.detail }}</p><p v-if="phone.model_call?.error" class="full-message">{{ phone.model_call.error }}</p><p v-if="phone.persistence_error" class="full-message">{{ phone.persistence_error }}</p></details>
      </div>
      <p class="hint">诊断每 5 秒独立同步。续跑保留当前运行和批次，已完成的实例不重复执行。</p>
    </template>
  </section>
</template>

<style scoped>
.owner-line{display:flex;gap:8px;align-items:center;flex-wrap:wrap;overflow-wrap:anywhere}.device-revision .el-input,.device-revision .el-textarea{margin-top:8px}.ownership-panel{padding:6px 10px;background:var(--surface-alt,#f7f8fa);border-radius:6px}.phone-diagnostics{margin-top:18px;border-top:1px solid var(--line);padding-top:14px;min-width:0}.diagnosis-heading,.phone-line,.phone-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.diagnosis-heading{justify-content:space-between}.diagnosis-heading h3{font-size:14px;margin:0 0 10px}.diagnosis-run-select{width:100%;margin-bottom:8px}.diagnosis-phone{padding:12px 0;border-top:1px solid var(--line);font-size:12px}.phone-line strong{overflow-wrap:anywhere}.phone-line>span{color:var(--muted)}.phone-reason{white-space:nowrap;text-overflow:ellipsis;overflow:hidden;margin:7px 0}.hint{color:var(--muted);font-size:12px;line-height:1.6}.stale{color:#b45309}.full-message,dd{overflow-wrap:anywhere;white-space:pre-wrap}details{margin-top:10px}summary{cursor:pointer;color:var(--muted)}dl{display:grid;grid-template-columns:95px minmax(0,1fr);gap:6px}dd{margin:0}
</style>
