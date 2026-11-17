import { computed, onBeforeUnmount, ref, shallowRef } from 'vue'
import type { TaskGenerationJob, TaskGenerationResult } from '@/types'
import { generationHistory, generationResults, type GenerationResultPage } from '@/generationReadApi'
import { cachedSummary, summaryKey, type SummaryPage, type SummaryQuery } from '@/utils/summaryCache'

export function useGenerationHistory(kind: TaskGenerationJob['kind']) {
  const apiScope = encodeURIComponent((import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, ''))
  const storageKey = `adf.generation-history:${apiScope}:${kind}`
  let saved: SummaryQuery = {}
  try { saved = JSON.parse(sessionStorage.getItem(storageKey) || '{}') } catch { /* optional display preferences */ }
  const query = ref<SummaryQuery>({ kind, offset: 0, limit: 10, search: '', status: '', ...saved })
  const snapshot = shallowRef(cachedSummary<SummaryPage<TaskGenerationJob>>(summaryKey('/api/task-generation/jobs', query.value))?.value)
  const total = computed(() => snapshot.value?.filtered_total ?? 0)
  const page = computed(() => Math.floor(Number(query.value.offset) / Number(query.value.limit)) + 1)
  let disposed = false, sequence = 0, failures = 0
  async function read() {
    const ticket = ++sequence
    try {
      const value = await generationHistory(query.value)
      if (!disposed && ticket === sequence) { snapshot.value = value; failures = 0 }
    } catch (error) {
      if (!disposed && ticket === sequence) failures = Math.min(failures + 1, 5)
      throw error
    }
    return (snapshot.value?.items || []).map(job => Object.assign({ errors: [], warnings: [], error: null, current_item: null, started_at: null, completed_at: null }, job))
  }
  function update(patch: SummaryQuery) {
    sequence++
    query.value = { ...query.value, ...patch, kind }
    try { sessionStorage.setItem(storageKey, JSON.stringify(query.value)) } catch { /* optional display preferences */ }
  }
  let timer: ReturnType<typeof setTimeout> | undefined
  let refresh: (() => Promise<unknown>) | undefined
  let hasActive = () => false
  function schedule() {
    if (timer) clearTimeout(timer)
    const interval = Math.min(120000, (hasActive() ? 5000 : 30000) * 2 ** failures)
    if (!disposed && refresh && (typeof document === 'undefined' || !document.hidden)) timer = setTimeout(poll, interval)
  }
  function poll() { if (refresh) void refresh().catch(() => { /* read keeps the last snapshot and the caller exposes the error */ }).finally(schedule) }
  function visibility() { if (document.hidden) { if (timer) clearTimeout(timer) } else poll() }
  function startPolling(load: () => Promise<unknown>, active: () => boolean) {
    refresh = load; hasActive = active; schedule()
    if (typeof document !== 'undefined') document.addEventListener('visibilitychange', visibility)
  }
  onBeforeUnmount(() => { disposed = true; sequence++; if (timer) clearTimeout(timer); if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', visibility) })
  return { query, snapshot, total, page, read, update, startPolling }
}

export function useGenerationResultPage(showDeleted = false) {
  const query = ref<SummaryQuery>({ offset: 0, limit: 10, show_deleted: showDeleted })
  const snapshot = shallowRef<GenerationResultPage>()
  let selected = '', sequence = 0, disposed = false
  async function read(jobId: string) {
    if (selected !== jobId) { selected = jobId; query.value = { offset: 0, limit: 10, show_deleted: showDeleted }; snapshot.value = undefined }
    const ticket = ++sequence
    const value = await generationResults(jobId, query.value)
    if (!disposed && ticket === sequence && selected === jobId) snapshot.value = value
    return { results: snapshot.value?.results || [], errors: [] }
  }
  function update(patch: SummaryQuery) { sequence++; query.value = { ...query.value, ...patch } }
  function metadata(rows: TaskGenerationResult[]) {
    const value = snapshot.value
    if (!value) return undefined
    const counts = (values: TaskGenerationResult[]) => ({
      all: values.length, active: values.filter(row => !row.deleted).length,
      main: values.filter(row => !row.deleted && row.pre_dependency !== 'pre_node').length,
      prerequisites: values.filter(row => !row.deleted && row.pre_dependency === 'pre_node').length,
      strong: values.filter(row => !row.deleted && row.pre_dependency === 'strong').length,
    })
    const before = counts(value.results), after = counts(rows), updated = { ...value.counts }
    for (const key of Object.keys(updated) as (keyof typeof updated)[]) updated[key] += after[key] - before[key]
    return { ...value, counts: updated }
  }
  onBeforeUnmount(() => { disposed = true; sequence++ })
  return { query, snapshot, read, update, metadata }
}
