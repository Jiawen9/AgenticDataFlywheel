import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'
import { pipelineStepLocation, type Pipeline } from './types/pipeline'
afterEach(() => vi.unstubAllGlobals())
describe('Pipeline API and navigation', () => {
  it('uses revisioned controls, idempotent creation, and abortable no-cache reads', async () => {
    const fetcher = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ pipeline: { pipeline_id: 'p' }, pipelines: [] })))
    vi.stubGlobal('fetch', fetcher)
    const signal = new AbortController().signal
    await api.listPipelines({ batch_id: 'a/b', active_only: true }, signal)
    expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/pipelines?batch_id=a%2Fb&active_only=true'); expect(fetcher.mock.calls.at(-1)?.[1]?.signal).toBe(signal)
    await api.pipeline('p/a', signal); expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/pipelines/p%2Fa')
    const body = { name: 'run', batch_id: 'batch', request_id: 'stable', mode: 'automatic' as const, start_mode: 'existing' as const, threshold: 3.5 }
    await api.createPipeline(body); expect(JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body))).toEqual(body)
    await api.controlPipeline('p/a', 'confirm-correction', 7, 12)
    expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/pipelines/p%2Fa/confirm-correction'); expect(JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body))).toEqual({ expected_revision: 7, session_revision: 12 })
  })
  it('preserves all authoritative identities when opening an existing or future module', () => {
    const pipeline = { pipeline_id: 'p', batch_id: 'batch', release_id: 'release' } as Pipeline
    expect(pipelineStepLocation(pipeline, 'quality')).toEqual({ path: '/quality', query: { pipeline_id: 'p', batch_id: 'batch', step_id: 'quality' } })
    expect(pipelineStepLocation(pipeline, 'publication')).toEqual({ path: '/data-publishing/archive', query: { pipeline_id: 'p', batch_id: 'batch', step_id: 'publication', release_id: 'release' } })
  })
})
