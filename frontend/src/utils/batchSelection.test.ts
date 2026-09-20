import { describe, expect, it, vi } from 'vitest'
import { resolveBatchSelection, resolveSessionSelection } from './batchSelection'

describe('exact route identities', () => {
  const sessions = [{ session_id: 'session-one', batch_id: 'one' }, { session_id: 'session-two', batch_id: 'two' }]
  it('chooses a default only when the route has no requested identity', async () => {
    const getRun = vi.fn()
    expect(await resolveBatchSelection(['one', 'two'], { defaultBatchId: 'two' }, getRun)).toBe('two')
    expect(getRun).not.toHaveBeenCalled()
    expect(resolveSessionSelection(sessions, {})).toBe('session-one')
  })
  it('maps a valid old COT session link to that exact session', () => {
    expect(resolveSessionSelection(sessions, { sessionId: 'session-two' })).toBe('session-two')
    expect(resolveSessionSelection(sessions, { batchId: 'two' })).toBe('session-two')
  })
  it.each([{ sessionId: 'deleted-session' }, { sessionId: '' }, { batchId: 'missing' }, { batchId: '' }])('rejects a missing COT identity without defaulting (%j)', options => {
    expect(() => resolveSessionSelection(sessions, options)).toThrow('不存在或已失效')
  })
  it('rejects an alias whose batch is unavailable in this workspace', async () => {
    await expect(resolveBatchSelection(['one'], { legacyRunId: 'old' }, async () => ({ batch_id: 'missing' }))).rejects.toThrow('指定批次不存在')
  })
  it('honors an explicit business batch over a legacy route token', async () => {
    const getRun = vi.fn()
    expect(await resolveBatchSelection(['one', 'two'], { batchId: 'two', legacyRunId: 'expired' }, getRun)).toBe('two')
    expect(getRun).not.toHaveBeenCalled()
  })
})
