import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, datasetReleaseTrainingUrl } from './api'

afterEach(() => vi.unstubAllGlobals())
describe('dataset internal upload API', () => {
  it('sends paired export and independent training upload fields without transferring file contents', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{}'))
    vi.stubGlobal('fetch', fetcher)
    await api.correctionDatasetExport('session', 8, { export_profile: 'corrected_selection_v1', cot_policy: 'skip', training_image_prefix: 's3://bucket/images/' })
    expect(JSON.parse(fetcher.mock.calls[0]?.[1].body)).toEqual({ expected_revision: 8, export_profile: 'corrected_selection_v1', cot_policy: 'skip', training_image_prefix: 's3://bucket/images/' })
    fetcher.mockResolvedValue(new Response('{"job":{"job_id":"jsonl"}}'))
    await api.uploadDatasetRelease('release', 'internal', { include_trajectories: false, destination_prefix: 'tables/', training_jsonl: true, training_only: true, training_destination_prefix: 'training/批次/' })
    expect(JSON.parse(fetcher.mock.calls[1]?.[1].body)).toEqual({ target: 'internal', include_trajectories: false, destination_prefix: 'tables/', training_jsonl: true, training_only: true, training_destination_prefix: 'training/批次/' })
    expect(datasetReleaseTrainingUrl('发布/1', 1, 'report')).toBe('/api/dataset-releases/%E5%8F%91%E5%B8%83%2F1/training/1?kind=report')
  })
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
  it('submits the editable destination and frozen release scope without client-side file paths', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{"job":{"job_id":"folders","folder_upload":true}}', { status: 202 }))
    vi.stubGlobal('fetch', fetcher)
    await api.uploadDatasetRelease('发布/1', 'internal', { include_trajectories: true, destination_prefix: 'data/中文目标/' })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher.mock.calls[0]?.[0]).toBe('/api/dataset-releases/%E5%8F%91%E5%B8%83%2F1/upload')
    expect(JSON.parse(fetcher.mock.calls[0]?.[1].body)).toEqual({ target: 'internal', include_trajectories: true, destination_prefix: 'data/中文目标/' })
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
  it('passes a same-target retry identity without changing the endpoint', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{"job":{"job_id":"retry"}}'))
    vi.stubGlobal('fetch', fetcher)
    await api.uploadDatasetRelease('rel', 'internal', { include_trajectories: true, destination_prefix: 'original/', retry_job_id: 'job-failed' })
    expect(JSON.parse(fetcher.mock.calls[0]?.[1].body)).toEqual({ target: 'internal', include_trajectories: true, destination_prefix: 'original/', retry_job_id: 'job-failed' })
  })
  it('surfaces 409 validation failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"表格 SHA256 不一致"}', { status: 409 })))
    await expect(api.uploadDatasetRelease('rel', 'internal')).rejects.toThrow('表格 SHA256 不一致')
  })
})
