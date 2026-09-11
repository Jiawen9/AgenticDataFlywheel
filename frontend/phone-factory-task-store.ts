import { createHash, randomUUID } from 'node:crypto'
import { promises as fs } from 'node:fs'
import path from 'node:path'

export interface StoredPhoneFactoryTask {
  description: string
  filename: string
  status: string
  source_batch_id?: string
  content_sha256?: string
}

export class PhoneFactoryTaskError extends Error {
  constructor(message: string, public status = 400) { super(message) }
}

/** One queue owns task registration, deletion and start-state updates. */
export function createPhoneFactoryTaskStore(dataDir: string, uploadDir: string) {
  let tail: Promise<unknown> = Promise.resolve()
  const tasksPath = path.join(dataDir, 'tasks.json')
  function serial<T>(action: () => Promise<T>): Promise<T> {
    const result = tail.then(action)
    tail = result.catch(() => undefined)
    return result
  }
  async function readTasks(): Promise<StoredPhoneFactoryTask[]> {
    try {
      const raw = await fs.readFile(tasksPath, 'utf8')
      const tasks = raw.trim() ? JSON.parse(raw) : []
      if (!Array.isArray(tasks)) throw new PhoneFactoryTaskError('任务登记文件格式无效', 409)
      return tasks
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return []
      throw error
    }
  }
  async function atomicWrite(destination: string, value: string | Buffer) {
    await fs.mkdir(path.dirname(destination), { recursive: true })
    const temporary = `${destination}.${randomUUID()}.tmp`
    try { await fs.writeFile(temporary, value); await fs.rename(temporary, destination) }
    finally { await fs.unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error }) }
  }
  const saveTasks = (tasks: StoredPhoneFactoryTask[]) => atomicWrite(tasksPath, `${JSON.stringify(tasks, null, 2)}\n`)
  function filenameOf(value: unknown): string {
    const filename = path.basename(String(value ?? '').trim().replace(/\\/g, '/'))
    if (!filename || filename === '.' || filename === '..') throw new PhoneFactoryTaskError('任务文件名不能为空')
    return filename
  }
  return {
    list: () => serial(readTasks),
    add: (input: { description?: unknown; filename?: unknown; content_base64?: unknown; source_batch_id?: unknown }) => serial(async () => {
      const description = String(input.description ?? '').trim()
      const filename = filenameOf(input.filename)
      const content = Buffer.from(String(input.content_base64 ?? ''), 'base64')
      if (!description) throw new PhoneFactoryTaskError('任务描述不能为空')
      if (!content.length) throw new PhoneFactoryTaskError('文件内容为空')
      const sourceBatchId = input.source_batch_id
      if (sourceBatchId !== undefined && (typeof sourceBatchId !== 'string' || !/^[A-Za-z0-9_-]+$/.test(sourceBatchId))) {
        throw new PhoneFactoryTaskError('采集批次编号无效')
      }
      if (sourceBatchId && filename !== `collection-batch-${sourceBatchId}.xlsx`) throw new PhoneFactoryTaskError('采集批次文件名不匹配', 409)
      if (!sourceBatchId && /^collection-batch-/i.test(filename)) throw new PhoneFactoryTaskError('该文件名前缀仅供采集批次使用，请重命名手动上传文件', 409)
      const tasks = await readTasks()
      const existingBatch = sourceBatchId ? tasks.find(task => task.source_batch_id === sourceBatchId) : undefined
      const contentHash = createHash('sha256').update(content).digest('hex')
      const destination = path.join(uploadDir, filename)
      if (existingBatch) {
        if (existingBatch.filename !== filename || (existingBatch.content_sha256 && existingBatch.content_sha256 !== contentHash)) {
          throw new PhoneFactoryTaskError('该采集批次已导入，不能替换为不同内容', 409)
        }
        try {
          const existing = await fs.readFile(destination)
          if (!existing.equals(content)) throw new PhoneFactoryTaskError('该采集批次已导入，不能替换为不同内容', 409)
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
          if (!existingBatch.content_sha256) throw new PhoneFactoryTaskError('已登记批次文件缺失，无法确认原始内容', 409)
          await atomicWrite(destination, content)
        }
        return tasks // In particular, preserve a running task's state.
      }
      if (tasks.some(task => task.filename === filename && (sourceBatchId || task.source_batch_id))) {
        throw new PhoneFactoryTaskError('任务文件名已被其他任务占用', 409)
      }
      if (sourceBatchId) {
        try {
          const existing = await fs.readFile(destination)
          if (!existing.equals(content)) throw new PhoneFactoryTaskError('批次文件已存在且内容不同', 409)
        } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error }
      }
      await atomicWrite(destination, content)
      tasks.push({ description, filename, status: '未运行', ...(sourceBatchId ? { source_batch_id: sourceBatchId, content_sha256: contentHash } : {}) })
      await saveTasks(tasks)
      return tasks
    }),
    start: (value: unknown) => serial(async () => {
      const filename = filenameOf(value)
      const tasks = await readTasks()
      const task = tasks.find(item => item.filename === filename)
      if (!task) throw new PhoneFactoryTaskError(`任务 ${filename} 不存在`, 404)
      task.status = '运行中'
      await saveTasks(tasks)
      return tasks
    }),
    remove: (value: unknown) => serial(async () => {
      const filename = filenameOf(value)
      const tasks = (await readTasks()).filter(task => task.filename !== filename)
      await saveTasks(tasks)
      return tasks
    }),
  }
}
