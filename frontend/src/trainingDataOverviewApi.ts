const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const BASE = `${API_BASE}/api/training-data-overview`

export interface OverviewFilters {
  source: string
  level1: string
  level2: string
  app: string
  start_date: string
  end_date: string
}
export interface OverviewCounts {
  total_trajectories: number
  subtask_trajectories: number
  total_steps: number
}
export interface OverviewConversion {
  release_id: string
  name: string
  status: 'queued' | 'running' | 'succeeded' | 'failed'
  error: string | null
  warnings: string[]
  updated_at: string
}
export interface TrainingDataOverview {
  schema_version: 1
  version: string | null
  updated_at: string | null
  overview: OverviewCounts & {
    show_manual_refine_steps: boolean
    manual_refine_steps: number | null
    manual_known_steps: number
    manual_unknown_steps: number
    level1_scenes: number
    level2_scenes: number
    total_apps: number
    avg_steps_per_trajectory: number
  }
  filters: {
    sources: string[]
    scenes: Array<{ name: string; level2_scenes: string[] }>
    apps: string[]
    date_range: { min_date: string | null; max_date: string | null }
  }
  trend: Array<OverviewCounts & { date: string }>
  app_stats: Array<OverviewCounts & { name: string }>
  scene_stats: Array<OverviewCounts & { name: string }>
  action_stats: Array<{ category: string; count: number }>
  step_stats: Array<{ steps: number; count: number }>
  conversions: OverviewConversion[]
  warnings: string[]
  workbook_url: string | null
}

async function checkedFetch(url: string, init?: RequestInit) {
  const response = await fetch(url, { ...init, cache: 'no-store' })
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try {
      const payload = await response.json() as { detail?: string | Array<{ msg: string }> }
      message = Array.isArray(payload.detail) ? payload.detail.map(item => item.msg).join('；') : payload.detail || message
    } catch { /* Keep the HTTP message for non-JSON errors. */ }
    throw new Error(message)
  }
  return response
}

export const overviewWorkbookUrl = `${BASE}/workbook`
export const trainingDataOverviewApi = {
  async workbook(): Promise<Blob> { return (await checkedFetch(overviewWorkbookUrl)).blob() },
  async read(filters: OverviewFilters, signal?: AbortSignal): Promise<TrainingDataOverview> {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(filters)) if (value) query.set(key, value)
    return (await checkedFetch(`${BASE}${query.size ? `?${query}` : ''}`, { signal })).json() as Promise<TrainingDataOverview>
  },
  async retry(releaseId: string): Promise<{ conversion: OverviewConversion }> {
    return (await checkedFetch(`${BASE}/releases/${encodeURIComponent(releaseId)}/retry`, { method: 'POST' })).json() as Promise<{ conversion: OverviewConversion }>
  },
}
