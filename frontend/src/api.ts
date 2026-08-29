import type { BuildJob, CorrectionBatch, CorrectionExport, CorrectionGroup, CorrectionGroupSummary, CorrectionRecommendation, CorrectionSession, QualityJob, RunQualitySummary, TaskQualityResult, TaskSummary, TrajectoryRecord, TrajectorySummary, TrajectoryTreeNode, TreeRun } from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (!(init?.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
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
  async qualityJobs(): Promise<QualityJob[]> {
    return (await request<{ jobs: QualityJob[] }>('/api/quality-jobs')).jobs
  },
  runQuality(runId: string): Promise<RunQualitySummary> {
    return request(`/api/tree-runs/${encodeURIComponent(runId)}/quality`)
  },
  taskQuality(runId: string, taskId: string): Promise<TaskQualityResult> {
    return request(`/api/tree-runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/quality`)
  },
  correctionBatches(): Promise<{ default_tree_run_id: string | null; batches: CorrectionBatch[] }> {
    return request('/api/correction/batches')
  },
  correctionRecommendation(treeRunId?: string): Promise<CorrectionRecommendation> {
    const query = treeRunId ? `?tree_run_id=${encodeURIComponent(treeRunId)}` : ''
    return request(`/api/correction/recommendation${query}`)
  },
  async correctionSessions(): Promise<CorrectionSession[]> {
    return (await request<{ sessions: CorrectionSession[] }>('/api/correction/sessions')).sessions
  },
  async createCorrectionSession(treeRunId: string): Promise<CorrectionSession> {
    return (await request<{ session: CorrectionSession }>('/api/correction/sessions', {
      method: 'POST',
      body: JSON.stringify({ tree_run_id: treeRunId }),
    })).session
  },
  correctionSession(sessionId: string): Promise<CorrectionSession> {
    return request<{ session: CorrectionSession }>(`/api/correction/sessions/${encodeURIComponent(sessionId)}`).then((result) => result.session)
  },
  correctionGroups(sessionId: string): Promise<CorrectionGroupSummary[]> {
    return request<{ groups: CorrectionGroupSummary[] }>(`/api/correction/sessions/${encodeURIComponent(sessionId)}/tasks`).then((result) => result.groups)
  },
  correctionGroup(sessionId: string, groupId: string): Promise<CorrectionGroup> {
    return request<{ group: CorrectionGroup }>(`/api/correction/sessions/${encodeURIComponent(sessionId)}/tasks/${encodeURIComponent(groupId)}`).then((result) => result.group)
  },
  async patchCorrectionRow(sessionId: string, excelRow: number, patch: { sop?: string; actions?: string; deleted?: boolean }): Promise<{ group: CorrectionGroupSummary; row: CorrectionGroup['rows'][number] }> {
    return request(`/api/correction/sessions/${encodeURIComponent(sessionId)}/rows/${excelRow}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    })
  },
  async patchCorrectionExport(sessionId: string, groupId: string, exportState: boolean): Promise<CorrectionGroupSummary> {
    return (await request<{ group: CorrectionGroupSummary }>(`/api/correction/sessions/${encodeURIComponent(sessionId)}/tasks/${encodeURIComponent(groupId)}/export`, {
      method: 'PATCH',
      body: JSON.stringify({ export: exportState }),
    })).group
  },
  correctionExport(sessionId: string): Promise<CorrectionExport> {
    return request(`/api/correction/sessions/${encodeURIComponent(sessionId)}/export`, { method: 'POST' })
  },
}

export function imageUrl(relativePath: string): string {
  const normalized = relativePath.replaceAll('\\', '/').replace(/^\/+/, '')
  return `${API_BASE}/api/assets/${normalized.split('/').map(encodeURIComponent).join('/')}`
}

export function correctionAssetUrl(sessionId: string, relativePath: string): string {
  const normalized = relativePath.replaceAll('\\', '/').replace(/^\/+/, '')
  return `${API_BASE}/api/correction/sessions/${encodeURIComponent(sessionId)}/assets/${normalized.split('/').map(encodeURIComponent).join('/')}`
}

export function correctionDownloadUrl(sessionId: string, filename: string): string {
  return `${API_BASE}/api/correction/sessions/${encodeURIComponent(sessionId)}/exports/${encodeURIComponent(filename)}`
}
