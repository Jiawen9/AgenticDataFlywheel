import { ApiError } from '@/api'
import type { DatasetRelease } from '@/types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
export const MAX_DATASET_IMPORT_BYTES = 50 * 1024 * 1024
export interface DatasetImportFields {
  data_source: string
  data_date: string
  app: string
  level1: string
  level2: string
  sheet_name: string
}
export interface DatasetImportIssue { sheet: string; row: number | null; field: string; message: string }
export interface DatasetImportPreview {
  import_id: string | null
  valid: boolean
  sheets: string[]
  sheet_name: string
  filename: string
  expires_at?: string | null
  summary: {
    trajectory_count: number
    step_count: number
    apps: Array<{ name: string; trajectory_count: number; step_count: number }>
    scenes: Array<{ level1: string; level2: string; trajectory_count: number; step_count: number }>
  }
  errors: DatasetImportIssue[]
  warnings: DatasetImportIssue[]
  duplicate_release: { release_id: string; name: string } | null
}
async function request<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(API_BASE + path, { ...init, cache: 'no-store' })
  if (!response.ok) {
    let message = response.status + ' ' + response.statusText
    let detail: { code?: string; message?: string; release_id?: string } | undefined
    try {
      const payload = await response.json()
      if (typeof payload.detail === 'string') message = payload.detail
      else if (Array.isArray(payload.detail)) message = payload.detail.map((item: { msg: string }) => item.msg).join('；')
      else if (payload.detail && typeof payload.detail === 'object') { detail = payload.detail; message = detail?.message || message }
    } catch { /* Preserve the HTTP fallback if the response is not JSON. */ }
    throw new ApiError(message, response.status, detail)
  }
  return response.json() as Promise<T>
}
export const datasetImportApi = {
  preview(file: File, fields: DatasetImportFields, signal?: AbortSignal): Promise<DatasetImportPreview> {
    const form = new FormData()
    form.append('file', file)
    for (const [key, value] of Object.entries(fields)) form.append(key, value)
    return request('/api/dataset-release-imports/preview', { method: 'POST', body: form, signal })
  },
  async publish(payload: { import_id: string; name: string; request_id: string }): Promise<DatasetRelease> {
    const response = await request<{ release: DatasetRelease }>('/api/dataset-releases/import', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    })
    return response.release
  },
}
