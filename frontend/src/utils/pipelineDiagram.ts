import type { Pipeline, PipelineJob, PipelineStep, PipelineStepId } from '@/types/pipeline'
import { pipelineStepLabels } from '@/types/pipeline'

export interface DiagramNode {
  id: string
  label: string
  stepId?: PipelineStepId
}

export interface DiagramStage {
  id: string
  label: string
  iconKey: 'TrendCharts' | 'DataAnalysis' | 'Collection' | 'Upload' | 'Cpu' | 'Promotion'
  stepIds: readonly PipelineStepId[]
  children: readonly DiagramNode[]
}

export interface DiagramNodeState {
  status: 'unavailable' | 'pending' | 'running' | 'waiting' | 'completed' | 'skipped' | 'failed'
  active: boolean
  detail: string
  percent?: number
}

// The diagram preserves the original product map. Execution continues to use
// the eight persisted backend steps; several visual nodes share one real job.
export const PIPELINE_DIAGRAM_STAGES: readonly DiagramStage[] = [
  { id: 'evaluation', label: '迭代评估', iconKey: 'TrendCharts', stepIds: [], children: [
    { id: 'seed-evaluation', label: '种子任务集评测' }, { id: 'badcase-extraction', label: 'BadCase 提取' },
  ] },
  { id: 'generation', label: '任务生成', iconKey: 'DataAnalysis', stepIds: [], children: [
    { id: 'badcase-augmentation', label: 'BadCase 扩增' },
  ] },
  { id: 'collection', label: '轨迹采集', iconKey: 'Collection', stepIds: ['collection', 'preprocessing', 'tree'], children: [
    { id: 'phone-factory', label: '手机工厂并行采集', stepId: 'collection' },
    { id: 'bounding-box', label: '标框', stepId: 'preprocessing' },
    { id: 'page-summary', label: '页面总结', stepId: 'tree' },
    { id: 'tree-building', label: '轨迹树构建', stepId: 'tree' },
  ] },
  { id: 'quality', label: '轨迹质检', iconKey: 'DataAnalysis', stepIds: ['quality'], children: [
    { id: 'rubrics-generation', label: 'Rubrics 生成', stepId: 'quality' },
    { id: 'rubrics-ranking', label: 'Rubrics 相对排序', stepId: 'quality' },
  ] },
  { id: 'publishing', label: '数据发布', iconKey: 'Upload', stepIds: ['publication'], children: [
    { id: 'training-data-archive', label: '训练数据归档', stepId: 'publication' },
  ] },
  { id: 'training', label: '模型训练', iconKey: 'Cpu', stepIds: [], children: [
    { id: 'data-mixture', label: '训练数据配比' },
    { id: 'dataset-split', label: '训练集/验证集划分' },
    { id: 'model-training', label: '模型训练' },
    { id: 'training-validation', label: '训练有效性验证' },
  ] },
  { id: 'model-publishing', label: '模型发布', iconKey: 'Promotion', stepIds: [], children: [
    { id: 'trained-model-archive', label: '增训模型归档' },
  ] },
]

const substageNodes: Record<string, string> = {
  classifying_and_observing: 'page-summary',
  building: 'tree-building',
  summarizing_trajectories: 'tree-building',
  generating_rubric: 'rubrics-generation',
  evaluating: 'rubrics-ranking',
}
const substageLabels: Record<string, string> = {
  queued: '等待作业执行', scanning: '扫描采集轨迹', converting: '转换轨迹，准备标框',
  annotating: '正在标框', preparing: '准备质检输入',
  classifying_and_observing: '页面分类与总结', building: '构建轨迹树',
  summarizing_trajectories: '生成轨迹摘要', generating_rubric: '生成 Rubrics', evaluating: '轨迹评分与排序',
  publishing: '保存过程件',
}

function unavailable(): DiagramNodeState {
  return { status: 'unavailable', active: false, detail: '尚未接入自动执行' }
}

function progress(step: PipelineStep): Pick<DiagramNodeState, 'percent'> {
  return typeof step.percent === 'number' && Number.isFinite(step.percent)
    ? { percent: Math.max(0, Math.min(100, step.percent)) } : {}
}

function activeJobs(step: PipelineStep): PipelineJob[] {
  return (step.jobs ?? []).filter(job => job.status === 'running' || job.status === 'queued')
}

function actualSubstages(step: PipelineStep): string[] {
  return [...new Set(activeJobs(step).map(job => typeof job.stage === 'string' ? job.stage : '').filter(Boolean))]
}

function describeStep(step: PipelineStep): string {
  const parts: string[] = [step.label, pipelineStepLabels[step.status]]
  const substages = actualSubstages(step)
  if (step.status === 'running' && substages.length) {
    parts.push(substages.map(stage => substageLabels[stage] ?? `作业工序：${stage}`).join('、'))
  }
  if (step.error) parts.push(step.error)
  if (step.message) parts.push(step.message)
  if (step.id === 'tree' || step.id === 'quality') {
    parts.push(step.id === 'tree' ? '共享整批建树作业进度' : '共享整批质检作业进度')
    if (step.status === 'running') {
      parts.push(substages.some(stage => substageNodes[stage])
        ? '按当前任务工序显示，整批完成后统一标记完成'
        : '后台未提供可细分工序，以所属作业状态为准')
    }
  }
  return parts.join(' · ')
}

export function diagramNodeState(pipeline: Pipeline | null | undefined, node: DiagramNode): DiagramNodeState {
  if (!node.stepId) return unavailable()
  const step = pipeline?.steps.find(item => item.id === node.stepId)
  if (!step) return { status: 'pending', active: false, detail: pipeline ? '等待前置步骤' : '等待创建 Pipeline' }
  const status = step.status === 'succeeded' ? 'completed' : step.status
  const shared = step.id === 'tree' || step.id === 'quality'
  let active = step.status === 'running' || step.status === 'waiting'
  if (shared && step.status === 'running') {
    // Completed jobs from earlier tasks cannot keep a subnode highlighted.
    // Concurrent jobs may legitimately highlight both substages at once.
    active = activeJobs(step).some(job => job.status === 'running' && typeof job.stage === 'string' && substageNodes[job.stage] === node.id)
  }
  return { status, active, detail: describeStep(step), ...progress(step) }
}

export function diagramStageTarget(pipeline: Pipeline | null | undefined, stage: DiagramStage): PipelineStepId | undefined {
  const actionable = (step: PipelineStep) => step.status === 'running' || step.status === 'waiting' || step.status === 'failed'
  const steps = stage.stepIds.map(id => pipeline?.steps.find(step => step.id === id)).filter((step): step is PipelineStep => Boolean(step))
  return steps.find(step => step.id === pipeline?.current_step && actionable(step))?.id
    ?? steps.find(actionable)?.id ?? stage.stepIds[0]
}

export function diagramStageState(pipeline: Pipeline | null | undefined, stage: DiagramStage): DiagramNodeState {
  if (!stage.stepIds.length) return unavailable()
  const states = stage.children.map(node => diagramNodeState(pipeline, node))
  const target = diagramStageTarget(pipeline, stage)
  const relevant = states.filter((_, index) => stage.children[index]?.stepId === target)
  const representative = relevant.find(state => state.active) ?? relevant[0] ?? states[0]
  if (!representative) return { status: 'pending', active: false, detail: '等待前置步骤' }
  let status: DiagramNodeState['status'] = 'pending'
  if (states.some(state => state.status === 'failed')) status = 'failed'
  else if (states.some(state => state.status === 'running')) status = 'running'
  else if (states.some(state => state.status === 'waiting')) status = 'waiting'
  else if (states.every(state => state.status === 'skipped')) status = 'skipped'
  else if (states.every(state => state.status === 'completed' || state.status === 'skipped')) status = 'completed'
  return { ...representative, status, active: status === 'running' || status === 'waiting' }
}
