import { describe, expect, it } from 'vitest'
import type { Pipeline, PipelineStep, PipelineStepId } from '@/types/pipeline'
import { PIPELINE_DIAGRAM_STAGES, diagramNodeState, diagramStageState, diagramStageTarget } from './pipelineDiagram'

function pipeline(...overrides: (Partial<PipelineStep> & { id: PipelineStepId })[]): Pipeline {
  const ids: PipelineStepId[] = ['collection', 'preprocessing', 'tree', 'quality', 'correction', 'cot', 'publication', 'overview']
  return {
    pipeline_id: 'pipeline-1', name: '真实流程', batch_id: 'batch-1', mode: 'manual', start_mode: 'existing',
    task_ids: ['A', 'B'], status: 'running', current_step: 'tree', storage_revision: 4,
    created_at: '2026-09-22', updated_at: '2026-09-22', collection_run_ids: [],
    steps: ids.map(id => ({ id, label: id, status: 'pending', job_ids: [], ...overrides.find(step => step.id === id) })),
  }
}
const node = (id: string) => PIPELINE_DIAGRAM_STAGES.flatMap(stage => stage.children).find(item => item.id === id)!
const stage = (id: string) => PIPELINE_DIAGRAM_STAGES.find(item => item.id === id)!

describe('original Pipeline diagram with real execution state', () => {
  it('preserves the seven original modules and all fifteen substeps', () => {
    expect(PIPELINE_DIAGRAM_STAGES.map(item => item.label)).toEqual(['迭代评估', '任务生成', '轨迹采集', '轨迹质检', '数据发布', '模型训练', '模型发布'])
    expect(PIPELINE_DIAGRAM_STAGES.flatMap(item => item.children.map(child => child.label))).toEqual([
      '种子任务集评测', 'BadCase 提取', 'BadCase 扩增', '手机工厂并行采集', '标框', '页面总结', '轨迹树构建',
      'Rubrics 生成', 'Rubrics 相对排序', '训练数据归档', '训练数据配比', '训练集/验证集划分', '模型训练', '训练有效性验证', '增训模型归档',
    ])
  })

  it('keeps unsupported modules unavailable even after a successful full run', () => {
    const p = pipeline()
    p.status = 'succeeded'
    p.steps.forEach(step => { step.status = 'succeeded' })
    for (const id of ['evaluation', 'generation', 'training', 'model-publishing']) {
      expect(diagramStageState(p, stage(id))).toEqual({ status: 'unavailable', active: false, detail: '尚未接入自动执行' })
      expect(diagramStageTarget(p, stage(id))).toBeUndefined()
      for (const item of stage(id).children) expect(diagramNodeState(p, item).status).toBe('unavailable')
    }
  })

  it('shows waiting before creation without manufacturing progress', () => {
    expect(diagramNodeState(null, node('bounding-box'))).toEqual({ status: 'pending', active: false, detail: '等待创建 Pipeline' })
    expect(diagramStageState(undefined, stage('collection')).status).toBe('pending')
    expect(diagramStageTarget(null, stage('collection'))).toBe('collection')
  })

  it.each([
    ['scanning', '扫描采集轨迹'], ['converting', '转换轨迹，准备标框'],
    ['annotating', '正在标框'], ['publishing', '保存过程件'],
  ])('reports the real preprocessing phase %s', (substage, label) => {
    const p = pipeline({ id: 'preprocessing', status: 'running', percent: 42, jobs: [{ status: 'running', stage: substage }] })
    expect(diagramNodeState(p, node('bounding-box'))).toMatchObject({ status: 'running', active: true, percent: 42 })
    expect(diagramNodeState(p, node('bounding-box')).detail).toContain(label)
  })

  it('reports existing trajectory reuse from the backend message', () => {
    const state = diagramNodeState(pipeline({ id: 'collection', status: 'succeeded', message: '接续已有轨迹' }), node('phone-factory'))
    expect(state).toMatchObject({ status: 'completed', active: false })
    expect(state.detail).toContain('接续已有轨迹')
  })

  it('does not equate remote device completion with registered collection completion', () => {
    const state = diagramNodeState(pipeline({ id: 'collection', status: 'running', message: '等待回传登记', jobs: [{ status: 'completed' }] }), node('phone-factory'))
    expect(state.status).toBe('running')
    expect(state.detail).toContain('等待回传登记')
  })

  it('follows alternating per-task tree phases without prematurely completing the other node', () => {
    const p = pipeline({ id: 'tree', status: 'running', percent: 57, jobs: [{ status: 'running', stage: 'classifying_and_observing' }] })
    for (const substage of ['classifying_and_observing', 'building', 'classifying_and_observing', 'summarizing_trajectories']) {
      p.steps[2]!.jobs![0]!.stage = substage
      const observing = substage === 'classifying_and_observing'
      expect(diagramNodeState(p, node('page-summary'))).toMatchObject({ status: 'running', active: observing, percent: 57 })
      expect(diagramNodeState(p, node('tree-building'))).toMatchObject({ status: 'running', active: !observing, percent: 57 })
    }
    p.steps[2]!.status = 'succeeded'
    for (const id of ['page-summary', 'tree-building']) expect(diagramNodeState(p, node(id))).toMatchObject({ status: 'completed', active: false })
  })

  it('represents concurrent jobs in different tree phases and ignores completed and queued jobs', () => {
    const p = pipeline({ id: 'tree', status: 'running', jobs: [
      { status: 'running', stage: 'classifying_and_observing' }, { status: 'running', stage: 'building' },
    ] })
    expect(diagramNodeState(p, node('page-summary')).active).toBe(true)
    expect(diagramNodeState(p, node('tree-building')).active).toBe(true)
    p.steps[2]!.jobs![0]!.status = 'succeeded'
    expect(diagramNodeState(p, node('page-summary')).active).toBe(false)
    p.steps[2]!.jobs![1]!.status = 'queued'
    expect(diagramNodeState(p, node('tree-building')).active).toBe(false)
    expect(diagramNodeState(p, node('tree-building')).status).toBe('running')
  })

  it('shares quality progress and handles rubric cache reuse without inventing rubric completion', () => {
    const p = pipeline({ id: 'quality', status: 'running', percent: 80, jobs: [{ status: 'running', stage: 'evaluating' }] })
    expect(diagramNodeState(p, node('rubrics-generation'))).toMatchObject({ status: 'running', active: false, percent: 80 })
    expect(diagramNodeState(p, node('rubrics-ranking'))).toMatchObject({ status: 'running', active: true, percent: 80 })
    expect(diagramNodeState(p, node('rubrics-ranking')).detail).toContain('共享整批质检作业进度')
    p.steps[3]!.jobs![0]!.stage = 'generating_rubric'
    expect(diagramNodeState(p, node('rubrics-generation')).active).toBe(true)
    expect(diagramNodeState(p, node('rubrics-ranking')).active).toBe(false)
    p.steps[3]!.status = 'succeeded'
    for (const id of ['rubrics-generation', 'rubrics-ranking']) expect(diagramNodeState(p, node(id)).status).toBe('completed')
  })

  it.each(['publishing', 'preparing', 'cache_reused', ''])('uses the parent job state when the shared substage is %s', substage => {
    const p = pipeline({ id: 'tree', status: 'running', message: '复用有效缓存', jobs: [{ status: 'running', stage: substage }] })
    for (const id of ['page-summary', 'tree-building']) {
      const state = diagramNodeState(p, node(id))
      expect(state).toMatchObject({ status: 'running', active: false })
      expect(state.percent).toBeUndefined()
      expect(state.detail).toContain('复用有效缓存')
      expect(state.detail).toContain('以所属作业状态为准')
    }
    expect(diagramStageState(p, stage('collection'))).toMatchObject({ status: 'running', active: true })
  })

  it('does not infer completion from 100 percent until the parent step succeeds', () => {
    const p = pipeline({ id: 'quality', status: 'running', percent: 100, jobs: [{ status: 'succeeded', stage: 'succeeded' }] })
    expect(diagramNodeState(p, node('rubrics-ranking'))).toMatchObject({ status: 'running', active: false, percent: 100 })
  })

  it('keeps failures and skipped reasons visible', () => {
    const p = pipeline({ id: 'tree', status: 'failed', error: '输入已失效' }, { id: 'publication', status: 'skipped', message: '无可发布数据' })
    expect(diagramNodeState(p, node('page-summary')).detail).toContain('输入已失效')
    expect(diagramStageState(p, stage('collection')).status).toBe('failed')
    expect(diagramStageState(p, stage('publishing'))).toMatchObject({ status: 'skipped', active: false })
    expect(diagramNodeState(p, node('training-data-archive')).detail).toContain('无可发布数据')
  })

  it('targets the current running, waiting, or failed step before the first step', () => {
    const p = pipeline({ id: 'collection', status: 'succeeded' }, { id: 'preprocessing', status: 'failed' }, { id: 'tree', status: 'running' })
    expect(diagramStageTarget(p, stage('collection'))).toBe('tree')
    p.current_step = 'preprocessing'
    expect(diagramStageTarget(p, stage('collection'))).toBe('preprocessing')
    p.steps[1]!.status = 'waiting'
    expect(diagramStageTarget(p, stage('collection'))).toBe('preprocessing')
    p.steps[1]!.status = 'succeeded'
    p.steps[2]!.status = 'succeeded'
    expect(diagramStageTarget(p, stage('collection'))).toBe('collection')
    expect(diagramStageState(p, stage('collection')).status).toBe('completed')
  })

  it('does not complete a module while a dependent backend step is pending', () => {
    const p = pipeline({ id: 'collection', status: 'succeeded' }, { id: 'preprocessing', status: 'succeeded' })
    expect(diagramStageState(p, stage('collection')).status).toBe('pending')
  })

  it('does not let summary failure undo successful publication or light up training', () => {
    const p = pipeline({ id: 'publication', status: 'succeeded', percent: 100 }, { id: 'overview', status: 'failed', error: '转换失败' })
    p.status = 'published_summary_failed'
    p.current_step = 'overview'
    expect(diagramStageState(p, stage('publishing'))).toMatchObject({ status: 'completed', active: false, percent: 100 })
    expect(diagramStageTarget(p, stage('publishing'))).toBe('publication')
    expect(diagramStageState(p, stage('training')).status).toBe('unavailable')
  })

  it('normalizes only supplied finite percentages and never derives them from children', () => {
    expect(diagramNodeState(pipeline({ id: 'tree', percent: Number.NaN }), node('page-summary')).percent).toBeUndefined()
    expect(diagramNodeState(pipeline({ id: 'tree', percent: 125 }), node('page-summary')).percent).toBe(100)
    expect(diagramNodeState(pipeline({ id: 'tree', percent: -5 }), node('page-summary')).percent).toBe(0)
  })
})
