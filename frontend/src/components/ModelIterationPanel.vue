<template>
  <section class="evaluation-panel" data-testid="model-iteration-panel">
    <div class="heading"><h2>评测模型配置</h2><el-button :loading="loading" :disabled="saving" @click="load">刷新配置与服务能力</el-button></div>
    <p>每条用例完成采集后自动评测，手机继续执行下一条。每个新运行自动生成检查清单；同次重试沿用原清单。</p>
    <el-alert v-if="capabilityMessage" :title="capabilityMessage" :type="supported ? 'success' : 'warning'" :closable="false" />
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <div class="models">
      <fieldset v-for="role in roles" :key="role">
        <legend>{{ role === 'text' ? '文本评测模型' : '视觉评测模型' }}</legend>
        <label>API 地址<el-input v-model="draft[role].base_url" :disabled="loading || saving" placeholder="https://host/v1/" /></label>
        <label>模型名<el-input v-model="draft[role].model" :disabled="loading || saving" placeholder="服务实际提供的模型名" /></label>
        <label>密钥环境变量名<el-input v-model="draft[role].api_key_env" :disabled="loading || saving" /></label>
      </fieldset>
    </div>
    <p class="hint">密钥由采集服务器的环境变量提供，此处只填写变量名。配置保存后用于新运行，已有运行保留原配置。</p>
    <el-button type="primary" :loading="saving" :disabled="loading" @click="save">保存评测配置</el-button>
    <span v-if="dirty" class="hint"> 配置尚未保存，保存后可开始评估。</span>
    <h2>用例评测进度</h2>
    <el-select v-model="selectedRun" placeholder="选择评估运行" class="run-picker">
      <el-option v-for="run in runs" :key="run.run_id" :label="`${run.run_id} · ${run.filename || ''}`" :value="run.run_id" />
    </el-select>
    <el-alert v-if="resultError" :title="resultError" type="warning" :closable="false" />
    <p v-if="!runs.length" class="hint">上传现有 17 列模板的用例文件，选择手机和 VLA 后开始运行。</p>
    <template v-if="results">
      <div class="progress" data-testid="evaluation-summary">
        <span>总用例 {{ results.summary.total }}</span>
        <span>采集完成 {{ results.summary.collection_completed }}</span>
        <span>采集失败 {{ results.summary.collection_failed }}</span>
        <span>评测完成 {{ results.summary.evaluation_completed }}</span>
        <span>评测中 {{ results.summary.evaluation_running }}</span>
        <span>待评测 {{ results.summary.evaluation_pending }}</span>
        <span>评测异常 {{ results.summary.evaluation_failed }}</span>
        <strong>成功率 {{ formatSuccessRate(results.summary.success_rate) }}</strong>
      </div>
      <p class="hint">成功率 = success /（success + partial_success + fail）；待评测和技术异常不计入分母。</p>
      <el-table :data="results.cases" row-key="case_key" border data-testid="evaluation-cases">
        <el-table-column prop="phone_id" label="手机" min-width="140" />
        <el-table-column prop="case_id" label="用例" min-width="130" />
        <el-table-column label="采集状态" min-width="110"><template #default="{ row }">{{ collectionStatus(row.collection_status) }}</template></el-table-column>
        <el-table-column label="评测状态" min-width="110"><template #default="{ row }">{{ evaluationStatus(row.evaluation_status) }}</template></el-table-column>
        <el-table-column label="结论" min-width="110"><template #default="{ row }">{{ verdictLabel(row.verdict) }}</template></el-table-column>
        <el-table-column prop="failure_reason" label="原因" min-width="220" show-overflow-tooltip />
        <el-table-column prop="attempt_count" label="评测次数" width="100" />
        <el-table-column label="报告" width="110"><template #default="{ row }"><el-button v-if="row.report_file_id" link :loading="downloading === row.case_key" @click="download(row)">下载 JSON</el-button></template></el-table-column>
      </el-table>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { modelIterationApi, type EvaluatorConfig, type EvaluationCase, type EvaluationResults, type FactoryRun } from '@/phoneFactoryApi'
import { useFactoryPolling } from '@/composables/useFactoryPolling'
import { defaultEvaluatorConfig, formatSuccessRate, evaluationStatus, collectionStatus, verdictLabel } from '@/utils/modelIteration'

const props = defineProps<{ runs: FactoryRun[] }>()
const emit = defineEmits<{ ready: [value: boolean]; config: [value: EvaluatorConfig] }>()
const roles = ['text', 'vision'] as const
const draft = reactive<EvaluatorConfig>(defaultEvaluatorConfig())
const saved = ref(''), supported = ref(false), loading = ref(false), saving = ref(false)
const capabilityMessage = ref('正在确认采集服务是否支持异步评测'), error = ref('')
const selectedRun = ref(''), results = ref<EvaluationResults | null>(null), resultError = ref(''), downloading = ref('')
const dirty = computed(() => JSON.stringify(draft) !== saved.value)
const ready = computed(() => supported.value && !dirty.value && !loading.value && !saving.value && roles.every(role => draft[role].base_url && draft[role].model))
let disposed = false
watch(ready, value => emit('ready', value), { immediate: true })
watch(() => props.runs.map(run => run.run_id), ids => {
  if (!ids.includes(selectedRun.value)) selectedRun.value = ids.at(-1) || ''
}, { immediate: true })
watch(selectedRun, () => { results.value = null; resultError.value = '' })
async function load() {
  if (loading.value || saving.value) return
  loading.value = true; error.value = ''
  try {
    const [config, capability] = await Promise.all([modelIterationApi.evaluationConfig(), modelIterationApi.evaluationCapabilities()])
    if (disposed) return
    Object.assign(draft, config); saved.value = JSON.stringify(draft)
    supported.value = capability.supported; capabilityMessage.value = capability.message
    emit('config', config)
  } catch (cause) { if (!disposed) { supported.value = false; error.value = (cause as Error).message } }
  finally { if (!disposed) loading.value = false }
}
async function save() {
  if (loading.value || saving.value) return
  saving.value = true; error.value = ''
  try {
    const config = await modelIterationApi.saveEvaluationConfig(JSON.parse(JSON.stringify(draft)))
    if (disposed) return
    Object.assign(draft, config); saved.value = JSON.stringify(draft); emit('config', config)
  } catch (cause) { if (!disposed) error.value = (cause as Error).message }
  finally { if (!disposed) saving.value = false }
}
useFactoryPolling(async (signal, current) => {
  const run = selectedRun.value
  if (!run) return
  try {
    const value = await modelIterationApi.evaluations(run, signal)
    if (current() && run === selectedRun.value) { results.value = value; resultError.value = value.stale ? `显示上次结果：${value.sync_error || '同步暂不可用'}` : '' }
  } catch (cause) { if (current() && run === selectedRun.value) resultError.value = (cause as Error).message }
})
async function download(row: EvaluationCase) {
  const run = selectedRun.value
  if (!row.report_file_id || downloading.value) return
  downloading.value = row.case_key
  try {
    const name = row.report_name || 'evaluation-report.json'
    const blob = await modelIterationApi.reportDownload({ index: 0, dir_name: run, run_id: run, files: [] }, { file_id: row.report_file_id, name, size: 0 })
    if (disposed) return
    const url = URL.createObjectURL(blob), link = document.createElement('a')
    link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 0)
  } catch (cause) { if (!disposed) resultError.value = (cause as Error).message }
  finally { if (!disposed) downloading.value = '' }
}
onMounted(load)
onBeforeUnmount(() => { disposed = true })
</script>

<style scoped>
.evaluation-panel { padding: 22px; margin-bottom: 24px; background: white; border: 1px solid var(--line); border-radius: 16px; }
.heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }.evaluation-panel h2 { font-size: 18px; }
.models { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; margin-top: 18px; }
fieldset { border: 1px solid var(--line); border-radius: 8px; padding: 16px; min-width: 0; }label { display: grid; grid-template-columns: 140px minmax(0, 1fr); align-items: center; gap: 10px; margin: 10px 0; }
.hint { color: var(--muted); font-size: 13px; }.run-picker { width: min(650px, 100%); }.progress { display: flex; flex-wrap: wrap; gap: 12px 22px; margin-top: 20px; }.el-alert { margin-top: 10px; }
@media (max-width: 760px) { .models { grid-template-columns: 1fr; }label { grid-template-columns: 1fr; }.heading { flex-wrap: wrap; } }
</style>
