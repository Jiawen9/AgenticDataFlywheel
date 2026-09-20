import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api'
import { useExternalDatasetImport, localImportDate } from './useExternalDatasetImport'
import { MAX_DATASET_IMPORT_BYTES, type DatasetImportPreview } from '@/datasetImportApi'
import type { DatasetRelease } from '@/types'

function result(id = 'import1', valid = true): DatasetImportPreview {
  return { import_id: valid ? id : null, valid, sheets: ['轨迹', '表2'], sheet_name: '轨迹', filename: 'a.xlsx', summary: { trajectory_count: 2, step_count: 5, apps: [{ name: 'A', trajectory_count: 2, step_count: 5 }], scenes: [] }, errors: valid ? [] : [{ sheet: '轨迹', row: 3, field: 'Step', message: '缺少步骤' }], warnings: [], duplicate_release: null }
}
const release = { release_id: 'external-release', source_kind: 'external_manual', name: '外部数据', batch_ids: ['external_import1'] } as DatasetRelease
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (error: Error) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
function setup() {
  const client = { preview: vi.fn(async () => result()), publish: vi.fn(async () => release) }
  const state = useExternalDatasetImport(client)
  state.open(); state.name.value = '外部数据'; state.selectFile(new File(['data'], 'a.xlsx'))
  return { client, state }
}

describe('external table draft and publication', () => {
  it('defaults to manual collection and the local calendar date, then requires valid preview and name', async () => {
    const { client, state } = setup()
    expect(localImportDate(new Date(2026, 0, 2, 0, 3))).toBe('2026-01-02')
    expect(state.fields).toMatchObject({ data_source: '人工采集', data_date: localImportDate(), app: '', level1: '', level2: '', sheet_name: '' })
    expect(state.canPublish.value).toBe(false)
    await state.prepare()
    expect(client.preview).toHaveBeenCalledWith(state.file.value, expect.objectContaining({ data_source: '人工采集', sheet_name: '' }), expect.any(AbortSignal))
    expect(state.canPublish.value).toBe(true)
    state.name.value = '  '
    expect(state.canPublish.value).toBe(false)
    state.dispose()
  })
  it.each(['data_source', 'data_date', 'app', 'level1', 'level2', 'sheet_name'] as const)('immediately invalidates preview when %s changes, keeping the workbook sheet choices', async field => {
    const { state } = setup()
    await state.prepare()
    state.fields[field] = field === 'data_date' ? '2026-09-19' : '新值'
    expect(state.preview.value).toBeNull()
    expect(state.canPublish.value).toBe(false)
    expect(state.sheets.value).toEqual(['轨迹', '表2'])
    state.dispose()
  })
  it('retains invalid preview issues and sheet options and can preview a different sheet', async () => {
    const { client, state } = setup()
    client.preview.mockResolvedValueOnce(result('', false))
    await state.prepare()
    expect(state.preview.value?.errors[0]?.row).toBe(3)
    expect(state.canPublish.value).toBe(false)
    expect(state.sheets.value).toEqual(['轨迹', '表2'])
    state.fields.sheet_name = '表2'
    await state.prepare()
    expect(client.preview).toHaveBeenLastCalledWith(state.file.value, expect.objectContaining({ sheet_name: '表2' }), expect.any(AbortSignal))
    expect(state.canPublish.value).toBe(true)
    state.dispose()
  })
  it('aborts and discards late preview successes and errors after edits or newer requests', async () => {
    const { client, state } = setup(), old = deferred<DatasetImportPreview>()
    client.preview.mockReturnValueOnce(old.promise)
    const first = state.prepare()
    const signal = (client.preview.mock.calls[0] as unknown as [File, object, AbortSignal])[2]
    state.fields.app = 'B'
    expect(signal.aborted).toBe(true)
    await state.prepare()
    old.resolve(result('obsolete'))
    await first
    expect(state.preview.value?.import_id).toBe('import1')
    const stale = deferred<DatasetImportPreview>()
    client.preview.mockReturnValueOnce(stale.promise)
    const pending = state.prepare()
    state.selectFile(new File(['b'], 'new.xlsm'))
    stale.reject(new Error('old error'))
    await pending
    expect(state.preview.value).toBeNull()
    expect(state.sheets.value).toEqual([])
    expect(state.error.value).toBe('')
    state.dispose()
  })
  it('retains the exact request_id after failed publish, including closing and reopening the dialog', async () => {
    const { client, state } = setup()
    await state.prepare()
    client.publish.mockRejectedValueOnce(new Error('response lost'))
    expect(await state.publish()).toBeNull()
    const first = client.publish.mock.calls[0]
    expect(state.error.value).toBe('response lost')
    expect(state.preview.value?.import_id).toBe('import1')
    expect(state.file.value?.name).toBe('a.xlsx')
    state.close(); state.open()
    expect(await state.publish()).toEqual(release)
    expect(client.publish.mock.calls[1]).toEqual(first)
    expect(state.visible.value).toBe(false)
    expect(state.file.value).toBeNull()
    expect(state.preview.value).toBeNull()
    expect(state.name.value).toBe('')
    state.dispose()
  })
  it.each([[410, 'import_expired'], [409, 'import_changed'], [409, 'duplicate_metadata']] as const)('requires another preview after %s/%s without clearing the file or metadata', async (status, code) => {
    const { client, state } = setup()
    state.fields.app = 'A'
    await state.prepare()
    client.publish.mockRejectedValueOnce(new ApiError('需要重新核对', status, { code }))
    await state.publish()
    expect(state.preview.value).toBeNull()
    expect(state.canPublish.value).toBe(false)
    expect(state.requestId.value).toBe('')
    expect(state.error.value).toContain('重新预览')
    expect(state.name.value).toBe('外部数据')
    expect(state.file.value?.name).toBe('a.xlsx')
    expect(state.fields.app).toBe('A')
    expect(state.sheets.value).toEqual(['轨迹', '表2'])
    expect(await state.publish()).toBeNull()
    expect(client.publish).toHaveBeenCalledTimes(1)
    await state.prepare()
    expect(state.canPublish.value).toBe(true)
    state.dispose()
  })
  it('retains the request identity after a server failure', async () => {
    const { client, state } = setup()
    await state.prepare()
    client.publish.mockRejectedValueOnce(new ApiError('暂时无法提交', 500))
    await state.publish()
    const first = client.publish.mock.calls[0]
    expect(state.canPublish.value).toBe(true)
    await state.publish()
    expect(client.publish.mock.calls[1]).toEqual(first)
    state.dispose()
  })
  it('prevents duplicate submits, closing, file changes and new preview while publishing', async () => {
    const { client, state } = setup(), pending = deferred<DatasetRelease>()
    await state.prepare()
    client.publish.mockReturnValueOnce(pending.promise)
    const publishing = state.publish()
    expect(state.close()).toBe(false)
    state.selectFile(new File(['other'], 'b.xlsx'))
    await state.prepare()
    expect(await state.publish()).toBeNull()
    expect(client.publish).toHaveBeenCalledTimes(1)
    expect(client.preview).toHaveBeenCalledTimes(1)
    expect(state.file.value?.name).toBe('a.xlsx')
    expect(state.publishing.value).toBe(true)
    pending.resolve(release)
    expect(await publishing).toEqual(release)
    state.dispose()
  })
  it('keeps a reviewed preview when only its name changes and starts a new request identity for the new payload', async () => {
    const { client, state } = setup()
    await state.prepare()
    client.publish.mockRejectedValue(new Error('failed'))
    await state.publish()
    const first = state.requestId.value
    state.name.value = '新名称'
    expect(state.preview.value?.import_id).toBe('import1')
    await state.publish()
    expect(state.requestId.value).not.toBe(first)
    state.dispose()
  })
  it('rejects oversized files and unsupported formats locally, allowing xlsm', async () => {
    const { client, state } = setup()
    const large = new File(['a'], 'large.xlsx')
    Object.defineProperty(large, 'size', { value: MAX_DATASET_IMPORT_BYTES + 1 })
    state.selectFile(large)
    await state.prepare()
    expect(state.error.value).toContain('50 MiB')
    state.selectFile(new File(['x'], 'old.xls'))
    await state.prepare()
    expect(state.error.value).toContain('.xlsm')
    expect(client.preview).not.toHaveBeenCalled()
    state.selectFile(new File(['m'], 'MACRO.XLSM'))
    await state.prepare()
    expect(client.preview).toHaveBeenCalledTimes(1)
    state.dispose()
  })
  it('ignores preview replies after closing or unmounting', async () => {
    const { client, state } = setup(), late = deferred<DatasetImportPreview>()
    client.preview.mockReturnValueOnce(late.promise)
    const pending = state.prepare()
    state.close()
    late.resolve(result())
    await pending
    expect(state.preview.value).toBeNull()
    state.open()
    const unmount = deferred<DatasetImportPreview>()
    client.preview.mockReturnValueOnce(unmount.promise)
    const second = state.prepare()
    state.dispose()
    unmount.resolve(result())
    await second
    expect(state.preview.value).toBeNull()
  })
})
