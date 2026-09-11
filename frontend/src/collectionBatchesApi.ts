/** Immutable collection batches shared by generation and phone collection pages. */
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const BASE = `${API_BASE}/api/task-generation`

export interface CollectionBatchTask {
  task_id: string
  source_result_id: string
  task: string
  app: string
  scene: string | null
  capability: string | null
  sub_capability: string | null
  pre_dependency: 'zero' | 'weak' | 'strong' | 'pre_node' | 'unknown'
  pre_task_id: string | null
  source_status: unknown
  source_seed_id: string | null
  source_row: number | string | null
  source_task: string | null
  case_id: string | null
  dependency_error: unknown
  collection_case_id: string
}
export interface CollectionBatchSnapshot {
  schema_version: 1
  job_id: string
  kind: 'task_generation' | 'augmentation'
  job_status: 'succeeded' | 'partial'
  knowledge_base_version: string | null
  task_count: number
  tasks: CollectionBatchTask[]
  errors: unknown[]
  warnings: unknown[]
}
export interface CollectionBatchSummary {
  schema_version: 1
  batch_id: string
  source_job_id: string
  kind: 'task_generation' | 'augmentation'
  job_status: 'succeeded' | 'partial'
  knowledge_base_version: string | null
  created_at: string
  task_count: number
  apps: string[]
  filename: string
  download_url: string
}
export interface CollectionBatchDetail extends CollectionBatchSummary {
  snapshot: CollectionBatchSnapshot
}

async function checkedFetch(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(`${BASE}${path}`, { ...init, cache: 'no-store' })
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try {
      const payload = await response.json() as { detail?: string | Array<{ msg: string }> }
      message = Array.isArray(payload.detail) ? payload.detail.map(item => item.msg).join('；') : payload.detail || message
    } catch { /* Preserve HTTP errors for non-JSON responses. */ }
    throw new Error(message)
  }
  return response
}
export const collectionBatchDownloadUrl = (batchId: string) => `${BASE}/collection-batches/${encodeURIComponent(batchId)}/workbook`
export const collectionBatchesApi = {
  async list(jobId?: string): Promise<CollectionBatchSummary[]> {
    const query = jobId ? `?job_id=${encodeURIComponent(jobId)}` : ''
    return ((await (await checkedFetch(`/collection-batches${query}`)).json()) as { batches: CollectionBatchSummary[] }).batches
  },
  async detail(batchId: string): Promise<CollectionBatchDetail> {
    return (await checkedFetch(`/collection-batches/${encodeURIComponent(batchId)}`)).json() as Promise<CollectionBatchDetail>
  },
  async submit(jobId: string): Promise<CollectionBatchDetail> {
    return (await checkedFetch(`/jobs/${encodeURIComponent(jobId)}/collection-batch`, { method: 'POST' })).json() as Promise<CollectionBatchDetail>
  },
  async workbook(batchId: string): Promise<Blob> {
    return (await checkedFetch(`/collection-batches/${encodeURIComponent(batchId)}/workbook`)).blob()
  },
}
