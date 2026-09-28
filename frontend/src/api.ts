import { activeBatchItems, notifyBatchesPublished } from '@/utils/batchLifecycle'
import type { Pipeline, CreatePipeline, PipelineAction } from './types/pipeline'
import type { BuildJob, CorrectionBatch, CorrectionCotJob, CorrectionCotResponse, CorrectionExport, CorrectionGroup, CorrectionGroupSummary, CorrectionRecommendation, CorrectionSession, DatasetRelease, DatasetReleaseCandidate, DatasetUploadJob, KnowledgeBaseSummary, QualityJob, RunQualitySummary, TaskGenerationExport, TaskGenerationJob, TaskGenerationResult, TaskGenerationTree, TaskGenerationSelection, TaskGenerationTreeNode, TaskQualityResult, TaskSummary, TrajectoryRecord, TrajectorySummary, TrajectoryTreeNode, TreeRun } from './types'

import type { AugmentationPreview, CollectionSourceRun, CollectionSourceTask, DatasetUploadCapabilities, PreprocessingBatch, PreprocessingJob, StageArtifact, TrajectoryScope } from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly detail?: { code?: string; batch_id?: string; release_id?: string | null; published_at?: string | null }) { super(message); this.name = 'ApiError' }
}

function scopeQuery(scope?: TrajectoryScope): string {
  return scope ? '?' + new URLSearchParams(Object.entries(scope).filter((entry): entry is [string, string] => typeof entry[1] === 'string')).toString() : ''
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (!(init?.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
  })
  if (!response.ok) {
    let message = response.status + ' ' + response.statusText
    let detail: { code?: string; message?: string; batch_id?: string; release_id?: string | null; published_at?: string | null } | undefined
    try {
      const payload = await response.json()
      if (typeof payload.detail === 'string') message = payload.detail
      else if (Array.isArray(payload.detail)) message = payload.detail.map((item: { msg: string }) => item.msg).join('；')
      else if (payload.detail && typeof payload.detail === 'object') { detail = payload.detail; message = detail?.message || message }
    } catch { /* Keep the HTTP fallback message. */ }
    if (detail?.code === 'batch_published' && detail.batch_id) notifyBatchesPublished({ batch_ids: [detail.batch_id], release_id: detail.release_id ?? null, published_at: detail.published_at, source_path: path.split('?')[0] })
    throw new ApiError(message, response.status, detail)
  }
  const result = await response.json()
  if (result?.error_code === 'batch_published' && result.detail?.batch_id) notifyBatchesPublished({ batch_ids: [result.detail.batch_id], release_id: result.detail.release_id ?? null, published_at: result.detail.published_at, source_path: path.split('?')[0] })
  return result as T
}

export const api = {
  async listPipelines(options: { batch_id?: string; active_only?: boolean } = {}, signal?: AbortSignal): Promise<Pipeline[]> {
    const query = new URLSearchParams()
    if (options.batch_id) query.set('batch_id', options.batch_id)
    if (options.active_only !== undefined) query.set('active_only', String(options.active_only))
    return (await request<{ pipelines: Pipeline[] }>('/api/pipelines' + (query.size ? '?' + query : ''), { cache: 'no-store', signal })).pipelines
  },
  async pipeline(id: string, signal?: AbortSignal): Promise<Pipeline> {
    return (await request<{ pipeline: Pipeline }>('/api/pipelines/' + encodeURIComponent(id), { cache: 'no-store', signal })).pipeline
  },
  async createPipeline(value: CreatePipeline): Promise<Pipeline> {
    return (await request<{ pipeline: Pipeline }>('/api/pipelines', { method: 'POST', body: JSON.stringify(value) })).pipeline
  },
  async controlPipeline(id: string, action: PipelineAction, expectedRevision: number, sessionRevision?: number): Promise<Pipeline> {
    return (await request<{ pipeline: Pipeline }>(`/api/pipelines/${encodeURIComponent(id)}/${action}`, { method: 'POST', body: JSON.stringify({ expected_revision: expectedRevision, ...(sessionRevision !== undefined ? { session_revision: sessionRevision } : {}) }) })).pipeline
  },
  batchLifecycle(batchId: string): Promise<{ batch_id: string; status: 'active' | 'published'; published_at: string | null; release_id: string | null }> {
    return request('/api/data-batches/' + encodeURIComponent(batchId) + '/lifecycle', { cache: 'no-store' })
  },
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
  async createAugmentation(file: File, generateN: number, autoStart = true): Promise<TaskGenerationJob> {
    const form = new FormData()
    form.append('file', file)
    form.append('generate_n', String(generateN))
    form.append('auto_start', String(autoStart))
    return request('/api/task-generation/augmentation-jobs', { method: 'POST', body: form })
  },
  augmentationPreview(jobId: string, includeTree = true): Promise<AugmentationPreview> {
    return request(`/api/task-generation/jobs/${encodeURIComponent(jobId)}/augmentation-preview?include_tree=${includeTree}`)
  },
  startAugmentation(jobId: string): Promise<TaskGenerationJob> {
    return request(`/api/task-generation/jobs/${encodeURIComponent(jobId)}/start-augmentation`, { method: 'POST' })
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
  async preprocessingBatches(): Promise<PreprocessingBatch[]> {
    return activeBatchItems((await request<{ batches: PreprocessingBatch[] }>('/api/trajectory-preprocessing/batches', { cache: 'no-store' })).batches)
  },
  async collectionSourceRuns(batchId: string): Promise<CollectionSourceRun[]> {
    return (await request<{ runs: CollectionSourceRun[] }>('/api/phone-factory/collection-runs?batch_id=' + encodeURIComponent(batchId))).runs
  },
  async collectionSourceTasks(batchId: string, sourceKind?: 'rollout_import'): Promise<CollectionSourceTask[]> {
    const path = sourceKind === 'rollout_import' ? '/api/rollout-imports/batches/' : '/api/task-generation/collection-batches/'
    return (await request<{ snapshot: { tasks: CollectionSourceTask[] } }>(path + encodeURIComponent(batchId))).snapshot.tasks
  },
  createPreprocessing(batchId: string): Promise<PreprocessingJob> {
    return request('/api/trajectory-preprocessing/jobs', { method: 'POST', body: JSON.stringify({ batch_id: batchId }) })
  },
  preprocessingJob(jobId: string): Promise<PreprocessingJob> {
    return request(`/api/trajectory-preprocessing/jobs/${encodeURIComponent(jobId)}`)
  },
  retryPreprocessing(jobId: string): Promise<PreprocessingJob> {
    return request(`/api/trajectory-preprocessing/jobs/${encodeURIComponent(jobId)}/retry`, { method: 'POST' })
  },
  async tasks(scope?: TrajectoryScope): Promise<TaskSummary[]> {
    return (await request<{ tasks: TaskSummary[] }>('/api/tasks' + scopeQuery(scope))).tasks
  },
  async trajectories(taskId: string, scope?: TrajectoryScope): Promise<{ task: TaskSummary; trajectories: TrajectorySummary[] }> {
    return request(`/api/tasks/${encodeURIComponent(taskId)}/trajectories` + scopeQuery(scope))
  },
  async trajectory(taskId: string, trajectoryId: string, scope?: TrajectoryScope): Promise<TrajectoryRecord> {
    return (await request<{ trajectory: TrajectoryRecord }>(
      `/api/tasks/${encodeURIComponent(taskId)}/trajectories/${encodeURIComponent(trajectoryId)}` + scopeQuery(scope),
    )).trajectory
  },
  async updateBBox(
    taskId: string,
    trajectoryId: string,
    step: number,
    excelRow: number,
    bbox: [number, number, number, number],
    scope: TrajectoryScope,
  ): Promise<{ actions_box: string; annotation_version: string }> {
    return request<{ actions_box: string; annotation_version: string }>(
      `/api/tasks/${encodeURIComponent(taskId)}/trajectories/${encodeURIComponent(trajectoryId)}/steps/${step}/bbox`,
      {
        method: 'PATCH',
        body: JSON.stringify({ excel_row: excelRow, bbox, ...scope }),
      },
    )
  },
  async createBuild(taskIds: string[], scope?: TrajectoryScope): Promise<BuildJob> {
    return request('/api/tree-builds', {
      method: 'POST',
      body: JSON.stringify({ task_ids: taskIds, batch_id: scope?.batch_id }),
    })
  },
  build(jobId: string): Promise<BuildJob> {
    return request(`/api/tree-builds/${encodeURIComponent(jobId)}`)
  },
  async runs(): Promise<TreeRun[]> {
    return (await request<{ runs: TreeRun[] }>('/api/tree-runs')).runs
  },
  treeRun(runId: string): Promise<TreeRun> {
    return request(`/api/tree-runs/${encodeURIComponent(runId)}`)
  },
  batchTree(batchId: string): Promise<TreeRun> {
    return request(`/api/data-batches/${encodeURIComponent(batchId)}/tree`)
  },
  tree(batchId: string, taskId: string): Promise<TrajectoryTreeNode> {
    return request(
      `/api/data-batches/${encodeURIComponent(batchId)}/tasks/${encodeURIComponent(taskId)}/tree`,
    )
  },
  createQuality(batchId: string, taskIds: string[]): Promise<QualityJob> {
    return request('/api/quality-jobs', {
      method: 'POST',
      body: JSON.stringify({ batch_id: batchId, task_ids: taskIds }),
    })
  },
  qualityJob(jobId: string): Promise<QualityJob> {
    return request(`/api/quality-jobs/${encodeURIComponent(jobId)}`)
  },
  async qualityJobs(): Promise<QualityJob[]> {
    return (await request<{ jobs: QualityJob[] }>('/api/quality-jobs')).jobs
  },
  runQuality(batchId: string): Promise<RunQualitySummary> {
    return request(`/api/data-batches/${encodeURIComponent(batchId)}/quality`)
  },
  taskQuality(batchId: string, taskId: string): Promise<TaskQualityResult> {
    return request(`/api/data-batches/${encodeURIComponent(batchId)}/tasks/${encodeURIComponent(taskId)}/quality`)
  },
  async correctionBatches(): Promise<{ default_batch_id: string | null; default_tree_run_id?: string | null; batches: CorrectionBatch[] }> {
    const result = await request<{ default_batch_id: string | null; batches: CorrectionBatch[] }>('/api/correction/batches', { cache: 'no-store' })
    return { ...result, batches: activeBatchItems(result.batches) }
  },
  correctionRecommendation(batchId?: string): Promise<CorrectionRecommendation> {
    const query = batchId ? `?batch_id=${encodeURIComponent(batchId)}` : ''
    return request(`/api/correction/recommendation${query}`)
  },
  async correctionSessions(): Promise<CorrectionSession[]> {
    return activeBatchItems((await request<{ sessions: CorrectionSession[] }>('/api/correction/sessions', { cache: 'no-store' })).sessions)
  },
  async createCorrectionSession(batchId: string): Promise<CorrectionSession> {
    return (await request<{ session: CorrectionSession }>('/api/correction/sessions', {
      method: 'POST',
      body: JSON.stringify({ batch_id: batchId }),
    })).session
  },
  correctionSession(sessionId: string): Promise<CorrectionSession> {
    return request<{ session: CorrectionSession }>(`/api/correction/sessions/${encodeURIComponent(sessionId)}`).then((result) => result.session)
  },
  correctionSessionCot(sessionId: string): Promise<CorrectionCotResponse> {
    return request(`/api/correction/sessions/${encodeURIComponent(sessionId)}/cot`)
  },
  async createCorrectionCotJob(sessionId: string, groupIds?: string[], rowIds?: number[], options?: { generateBBox?: boolean; forceOverwrite?: boolean; expectedRevision?: number }): Promise<CorrectionCotJob> {
    return request('/api/correction/cot-jobs', {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId, group_ids: groupIds && groupIds.length ? groupIds : undefined, row_ids: rowIds && rowIds.length ? rowIds : undefined, generate_bbox: options?.generateBBox ?? false, force_overwrite: options?.forceOverwrite ?? false, expected_revision: options?.expectedRevision }),
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
  async patchCorrectionRow(sessionId: string, excelRow: number, patch: { sop?: string; actions?: string; actions_box?: string; summary?: string; thought?: string; deleted?: boolean }, expectedRevision?: number): Promise<{ group: CorrectionGroupSummary; row: CorrectionGroup['rows'][number]; storage_revision?: number }> {
    return request(`/api/correction/sessions/${encodeURIComponent(sessionId)}/rows/${excelRow}`, {
      method: 'PATCH',
      body: JSON.stringify({ ...patch, expected_revision: expectedRevision }),
    })
  },
  async patchCorrectionExport(sessionId: string, groupId: string, exportState: boolean, expectedRevision?: number): Promise<CorrectionGroupSummary> {
    const result = await request<{ group: CorrectionGroupSummary; storage_revision?: number }>(`/api/correction/sessions/${encodeURIComponent(sessionId)}/tasks/${encodeURIComponent(groupId)}/export`, {
      method: 'PATCH',
      body: JSON.stringify({ export: exportState, expected_revision: expectedRevision }),
    })
    return { ...result.group, storage_revision: result.storage_revision ?? result.group.storage_revision }
  },
  reviewCorrectionGroup(sessionId: string, groupId: string, decision: 'adopt' | 'discard', expectedRevision?: number): Promise<{ session: CorrectionSession; group: CorrectionGroup }> {
    return request(`/api/correction/sessions/${encodeURIComponent(sessionId)}/tasks/${encodeURIComponent(groupId)}/review`, { method: 'PATCH', body: JSON.stringify({ decision, expected_revision: expectedRevision }) })
  },
  correctionExport(sessionId: string, expectedRevision?: number): Promise<CorrectionExport> {
    return request(`/api/correction/sessions/${encodeURIComponent(sessionId)}/export`, { method: 'POST', body: JSON.stringify({ expected_revision: expectedRevision }) })
  },
  correctionDatasetExport(sessionId: string, expectedRevision?: number): Promise<CorrectionExport> {
    return request(`/api/correction/sessions/${encodeURIComponent(sessionId)}/dataset-export`, { method: 'POST', body: JSON.stringify({ expected_revision: expectedRevision }) })
  },
  async datasetReleaseCandidates(): Promise<DatasetReleaseCandidate[]> {
    return activeBatchItems((await request<{ candidates: DatasetReleaseCandidate[] }>('/api/dataset-releases/candidates', { cache: 'no-store' })).candidates)
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
  async datasetUploadCapabilities(): Promise<DatasetUploadCapabilities> {
    return request<DatasetUploadCapabilities>('/api/dataset-upload-capabilities')
  },
  async builds(): Promise<BuildJob[]> {
    return (await request<{ jobs: BuildJob[] }>('/api/tree-builds')).jobs
  },
  async uploadDatasetRelease(releaseId: string, target?: 'internal'): Promise<DatasetUploadJob> {
    return (await request<{ job: DatasetUploadJob }>(`/api/dataset-releases/${encodeURIComponent(releaseId)}/upload`, {
      method: 'POST',
      ...(target ? { body: JSON.stringify({ target }) } : {}),
    })).job
  },
  async datasetUploadJob(jobId: string): Promise<DatasetUploadJob> {
    return (await request<{ job: DatasetUploadJob }>(`/api/dataset-upload-jobs/${encodeURIComponent(jobId)}`)).job
  },
}

export function treeRunScope(run: Pick<TreeRun, 'batch_id' | 'annotation_version'> | undefined): TrajectoryScope | undefined {
  return run?.batch_id ? { batch_id: run.batch_id } : undefined
}

export function imageUrl(relativePath: string, scope?: TrajectoryScope): string {
  const normalized = relativePath.replaceAll('\\', '/').replace(/^\/+/, '')
  return `${API_BASE}/api/assets/${normalized.split('/').map(encodeURIComponent).join('/')}` + scopeQuery(scope)
}

export function stageArtifactDownloadUrl(artifact: StageArtifact, filename: string): string {
  return `${API_BASE}/api/data-batches/${encodeURIComponent(artifact.batch_id)}/artifacts/${encodeURIComponent(artifact.stage)}/${encodeURIComponent(artifact.version)}/files/${encodeURIComponent(filename)}`
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
