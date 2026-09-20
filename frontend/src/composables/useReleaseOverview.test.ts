import { afterEach, describe, expect, it, vi } from 'vitest'
import { useReleaseOverview } from './useReleaseOverview'
import type { OverviewConversion, TrainingDataOverview } from '@/trainingDataOverviewApi'

function item(release_id: string, status: OverviewConversion['status']): OverviewConversion {
  return { release_id, status, name: release_id, error: status === 'failed' ? 'bad input' : null, warnings: [], updated_at: 'now' }
}
function response(conversions: OverviewConversion[], workbook = true) {
  return { conversions, workbook_url: workbook ? '/api/training-data-overview/workbook' : null } as TrainingDataOverview
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function setup() {
  const client = {
    read: vi.fn(async () => response([item('r1', 'succeeded')])),
    retry: vi.fn(async () => ({ conversion: item('r1', 'queued') })),
  }
  return { client, state: useReleaseOverview('r1', client, 1000) }
}
afterEach(() => vi.useRealTimers())

describe('release overview maintenance', () => {
  it('reads only the chosen release status and keeps full-catalog download independent', async () => {
    vi.useFakeTimers()
    const { client, state } = setup()
    client.read.mockResolvedValue(response([item('other', 'running'), item('r1', 'failed')]))
    await state.load()
    expect(state.conversion.value?.release_id).toBe('r1')
    expect(state.error.value).toBe('')
    expect(state.workbookReady.value).toBe(true)
    expect(state.active.value).toBe(false)
    await vi.advanceTimersByTimeAsync(5000)
    expect(client.read).toHaveBeenCalledTimes(1)
    state.dispose()
  })
  it('polls this release until completion and stops on closing the dialog', async () => {
    vi.useFakeTimers()
    const { client, state } = setup()
    client.read.mockResolvedValueOnce(response([item('r1', 'queued')]))
    await state.load()
    expect(state.active.value).toBe(true)
    await vi.advanceTimersByTimeAsync(1000)
    expect(state.conversion.value?.status).toBe('succeeded')
    await vi.advanceTimersByTimeAsync(5000)
    expect(client.read).toHaveBeenCalledTimes(2)
    client.read.mockResolvedValue(response([item('r1', 'running')]))
    await state.load()
    state.dispose()
    await vi.advanceTimersByTimeAsync(5000)
    expect(client.read).toHaveBeenCalledTimes(3)
  })
  it('drops older responses and responses arriving after the details are closed', async () => {
    const { client, state } = setup()
    const old = deferred<TrainingDataOverview>()
    client.read.mockReturnValueOnce(old.promise)
    const first = state.load()
    await state.load()
    old.resolve(response([item('r1', 'failed')], false))
    await first
    expect(state.conversion.value?.status).toBe('succeeded')
    expect(state.workbookReady.value).toBe(true)
    const late = deferred<TrainingDataOverview>()
    client.read.mockReturnValueOnce(late.promise)
    const closing = state.load()
    state.dispose()
    late.resolve(response([item('r1', 'failed')]))
    await closing
    expect(state.conversion.value?.status).toBe('succeeded')
  })
  it('retries the exact release once and does not resume polling after unmount', async () => {
    const { client, state } = setup()
    const accepted = deferred<{ conversion: OverviewConversion }>()
    client.retry.mockReturnValueOnce(accepted.promise)
    const retry = state.retry()
    await state.retry()
    expect(client.retry).toHaveBeenCalledTimes(1)
    expect(client.retry).toHaveBeenCalledWith('r1')
    state.dispose()
    accepted.resolve({ conversion: item('r1', 'queued') })
    await retry
    expect(client.read).not.toHaveBeenCalled()
  })
  it('allows retrying an unregistered conversion and preserves its state on network errors', async () => {
    const { client, state } = setup()
    client.read.mockResolvedValueOnce(response([], false))
    await state.load()
    expect(state.conversion.value).toBeNull()
    expect(state.workbookReady.value).toBe(false)
    client.retry.mockRejectedValueOnce(new Error('offline'))
    await state.retry()
    expect(state.error.value).toBe('offline')
    expect(state.conversion.value).toBeNull()
    expect(state.retrying.value).toBe(false)
    await state.retry()
    expect(state.conversion.value?.status).toBe('succeeded')
    expect(state.error.value).toBe('')
    state.dispose()
  })
  it('does not submit another conversion while the selected release is running', async () => {
    const { client, state } = setup()
    client.read.mockResolvedValueOnce(response([item('r1', 'running')]))
    await state.load()
    await state.retry()
    expect(client.retry).not.toHaveBeenCalled()
    state.dispose()
  })
})
