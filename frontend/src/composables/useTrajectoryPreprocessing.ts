import { activeBatchItems, publishedBatch } from '@/utils/batchLifecycle'
import { computed, reactive, ref, watch } from 'vue'
import { useBusinessBatchChoices } from './useBusinessBatchChoices'
import { preprocessingTaskPage, jobPage, type TreeCounts } from '@/businessPageApi'
import type { api } from '@/api'
import type { BuildJob, CollectionSourceRun, CollectionSourceTask, PreprocessingBatch, PreprocessingJob, TaskSummary, TrajectoryRecord, TrajectoryScope, TrajectoryStep, TrajectorySummary } from '@/types'

type Client = Pick<typeof api, 'preprocessingBatches' | 'collectionSourceRuns' | 'collectionSourceTasks' | 'createPreprocessing' | 'preprocessingJob' | 'retryPreprocessing' | 'tasks' | 'trajectories' | 'trajectory' | 'updateBBox' | 'createBuild' | 'build' | 'builds'>
const active = (job: { status: string } | null) => Boolean(job && ['queued', 'running'].includes(job.status))
const clear = (value: Record<string, unknown>) => Object.keys(value).forEach(key => delete value[key])

export function useTrajectoryPreprocessing(client: Client, protect: () => Promise<boolean> = async () => true, options: {
  readOnly?: () => boolean
  historyOnly?: () => boolean
  boundJobs?: () => { preprocessing: string[]; tree: string[] } | null
  summaryPages?: boolean
} = {}) {
  const retiredIds = new Set<string>()
  const batches = ref<PreprocessingBatch[]>([])
  const batchId = ref(''), annotationVersion = ref(''), error = ref('')
  const choices = options.summaryPages ? useBusinessBatchChoices('preprocessing', () => !options.historyOnly?.()) : null
  const taskPage = ref(1), taskSize = ref(10), taskSearch = ref(''), taskTotal = ref(0), allTaskIds = ref<string[]>([])
  let taskRequest = 0
  const loadingBatches = ref(false), loading = ref(false), submitting = ref(false), protecting = ref(false), saving = ref(false)
  const tasks = ref<TaskSummary[]>([]), selectedTasks = ref<string[]>([]), expandedTasks = ref<string[]>([])
  const unknownTreeCounts = (): TreeCounts => ({ completed: null, stale: null, pending: null })
  const batchTreeCounts = ref<TreeCounts>(unknownTreeCounts())
  const treeCounts = computed<TreeCounts>(() => {
    if (options.summaryPages) return batchTreeCounts.value
    const completed = tasks.value.filter(task => task.tree_status === 'succeeded').length
    const stale = tasks.value.filter(task => ['stale', 'invalidated'].includes(task.tree_status || '')).length
    return { completed, stale, pending: tasks.value.length - completed - stale }
  })
  const expandedTrajectories = reactive<Record<string, string>>({})
  const taskData = reactive<Record<string, TrajectorySummary[]>>({})
  const trajectoryData = reactive<Record<string, TrajectoryRecord>>({})
  const loadingTasks = reactive<Record<string, boolean>>({}), loadingTrajectories = reactive<Record<string, boolean>>({})
  const preprocessingJob = ref<PreprocessingJob | null>(null), buildJob = ref<BuildJob | null>(null)
  const sourceTasks = ref<CollectionSourceTask[]>([]), sourceRuns = ref<CollectionSourceRun[]>([]), loadingSources = ref(false), sourceError = ref('')
  const selectedBatch = computed(() => batches.value.find(item => item.batch_id === batchId.value) || null)
  const scope = computed<TrajectoryScope | null>(() => batchId.value && annotationVersion.value
    ? { batch_id: batchId.value, annotation_version: annotationVersion.value } : null)
  const busy = computed(() => submitting.value || protecting.value || saving.value)
  const processing = computed(() => active(preprocessingJob.value))
  const building = computed(() => active(buildJob.value))
  const newerVersion = computed(() => Boolean(selectedBatch.value?.annotation_version && selectedBatch.value.annotation_version !== annotationVersion.value))
  let epoch = 0, listRequest = 0, selectionRequest = 0, sourceRequest = 0, disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let pollFailures = 0
  let adoption: { id: string; promise: Promise<boolean> } | null = null
  let automaticVersionAttempt = ''
  const current = (token: number) => !disposed && token === epoch
  const key = (taskId: string, trajectoryId: string) => `${taskId}/${trajectoryId}`
  function stopPoll() { if (timer) clearTimeout(timer); timer = undefined }
  function schedulePoll() {
    stopPoll()
    if (!disposed && (processing.value || building.value)) timer = setTimeout(() => { if (typeof document === 'undefined' || !document.hidden) void poll(); else schedulePoll() }, Math.min(120000, 5000 * 2 ** Math.min(pollFailures, 5)))
  }
  function resetData() {
    tasks.value = []; selectedTasks.value = []; expandedTasks.value = []
    batchTreeCounts.value = unknownTreeCounts()
    clear(expandedTrajectories); clear(taskData); clear(trajectoryData); clear(loadingTasks); clear(loadingTrajectories)
    loading.value = false
  }
  async function guard() {
    if (busy.value || disposed) return false
    protecting.value = true
    try { return await protect() && !disposed }
    finally { protecting.value = false }
  }
  async function loadTasks() {
    const selected = scope.value, token = epoch
    const request = ++taskRequest
    if (!selected) return
    loading.value = true
    try {
      if (options.summaryPages) {
        const result = await preprocessingTaskPage({ ...selected, offset: (taskPage.value - 1) * taskSize.value, limit: taskSize.value, q: taskSearch.value })
        if (current(token) && request === taskRequest) {
          batchTreeCounts.value = result.counts_complete && result.tree_counts_scope === 'batch' && result.tree_counts
            ? result.tree_counts : unknownTreeCounts()
          if (result.counts_complete || result.items.length || !tasks.value.length) { tasks.value = result.items; taskTotal.value = result.filtered_total ?? 0; allTaskIds.value = result.task_ids || [] }
        }
      } else { const result = await client.tasks({ ...selected }); if (current(token)) tasks.value = result }
    }
    catch (cause) { if (current(token) && request === taskRequest) error.value = (cause as Error).message }
    finally { if (current(token) && request === taskRequest) loading.value = false }
  }
  async function loadBuild() {
    const token = epoch, id = batchId.value
    try {
      const bound = options.boundJobs?.()
      if (bound) {
        const jobs = await Promise.all(bound.tree.map(jobId => client.build(jobId)))
        if (current(token) && JSON.stringify(options.boundJobs?.()) === JSON.stringify(bound)) {
          if (jobs.some(job => job.batch_id !== id)) throw new Error('Pipeline 建树作业与当前批次不一致')
          buildJob.value = jobs.find(job => active(job)) || jobs.at(-1) || null
        }
        schedulePoll()
        return
      }
      const jobs = options.summaryPages ? (await jobPage<BuildJob>('tree-builds', { batch_id: id, limit: 10 })).items : await client.builds()
      if (current(token)) buildJob.value = jobs.filter(item => item.batch_id === id)
        .sort((a, b) => b.created_at.localeCompare(a.created_at))[0] || null
    } catch (cause) { if (current(token)) error.value = (cause as Error).message }
    if (current(token)) schedulePoll()
  }
  async function bindJobs() {
    const bound = options.boundJobs?.(), token = epoch, id = batchId.value
    if (!bound || !id) return
    try {
      const jobs = await Promise.all(bound.preprocessing.map(jobId => client.preprocessingJob(jobId)))
      if (!current(token) || JSON.stringify(options.boundJobs?.()) !== JSON.stringify(bound)) return
      if (jobs.some(job => job.batch_id !== id)) throw new Error('Pipeline 预处理作业与当前批次不一致')
      preprocessingJob.value = jobs.find(job => active(job)) || jobs.at(-1) || null
      await loadBuild()
    } catch (cause) { if (current(token)) error.value = (cause as Error).message }
    if (current(token)) schedulePoll()
  }
  async function loadSources() {
    const token = epoch, request = ++sourceRequest, id = batchId.value
    if (!id || selectedBatch.value?.kind === 'existing_trajectories') return
    loadingSources.value = true; sourceError.value = ''
    const tasksRequest = selectedBatch.value?.kind === 'rollout_import'
      ? client.collectionSourceTasks(id, 'rollout_import') : client.collectionSourceTasks(id)
    const results = await Promise.allSettled([tasksRequest, client.collectionSourceRuns(id)])
    if (!current(token) || request !== sourceRequest) return
    const [tasksResult, runsResult] = results
    if (tasksResult.status === 'fulfilled') sourceTasks.value = tasksResult.value
    if (runsResult.status === 'fulfilled') {
      sourceRuns.value = runsResult.value
      if (tasksResult.status === 'rejected') sourceTasks.value = [...new Map(runsResult.value.flatMap(run => Object.values(run.batch_tasks)).map(task => [task.task_id, task])).values()]
    }
    sourceError.value = results.filter(result => result.status === 'rejected').map(result => (result.reason as Error).message).join('；')
    loadingSources.value = false
  }
  async function selectBatch(id: string) {
    const attempt = ++selectionRequest
    if (publishedBatch(id) || retiredIds.has(id)) { retireBatches([id]); return false }
    if (id === batchId.value) return true
    if (!(await guard()) || attempt !== selectionRequest) return false
    if (choices && id) { await choices.exact(id); if (attempt !== selectionRequest) return false; batches.value = choices.rows.value }
    const batch = batches.value.find(item => item.batch_id === id)
    if (id && !batch) { error.value = '未找到对应批次，请刷新批次列表'; return false }
    stopPoll(); epoch++; resetData(); error.value = ''
    const token = epoch
    sourceRequest++; sourceTasks.value = []; sourceRuns.value = []; sourceError.value = ''; loadingSources.value = false
    batchId.value = id; annotationVersion.value = batch?.annotation_version || ''
    taskPage.value = 1; taskSearch.value = ''; taskTotal.value = 0; allTaskIds.value = []
    if (choices) choices.selected.value = id
    preprocessingJob.value = options.boundJobs?.() ? null : batch?.latest_job || null; buildJob.value = null
    if (id) await Promise.all([loadTasks(), options.boundJobs?.() ? bindJobs() : loadBuild(), options.summaryPages ? Promise.resolve() : loadSources()])
    if (!current(token)) return false
    schedulePoll()
    return true
  }
  async function loadBatches(preferredId?: string) {
    if (options.historyOnly?.()) { await selectBatch(''); return }
    const request = ++listRequest, token = epoch
    loadingBatches.value = true
    try {
      if (choices) await choices.refresh()
      if (choices && batchId.value) await choices.exact(batchId.value, true)
      const result = choices ? choices.rows.value : await client.preprocessingBatches()
      if (!current(token) || request !== listRequest) return
      batches.value = activeBatchItems(result).filter(batch => !retiredIds.has(batch.batch_id))
      if (preferredId !== undefined) await selectBatch(preferredId)
      else if (!batchId.value && choices?.selected.value) await selectBatch(choices.selected.value)
      else if (batchId.value && selectedBatch.value) {
        if (options.boundJobs?.()) await bindJobs()
        else preprocessingJob.value = selectedBatch.value.latest_job
        if (!options.summaryPages) void loadSources()
        if (!annotationVersion.value && selectedBatch.value.annotation_version) {
          annotationVersion.value = selectedBatch.value.annotation_version
          await loadTasks()
        } else if (newerVersion.value && !busy.value) {
          await adoptLatestVersion()
        }
      }
      schedulePoll()
    } catch (cause) { if (current(token) && request === listRequest) error.value = (cause as Error).message }
    finally { if (!disposed && request === listRequest) loadingBatches.value = false }
  }
  async function poll() {
    const token = epoch, pre = preprocessingJob.value, build = buildJob.value
    try {
      await Promise.all([
        active(pre) && pre ? client.preprocessingJob(pre.job_id).then(async value => {
          if (!current(token) || value.batch_id !== batchId.value || preprocessingJob.value?.job_id !== pre.job_id || (options.boundJobs?.() && !options.boundJobs()!.preprocessing.includes(value.job_id))) return
          preprocessingJob.value = value
          if (selectedBatch.value) { selectedBatch.value.latest_job = value; selectedBatch.value.preprocessing_status = value.status }
          if (!active(value)) await loadBatches()
        }) : Promise.resolve(),
        active(build) && build ? client.build(build.job_id).then(value => {
          if (current(token) && value.batch_id === batchId.value && buildJob.value?.job_id === build.job_id && (!options.boundJobs?.() || options.boundJobs()!.tree.includes(value.job_id))) { buildJob.value = value; if (!active(value)) void loadTasks() }
        }) : Promise.resolve(),
      ])
      if (current(token)) pollFailures = 0
    } catch (cause) { if (current(token)) { pollFailures++; error.value = `状态刷新失败：${(cause as Error).message}；将继续重试` } }
    finally { if (current(token)) schedulePoll() }
  }
  async function start(retry = false) {
    if (options.readOnly?.()) return false
    const batch = selectedBatch.value, oldJob = preprocessingJob.value
    if (!batch || processing.value || building.value || (!retry && !batch.can_start)) return false
    if (retry && (!oldJob || !['failed', 'interrupted'].includes(oldJob.status))) return false
    if (!(await guard()) || batch.batch_id !== batchId.value) return false
    const token = epoch
    submitting.value = true; error.value = ''
    try {
      const value = retry ? await client.retryPreprocessing(oldJob!.job_id) : await client.createPreprocessing(batch.batch_id)
      if (!current(token) || value.batch_id !== batchId.value) return false
      epoch++; resetData(); annotationVersion.value = ''; preprocessingJob.value = value
      if (selectedBatch.value) { selectedBatch.value.latest_job = value; selectedBatch.value.preprocessing_status = value.status }
      if (!active(value)) await loadBatches()
      schedulePoll(); return true
    } catch (cause) { if (current(token)) error.value = (cause as Error).message; return false }
    finally { if (!disposed) submitting.value = false }
  }
  function adoptLatestVersion(): Promise<boolean> {
    const id = batchId.value
    if (adoption?.id === id) return adoption.promise
    const promise = (async () => {
      // A late/cached selector response is only a hint. Re-read the selected
      // batch before adopting a version or asking to leave a manual editor.
      if (choices && id) {
        const authoritative = await choices.exact(id, true)
        if (disposed || id !== batchId.value || !authoritative) return false
        batches.value = activeBatchItems(choices.rows.value).filter(batch => !retiredIds.has(batch.batch_id))
      }
      if (!selectedBatch.value?.annotation_version) return false
      if (selectedBatch.value.annotation_version === annotationVersion.value) return true
      if (!(await guard()) || id !== batchId.value) return false
      const version = selectedBatch.value?.annotation_version
      if (!version) return false
      epoch++; resetData(); annotationVersion.value = version
      await loadTasks(); return true
    })()
    adoption = { id, promise }
    void promise.finally(() => { if (adoption?.promise === promise) adoption = null }).catch(() => {})
    return promise
  }
  async function loadTask(taskId: string) {
    const selected = scope.value, token = epoch
    if (!selected || taskData[taskId] || loadingTasks[taskId]) return
    loadingTasks[taskId] = true
    try { const result = await client.trajectories(taskId, { ...selected }); if (current(token)) taskData[taskId] = result.trajectories }
    catch (cause) { if (current(token)) error.value = (cause as Error).message }
    finally { if (current(token)) loadingTasks[taskId] = false }
  }
  async function expandTasks(ids: string[]) {
    const id = batchId.value
    if (!(await guard()) || id !== batchId.value) return false
    expandedTasks.value = ids
    await Promise.all(ids.map(loadTask)); return true
  }
  async function expandTrajectory(taskId: string, trajectoryId: string) {
    const batch = batchId.value
    if (!scope.value || !(await guard()) || batch !== batchId.value || !scope.value) return false
    const token = epoch, selected = scope.value
    clear(expandedTrajectories); expandedTrajectories[taskId] = trajectoryId
    if (!trajectoryId) return true
    const id = key(taskId, trajectoryId)
    if (trajectoryData[id] || loadingTrajectories[id]) return true
    loadingTrajectories[id] = true
    try { const value = await client.trajectory(taskId, trajectoryId, { ...selected }); if (current(token)) trajectoryData[id] = value }
    catch (cause) { if (current(token)) error.value = (cause as Error).message }
    finally { if (current(token)) loadingTrajectories[id] = false }
    return true
  }
  async function saveBBox(taskId: string, trajectoryId: string, step: TrajectoryStep, bbox: [number, number, number, number]) {
    if (options.readOnly?.()) throw new Error('该批次由 Pipeline 管理，请在 Pipeline 中操作')
    const selected = scope.value, token = epoch
    if (!selected || saving.value) throw new Error('当前批次尚未就绪，或正在保存')
    saving.value = true
    try {
      const result = await client.updateBBox(taskId, trajectoryId, step.step, step.excel_row, bbox, { ...selected })
      if (!current(token)) throw new Error('已切换批次，请重新加载当前结果')
      if (!result.annotation_version) throw new Error('保存结果缺少更新标识，请刷新后重试')
      const cached = trajectoryData[key(taskId, trajectoryId)]
      step.actions_box = result.actions_box; epoch++
      const currentTask = taskData[taskId]
      clear(trajectoryData); clear(taskData); clear(loadingTasks); clear(loadingTrajectories)
      if (currentTask) taskData[taskId] = currentTask
      if (cached) trajectoryData[key(taskId, trajectoryId)] = cached
      annotationVersion.value = result.annotation_version
      if (selectedBatch.value) selectedBatch.value.annotation_version = result.annotation_version
      void loadBatches(); schedulePoll()
    } finally { saving.value = false }
  }
  async function submitBuild() {
    if (options.readOnly?.()) return false
    const id = batchId.value
    if (!selectedTasks.value.length || !scope.value || processing.value || building.value || !(await guard()) || id !== batchId.value || !scope.value) return false
    const token = epoch
    const selected = { ...scope.value }
    submitting.value = true; error.value = ''
    try {
      const value = await client.createBuild([...selectedTasks.value], selected)
      if (!current(token)) return false
      buildJob.value = value; schedulePoll(); return true
    } catch (cause) { if (current(token)) error.value = (cause as Error).message; return false }
    finally { if (!disposed) submitting.value = false }
  }
  function retireBatches(ids: string[], forceReset = false) {
    ids.forEach(id => retiredIds.add(id))
    batches.value = batches.value.filter(batch => !ids.includes(batch.batch_id))
    if (!forceReset && !ids.includes(batchId.value)) return
    ++listRequest; loadingBatches.value = false
    stopPoll(); ++epoch; ++selectionRequest; ++sourceRequest; resetData()
    batchId.value = ''; annotationVersion.value = ''; error.value = ''
    preprocessingJob.value = null; buildJob.value = null
    sourceTasks.value = []; sourceRuns.value = []; loadingSources.value = false; sourceError.value = ''
  }
  function dispose() { disposed = true; epoch++; listRequest++; stopPoll() }
  if (choices) watch(choices.rows, rows => { batches.value = activeBatchItems(rows).filter(batch => !retiredIds.has(batch.batch_id)) })
  if (choices) watch([() => selectedBatch.value?.annotation_version, busy], ([version, isBusy]) => {
    if (disposed || isBusy || !batchId.value || !annotationVersion.value || !version || version === annotationVersion.value) return
    const identity = `${batchId.value}:${version}`
    if (automaticVersionAttempt === identity) return
    automaticVersionAttempt = identity
    const id = batchId.value
    void adoptLatestVersion().then(adopted => {
      if (!adopted && !disposed && id === batchId.value && newerVersion.value) error.value = '标框版本已更新；保留当前编辑，完成或取消编辑后刷新以读取新版本。'
    }).catch(cause => { if (!disposed && id === batchId.value) error.value = (cause as Error).message })
  }, { flush: 'post' })
  async function changeTaskPage(page: number, size = taskSize.value) {
    if (!await guard()) return
    taskPage.value = page; taskSize.value = size; await loadTasks()
  }
  async function searchTasks(value: string) { if (!await guard()) return; taskSearch.value = value; taskPage.value = 1; await loadTasks() }
  return { batches, batchId, selectedBatch, annotationVersion, scope, error, busy, processing, building, newerVersion,
    choices, taskPage, taskSize, taskSearch, taskTotal, allTaskIds, treeCounts, changeTaskPage, searchTasks, loadSources,
    sourceTasks, sourceRuns, loadingSources, sourceError,
    loadingBatches, loading, submitting, tasks, selectedTasks, expandedTasks, expandedTrajectories,
    taskData, trajectoryData, loadingTasks, loadingTrajectories, preprocessingJob, buildJob,
    loadBatches, selectBatch, start, adoptLatestVersion, expandTasks, expandTrajectory, saveBBox, submitBuild, poll, bindJobs, retireBatches, dispose, key, guard }
}
