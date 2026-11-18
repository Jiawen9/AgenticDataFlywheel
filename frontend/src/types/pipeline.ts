import type { FactoryConfig, AgentType, ModelOverrides } from '@/phoneFactoryApi'
import type { CorrectionExportProfile } from '@/types'

export type PipelineMode = 'manual' | 'automatic'
export type PipelineStatus = 'waiting_for_evaluation' | 'waiting_for_generation_config' | 'waiting_for_generation_review' | 'running' | 'retry_waiting' | 'paused' | 'waiting_for_correction' | 'failed' | 'terminating' | 'terminated' | 'no_publishable_data' | 'succeeded' | 'published_summary_failed'
export type PipelineStepId = 'evaluation' | 'generation' | 'collection' | 'preprocessing' | 'tree' | 'quality' | 'correction' | 'cot' | 'publication' | 'overview'
export type PipelineStartMode = 'evaluation' | 'generate' | 'collect' | 'existing'
export type PipelineStepStatus = 'pending' | 'running' | 'retry_waiting' | 'waiting' | 'succeeded' | 'skipped' | 'failed'
export interface PipelineJob { job_id?: string; collection_run_id?: string; run_id?: string; status: string; [key: string]: unknown }
export interface PipelineRetryInfo { attempts: number; max_attempts: number; last_error: string | null; next_retry_at: string | null }
export interface PipelineStep {
  id: PipelineStepId; label: string; status: PipelineStepStatus; job_ids: string[];
  percent?: number; message?: string | null; error?: string | null; jobs?: PipelineJob[];
  retry_info?: PipelineRetryInfo | null;
  started_at?: string | null; completed_at?: string | null
}
export interface PipelineCollectionConfig extends ModelOverrides { agent_type?: AgentType; repeat_times?: number; phone_id: string; app: string; vla: string; config: FactoryConfig }
export interface Pipeline {
  generation_attempt?: number;
  generation_kind?: 'initial' | 'augmentation'; generation_job_id?: string | null; feedback_id?: string; parent_pipeline_id?: string;
  evaluation_run_id?: string; source_evaluation_run_id?: string; evaluation_attempt?: number;
  next_pipeline_ids?: string[]; next_rounds?: Array<{ pipeline_id: string; generation_job_id?: string; status: string; generated_count?: number | null }>;
  evaluation?: { index_state?: string; run_id?: string; badcases?: Record<string, number>; status?: string; summary?: Record<string, number>; final?: boolean };

  export_profile?: CorrectionExportProfile; cot_policy?: 'generate' | 'skip';
  pipeline_id: string; name: string; batch_id: string | null; mode: PipelineMode; start_mode: PipelineStartMode;
  batch_role?: 'collection_result'; task_source_batch_id?: string;
  threshold?: number | null; task_ids: string[]; collection_config?: PipelineCollectionConfig | null;
  status: PipelineStatus; current_step: PipelineStepId; storage_revision: number; created_at: string; updated_at: string;
  history_only?: boolean; error?: string | null; steps: PipelineStep[]; session_id?: string | null; release_id?: string | null;
  collection_run_ids: string[]; selection?: Record<string, unknown> | null
}
export interface CreatePipeline {
  generation_kind?: 'initial' | 'augmentation'; feedback_id?: string; parent_pipeline_id?: string;

  export_profile?: CorrectionExportProfile; cot_policy?: 'generate' | 'skip';
  request_id: string; name: string; batch_id?: string; start_mode: PipelineStartMode; mode: PipelineMode;
  threshold?: number; collection_config?: PipelineCollectionConfig
}
export type PipelineAction = 'pause' | 'resume' | 'retry' | 'confirm-correction' | 'terminate'
export const PIPELINE_TERMINAL: PipelineStatus[] = ['terminated', 'no_publishable_data', 'succeeded', 'published_summary_failed']
export const pipelineStatusLabels: Record<PipelineStatus, string> = {
  waiting_for_evaluation: '等待评测与 BadCase 回流', waiting_for_generation_config: '等待配置任务生成', waiting_for_generation_review: '等待审核生成任务',
  running: '正在执行', retry_waiting: '等待自动重试', paused: '已暂停', waiting_for_correction: '等待人工修正', failed: '执行失败',
  terminating: '正在终止，等待作业结束', terminated: '已终止', no_publishable_data: '无可发布数据',
  succeeded: '已发布并完成汇总', published_summary_failed: '已发布，汇总失败',
}
export const pipelineStepLabels: Record<PipelineStepStatus, string> = {
  pending: '等待前置步骤', running: '执行中', retry_waiting: '等待自动重试', waiting: '等待人工确认', succeeded: '已完成', skipped: '已跳过', failed: '失败',
}
export const pipelineStepPaths: Record<PipelineStepId, string> = {
  evaluation: '/model-iteration-evaluation',
  generation: '/task-generation/generate', collection: '/collection/phone-factory', preprocessing: '/collection/tree-building', tree: '/collection/tree-building',
  quality: '/quality', correction: '/correction/expert-action', cot: '/correction/cot-generation',
  publication: '/data-publishing/archive', overview: '/data-publishing/archive',
}
export function pipelineEvaluationLocation(pipeline: Pipeline, showBadcases = false) {
  const runId = pipeline.evaluation_run_id || pipeline.source_evaluation_run_id
  return { path: pipelineStepPaths.evaluation, query: {
    evaluation_pipeline_id: pipeline.pipeline_id,
    ...(runId ? { run_id: runId } : {}),
    ...(showBadcases ? { badcases: '1' } : {}),
  } }
}
export function pipelineStepLocation(pipeline: Pipeline, stepId: PipelineStepId) {
  if (stepId === 'evaluation') return pipelineEvaluationLocation(pipeline)
  return { path: stepId === 'generation' && pipeline.generation_kind === 'augmentation' ? '/task-generation/augmentation' : pipelineStepPaths[stepId], query: {
    pipeline_id: pipeline.pipeline_id, step_id: stepId, ...(pipeline.batch_id ? { batch_id: pipeline.batch_id } : {}),
    ...(stepId === 'generation' && pipeline.generation_job_id ? { job_id: pipeline.generation_job_id } : {}),
    ...(stepId === 'generation' && pipeline.feedback_id ? { feedback_id: pipeline.feedback_id } : {}),
    ...(pipeline.release_id && (stepId === 'publication' || stepId === 'overview') ? { release_id: pipeline.release_id } : {}),
  } }
}
