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
        results.value = payload.results; errors.value = payload.errors
      }
    } catch (error) { if (!disposed && ticket === epoch) detailError.value = (error as Error).message }
    finally { if (!disposed && ticket === epoch) loading.value = false }
  }
  async function selectJob(job: TaskGenerationJob, retry = false) {
    if (!retry && job.job_id === selectedJob.value?.job_id) return true
    if (!(await protect())) return false
    await loadSelected(job)
    return true
  }
  function schedule() {
    if (timer) clearTimeout(timer)
    if (!disposed && jobs.value.some(isGenerationActive)) timer = setTimeout(() => void refreshJobs(), 1500)
  }
  async function refreshJobs() {
    if (refreshing.value || disposed) return
    refreshing.value = true
    const ticket = epoch
    try {
      const list = await api.taskGenerationJobs()
      if (disposed) return
      jobs.value = list.filter(job => job.kind === 'task_generation')
      listError.value = ''
      if (ticket !== epoch) return
      const current = selectedJob.value
      if (!current) {
        const first = jobs.value.find(isGenerationActive) || jobs.value[0]
        if (first) await loadSelected(first)
      } else {
        const updated = jobs.value.find(job => job.job_id === current.job_id)
        if (updated) {
          // List responses deliberately omit execution_units; retain the saved detail snapshot.
          selectedJob.value = { ...current, ...updated, execution_units: current.execution_units }
          if (isGenerationActive(updated)) errors.value = updated.errors || []
          if (isGenerationActive(current) && !isGenerationActive(updated)) await loadSelected(selectedJob.value)
        }
      }
    } catch (error) { if (!disposed) listError.value = (error as Error).message }
    finally { refreshing.value = false; schedule() }
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
  function dispose() { disposed = true; epoch++; if (timer) clearTimeout(timer) }
  return { jobs, selectedJob, results, errors, listError, detailError, loading, refreshing, busy, editingId, editingText, dirty, protect, cancelEdit, startEdit, saveEdit, selectJob, refreshJobs, acceptJob, toggleDeleted, exportResults, dispose }
}
