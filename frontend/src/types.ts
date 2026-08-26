export type ActionPayload = Record<string, unknown>

export interface TaskSummary {
  task_id: string
  goal: string
  warning: string
  first_trajectory: string
  trajectory_count: number
  step_count: number
  annotated: boolean
}

export interface TrajectoryStep {
  step: number
  excel_row: number
  image: string
  image_url: string
  xml: string
  action_text: string
  action: ActionPayload
  action_summary: string
  actions_box: string
}

export interface TrajectoryRecord {
  trajectory_id: string
  step_count: number
  steps: TrajectoryStep[]
}

export interface TrajectorySummary {
  trajectory_id: string
  step_count: number
}

export interface BuildJob {
  job_id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted'
  stage: string
  task_ids: string[]
  created_at: string
  started_at: string | null
  completed_at: string | null
  current_task: string | null
  task_index: number
  total_tasks: number
  classified_steps: number
  total_steps: number
  summarized_trajectories?: number
  total_trajectories?: number
  percent: number
  error: string | null
  run_id: string | null
}

export interface QualityJob {
  job_id: string
  run_id: string
  task_ids: string[]
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted'
  stage: 'queued' | 'preparing' | 'generating_rubric' | 'evaluating' | 'publishing' | 'succeeded' | 'failed' | 'interrupted'
  created_at: string
  started_at: string | null
  completed_at: string | null
  current_task: string | null
  current_trajectory: string | null
  completed_trajectories: number
  total_trajectories: number
  percent: number
  error: string | null
}

export interface QualityTaskSummary {
  task_id: string
  status: 'unreviewed' | 'succeeded'
  rubric_ready: boolean
  trajectory_count?: number
  average_score?: number
  passed_count?: number
  updated_at?: string
}

export interface RunQualitySummary {
  run_id: string
  updated_at?: string
  tasks: QualityTaskSummary[]
}

export interface DimensionEvaluation {
  dimension_id?: string
  dimension_name: string
  score: number
  rationale?: string
  confidence?: number
}

export interface StepQualityEvaluation {
  step_id: number
  dimension_scores: DimensionEvaluation[]
  step_quality_summary?: string
}

export interface TrajectoryQualityEvaluation {
  trajectory_id: string
  global_score: number
  passed_threshold: boolean
  dimension_global_scores: DimensionEvaluation[] | Record<string, number>
  step_evaluations: StepQualityEvaluation[]
  evaluation_config?: Record<string, unknown>
}

export interface TaskQualityResult {
  run_id: string
  task_id: string
  completed_at: string
  rubric: Record<string, unknown>
  evaluations: Record<string, TrajectoryQualityEvaluation>
  average_score: number
  passed_count: number
}

export interface TreeRunTask {
  task_id: string
  goal: string
  tree_file: string
  trajectory_count: number
  original_step_count: number
  tree_step_count: number
  ignored_step_count: number
  action_node_count: number
}

export interface TreeRun {
  run_id: string
  completed_at: string
  model_name: string
  task_ids: string[]
  task_count: number
  total_original_steps: number
  total_tree_steps: number
  tasks: TreeRunTask[]
}

export interface ClassificationResult {
  is_intermediate: boolean
  category: string
  confidence: number
  reason: string
  effective_intermediate: boolean
  uncertain: boolean
}

export interface TreeOccurrence {
  trajectory: string
  step: number
  excel_row: number
  image: string
  xml: string
  action: ActionPayload
  action_text: string
  summary: string
  observation?: string
  actions_box: string
  score: number
  reused: boolean
  classification: ClassificationResult | null
}

export interface AuditStep {
  step: number
  image: string
  xml: string
  action: ActionPayload
  action_text: string
  summary: string
  observation?: string
  actions_box: string
  classification: ClassificationResult | null
  counted_in_tree: boolean
  decision: string
  decision_source: string
}

export interface SourceTrajectory {
  trajectory: string
  original_step_count: number
  tree_step_count: number
  ignored_incidental_step_count: number
  steps: AuditStep[]
}

export interface TrajectoryTreeNode {
  id: number
  depth: number
  label: string
  action: ActionPayload
  summary: string
  observation?: string
  actions_box: string
  image: string
  xml: string
  reference_trajectory: string
  reference_step: number
  occurrence_count: number
  occurrences: TreeOccurrence[]
  terminal_trajectories: string[]
  children: TrajectoryTreeNode[]
  task_id?: string
  trajectory_count?: number
  original_step_count?: number
  tree_step_count?: number
  ignored_incidental_step_count?: number
  classification_category_counts?: Record<string, number>
  source_trajectories?: SourceTrajectory[]
}
