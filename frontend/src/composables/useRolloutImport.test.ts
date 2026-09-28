import { afterEach, describe, expect, it, vi } from 'vitest'
import { useRolloutImport } from './useRolloutImport'
import type { RolloutImportPreview, RolloutImportResult } from '@/rolloutImportApi'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}
const preview = (overrides: Partial<RolloutImportPreview> = {}): RolloutImportPreview => ({
  import_id: 'import1', source_path: 'D:\\raw\\rollout', batch_id: 'new-batch', valid: true,
  task_count: 1, trajectory_count: 2, step_count: 4, tasks: [{ collection_case_id: '001', task_id: 'task1', task: '', trajectory_count: 2, step_count: 4 }],
  warnings: [], errors: [], expires_at: '2099-01-01T00:00:00Z', ...overrides,
})
const result: RolloutImportResult = { batch_id: 'new-batch', collection_run_id: 'run1', task_count: 1, trajectory_count: 2, step_count: 4, reused: false }
const cleanups: Array<() => void> = []
function setup() {
  const client = {
    options: vi.fn().mockResolvedValue({ default_source_path: 'D:\\raw\\rollout', allowed_roots: ['D:\\raw'] }),
    preview: vi.fn().mockResolvedValue(preview()), commit: vi.fn().mockResolvedValue(result),
  }
  const imported = vi.fn(), flow = useRolloutImport(client, imported)
  cleanups.push(flow.dispose)
  return { client, imported, flow }
}
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()); vi.restoreAllMocks() })
describe('rollout import draft and request lifecycle', () => {
  it('requires a reviewed preview and invalidates it when metadata or a task goal changes', async () => {
    const { flow, client } = setup()
    await flow.open()
    expect(flow.draft.source_path).toBe('D:\\raw\\rollout')
    expect(flow.draft.batch_id).toMatch(/^rollout-\d{8}-[a-f0-9]{8}$/)
    expect(flow.canCommit.value).toBe(false)
    await flow.validate()
    expect(flow.canCommit.value).toBe(true)
    flow.draft.app = '地图'
    expect(flow.canCommit.value).toBe(false)
    expect(flow.stale.value).toBe(true)
    await flow.commit()
    expect(client.commit).not.toHaveBeenCalled()
    await flow.validate()
    flow.taskEdits['001'] = '搜索附近餐厅'
    expect(flow.canCommit.value).toBe(false)
    await flow.validate()
    expect(client.preview.mock.lastCall![0]).toMatchObject({ app: '地图', task_overrides: [{ collection_case_id: '001', task: '搜索附近餐厅' }] })
    expect(flow.canCommit.value).toBe(true)
    flow.draft.source_path = 'D:\\raw\\other'
    expect(flow.taskEdits).toEqual({})
    expect(flow.canCommit.value).toBe(false)
  })
  it('keeps invalid tasks editable while blocking invalid and expired previews', async () => {
    const { flow, client } = setup()
    client.preview.mockResolvedValueOnce(preview({ valid: false, errors: ['001：缺少任务目标'] }))
    await flow.open(); await flow.validate()
    expect(flow.preview.value?.tasks[0]?.collection_case_id).toBe('001')
    expect(flow.canCommit.value).toBe(false)
    flow.taskEdits['001'] = '补充任务目标'
    client.preview.mockResolvedValueOnce(preview({ expires_at: '2000-01-01T00:00:00Z' }))
    await flow.validate(); await flow.commit()
    expect(client.commit).not.toHaveBeenCalled()
    expect(flow.error.value).toContain('已过期')
    expect(flow.canCommit.value).toBe(false)
  })
  it('aborts and ignores an older preview after an edit and after closing', async () => {
    const { flow, client } = setup()
    await flow.open()
    const old = deferred<RolloutImportPreview>()
    client.preview.mockReturnValueOnce(old.promise)
    const first = flow.validate(), firstSignal = client.preview.mock.calls[0]![1] as AbortSignal
    flow.draft.scene = '生活'
    expect(firstSignal.aborted).toBe(true)
    await flow.validate()
    old.resolve(preview({ import_id: 'outdated' })); await first
    expect(flow.preview.value?.import_id).toBe('import1')
    const pending = deferred<RolloutImportPreview>()
    client.preview.mockReturnValueOnce(pending.promise)
    const second = flow.validate()
    expect(flow.close()).toBe(true)
    pending.resolve(preview({ import_id: 'closed' })); await second
    expect(flow.visible.value).toBe(false)
    expect(flow.preview.value?.import_id).toBe('import1')
  })
  it('does not let old options overwrite a reopened dialog or the user supplied path', async () => {
    const { flow, client } = setup()
    const old = deferred<{ default_source_path: string; allowed_roots: string[] }>()
    client.options.mockReturnValueOnce(old.promise)
    const first = flow.open()
    flow.close()
    await flow.open()
    flow.draft.source_path = 'D:\\chosen'
    old.resolve({ default_source_path: 'D:\\outdated', allowed_roots: ['D:\\outdated'] }); await first
    expect(flow.draft.source_path).toBe('D:\\chosen')
    expect(flow.allowedRoots.value).toEqual(['D:\\raw'])
  })
  it('blocks double submits and close during import, and reuses request identity after a lost response', async () => {
    const { flow, client, imported } = setup()
    await flow.open(); await flow.validate()
    client.commit.mockRejectedValueOnce(new Error('响应丢失'))
    await flow.commit()
    const firstRequest = client.commit.mock.calls[0]![0]
    expect(flow.error.value).toContain('同一请求编号')
    const pending = deferred<RolloutImportResult>()
    client.commit.mockReturnValueOnce(pending.promise)
    const retry = flow.commit()
    expect(client.commit.mock.lastCall![0]).toEqual(firstRequest)
    expect(flow.close()).toBe(false)
    await flow.commit()
    expect(client.commit).toHaveBeenCalledTimes(2)
    pending.resolve({ ...result, reused: true }); await retry
    expect(imported).toHaveBeenCalledExactlyOnceWith({ ...result, reused: true })
    expect(flow.visible.value).toBe(false)
    expect(flow.preview.value).toBeNull()
  })
  it('ignores commit completion after the hosting page is disposed', async () => {
    const { flow, client, imported } = setup()
    await flow.open(); await flow.validate()
    const pending = deferred<RolloutImportResult>()
    client.commit.mockReturnValueOnce(pending.promise)
    const commit = flow.commit()
    const signal = client.commit.mock.lastCall![1] as AbortSignal
    flow.dispose()
    expect(signal.aborted).toBe(true)
    pending.resolve(result); await commit
    expect(imported).not.toHaveBeenCalled()
  })
})
