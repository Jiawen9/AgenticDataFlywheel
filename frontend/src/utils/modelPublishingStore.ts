import type { ModelPublishingState, ModelTrainingMonitor } from '@/types'

export const MODEL_PUBLISHING_STORAGE_KEY = 'agentic-data-flywheel.model-publishing.v1'
export const MODEL_PUBLISHING_EVENT = 'agentic-data-flywheel:model-publishing-changed'

export interface StorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export interface CreateMonitorInput {
  trainingName: string
  targetModel: string
  resultDirectory: string
  modelArtifactId?: string
  optimizationAlgorithm?: 'CHORD' | 'OPSD' | 'GKD' | 'OPD-RL'
  trainingJob?: string
  mixtureJobId?: string
  trainDatasetPath?: string
  evalDatasetPath?: string
  cardsPerNode?: number
  instanceCount?: number
}

export function emptyModelPublishingState(): ModelPublishingState {
  return { version: 1, monitors: [], artifacts: [] }
}

function browserStorage(): StorageLike | null {
  return typeof window === 'undefined' ? null : window.localStorage
}

export function readModelPublishingState(storage: StorageLike | null = browserStorage()): ModelPublishingState {
  if (!storage) return emptyModelPublishingState()
  const raw = storage.getItem(MODEL_PUBLISHING_STORAGE_KEY)
  if (!raw) return emptyModelPublishingState()
  try {
    const value = JSON.parse(raw) as Partial<ModelPublishingState>
    if (value.version !== 1 || !Array.isArray(value.monitors) || !Array.isArray(value.artifacts)) throw new Error('unsupported state')
    return { version: 1, monitors: value.monitors, artifacts: value.artifacts }
  } catch {
    storage.removeItem(MODEL_PUBLISHING_STORAGE_KEY)
    return emptyModelPublishingState()
  }
}

export function saveModelPublishingState(state: ModelPublishingState, storage: StorageLike | null = browserStorage()): void {
  if (!storage) return
  storage.setItem(MODEL_PUBLISHING_STORAGE_KEY, JSON.stringify(state))
  if (typeof window !== 'undefined' && storage === window.localStorage) window.dispatchEvent(new CustomEvent(MODEL_PUBLISHING_EVENT))
}

function monitorId(): string {
  const uuid = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID().replaceAll('-', '').slice(0, 16)
    : `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`.slice(0, 16)
  return `monitor_${uuid}`
}

export function createTrainingMonitor(
  input: CreateMonitorInput,
  storage: StorageLike | null = browserStorage(),
  id = monitorId(),
  startedAt = new Date().toISOString(),
): ModelTrainingMonitor {
  const trainingName = input.trainingName.trim()
  const targetModel = input.targetModel.trim()
  const resultDirectory = input.resultDirectory.trim()
  if (!trainingName || !targetModel || !resultDirectory) throw new Error('训练名称、目标模型和结果监测目录均不能为空')
  if (input.cardsPerNode !== undefined && (!Number.isInteger(input.cardsPerNode) || input.cardsPerNode < 1 || input.cardsPerNode > 16)) throw new Error('单节点卡数必须是 1～16 的整数')
  if (input.instanceCount !== undefined && (!Number.isInteger(input.instanceCount) || input.instanceCount < 1 || input.instanceCount > 128)) throw new Error('实例数必须是 1～128 的整数')
  const monitor: ModelTrainingMonitor = {
    monitor_id: id,
    training_name: trainingName,
    target_model: targetModel,
    result_directory: resultDirectory.replaceAll('\\', '/'),
    started_at: startedAt,
    status: 'monitoring',
    ...(input.modelArtifactId ? { model_artifact_id: input.modelArtifactId } : {}),
    ...(input.optimizationAlgorithm ? { optimization_algorithm: input.optimizationAlgorithm } : {}),
    ...(input.trainingJob ? { training_job: input.trainingJob } : {}),
    ...(input.mixtureJobId ? { mixture_job_id: input.mixtureJobId } : {}),
    ...(input.trainDatasetPath ? { train_dataset_path: input.trainDatasetPath.replaceAll('\\', '/') } : {}),
    ...(input.evalDatasetPath ? { eval_dataset_path: input.evalDatasetPath.replaceAll('\\', '/') } : {}),
    ...(input.cardsPerNode !== undefined ? { cards_per_node: input.cardsPerNode } : {}),
    ...(input.instanceCount !== undefined ? { instance_count: input.instanceCount } : {}),
  }
  const state = readModelPublishingState(storage)
  state.monitors.unshift(monitor)
  saveModelPublishingState(state, storage)
  return monitor
}
