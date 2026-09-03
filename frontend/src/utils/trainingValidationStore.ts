import type {
  ModelArtifact,
  ModelPublishingState,
  ModelTrainingMonitor,
  TrainingMixtureJob,
  TrainingMixtureState,
  TrainingValidationMetricGroup,
  TrainingValidationReport,
  TrainingValidationState,
} from '@/types'
import type { StorageLike } from './modelPublishingStore'

export const TRAINING_VALIDATION_STORAGE_KEY = 'agentic-data-flywheel.training-validation.v1'
export const TRAINING_VALIDATION_EVENT = 'agentic-data-flywheel:training-validation-changed'
export const VALIDATION_COLUMNS = [
  'Tier 1（极易）', 'Tier 2（简单）', 'Tier 3（中等）', 'Tier 4（困难）', 'Tier 5（极难）',
  '配比数据', '价值数据', '修正内化', '经验内化', '数据总量',
] as const

const SAMPLE_COUNTS = [2578, 318, 419, 351, 1643, 2896, 2413, 1207, 1206, 5309]
const PASS_AT_K: TrainingValidationMetricGroup = {
  teacher: [0.9828, 0.9528, 0.9365, 0.8940, 0.7776, 0.9795, 0.8221, 0.7626, 0.8818, 0.9080],
  student_before: [1.0000, 0.8000, 0.6000, 0.4000, 0.0458, 0.9780, 0.1935, 0.1364, 0.2507, 0.6215],
  student_after: [0.9679, 0.8396, 0.7742, 0.6838, 0.3541, 0.9538, 0.4750, 0.3592, 0.5909, 0.7362],
  improvement: [-0.0321, 0.0396, 0.1742, 0.2838, 0.3083, -0.0242, 0.2815, 0.2229, 0.3401, 0.1147],
}
const ACCURACY: TrainingValidationMetricGroup = {
  teacher: [0.9950, 0.9717, 0.9570, 0.9145, 0.8138, 0.9924, 0.8533, 0.7987, 0.9080, 0.9292],
  student_before: [0.9876, 0.8050, 0.6706, 0.4188, 0.0700, 0.9675, 0.2250, 0.1624, 0.2877, 0.6301],
  student_after: [0.9639, 0.8333, 0.8043, 0.7009, 0.3494, 0.9496, 0.4795, 0.3463, 0.6128, 0.7359],
  improvement: [-0.0237, 0.0283, 0.1337, 0.2821, 0.2794, -0.0180, 0.2545, 0.1839, 0.3250, 0.1059],
}

export interface EligibleTrainingSession {
  monitor: ModelTrainingMonitor
  mixtureJob: TrainingMixtureJob
  checkpoints: ModelArtifact[]
}

function browserStorage(): StorageLike | null { return typeof window === 'undefined' ? null : window.localStorage }
export function emptyTrainingValidationState(): TrainingValidationState { return { version: 1, reports: [] } }

export function readTrainingValidationState(storage: StorageLike | null = browserStorage()): TrainingValidationState {
  if (!storage) return emptyTrainingValidationState()
  const raw = storage.getItem(TRAINING_VALIDATION_STORAGE_KEY)
  if (!raw) return emptyTrainingValidationState()
  try {
    const value = JSON.parse(raw) as Partial<TrainingValidationState>
    if (value.version !== 1 || !Array.isArray(value.reports)) throw new Error('unsupported state')
    return { version: 1, reports: value.reports }
  } catch {
    storage.removeItem(TRAINING_VALIDATION_STORAGE_KEY)
    return emptyTrainingValidationState()
  }
}

function saveTrainingValidationState(state: TrainingValidationState, storage: StorageLike | null = browserStorage()) {
  if (!storage) return
  storage.setItem(TRAINING_VALIDATION_STORAGE_KEY, JSON.stringify(state))
  if (typeof window !== 'undefined' && storage === window.localStorage) window.dispatchEvent(new CustomEvent(TRAINING_VALIDATION_EVENT))
}

export function eligibleTrainingSessions(modelState: ModelPublishingState, mixtureState: TrainingMixtureState): EligibleTrainingSession[] {
  return modelState.monitors.flatMap((monitor) => {
    if (!monitor.mixture_job_id || !monitor.training_name || !monitor.target_model || !monitor.result_directory) return []
    const mixtureJob = mixtureState.jobs.find(item => item.job_id === monitor.mixture_job_id && item.status === 'succeeded')
    if (!mixtureJob || !mixtureJob.dataset_name || !mixtureJob.result_filename || !mixtureJob.result_path || !Number.isInteger(mixtureJob.pass_k)) return []
    const checkpoints = modelState.artifacts.filter(item => item.monitor_id === monitor.monitor_id && item.release_status !== 'failed' && item.validation_status !== 'failed')
    return checkpoints.length ? [{ monitor, mixtureJob, checkpoints }] : []
  })
}

function reportId() {
  const token = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID().replaceAll('-', '').slice(0, 16)
    : `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`.slice(0, 16)
  return `validation_${token}`
}

function cloneMetric(group: TrainingValidationMetricGroup): TrainingValidationMetricGroup {
  return {
    teacher: [...group.teacher],
    student_before: [...group.student_before],
    student_after: [...group.student_after],
    improvement: [...group.improvement],
  }
}

export function createTrainingValidationReport(
  session: EligibleTrainingSession,
  artifactId: string,
  storage: StorageLike | null = browserStorage(),
  id = reportId(),
  createdAt = new Date().toISOString(),
): TrainingValidationReport {
  const checkpoint = session.checkpoints.find(item => item.artifact_id === artifactId)
  if (!checkpoint) throw new Error('请选择当前训练会话下的有效 checkpoint')
  const { monitor, mixtureJob } = session
  const report: TrainingValidationReport = {
    report_id: id,
    monitor_id: monitor.monitor_id,
    artifact_id: checkpoint.artifact_id,
    training_name: monitor.training_name,
    trained_model: monitor.target_model,
    checkpoint_version: checkpoint.model_version,
    checkpoint_filename: checkpoint.filename,
    dataset_name: mixtureJob.dataset_name,
    mixture_result_filename: mixtureJob.result_filename,
    mixture_result_path: mixtureJob.result_path,
    pass_k: mixtureJob.pass_k,
    optimization_algorithm: monitor.optimization_algorithm,
    training_job: monitor.training_job,
    cards_per_node: monitor.cards_per_node,
    instance_count: monitor.instance_count,
    output_directory: monitor.result_directory,
    sample_counts: [...SAMPLE_COUNTS],
    pass_at_k: cloneMetric(PASS_AT_K),
    accuracy: cloneMetric(ACCURACY),
    created_at: createdAt,
    demo: true,
  }
  const state = readTrainingValidationState(storage)
  state.reports.unshift(report)
  saveTrainingValidationState(state, storage)
  return report
}
