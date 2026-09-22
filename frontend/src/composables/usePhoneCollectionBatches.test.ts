import { describe, expect, it, vi } from 'vitest'
import type { CollectionBatchDetail } from '@/collectionBatchesApi'
import type { FactoryState } from '@/phoneFactoryApi'
import { usePhoneCollectionBatches } from './usePhoneCollectionBatches'

const detail = (id = 'batch-a') => ({
  schema_version: 1, batch_id: id, source_job_id: `job-${id}`, kind: 'task_generation', job_status: 'succeeded',
  knowledge_base_version: 'version', created_at: 'now', task_count: 1, apps: ['AppA'], filename: `collection-batch-${id}.xlsx`, download_url: `/download/${id}`,
  snapshot: { tasks: [] },
}) as CollectionBatchDetail
const state = (id = 'batch-a', status = '未运行'): FactoryState => ({ phones: [], apps: [], phoneApps: [], vla: [], tasks: [{ description: '批次', filename: detail(id).filename, status, source_batch_id: id }] })
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes }); return { promise, resolve } }
function setup() {
  const batchApi = { list: vi.fn(async () => [detail()]), detail: vi.fn(async (id: string) => detail(id)), workbook: vi.fn(async () => new Blob(['workbook bytes'])) }
  const factoryApi = { addTask: vi.fn(async (_description: string, _filename: string, _content: string, id?: string) => state(id)), remoteStartRun: vi.fn(async () => ({ ok: true, message: '已接受' })), state: vi.fn(async () => state('batch-a', '运行中')) }
  const setTasks = vi.fn()
  const downloadFile = vi.fn()
  const collection = usePhoneCollectionBatches(batchApi, factoryApi, { setTasks, downloadFile, runOptions: () => ({ vla: 'http://vla.test/v1', run_mode: 'generate' }) })
  return { collection, batchApi, factoryApi, setTasks, downloadFile }
}

describe('phone collection batch selection and dispatch', () => {
  it('retiring one batch blocks its retry while keeping another batch request identity', async () => {
    const { collection, factoryApi } = setup()
    factoryApi.remoteStartRun.mockRejectedValueOnce(new Error('lost a')).mockRejectedValueOnce(new Error('lost b'))
    await expect(collection.runBatch('batch-a')).rejects.toThrow('lost a')
    await expect(collection.runBatch('batch-b')).rejects.toThrow('lost b')
    collection.retireBatches(['batch-a'])
    await expect(collection.runBatch('batch-a')).rejects.toThrow('已发布')
    await collection.runBatch('batch-b')
    expect(factoryApi.remoteStartRun.mock.calls[1]).toEqual(factoryApi.remoteStartRun.mock.calls[2])
  })

  it('reuses request identity after a lost response and passes per-run configuration', async () => {
    const { collection, factoryApi } = setup()
    const config = { sampling_enabled: true, temperature: 0.5, top_p: 0.9, use_experience_lib: true }
    factoryApi.remoteStartRun.mockRejectedValueOnce(new Error('network lost'))
    await expect(collection.runBatch('batch-a', 'phone-1', 'AppA', { vla: 'vla:8000', config })).rejects.toThrow('network lost')
    await collection.runBatch('batch-a', 'phone-1', 'AppA', { vla: 'vla:8000', config })
    expect(factoryApi.remoteStartRun.mock.calls[0]).toEqual(factoryApi.remoteStartRun.mock.calls[1])
    expect(factoryApi.remoteStartRun).toHaveBeenLastCalledWith(expect.objectContaining({ config, vla: 'vla:8000' }))
  })
  it('keeps an accepted dispatch successful when the following state refresh fails', async () => {
    const { collection, factoryApi } = setup()
    factoryApi.state.mockRejectedValueOnce(new Error('state unavailable'))
    await expect(collection.runBatch('batch-a')).resolves.toEqual({ ok: true, message: '已接受' })
    expect(factoryApi.remoteStartRun).toHaveBeenCalledTimes(1)
  })

  it('retirement invalidates an in-flight detail without defaulting another batch', async () => {
    const { collection, batchApi } = setup(), pending = deferred<CollectionBatchDetail>()
    batchApi.detail.mockReturnValueOnce(pending.promise)
    const request = collection.selectBatch('batch-a')
    collection.retireBatches(['batch-a']); pending.resolve(detail()); await request
    expect(collection.selectedBatchId.value).toBe(''); expect(collection.selectedBatch.value).toBeNull()
  })
  it('loads the query-selected batch without downloading, registering, or starting it', async () => {
    const { collection, batchApi, factoryApi } = setup()
    await collection.loadBatches('batch-a')
    expect(collection.selectedBatch.value?.batch_id).toBe('batch-a')
    expect(batchApi.workbook).not.toHaveBeenCalled()
    expect(factoryApi.addTask).not.toHaveBeenCalled()
    expect(factoryApi.remoteStartRun).not.toHaveBeenCalled()
  })
  it('rejects an old selection response after a newer selection is chosen', async () => {
    const { collection, batchApi } = setup(), first = deferred<CollectionBatchDetail>()
    batchApi.detail.mockImplementation(id => id === 'batch-a' ? first.promise : Promise.resolve(detail(id)))
    const oldSelection = collection.selectBatch('batch-a')
    await collection.selectBatch('batch-b')
    first.resolve(detail())
    await oldSelection
    expect(collection.selectedBatch.value?.batch_id).toBe('batch-b')
  })
  it('downloads and imports before custom dispatch with unchanged phone/app arguments', async () => {
    const { collection, batchApi, factoryApi, setTasks } = setup()
    await collection.selectBatch('batch-a')
    await collection.runBatch('batch-a', 'phone-1', 'AppA')
    expect(factoryApi.addTask).toHaveBeenCalledWith('采集批次 batch-a · 1 条任务', 'collection-batch-batch-a.xlsx', btoa('workbook bytes'), 'batch-a')
    expect(factoryApi.remoteStartRun).toHaveBeenCalledWith(expect.objectContaining({ filename: 'collection-batch-batch-a.xlsx', phone_id: 'phone-1', app: 'AppA', vla: 'http://vla.test/v1', request_id: expect.any(String), run_mode: 'generate' }))
    expect(batchApi.workbook.mock.invocationCallOrder[0]!).toBeLessThan(factoryApi.addTask.mock.invocationCallOrder[0]!)
    expect(factoryApi.addTask.mock.invocationCallOrder[0]!).toBeLessThan(factoryApi.remoteStartRun.mock.invocationCallOrder[0]!)
    expect(factoryApi.state).toHaveBeenCalledTimes(1)
    expect(setTasks).toHaveBeenLastCalledWith(state('batch-a', '运行中').tasks)
  })
  it('never dispatches after a failed import and allows a deliberate retry', async () => {
    const { collection, factoryApi } = setup()
    factoryApi.addTask.mockRejectedValueOnce(new Error('导入失败'))
    await expect(collection.runBatch('batch-a')).rejects.toThrow('导入失败')
    expect(factoryApi.remoteStartRun).not.toHaveBeenCalled()
    expect(collection.busy.value).toBe(false)
    await collection.runBatch('batch-a')
    expect(factoryApi.remoteStartRun).toHaveBeenCalledTimes(1)
  })
  it('blocks repeat clicks and selection changes while importing', async () => {
    const { collection, factoryApi } = setup(), pending = deferred<FactoryState>()
    await collection.selectBatch('batch-a')
    factoryApi.addTask.mockReturnValueOnce(pending.promise)
    const run = collection.runBatch('batch-a')
    await expect(collection.runBatch('batch-a')).rejects.toThrow('正在处理')
    expect(await collection.selectBatch('batch-b')).toBe(false)
    pending.resolve(state())
    await run
    expect(factoryApi.remoteStartRun).toHaveBeenCalledTimes(1)
    expect(collection.selectedBatchId.value).toBe('batch-a')
  })
  it('does not continue dispatch after unmount', async () => {
    const first = setup(), pending = deferred<Blob>()
    first.batchApi.workbook.mockReturnValueOnce(pending.promise)
    await first.collection.selectBatch('batch-a')
    const run = first.collection.runBatch('batch-a')
    first.collection.dispose()
    pending.resolve(new Blob(['bytes']))
    await expect(run).rejects.toThrow('未继续下发')
    expect(first.factoryApi.remoteStartRun).not.toHaveBeenCalled()
  })
  it('finishes custom dispatch for B when the independently selected batch A closes', async () => {
    const { collection, batchApi, factoryApi, setTasks } = setup(), workbook = deferred<Blob>(), response = deferred<{ ok: boolean; message: string }>()
    await collection.selectBatch('batch-a')
    batchApi.workbook.mockReturnValueOnce(workbook.promise)
    factoryApi.remoteStartRun.mockReturnValueOnce(response.promise)
    factoryApi.state.mockResolvedValueOnce(state('batch-b', '运行中'))
    const run = collection.runBatch('batch-b', 'phone-2', 'AppA')
    await vi.waitFor(() => expect(batchApi.workbook).toHaveBeenCalled())
    collection.retireBatches(['batch-a'], true)
    workbook.resolve(new Blob(['workbook bytes']))
    await vi.waitFor(() => expect(factoryApi.remoteStartRun).toHaveBeenCalled())
    response.resolve({ ok: true, message: 'accepted' })
    await expect(run).resolves.toEqual({ ok: true, message: 'accepted' })
    expect(setTasks).toHaveBeenLastCalledWith(state('batch-b', '运行中').tasks)
    expect(collection.selectedBatchId.value).toBe('')
  })
  it('dispatches the same running batch to another phone with its own request identity', async () => {
    const { collection, factoryApi } = setup()
    factoryApi.addTask.mockResolvedValue(state('batch-a', '运行中'))
    await collection.runBatch('batch-a', 'phone-1', 'AppA')
    await collection.runBatch('batch-a', 'phone-2', 'AppA')
    const calls = factoryApi.remoteStartRun.mock.calls as unknown as Array<[{ phone_id: string; filename: string; request_id: string }]>
    expect(calls.map(([request]) => request.phone_id)).toEqual(['phone-1', 'phone-2'])
    expect(calls[0]![0].filename).toBe(calls[1]![0].filename)
    expect(calls[0]![0].request_id).not.toBe(calls[1]![0].request_id)
  })
  it('keeps the task state intact when the server rejects an occupied physical phone', async () => {
    const { collection, factoryApi, setTasks } = setup()
    factoryApi.remoteStartRun.mockRejectedValueOnce(new Error('手机已有运行中的任务：phone-1'))
    await expect(collection.runBatch('batch-a', 'phone-1', 'AppB')).rejects.toThrow('手机已有运行中的任务')
    expect(factoryApi.state).not.toHaveBeenCalled()
    expect(setTasks).toHaveBeenCalledTimes(1)
    expect(collection.busy.value).toBe(false)
    await collection.runBatch('batch-a', 'phone-2', 'AppA')
    expect(factoryApi.remoteStartRun).toHaveBeenCalledTimes(2)
  })
  it('does not restore retired task choices when an accepted response arrives late', async () => {
    const { collection, factoryApi, setTasks } = setup(), response = deferred<{ ok: boolean; message: string }>()
    factoryApi.remoteStartRun.mockReturnValueOnce(response.promise)
    const run = collection.runBatch('batch-a', 'phone-1', 'AppA')
    await vi.waitFor(() => expect(factoryApi.remoteStartRun).toHaveBeenCalledTimes(1))
    collection.retireBatches(['batch-a'])
    const callsBefore = setTasks.mock.calls.length
    response.resolve({ ok: true, message: 'accepted' })
    await expect(run).rejects.toThrow('已发布')
    expect(factoryApi.state).not.toHaveBeenCalled()
    expect(setTasks).toHaveBeenCalledTimes(callsBefore)
  })
  it('does not make a late status write after an accepted run unmounts', async () => {
    const { collection, factoryApi, setTasks } = setup(), response = deferred<{ ok: boolean; message: string }>()
    factoryApi.remoteStartRun.mockReturnValueOnce(response.promise)
    const run = collection.runBatch('batch-a')
    await vi.waitFor(() => expect(factoryApi.remoteStartRun).toHaveBeenCalledTimes(1))
    collection.dispose()
    const callsBefore = setTasks.mock.calls.length
    response.resolve({ ok: true, message: 'accepted' })
    await expect(run).rejects.toThrow('未继续下发')
    expect(factoryApi.state).not.toHaveBeenCalled()
    expect(setTasks).toHaveBeenCalledTimes(callsBefore)
  })
  it('downloads the selected workbook without importing or running and rejects late downloads', async () => {
    const first = setup()
    await first.collection.selectBatch('batch-a')
    expect(await first.collection.downloadBatch()).toBe(true)
    expect(first.downloadFile).toHaveBeenCalledWith(expect.any(Blob), detail().filename)
    expect(first.factoryApi.addTask).not.toHaveBeenCalled()
    expect(first.factoryApi.remoteStartRun).not.toHaveBeenCalled()
    const second = setup(), pending = deferred<Blob>()
    await second.collection.selectBatch('batch-a')
    second.batchApi.workbook.mockReturnValueOnce(pending.promise)
    const download = second.collection.downloadBatch()
    expect(await second.collection.selectBatch('batch-b')).toBe(false)
    second.collection.dispose()
    pending.resolve(new Blob(['bytes']))
    expect(await download).toBe(false)
    expect(second.downloadFile).not.toHaveBeenCalled()
  })
})
