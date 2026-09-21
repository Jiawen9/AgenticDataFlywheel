import { afterEach, describe, expect, it, vi } from 'vitest'
import { factoryBatchesApi, modelIterationApi, phoneFactoryApi } from './phoneFactoryApi'
afterEach(() => vi.unstubAllGlobals())
describe('phone factory transport contracts', () => {
  it('passes an explicit manual upload request ID without changing generated batch import identity', async () => {
    const fetcher = vi.fn(async () => new Response('{"tasks":[]}'))
    vi.stubGlobal('fetch', fetcher)
    await phoneFactoryApi.addTask('manual', 'same.xlsx', 'Ynl0ZXM=', undefined, 'upload-one')
    await phoneFactoryApi.addTask('batch', 'collection-batch-a.xlsx', 'Ynl0ZXM=', 'a')
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({ description: 'manual', filename: 'same.xlsx', content_base64: 'Ynl0ZXM=', request_id: 'upload-one' })
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual({ description: 'batch', filename: 'collection-batch-a.xlsx', content_base64: 'Ynl0ZXM=', source_batch_id: 'a' })
  })

  it('keeps evaluation requests separate and passes run configuration with caller idempotency', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ ok: true, runs: [], folders: [] })))
    vi.stubGlobal('fetch', fetcher)
    const options = { filename: 'manual.xlsx', phone_id: 'phone-1', app: 'App', request_id: 'same-request', vla: 'vla:8000', run_mode: 'modeliter' as const, config: { sampling_enabled: true, temperature: 0.4, top_p: 0.8, use_experience_lib: false } }
    await modelIterationApi.remoteStartRun(options); await modelIterationApi.runs(); await modelIterationApi.reports()
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/api/model-iter/remote/start-run', '/api/model-iter/runs', '/api/model-iter/reports'])
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual(options)
    await phoneFactoryApi.syncRun('cr/a')
    expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/phone-factory/collection-runs/cr%2Fa/sync')
  })
  it('uses stable report identities and forwards polling cancellation', async () => {
    const fetcher = vi.fn(async () => new Response('{"ok":true,"devices":[]}'))
    vi.stubGlobal('fetch', fetcher)
    const controller = new AbortController()
    await phoneFactoryApi.adbDevices(controller.signal)
    expect(fetcher.mock.calls[0]?.[1]?.signal).toBe(controller.signal)
    await modelIterationApi.reportDownload({ index: 1, dir_name: 'old-folder', run_id: 'eval-1', files: [] }, { name: 'report.xlsx', file_id: 'file-1', size: 1 })
    expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/model-iter/report-download?run_id=eval-1&file_id=file-1')
  })
  it('queries the unified batch source and reports remote deletion failure', async () => {
    const fetcher = vi.fn(async () => new Response('{"batches":[]}'))
    vi.stubGlobal('fetch', fetcher)
    await factoryBatchesApi.list()
    expect(fetcher.mock.calls[0]?.[0]).toBe('/api/phone-factory/batches')
    fetcher.mockResolvedValueOnce(new Response('{"ok":false,"error":"device stop failed"}'))
    await expect(phoneFactoryApi.remoteDeletePhone('phone-1')).rejects.toThrow('device stop failed')
    expect(fetcher.mock.calls).toHaveLength(2)
  })
})
