import { afterEach, describe, expect, it } from 'vitest'
import { promises as fs } from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { createPhoneFactoryTaskStore } from './phone-factory-task-store'

const directories: string[] = []
async function setup() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'phone-task-store-'))
  directories.push(root)
  return { root, uploads: path.join(root, 'uploads'), store: createPhoneFactoryTaskStore(path.join(root, 'data'), path.join(root, 'uploads')) }
}
const batch = (id = 'batch-a', content = 'workbook bytes') => ({
  description: `采集批次 ${id}`, filename: `collection-batch-${id}.xlsx`, content_base64: Buffer.from(content).toString('base64'), source_batch_id: id,
})
afterEach(async () => { await Promise.all(directories.splice(0).map(directory => fs.rm(directory, { recursive: true, force: true }))) })

describe('phone factory batch registration', () => {
  it('imports a batch once and keeps its running status on an identical retry', async () => {
    const { store, uploads } = await setup()
    await store.add(batch())
    await store.start(batch().filename)
    const rows = await store.add({ ...batch(), description: '不覆盖原描述' })
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ source_batch_id: 'batch-a', status: '运行中', description: '采集批次 batch-a' })
    expect(await fs.readFile(path.join(uploads, batch().filename), 'utf8')).toBe('workbook bytes')
  })
  it('rejects a different payload for the same batch without changing its file or registration', async () => {
    const { store, uploads } = await setup()
    await store.add(batch())
    await expect(store.add(batch('batch-a', 'changed'))).rejects.toMatchObject({ status: 409 })
    expect(await fs.readFile(path.join(uploads, batch().filename), 'utf8')).toBe('workbook bytes')
    expect(await store.list()).toHaveLength(1)
  })
  it('serializes duplicate imports, other uploads, start and delete without losing rows', async () => {
    const { store } = await setup()
    await Promise.all([store.add(batch()), store.add(batch()), store.add(batch('batch-b')), store.add(batch('batch-c'))])
    await Promise.all([store.start(batch().filename), store.remove(batch('batch-b').filename), store.add(batch('batch-d'))])
    const rows = await store.list()
    expect(rows.map(row => row.source_batch_id)).toEqual(['batch-a', 'batch-c', 'batch-d'])
    expect(rows[0]!.status).toBe('运行中')
  })
  it('keeps legacy manual upload/start/delete and protects batch filenames', async () => {
    const { store, uploads } = await setup()
    await store.add({ description: '手工任务', filename: 'manual.xlsx', content_base64: Buffer.from('manual').toString('base64') })
    await store.start('manual.xlsx')
    expect((await store.list())[0]).toMatchObject({ description: '手工任务', status: '运行中' })
    await store.add(batch())
    await expect(store.add({ ...batch(), source_batch_id: undefined })).rejects.toMatchObject({ status: 409 })
    await store.remove('manual.xlsx')
    expect(await store.list()).toHaveLength(1)
    expect(await fs.readFile(path.join(uploads, batch().filename), 'utf8')).toBe('workbook bytes')
  })
  it('recovers a missing identical batch file using its saved digest and rejects wrong content', async () => {
    const { store, uploads } = await setup()
    await store.add(batch())
    await store.start(batch().filename)
    await fs.unlink(path.join(uploads, batch().filename))
    await expect(store.add(batch('batch-a', 'changed'))).rejects.toMatchObject({ status: 409 })
    expect((await store.add(batch()))[0]!.status).toBe('运行中')
    expect(await fs.readFile(path.join(uploads, batch().filename), 'utf8')).toBe('workbook bytes')
  })
  it('does not let failed operations poison the queue or rename a batch', async () => {
    const { store } = await setup()
    await expect(store.start('missing.xlsx')).rejects.toMatchObject({ status: 404 })
    await expect(store.add({ ...batch(), filename: 'other.xlsx' })).rejects.toMatchObject({ status: 409 })
    await expect(store.add({ ...batch(), source_batch_id: '../escape' })).rejects.toMatchObject({ status: 400 })
    expect(await store.add(batch())).toHaveLength(1)
  })
})
