import type { BuildJob, CorrectionBatch, CorrectionCotJob, CorrectionCotResponse, CorrectionExport, CorrectionGroup, CorrectionGroupSummary, CorrectionRecommendation, CorrectionSession, DatasetRelease, DatasetReleaseCandidate, DatasetUploadJob, KnowledgeBaseSummary, QualityJob, RunQualitySummary, TaskGenerationExport, TaskGenerationJob, TaskGenerationResult, TaskGenerationTree, TaskGenerationSelection, TaskGenerationTreeNode, TaskQualityResult, TaskSummary, TrajectoryRecord, TrajectorySummary, TrajectoryTreeNode, TreeRun } from './types'

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
      const payload = (await response.json()) as { detail?: string | Array<{ msg: string }> }
      detail = Array.isArray(payload.detail) ? payload.detail.map(item => item.msg).join('；') : payload.detail || detail
    } catch {
      // Keep the HTTP fallback message.
    }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

export const api = {
  async taskGenerationKnowledgeBases(): Promise<KnowledgeBaseSummary[]> {
    return (await request<{ knowledge_bases: KnowledgeBaseSummary[] }>('/api/task-generation/knowledge-bases')).knowledge_bases
  },
  async taskGenerationTree(): Promise<TaskGenerationTree> {
    return request('/api/task-generation/tree')
  },
  async saveTaskGenerationTree(scenes: TaskGenerationTreeNode[], baseVersion: string): Promise<TaskGenerationTree> {
    return request('/api/task-generation/tree', { method: 'PUT', body: JSON.stringify({ scenes, base_version: baseVersion }) })
  },
  async replaceTaskGenerationKnowledgeBase(kind: KnowledgeBaseSummary['kind'], file: File, baseVersion?: string): Promise<KnowledgeBaseSummary> {
    const form = new FormData()
    form.append('file', file)
    if (baseVersion) form.append('base_version', baseVersion)
    return (await request<{ knowledge_base: KnowledgeBaseSummary }>(`/api/task-generation/knowledge-bases/${encodeURIComponent(kind)}`, { method: 'PUT', body: form })).knowledge_base
  },
  async createTaskGeneration(selections: TaskGenerationSelection[], generateN: number, version: string): Promise<TaskGenerationJob> {
    return request('/api/task-generation/jobs', { method: 'POST', body: JSON.stringify({ selections, generate_n: generateN, version }) })
  },
  async createAugmentation(file: File, generateN: number): Promise<TaskGenerationJob> {
    const form = new FormData()
    form.append('file', file)
    form.append('generate_n', String(generateN))
    return request('/api/task-generation/augmentation-jobs', { method: 'POST', body: form })
  },
  async taskGenerationJobs(): Promise<TaskGenerationJob[]> {
    return (await request<{ jobs: TaskGenerationJob[] }>('/api/task-generation/jobs')).jobs
  },
  taskGenerationJob(jobId: string): Promise<TaskGenerationJob> {
    return request(`/api/task-generation/jobs/${encodeURIComponent(jobId)}`)
  },
  async taskGenerationResults(jobId: string): Promise<{ results: TaskGenerationResult[]; errors: TaskGenerationJob['errors'] }> {
    return request(`/api/task-generation/jobs/${encodeURIComponent(jobId)}/results`)
  },
  async patchTaskGenerationResult(jobId: string, resultId: string, patch: { task?: string; deleted?: boolean }): Promise<TaskGenerationResult> {
    return (await request<{ result: TaskGenerationResult }>(`/api/task-generation/jobs/${encodeURIComponent(jobId)}/results/${encodeURIComponent(resultId)}`, { method: 'PATCH', body: JSON.stringify(patch) })).result
  },
  taskGenerationExport(jobId: string): Promise<TaskGenerationExport> {
    return request(`/api/task-generation/jobs/${encodeURIComponent(jobId)}/export`, { method: 'POST' })
  },
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
  correctionSessionCot(sessionId: string): Promise<CorrectionCotResponse> {
    return request(`/api/correction/sessions/${encodeURIComponent(sessionId)}/cot`)
  },
  async createCorrectionCotJob(sessionId: string, groupIds?: string[], rowIds?: number[], options?: { generateBBox?: boolean; forceOverwrite?: boolean }): Promise<CorrectionCotJob> {
    return request('/api/correction/cot-jobs', {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId, group_ids: groupIds && groupIds.length ? groupIds : undefined, row_ids: rowIds && rowIds.length ? rowIds : undefined, generate_bbox: options?.generateBBox ?? false, force_overwrite: options?.forceOverwrite ?? false }),
    })
  },
  updateActionBBox(taskId: string, trajectoryId: string, step: number, excelRow: number, bbox: [number, number, number, number], action?: Record<string, unknown>): Promise<{ actions_box: string }> {
    return request(`/api/tasks/${encodeURIComponent(taskId)}/trajectories/${encodeURIComponent(trajectoryId)}/steps/${step}/bbox`, {
      method: 'PATCH',
      body: JSON.stringify({ excel_row: excelRow, bbox, action }),
    })
  },
  correctionCotJob(jobId: string): Promise<CorrectionCotJob> {
    return request(`/api/correction/cot-jobs/${encodeURIComponent(jobId)}`)
  },
  async correctionCotJobs(): Promise<CorrectionCotJob[]> {
    return (await request<{ jobs: CorrectionCotJob[] }>('/api/correction/cot-jobs')).jobs
  },
  correctionGroups(sessionId: string): Promise<CorrectionGroupSummary[]> {
    return request<{ groups: CorrectionGroupSummary[] }>(`/api/correction/sessions/${encodeURIComponent(sessionId)}/tasks`).then((result) => result.groups)
  },
  correctionGroup(sessionId: string, groupId: string): Promise<CorrectionGroup> {
    return request<{ group: CorrectionGroup }>(`/api/correction/sessions/${encodeURIComponent(sessionId)}/tasks/${encodeURIComponent(groupId)}`).then((result) => result.group)
  },
  async patchCorrectionRow(sessionId: string, excelRow: number, patch: { sop?: string; actions?: string; actions_box?: string; summary?: string; thought?: string; deleted?: boolean }): Promise<{ group: CorrectionGroupSummary; row: CorrectionGroup['rows'][number] }> {
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
  correctionDatasetExport(sessionId: string): Promise<CorrectionExport> {
    return request(`/api/correction/sessions/${encodeURIComponent(sessionId)}/dataset-export`, { method: 'POST' })
  },
  async datasetReleaseCandidates(): Promise<DatasetReleaseCandidate[]> {
    return (await request<{ candidates: DatasetReleaseCandidate[] }>('/api/dataset-releases/candidates')).candidates
  },
  async datasetReleases(): Promise<DatasetRelease[]> {
    return (await request<{ releases: DatasetRelease[] }>('/api/dataset-releases')).releases
  },
  async createDatasetRelease(name: string, sessionIds: string[]): Promise<DatasetRelease> {
    return (await request<{ release: DatasetRelease }>('/api/dataset-releases', {
      method: 'POST',
      body: JSON.stringify({ name, session_ids: sessionIds }),
    })).release
  },
  async datasetRelease(releaseId: string): Promise<DatasetRelease> {
    return (await request<{ release: DatasetRelease }>(`/api/dataset-releases/${encodeURIComponent(releaseId)}`)).release
  },
  async uploadDatasetRelease(releaseId: string): Promise<DatasetUploadJob> {
    return (await request<{ job: DatasetUploadJob }>(`/api/dataset-releases/${encodeURIComponent(releaseId)}/upload`, { method: 'POST' })).job
  },
  async datasetUploadJob(jobId: string): Promise<DatasetUploadJob> {
    return (await request<{ job: DatasetUploadJob }>(`/api/dataset-upload-jobs/${encodeURIComponent(jobId)}`)).job
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

export function taskGenerationDownloadUrl(jobId: string, filename: string): string {
  return `${API_BASE}/api/task-generation/jobs/${encodeURIComponent(jobId)}/exports/${encodeURIComponent(filename)}`
}

export function sceneTreeDownloadUrl(): string {
  return `${API_BASE}/api/task-generation/tree/export`
}

export function datasetReleaseExcelUrl(releaseId: string, index: number): string {
  return `${API_BASE}/api/dataset-releases/${encodeURIComponent(releaseId)}/excels/${index}`
}
