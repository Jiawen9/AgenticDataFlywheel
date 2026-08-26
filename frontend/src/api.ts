import type { BuildJob, QualityJob, RunQualitySummary, TaskQualityResult, TaskSummary, TrajectoryRecord, TrajectorySummary, TrajectoryTreeNode, TreeRun } from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const payload = (await response.json()) as { detail?: string }
      detail = payload.detail || detail
    } catch {
      // Keep the HTTP fallback message.
    }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

export const api = {
  async tasks(): Promise<TaskSummary[]> {
    return (await request<{ tasks: TaskSummary[] }>('/api/tasks')).tasks
  },
  async trajectories(taskId: string): Promise<{ task: TaskSummary; trajectories: TrajectorySummary[] }> {
    return request(`/api/tasks/${encodeURIComponent(taskId)}/trajectories`)
  },
  async trajectory(taskId: string, trajectoryId: string): Promise<TrajectoryRecord> {
    return (await request<{ trajectory: TrajectoryRecord }>(
      `/api/tasks/${encodeURIComponent(taskId)}/trajectories/${encodeURIComponent(trajectoryId)}`,
    )).trajectory
  },
  async updateBBox(
    taskId: string,
    trajectoryId: string,
    step: number,
    excelRow: number,
    bbox: [number, number, number, number],
  ): Promise<string> {
    const result = await request<{ actions_box: string }>(
      `/api/tasks/${encodeURIComponent(taskId)}/trajectories/${encodeURIComponent(trajectoryId)}/steps/${step}/bbox`,
      {
        method: 'PATCH',
        body: JSON.stringify({ excel_row: excelRow, bbox }),
      },
    )
    return result.actions_box
  },
  async createBuild(taskIds: string[]): Promise<BuildJob> {
    return request('/api/tree-builds', {
      method: 'POST',
      body: JSON.stringify({ task_ids: taskIds }),
    })
  },
  build(jobId: string): Promise<BuildJob> {
    return request(`/api/tree-builds/${encodeURIComponent(jobId)}`)
  },
  async runs(): Promise<TreeRun[]> {
    return (await request<{ runs: TreeRun[] }>('/api/tree-runs')).runs
  },
  tree(runId: string, taskId: string): Promise<TrajectoryTreeNode> {
    return request(
      `/api/tree-runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/tree`,
    )
  },
  createQuality(runId: string, taskIds: string[]): Promise<QualityJob> {
    return request('/api/quality-jobs', {
      method: 'POST',
      body: JSON.stringify({ run_id: runId, task_ids: taskIds }),
    })
  },
  qualityJob(jobId: string): Promise<QualityJob> {
    return request(`/api/quality-jobs/${encodeURIComponent(jobId)}`)
  },
  runQuality(runId: string): Promise<RunQualitySummary> {
    return request(`/api/tree-runs/${encodeURIComponent(runId)}/quality`)
  },
  taskQuality(runId: string, taskId: string): Promise<TaskQualityResult> {
    return request(`/api/tree-runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/quality`)
  },
}

export function imageUrl(relativePath: string): string {
  const normalized = relativePath.replaceAll('\\', '/').replace(/^\/+/, '')
  return `${API_BASE}/api/assets/${normalized.split('/').map(encodeURIComponent).join('/')}`
}
