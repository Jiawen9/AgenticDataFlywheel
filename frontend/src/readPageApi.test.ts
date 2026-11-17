import { beforeEach, describe, expect, it, vi } from 'vitest'
const transport = vi.hoisted(() => vi.fn())
vi.mock('@/api', () => ({ request: transport }))
import { readPageApi } from './readPageApi'

beforeEach(() => { transport.mockReset(); transport.mockResolvedValue({ session: {} }) })
describe('business summary API paths', () => {
  it('uses the existing API routes with server-side query scope', async () => {
    await readPageApi.releases({ offset: 20, limit: 20, search: '中文发布', status: 'failed' })
    const url = new URL(transport.mock.calls[0]![0], 'http://local')
    expect(url.pathname).toBe('/api/dataset-releases')
    expect(Object.fromEntries(url.searchParams)).toEqual({ view: 'summary', offset: '20', limit: '20', search: '中文发布', status: 'failed' })
  })
  it('keeps COT rows scoped to one group and jobs scoped to one session', async () => {
    await readPageApi.cot('s-one', 'group-one')
    await readPageApi.cotJobs('s-one')
    expect(transport.mock.calls[0]![0]).toBe('/api/correction/sessions/s-one/cot?group_id=group-one')
    expect(transport.mock.calls[1]![0]).toContain('session_id=s-one')
  })
  it('only explicit correction opening requests reconciliation', async () => {
    await readPageApi.session('s-one'); await readPageApi.session('s-one', true)
    expect(transport.mock.calls[0]![0]).not.toContain('reconcile')
    expect(transport.mock.calls[1]![0]).toContain('reconcile=true')
  })
})
