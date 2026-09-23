import type { FactoryConfig } from '@/phoneFactoryApi'

export type PipelineMode = 'manual' | 'automatic'
export type PipelineStatus = 'running' | 'paused' | 'waiting_for_correction' | 'failed' | 'terminating' | 'terminated' | 'no_publishable_data' | 'succeeded' | 'published_summary_failed'
export type PipelineStepId = 'collection' | 'preprocessing' | 'tree' | 'quality' | 'correction' | 'cot' | 'publication' | 'overview'
export type PipelineStepStatus = 'pending' | 'running' | 'waiting' | 'succeeded' | 'skipped' | 'failed'
export interface PipelineJob { job_id?: string; collection_run_id?: string; run_id?: string; status: string; [key: string]: unknown }
export interface PipelineStep {
  id: PipelineStepId; label: string; status: PipelineStepStatus; job_ids: string[];
  percent?: number; message?: string | null; error?: string | null; jobs?: PipelineJob[];
  started_at?: string | null; completed_at?: string | null
}
export interface PipelineCollectionConfig { phone_id: string; app: string; vla: string; config: FactoryConfig }
export interface Pipeline {
  pipeline_id: string; name: string; batch_id: string; mode: PipelineMode; start_mode: 'collect' | 'existing';
  threshold?: number | null; task_ids: string[]; collection_config?: PipelineCollectionConfig | null;
  status: PipelineStatus; current_step: PipelineStepId; storage_revision: number; created_at: string; updated_at: string;
  history_only?: boolean; error?: string | null; steps: PipelineStep[]; session_id?: string | null; release_id?: string | null;
  collection_run_ids: string[]; selection?: Record<string, unknown> | null
}
export interface CreatePipeline {
  request_id: string; name: string; batch_id: string; start_mode: 'collect' | 'existing'; mode: PipelineMode;
  threshold?: number; collection_config?: PipelineCollectionConfig
}
export type PipelineAction = 'pause' | 'resume' | 'retry' | 'confirm-correction' | 'terminate'
export const PIPELINE_TERMINAL: PipelineStatus[] = ['terminated', 'no_publishable_data', 'succeeded', 'published_summary_failed']
export const pipelineStatusLabels: Record<PipelineStatus, string> = {
  running: '正在执行', paused: '已暂停', waiting_for_correction: '等待人工修正', failed: '执行失败',
  terminating: '正在终止，等待作业结束', terminated: '已终止', no_publishable_data: '无可发布数据',
  succeeded: '已发布并完成汇总', published_summary_failed: '已发布，汇总失败',
}
export const pipelineStepLabels: Record<PipelineStepStatus, string> = {
  pending: '等待前置步骤', running: '执行中', waiting: '等待人工确认', succeeded: '已完成', skipped: '已跳过', failed: '失败',
}
export const pipelineStepPaths: Record<PipelineStepId, string> = {
  collection: '/collection/phone-factory', preprocessing: '/collection/tree-building', tree: '/collection/tree-building',
  quality: '/quality', correction: '/correction/expert-action', cot: '/correction/cot-generation',
  publication: '/data-publishing/archive', overview: '/data-publishing/archive',
}
export function pipelineStepLocation(pipeline: Pipeline, stepId: PipelineStepId) {
  return { path: pipelineStepPaths[stepId], query: {
    pipeline_id: pipeline.pipeline_id, step_id: stepId, batch_id: pipeline.batch_id,
    ...(pipeline.release_id && (stepId === 'publication' || stepId === 'overview') ? { release_id: pipeline.release_id } : {}),
  } }
}