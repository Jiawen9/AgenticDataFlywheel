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
      <p class="hint">新独立评测直接只读轨迹原件，不复制截图。评测期间请保留原目录；日志和报告单独保存。</p>
      <p v-if="sourceKind === 'directory'" class="hint">目录位于采集服务器，而非当前浏览器所在电脑。{{ allowedRoots.length ? `允许范围：${allowedRoots.join('；')}` : '允许范围由采集服务器配置。' }}</p>
      <el-button :loading="sourcesLoading" :disabled="starting || loading || !offlineSupported" @click="loadSources">刷新可用采集结果</el-button>
      <el-alert v-if="!loading && !jobsSupported" title="当前采集端尚未支持轨迹后台检查与输入准备，请升级采集端后重试。" type="warning" :closable="false" />
      <el-button v-if="!preview" :loading="previewLoading" :disabled="!canPreview" @click="previewSource()">检查轨迹并预览</el-button>
      <el-button v-if="preview?.status === 'preparing' && preview.job_version === 1" :loading="controllingPreview" :disabled="controllingPreview || previewControlPending || starting" @click="controlPreview('cancel')">取消检查</el-button>
      <el-button v-if="preview?.status === 'failed' && preview.job_version === 1 && preview.retryable !== false" :loading="controllingPreview" :disabled="controllingPreview || previewControlPending || starting" @click="controlPreview('retry')">重试检查</el-button>
      <el-button v-if="preview && preview.status !== 'preparing'" :disabled="!canPreview || controllingPreview || previewControlPending" @click="previewSource(true)">重新检查</el-button>
      <el-alert v-if="preview?.control_error" :title="preview.control_error" type="warning" :closable="false" />
      <el-alert v-if="sourceError" :title="sourceError" type="error" :closable="false" />
      <template v-if="preview">
        <p v-if="collectionSnapshotLabel(preview)" class="hint" data-testid="stale-evaluation-preview">{{ collectionSnapshotLabel(preview) }}。此预览使用旧采集快照；重新检查可获取当前结果。</p>
        <p v-if="previewPending(preview)" data-testid="preview-job-progress">{{ evaluationJobPhase(preview.phase, preview.status) }}<span v-if="evaluationJobCounts(preview.progress)"> · {{ evaluationJobCounts(preview.progress) }}</span>。检查在后台继续，不操作手机。</p>
        <p v-else-if="preview.status === 'cancelled'">本次检查已取消，原始轨迹保留。</p>
        <p v-else-if="preview.status === 'failed'">检查未完成：{{ preview.error || '请查看错误原因，修复后重试。' }}</p>
        <p v-else>可评测 {{ preview.trajectory_count }} 条<template v-if="preview.coverage"> / 共 {{ preview.coverage.total }} 条，排除 {{ preview.coverage.excluded }} 条</template>。{{ preview.valid ? '将评测全部可用轨迹，报告保留排除原因。' : '没有可评测轨迹，请查看排除原因。' }}</p>
        <details v-if="preview.errors.length"><summary>查看排除原因（摘要最多 100 条）</summary><el-alert v-for="(issue, index) in preview.errors" :key="index" :title="evaluationIssueText(issue)" type="warning" :closable="false" /></details>
        <p v-if="preview.sync_error" class="hint">{{ preview.sync_error }}，正在重试同步。</p>
        <el-table :data="preview.cases.slice(0, previewPageSize)" row-key="source_trajectory_id" border max-height="320" data-testid="evaluation-preview">
          <el-table-column prop="case_id" label="用例编号" min-width="130" />
          <el-table-column prop="task" label="任务目标" min-width="240" show-overflow-tooltip />
          <el-table-column prop="phone_id" label="来源手机" min-width="140" />
        </el-table>
        <el-pagination v-if="preview.valid" v-model:current-page="previewPage" v-model:page-size="previewPageSize" :page-sizes="[10, 20, 50]" :total="preview.trajectory_count" layout="total, sizes, prev, pager, next" @current-change="loadPreviewPage" @size-change="resetPreviewPage" />
      </template>
      <el-button type="primary" :loading="starting" :disabled="!canStartOffline" class="start-offline" @click="startOffline">创建新的评测运行</el-button>
      <el-button v-if="pendingStart" :loading="starting" :disabled="starting || loading" @click="recoverStart">确认上次评测提交</el-button>
      <p v-if="pendingStart" class="hint">上次提交尚未确认；将使用当时保存的模型、规则和请求编号查询或重放，不创建重复运行。</p>
      <el-alert v-if="startMessage" :title="startMessage" type="success" :closable="false" />
      <el-alert v-if="startError" :title="startError" type="error" :closable="false" />
    </section>

    <div class="heading"><h2>用例评测进度</h2><el-button :disabled="!selectedRun" @click="emit('details')">查看用例明细</el-button></div>
    <el-select :model-value="selectedRun" filterable :filter-method="filterRuns" placeholder="选择评估运行" class="run-picker" :disabled="progressBusy" @update:model-value="selectRun">
      <el-option v-for="run in filteredRuns" :key="run.run_id" :label="`${displayId('run', run.run_id)} · ${evaluationModeLabel(run.execution_mode)} · ${run.evaluation_profile_id || '历史规则'}${run.stale_collection_snapshot ? ' · 旧采集快照' : ''}`" :value="run.run_id" />
    </el-select>
    <p v-if="selectedRun" class="hint">运行：<DisplayId kind="run" :id="selectedRun" /></p>
    <p v-if="collectionSnapshotLabel(resultMetadata)" class="hint" data-testid="stale-evaluation-run">{{ collectionSnapshotLabel(resultMetadata) }}。此评测使用旧采集快照，结果与报告保持原样。</p>
    <CollectionRunProgress :run="resultMetadata" />
    <p v-if="resultMetadata.preparation" data-testid="evaluation-preparation-progress">{{ evaluationJobPhase(resultMetadata.preparation.phase, resultMetadata.preparation.status) }}<span v-if="evaluationJobCounts(resultMetadata.preparation.progress)"> · {{ evaluationJobCounts(resultMetadata.preparation.progress) }}</span></p>
    <el-alert v-if="resultMetadata.preparation?.error" :title="resultMetadata.preparation.error" type="warning" :closable="false" />
    <el-alert v-if="resultMetadata.preparation_control_error" :title="resultMetadata.preparation_control_error" type="warning" :closable="false" />
    <p v-if="resultMetadata.preparation_control_pending" class="hint">输入准备重试请求已受理，正在同步后台状态。</p>
    <el-button v-if="canRetryPreparation" :loading="retryingPreparation" :disabled="progressBusy" @click="retryPreparation">重试输入准备</el-button>
    <p v-if="resultMetadata.batch_id && resultMetadata.execution_mode !== 'evaluate_only'" class="hint">采集结果批次：<DisplayId kind="batch" :id="resultMetadata.batch_id" /> · 采集、回传和评测分别更新。</p>
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
      <p v-if="results.coverage" class="hint">评测覆盖 {{ results.coverage.included }}/{{ results.coverage.total }}，排除 {{ results.coverage.excluded }} 条。排除项与证据警告随本次报告保存。</p>
      <details v-if="results.excluded?.length"><summary>查看排除原因</summary><p v-for="(issue, index) in results.excluded" :key="index" class="hint">{{ evaluationIssueText(issue) }}</p></details>
      <p class="hint">成功率 = success /（success + partial_success + fail）；待评测和技术异常不计入分母。</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { modelIterationApi, newRunRequestId, type EvaluatorConfig, type FactoryRun, type EvaluationExecutionMode, type EvaluationProfile, type EvaluationIssue, type EvaluationSource, type EvaluationSourceInput, type EvaluationPreview, type StartEvaluationOptions } from '@/phoneFactoryApi'
import { evaluationPreviewSession, evaluationSourceKey, evaluationJobPhase, evaluationJobCounts, type PreviewSessionEntry } from '@/utils/evaluationJobs'
import type { EvaluationProgress } from '@/composables/useEvaluationProgress'
import DisplayId from '@/components/DisplayId.vue'
import CollectionRunProgress from '@/components/CollectionRunProgress.vue'
import { useDisplayIdentifiers } from '@/composables/useDisplayIdentifiers'
import { defaultEvaluatorConfig, formatSuccessRate, evaluationIssueText, evaluationModeLabel, evaluationSourceLabel, collectionSnapshotLabel } from '@/utils/modelIteration'

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
const saved = ref(''), supported = ref(false), profilesSupported = ref(false), offlineSupported = ref(false), jobsSupported = ref(false), loading = ref(false), saving = ref(false)
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
const session = evaluationPreviewSession()
const controllingPreview = ref(false), retryingPreparation = ref(false), sessionRevision = ref(0)
const preparationRequestIds = new Map<string, string>()
const dirty = computed(() => JSON.stringify(draft) !== saved.value)
const ready = computed(() => supported.value && profilesSupported.value && !!selectedProfile.value && !dirty.value && !loading.value && !saving.value && !starting.value && roles.every(role => draft[role].base_url && draft[role].model))
const sourceInput = computed<EvaluationSourceInput>(() => sourceKind.value === 'run' ? { source_run_id: sourceRunId.value } : { source_path: sourcePath.value.trim() })
const sourceKey = computed(() => evaluationSourceKey(sourceInput.value))
const pendingStart = computed(() => { void sessionRevision.value; return session.get(sourceKey.value)?.start_request })
const previewControlPending = computed(() => ['cancelling', 'retrying'].includes(preview.value?.phase || ''))
const previewPending = (value: EvaluationPreview) => value.status === 'preparing' || ['cancelling', 'retrying'].includes(value.phase || '')
const canRetryPreparation = computed(() => !resultMetadata.value.preparation_control_pending && resultMetadata.value.execution_mode === 'evaluate_only' && ['failed', 'paused'].includes(resultMetadata.value.preparation?.status || '') && resultMetadata.value.preparation?.retryable === true)
const canPreview = computed(() => offlineSupported.value && jobsSupported.value && !pendingStart.value && !loading.value && !starting.value && !previewLoading.value && !!(sourceKind.value === 'run' ? sourceRunId.value : sourcePath.value.trim()))
const canStartOffline = computed(() => executionMode.value === 'evaluate_only' && !pendingStart.value && ready.value && offlineSupported.value && !!preview.value?.valid && preview.value.trajectory_count > 0 && !previewLoading.value)
const previewPage = ref(1), previewPageSize = ref(10)
let previewPageEpoch = 0
let disposed = false, previewEpoch = 0
let previewController: AbortController | undefined
const restored = session.selected()
if (restored) {
  executionMode.value = 'evaluate_only'
  sourceKind.value = restored.source.source_path !== undefined ? 'directory' : 'run'
  sourcePath.value = restored.source.source_path || ''
  sourceRunId.value = restored.source.source_run_id || ''
}
function remember(key: string, entry: PreviewSessionEntry) { session.put(key, entry, key === sourceKey.value); sessionRevision.value++ }
function acceptPreview(value: EvaluationPreview) {
  const current = preview.value
  if (current?.preview_id === value.preview_id && typeof current.revision === 'number' && typeof value.revision === 'number' && value.revision < current.revision) return false
  preview.value = value
  return true
}
watch(ready, value => emit('ready', value), { immediate: true })
watch(executionMode, value => emit('mode', value), { immediate: true })
watch(selectedProfile, value => emit('profile', value), { immediate: true })
watch(sourceKey, () => { previewPageEpoch++; previewPage.value = 1; previewEpoch++; clearTimeout(previewTimer); previewController?.abort(); preview.value = null; previewLoading.value = false; sourceError.value = ''; startError.value = ''; startMessage.value = '' }, { flush: 'sync' })

async function loadSources() {
  if (sourcesLoading.value || starting.value || !offlineSupported.value) return
  sourcesLoading.value = true; sourceError.value = ''
  try {
    const value = await modelIterationApi.evaluationSources()
    if (disposed) return
    sources.value = value.sources; allowedRoots.value = value.allowed_roots
    if (sourceRunId.value && !session.get(sourceKey.value) && !value.sources.some(item => item.run_id === sourceRunId.value)) sourceRunId.value = ''
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
    supported.value = capability.supported; profilesSupported.value = !!capability.profiles_supported; offlineSupported.value = !!capability.offline_supported; jobsSupported.value = capability.offline_evaluation_jobs?.version === 1 || capability.jobs_supported === true; capabilityMessage.value = capability.message
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
async function previewSource(forceNew = false) {
  if (!canPreview.value) return
  const epoch = ++previewEpoch, key = sourceKey.value
  previewPageEpoch++; previewPage.value = 1
  clearTimeout(previewTimer)
  previewController?.abort(); previewController = new AbortController()
  previewLoading.value = true; preview.value = null; sourceError.value = ''; startError.value = ''; startMessage.value = ''
  const old = session.get(key)
  const entry: PreviewSessionEntry = !forceNew && old ? old : { source: { ...sourceInput.value }, request_id: newRunRequestId(), updated_at: Date.now() }
  if (jobsSupported.value) remember(key, entry)
  try {
    const value = await modelIterationApi.evaluationPreview({ ...entry.source, ...(jobsSupported.value ? { request_id: entry.request_id } : {}) }, previewController.signal)
    const latest = session.get(key)
    if (!disposed && jobsSupported.value && latest?.request_id === entry.request_id) remember(key, { ...latest, preview_id: value.preview_id })
    if (!disposed && epoch === previewEpoch && key === sourceKey.value) {
      preview.value = value
      if (previewPending(value)) schedulePreview(epoch, key, value.preview_id)
    }
  } catch (cause) {
    if (!disposed && epoch === previewEpoch) sourceError.value = `${(cause as Error).message}${jobsSupported.value ? '；再次检查会复用本次请求编号，确认后台受理结果。' : ''}`
  } finally { if (!disposed && epoch === previewEpoch) previewLoading.value = !!preview.value && previewPending(preview.value) }
}
let previewTimer: ReturnType<typeof setTimeout> | undefined
function schedulePreview(epoch: number, key: string, id: string) {
  clearTimeout(previewTimer)
  previewTimer = setTimeout(() => void refreshPreview(epoch, key, id), 2000)
}
async function refreshPreview(epoch: number, key: string, id: string) {
  if (disposed || epoch !== previewEpoch || key !== sourceKey.value) return
  try {
    const value = await modelIterationApi.evaluationPreviewState(id, previewController?.signal, (previewPage.value - 1) * previewPageSize.value, previewPageSize.value)
    if (disposed || epoch !== previewEpoch || key !== sourceKey.value) return
    if (!acceptPreview(value)) {
      if (preview.value && previewPending(preview.value)) schedulePreview(epoch, key, id)
      return
    }
    sourceError.value = ''
    previewLoading.value = previewPending(value)
    if (previewPending(value)) schedulePreview(epoch, key, id)
  } catch (cause) {
    if (disposed || epoch !== previewEpoch || key !== sourceKey.value) return
    sourceError.value = `预览同步暂不可用：${(cause as Error).message}；保留本次任务并继续查询。`
    schedulePreview(epoch, key, id)
  }
}
async function restorePreview() {
  const entry = session.get(sourceKey.value)
  if (!entry || !offlineSupported.value || (sourceKind.value === 'directory' && !jobsSupported.value)) return
  if (!entry.preview_id) { await previewSource(); return }
  previewController?.abort(); previewController = new AbortController()
  previewLoading.value = true
  await refreshPreview(++previewEpoch, sourceKey.value, entry.preview_id)
}
async function controlPreview(action: 'cancel' | 'retry') {
  const current = preview.value, key = sourceKey.value, epoch = previewEpoch
  if (!current || previewControlPending.value || current.job_version !== 1 || typeof current.revision !== 'number' || controllingPreview.value) return
  const entry = session.get(key)
  if (!entry) return
  const request = entry.control?.action === action ? entry.control : { action, request_id: newRunRequestId(), expected_revision: current.revision }
  remember(key, { ...entry, control: request }); controllingPreview.value = true
  try {
    const value = await modelIterationApi.evaluationPreviewControl(current.preview_id, action, { request_id: request.request_id, expected_revision: request.expected_revision })
    const latest = session.get(key)
    if (!disposed && latest?.control?.request_id === request.request_id) remember(key, { ...latest, control: undefined })
    if (!disposed && epoch === previewEpoch && key === sourceKey.value) {
      acceptPreview(value); previewLoading.value = !!preview.value && previewPending(preview.value); sourceError.value = ''
      if (preview.value && previewPending(preview.value)) schedulePreview(epoch, key, current.preview_id)
      else clearTimeout(previewTimer)
    }
  } catch (cause) {
    if (!disposed && epoch === previewEpoch) {
      sourceError.value = `${(cause as Error).message}；保留操作编号，请重试确认。`
      if ((cause as Error & { status?: number }).status === 409) {
        const latest = session.get(key)
        if (latest?.control?.request_id === request.request_id) remember(key, { ...latest, control: undefined })
        await refreshPreview(epoch, key, current.preview_id)
      }
    }
  } finally { if (!disposed) controllingPreview.value = false }
}
async function loadPreviewPage() {
  if (!preview.value?.valid) return
  const id = preview.value.preview_id, epoch = ++previewPageEpoch, sourceEpoch = previewEpoch
  try {
    const value = await modelIterationApi.evaluationPreviewState(id, previewController?.signal, (previewPage.value - 1) * previewPageSize.value, previewPageSize.value)
    if (!disposed && epoch === previewPageEpoch && sourceEpoch === previewEpoch && preview.value?.preview_id === id) acceptPreview(value)
  } catch (cause) { if (!disposed && epoch === previewPageEpoch && sourceEpoch === previewEpoch) sourceError.value = (cause as Error).message }
}
function resetPreviewPage() { previewPage.value = 1; void loadPreviewPage() }
async function startOffline() {
  if (!canStartOffline.value || !preview.value || !selectedProfile.value) return
  const parameters = { preview_id: preview.value.preview_id, evaluator_config: JSON.parse(JSON.stringify(draft)) as EvaluatorConfig,
    evaluation_profile_id: selectedProfile.value.id, evaluation_profile_digest: selectedProfile.value.digest, checklist_source: 'auto' as const }
  const key = JSON.stringify(parameters), requestId = requestIds.get(key) || newRunRequestId()
  requestIds.set(key, requestId)
  await submitEvaluation({ ...parameters, request_id: requestId })
}
async function recoverStart() {
  if (!pendingStart.value || starting.value) return
  await submitEvaluation(pendingStart.value)
}
async function submitEvaluation(parameters: StartEvaluationOptions) {
  const key = sourceKey.value, entry = session.get(key)
  if (entry) remember(key, { ...entry, start_request: parameters })
  starting.value = true; startError.value = ''; startMessage.value = ''
  try {
    const value = await modelIterationApi.remoteStartEvaluation(parameters)
    const latest = session.get(key)
    if (!disposed && latest?.start_request?.request_id === parameters.request_id) remember(key, { ...latest, start_request: undefined })
    for (const [name, id] of requestIds) if (id === parameters.request_id) requestIds.delete(name)
    if (disposed) return
    if (value.run_id) await resolve([{ kind: 'run', id: value.run_id }])
    if (disposed) return
    startMessage.value = `已创建评测运行 ${displayId('run', value.run_id)}。输入准备在后台进行，本次报告独立保存。`
    if (value.run_id) emit('started', value.run_id)
  } catch (cause) { if (!disposed) startError.value = `${(cause as Error).message}；原参数重试会复用本次请求编号，避免重复创建。` }
  finally { if (!disposed) starting.value = false }
}
async function retryPreparation() {
  const id = selectedRun.value, preparation = resultMetadata.value.preparation
  if (!id || !canRetryPreparation.value || typeof preparation?.revision !== 'number' || retryingPreparation.value) return
  const key = `${id}:${preparation.revision}`, requestId = preparationRequestIds.get(key) || newRunRequestId()
  preparationRequestIds.set(key, requestId); retryingPreparation.value = true
  try {
    await modelIterationApi.retryEvaluationPreparation(id, { request_id: requestId, expected_revision: preparation.revision })
    if (!disposed) { preparationRequestIds.delete(key); if (id === selectedRun.value) resultError.value = ''; emit('started', id) }
  } catch (cause) { if (!disposed && id === selectedRun.value) resultError.value = `输入准备重试尚未确认：${(cause as Error).message}` }
  finally { if (!disposed) retryingPreparation.value = false }
}
onMounted(async () => { await load(); if (!disposed && restored) await restorePreview() })
onBeforeUnmount(() => { clearTimeout(previewTimer); disposed = true; previewEpoch++; previewController?.abort() })
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
