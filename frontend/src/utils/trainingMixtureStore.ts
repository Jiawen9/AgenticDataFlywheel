import type { TrainingMixtureJob, TrainingMixtureState } from '@/types'
import type { StorageLike } from './modelPublishingStore'

export const TRAINING_MIXTURE_STORAGE_KEY = 'agentic-data-flywheel.training-mixture.v1'
export const TRAINING_MIXTURE_EVENT = 'agentic-data-flywheel:training-mixture-changed'

export interface CreateTrainingMixtureInput {
  datasetReleaseId: string
  datasetName: string
  studentArtifactId: string
  studentModelName: string
  teacherModelName: string
  teacherModelPath: string
  maxWorkers: number
  passK: number
  outputDirectory: string
}

function browserStorage(): StorageLike | null { return typeof window === 'undefined' ? null : window.localStorage }
export function emptyTrainingMixtureState(): TrainingMixtureState { return { version: 1, jobs: [] } }

export function trainingDatasetPair(job: Pick<TrainingMixtureJob, 'result_filename' | 'output_directory'>) {
  const stem = job.result_filename.replace(/\.xlsx$/i, '')
  const directory = job.output_directory.replaceAll('\\', '/').replace(/\/+$/, '')
  const trainFilename = `${stem}-train.xlsx`
  const evalFilename = `${stem}-eval.xlsx`
  return {
    train_dataset_filename: trainFilename,
    train_dataset_path: `${directory}/${trainFilename}`,
    eval_dataset_filename: evalFilename,
    eval_dataset_path: `${directory}/${evalFilename}`,
  }
}

function normalizeJob(job: TrainingMixtureJob): TrainingMixtureJob {
  const pair = trainingDatasetPair(job)
  return {
    ...job,
    train_dataset_filename: job.train_dataset_filename || pair.train_dataset_filename,
    train_dataset_path: job.train_dataset_path || pair.train_dataset_path,
    eval_dataset_filename: job.eval_dataset_filename || pair.eval_dataset_filename,
    eval_dataset_path: job.eval_dataset_path || pair.eval_dataset_path,
  }
}

export function readTrainingMixtureState(storage: StorageLike | null = browserStorage()): TrainingMixtureState {
  if (!storage) return emptyTrainingMixtureState()
  const raw = storage.getItem(TRAINING_MIXTURE_STORAGE_KEY)
  if (!raw) return emptyTrainingMixtureState()
  try {
    const value = JSON.parse(raw) as Partial<TrainingMixtureState>
    if (value.version !== 1 || !Array.isArray(value.jobs)) throw new Error('unsupported state')
    return { version: 1, jobs: value.jobs.map(job => normalizeJob(job as TrainingMixtureJob)) }
  } catch {
    storage.removeItem(TRAINING_MIXTURE_STORAGE_KEY)
    return emptyTrainingMixtureState()
  }
}

export function saveTrainingMixtureState(state: TrainingMixtureState, storage: StorageLike | null = browserStorage()): void {
  if (!storage) return
  storage.setItem(TRAINING_MIXTURE_STORAGE_KEY, JSON.stringify(state))
  if (typeof window !== 'undefined' && storage === window.localStorage) window.dispatchEvent(new CustomEvent(TRAINING_MIXTURE_EVENT))
}

function safeName(value: string): string {
  return value.trim().replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').replace(/\s+/g, '_') || 'unnamed'
}

function randomJobId(): string {
  const token = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID().replaceAll('-', '').slice(0, 16)
    : `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`.slice(0, 16)
  return `mix_${token}`
}

export function createTrainingMixtureJob(
  input: CreateTrainingMixtureInput,
  storage: StorageLike | null = browserStorage(),
  jobId = randomJobId(),
  createdAt = new Date().toISOString(),
): TrainingMixtureJob {
  const required = [input.datasetReleaseId, input.datasetName, input.studentArtifactId, input.studentModelName, input.teacherModelName, input.teacherModelPath, input.outputDirectory]
  if (required.some(value => !String(value || '').trim())) throw new Error('请完整选择数据集、Student 模型并配置 Teacher 模型和结果目录')
  if (!Number.isInteger(input.maxWorkers) || input.maxWorkers < 1 || input.maxWorkers > 64) throw new Error('max_workers 必须是 1～64 的整数')
  if (!Number.isInteger(input.passK) || input.passK < 1 || input.passK > 20) throw new Error('Pass@K 的 K 必须是 1～20 的整数')
  const resultFilename = `${safeName(input.teacherModelName)}-${safeName(input.studentModelName)}-${safeName(input.datasetName)}.xlsx`
  const outputDirectory = input.outputDirectory.trim().replaceAll('\\', '/').replace(/\/+$/, '')
  const datasetPair = trainingDatasetPair({ result_filename: resultFilename, output_directory: outputDirectory })
  const job: TrainingMixtureJob = {
    job_id: jobId,
    dataset_release_id: input.datasetReleaseId,
    dataset_name: input.datasetName.trim(),
    student_artifact_id: input.studentArtifactId,
    student_model_name: input.studentModelName.trim(),
    teacher_model_name: input.teacherModelName.trim(),
    teacher_model_path: input.teacherModelPath.trim().replaceAll('\\', '/'),
    max_workers: input.maxWorkers,
    pass_k: input.passK,
    output_directory: outputDirectory,
    result_filename: resultFilename,
    result_path: `${outputDirectory}/${resultFilename}`,
    ...datasetPair,
    status: 'running',
    stage: 'student_pass_at_k',
    progress: 0,
    created_at: createdAt,
    completed_at: null,
    error: null,
  }
  const state = readTrainingMixtureState(storage)
  state.jobs.unshift(job)
  saveTrainingMixtureState(state, storage)
  return job
}

export function updateTrainingMixtureJob(job: TrainingMixtureJob, storage: StorageLike | null = browserStorage()): void {
  const state = readTrainingMixtureState(storage)
  const index = state.jobs.findIndex(item => item.job_id === job.job_id)
  if (index >= 0) state.jobs[index] = { ...job }
  else state.jobs.unshift({ ...job })
  saveTrainingMixtureState(state, storage)
}
