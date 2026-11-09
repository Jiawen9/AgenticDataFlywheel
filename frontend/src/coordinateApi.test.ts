import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'

afterEach(() => vi.unstubAllGlobals())
describe('coordinate API requests', () => {
  it('sends the batch, immutable version, trajectory and image identity without a local path parameter', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ coordinate_info: { space: 'pixel', width: 1080, height: 2400 }, annotation_version: 'v2' }), { status: 200 }))
    vi.stubGlobal('fetch', fetch)
    await api.coordinateInfo({ kind: 'batch', batch_id: 'b1', annotation_version: 'v2', trajectory_id: 't1', image: 't1/step1.jpg' })
    expect(fetch.mock.calls[0][0]).toBe('/api/coordinate-info')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ batch_id: 'b1', annotation_version: 'v2', trajectory_id: 't1', image: 't1/step1.jpg' })
  })
  it('binds correction row lookups to the session revision and surfaces conflicts', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '会话已更新' }), { status: 409 }))
    vi.stubGlobal('fetch', fetch)
    await expect(api.correctionCoordinateInfo('session/1', 32, 6)).rejects.toMatchObject({ status: 409, message: '会话已更新' })
    expect(fetch.mock.calls[0][0]).toBe('/api/correction/sessions/session%2F1/rows/32/coordinate-info?expected_revision=6')
  })
})
