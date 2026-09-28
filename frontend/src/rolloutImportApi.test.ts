import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { rolloutImportApi } from './rolloutImportApi'

afterEach(() => vi.unstubAllGlobals())
describe('raw rollout import API', () => {
  it('loads server-local allowed directories without caching', async () => {
    const options = { default_source_path: 'D:\\data\\rollout', allowed_roots: ['D:\\data'] }
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(options)))
    vi.stubGlobal('fetch', fetcher)
    const signal = new AbortController().signal
    expect(await rolloutImportApi.options(signal)).toEqual(options)
    expect(fetcher).toHaveBeenCalledWith('/api/rollout-imports/options', { signal, cache: 'no-store' })
  })
  it('sends reviewed paths, stable case identities and metadata as JSON', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ import_id: 'i', valid: false })))
    vi.stubGlobal('fetch', fetcher)
    const body = { source_path: 'D:\\raw data\\rollout', batch_id: 'rollout-new', app: '地图', scene: '出行', capability: '搜索', task_overrides: [{ collection_case_id: '001', task: '搜索附近的餐厅' }] }
    const signal = new AbortController().signal
    expect(await rolloutImportApi.preview(body, signal)).toEqual({ import_id: 'i', valid: false })
    expect(fetcher).toHaveBeenCalledWith('/api/rollout-imports/preview', { method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' }, signal, cache: 'no-store' })
  })
  it('commits only the frozen preview and idempotency identity', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ batch_id: 'rollout-new', reused: true })))
    vi.stubGlobal('fetch', fetcher)
    const body = { import_id: 'i', request_id: 'same-request' }
    expect(await rolloutImportApi.commit(body)).toEqual({ batch_id: 'rollout-new', reused: true })
    expect(fetcher).toHaveBeenCalledWith('/api/rollout-imports', { method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' }, signal: undefined, cache: 'no-store' })
  })
  it('preserves conflict details and exposes validation errors', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: 'source_changed', message: '源文件已改变' } }), { status: 409 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: [{ msg: '批次编号无效' }] }), { status: 422 }))
      .mockResolvedValueOnce(new Response('unavailable', { status: 503, statusText: 'Unavailable' })))
    const result = await rolloutImportApi.commit({ import_id: 'i', request_id: 'r' }).catch(error => error)
    expect(result).toBeInstanceOf(ApiError)
    expect(result).toMatchObject({ status: 409, message: '源文件已改变', detail: { code: 'source_changed' } })
    await expect(rolloutImportApi.preview({ source_path: 'x', batch_id: '/' })).rejects.toThrow('批次编号无效')
    await expect(rolloutImportApi.options()).rejects.toThrow('503 Unavailable')
  })
})
