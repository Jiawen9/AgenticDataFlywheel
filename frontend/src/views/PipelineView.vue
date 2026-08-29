<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { Check, Collection, Cpu, DataAnalysis, Plus, Promotion, RefreshRight, TrendCharts, Upload, VideoPause, VideoPlay, WarningFilled } from '@element-plus/icons-vue'

type PipelineStatus = 'idle' | 'running' | 'paused' | 'completed' | 'failed'
type StepStatus = 'pending' | 'running' | 'completed' | 'failed'
interface PipelineStep { id: string; name: string }
interface PipelineStage { id: string; name: string; icon: object; steps: PipelineStep[] }
interface PipelineConfig { iterationName: string; seedTaskSet: string; targetModel: string }
interface PersistedPipeline { version: number; config: PipelineConfig; status: PipelineStatus; activeStepIndex: number; stepStatuses: Record<string, StepStatus> }

const STORAGE_KEY = 'automatic-pipeline-circuit-v3'
const STEP_DURATION = 1800
const stages: PipelineStage[] = [
  { id: 'evaluation', name: '迭代评估', icon: TrendCharts, steps: [{ id: 'seed-evaluation', name: '种子任务集评测' }, { id: 'badcase-extraction', name: 'BadCase 提取' }] },
  { id: 'generation', name: '任务生成', icon: DataAnalysis, steps: [{ id: 'badcase-augmentation', name: 'BadCase 扩增' }] },
  { id: 'collection', name: '轨迹采集', icon: Collection, steps: [{ id: 'phone-factory', name: '手机工厂并行采集' }, { id: 'bounding-box', name: '标框' }, { id: 'page-summary', name: '页面总结' }, { id: 'tree-building', name: '轨迹树构建' }] },
  { id: 'quality', name: '轨迹质检', icon: DataAnalysis, steps: [{ id: 'rubrics-generation', name: 'Rubrics 生成' }, { id: 'rubrics-ranking', name: 'Rubrics 相对排序' }] },
  { id: 'publishing', name: '数据发布', icon: Upload, steps: [{ id: 'dataset-archive', name: '数据集归档' }] },
  { id: 'training', name: '模型训练', icon: Cpu, steps: [{ id: 'data-mixture', name: '训练数据配比' }, { id: 'dataset-split', name: '训练集/验证集划分' }, { id: 'model-training', name: '模型训练' }, { id: 'training-validation', name: '训练有效性验证' }] },
  { id: 'model-publishing', name: '模型发布', icon: Promotion, steps: [{ id: 'trained-model-archive', name: '增训模型归档' }] },
]
const executionQueue = stages.flatMap((stage, stageIndex) => stage.steps.map((step) => ({ ...step, stageId: stage.id, stageName: stage.name, stageIndex })))
const emptyConfig = (): PipelineConfig => ({ iterationName: '', seedTaskSet: '种子任务集 · GUI 基础能力', targetModel: 'Qwen GUI Agent' })
const config = reactive<PipelineConfig>(emptyConfig())
const draft = reactive<PipelineConfig>(emptyConfig())
const dialogVisible = ref(false)
const status = ref<PipelineStatus>('idle')
const activeStepIndex = ref(-1)
const stepStatuses = reactive<Record<string, StepStatus>>(Object.fromEntries(executionQueue.map((step) => [step.id, 'pending'])))
let timer: ReturnType<typeof setTimeout> | undefined

const currentStep = computed(() => executionQueue[activeStepIndex.value] ?? null)
const completedCount = computed(() => Object.values(stepStatuses).filter((value) => value === 'completed').length)
const canCreate = computed(() => status.value === 'idle' || status.value === 'completed')
const canSubmit = computed(() => Boolean(draft.iterationName.trim() && draft.seedTaskSet.trim() && draft.targetModel.trim()))
const statusText = computed(() => ({ idle: '等待创建', running: '自动迭代中', paused: 'Pipeline 已暂停', completed: '本轮 Pipeline 已完成', failed: 'Pipeline 执行失败' }[status.value]))

function stageStatus(stage: PipelineStage): StepStatus {
  const values = stage.steps.map((step) => stepStatuses[step.id])
  if (values.includes('failed')) return 'failed'
  if (values.includes('running')) return 'running'
  if (values.every((value) => value === 'completed')) return 'completed'
  return 'pending'
}
function branchStatus(stage: PipelineStage, stepIndex: number): StepStatus {
  const current = stepStatuses[stage.steps[stepIndex].id]
  if (current !== 'pending') return current
  return stage.steps.slice(0, stepIndex).every((step) => stepStatuses[step.id] === 'completed') && stageStatus(stage) !== 'pending' ? 'running' : 'pending'
}
function connectorStatus(index: number): StepStatus {
  return stageStatus(stages[index]) === 'completed' ? 'completed' : stageStatus(stages[index])
}
function clearTimer() { if (timer) clearTimeout(timer); timer = undefined }
function scheduleNext() {
  clearTimer()
  if (status.value !== 'running' || !currentStep.value) return
  timer = setTimeout(() => {
    if (!currentStep.value || status.value !== 'running') return
    stepStatuses[currentStep.value.id] = 'completed'
    if (activeStepIndex.value === executionQueue.length - 1) { activeStepIndex.value = executionQueue.length; status.value = 'completed'; return }
    activeStepIndex.value += 1
    stepStatuses[executionQueue[activeStepIndex.value].id] = 'running'
    scheduleNext()
  }, STEP_DURATION)
}
function openCreate() {
  if (!canCreate.value) return
  Object.assign(draft, emptyConfig())
  dialogVisible.value = true
}
function createAndStart() {
  if (!canSubmit.value) return
  clearTimer(); Object.assign(config, draft)
  executionQueue.forEach((step) => { stepStatuses[step.id] = 'pending' })
  activeStepIndex.value = 0; stepStatuses[executionQueue[0].id] = 'running'; status.value = 'running'; dialogVisible.value = false
  scheduleNext()
}
function pausePipeline() { if (status.value === 'running') { clearTimer(); status.value = 'paused' } }
function resumePipeline() { if (status.value === 'paused') { status.value = 'running'; scheduleNext() } }
function retryPipeline() { if (status.value === 'failed' && currentStep.value) { stepStatuses[currentStep.value.id] = 'running'; status.value = 'running'; scheduleNext() } }
function restorePipeline() {
  const raw = localStorage.getItem(STORAGE_KEY); if (!raw) return
  try {
    const saved = JSON.parse(raw) as PersistedPipeline
    if (saved.version !== 3) throw new Error('unsupported pipeline state')
    Object.assign(config, saved.config); activeStepIndex.value = saved.activeStepIndex
    executionQueue.forEach((step) => { stepStatuses[step.id] = saved.stepStatuses?.[step.id] ?? 'pending' })
    status.value = saved.status === 'running' ? 'paused' : saved.status
  } catch { localStorage.removeItem(STORAGE_KEY) }
}
watch([config, status, activeStepIndex, stepStatuses], () => {
  if (status.value === 'idle') return
  const saved: PersistedPipeline = { version: 3, config: { ...config }, status: status.value, activeStepIndex: activeStepIndex.value, stepStatuses: { ...stepStatuses } }
  localStorage.setItem(STORAGE_KEY, JSON.stringify(saved))
}, { deep: true })
onMounted(restorePipeline)
onBeforeUnmount(clearTimer)
</script>

<template>
  <div class="page pipeline-page">
    <header class="page-hero pipeline-hero">
      <div><span>AUTOMATED DATA FLYWHEEL</span><h1>自动 Pipeline</h1><p>端到端数据飞轮自动迭代流水线</p></div>
      <el-button type="primary" :disabled="!canCreate" @click="openCreate"><el-icon><Plus /></el-icon>新建 Pipeline</el-button>
    </header>

    <section class="circuit-shell">
      <header class="run-strip">
        <div class="run-signal" :class="`is-${status}`"><i></i><span>{{ statusText }}</span></div>
        <template v-if="status !== 'idle'">
          <span class="run-name">{{ config.iterationName }}</span>
          <span class="run-current">{{ currentStep ? `${currentStep.stageName} / ${currentStep.name}` : `${completedCount} 个步骤已完成` }}</span>
          <div class="run-controls">
            <el-button v-if="status === 'running'" circle size="small" title="暂停" @click="pausePipeline"><el-icon><VideoPause /></el-icon></el-button>
            <el-button v-if="status === 'paused'" circle size="small" type="primary" title="继续" @click="resumePipeline"><el-icon><VideoPlay /></el-icon></el-button>
            <el-button v-if="status === 'failed'" circle size="small" type="danger" title="重试" @click="retryPipeline"><el-icon><RefreshRight /></el-icon></el-button>
          </div>
        </template>
        <span v-else class="run-current">点击“新建 Pipeline”配置并启动一次迭代</span>
      </header>

      <div class="circuit-scroll">
        <div class="circuit-board">
          <div class="main-bus"></div>
          <template v-for="(stage, stageIndex) in stages" :key="stage.id">
            <section class="stage-column" :style="{ gridColumn: stageIndex + 1 }">
              <div class="stage-node" :class="`is-${stageStatus(stage)}`">
                <span class="node-order">{{ String(stageIndex + 1).padStart(2, '0') }}</span>
                <span class="node-core"><el-icon><component :is="stage.icon" /></el-icon></span>
                <strong>{{ stage.name }}</strong>
                <el-icon v-if="stageStatus(stage) === 'completed'" class="node-state"><Check /></el-icon>
                <el-icon v-else-if="stageStatus(stage) === 'failed'" class="node-state"><WarningFilled /></el-icon>
              </div>
              <div v-if="stageIndex < stages.length - 1" class="bus-segment" :class="`is-${connectorStatus(stageIndex)}`"><i></i></div>
              <div class="branch" :class="`is-${stageStatus(stage)}`">
                <div v-for="(step, stepIndex) in stage.steps" :key="step.id" class="step-node" :class="`is-${stepStatuses[step.id]}`">
                  <span class="branch-wire" :class="`is-${branchStatus(stage, stepIndex)}`"></span>
                  <i class="step-port"><el-icon v-if="stepStatuses[step.id] === 'completed'"><Check /></el-icon></i>
                  <span>{{ step.name }}</span>
                </div>
              </div>
            </section>
          </template>
        </div>
      </div>

      <footer class="legend"><span><i class="pending"></i>等待</span><span><i class="running"></i>执行中</span><span><i class="completed"></i>已完成</span><span><i class="failed"></i>失败</span></footer>
    </section>

    <el-dialog v-model="dialogVisible" title="新建自动 Pipeline" width="500px" destroy-on-close>
      <el-form label-position="top">
        <el-form-item label="迭代名称" required><el-input v-model="draft.iterationName" placeholder="例如：GUI-Agent 第 3 轮迭代" /></el-form-item>
        <el-form-item label="种子任务集" required><el-input v-model="draft.seedTaskSet" /></el-form-item>
        <el-form-item label="目标模型" required><el-input v-model="draft.targetModel" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="dialogVisible = false">取消</el-button><el-button type="primary" :disabled="!canSubmit" @click="createAndStart"><el-icon><VideoPlay /></el-icon>开始运行</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
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
</style>
