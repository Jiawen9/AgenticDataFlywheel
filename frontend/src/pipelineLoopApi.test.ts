import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'
import { completeLoopRequest, loopRequestId, pipelineLoopApi } from './pipelineLoopApi'
import { pipelineStepLocation, type Pipeline } from './types/pipeline'
afterEach(() => vi.unstubAllGlobals())
describe('generation and feedback wire contract', () => {
  function transport(value: unknown = {}) { const fn = vi.fn(async (_url: string, _init: RequestInit) => ({ ok: true, json: async () => value })); vi.stubGlobal('fetch', fn); return fn }
  it('posts a generation start without inventing a result batch', async () => {
    const fetch = transport({ pipeline: { pipeline_id: 'p1', batch_id: null } })
    const p = await api.createPipeline({ name: '从生成开始', request_id: 'g1', start_mode: 'generate', generation_kind: 'initial', mode: 'manual' })
    const body = JSON.parse(String(fetch.mock.calls[0]![1]!.body))
    expect(body).not.toHaveProperty('batch_id'); expect(body).not.toHaveProperty('collection_config')
    expect(p.batch_id).toBeNull()
  })
  it('sends generation intent and pipeline identity through the existing API', async () => {
    const fetch = transport({ job_id: 'g1' })
    await api.createTaskGeneration([{ node_id: 'leaf', apps: ['App'] }], 3, 'kb-v1', { pipeline_id: 'p1', request_id: 'once' })
    expect(JSON.parse(String(fetch.mock.calls[0]![1]!.body))).toMatchObject({ pipeline_id: 'p1', request_id: 'once', version: 'kb-v1', generate_n: 3 })
  })
  it('keeps multipart augmentation compatibility and adds optional context', async () => {
    const fetch = transport({ job_id: 'g1' })
    await api.createAugmentation(new File(['content'], '中文.xlsx'), 2, false, { pipeline_id: 'p1', request_id: 'a1' })
    const form = fetch.mock.calls[0]![1]!.body as FormData
    expect(form.get('pipeline_id')).toBe('p1'); expect(form.get('request_id')).toBe('a1'); expect(form.get('auto_start')).toBe('false')
  })
  it('confirms once with collection settings and revision', async () => {
    const fetch = transport({ pipeline: { pipeline_id: 'p1', batch_id: 'b1' } })
    await pipelineLoopApi.confirm('p1', { request_id: 'confirm1', expected_revision: 9, collection_config: { phone_id: '', app: '', vla: 'http://model.invalid/v1', repeat_times: 2, config: {} as never } })
    expect(fetch.mock.calls[0]![0]).toBe('/api/pipelines/p1/confirm-generation')
    expect(JSON.parse(String(fetch.mock.calls[0]![1]!.body))).toMatchObject({ expected_revision: 9, request_id: 'confirm1', collection_config: { repeat_times: 2 } })
  })
  it('keeps extraction asynchronous and lists only the requested page', async () => {
    const fetch = transport({ items: [] })
    await pipelineLoopApi.refresh('eval1'); await pipelineLoopApi.badcases('eval1', 20, 10, 'technical')
    expect(JSON.parse(String(fetch.mock.calls[0]![1]!.body))).toEqual({ run_id: 'eval1' })
    expect(fetch.mock.calls[1]![0]).toContain('offset=20&limit=10&status=technical')
  })
  it('does not leak null batch identifiers into navigation', () => {
    const p = { pipeline_id: 'p1', batch_id: null, generation_kind: 'augmentation', generation_job_id: 'g1', feedback_id: 'fb1' } as Pipeline
    expect(pipelineStepLocation(p, 'generation')).toEqual({ path: '/task-generation/augmentation', query: { pipeline_id: 'p1', step_id: 'generation', job_id: 'g1', feedback_id: 'fb1' } })
  })
  it('clears only acknowledged creation intent from memory and browser session', () => {
    const values = new Map<string, string>()
    vi.stubGlobal('sessionStorage', { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => values.set(key, value), removeItem: (key: string) => values.delete(key) })
    const first = loopRequestId('create-test', { name: 'same' })
    const feedback = loopRequestId('feedback-test', ['a'])
    completeLoopRequest('create-test', 'old-unrelated-response')
    expect(loopRequestId('create-test', { name: 'same' })).toBe(first)
    completeLoopRequest('create-test', first)
    expect(values.has('adf.loop.create-test')).toBe(false)
    expect(loopRequestId('create-test', { name: 'same' })).not.toBe(first)
    expect(loopRequestId('feedback-test', ['a'])).toBe(feedback)
  })
  it('reuses the pending request after retries and changes it for a different intent', () => {
    const first = loopRequestId('test', ['pipeline', 0, { count: 2 }])
    expect(loopRequestId('test', ['pipeline', 0, { count: 2 }])).toBe(first)
    expect(loopRequestId('test', ['pipeline', 1, { count: 2 }])).not.toBe(first)
  })
})
