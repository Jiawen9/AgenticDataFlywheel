import { computed, ref, shallowRef } from 'vue'
import type { api as applicationApi } from '@/api'
import type { TaskGenerationJob, TaskGenerationResult } from '@/types'

type PreviewApi = Pick<typeof applicationApi, 'createAugmentation' | 'augmentationPreview' | 'startAugmentation' | 'taskGenerationJobs' | 'taskGenerationJob' | 'taskGenerationResults' | 'patchTaskGenerationResult' | 'taskGenerationExport'>
type Preview = Awaited<ReturnType<PreviewApi['augmentationPreview']>>
interface Feedback {
  error: (message: string) => void
  decide: () => Promise<'save' | 'discard' | 'cancel'>
  confirmDelete: (row: TaskGenerationResult) => Promise<boolean>
}

const running = (job: TaskGenerationJob | null) => Boolean(job && ['queued', 'running'].includes(job.status))
const completed = (job: TaskGenerationJob) => ['succeeded', 'partial', 'failed', 'interrupted'].includes(job.status)
const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error)

/** Keep each job's snapshot and progress together, even when requests finish out of order. */
export function useAugmentationPreview(api: PreviewApi, feedback: Feedback) {
  const jobs = ref<TaskGenerationJob[]>([])
  const selectedJob = ref<TaskGenerationJob | null>(null)
  const preview = shallowRef<Preview | null>(null)
  const results = ref<TaskGenerationResult[]>([])
  const errors = ref<TaskGenerationJob['errors']>([])
  const listError = ref('')
  const detailError = ref('')
  const loading = ref(false)
  const refreshing = ref(false)
  const busy = ref(false)
  const editingId = ref<string | null>(null)
  const editingText = ref('')
  const originalText = ref('')
  const dirty = computed(() => editingId.value !== null && editingText.value !== originalText.value)
  const active = computed(() => running(selectedJob.value))
  let disposed = false
  let epoch = 0
  let requestId = 0
  let listRequest = 0
  let deciding = false
  let timer: ReturnType<typeof setTimeout> | undefined

  function stopTimer() { if (timer) clearTimeout(timer); timer = undefined }
  function clearEdit() { editingId.value = null; editingText.value = ''; originalText.value = '' }
  function valid(jobId: string, ticket: number) { return !disposed && ticket === epoch && selectedJob.value?.job_id === jobId }
  function updateJob(job: TaskGenerationJob) {
    selectedJob.value = job
    const index = jobs.value.findIndex(item => item.job_id === job.job_id)
    if (index < 0) jobs.value = [job, ...jobs.value]
    else jobs.value = jobs.value.map(item => item.job_id === job.job_id ? job : item)
  }
  function schedule() {
    stopTimer()
    if (!disposed && active.value) timer = setTimeout(() => void refresh(), 1200)
  }

  async function refresh() {
    const jobId = selectedJob.value?.job_id, ticket = epoch, request = ++requestId
    if (!jobId || disposed) return
    stopTimer()
    refreshing.value = true
    const failures: string[] = []
    try {
      const job = await api.taskGenerationJob(jobId)
      if (!valid(jobId, ticket) || request !== requestId) return
      updateJob(job)
      errors.value = job.errors || []
      // Read progress after the job state: a terminal/confirmation response must
      // never be paired with a preview captured before the last seed was saved.
      const [previewReply, resultReply] = await Promise.allSettled([
        api.augmentationPreview(jobId, !preview.value?.tree),
        completed(job) ? api.taskGenerationResults(jobId) : Promise.resolve(null),
      ])
      if (!valid(jobId, ticket) || request !== requestId) return
      if (previewReply.status === 'fulfilled') {
        const payload = previewReply.value
        preview.value = { ...payload, tree: payload.available ? payload.tree || preview.value?.tree : undefined }
      } else failures.push(`场景预览读取失败：${errorMessage(previewReply.reason)}`)
      if (resultReply.status === 'fulfilled') {
        if (resultReply.value) { results.value = resultReply.value.results; errors.value = resultReply.value.errors }
      } else {
        failures.push(`变体结果读取失败：${errorMessage(resultReply.reason)}`)
      }
    } catch (error) { failures.push(errorMessage(error)) }
    if (!valid(jobId, ticket) || request !== requestId) return
    detailError.value = failures.join('；')
    refreshing.value = false
    loading.value = false
    schedule()
  }

  async function saveEdit(): Promise<boolean> {
    const jobId = selectedJob.value?.job_id, id = editingId.value, ticket = epoch
    if (busy.value) return false
    if (!jobId || !id) return true
    if (!dirty.value) { clearEdit(); return true }
    if (!editingText.value.trim()) { feedback.error('任务文本不能为空'); return false }
    busy.value = true
    try {
      const updated = await api.patchTaskGenerationResult(jobId, id, { task: editingText.value })
      if (!valid(jobId, ticket)) return false
      results.value = results.value.map(row => row.result_id === id ? updated : row)
      clearEdit()
      return true
    } catch (error) { if (valid(jobId, ticket)) feedback.error(errorMessage(error)); return false }
    finally { if (!disposed && ticket === epoch) busy.value = false }
  }
  async function protect(): Promise<boolean> {
    if (busy.value || deciding || disposed) return false
    if (!dirty.value) return true
    deciding = true
    try {
      const decision = await feedback.decide()
      if (disposed || decision === 'cancel') return false
      if (decision === 'save') return await saveEdit()
      clearEdit()
      return true
    } finally { deciding = false }
  }
  async function selectJob(job: TaskGenerationJob) {
    if (!(await protect())) return false
    stopTimer()
    epoch++; requestId++
    selectedJob.value = job
    preview.value = null; results.value = []; errors.value = []; detailError.value = ''
    clearEdit(); loading.value = true
    await refresh()
    return true
  }
  async function loadJobs(preferredId?: string) {
    const request = ++listRequest, ticket = epoch
    try {
      const payload = await api.taskGenerationJobs()
      if (disposed || request !== listRequest) return
      jobs.value = payload.filter(item => item.kind === 'augmentation')
      listError.value = ''
      if (ticket !== epoch || selectedJob.value) return
      const next = jobs.value.find(item => item.job_id === preferredId)
        || jobs.value.find(item => running(item) || item.status === 'awaiting_confirmation') || jobs.value[0]
      if (next) await selectJob(next)
    } catch (error) { if (!disposed && request === listRequest) listError.value = errorMessage(error) }
  }
  async function create(file: File, generateN: number) {
    if (!(await protect())) return false
    busy.value = true
    try {
      const job = await api.createAugmentation(file, generateN, false)
      if (disposed) return false
      busy.value = false
      jobs.value = [job, ...jobs.value.filter(item => item.job_id !== job.job_id)]
      await selectJob(job)
      return true
    } catch (error) { if (!disposed) feedback.error(errorMessage(error)); return false }
    finally { if (!disposed) busy.value = false }
  }
  async function start() {
    const jobId = selectedJob.value?.job_id, ticket = epoch
    if (!jobId || selectedJob.value?.status !== 'awaiting_confirmation' || !preview.value?.stats.eligible || !(await protect())) return false
    busy.value = true
    stopTimer(); requestId++
    try {
      const job = await api.startAugmentation(jobId)
      if (!valid(jobId, ticket)) return false
      updateJob(job)
      await refresh()
      return true
    } catch (error) { if (valid(jobId, ticket)) feedback.error(errorMessage(error)); return false }
    finally { if (!disposed && ticket === epoch) { busy.value = false; schedule() } }
  }
  async function startEdit(row: TaskGenerationResult) {
    if (row.deleted || active.value || !(await protect())) return false
    editingId.value = row.result_id; editingText.value = row.task; originalText.value = row.task
    return true
  }
  async function cancelEdit() { if (await protect()) clearEdit() }
  async function toggleDeleted(row: TaskGenerationResult) {
    const jobId = selectedJob.value?.job_id, ticket = epoch
    if (!jobId || active.value || !(await protect()) || !(await feedback.confirmDelete(row)) || busy.value || !valid(jobId, ticket)) return
    busy.value = true
    try {
      const updated = await api.patchTaskGenerationResult(jobId, row.result_id, { deleted: !row.deleted })
      if (valid(jobId, ticket)) results.value = results.value.map(value => value.result_id === updated.result_id ? updated : value)
    } catch (error) { if (valid(jobId, ticket)) feedback.error(errorMessage(error)) }
    finally { if (!disposed && ticket === epoch) busy.value = false }
  }
  async function exportResults() {
    if (active.value || !(await protect())) return null
    const jobId = selectedJob.value?.job_id, ticket = epoch
    if (!jobId) return null
    busy.value = true
    try {
      const exported = await api.taskGenerationExport(jobId)
      return valid(jobId, ticket) ? { jobId, exported } : null
    } catch (error) { if (valid(jobId, ticket)) feedback.error(errorMessage(error)); return null }
    finally { if (!disposed && ticket === epoch) busy.value = false }
  }
  async function runExternalAction<T>(action: (jobId: string) => Promise<T>): Promise<T | null> {
    const jobId = selectedJob.value?.job_id, ticket = epoch
    if (!jobId || !(await protect()) || !valid(jobId, ticket) || busy.value) return null
    busy.value = true
    try {
      const result = await action(jobId)
      return valid(jobId, ticket) ? result : null
    } finally { if (!disposed && ticket === epoch) busy.value = false }
  }
  function dispose() { disposed = true; epoch++; requestId++; listRequest++; stopTimer() }
  return { jobs, selectedJob, preview, results, errors, listError, detailError, loading, refreshing, busy, active, editingId, editingText, dirty, protect, selectJob, loadJobs, refresh, create, start, startEdit, saveEdit, cancelEdit, toggleDeleted, exportResults, runExternalAction, dispose }
}
