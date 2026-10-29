<template>
  <section class="evaluation-panel" data-testid="model-iteration-panel">
    <div class="heading"><h2>评测模型配置</h2><el-button :loading="loading" :disabled="saving || starting" @click="load">刷新配置与服务能力</el-button></div>
    <el-radio-group v-model="executionMode" :disabled="starting" data-testid="evaluation-mode">
      <el-radio-button value="collect_and_evaluate">采集并评测</el-radio-button>
      <el-radio-button value="evaluate_only">已有轨迹单独评测</el-radio-button>
    </el-radio-group>
    <p>{{ executionMode === 'evaluate_only' ? '使用已采集的轨迹创建新的评测运行，不启动手机。更换规则版本可重新评测同一批轨迹，原报告保留。' : '每条用例完成采集后自动评测，手机继续执行下一条。每条轨迹评一次。' }}</p>
    <el-alert v-if="capabilityMessage" :title="capabilityMessage" :type="supported ? 'success' : 'warning'" :closable="false" />
    <el-alert v-if="!loading && !profilesSupported" title="9011 尚未提供规则版本能力，请先升级采集服务。历史结果仍可查看。" type="warning" :closable="false" />
    <el-alert v-if="!loading && profilesSupported && !profiles.length" title="未找到可用规则版本，请在采集服务器配置规则版本后刷新。" type="warning" :closable="false" />
    <el-alert v-if="executionMode === 'evaluate_only' && !loading && !offlineSupported" title="当前 9011 尚未提供独立评测能力，请先完成采集端升级。" type="warning" :closable="false" />
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <label>评测规则版本
      <el-select v-model="selectedProfileId" :disabled="loading || starting || !profilesSupported" placeholder="选择服务器维护的规则版本" data-testid="evaluation-profile">
        <el-option v-for="profile in profiles" :key="profile.id" :value="profile.id" :label="`${profile.name} · ${profile.id}`" />
      </el-select>
    </label>
    <p class="hint">优先按用例编号、任务文本匹配固定清单，未匹配时自动生成；同次异常重试沿用原清单。规则与模型配置随每次运行保存。</p>
    <el-alert v-for="(issue, index) in profileErrors" :key="index" :title="evaluationIssueText(issue)" type="warning" :closable="false" />
    <div class="models">
      <fieldset v-for="role in roles" :key="role">
        <legend>{{ role === 'text' ? '文本评测模型' : '视觉评测模型' }}</legend>
        <label>API 地址<el-input v-model="draft[role].base_url" :disabled="loading || saving || starting" placeholder="https://host/v1/" /></label>
        <label>模型名<el-input v-model="draft[role].model" :disabled="loading || saving || starting" placeholder="服务实际提供的模型名" /></label>
        <label>密钥环境变量名<el-input v-model="draft[role].api_key_env" :disabled="loading || saving || starting" /></label>
      </fieldset>
    </div>
    <p class="hint">密钥由采集服务器的环境变量提供，此处只填写变量名。配置保存后用于新运行，已有运行保留原配置。</p>
    <el-button type="primary" :loading="saving" :disabled="loading || starting" @click="save">保存评测配置</el-button>
    <span v-if="dirty" class="hint"> 配置尚未保存，保存后可开始评估。</span>

    <section v-if="executionMode === 'evaluate_only'" class="offline-source" data-testid="offline-evaluation">
      <h2>选择已有轨迹</h2>
      <el-radio-group v-model="sourceKind" :disabled="starting">
        <el-radio value="run">9011 已有采集运行</el-radio><el-radio value="directory">服务器轨迹目录</el-radio>
      </el-radio-group>
      <label v-if="sourceKind === 'run'">采集运行
        <el-select v-model="sourceRunId" filterable :filter-method="filterSources" :disabled="starting || sourcesLoading || !offlineSupported" placeholder="选择已有采集结果">
          <el-option v-for="source in filteredSources" :key="source.run_id" :value="source.run_id" :label="`${displayId('run', source.run_id)} · ${source.trajectory_count ?? 0} 条轨迹 · ${source.status || ''}`" />
        </el-select>
      </label>
      <p v-if="sourceKind === 'run' && sourceRunId" class="hint">来源运行：<DisplayId kind="run" :id="sourceRunId" /></p>
      <label v-if="sourceKind === 'directory'">轨迹目录<el-input v-model="sourcePath" :disabled="starting || !offlineSupported" placeholder="9011 服务器上的完整轨迹目录" /></label>
      <p v-if="sourceKind === 'directory'" class="hint">目录位于采集服务器，而非当前浏览器所在电脑。{{ allowedRoots.length ? `允许范围：${allowedRoots.join('；')}` : '允许范围由采集服务器配置。' }}</p>
      <el-button :loading="sourcesLoading" :disabled="starting || loading || !offlineSupported" @click="loadSources">刷新可用采集结果</el-button>
      <el-button :loading="previewLoading" :disabled="!canPreview" @click="previewSource">检查轨迹并预览</el-button>
      <el-alert v-if="sourceError" :title="sourceError" type="error" :closable="false" />
      <template v-if="preview">
        <p>已识别 {{ preview.trajectory_count }} 条轨迹。{{ preview.valid ? '轨迹检查通过，可以创建新的评测运行。' : '请处理以下问题后重新预览。' }}</p>
        <el-alert v-for="(issue, index) in preview.errors" :key="index" :title="evaluationIssueText(issue)" type="error" :closable="false" />
        <el-table :data="preview.cases" row-key="source_trajectory_id" border max-height="320" data-testid="evaluation-preview">
          <el-table-column prop="case_id" label="用例编号" min-width="130" />
          <el-table-column prop="task" label="任务目标" min-width="240" show-overflow-tooltip />
          <el-table-column prop="phone_id" label="来源手机" min-width="140" />
        </el-table>
      </template>
      <el-button type="primary" :loading="starting" :disabled="!canStartOffline" class="start-offline" @click="startOffline">创建新的评测运行</el-button>
      <el-alert v-if="startMessage" :title="startMessage" type="success" :closable="false" />
      <el-alert v-if="startError" :title="startError" type="error" :closable="false" />
    </section>

    <div class="heading"><h2>用例评测进度</h2><el-button :disabled="!selectedRun" @click="emit('details')">查看用例明细</el-button></div>
    <el-select :model-value="selectedRun" filterable :filter-method="filterRuns" placeholder="选择评估运行" class="run-picker" :disabled="progressBusy" @update:model-value="selectRun">
      <el-option v-for="run in filteredRuns" :key="run.run_id" :label="`${displayId('run', run.run_id)} · ${evaluationModeLabel(run.execution_mode)} · ${run.evaluation_profile_id || '历史规则'}`" :value="run.run_id" />
    </el-select>
    <p v-if="selectedRun" class="hint">运行：<DisplayId kind="run" :id="selectedRun" /></p>
    <el-button v-if="canCancel" type="danger" plain :loading="cancelling" class="cancel-run" @click="cancelRun">停止本次运行</el-button>
    <el-alert v-if="resultError" :title="resultError" type="warning" :closable="false" />
    <p v-if="!runs.length" class="hint">可上传 17 列用例表开始采集并评测，或选择已有轨迹创建独立评测运行。</p>
    <template v-if="results">
      <p class="hint">方式：{{ evaluationModeLabel(resultMetadata.execution_mode) }} · 规则版本：{{ resultMetadata.evaluation_profile_name || resultMetadata.evaluation_profile_id || '历史配置' }} · 数据来源：<DisplayId v-if="sourceIdentifier(resultMetadata)" kind="run" :id="sourceIdentifier(resultMetadata)" /><template v-else>{{ evaluationSourceLabel(resultMetadata) }}</template></p>
      <div class="progress" data-testid="evaluation-summary">
        <span>总轨迹 {{ results.summary.total }}</span>
        <template v-if="resultMetadata.execution_mode !== 'evaluate_only'"><span>采集完成 {{ results.summary.collection_completed }}</span><span>采集失败 {{ results.summary.collection_failed }}</span></template>
        <span>评测完成 {{ results.summary.evaluation_completed }}</span>
        <span>评测中 {{ results.summary.evaluation_running }}</span>
        <span>待评测 {{ results.summary.evaluation_pending }}</span>
        <span>评测异常 {{ results.summary.evaluation_failed }}</span>
        <strong>成功率 {{ formatSuccessRate(results.summary.success_rate) }}</strong>
      </div>
      <p class="hint">成功率 = success /（success + partial_success + fail）；待评测和技术异常不计入分母。</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { modelIterationApi, newRunRequestId, type EvaluatorConfig, type FactoryRun, type EvaluationExecutionMode, type EvaluationProfile, type EvaluationIssue, type EvaluationSource, type EvaluationSourceInput, type EvaluationPreview } from '@/phoneFactoryApi'
import type { EvaluationProgress } from '@/composables/useEvaluationProgress'
import DisplayId from '@/components/DisplayId.vue'
import { useDisplayIdentifiers } from '@/composables/useDisplayIdentifiers'
import { defaultEvaluatorConfig, formatSuccessRate, evaluationIssueText, evaluationModeLabel, evaluationSourceLabel } from '@/utils/modelIteration'

const props = defineProps<{ runs: FactoryRun[]; progress: EvaluationProgress }>()
const { displayId, matches, resolve } = useDisplayIdentifiers()
const sourceQuery = ref(''), runQuery = ref('')
const filterSources = (query: string) => { sourceQuery.value = query }
const filterRuns = (query: string) => { runQuery.value = query }
const sourceIdentifier = (run: { source?: EvaluationSource }) => run.source?.run_id || run.source?.source_run_id || ''
const filteredRuns = computed(() => props.runs.filter(run => matches(runQuery.value, 'run', run.run_id, '', run.filename || '', run.evaluation_profile_id || '', evaluationModeLabel(run.execution_mode))))
const emit = defineEmits<{ ready: [value: boolean]; config: [value: EvaluatorConfig]; mode: [value: EvaluationExecutionMode]; profile: [value: EvaluationProfile | undefined]; started: [runId: string]; details: [] }>()
const roles = ['text', 'vision'] as const
const draft = reactive<EvaluatorConfig>(defaultEvaluatorConfig())
const saved = ref(''), supported = ref(false), profilesSupported = ref(false), offlineSupported = ref(false), loading = ref(false), saving = ref(false)
const capabilityMessage = ref('正在确认采集服务是否支持评测'), error = ref('')
const executionMode = ref<EvaluationExecutionMode>('collect_and_evaluate')
const profiles = ref<EvaluationProfile[]>([]), profileErrors = ref<EvaluationIssue[]>([]), selectedProfileId = ref('')
const selectedProfile = computed(() => profiles.value.find(item => item.id === selectedProfileId.value))
const { selectedRun, results, resultError, cancelling, canCancel, resultMetadata, busy: progressBusy, selectRun, cancelRun } = props.progress
const sourceKind = ref<'run' | 'directory'>('run'), sourceRunId = ref(''), sourcePath = ref('')
const sources = ref<EvaluationSource[]>([]), allowedRoots = ref<string[]>([]), sourcesLoading = ref(false), sourceError = ref('')
const filteredSources = computed(() => sources.value.filter(source => matches(sourceQuery.value, 'run', source.run_id, '', source.status || '', source.created_at || '')))
const preview = ref<EvaluationPreview | null>(null), previewLoading = ref(false), starting = ref(false), startError = ref(''), startMessage = ref('')
const requestIds = new Map<string, string>()
const dirty = computed(() => JSON.stringify(draft) !== saved.value)
const ready = computed(() => supported.value && profilesSupported.value && !!selectedProfile.value && !dirty.value && !loading.value && !saving.value && !starting.value && roles.every(role => draft[role].base_url && draft[role].model))
const sourceInput = computed<EvaluationSourceInput>(() => sourceKind.value === 'run' ? { source_run_id: sourceRunId.value } : { source_path: sourcePath.value.trim() })
const sourceKey = computed(() => JSON.stringify(sourceInput.value))
const canPreview = computed(() => offlineSupported.value && !loading.value && !starting.value && !previewLoading.value && !!(sourceKind.value === 'run' ? sourceRunId.value : sourcePath.value.trim()))
const canStartOffline = computed(() => executionMode.value === 'evaluate_only' && ready.value && offlineSupported.value && !!preview.value?.valid && preview.value.trajectory_count > 0 && !previewLoading.value)
let disposed = false, previewEpoch = 0
let previewController: AbortController | undefined
watch(ready, value => emit('ready', value), { immediate: true })
watch(executionMode, value => emit('mode', value), { immediate: true })
watch(selectedProfile, value => emit('profile', value), { immediate: true })
watch(sourceKey, () => { previewEpoch++; previewController?.abort(); preview.value = null; previewLoading.value = false; sourceError.value = ''; startError.value = ''; startMessage.value = '' }, { flush: 'sync' })

async function loadSources() {
  if (sourcesLoading.value || starting.value || !offlineSupported.value) return
  sourcesLoading.value = true; sourceError.value = ''
  try {
    const value = await modelIterationApi.evaluationSources()
    if (disposed) return
    sources.value = value.sources; allowedRoots.value = value.allowed_roots
    if (sourceRunId.value && !value.sources.some(item => item.run_id === sourceRunId.value)) sourceRunId.value = ''
  } catch (cause) { if (!disposed) sourceError.value = (cause as Error).message }
  finally { if (!disposed) sourcesLoading.value = false }
}
async function load() {
  if (loading.value || saving.value || starting.value) return
  loading.value = true; error.value = ''
  try {
    const [config, capability] = await Promise.all([modelIterationApi.evaluationConfig(), modelIterationApi.evaluationCapabilities()])
    if (disposed) return
    Object.assign(draft, config); saved.value = JSON.stringify(draft)
    supported.value = capability.supported; profilesSupported.value = !!capability.profiles_supported; offlineSupported.value = !!capability.offline_supported; capabilityMessage.value = capability.message
    emit('config', config)
    profiles.value = []; profileErrors.value = []
    if (profilesSupported.value) {
      const value = await modelIterationApi.evaluationProfiles()
      if (disposed) return
      profiles.value = value.profiles; profileErrors.value = value.errors
      if (!selectedProfile.value) selectedProfileId.value = value.profiles[0]?.id || ''
    }
    await loadSources()
  } catch (cause) { if (!disposed) { supported.value = false; error.value = (cause as Error).message } }
  finally { if (!disposed) loading.value = false }
}
async function save() {
  if (loading.value || saving.value || starting.value) return
  saving.value = true; error.value = ''
  try {
    const config = await modelIterationApi.saveEvaluationConfig(JSON.parse(JSON.stringify(draft)))
    if (disposed) return
    Object.assign(draft, config); saved.value = JSON.stringify(draft); emit('config', config)
  } catch (cause) { if (!disposed) error.value = (cause as Error).message }
  finally { if (!disposed) saving.value = false }
}
async function previewSource() {
  if (!canPreview.value) return
  const epoch = ++previewEpoch, key = sourceKey.value
  previewController?.abort(); previewController = new AbortController()
  previewLoading.value = true; preview.value = null; sourceError.value = ''; startError.value = ''; startMessage.value = ''
  try {
    const value = await modelIterationApi.evaluationPreview(sourceInput.value, previewController.signal)
    if (!disposed && epoch === previewEpoch && key === sourceKey.value) preview.value = value
  } catch (cause) { if (!disposed && epoch === previewEpoch) sourceError.value = (cause as Error).message }
  finally { if (!disposed && epoch === previewEpoch) previewLoading.value = false }
}
async function startOffline() {
  if (!canStartOffline.value || !preview.value || !selectedProfile.value) return
  const parameters = { preview_id: preview.value.preview_id, evaluator_config: JSON.parse(JSON.stringify(draft)) as EvaluatorConfig,
    evaluation_profile_id: selectedProfile.value.id, evaluation_profile_digest: selectedProfile.value.digest, checklist_source: 'auto' as const }
  const key = JSON.stringify(parameters), requestId = requestIds.get(key) || newRunRequestId()
  requestIds.set(key, requestId); starting.value = true; startError.value = ''; startMessage.value = ''
  try {
    const value = await modelIterationApi.remoteStartEvaluation({ ...parameters, request_id: requestId })
    requestIds.delete(key)
    if (disposed) return
    if (value.run_id) await resolve([{ kind: 'run', id: value.run_id }])
    if (disposed) return
    startMessage.value = `已创建评测运行 ${displayId('run', value.run_id)}。本次报告独立保存，原报告保留。`
    if (value.run_id) emit('started', value.run_id)
  } catch (cause) { if (!disposed) startError.value = `${(cause as Error).message}；原参数重试会复用本次请求编号，避免重复创建。` }
  finally { if (!disposed) starting.value = false }
}
onMounted(load)
onBeforeUnmount(() => { disposed = true; previewEpoch++; previewController?.abort() })
</script>

<style scoped>
.evaluation-panel { padding: 22px; margin-bottom: 24px; background: white; border: 1px solid var(--line); border-radius: 16px; }
.heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }.evaluation-panel h2 { font-size: 18px; }
.models { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; margin-top: 18px; }
fieldset { border: 1px solid var(--line); border-radius: 8px; padding: 16px; min-width: 0; }label { display: grid; grid-template-columns: 140px minmax(0, 1fr); align-items: center; gap: 10px; margin: 10px 0; }
.hint { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }.run-picker { width: min(850px, 100%); }.progress { display: flex; flex-wrap: wrap; gap: 12px 22px; margin-top: 20px; }.el-alert { margin-top: 10px; }
.offline-source { margin-top: 24px; padding-top: 8px; border-top: 1px solid var(--line); }.start-offline { margin-top: 16px; }
.cancel-run { margin-left: 10px; }
@media (max-width: 760px) { .models { grid-template-columns: 1fr; }label { grid-template-columns: 1fr; }.heading { flex-wrap: wrap; } }
</style>
