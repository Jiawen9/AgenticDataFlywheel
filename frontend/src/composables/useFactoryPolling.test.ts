import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSerialPoller } from './useFactoryPolling'
function deferred() { let resolve!: () => void; const promise = new Promise<void>(yes => { resolve = yes }); return { promise, resolve } }
afterEach(() => vi.useRealTimers())
describe('factory serial polling', () => {
  it('waits for each cycle and invalidates late responses after stop/start', async () => {
    vi.useFakeTimers()
    const first = deferred(), applied: number[] = [], signals: AbortSignal[] = []
    let calls = 0
    const poller = createSerialPoller(async (signal, current) => {
      const id = ++calls; signals.push(signal)
      if (id === 1) await first.promise
      if (current()) applied.push(id)
    }, 500)
    poller.start()
    await vi.advanceTimersByTimeAsync(1500)
    expect(calls).toBe(1)
    poller.stop(); poller.start()
    expect(signals[0]?.aborted).toBe(true)
    expect(calls).toBe(1)
    first.resolve()
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toBe(2); expect(applied).toEqual([2])
    poller.stop(); await vi.advanceTimersByTimeAsync(2000)
    expect(calls).toBe(2)
    poller.dispose(); poller.start(); await vi.advanceTimersByTimeAsync(2000)
    expect(calls).toBe(2)
  })
  it('continues after a failed cycle without overlapping it', async () => {
    vi.useFakeTimers()
    const work = vi.fn(async () => { if (work.mock.calls.length === 1) throw new Error('offline') })
    const poller = createSerialPoller(work, 500)
    poller.start(); await vi.advanceTimersByTimeAsync(501)
    expect(work).toHaveBeenCalledTimes(2)
    poller.stop()
  })
})
