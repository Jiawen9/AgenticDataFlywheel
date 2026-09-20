import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '@/api'
import { activeBatchItems, eventMatchesRoute, notifyBatchesPublished, publishedBatch, subscribeBatchLifecycle, withoutBatchQuery } from './batchLifecycle'

afterEach(() => vi.unstubAllGlobals())
describe('published batch lifecycle', () => {
  it('notifies locally, broadcasts, deduplicates incoming events and closes the channel', () => {
    const channels: Array<{ onmessage: ((event: { data: unknown }) => void) | null; postMessage: ReturnType<typeof vi.fn>; close: ReturnType<typeof vi.fn> }> = []
    vi.stubGlobal('window', {})
    vi.stubGlobal('BroadcastChannel', class { onmessage = null; postMessage = vi.fn(); close = vi.fn(); constructor() { channels.push(this) } })
    const listener = vi.fn(), stop = subscribeBatchLifecycle(listener)
    notifyBatchesPublished({ batch_ids: ['bus-a'], release_id: 'release-a' })
    expect(listener).toHaveBeenCalledTimes(1)
    expect(channels[0]!.postMessage).toHaveBeenCalledTimes(1)
    const event = channels[0]!.postMessage.mock.calls[0]![0]
    channels[0]!.onmessage!({ data: event })
    expect(listener).toHaveBeenCalledTimes(1)
    channels[0]!.onmessage!({ data: { ...event, event_id: 'other-tab', batch_ids: ['bus-b'] } })
    expect(listener).toHaveBeenCalledTimes(2)
    expect(publishedBatch('bus-b')?.release_id).toBe('release-a')
    expect(activeBatchItems([{ batch_id: 'bus-a' }, { batch_id: 'unaffected' }])).toEqual([{ batch_id: 'unaffected' }])
    stop(); expect(channels[0]!.close).toHaveBeenCalledOnce()
  })
  it('recognizes structured 409 closure and preserves its details for the page', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: { code: 'batch_published', message: '该批次已发布，处理已结束', batch_id: 'closed-api', release_id: 'r' } }), { status: 409 })))
    const listener = vi.fn(), stop = subscribeBatchLifecycle(listener)
    try {
      await expect(api.batchTree('closed-api')).rejects.toMatchObject({ status: 409, message: '该批次已发布，处理已结束', detail: { code: 'batch_published' } })
      expect(listener).toHaveBeenCalledWith(expect.objectContaining({ batch_ids: ['closed-api'], release_id: 'r' }))
    } finally { stop() }
  })
  it('does not publish an event after an ordinary failed release', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'publish failed' }), { status: 500 })))
    const listener = vi.fn(), stop = subscribeBatchLifecycle(listener)
    try { await expect(api.createDatasetRelease('test', ['sid'])).rejects.toBeInstanceOf(ApiError); expect(listener).not.toHaveBeenCalled() }
    finally { stop() }
  })
  it('removes batch routes while preserving unrelated query filters', () => {
    expect(withoutBatchQuery({ batch_id: 'a', run: 'x', tree_run_id: 'x', session_id: 's', session: 's', collection_batch_id: 'a', filter: 'keep' })).toEqual({ filter: 'keep' })
    const event = { type: 'batches-published' as const, event_id: '1', batch_ids: ['a'], release_id: 'r', source_path: '/api/correction/sessions/old' }
    expect(eventMatchesRoute(event, '', { session_id: 'old' })).toBe(true)
    expect(eventMatchesRoute(event, 'b', {})).toBe(false)
  })
  it('does not return a published batch from an older candidate response', async () => {
    let respond!: (value: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { respond = resolve })))
    const pending = api.datasetReleaseCandidates()
    notifyBatchesPublished({ batch_ids: ['late-list-a'], release_id: 'r' })
    respond(new Response(JSON.stringify({ candidates: [{ batch_id: 'late-list-a' }, { batch_id: 'late-list-b' }] })))
    expect(await pending).toEqual([{ batch_id: 'late-list-b' }])
  })
})
