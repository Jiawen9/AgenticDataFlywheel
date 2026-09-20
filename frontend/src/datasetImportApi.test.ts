import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { datasetImportApi } from './datasetImportApi'

const fields = { data_source: '人工采集', data_date: '2026-09-20', app: 'App / A', level1: '生活', level2: '搜索', sheet_name: '轨迹 2' }
afterEach(() => vi.unstubAllGlobals())
describe('external dataset import API', () => {
  it('sends file bytes and all metadata as multipart without overriding its boundary', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ valid: false, sheets: ['a', 'b'] })))
    vi.stubGlobal('fetch', fetcher)
    const file = new File([new Uint8Array([80, 75, 255])], '轨迹.xlsm')
    const signal = new AbortController().signal
    const result = await datasetImportApi.preview(file, fields, signal)
    const [url, init] = fetcher.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/dataset-release-imports/preview')
    expect(init).toMatchObject({ method: 'POST', cache: 'no-store', signal })
    expect(init.headers).toBeUndefined()
    const form = init.body as FormData
    expect(new Uint8Array(await (form.get('file') as File).arrayBuffer())).toEqual(new Uint8Array([80, 75, 255]))
    for (const [key, value] of Object.entries(fields)) expect(form.get(key)).toBe(value)
    expect(result).toEqual({ valid: false, sheets: ['a', 'b'] })
  })
  it('submits the reviewed import with its stable request identity and returns existing or new releases', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ release: { release_id: 'existing', source_kind: 'external_manual' } })))
    vi.stubGlobal('fetch', fetcher)
    const payload = { import_id: 'import1', name: '数据集', request_id: 'same-request' }
    expect(await datasetImportApi.publish(payload)).toEqual({ release_id: 'existing', source_kind: 'external_manual' })
    expect(fetcher).toHaveBeenCalledWith('/api/dataset-releases/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), cache: 'no-store' })
  })
  it('retains conflict metadata and surfaces validation/oversize errors', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: 'import_conflict', message: '相同内容已发布', release_id: 'r1' } }), { status: 409 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: [{ msg: '文件格式无效' }] }), { status: 422 }))
      .mockResolvedValueOnce(new Response('too large', { status: 413, statusText: 'Too Large' }))
    vi.stubGlobal('fetch', fetcher)
    const payload = { import_id: 'i', name: 'n', request_id: 'q' }
    const error = await datasetImportApi.publish(payload).catch(error => error)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 409, message: '相同内容已发布', detail: { code: 'import_conflict', release_id: 'r1' } })
    await expect(datasetImportApi.preview(new File(['x'], 'a.xlsx'), fields)).rejects.toThrow('文件格式无效')
    await expect(datasetImportApi.preview(new File(['x'], 'a.xlsx'), fields)).rejects.toThrow('413 Too Large')
  })
})
