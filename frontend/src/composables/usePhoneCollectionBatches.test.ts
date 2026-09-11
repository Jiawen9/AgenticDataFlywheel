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
  const factoryApi = { addTask: vi.fn(async (_description: string, _filename: string, _content: string, id?: string) => state(id)), remoteStartRun: vi.fn(async () => ({ ok: true, message: '已接受' })), startTask: vi.fn(async () => state('batch-a', '运行中')) }
  const setTasks = vi.fn()
  const downloadFile = vi.fn()
  const collection = usePhoneCollectionBatches(batchApi, factoryApi, { setTasks, downloadFile })
  return { collection, batchApi, factoryApi, setTasks, downloadFile }
}

describe('phone collection batch selection and dispatch', () => {
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
    expect(factoryApi.remoteStartRun).toHaveBeenCalledWith('collection-batch-batch-a.xlsx', 'phone-1', 'AppA')
    expect(batchApi.workbook.mock.invocationCallOrder[0]!).toBeLessThan(factoryApi.addTask.mock.invocationCallOrder[0]!)
    expect(factoryApi.addTask.mock.invocationCallOrder[0]!).toBeLessThan(factoryApi.remoteStartRun.mock.invocationCallOrder[0]!)
    expect(factoryApi.startTask).toHaveBeenCalledWith('collection-batch-batch-a.xlsx')
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
  it('does not continue dispatch after unmount or re-dispatch a running import', async () => {
    const first = setup(), pending = deferred<Blob>()
    first.batchApi.workbook.mockReturnValueOnce(pending.promise)
    await first.collection.selectBatch('batch-a')
    const run = first.collection.runBatch('batch-a')
    first.collection.dispose()
    pending.resolve(new Blob(['bytes']))
    await expect(run).rejects.toThrow('未继续下发')
    expect(first.factoryApi.remoteStartRun).not.toHaveBeenCalled()
    const second = setup()
    second.factoryApi.addTask.mockResolvedValue(state('batch-a', '运行中'))
    await expect(second.collection.runBatch('batch-a')).rejects.toThrow('已经在运行中')
    expect(second.factoryApi.remoteStartRun).not.toHaveBeenCalled()
  })
  it('persists an accepted remote run after unmount without updating destroyed UI', async () => {
    const { collection, factoryApi, setTasks } = setup(), response = deferred<{ ok: boolean; message: string }>()
    factoryApi.remoteStartRun.mockReturnValueOnce(response.promise)
    const run = collection.runBatch('batch-a')
    await vi.waitFor(() => expect(factoryApi.remoteStartRun).toHaveBeenCalledTimes(1))
    collection.dispose()
    const callsBefore = setTasks.mock.calls.length
    response.resolve({ ok: true, message: 'accepted' })
    await run
    expect(factoryApi.startTask).toHaveBeenCalledWith('collection-batch-batch-a.xlsx')
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
