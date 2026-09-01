/**
 * 手机工厂采集页 API 客户端。
 * 开发期由 vite-plugin-phone-factory 中间件提供服务，
 * 后续接入真实 backend 时替换实现即可。
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init)
  const payload = (await response.json().catch(() => null)) as (T & { error?: string }) | null
  if (!response.ok || payload === null || (payload as { error?: string }).error) {
    const message = (payload as { error?: string } | null)?.error || `${response.status} ${response.statusText}`
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
  /** 新增手机ID（追加到 phones.json，不能重复） */
  addPhone(phoneId: string): Promise<FactoryState> {
    return request<FactoryState>('/phones', jsonInit('POST', { phone_id: phoneId }))
  },
  /** 新增运行APP（追加到 apps.json，不能重复） */
  addApp(app: string): Promise<FactoryState> {
    return request<FactoryState>('/apps', jsonInit('POST', { app }))
  },
  /** 建立手机ID与运行APP的关联（phone_apps.json；同一手机可多APP，同一对不重复） */
  addPhoneApp(phoneId: string, app: string): Promise<FactoryState> {
    return request<FactoryState>('/phone-apps', jsonInit('POST', { phone_id: phoneId, app }))
  },
  /** 删除手机与某APP的关联（只删这一对，不影响该手机其他APP） */
  removePhoneApp(phoneId: string, app: string): Promise<FactoryState> {
    return request<FactoryState>('/phone-apps', jsonInit('DELETE', { phone_id: phoneId, app }))
  },
  /** 保存VLA接口（追加到 vla.json，不能重复） */
  saveVla(value: string): Promise<FactoryState> {
    return request<FactoryState>('/vla', jsonInit('POST', { value }))
  },
  /** 新增任务：上传文件到 /root/uuupppfffiiillleee 并登记 tasks.json */
  addTask(description: string, filename: string, contentBase64: string): Promise<FactoryState> {
    return request<FactoryState>(
      '/tasks',
      jsonInit('POST', { description, filename, content_base64: contentBase64 }),
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
  /** 删除任务（从 tasks.json 移除） */
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
  remoteStartRun(filename: string, phoneId: string, app: string): Promise<{ ok: boolean; message?: string; error?: string }> {
    return request<{ ok: boolean; message?: string; error?: string }>(
      '/remote/start-run',
      jsonInit('POST', { filename, phone_id: phoneId, app }),
    )
  },
}
