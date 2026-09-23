import { ApiError } from '@/api'

export interface RolloutTaskOverride {
  collection_case_id: string
  task: string
  app?: string
  scene?: string
  capability?: string
}
export interface RolloutImportInput {
  source_path: string
  batch_id: string
  name?: string
  app?: string
  scene?: string
  capability?: string
  task_overrides?: RolloutTaskOverride[]
}
export interface RolloutImportTask extends Omit<RolloutTaskOverride, 'app' | 'scene' | 'capability'> {
  app?: string | null
  scene?: string | null
  capability?: string | null
  task_id: string
  trajectory_count: number
  step_count: number
}
export interface RolloutImportPreview {
  import_id: string
  batch_id: string
  source_path: string
  valid: boolean
  task_count: number
  trajectory_count: number
  step_count: number
  tasks: RolloutImportTask[]
  warnings: string[]
  errors: string[]
  expires_at: string
}
export interface RolloutImportResult {
  batch_id: string
  collection_run_id: string
  task_count: number
  trajectory_count: number
  step_count: number
  reused: boolean
}
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(API_BASE + '/api/rollout-imports' + path, { ...init, cache: 'no-store' })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    const detail = payload?.detail
    const message = typeof detail === 'string' ? detail : Array.isArray(detail)
      ? detail.map((item: { msg: string }) => item.msg).join('；')
      : detail?.message || `${response.status} ${response.statusText}`
    throw new ApiError(message, response.status, detail && !Array.isArray(detail) && typeof detail === 'object' ? detail : undefined)
  }
  return response.json() as Promise<T>
}
const json = (body: unknown, signal?: AbortSignal): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal })
export const rolloutImportApi = {
  options: (signal?: AbortSignal) => request<{ default_source_path: string; allowed_roots: string[] }>('/options', { signal }),
  preview: (input: RolloutImportInput, signal?: AbortSignal) => request<RolloutImportPreview>('/preview', json(input, signal)),
  commit: (input: { import_id: string; request_id: string }, signal?: AbortSignal) => request<RolloutImportResult>('', json(input, signal)),
}
