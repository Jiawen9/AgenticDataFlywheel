import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, imageUrl, stageArtifactDownloadUrl, treeRunScope } from './api'
import { phoneFactoryApi, newRunRequestId } from './phoneFactoryApi'
import type { StageArtifact } from './types'

afterEach(() => vi.unstubAllGlobals())
describe('batch-scoped trajectory API', () => {
  it('loads quality images from the selected business batch', () => {
    expect(imageUrl('runs/cr/original/1.png', treeRunScope({ batch_id: 'batch-b', annotation_version: 'v2' }))).toBe('/api/assets/runs/cr/original/1.png?batch_id=batch-b')
    expect(treeRunScope(undefined)).toBeUndefined()
    expect(treeRunScope({ batch_id: 'incomplete' })).toEqual({ batch_id: 'incomplete' })
  })
  it('passes scope to tasks, trajectories, detail, images, bbox and tree builds', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ tasks: [], trajectories: [], trajectory: {}, annotation_version: 'v2', actions_box: '[]' })))
    vi.stubGlobal('fetch', fetcher)
    const scope = { batch_id: 'batch / a', annotation_version: 'v:1' }
    await api.tasks(scope); await api.trajectories('task/a', scope); await api.trajectory('task/a', 'traj/1', scope)
    for (const [url] of fetcher.mock.calls) {
      expect(new URL(String(url), 'http://localhost').searchParams.get('batch_id')).toBe(scope.batch_id)
      expect(new URL(String(url), 'http://localhost').searchParams.get('annotation_version')).toBe(scope.annotation_version)
    }
    expect(imageUrl('a\\b/1.png', scope)).toBe('/api/assets/a/b/1.png?batch_id=batch+%2F+a&annotation_version=v%3A1')
    await api.updateBBox('task/a', 'traj/1', 1, 2, [1, 2, 3, 4], scope)
    expect(JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body))).toEqual({ excel_row: 2, bbox: [1, 2, 3, 4], ...scope })
    await api.createBuild(['task/a'], scope)
    expect(JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body))).toEqual({ task_ids: ['task/a'], batch_id: scope.batch_id })
  })
  it('uses persisted-job endpoints and downloads exactly the frozen stage version', async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ batches: [], jobs: [] })))
    vi.stubGlobal('fetch', fetcher)
    await api.preprocessingBatches(); await api.createPreprocessing('batch-a')
    expect(JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body))).toEqual({ batch_id: 'batch-a' })
    await api.preprocessingJob('job/a'); expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/trajectory-preprocessing/jobs/job%2Fa')
    await api.retryPreprocessing('job/a'); expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/trajectory-preprocessing/jobs/job%2Fa/retry')
    await api.builds(); expect(fetcher.mock.calls.at(-1)?.[0]).toBe('/api/tree-builds')
    expect(stageArtifactDownloadUrl({ batch_id: 'batch-a', stage: '02_annotation', version: 'v1' } as StageArtifact, 'result.xlsx')).toBe('/api/data-batches/batch-a/artifacts/02_annotation/v1/files/result.xlsx')
  })
  it('assigns a fresh idempotency ID per run and permits reusing it for a network retry', async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response('{"ok":true}'))
    vi.stubGlobal('fetch', fetcher)
    await phoneFactoryApi.remoteStartRun({ filename: 'tasks.xlsx', phone_id: 'phone', app: 'App', vla: 'vla:8000', request_id: newRunRequestId() })
    const first = JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))
    expect(first.request_id).toMatch(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/)
    await phoneFactoryApi.remoteStartRun(first)
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual(first)
    await phoneFactoryApi.remoteStartRun({ filename: 'tasks.xlsx', phone_id: 'phone', app: 'App', vla: 'vla:8000', request_id: newRunRequestId() })
    expect(JSON.parse(String(fetcher.mock.calls[2]?.[1]?.body)).request_id).not.toBe(first.request_id)
  })
})
