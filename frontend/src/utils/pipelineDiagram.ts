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
  inherited?: boolean
  status: 'unavailable' | 'pending' | 'running' | 'waiting' | 'completed' | 'skipped' | 'failed'
  active: boolean
  detail: string
  percent?: number
}

// Preserve original module names and order. Legacy runs retain eight steps;
// generation-start runs additionally own a real generation job.
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
    { id: 'rubrics-ranking', label: 'Checklist 融合评分', stepId: 'quality' },
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

export const PIPELINE_DIAGRAM_GROUPS = [
  { id: 'evaluation', label: '模型评测', stageIds: ['evaluation'] },
  { id: 'governance', label: '数据治理', stageIds: ['generation', 'collection', 'quality', 'publishing'] },
  { id: 'training', label: '模型训练', stageIds: ['training', 'model-publishing'] },
] as const

function inherited(): DiagramNodeState { return { status: 'completed', active: false, inherited: true, detail: '前置环节 · 本轮不执行' } }

const substageNodes: Record<string, string> = {
  classifying_and_observing: 'page-summary',
  building: 'tree-building',
  summarizing_trajectories: 'tree-building',
  generating_rubric: 'rubrics-generation',
  evaluating: 'rubrics-ranking',
  checklist_preparing: 'rubrics-ranking',
  checklist_evaluating: 'rubrics-ranking',
  fusing: 'rubrics-ranking',
}
const substageLabels: Record<string, string> = {
  queued: '等待作业执行', scanning: '扫描采集轨迹', converting: '转换轨迹，准备标框',
  annotating: '正在标框', preparing: '准备质检输入',
  classifying_and_observing: '页面分类与总结', building: '构建轨迹树',
  summarizing_trajectories: '生成轨迹摘要', generating_rubric: '生成 Rubrics', evaluating: '轨迹评分与排序',
  publishing: '保存过程件',
  checklist_preparing: '准备 Checklist', checklist_evaluating: 'Checklist 评测中', fusing: '融合评分与节点 Q',
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
  if (pipeline && ['seed-evaluation', 'badcase-extraction'].includes(node.id)) {
    if (!pipeline.evaluation_run_id) return inherited()
    if (node.id === 'badcase-extraction') {
      const index = pipeline.evaluation?.index_state
      return { status: index === 'ready' ? 'completed' : index === 'failed' ? 'failed' : 'waiting', active: index === 'pending', detail: index === 'ready' ? '已整理 BadCase 候选' : index === 'failed' ? 'BadCase 整理失败，可重试' : '等待整理评测结果' }
    }
    const status = pipeline.evaluation?.status
    const complete = ['completed', 'succeeded', 'partial'].includes(status || '')
    return { status: complete ? 'completed' : ['failed', 'interrupted', 'cancelled'].includes(status || '') ? 'failed' : status === 'running' ? 'running' : 'pending', active: !complete && status === 'running', detail: '关联评测 ' + pipeline.evaluation_run_id + (status ? ' · ' + status : '') }
  }
  if (node.id === 'badcase-augmentation' && pipeline) {
    if (pipeline.start_mode !== 'generate') return inherited()
    if (pipeline.generation_kind !== 'augmentation') return { status: 'skipped', active: false, detail: '本轮从场景生成任务，未使用 BadCase 扩增' }
    return diagramNodeState(pipeline, { ...node, id: 'generation-job', stepId: 'generation' })
  }
  if (!node.stepId) return unavailable()
  const step = pipeline?.steps.find(item => item.id === node.stepId)
  if (!step) return { status: 'pending', active: false, detail: pipeline ? '等待前置步骤' : '等待创建 Pipeline' }
  const status = step.status === 'succeeded' ? 'completed' : step.status === 'retry_waiting' ? 'waiting' : step.status
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
  if (stage.id === 'generation' && pipeline?.start_mode === 'generate') return 'generation'
  const actionable = (step: PipelineStep) => step.status === 'running' || step.status === 'retry_waiting' || step.status === 'waiting' || step.status === 'failed'
  const steps = stage.stepIds.map(id => pipeline?.steps.find(step => step.id === id)).filter((step): step is PipelineStep => Boolean(step))
  return steps.find(step => step.id === pipeline?.current_step && actionable(step))?.id
    ?? steps.find(actionable)?.id ?? stage.stepIds[0]
}

export function diagramStageState(pipeline: Pipeline | null | undefined, stage: DiagramStage): DiagramNodeState {
  if (pipeline && stage.id === 'evaluation') return diagramNodeState(pipeline, stage.children[0]!)
  if (pipeline && stage.id === 'generation') {
    if (pipeline.start_mode !== 'generate') return inherited()
    return diagramNodeState(pipeline, { id: 'generation-job', label: stage.label, stepId: 'generation' })
  }
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
