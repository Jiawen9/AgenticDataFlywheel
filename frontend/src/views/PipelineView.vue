<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Collection, Cpu, DataAnalysis, Plus, Promotion, Refresh, TrendCharts, Upload, WarningFilled } from '@element-plus/icons-vue'
import { api } from '@/api'
import RolloutImportDialog from '@/components/RolloutImportDialog.vue'
import type { RolloutImportResult } from '@/rolloutImportApi'
import { PIPELINE_DIAGRAM_STAGES, diagramNodeState, diagramStageState, diagramStageTarget, type DiagramStage, type DiagramNodeState } from '@/utils/pipelineDiagram'
import { factoryBatchesApi, newRunRequestId, phoneFactoryApi, type FactoryState } from '@/phoneFactoryApi'
import { usePipelineContext } from '@/composables/usePipelineContext'
import { activeBatchItems, subscribeBatchLifecycle } from '@/utils/batchLifecycle'
import { PIPELINE_TERMINAL, pipelineStatusLabels, pipelineStepLabels, pipelineStepLocation, type CreatePipeline, type Pipeline, type PipelineAction, type PipelineStepId } from '@/types/pipeline'
const route = useRoute(), router = useRouter(), context = usePipelineContext()
const pipeline = context.pipeline, history = ref<Pipeline[]>([])
const error = ref(''), createError = ref(''), loading = ref(false), submitting = ref(false), controlling = ref(false), dialogVisible = ref(false), choicesLoading = ref(false)
const batches = ref<Array<{ batch_id: string; label: string; collectible: boolean; task_count: number }>>([])
const factory = ref<FactoryState>({ phones: [], apps: [], phoneApps: [], vla: [], tasks: [] })
const rolloutImportVisible = ref(false)
const draft = reactive({ name: '', batch_id: '', start_mode: 'existing' as 'existing' | 'collect', mode: 'manual' as 'manual' | 'automatic', threshold: 3, phone_id: '', app: '', vla: '', sampling_enabled: false, temperature: 1, top_p: 1, use_experience_lib: false })
let disposed = false, listGeneration = 0, choicesGeneration = 0, listController: AbortController | undefined
const requests = new Map<string, string>()
const icons = { TrendCharts, DataAnalysis, Collection, Upload, Cpu, Promotion }
const stages = computed(() => PIPELINE_DIAGRAM_STAGES.map(stage => ({
  ...stage, state: diagramStageState(pipeline.value, stage),
  children: stage.children.map(node => ({ ...node, state: diagramNodeState(pipeline.value, node) })),
})))
const auxiliarySteps: Array<{ id: PipelineStepId; label: string }> = [
  { id: 'correction', label: '人工修正' }, { id: 'cot', label: 'COT' }, { id: 'overview', label: '看板汇总' },
]
const currentStep = computed(() => pipeline.value?.steps.find(step => step.id === pipeline.value?.current_step))
const currentDetails = computed(() => {
  const nodes = stages.value.flatMap(stage => stage.children).filter(node => node.stepId === currentStep.value?.id)
  const activeNodes = nodes.filter(node => node.state.active)
  return [...new Set((activeNodes.length ? activeNodes : nodes).map(node => node.state.detail))].join('；') || currentStep.value?.message || ''
})
const runSignal = computed(() => {
  const status = pipeline.value?.status
  if (status === 'succeeded') return 'completed'
  if (status === 'failed' || status === 'published_summary_failed') return 'failed'
  if (status === 'paused' || status === 'waiting_for_correction') return 'paused'
  if (status === 'running' || status === 'terminating') return 'running'
  return 'pending'
})
function visualClass(state: DiagramNodeState) {
  if (state.status === 'running' && !state.active) return 'is-pending'
  return 'is-' + state.status
}
function openStage(stage: DiagramStage) {
  const target = diagramStageTarget(pipeline.value, stage)
  if (target) openStep(target)
}
function percentage(value: number | undefined) { return Number.isFinite(value) ? Math.max(0, Math.min(100, value!)) : undefined }
const choices = computed(() => activeBatchItems(batches.value).filter(batch => draft.start_mode !== 'collect' || batch.collectible))
const apps = computed(() => draft.phone_id ? factory.value.phoneApps.filter(row => row.phone_id === draft.phone_id).map(row => row.app) : factory.value.apps)
const canSubmit = computed(() => Boolean(draft.name.trim() && draft.batch_id && choices.value.some(batch => batch.batch_id === draft.batch_id) && (draft.mode !== 'automatic' || (Number.isFinite(draft.threshold) && draft.threshold >= 0 && draft.threshold <= 5)) && (draft.start_mode !== 'collect' || draft.vla.trim())))
const done = computed(() => pipeline.value?.steps.filter(step => ['succeeded', 'skipped'].includes(step.status)).length || 0)
const currentError = computed(() => error.value || context.error.value || pipeline.value?.error || '')
function state(id: PipelineStepId) { return pipeline.value?.steps.find(step => step.id === id) }
function openStep(id: PipelineStepId) { if (pipeline.value) void router.push(pipelineStepLocation(pipeline.value, id)) }
function selectPipeline(id: string) { void router.replace({ path: '/pipeline', query: { pipeline_id: id } }) }
function date(value: string) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—' }
async function loadHistory() {
  const generation = ++listGeneration
  listController?.abort(); listController = new AbortController(); loading.value = true
  try {
    const rows = await api.listPipelines({}, listController.signal)
    if (disposed || generation !== listGeneration) return
    history.value = rows; error.value = ''
    if (!route.query.pipeline_id && rows.length) selectPipeline(rows[0]!.pipeline_id)
  } catch (cause) { if (!disposed && generation === listGeneration && (cause as Error).name !== 'AbortError') error.value = (cause as Error).message }
  finally { if (!disposed && generation === listGeneration) loading.value = false }
}
async function loadChoices(hydrateConfig = true) {
  const generation = ++choicesGeneration; choicesLoading.value = true
  const [collection, prepared, state, config] = await Promise.allSettled([factoryBatchesApi.list(), api.preprocessingBatches(), phoneFactoryApi.state(), phoneFactoryApi.config()])
  if (disposed || generation !== choicesGeneration) return
  const options = new Map<string, { batch_id: string; label: string; collectible: boolean; task_count: number }>()
  if (collection.status === 'fulfilled') for (const batch of collection.value) options.set(batch.batch_id, { batch_id: batch.batch_id, label: batch.batch_id, collectible: true, task_count: batch.task_count })
  if (prepared.status === 'fulfilled') for (const batch of prepared.value) options.set(batch.batch_id, { batch_id: batch.batch_id, label: batch.label || batch.batch_id, collectible: options.get(batch.batch_id)?.collectible || false, task_count: batch.task_count })
  batches.value = activeBatchItems([...options.values()])
  if (state.status === 'fulfilled') { factory.value = state.value; if (hydrateConfig && !draft.vla) draft.vla = state.value.vla[0] || '' }
  if (hydrateConfig && config.status === 'fulfilled') Object.assign(draft, config.value)
  if (collection.status === 'rejected' && prepared.status === 'rejected') createError.value = '批次列表读取失败，请关闭弹窗后重试'
  else if (state.status === 'rejected' || config.status === 'rejected') createError.value = '手机工厂配置读取失败；可接续已有结果，下发采集请先检查配置'
  choicesLoading.value = false
}
async function importedRollout(result: RolloutImportResult) {
  await loadChoices(false)
  if (disposed || !dialogVisible.value) return
  if (!choices.value.some(batch => batch.batch_id === result.batch_id)) {
    createError.value = `批次 ${result.batch_id} 已登记，但未出现在当前活动列表；请重新打开创建窗口刷新确认。`
    return
  }
  draft.batch_id = result.batch_id
  ElMessage.success('原始轨迹已登记。确认当前配置后，点击“创建并开始”接续处理。')
}
function openCreate() {
  createError.value = ''; dialogVisible.value = true
  Object.assign(draft, { name: '', batch_id: '', start_mode: 'existing', mode: 'manual', threshold: 3, phone_id: '', app: '', vla: '' })
  void loadChoices()
}
async function create() {
  if (!canSubmit.value || submitting.value) return
  submitting.value = true; createError.value = ''
  const body: Omit<CreatePipeline, 'request_id'> = { name: draft.name.trim(), batch_id: draft.batch_id, start_mode: draft.start_mode, mode: draft.mode,
    ...(draft.mode === 'automatic' ? { threshold: draft.threshold } : {}),
    ...(draft.start_mode === 'collect' ? { collection_config: { phone_id: draft.phone_id, app: draft.app, vla: draft.vla.trim(), config: { sampling_enabled: draft.sampling_enabled, temperature: draft.temperature, top_p: draft.top_p, use_experience_lib: draft.use_experience_lib } } } : {}),
  }
  const key = JSON.stringify(body), requestId = requests.get(key) || newRunRequestId(); requests.set(key, requestId)
  try {
    const created = await api.createPipeline({ ...body, request_id: requestId })
    if (disposed) return
    requests.delete(key); history.value = [created, ...history.value.filter(item => item.pipeline_id !== created.pipeline_id)]
    dialogVisible.value = false; selectPipeline(created.pipeline_id); ElMessage.success('Pipeline 已交给后台执行')
  } catch (cause) { if (!disposed) createError.value = (cause as Error).message }
  finally { if (!disposed) submitting.value = false }
}
async function control(action: PipelineAction) {
  if (!pipeline.value || controlling.value) return
  if (action === 'terminate') {
    try { await ElMessageBox.confirm('停止后续派发。已启动的作业将完成并保留结果，正在提交的发布以最终结果为准。', '终止 Pipeline', { type: 'warning', confirmButtonText: '终止流程', cancelButtonText: '取消' }) } catch { return }
  }
  const target = pipeline.value; controlling.value = true
  try {
    const updated = await api.controlPipeline(target.pipeline_id, action, target.storage_revision)
    if (!disposed) { history.value = history.value.map(item => item.pipeline_id === updated.pipeline_id ? updated : item); if (pipeline.value?.pipeline_id === updated.pipeline_id) pipeline.value = updated; await context.refresh(); error.value = '' }
  } catch (cause) { if (!disposed) { error.value = (cause as Error).message; await context.refresh() } }
  finally { if (!disposed) controlling.value = false }
}
watch(pipeline, value => { if (value) history.value = history.value.map(item => item.pipeline_id === value.pipeline_id ? value : item) })
watch(() => draft.phone_id, () => { if (draft.app && !apps.value.includes(draft.app)) draft.app = '' })
watch(() => draft.start_mode, () => { if (!choices.value.some(batch => batch.batch_id === draft.batch_id)) draft.batch_id = '' })
function focus() { void loadHistory() }
function visible() { if (document.visibilityState === 'visible') void loadHistory() }
const unsubscribe = subscribeBatchLifecycle(event => { batches.value = batches.value.filter(batch => !event.batch_ids.includes(batch.batch_id)); if (event.batch_ids.includes(draft.batch_id)) draft.batch_id = '' })
onMounted(() => { localStorage.removeItem('automatic-pipeline-circuit-v3'); void loadHistory(); window.addEventListener('focus', focus); document.addEventListener('visibilitychange', visible) })
onBeforeUnmount(() => { disposed = true; listGeneration++; choicesGeneration++; listController?.abort(); unsubscribe(); window.removeEventListener('focus', focus); document.removeEventListener('visibilitychange', visible) })
</script>

<template>
  <div class="page pipeline-page" :class="{ 'is-paused': pipeline?.status === 'paused' }">
    <header class="page-hero pipeline-hero"><div><span>AUTOMATED DATA FLYWHEEL</span><h1>自动 Pipeline</h1><p>端到端数据飞轮自动迭代流水线</p></div><el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon>新建 Pipeline</el-button></header>
    <el-alert v-if="currentError" :title="currentError" type="error" :closable="false" show-icon />
    <section class="circuit-shell">
      <header class="run-strip">
        <div class="run-signal" :class="'is-' + runSignal"><i></i><span>{{ pipeline ? pipelineStatusLabels[pipeline.status] : '等待创建或选择 Pipeline' }}</span></div>
        <span v-if="pipeline" class="run-name">{{ pipeline.name }} · {{ pipeline.batch_id }}</span>
        <span class="run-current">{{ pipeline ? currentStep?.label : '点击“新建 Pipeline”配置并启动一次迭代' }}</span>
        <div v-if="pipeline" class="run-controls">
          <el-button v-if="pipeline.status === 'running' || pipeline.status === 'waiting_for_correction'" size="small" :loading="controlling" @click="control('pause')">暂停</el-button>
          <el-button v-if="pipeline.status === 'paused'" size="small" type="primary" :loading="controlling" @click="control('resume')">继续</el-button>
          <el-button v-if="pipeline.status === 'failed' || pipeline.status === 'published_summary_failed'" size="small" type="primary" :loading="controlling" @click="control('retry')">{{ pipeline.status === 'published_summary_failed' ? '重试汇总' : '重试' }}</el-button>
          <el-button v-if="!PIPELINE_TERMINAL.includes(pipeline.status) && pipeline.status !== 'terminating'" size="small" :disabled="controlling" @click="control('terminate')">终止</el-button>
        </div>
      </header>
      <div v-if="pipeline" class="run-workspace" data-testid="pipeline-run-status">
        <div class="run-summary">
          <span>{{ pipeline.mode === 'manual' ? '人工修正模式 · 每任务一条 Top1' : '自动发布模式 · 总分 ≥ ' + pipeline.threshold }}</span>
          <span>本轮执行：{{ done }}/{{ pipeline.steps.length }} 步骤已处理</span>
          <el-progress :percentage="pipeline.steps.length ? Math.round(done / pipeline.steps.length * 100) : 0" :show-text="false" />
        </div>
        <div v-if="currentStep" class="run-detail">
          <span>{{ currentStep.label }} · {{ pipelineStepLabels[currentStep.status] }}</span>
          <el-progress v-if="currentStep.status === 'running' && percentage(currentStep.percent) !== undefined" :percentage="percentage(currentStep.percent)" />
          <span v-if="currentDetails">{{ currentDetails }}</span>
          <span v-if="currentStep.error" class="run-error">{{ currentStep.error }}</span>
          <span v-for="job in currentStep.job_ids" :key="job" class="job-id">{{ job }}</span>
        </div>
        <div class="run-links">
          <div v-for="item in auxiliarySteps" :key="item.id" class="auxiliary-step">
            <el-button :type="pipeline.current_step === item.id ? 'primary' : undefined" :plain="pipeline.current_step === item.id" size="small" :data-testid="'pipeline-step-' + item.id" :title="state(item.id)?.error || state(item.id)?.message || ''" @click="openStep(item.id)">{{ item.label }} · {{ state(item.id) ? pipelineStepLabels[state(item.id)!.status] : '等待前置步骤' }}</el-button>
            <el-progress v-if="state(item.id)?.status === 'running' && percentage(state(item.id)?.percent) !== undefined" :percentage="percentage(state(item.id)?.percent)" />
          </div>
          <el-button v-if="pipeline.status === 'waiting_for_correction'" type="primary" size="small" @click="openStep('correction')">进入修正并确认继续</el-button>
          <el-button v-if="pipeline.release_id" size="small" type="primary" plain @click="openStep('publication')">查看发布详情</el-button>
          <el-button v-if="pipeline.status === 'succeeded'" size="small" @click="router.push('/data-publishing/overview')">查看数据总览</el-button>
        </div>
      </div>
      <div class="circuit-scroll">
        <div class="circuit-board">
          <div class="main-bus"></div>
          <section v-for="(stage, index) in stages" :key="stage.id" class="stage-column" :style="{ gridColumn: index + 1 }">
            <button type="button" class="stage-node" :class="visualClass(stage.state)" :disabled="!pipeline || !stage.stepIds.length" :data-status="stage.state.status" :data-testid="'pipeline-stage-' + stage.id" :title="stage.state.detail" :aria-label="stage.label" @click="openStage(stage)">
              <span class="node-order">{{ String(index + 1).padStart(2, '0') }}</span>
              <span class="node-core"><el-icon><component :is="icons[stage.iconKey]" /></el-icon></span>
              <strong>{{ stage.label }}</strong>
              <el-icon v-if="stage.state.status === 'completed'" class="node-state"><Check /></el-icon>
              <el-icon v-else-if="stage.state.status === 'failed'" class="node-state"><WarningFilled /></el-icon>
            </button>
            <div v-if="index < stages.length - 1" class="bus-segment" :class="stage.stepIds.length && stages[index + 1]?.stepIds.length ? visualClass(stage.state) : 'is-unavailable'"><i></i></div>
            <div class="branch" :class="visualClass(stage.state)">
              <button v-for="node in stage.children" :key="node.id" type="button" class="step-node" :class="visualClass(node.state)" :disabled="!pipeline || !node.stepId" :data-status="node.state.status" :data-active="node.state.active" :data-testid="'pipeline-node-' + node.id" :title="node.state.detail" @click="node.stepId && openStep(node.stepId)">
                <span class="branch-wire" :class="visualClass(node.state)"></span>
                <i class="step-port"><el-icon v-if="node.state.status === 'completed'"><Check /></el-icon></i>
                <span>{{ node.label }}</span>
              </button>
            </div>
          </section>
        </div>
      </div>
      <footer v-if="pipeline" class="run-footer">创建于 {{ date(pipeline.created_at) }}</footer>
    </section>
    <section class="history-panel"><div class="history-heading"><h2>运行记录</h2><el-button size="small" :loading="loading" @click="loadHistory"><el-icon><Refresh /></el-icon>刷新</el-button></div>
      <el-table :data="history" stripe empty-text="暂无 Pipeline，点击右上角新建" @row-click="(row: Pipeline) => selectPipeline(row.pipeline_id)">
        <el-table-column prop="name" label="名称" min-width="150" /><el-table-column prop="batch_id" label="业务批次" min-width="200" />
        <el-table-column label="模式" width="120"><template #default="{ row }">{{ row.mode === 'manual' ? '人工修正' : '自动发布' }}</template></el-table-column>
        <el-table-column label="状态" min-width="160"><template #default="{ row }">{{ pipelineStatusLabels[row.status as Pipeline['status']] }}</template></el-table-column>
        <el-table-column label="创建时间" min-width="165"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column>
        <el-table-column label="操作" width="85"><template #default="{ row }"><el-button link type="primary" @click.stop="selectPipeline(row.pipeline_id)">查看</el-button></template></el-table-column>
      </el-table>
    </section>
    <el-dialog v-model="dialogVisible" title="新建 Pipeline" width="620px" :close-on-click-modal="!submitting" :close-on-press-escape="!submitting" :show-close="!submitting" destroy-on-close>
      <el-alert v-if="createError" :title="createError" type="warning" :closable="false" />
      <el-form label-position="top" :disabled="submitting">
        <el-form-item label="名称" required><el-input v-model="draft.name" maxlength="100" placeholder="例如：手机采集批次验收" /></el-form-item>
        <el-form-item label="起点" required><el-radio-group v-model="draft.start_mode"><el-radio-button value="existing">接续已有结果</el-radio-button><el-radio-button value="collect">下发采集</el-radio-button></el-radio-group></el-form-item>
        <el-form-item label="业务批次" required><el-select v-model="draft.batch_id" filterable :loading="choicesLoading" placeholder="选择未发布批次" style="width:100%"><el-option v-for="batch in choices" :key="batch.batch_id" :label="`${batch.batch_id}${batch.label && batch.label !== batch.batch_id ? ' · ' + batch.label : ''} · ${batch.task_count} 个任务`" :value="batch.batch_id" /></el-select></el-form-item>
        <el-form-item v-if="draft.start_mode === 'existing'"><el-button :disabled="choicesLoading || submitting" data-testid="open-rollout-import" @click="rolloutImportVisible = true">导入已有 Rollout</el-button><span class="form-hint">已有本地轨迹尚未登记时，从这里添加新批次。</span></el-form-item>
        <el-form-item label="处理模式" required><el-radio-group v-model="draft.mode"><el-radio-button value="manual">人工修正</el-radio-button><el-radio-button value="automatic">自动发布</el-radio-button></el-radio-group></el-form-item>
        <p class="form-hint">{{ draft.mode === 'manual' ? '每任务选择一条 Top1，修正关卡等待人工确认；无需修改的轨迹也会导出。' : '每任务选择一条达标的最高分轨迹，未达标任务跳过；发布成功后整个批次结束处理。' }}</p>
        <el-form-item v-if="draft.mode === 'automatic'" label="质检总分阈值（0～5）" required><el-input-number v-model="draft.threshold" :min="0" :max="5" :step="0.1" /></el-form-item>
        <template v-if="draft.start_mode === 'collect'">
          <div class="form-grid"><el-form-item label="手机"><el-select v-model="draft.phone_id" clearable placeholder="全部关联手机"><el-option v-for="phone in factory.phones" :key="phone" :label="phone" :value="phone" /></el-select></el-form-item><el-form-item label="App"><el-select v-model="draft.app" clearable placeholder="全部关联 App"><el-option v-for="app in apps" :key="app" :label="app" :value="app" /></el-select></el-form-item></div>
          <el-form-item label="VLA" required><el-select v-model="draft.vla" filterable allow-create default-first-option style="width:100%"><el-option v-for="vla in factory.vla" :key="vla" :label="vla" :value="vla" /></el-select></el-form-item>
          <div class="form-grid"><el-form-item label="采样"><el-switch v-model="draft.sampling_enabled" /></el-form-item><el-form-item label="经验库"><el-switch v-model="draft.use_experience_lib" /></el-form-item><el-form-item label="temperature"><el-input-number v-model="draft.temperature" :min="0" :max="2" :step="0.1" /></el-form-item><el-form-item label="top_p"><el-input-number v-model="draft.top_p" :min="0" :max="1" :step="0.05" /></el-form-item></div>
        </template>
      </el-form>
      <template #footer><el-button :disabled="submitting" @click="dialogVisible = false">取消</el-button><el-button type="primary" :disabled="!canSubmit || choicesLoading" :loading="submitting" @click="create">创建并开始</el-button></template>
    </el-dialog>
    <RolloutImportDialog v-model="rolloutImportVisible" @imported="importedRollout" />
  </div>
</template>
<style scoped>
/* Diagram baseline: 6f85f34; keep its complete effective geometry and palette. */
.pipeline-page{min-height:100vh;padding:30px 34px;background:#f5f7fa}.page-head{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;max-width:1540px;margin:0 auto 20px}.page-head span{color:#0f766e;font-size:10px;letter-spacing:.17em}.page-head h1{margin:5px 0 2px;font-size:34px;font-weight:500;letter-spacing:-.035em}.page-head p{margin:0;color:#64748b;font-size:13px}
.circuit-shell{max-width:1540px;margin:auto;overflow:hidden;border:1px solid #26364d;border-radius:18px;background:#09111f;box-shadow:0 24px 60px rgba(15,23,42,.2)}.run-strip{display:flex;align-items:center;gap:16px;height:50px;padding:0 18px;border-bottom:1px solid #26364d;background:rgba(15,27,45,.92);color:#dbe8f5}.run-signal{display:flex;align-items:center;gap:8px;color:#71849d;font-size:11px}.run-signal i{width:7px;height:7px;border-radius:50%;background:#54657a}.run-signal.is-running,.run-signal.is-paused{color:#67e8f9}.run-signal.is-running i{background:#22d3ee;box-shadow:0 0 10px #22d3ee;animation:signal 1.2s infinite}.run-signal.is-paused i{background:#fbbf24}.run-signal.is-completed{color:#6ee7b7}.run-signal.is-completed i{background:#34d399;box-shadow:0 0 9px #34d399}.run-signal.is-failed{color:#fca5a5}.run-signal.is-failed i{background:#ef4444}.run-name{padding-left:15px;border-left:1px solid #334155;color:#f1f5f9;font-size:12px}.run-current{flex:1;color:#7f93aa;font-size:11px}.run-controls{display:flex;gap:6px}
.circuit-scroll{overflow-x:auto;overflow-y:hidden;background-image:linear-gradient(rgba(71,95,125,.12) 1px,transparent 1px),linear-gradient(90deg,rgba(71,95,125,.12) 1px,transparent 1px);background-size:24px 24px}.circuit-board{position:relative;display:grid;grid-template-columns:repeat(7,180px);column-gap:60px;width:max-content;min-width:100%;min-height:540px;padding:65px 70px 50px}.main-bus{position:absolute;top:104px;left:70px;width:calc(100% - 140px);height:2px;background:#263b54;box-shadow:0 0 8px rgba(82,113,148,.2)}.stage-column{position:relative;display:flex;align-items:center;flex-direction:column;z-index:1}.stage-node{position:relative;display:grid;grid-template-columns:32px 1fr auto;align-items:center;width:180px;height:78px;padding:0 12px;border:1px solid #344860;border-radius:9px;background:#0d192a;color:#8296ae;box-sizing:border-box;transition:.3s}.node-order{position:absolute;top:-19px;left:2px;color:#4f6278;font:9px/1 monospace;letter-spacing:.14em}.node-core{display:grid;place-items:center;width:27px;height:27px;border:1px solid #3d526b;border-radius:50%;background:#111f32;font-size:14px}.stage-node strong{font-size:13px;font-weight:500;text-align:center}.node-state{font-size:14px}.stage-node.is-running{border-color:#22d3ee;background:#0b2637;color:#cffafe;box-shadow:0 0 0 1px rgba(34,211,238,.28),0 0 25px rgba(34,211,238,.2)}.stage-node.is-running .node-core{border-color:#67e8f9;color:#67e8f9;box-shadow:inset 0 0 10px rgba(34,211,238,.22)}.stage-node.is-completed{border-color:#2a8b77;background:#0c2828;color:#a7f3d0}.stage-node.is-completed .node-core{border-color:#34d399;color:#34d399}.stage-node.is-failed{border-color:#ef4444;color:#fecaca}
.bus-segment{position:absolute;top:38px;left:180px;width:60px;height:2px;background:#263b54;overflow:hidden}.bus-segment.is-completed{background:#34d399;box-shadow:0 0 8px rgba(52,211,153,.7)}.bus-segment.is-running{background:#155e75}.bus-segment.is-running i{position:absolute;width:28px;height:100%;background:linear-gradient(90deg,transparent,#67e8f9,transparent);animation:current 1.1s linear infinite}.branch{position:relative;width:180px;margin-top:27px;padding-top:6px}.branch:before{content:"";position:absolute;top:-27px;left:89px;width:2px;height:33px;background:#263b54}.branch.is-running:before{background:#22d3ee;box-shadow:0 0 8px #22d3ee}.branch.is-completed:before{background:#34d399}.branch.is-failed:before{background:#ef4444}.step-node{position:relative;display:flex;align-items:center;width:180px;height:52px;margin-bottom:16px;padding:0 11px;border:1px solid #2c3e55;border-radius:7px;background:#0c1727;color:#6e829a;font-size:11px;box-sizing:border-box;transition:.25s}.step-node span:last-child{width:100%;text-align:center}.step-port{position:absolute;top:-10px;left:80px;display:grid;place-items:center;width:18px;height:18px;border:2px solid #3a4d64;border-radius:50%;background:#09111f;color:white;font-style:normal;font-size:10px;z-index:2}.branch-wire{position:absolute;top:-17px;left:88px;width:2px;height:17px;background:#2b3d53}.step-node.is-running{border-color:#22d3ee;background:#0b2637;color:#cffafe;box-shadow:0 0 18px rgba(34,211,238,.22)}.step-node.is-running .step-port,.branch-wire.is-running{border-color:#67e8f9;background:#22d3ee;box-shadow:0 0 10px #22d3ee}.step-node.is-completed{border-color:#287964;background:#0b2425;color:#a7f3d0}.step-node.is-completed .step-port,.branch-wire.is-completed{border-color:#34d399;background:#34d399}.step-node.is-failed{border-color:#ef4444;color:#fecaca}.step-node.is-failed .step-port,.branch-wire.is-failed{border-color:#ef4444;background:#ef4444}
.legend{display:flex;justify-content:flex-end;gap:18px;padding:10px 18px;border-top:1px solid #26364d;background:#0d1726;color:#71849d;font-size:10px}.legend span{display:flex;align-items:center;gap:6px}.legend i{width:7px;height:7px;border-radius:50%}.legend .pending{background:#54657a}.legend .running{background:#22d3ee;box-shadow:0 0 7px #22d3ee}.legend .completed{background:#34d399}.legend .failed{background:#ef4444}
@keyframes current{from{transform:translateX(-28px)}to{transform:translateX(60px)}}@keyframes signal{50%{opacity:.45;box-shadow:0 0 17px #22d3ee}}
@media(max-width:760px){.pipeline-page{padding:22px 14px}.page-head{align-items:flex-start;flex-direction:column}.page-head h1{font-size:29px}.circuit-board{padding-left:45px;padding-right:45px}.main-bus{left:45px;width:calc(100% - 90px)}.run-name{display:none}}

/* Light engineering-circuit theme, aligned with the rest of the workspace. */
.pipeline-page{min-height:100vh;background:radial-gradient(circle at 82% 0%,rgba(14,165,233,.1),transparent 28%),radial-gradient(circle at 18% 88%,rgba(20,184,166,.08),transparent 24%)}
.pipeline-hero{max-width:none;margin:0;padding-top:4px}
.pipeline-hero>div>span{color:var(--accent-deep);font-size:10px;font-weight:900;letter-spacing:.18em}
.pipeline-hero h1{margin:6px 0 4px;font-size:clamp(28px,3vw,43px);font-weight:600;letter-spacing:-.035em}
.pipeline-hero p{margin:0;color:var(--muted);font-size:14px;line-height:1.6}
.circuit-shell{max-width:none;margin:20px 0 0;border-color:rgba(255,255,255,.92);border-radius:16px;background:rgba(255,255,255,.72);box-shadow:0 18px 48px rgba(15,23,42,.08);backdrop-filter:blur(10px)}
.run-strip{height:48px;padding:0 17px;border-bottom-color:#dce5eb;background:rgba(255,255,255,.84);color:var(--ink)}
.run-signal{color:#64748b}.run-signal i{background:#94a3b8}
.run-signal.is-running,.run-signal.is-paused{color:#0f766e}.run-signal.is-running i{background:#14b8a6;box-shadow:0 0 0 4px rgba(20,184,166,.13)}.run-signal.is-paused i{background:#f59e0b}
.run-signal.is-completed{color:#047857}.run-signal.is-completed i{background:#10b981;box-shadow:0 0 0 4px rgba(16,185,129,.12)}
.run-name{border-left-color:#dce5eb;color:#0f172a;font-weight:600}.run-current{color:#64748b}
.circuit-scroll{background-color:rgba(248,250,252,.7);background-image:linear-gradient(rgba(148,163,184,.12) 1px,transparent 1px),linear-gradient(90deg,rgba(148,163,184,.12) 1px,transparent 1px);background-size:24px 24px}
.circuit-board{grid-template-columns:repeat(7,176px);column-gap:58px;min-height:505px;padding:82px 62px 42px}
.main-bus{top:103px;left:62px;width:calc(100% - 124px);height:2px;background:#cbd7e1;box-shadow:none}
.stage-node{display:block;width:44px;height:44px;padding:0;border:2px solid #aebdca;border-radius:50%;background:#fff;color:#64748b;box-shadow:0 5px 14px rgba(15,23,42,.08)}
.node-order{top:-28px;left:50%;transform:translateX(-50%);color:#94a3b8;font:9px/1 monospace}
.node-core{width:40px;height:40px;border:0;border-radius:50%;background:#fff;color:#64748b;font-size:16px}
.stage-node strong{position:absolute;top:51px;left:50%;z-index:2;width:150px;padding:1px 5px;transform:translateX(-50%);background:rgba(248,250,252,.94);color:#334155;font-size:13px;font-weight:600;text-align:center;white-space:nowrap}
.node-state{position:absolute;right:-5px;bottom:-4px;display:grid;place-items:center;width:17px;height:17px;border:2px solid #fff;border-radius:50%;background:#10b981;color:#fff;font-size:10px}
.stage-node.is-running{border-color:#14b8a6;background:#fff;color:#0f766e;box-shadow:0 0 0 5px rgba(20,184,166,.12),0 8px 20px rgba(15,118,110,.12);transform:none}
.stage-node.is-running .node-core{border:0;background:#ecfdf5;color:#0f766e;box-shadow:none}.stage-node.is-running strong{color:#0f766e}
.stage-node.is-completed{border-color:#10b981;background:#10b981;color:#fff;box-shadow:0 0 0 4px rgba(16,185,129,.1)}
.stage-node.is-completed .node-core{border:0;background:#10b981;color:#fff}.stage-node.is-completed strong{color:#047857}
.stage-node.is-failed{border-color:#ef4444;background:#fff;color:#dc2626}.stage-node.is-failed strong{color:#b91c1c}
.bus-segment{top:21px;left:110px;width:190px;height:2px;background:#cbd7e1;overflow:hidden}
.bus-segment.is-completed{background:#10b981;box-shadow:none}.bus-segment.is-running{background:#99f6e4}
.bus-segment.is-running i{top:-1px;height:4px;border-radius:4px;background:linear-gradient(90deg,transparent,#14b8a6,transparent);animation:light-current 1.25s linear infinite}
.branch{width:176px;margin-top:58px;padding-top:10px}
.branch:before{top:-58px;left:87px;width:2px;height:68px;background:#cbd7e1}
.branch.is-running:before{background:#14b8a6;box-shadow:none}.branch.is-completed:before{background:#10b981}.branch.is-failed:before{background:#ef4444}
.step-node{width:176px;height:40px;margin-bottom:14px;padding:0 12px;border-color:#d5e0e7;border-radius:20px;background:rgba(255,255,255,.94);color:#64748b;font-size:11px;box-shadow:0 4px 12px rgba(15,23,42,.04)}
.step-port{top:-8px;left:79px;width:16px;height:16px;border-color:#bdcbd6;background:#fff;color:#fff;font-size:9px}
.branch-wire{top:-15px;left:86px;height:15px;background:#cbd7e1}
.step-node.is-running{border-color:#2dd4bf;background:#f0fdfa;color:#0f766e;box-shadow:0 0 0 3px rgba(45,212,191,.12)}
.step-node.is-running .step-port,.branch-wire.is-running{border-color:#14b8a6;background:#14b8a6;box-shadow:0 0 0 3px rgba(20,184,166,.12)}
.step-node.is-completed{border-color:#86efac;background:#f0fdf4;color:#047857}
.step-node.is-completed .step-port,.branch-wire.is-completed{border-color:#10b981;background:#10b981}
.step-node.is-failed{border-color:#fca5a5;background:#fff1f2;color:#b91c1c}.step-node.is-failed .step-port,.branch-wire.is-failed{border-color:#ef4444;background:#ef4444}
.legend{padding:9px 17px;border-top-color:#dce5eb;background:rgba(255,255,255,.82);color:#64748b}.legend .pending{background:#94a3b8}.legend .running{background:#14b8a6;box-shadow:0 0 0 3px rgba(20,184,166,.12)}.legend .completed{background:#10b981}
@keyframes light-current{from{transform:translateX(-28px)}to{transform:translateX(190px)}}
@media(max-width:760px){.pipeline-page{padding:24px 20px}.pipeline-hero{align-items:flex-start;flex-direction:column}.circuit-board{padding-left:42px;padding-right:42px}.main-bus{left:42px;width:calc(100% - 84px)}.run-name{display:none}}

/* Keep the pipeline as one platform-style card; the inner surface should not
   read as a second, differently coloured canvas. */
.circuit-shell{background:#fff;backdrop-filter:none;box-shadow:0 12px 35px rgba(15,23,42,.06)}
.circuit-scroll{background:transparent;background-image:none}
.legend{background:#fff}
.circuit-board{column-gap:36px}
.bus-segment{width:168px}
@keyframes light-current{from{transform:translateX(-28px)}to{transform:translateX(168px)}}
.stage-node strong{top:-34px;background:transparent;font-size:15px}
.node-order{top:-52px}
.branch{margin-top:38px}
.branch:before{top:-38px;height:48px}

/* Sharper blue-violet engineering palette for the pipeline card. */
.pipeline-page{background:radial-gradient(circle at 82% 0%,rgba(99,102,241,.1),transparent 28%),radial-gradient(circle at 18% 88%,rgba(14,165,233,.08),transparent 24%)}
.circuit-shell{border-color:#d7def3;border-radius:8px;background:#fbfcff;box-shadow:none}
.run-strip{border-bottom-color:#dbe4f5;background:#f8faff}
.run-signal.is-running,.run-signal.is-paused{color:#4338ca}.run-signal.is-running i{background:#6366f1;box-shadow:0 0 0 4px rgba(99,102,241,.14)}
.run-signal.is-completed{color:#047857}.run-signal.is-completed i{background:#10b981;box-shadow:0 0 0 4px rgba(16,185,129,.12)}
.main-bus,.bus-segment,.branch:before,.branch-wire{background:#cbd5e1}
.bus-segment.is-completed,.branch.is-completed:before,.branch-wire.is-completed{background:#10b981;box-shadow:none}
.bus-segment.is-running{background:#c7d2fe}.bus-segment.is-running i{background:linear-gradient(90deg,transparent,#6366f1,transparent)}
.stage-node.is-running{border-color:#6366f1;background:#eef2ff;color:#4338ca;box-shadow:0 0 0 5px rgba(99,102,241,.12),0 8px 20px rgba(79,70,229,.1)}
.stage-node.is-running .node-core{background:#eef2ff;color:#4338ca}.stage-node.is-running strong{color:#4338ca}
.stage-node.is-completed{border-color:#10b981;background:#10b981}.stage-node.is-completed .node-core{background:#10b981}
.branch.is-running:before{background:#6366f1;box-shadow:none}
.step-node.is-running{border-color:#818cf8;background:#eef2ff;color:#4338ca;box-shadow:0 0 0 3px rgba(99,102,241,.12)}
.step-node.is-running .step-port,.branch-wire.is-running{border-color:#6366f1;background:#6366f1;box-shadow:0 0 0 3px rgba(99,102,241,.12)}
.run-signal.is-running,.run-signal.is-paused{color:#0e7490}.run-signal.is-running i{background:#06b6d4;box-shadow:0 0 0 4px rgba(6,182,212,.14)}
.main-bus,.bus-segment,.branch:before,.branch-wire{background:#cbd5e1}
.bus-segment.is-completed,.branch.is-completed:before,.branch-wire.is-completed{background:#6366f1}
.bus-segment.is-running{background:#a5f3fc}.bus-segment.is-running i{background:linear-gradient(90deg,transparent,#06b6d4,transparent)}
.stage-node.is-running{border-color:#06b6d4;background:#ecfeff;color:#0e7490;box-shadow:0 0 0 5px rgba(6,182,212,.13),0 8px 20px rgba(8,145,178,.1)}
.stage-node.is-running .node-core{background:#ecfeff;color:#0e7490}.stage-node.is-running strong{color:#0e7490}
.stage-node.is-completed{border-color:#6366f1;background:#6366f1}.stage-node.is-completed .node-core{background:#6366f1}.stage-node.is-completed strong{color:#4338ca}.stage-node.is-completed .node-state{background:#6366f1}
.branch.is-running:before{background:#06b6d4}
.step-node.is-running{border-color:#67e8f9;background:#ecfeff;color:#0e7490;box-shadow:0 0 0 3px rgba(6,182,212,.13)}
.step-node.is-running .step-port,.branch-wire.is-running{border-color:#06b6d4;background:#06b6d4;box-shadow:0 0 0 3px rgba(6,182,212,.13)}
.step-node.is-completed{border-color:#a5b4fc;background:#eef2ff;color:#4338ca}.step-node.is-completed .step-port,.branch-wire.is-completed{border-color:#6366f1;background:#6366f1}
.run-signal.is-paused{color:#b45309}.run-signal.is-paused i{background:#f59e0b;box-shadow:0 0 0 4px rgba(245,158,11,.14)}

/* Real execution controls sit outside the original seven-column diagram. */
.run-strip{min-height:48px;height:auto;padding-top:10px;padding-bottom:10px;flex-wrap:wrap}
.run-controls{margin-left:auto;flex-shrink:0}
.run-signal.is-failed{color:#b91c1c}
.run-workspace{padding:14px 17px;border-bottom:1px solid #dbe4f5;color:var(--muted);font-size:12px;background:#f8faff}
.run-summary,.run-detail,.run-links{display:flex;align-items:center;flex-wrap:wrap;gap:10px 18px}
.run-summary>.el-progress{width:140px}
.run-detail{margin-top:12px;line-height:1.6}
.run-detail>.el-progress{width:180px}
.run-detail .job-id{overflow-wrap:anywhere;font:11px monospace}
.run-error{color:#b91c1c}
.run-links{margin-top:12px;gap:8px 12px}
.auxiliary-step{display:flex;align-items:center;gap:10px}
.auxiliary-step>.el-progress{width:120px}
.run-links>.el-button{margin-left:0}
.stage-node,.step-node{cursor:pointer;font-family:inherit}
.stage-node:disabled,.step-node:disabled{cursor:default;opacity:1}
.stage-node:focus-visible,.step-node:focus-visible{outline:2px solid #06b6d4;outline-offset:5px}
.stage-node.is-waiting{border-color:#f59e0b;color:#b45309}
.stage-node.is-waiting .node-core{color:#b45309;background:#fffbeb}
.stage-node.is-waiting strong{color:#b45309}
.step-node.is-waiting{border-color:#fbbf24;background:#fffbeb;color:#b45309}
.stage-node.is-failed .node-state{background:#ef4444}
.pipeline-page.is-paused .bus-segment i,.pipeline-page.is-paused .run-signal i{animation-play-state:paused}
.run-footer{padding:12px 17px;border-top:1px solid #dbe4f5;color:var(--muted);font-size:12px}
.history-panel{margin:20px 0;background:#fff;border:1px solid #dce5eb;border-radius:16px;padding:18px 20px;overflow:hidden}
.history-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.history-heading h2{font-size:16px;margin:0}
.form-hint{font-size:12px;line-height:1.6;color:var(--muted);margin:-4px 0 16px}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
.el-alert{margin:14px 0}
@media(max-width:760px){.run-current{flex-basis:100%}.run-controls{margin-left:0}.form-grid{grid-template-columns:1fr}}
</style>
