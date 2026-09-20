import { notifyBatchesPublished } from '@/utils/batchLifecycle'
/**
 * 手机工厂采集页 API 客户端。
 * 由 FastAPI 的 /api/phone-factory/* 提供服务，
 * 开发环境经 Vite 代理访问同一后端。
 */

export interface PhoneAppRow {
  phone_id: string
  app: string
  status: string
}

export interface TaskRow {
  description: string
  filename: string
  status: string
  source_batch_id?: string
}

export interface FactoryState {
  phones: string[]
  apps: string[]
  phoneApps: PhoneAppRow[]
  vla: string[]
  tasks: TaskRow[]
}

export interface FactoryConfig {
  sampling_enabled: boolean
  temperature: number
  top_p: number
  use_experience_lib: boolean
}

const BASE = '/api/phone-factory'

// One ID per intentional run; callers that retry a request can reuse the same ID.
export function newRunRequestId(): string {
  if (globalThis.crypto.randomUUID) return globalThis.crypto.randomUUID()
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init)
  const payload = (await response.json().catch(() => null)) as (T & { error?: string }) | null
  if (!response.ok || payload === null || (payload as { error?: string }).error) {
    const detail = (payload as { detail?: { code?: string; message?: string; batch_id?: string; release_id?: string; published_at?: string } } | null)?.detail
    if (detail?.code === 'batch_published' && detail.batch_id) notifyBatchesPublished({ batch_ids: [detail.batch_id], release_id: detail.release_id ?? null, published_at: detail.published_at })
    const message = detail?.message || (payload as { error?: string } | null)?.error || `${response.status} ${response.statusText}`
    throw new Error(message)
  }
  return payload as T
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

export const phoneFactoryApi = {
  /** 一次性拉取全部状态 */
  state(): Promise<FactoryState> {
    return request<FactoryState>('/state')
  },
  /** 新增手机ID（SQLite 登记，不能重复） */
  addPhone(phoneId: string): Promise<FactoryState> {
    return request<FactoryState>('/phones', jsonInit('POST', { phone_id: phoneId }))
  },
  /** 新增运行APP（SQLite 登记，不能重复） */
  addApp(app: string): Promise<FactoryState> {
    return request<FactoryState>('/apps', jsonInit('POST', { app }))
  },
  /** 建立手机ID与运行APP的关联（SQLite；同一手机可多APP，同一对不重复） */
  addPhoneApp(phoneId: string, app: string): Promise<FactoryState> {
    return request<FactoryState>('/phone-apps', jsonInit('POST', { phone_id: phoneId, app }))
  },
  /** 删除手机与某APP的关联（只删这一对，不影响该手机其他APP） */
  removePhoneApp(phoneId: string, app: string): Promise<FactoryState> {
    return request<FactoryState>('/phone-apps', jsonInit('DELETE', { phone_id: phoneId, app }))
  },
  /** 保存VLA接口（SQLite 登记，不能重复） */
  saveVla(value: string): Promise<FactoryState> {
    return request<FactoryState>('/vla', jsonInit('POST', { value }))
  },
  /** 新增任务：后端保存到数据根目录 inputs/phone_factory，并在 SQLite 登记。 */
  addTask(description: string, filename: string, contentBase64: string, sourceBatchId?: string): Promise<FactoryState> {
    return request<FactoryState>(
      '/tasks',
      jsonInit('POST', { description, filename, content_base64: contentBase64, ...(sourceBatchId ? { source_batch_id: sourceBatchId } : {}) }),
    )
  },
  /** 开始运行任务：状态 未运行 -> 运行中 */
  startTask(filename: string): Promise<FactoryState> {
    return request<FactoryState>('/tasks/start', jsonInit('POST', { filename }))
  },
  /** 读取采样/经验库配置 */
  config(): Promise<FactoryConfig> {
    return request<FactoryConfig>('/config')
  },
  /** 保存采样/经验库配置 */
  saveConfig(config: FactoryConfig): Promise<FactoryConfig> {
    return request<FactoryConfig>('/config', jsonInit('POST', config))
  },
  /** 删除任务登记 */
  removeTask(filename: string): Promise<FactoryState> {
    return request<FactoryState>('/tasks', jsonInit('DELETE', { filename }))
  },
  /** 新增手机 -> 通知 server 端（client add-phone） */
  remoteAddPhone(phoneId: string): Promise<{ ok: boolean; message?: string; error?: string }> {
    return request<{ ok: boolean; message?: string; error?: string }>(
      '/remote/add-phone',
      jsonInit('POST', { phone_id: phoneId }),
    )
  },
  /** 开始运行 -> 把任务文件与 手机ID/运行APP 关联文件 发送到 server 端 */
  remoteStartRun(filename: string, phoneId: string, app: string, requestId = newRunRequestId()): Promise<{ ok: boolean; message?: string; error?: string }> {
    return request<{ ok: boolean; message?: string; error?: string }>(
      '/remote/start-run',
      jsonInit('POST', { filename, phone_id: phoneId, app, request_id: requestId }),
    )
  },
}
