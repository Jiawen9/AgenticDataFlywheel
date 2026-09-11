import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'

afterEach(() => vi.unstubAllGlobals())
describe('dataset internal upload API', () => {
  it('submits only release ID and target without fetching any workbook', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{"job":{"job_id":"internal","mode":"internal"}}', { status: 202 }))
    vi.stubGlobal('fetch', fetcher)
    expect(await api.uploadDatasetRelease('rel_test', 'internal')).toEqual({ job_id: 'internal', mode: 'internal' })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher).toHaveBeenCalledWith('/api/dataset-releases/rel_test/upload', expect.objectContaining({
      method: 'POST', body: '{"target":"internal"}',
    }))
  })
  it('keeps the old no-body mock request compatible', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{"job":{"mode":"mock"}}'))
    vi.stubGlobal('fetch', fetcher)
    await api.uploadDatasetRelease('legacy')
    expect(fetcher.mock.calls[0]?.[1].body).toBeUndefined()
  })
  it('reads capabilities and persisted progress', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response('{"internal":{"configured":false,"reason":"云道S3上传尚未配置"}}'))
      .mockResolvedValueOnce(new Response('{"job":{"mode":"internal","completed_files":1,"total_files":2}}'))
    vi.stubGlobal('fetch', fetcher)
    expect((await api.datasetUploadCapabilities()).internal).toEqual({ configured: false, reason: '云道S3上传尚未配置' })
    expect((await api.datasetUploadJob('job')).completed_files).toBe(1)
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/api/dataset-upload-capabilities', '/api/dataset-upload-jobs/job'])
  })
  it('surfaces 409 validation failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"表格 SHA256 不一致"}', { status: 409 })))
    await expect(api.uploadDatasetRelease('rel', 'internal')).rejects.toThrow('表格 SHA256 不一致')
  })
})
