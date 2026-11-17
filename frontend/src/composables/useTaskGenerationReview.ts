import { computed, ref } from 'vue'
import type { api as applicationApi } from '@/api'
import type { TaskGenerationJob, TaskGenerationResult } from '@/types'
import { isGenerationActive } from '@/utils/taskGeneration'

type ReviewApi = Pick<typeof applicationApi, 'taskGenerationJobs' | 'taskGenerationJob' | 'taskGenerationResults' | 'patchTaskGenerationResult' | 'taskGenerationExport'>
interface Feedback {
  error: (message: string) => void
  decide: () => Promise<'save' | 'discard' | 'cancel'>
  confirmDelete: (row: TaskGenerationResult) => Promise<boolean>
}

export function useTaskGenerationReview(api: ReviewApi, feedback: Feedback) {
  const jobs = ref<TaskGenerationJob[]>([])
  const selectedJob = ref<TaskGenerationJob | null>(null)
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
  let disposed = false
  let epoch = 0
  let listRequest = 0
  let failures = 0
  let timer: ReturnType<typeof setTimeout> | undefined
  let deciding = false

  function clearEdit() { editingId.value = null; editingText.value = ''; originalText.value = '' }
  async function saveEdit(): Promise<boolean> {
    const id = editingId.value, jobId = selectedJob.value?.job_id, ticket = epoch
    if (busy.value) return false
    if (!id || !jobId) return true
    if (!editingText.value.trim()) { feedback.error('任务文本不能为空'); return false }
    busy.value = true
    try {
      const updated = await api.patchTaskGenerationResult(jobId, id, { task: editingText.value })
      if (disposed || ticket !== epoch) return false
      results.value = results.value.map(row => row.result_id === updated.result_id ? updated : row)
      clearEdit()
      return true
    } catch (error) { if (!disposed) feedback.error((error as Error).message); return false }
    finally { busy.value = false }
  }
  async function protect(): Promise<boolean> {
    if (busy.value || deciding) return false
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
  async function cancelEdit() { if (await protect()) clearEdit() }
  async function startEdit(row: TaskGenerationResult) {
    if (row.deleted || editingId.value === row.result_id || !(await protect())) return
    if (!results.value.some(value => value.result_id === row.result_id)) return
    editingId.value = row.result_id; editingText.value = row.task; originalText.value = row.task
  }

  async function loadSelected(job: TaskGenerationJob) {
    const ticket = ++epoch
    selectedJob.value = job; results.value = []; errors.value = job.errors || []; detailError.value = ''; loading.value = true
    clearEdit()
    try {
      const detail = await api.taskGenerationJob(job.job_id)
      if (disposed || ticket !== epoch) return
      selectedJob.value = detail
      errors.value = detail.errors || []
      if (!isGenerationActive(detail)) {
        const payload = await api.taskGenerationResults(job.job_id)
        if (disposed || ticket !== epoch) return
        results.value = payload.results; errors.value = detail.errors?.length ? detail.errors : payload.errors
      }
    } catch (error) { if (!disposed && ticket === epoch) detailError.value = (error as Error).message }
    finally { if (!disposed && ticket === epoch) loading.value = false }
  }
  async function selectJob(job: TaskGenerationJob, retry = false) {
    if (!retry && job.job_id === selectedJob.value?.job_id) return true
    if (!(await protect())) return false
    await loadSelected(job)
    schedule()
    return true
  }
  function schedule() {
    if (timer) clearTimeout(timer)
    const active = selectedJob.value && isGenerationActive(selectedJob.value) || jobs.value.some(isGenerationActive)
    const interval = Math.min(120000, (active ? 5000 : 30000) * 2 ** failures)
    if (!disposed && (typeof document === 'undefined' || !document.hidden)) timer = setTimeout(() => void refreshJobs(), interval)
  }
  async function refreshJobs() {
    if (disposed) return
    refreshing.value = true
    const ticket = epoch, request = ++listRequest
    try {
      const list = await api.taskGenerationJobs()
      if (disposed || request !== listRequest) return
      jobs.value = list.filter(job => job.kind === 'task_generation')
      listError.value = ''
      if (ticket !== epoch) return
      const current = selectedJob.value
      if (current) {
        const updated = jobs.value.find(job => job.job_id === current.job_id) || (isGenerationActive(current) ? await api.taskGenerationJob(current.job_id) : null)
        if (disposed || ticket !== epoch || request !== listRequest) return
        if (updated) {
          // List responses deliberately omit execution_units; retain the saved detail snapshot.
          selectedJob.value = { ...current, ...updated, errors: current.errors, warnings: current.warnings, error: current.error, execution_units: current.execution_units }
          if (isGenerationActive(updated)) errors.value = updated.errors || []
          if (isGenerationActive(current) && !isGenerationActive(updated)) await loadSelected(selectedJob.value)
        }
      }
      if (!disposed && request === listRequest) failures = 0
    } catch (error) { if (!disposed && request === listRequest) { failures = Math.min(failures + 1, 5); listError.value = (error as Error).message } }
    finally { if (request === listRequest) { refreshing.value = false; schedule() } }
  }
  async function acceptJob(job: TaskGenerationJob) {
    jobs.value = [job, ...jobs.value.filter(value => value.job_id !== job.job_id)]
    await loadSelected(job)
    schedule()
  }
  async function toggleDeleted(row: TaskGenerationResult) {
    const sourceJob = selectedJob.value?.job_id, sourceEpoch = epoch
    if (!(await protect()) || !(await feedback.confirmDelete(row)) || busy.value) return
    if (disposed || sourceEpoch !== epoch || sourceJob !== selectedJob.value?.job_id) return
    const jobId = selectedJob.value?.job_id, ticket = epoch
    if (!jobId || !results.value.some(value => value.result_id === row.result_id)) return
    busy.value = true
    try {
      const updated = await api.patchTaskGenerationResult(jobId, row.result_id, { deleted: !row.deleted })
      if (disposed || ticket !== epoch) return
      results.value = results.value.map(value => value.result_id === updated.result_id || (updated.dependency_group_id && value.dependency_group_id === updated.dependency_group_id) ? { ...value, deleted: updated.deleted } : value)
      clearEdit()
    } catch (error) { if (!disposed) feedback.error((error as Error).message) }
    finally { busy.value = false }
  }
  async function exportResults() {
    if (!(await protect())) return null
    const jobId = selectedJob.value?.job_id
    if (!jobId) return null
    busy.value = true
    try { return { jobId, exported: await api.taskGenerationExport(jobId) } }
    catch (error) { if (!disposed) feedback.error((error as Error).message); return null }
    finally { busy.value = false }
  }
  async function runExternalAction<T>(action: (jobId: string) => Promise<T>): Promise<T | null> {
    const jobId = selectedJob.value?.job_id, ticket = epoch
    if (!jobId || !(await protect()) || disposed || ticket !== epoch || selectedJob.value?.job_id !== jobId || busy.value) return null
    busy.value = true
    try {
      const result = await action(jobId)
      return !disposed && ticket === epoch && selectedJob.value?.job_id === jobId ? result : null
    } finally { if (!disposed && ticket === epoch) busy.value = false }
  }
  async function restoreJob(id: string) {
    if (!id || selectedJob.value) return
    const ticket = epoch
    try { const job = await api.taskGenerationJob(id); if (!disposed && ticket === epoch && job.kind === 'task_generation') await selectJob(job) }
    catch (error) { if (!disposed && ticket === epoch) detailError.value = (error as Error).message }
  }
  async function reloadResults() {
    const id = selectedJob.value?.job_id, ticket = epoch
    if (!id || !(await protect())) return
    loading.value = true
    try { const payload = await api.taskGenerationResults(id); if (!disposed && ticket === epoch) results.value = payload.results }
    catch (error) { if (!disposed && ticket === epoch) detailError.value = (error as Error).message }
    finally { if (!disposed && ticket === epoch) loading.value = false }
  }
  function visibilityChanged() { if (document.hidden) { if (timer) clearTimeout(timer) } else void refreshJobs() }
  if (typeof document !== 'undefined') document.addEventListener('visibilitychange', visibilityChanged)
  function dispose() { disposed = true; epoch++; listRequest++; if (timer) clearTimeout(timer); if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', visibilityChanged) }
  return { jobs, selectedJob, results, errors, listError, detailError, loading, refreshing, busy, editingId, editingText, dirty, protect, cancelEdit, startEdit, saveEdit, selectJob, refreshJobs, acceptJob, toggleDeleted, exportResults, runExternalAction, restoreJob, reloadResults, dispose }
}
