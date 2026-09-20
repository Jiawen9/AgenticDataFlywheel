import { afterEach, describe, expect, it, vi } from 'vitest'
import { emptyOverviewFilters } from './composables/useTrainingDataOverview'
import { overviewWorkbookUrl, trainingDataOverviewApi } from './trainingDataOverviewApi'

afterEach(() => vi.unstubAllGlobals())

describe('training overview API', () => {
  it('preserves DEV all sentinels and encodes filter values', async () => {
    const request = vi.fn(async () => new Response(JSON.stringify({ schema_version: 1 })))
    vi.stubGlobal('fetch', request)
    const signal = new AbortController().signal
    await trainingDataOverviewApi.read({ ...emptyOverviewFilters(), app: 'all', level1: '生活 / 服务' }, signal)
    const [url, init] = request.mock.calls[0] as unknown as [string, RequestInit]
    const parsed = new URL(url, 'http://local')
    expect(parsed.searchParams.get('app')).toBe('all')
    expect(parsed.searchParams.get('level1')).toBe('生活 / 服务')
    expect(parsed.searchParams.get('source')).toBe('all')
    expect(init).toMatchObject({ cache: 'no-store', signal })
  })
  it('downloads the complete workbook without using view filters and preserves bytes', async () => {
    const bytes = new Uint8Array([80, 75, 3, 4, 255])
    const request = vi.fn(async () => new Response(bytes))
    vi.stubGlobal('fetch', request)
    const blob = await trainingDataOverviewApi.workbook()
    expect(new Uint8Array(await blob.arrayBuffer())).toEqual(bytes)
    expect(request).toHaveBeenCalledWith(overviewWorkbookUrl, { cache: 'no-store' })
  })
  it('encodes release identity and sends retry without mutating data in GET', async () => {
    const request = vi.fn(async () => new Response(JSON.stringify({ conversion: { status: 'queued' } })))
    vi.stubGlobal('fetch', request)
    await trainingDataOverviewApi.retry('release / 1')
    expect(request).toHaveBeenCalledWith('/api/training-data-overview/releases/release%20%2F%201/retry', { cache: 'no-store', method: 'POST' })
  })
  it('surfaces structured and non-JSON failures', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: [{ msg: '非法日期' }] }), { status: 422 }))
      .mockResolvedValueOnce(new Response('upstream', { status: 503, statusText: 'Unavailable' })))
    await expect(trainingDataOverviewApi.read(emptyOverviewFilters())).rejects.toThrow('非法日期')
    await expect(trainingDataOverviewApi.workbook()).rejects.toThrow('503 Unavailable')
  })
})
