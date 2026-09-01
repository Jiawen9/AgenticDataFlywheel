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

export interface CorrectionSource {
  source_id: string
  name: string
  kind: 'annotated_workbook'
  relative_path: string
  size_bytes: number
  package_root: string
}

export interface CorrectionTop1Task {
  task_id: string
  goal: string
  trajectory_id: string
  global_score: number
  passed_threshold: boolean
  trajectory_count: number
  step_count: number
}

export interface CorrectionRecommendation {
  status: 'ready' | 'blocked'
  message?: string
  tree_run_id?: string
  run_id?: string
  tree_completed_at?: string
  quality_completed_at?: string
  completed_at?: string
  total_task_count?: number
  reviewed_task_count?: number
  source_id?: string
  source_path?: string
  source_sha256?: string
  tasks: CorrectionTop1Task[]
  selected_trajectories?: Record<string, string>
}

export interface CorrectionBatch {
  tree_run_id: string
  tree_completed_at: string
  quality_completed_at: string
  total_task_count: number
  reviewed_task_count: number
  status: 'ready'
  is_default: boolean
}

export interface CorrectionRow {
  excel_row: number
  step: number
  task: string
  meta_task: string
  image: string
  image_url: string
  xml: string
  actions: string
  action: ActionPayload
  sop: string
  summary: string
  task_manual_result: string
  micro_manual: string
  macro_manual: string
  micro_pred: string
  macro_pred: string
  Bad_Interval: string
  trajectory_quality_type: string
  actions_box: string
  deleted: boolean
  edited: boolean
  action_edited: boolean
  sop_edited: boolean
  edit_status: string
}

export interface CorrectionGroupSummary {
  group_id: string
  task: string
  meta_task: string
  quality: string
  prefix: string
  export: boolean
  row_count: number
  active_row_count: number
  edited_row_count: number
  action_edit_count: number
}

export interface CorrectionGroup extends CorrectionGroupSummary {
  rows: CorrectionRow[]
}

// View models only: the persisted Top-1 selection and API remain unchanged.
export interface CorrectionTrajectoryItem {
  trajectory_id: string
  rank: number
  global_score: number
  passed_threshold: boolean
  group: CorrectionGroupSummary
}

export interface CorrectionTaskItem {
  task_id: string
  goal: string
  trajectories: CorrectionTrajectoryItem[]
  edited_row_count: number
  export_count: number
}

export interface CorrectionExport {
  export_id: string
  filename: string
  created_at: string
  download_url: string
  sheets: Record<string, number>
  summary?: { rows: number; groups: number }
}

export interface CorrectionSession {
  session_id: string
  source_id: string
  source: CorrectionSource | null
  tree_run_id: string
  selection: CorrectionRecommendation
  created_at: string
  updated_at: string
  row_count: number
  group_count: number
  groups: CorrectionGroupSummary[]
  exports: CorrectionExport[]
}

export interface KnowledgeBaseSummary {
  kind: 'scene_tree' | 'control_prior' | 'resource_prior'
  filename: string
  exists: boolean
  valid: boolean
  rows?: number
  sheets?: string[]
  size_bytes: number
  modified_at?: string
  error?: string
  version?: string | null
}

export interface TaskTypeAppConfig {
  app: string
  reference_example: string
  use_resource_prior: boolean
  control_prior_available?: boolean
  resource_count?: number
}

export interface TaskGenerationTreeNode {
  id: string
  label: string
  kind: 'scene' | 'capability' | 'sub_capability'
  children?: TaskGenerationTreeNode[]
  app_configs?: TaskTypeAppConfig[]
  generatable?: boolean
  scene?: string
  capability?: string
  sub_capability?: string
}

export interface TaskGenerationTree {
  version: string
  scenes: TaskGenerationTreeNode[]
  leaf_count: number
  execution_unit_count: number
  warnings: string[]
}

export interface TaskGenerationSelection {
  node_id: string
  apps: string[]
}

export interface TaskGenerationResult {
  result_id: string
  task_uuid?: string
  pre_task_uuid?: string | null
  pre_dependency?: 'pre_node' | 'zero' | 'weak' | 'strong'
  dependency_group_id?: string
  status?: string
  app: string
  target_app?: string
  scene: string
  capability: string
  sub_capability: string
  task: string
  source_node_id?: string
  source_row?: number
  source_task?: string
  '用例编号'?: string
  '源失败任务'?: string
  '生成的变体任务'?: string
  run?: string
  '审核状态'?: string
  deleted: boolean
  dependency_error?: string
  created_at?: string
  updated_at?: string
}

export interface TaskGenerationJob {
  job_id: string
  kind: 'task_generation' | 'augmentation'
  status: 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'interrupted'
  stage: string
  created_at: string
  started_at: string | null
  completed_at: string | null
  current_item: string | null
  completed_items: number
  total_items: number
  percent: number
  generate_n: number
  input_filename?: string | null
  result_count: number
  errors: Array<{ item_id?: string; stage?: string; error: string }>
  warnings: string[]
  error: string | null
  knowledge_base_version?: string
  task_type_count?: number
  expected_main_tasks?: number
}

export interface TaskGenerationExport {
  filename: string
  created_at: string
  download_url: string
  row_count: number
}
