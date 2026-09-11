import { afterEach, describe, expect, it, vi } from 'vitest'
import { collectionBatchesApi, collectionBatchDownloadUrl } from './collectionBatchesApi'

afterEach(() => vi.unstubAllGlobals())
describe('collection batches shared API', () => {
  it('lists all batches or queries by encoded job ID without caching', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ batches: [{ batch_id: 'batch' }] })))
    vi.stubGlobal('fetch', fetcher)
    expect(await collectionBatchesApi.list()).toEqual([{ batch_id: 'batch' }])
    await collectionBatchesApi.list('job/a')
    expect(fetcher).toHaveBeenNthCalledWith(1, '/api/task-generation/collection-batches', { cache: 'no-store' })
    expect(fetcher).toHaveBeenLastCalledWith('/api/task-generation/collection-batches?job_id=job%2Fa', { cache: 'no-store' })
  })
  it('POSTs no body and accepts both the first 201 and repeated 200 result', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response('{"batch_id":"same"}', { status: 201 })).mockResolvedValueOnce(new Response('{"batch_id":"same"}', { status: 200 }))
    vi.stubGlobal('fetch', fetcher)
    expect(await collectionBatchesApi.submit('job')).toEqual({ batch_id: 'same' })
    expect(await collectionBatchesApi.submit('job')).toEqual({ batch_id: 'same' })
    expect(fetcher).toHaveBeenLastCalledWith('/api/task-generation/jobs/job/collection-batch', { method: 'POST', cache: 'no-store' })
  })
  it('fetches detail and returns the workbook as a Blob for phone ingestion', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response('{"batch_id":"batch"}')).mockResolvedValueOnce(new Response('xlsx', { headers: { 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' } }))
    vi.stubGlobal('fetch', fetcher)
    expect(await collectionBatchesApi.detail('batch')).toEqual({ batch_id: 'batch' })
    const workbook = await collectionBatchesApi.workbook('batch')
    expect(workbook).toBeInstanceOf(Blob)
    expect(await workbook.text()).toBe('xlsx')
    expect(collectionBatchDownloadUrl('a/b')).toBe('/api/task-generation/collection-batches/a%2Fb/workbook')
  })
  it('surfaces server validation errors for JSON and workbook requests', async () => {
    const fetcher = vi.fn(async () => new Response('{"detail":"弱依赖缺失前置任务"}', { status: 409 }))
    vi.stubGlobal('fetch', fetcher)
    await expect(collectionBatchesApi.submit('job')).rejects.toThrow('弱依赖缺失前置任务')
    await expect(collectionBatchesApi.workbook('batch')).rejects.toThrow('弱依赖缺失前置任务')
  })
})
