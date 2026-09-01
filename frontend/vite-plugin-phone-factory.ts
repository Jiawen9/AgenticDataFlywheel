import { promises as fs } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import type { IncomingMessage, ServerResponse } from 'node:http'
import type { Plugin } from 'vite'

/**
 * 手机工厂采集页开发期数据服务。
 * 仅在 `npm run dev` 时生效：提供 /api/phone-factory/* 接口，
 * 读写工程内 frontend/data/*.json，并把任务文件上传到 /root/uuupppfffiiillleee。
 * 后续接入真实 backend 时，可直接删除本插件，把相同接口迁移到后端。
 */

const FRONTEND_ROOT = path.dirname(fileURLToPath(import.meta.url))
const DATA_DIR = path.join(FRONTEND_ROOT, 'data')
const UPLOAD_DIR = '/root/uuupppfffiiillleee'

interface PhoneAppRow {
  phone_id: string
  app: string
  status: string
}

interface TaskRow {
  description: string
  filename: string
  status: string
}

interface FactoryState {
  phones: string[]
  apps: string[]
  phoneApps: PhoneAppRow[]
  vla: string[]
  tasks: TaskRow[]
}

interface FactoryConfig {
  sampling_enabled: boolean
  temperature: number
  top_p: number
  use_experience_lib: boolean
}

const DEFAULT_CONFIG: FactoryConfig = {
  sampling_enabled: false,
  temperature: 0.7,
  top_p: 0.85,
  use_experience_lib: false,
}

const API_PREFIX = '/api/phone-factory'

async function ensureDirs(): Promise<void> {
  await fs.mkdir(DATA_DIR, { recursive: true })
  await fs.mkdir(UPLOAD_DIR, { recursive: true })
}

async function readJson<T>(file: string, fallback: T): Promise<T> {
  try {
    const raw = await fs.readFile(path.join(DATA_DIR, file), 'utf-8')
    return raw.trim() ? (JSON.parse(raw) as T) : fallback
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return fallback
    throw error
  }
}

async function writeJson(file: string, data: unknown): Promise<void> {
  await fs.writeFile(path.join(DATA_DIR, file), `${JSON.stringify(data, null, 2)}\n`, 'utf-8')
}

async function loadState(): Promise<FactoryState> {
  const [phones, apps, phoneApps, vla, tasks] = await Promise.all([
    readJson<string[]>('phones.json', []),
    readJson<string[]>('apps.json', []),
    readJson<PhoneAppRow[]>('phone_apps.json', []),
    readJson<string[]>('vla.json', []),
    readJson<TaskRow[]>('tasks.json', []),
  ])
  return { phones, apps, phoneApps, vla, tasks }
}

function send(res: ServerResponse, status: number, payload: unknown): void {
  res.statusCode = status
  res.setHeader('Content-Type', 'application/json; charset=utf-8')
  res.end(JSON.stringify(payload))
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    req.on('data', (chunk) => chunks.push(chunk as Buffer))
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf-8')))
    req.on('error', reject)
  })
}

function fail(res: ServerResponse, message: string, status = 400): void {
  send(res, status, { error: message })
}

/** 处理 /api/phone-factory/* 请求；返回 true 表示已处理，false 表示不归本中间件管。 */
async function handle(req: IncomingMessage, res: ServerResponse): Promise<boolean> {
  const url = req.url || ''
  if (!url.startsWith(API_PREFIX)) return false
  await ensureDirs()

  const pathname = url.split('?')[0]
  const method = (req.method || 'GET').toUpperCase()
  const route = pathname.slice(API_PREFIX.length) || '/'

  try {
    // 初始加载：一次性返回全部数据
    if (route === '/state' && method === 'GET') {
      send(res, 200, await loadState())
      return true
    }

    // 采样/经验库配置读取
    if (route === '/config' && method === 'GET') {
      const config = await readJson<FactoryConfig>('config.json', DEFAULT_CONFIG)
      send(res, 200, config)
      return true
    }

    if (method === 'GET') {
      send(res, 404, { error: 'Not found' })
      return true
    }

    const body = await readBody(req)
    const data = body ? (JSON.parse(body) as Record<string, unknown>) : {}

    if (route === '/phones' && method === 'POST') {
      const phoneId = String(data.phone_id ?? '').trim()
      if (!phoneId) return fail(res, '手机ID不能为空'), true
      const state = await loadState()
      if (state.phones.includes(phoneId)) return fail(res, `手机ID ${phoneId} 已存在`), true
      state.phones.push(phoneId)
      await writeJson('phones.json', state.phones)
      send(res, 200, await loadState())
      return true
    }

    if (route === '/apps' && method === 'POST') {
      const app = String(data.app ?? '').trim()
      if (!app) return fail(res, 'APP名称不能为空'), true
      const state = await loadState()
      if (state.apps.includes(app)) return fail(res, `APP ${app} 已存在`), true
      state.apps.push(app)
      await writeJson('apps.json', state.apps)
      send(res, 200, await loadState())
      return true
    }

    if (route === '/phone-apps' && method === 'POST') {
      const phoneId = String(data.phone_id ?? '').trim()
      const app = String(data.app ?? '').trim()
      if (!phoneId) return fail(res, '手机ID不能为空'), true
      if (!app) return fail(res, '运行APP不能为空'), true
      const state = await loadState()
      const exists = state.phoneApps.some((row) => row.phone_id === phoneId && row.app === app)
      if (exists) return fail(res, `手机 ${phoneId} 已关联 APP ${app}`), true
      state.phoneApps.push({ phone_id: phoneId, app, status: '空闲' })
      // 同步登记：手机ID 与 APP 各自的文件不重复
      if (!state.phones.includes(phoneId)) state.phones.push(phoneId)
      if (!state.apps.includes(app)) state.apps.push(app)
      await Promise.all([
        writeJson('phone_apps.json', state.phoneApps),
        writeJson('phones.json', state.phones),
        writeJson('apps.json', state.apps),
      ])
      send(res, 200, await loadState())
      return true
    }

    if (route === '/phone-apps' && method === 'DELETE') {
      const phoneId = String(data.phone_id ?? '').trim()
      const app = String(data.app ?? '').trim()
      const state = await loadState()
      state.phoneApps = state.phoneApps.filter(
        (row) => !(row.phone_id === phoneId && row.app === app),
      )
      await writeJson('phone_apps.json', state.phoneApps)
      send(res, 200, await loadState())
      return true
    }

    if (route === '/vla' && method === 'POST') {
      const value = String(data.value ?? '').trim()
      if (!value) return fail(res, 'VLA接口不能为空'), true
      const state = await loadState()
      if (state.vla.includes(value)) return fail(res, `VLA接口 ${value} 已存在`), true
      state.vla.push(value)
      await writeJson('vla.json', state.vla)
      send(res, 200, await loadState())
      return true
    }

    // 采样/经验库配置保存
    if (route === '/config' && method === 'POST') {
      const samplingEnabled = Boolean(data.sampling_enabled)
      const useExperienceLib = Boolean(data.use_experience_lib)
      const temperature = Number(data.temperature)
      const topP = Number(data.top_p)
      if (samplingEnabled) {
        if (!Number.isFinite(temperature) || !Number.isFinite(topP)) {
          return fail(res, 'temperature 与 top_p 必须是数字'), true
        }
      }
      const config: FactoryConfig = {
        sampling_enabled: samplingEnabled,
        temperature: Number.isFinite(temperature) ? temperature : DEFAULT_CONFIG.temperature,
        top_p: Number.isFinite(topP) ? topP : DEFAULT_CONFIG.top_p,
        use_experience_lib: useExperienceLib,
      }
      await writeJson('config.json', config)
      send(res, 200, config)
      return true
    }

    if (route === '/tasks' && method === 'POST') {
      const description = String(data.description ?? '').trim()
      const rawFilename = String(data.filename ?? '').trim()
      const contentBase64 = String(data.content_base64 ?? '')
      if (!description) return fail(res, '任务描述不能为空'), true
      if (!rawFilename) return fail(res, '请先选择文件'), true
      if (!contentBase64) return fail(res, '文件内容为空'), true
      const filename = path.basename(rawFilename)
      await fs.writeFile(path.join(UPLOAD_DIR, filename), Buffer.from(contentBase64, 'base64'))
      const state = await loadState()
      state.tasks.push({ description, filename, status: '未运行' })
      await writeJson('tasks.json', state.tasks)
      send(res, 200, await loadState())
      return true
    }

    // 开始运行任务：状态 未运行 -> 运行中
    if (route === '/tasks/start' && method === 'POST') {
      const filename = path.basename(String(data.filename ?? '').trim())
      const state = await loadState()
      const task = state.tasks.find((item) => item.filename === filename)
      if (!task) return fail(res, `任务 ${filename} 不存在`, 404), true
      task.status = '运行中'
      await writeJson('tasks.json', state.tasks)
      send(res, 200, await loadState())
      return true
    }

    if (route === '/tasks' && method === 'DELETE') {
      const filename = path.basename(String(data.filename ?? '').trim())
      const state = await loadState()
      state.tasks = state.tasks.filter((task) => task.filename !== filename)
      await writeJson('tasks.json', state.tasks)
      send(res, 200, await loadState())
      return true
    }

    send(res, 404, { error: `Unsupported ${method} ${route}` })
  } catch (error) {
    console.error('[phone-factory]', error)
    send(res, 500, { error: (error as Error).message || '服务器内部错误' })
  }
  return true
}

export function phoneFactoryDevServer(): Plugin {
  return {
    name: 'phone-factory-dev-server',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        void handle(req, res)
          .then((handled) => {
            if (!handled) next()
          })
          .catch((error) => {
            console.error('[phone-factory] middleware error:', error)
            if (!res.writableEnded) {
              res.statusCode = 500
              res.setHeader('Content-Type', 'application/json; charset=utf-8')
              res.end(JSON.stringify({ error: String((error as Error).message || error) }))
            }
          })
      })
    },
  }
}
