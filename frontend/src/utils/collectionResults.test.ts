import { describe, expect, it } from 'vitest'
import type { FactoryRun } from '@/phoneFactoryApi'
import { collectionResultLocation, collectionRunBelongsToSource, collectionTaskSource } from './collectionResults'

describe('collection result identity and navigation', () => {
  it('groups sibling runs by their task source but navigates to each independent result', () => {
    const a: FactoryRun = { batch_id: 'result-a', task_source_batch_id: 'same-tasks', collection_run_id: 'cr-a', agent_type: 'newagent', status: 'completed' }
    const b: FactoryRun = { ...a, batch_id: 'result-b', collection_run_id: 'cr-b', agent_type: 'pipelineagent' }
    expect(collectionRunBelongsToSource(a, 'same-tasks')).toBe(true)
    expect(collectionRunBelongsToSource(b, 'same-tasks')).toBe(true)
    expect(collectionRunBelongsToSource(b, 'other-tasks')).toBe(false)
    expect(collectionResultLocation(a).query).toEqual({ batch_id: 'result-a' })
    expect(collectionResultLocation(b).query).toEqual({ batch_id: 'result-b' })
  })
  it('keeps historical batches addressable without inventing a new source identity', () => {
    const legacy: FactoryRun = { batch_id: 'old-mixed', collection_run_id: 'cr-old', status: 'completed' }
    expect(collectionTaskSource(legacy)).toBe('old-mixed')
    expect(collectionRunBelongsToSource(legacy, 'old-mixed')).toBe(true)
    expect(collectionResultLocation(legacy).query.batch_id).toBe('old-mixed')
  })
})
