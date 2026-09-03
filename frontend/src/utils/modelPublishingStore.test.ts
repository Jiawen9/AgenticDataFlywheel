import { describe, expect, it } from 'vitest'
import {
  createTrainingMonitor,
  MODEL_PUBLISHING_STORAGE_KEY,
  readModelPublishingState,
  type StorageLike,
} from './modelPublishingStore'

function memoryStorage(initial?: string): StorageLike & { values: Map<string, string> } {
  const values = new Map<string, string>()
  if (initial !== undefined) values.set(MODEL_PUBLISHING_STORAGE_KEY, initial)
  return {
    values,
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, value) },
    removeItem: key => { values.delete(key) },
  }
}

describe('model publishing store', () => {
  it('creates independent persistent monitoring tasks', () => {
    const storage = memoryStorage()
    createTrainingMonitor(
      { trainingName: '训练 A', targetModel: 'model-a', resultDirectory: String.raw`D:\results\a` },
      storage,
      'monitor_a',
      '2026-09-02T10:00:00Z',
    )
    createTrainingMonitor(
      { trainingName: '训练 B', targetModel: 'model-b', resultDirectory: '/results/b' },
      storage,
      'monitor_b',
      '2026-09-02T11:00:00Z',
    )

    const state = readModelPublishingState(storage)
    expect(state.monitors.map(item => item.monitor_id)).toEqual(['monitor_b', 'monitor_a'])
    expect(state.monitors[1].result_directory).toBe('D:/results/a')
    expect(state.monitors.every(item => item.status === 'monitoring')).toBe(true)
    expect(state.artifacts).toEqual([])
  })

  it('rejects incomplete training monitor input', () => {
    const storage = memoryStorage()
    expect(() => createTrainingMonitor({ trainingName: '', targetModel: 'model', resultDirectory: '/results' }, storage)).toThrow('均不能为空')
    expect(readModelPublishingState(storage).monitors).toEqual([])
  })

  it('persists complete training configuration and validates resources', () => {
    const storage = memoryStorage()
    const monitor = createTrainingMonitor({
      trainingName: 'GUI 增训',
      targetModel: 'student-v1',
      resultDirectory: String.raw`D:\models\output`,
      modelArtifactId: 'artifact_1',
      optimizationAlgorithm: 'GKD',
      trainingJob: 'gkd_vla-qwen3vl-8b_teacher-qwen3vl-32b',
      mixtureJobId: 'mix_1',
      trainDatasetPath: String.raw`D:\datasets\train.xlsx`,
      evalDatasetPath: String.raw`D:\datasets\eval.xlsx`,
      cardsPerNode: 8,
      instanceCount: 2,
    }, storage, 'monitor_full')

    expect(monitor.train_dataset_path).toBe('D:/datasets/train.xlsx')
    expect(monitor.eval_dataset_path).toBe('D:/datasets/eval.xlsx')
    expect(monitor.optimization_algorithm).toBe('GKD')
    expect(monitor.cards_per_node).toBe(8)
    expect(monitor.instance_count).toBe(2)
    expect(() => createTrainingMonitor({ trainingName: 'x', targetModel: 'm', resultDirectory: '/r', cardsPerNode: 17 }, storage)).toThrow('单节点卡数')
    expect(() => createTrainingMonitor({ trainingName: 'x', targetModel: 'm', resultDirectory: '/r', instanceCount: 0 }, storage)).toThrow('实例数')
  })

  it('recovers safely from corrupt or unsupported local data', () => {
    const corrupt = memoryStorage('{broken')
    expect(readModelPublishingState(corrupt)).toEqual({ version: 1, monitors: [], artifacts: [] })
    expect(corrupt.values.has(MODEL_PUBLISHING_STORAGE_KEY)).toBe(false)

    const old = memoryStorage(JSON.stringify({ version: 0, monitors: [], artifacts: [] }))
    expect(readModelPublishingState(old).monitors).toEqual([])
    expect(old.values.has(MODEL_PUBLISHING_STORAGE_KEY)).toBe(false)
  })
})
