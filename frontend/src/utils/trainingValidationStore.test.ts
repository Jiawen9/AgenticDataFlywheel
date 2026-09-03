import { describe, expect, it } from 'vitest'
import type { ModelArtifact, ModelPublishingState, TrainingMixtureState } from '@/types'
import { createTrainingMonitor, type StorageLike } from './modelPublishingStore'
import { createTrainingMixtureJob } from './trainingMixtureStore'
import {
  createTrainingValidationReport,
  eligibleTrainingSessions,
  readTrainingValidationState,
  TRAINING_VALIDATION_STORAGE_KEY,
  VALIDATION_COLUMNS,
} from './trainingValidationStore'

function memoryStorage(initial?: string): StorageLike & { values: Map<string, string> } {
  const values = new Map<string, string>()
  if (initial !== undefined) values.set(TRAINING_VALIDATION_STORAGE_KEY, initial)
  return { values, getItem: key => values.get(key) ?? null, setItem: (key, value) => { values.set(key, value) }, removeItem: key => { values.delete(key) } }
}

function fixture() {
  const monitor = createTrainingMonitor({
    trainingName: 'GUI 增训', targetModel: 'student-v1', resultDirectory: '/models/output',
    modelArtifactId: 'base_1', optimizationAlgorithm: 'GKD', trainingJob: 'gkd_job', mixtureJobId: 'mix_1',
    trainDatasetPath: '/datasets/train.xlsx', evalDatasetPath: '/datasets/eval.xlsx', cardsPerNode: 8, instanceCount: 2,
  }, null, 'monitor_1', '2026-09-03T10:00:00Z')
  const mixtureJob = createTrainingMixtureJob({
    datasetReleaseId: 'rel_1', datasetName: 'GUI 数据集', studentArtifactId: 'base_1', studentModelName: 'student-v1',
    teacherModelName: 'teacher-v1', teacherModelPath: '/models/teacher', maxWorkers: 4, passK: 8, outputDirectory: '/mixture',
  }, null, 'mix_1', '2026-09-03T09:00:00Z')
  mixtureJob.status = 'succeeded'
  mixtureJob.stage = 'completed'
  const checkpoint: ModelArtifact = {
    artifact_id: 'checkpoint_1', monitor_id: monitor.monitor_id, training_name: monitor.training_name,
    model_version: 'student-v1-step-1000', filename: 'checkpoint-1000.bin', file_size: 1024,
    detected_at: '2026-09-03T11:00:00Z', validation_status: 'pending', release_status: 'detected',
  }
  return {
    monitor,
    mixtureJob,
    checkpoint,
    modelState: { version: 1, monitors: [monitor], artifacts: [checkpoint] } as ModelPublishingState,
    mixtureState: { version: 1, jobs: [mixtureJob] } as TrainingMixtureState,
  }
}

describe('training validation store', () => {
  it('only exposes sessions with a completed mixture job and non-failed checkpoint', () => {
    const data = fixture()
    const failed = { ...data.checkpoint, artifact_id: 'failed', release_status: 'failed' as const }
    data.modelState.artifacts.push(failed)
    const sessions = eligibleTrainingSessions(data.modelState, data.mixtureState)
    expect(sessions).toHaveLength(1)
    expect(sessions[0].checkpoints.map(item => item.artifact_id)).toEqual(['checkpoint_1'])

    data.mixtureJob.status = 'running'
    expect(eligibleTrainingSessions(data.modelState, data.mixtureState)).toEqual([])

    data.mixtureJob.status = 'succeeded'
    data.mixtureJob.result_path = ''
    expect(eligibleTrainingSessions(data.modelState, data.mixtureState)).toEqual([])
  })

  it('creates and persists a complete demo report', () => {
    const data = fixture()
    const storage = memoryStorage()
    const session = eligibleTrainingSessions(data.modelState, data.mixtureState)[0]
    const report = createTrainingValidationReport(session, 'checkpoint_1', storage, 'validation_1', '2026-09-03T12:00:00Z')
    expect(report.pass_k).toBe(8)
    expect(report.checkpoint_version).toBe('student-v1-step-1000')
    expect(report.sample_counts).toHaveLength(VALIDATION_COLUMNS.length)
    expect(report.pass_at_k.improvement).toHaveLength(VALIDATION_COLUMNS.length)
    expect(readTrainingValidationState(storage).reports[0]).toEqual(report)
  })

  it('rejects a checkpoint outside the selected session and recovers corrupt state', () => {
    const data = fixture()
    const session = eligibleTrainingSessions(data.modelState, data.mixtureState)[0]
    expect(() => createTrainingValidationReport(session, 'unknown', memoryStorage())).toThrow('checkpoint')
    const corrupt = memoryStorage('{bad')
    expect(readTrainingValidationState(corrupt)).toEqual({ version: 1, reports: [] })
    expect(corrupt.values.has(TRAINING_VALIDATION_STORAGE_KEY)).toBe(false)
  })
})
