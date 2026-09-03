import { describe, expect, it } from 'vitest'
import type { StorageLike } from './modelPublishingStore'
import { createTrainingMixtureJob, readTrainingMixtureState, TRAINING_MIXTURE_STORAGE_KEY, updateTrainingMixtureJob } from './trainingMixtureStore'

function memoryStorage(initial?: string): StorageLike & { values: Map<string, string> } {
  const values = new Map<string, string>()
  if (initial !== undefined) values.set(TRAINING_MIXTURE_STORAGE_KEY, initial)
  return { values, getItem: key => values.get(key) ?? null, setItem: (key, value) => { values.set(key, value) }, removeItem: key => { values.delete(key) } }
}

const input = {
  datasetReleaseId: 'rel_1', datasetName: '爱奇艺 数据集 v1',
  studentArtifactId: 'model_1', studentModelName: 'student/model',
  teacherModelName: 'teacher:model', teacherModelPath: String.raw`D:\models\teacher`,
  maxWorkers: 8, passK: 4, outputDirectory: String.raw`D:\results\mixture`,
}

describe('training mixture store', () => {
  it('persists configuration and creates the required result filename', () => {
    const storage = memoryStorage()
    const job = createTrainingMixtureJob(input, storage, 'mix_1', '2026-09-02T12:00:00Z')
    expect(job.result_filename).toBe('teacher_model-student_model-爱奇艺_数据集_v1.xlsx')
    expect(job.result_path).toBe(`D:/results/mixture/${job.result_filename}`)
    expect(job.train_dataset_path).toBe('D:/results/mixture/teacher_model-student_model-爱奇艺_数据集_v1-train.xlsx')
    expect(job.eval_dataset_path).toBe('D:/results/mixture/teacher_model-student_model-爱奇艺_数据集_v1-eval.xlsx')
    expect(readTrainingMixtureState(storage).jobs[0]).toEqual(job)
  })

  it('backfills Train and Eval paths for existing version 1 jobs', () => {
    const storage = memoryStorage()
    const job = createTrainingMixtureJob(input, storage, 'mix_legacy')
    const legacy = { ...job } as Record<string, unknown>
    delete legacy.train_dataset_filename
    delete legacy.train_dataset_path
    delete legacy.eval_dataset_filename
    delete legacy.eval_dataset_path
    storage.setItem(TRAINING_MIXTURE_STORAGE_KEY, JSON.stringify({ version: 1, jobs: [legacy] }))

    const restored = readTrainingMixtureState(storage).jobs[0]
    expect(restored.train_dataset_filename).toMatch(/-train\.xlsx$/)
    expect(restored.eval_dataset_filename).toMatch(/-eval\.xlsx$/)
  })

  it('validates max_workers and Pass@K', () => {
    const storage = memoryStorage()
    expect(() => createTrainingMixtureJob({ ...input, maxWorkers: 0 }, storage)).toThrow('max_workers')
    expect(() => createTrainingMixtureJob({ ...input, passK: 21 }, storage)).toThrow('Pass@K')
  })

  it('updates progress without creating duplicate history rows', () => {
    const storage = memoryStorage()
    const job = createTrainingMixtureJob(input, storage, 'mix_1')
    job.progress = 100
    job.status = 'succeeded'
    job.stage = 'completed'
    updateTrainingMixtureJob(job, storage)
    const state = readTrainingMixtureState(storage)
    expect(state.jobs).toHaveLength(1)
    expect(state.jobs[0].status).toBe('succeeded')
  })

  it('clears corrupt persisted state', () => {
    const storage = memoryStorage('{bad')
    expect(readTrainingMixtureState(storage).jobs).toEqual([])
    expect(storage.values.has(TRAINING_MIXTURE_STORAGE_KEY)).toBe(false)
  })
})
