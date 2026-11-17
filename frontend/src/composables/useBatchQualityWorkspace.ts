import { activeBatchItems, publishedBatch } from '@/utils/batchLifecycle'
import { computed, ref, watch } from 'vue'
import { useBusinessBatchChoices } from './useBusinessBatchChoices'
import { qualityTaskPage } from '@/businessPageApi'
import { ApiError, type api } from '@/api'
import { resolveBatchSelection } from '@/utils/batchSelection'
import type { CorrectionRecommendation, PreprocessingBatch, QualityTaskSummary, TaskQualityResult, TrajectoryTreeNode, TreeRun, TreeRunTask } from '@/types'

type Client = Pick<typeof api, 'preprocessingBatches' | 'batchTree' | 'runQuality' | 'tree' | 'taskQuality' | 'correctionRecommendation' | 'treeRun'>
export function useBatchQualityWorkspace(client: Client, options: {
  historyOnly?: () => boolean
  waitingForTree?: () => boolean
  waitingForQuality?: () => boolean // Gates recommendations, never reading existing scores.
  summaryPages?: boolean
} = {}) {
  const retiredIds = new Set<string>()
  const batches = ref<PreprocessingBatch[]>([]), batchId = ref('')
  const choices = options.summaryPages ? useBusinessBatchChoices('quality', () => !options.historyOnly?.()) : null
  const taskPage = ref(1), taskSize = ref(10), taskSearch = ref(''), taskTotal = ref(0), readyTaskIds = ref<string[]>([])
  const serverCounts = ref({ completed: 0, pending: 0, stale: 0 })
  let taskRequest = 0
  const currentTree = ref<TreeRun | null>(null), summaries = ref<Record<string, QualityTaskSummary>>({})
  const recommendation = ref<CorrectionRecommendation | null>(null)
  const selectedTaskId = ref(''), tree = ref<TrajectoryTreeNode | null>(null), quality = ref<TaskQualityResult | null>(null)
  const selectedTaskRow = ref<TreeRunTask | null>(null)
  const loadingBatches = ref(false), loadingTree = ref(false), loadingBatch = ref(false), error = ref('')
  let epoch = 0, detailRequest = 0, listRequest = 0, refreshRequest = 0, disposed = false
  let detailBinding: { fingerprint: string; tree: string } | null = null
  function detailIdentity(taskId = selectedTaskId.value) {
    const row = summaries.value[taskId]
    const versions = currentTree.value as (TreeRun & { quality_revision?: string }) | null
    return {
      fingerprint: JSON.stringify([batchId.value, taskId, versions?.revision, versions?.quality_revision, row]),
      tree: JSON.stringify([batchId.value, taskId, versions?.revision]),
    }
  }
  const counts = computed(() => {
    if (options.summaryPages) return serverCounts.value
    const tasks = currentTree.value?.tasks ?? []
    const completed = tasks.filter(task => {
      const summary = summaries.value[task.task_id]
      return summary?.status === 'succeeded' && summary.fusion_complete !== false && summary.result_state !== 'partial'
    }).length
    const stale = tasks.filter(task => ['stale', 'invalidated'].includes(summaries.value[task.task_id]?.status ?? task.tree_status ?? task.status ?? '')).length
    return { completed, stale, pending: tasks.length - completed - stale }
  })
  const readyTasks = computed(() => (currentTree.value?.tasks ?? []).filter(task => !['pending', 'stale', 'invalidated', 'failed', 'running'].includes(task.tree_status ?? task.status ?? 'succeeded')))
  function clearDetail() { ++detailRequest; detailBinding = null; selectedTaskId.value = ''; selectedTaskRow.value = null; tree.value = null; quality.value = null; loadingTree.value = false }
  async function loadSummary() {
    const id = batchId.value, token = epoch, request = ++taskRequest
    if (!id) return
    loadingBatch.value = true
    try {
      const result = await qualityTaskPage(id, { offset: (taskPage.value - 1) * taskSize.value, limit: taskSize.value, q: taskSearch.value, selected_task_id: selectedTaskId.value || undefined })
      if (disposed || epoch !== token || request !== taskRequest) return
      if (!result.counts_complete && !result.items.length && currentTree.value) return
      const previousSelected = summaries.value[selectedTaskId.value]
      currentTree.value = result
      summaries.value = Object.fromEntries(result.items.map(item => [item.task_id, { ...item, status: item.quality_status }]))
      if (result.selected_task) summaries.value[result.selected_task.task_id] = { ...result.selected_task, status: result.selected_task.quality_status }
      else if (previousSelected && result.ready_task_ids?.includes(selectedTaskId.value) && !summaries.value[selectedTaskId.value]) summaries.value[selectedTaskId.value] = previousSelected
      if (result.selected_task) selectedTaskRow.value = result.selected_task
      taskTotal.value = result.filtered_total ?? 0; readyTaskIds.value = result.ready_task_ids || []
      serverCounts.value = result.quality_counts || { completed: 0, pending: 0, stale: 0 }
      // The displayed evidence owns its own version. A list/page response must
      // not silently advance that binding while keeping older results on screen.
      if (detailBinding && detailBinding.fingerprint !== detailIdentity().fingerprint) {
        quality.value = null
        if (detailBinding.tree !== detailIdentity().tree) tree.value = null
      }
    } catch (cause) { if (!disposed && token === epoch && request === taskRequest) error.value = (cause as Error).message }
    finally { if (!disposed && token === epoch && request === taskRequest) loadingBatch.value = false }
  }
  async function selectBatch(id: string) {
    const token = ++epoch
    if (!id && choices) { choices.selected.value = ''; await choices.exact('') }
    batchId.value = id; clearDetail(); currentTree.value = null; summaries.value = {}; recommendation.value = null; error.value = ''
    if (!id || options.historyOnly?.() || publishedBatch(id) || retiredIds.has(id)) { batchId.value = ''; loadingBatch.value = false; return }
    if (choices) { await choices.exact(id); if (disposed || epoch !== token) return; batches.value = choices.rows.value }
    if (batches.value.length && !batches.value.some(batch => batch.batch_id === id)) {
      batchId.value = ''; loadingBatch.value = false; error.value = '指定批次不存在或当前不可用，请重新选择批次'; return
    }
    if (options.waitingForTree?.()) { loadingBatch.value = false; return }
    if (options.summaryPages) { taskPage.value = 1; taskSearch.value = ''; await loadSummary(); return }
    loadingBatch.value = true
    // A failed or still-running batch can already have valid per-task results,
    // including Checklist errors. Completeness only gates downstream selection.
    const results = await Promise.allSettled([client.batchTree(id), client.runQuality(id), options.waitingForQuality?.() ? Promise.resolve(null) : client.correctionRecommendation(id)])
    if (disposed || epoch !== token) return
    const [built, reviewed, suggested] = results
    if (built.status === 'fulfilled') currentTree.value = built.value
    else error.value = (built.reason as Error).message
    if (reviewed.status === 'fulfilled' && reviewed.value) summaries.value = Object.fromEntries(reviewed.value.tasks.map(task => [task.task_id, task]))
    else if (reviewed.status === 'rejected' && !error.value) error.value = (reviewed.reason as Error).message
    if (suggested.status === 'fulfilled' && !options.waitingForQuality?.()) recommendation.value = suggested.value
    loadingBatch.value = false
  }
  async function loadBatches(preferredId?: string, legacyRunId?: string) {
    if (options.historyOnly?.()) { await selectBatch(''); return }
    const request = ++listRequest
    loadingBatches.value = true
    if (legacyRunId || (preferredId !== undefined && preferredId !== batchId.value)) await selectBatch('')
    const token = epoch
    try {
      if (choices) await choices.refresh()
      const result = choices ? choices.rows.value : await client.preprocessingBatches()
      if (disposed || request !== listRequest) return
      batches.value = activeBatchItems(result).filter(batch => !retiredIds.has(batch.batch_id))
      const selected = options.summaryPages
        ? preferredId !== undefined ? preferredId : legacyRunId ? (await client.treeRun(legacyRunId)).batch_id || '' : batchId.value || choices?.selected.value || ''
        : await resolveBatchSelection(batches.value.map(batch => batch.batch_id), { batchId: preferredId, legacyRunId }, client.treeRun)
      if (legacyRunId && !selected) throw new Error('指定建树记录没有有效批次，请重新选择批次')
      if (disposed || request !== listRequest || token !== epoch) return
      if (selected !== batchId.value || !currentTree.value) await selectBatch(selected)
    } catch (cause) { if (!disposed && request === listRequest && token === epoch) error.value = (cause as Error).message }
    finally { if (!disposed && request === listRequest) loadingBatches.value = false }
  }
  async function viewTree(taskId: string) {
    const id = batchId.value, token = epoch, request = ++detailRequest
    detailBinding = null
    selectedTaskId.value = taskId; tree.value = null; quality.value = null; loadingTree.value = true
    selectedTaskRow.value = currentTree.value?.tasks.find(t => t.task_id === taskId) || null
    const requestedIdentity = detailIdentity(taskId)
    try {
      const summary = summaries.value[taskId]
      const readable = summary?.status === 'succeeded' || summary?.results_available || summary?.partial_results_available
      const [built, reviewed] = await Promise.allSettled([client.tree(id, taskId), readable ? client.taskQuality(id, taskId) : Promise.resolve(null)])
      if (disposed || token !== epoch || request !== detailRequest) return
      if (options.summaryPages && requestedIdentity.fingerprint !== detailIdentity(taskId).fingerprint) return
      if (built.status === 'rejected') throw built.reason
      tree.value = built.value
      if (reviewed.status === 'fulfilled') quality.value = reviewed.value
      else if (!(reviewed.reason instanceof ApiError && (
        (reviewed.reason.status === 409 && reviewed.reason.message === '该任务尚无有效质检结果，请先完成质检') ||
        (reviewed.reason.status === 404 && reviewed.reason.message === '该任务还没有质检结果')
      ))) throw reviewed.reason
      detailBinding = requestedIdentity
    } catch (cause) { if (!disposed && token === epoch && request === detailRequest) error.value = (cause as Error).message }
    finally { if (!disposed && token === epoch && request === detailRequest) loadingTree.value = false }
  }
  async function refresh() {
    const id = batchId.value, taskId = selectedTaskId.value, token = epoch
    const request = ++refreshRequest, detail = detailRequest
    if (!id || options.historyOnly?.() || options.waitingForTree?.()) { await selectBatch(id); return }
    if (options.summaryPages) {
      const currentDetail = () => !disposed && token === epoch && request === refreshRequest && detail === detailRequest && selectedTaskId.value === taskId
      await loadSummary()
      if (!currentDetail()) return
      if (taskId && !readyTaskIds.value.includes(taskId)) { clearDetail(); return }
      // Only refresh the opened task when its version/result actually changes.
      if (taskId && detailBinding?.fingerprint !== detailIdentity(taskId).fingerprint && !loadingTree.value) {
        const requestedIdentity = detailIdentity(taskId)
        const summary = summaries.value[taskId]
        const readable = summary?.status === 'succeeded' || summary?.results_available || summary?.partial_results_available
        const [nextTree, nextQuality] = await Promise.allSettled([client.tree(id, taskId), readable ? client.taskQuality(id, taskId) : Promise.resolve(null)])
        if (!currentDetail()) return
        if (nextTree.status === 'fulfilled') tree.value = nextTree.value
        else { tree.value = null; quality.value = null; error.value = (nextTree.reason as Error).message }
        if (nextQuality.status === 'fulfilled') quality.value = nextQuality.value
        else { quality.value = null; error.value = (nextQuality.reason as Error).message }
        if (nextTree.status === 'fulfilled' && nextQuality.status === 'fulfilled') detailBinding = requestedIdentity
      }
      return
    }
    const current = () => !disposed && epoch === token && batchId.value === id && request === refreshRequest
    const currentDetail = () => current() && selectedTaskId.value === taskId && detailRequest === detail
    loadingBatch.value = true
    error.value = ''
    try {
      // Refresh published data in place: unmounting the tree here also loses
      // its viewport, the selected screenshot and expanded evidence.
      const [built, reviewed, suggested] = await Promise.allSettled([
        client.batchTree(id), client.runQuality(id),
        options.waitingForQuality?.() ? Promise.resolve(null) : client.correctionRecommendation(id),
      ])
      if (!current()) return
      if (built.status === 'rejected') throw built.reason
      currentTree.value = built.value
      if (reviewed.status === 'fulfilled' && reviewed.value) summaries.value = Object.fromEntries(reviewed.value.tasks.map(task => [task.task_id, task]))
      else if (reviewed.status === 'rejected') error.value = (reviewed.reason as Error).message
      recommendation.value = suggested.status === 'fulfilled' && !options.waitingForQuality?.() ? suggested.value : null
      if (!taskId || !currentDetail() || loadingTree.value) return
      if (!readyTasks.value.some(task => task.task_id === taskId)) { clearDetail(); return }
      const summary = summaries.value[taskId]
      const readable = summary?.status === 'succeeded' || summary?.results_available || summary?.partial_results_available
      const [nextTree, nextQuality] = await Promise.allSettled([
        client.tree(id, taskId), readable ? client.taskQuality(id, taskId) : Promise.resolve(null),
      ])
      if (!currentDetail()) return
      if (nextTree.status === 'rejected') throw nextTree.reason
      if (nextQuality.status === 'rejected' && !(nextQuality.reason instanceof ApiError && (
        (nextQuality.reason.status === 409 && nextQuality.reason.message === '该任务尚无有效质检结果，请先完成质检') ||
        (nextQuality.reason.status === 404 && nextQuality.reason.message === '该任务还没有质检结果')
      ))) throw nextQuality.reason
      tree.value = nextTree.value
      quality.value = nextQuality.status === 'fulfilled' ? nextQuality.value : null
    } catch (cause) { if (current()) error.value = (cause as Error).message }
    finally { if (current()) loadingBatch.value = false }
  }
  async function refreshChoices() {
    if (options.historyOnly?.()) return
    const request = ++listRequest
    if (choices) await choices.refresh()
    const result = choices ? choices.rows.value : await client.preprocessingBatches()
    if (!disposed && request === listRequest) batches.value = activeBatchItems(result).filter(batch => !retiredIds.has(batch.batch_id))
  }
  function retireBatches(ids: string[], forceReset = false) {
    ids.forEach(id => retiredIds.add(id))
    batches.value = batches.value.filter(batch => !ids.includes(batch.batch_id))
    if (!forceReset && !ids.includes(batchId.value)) return
    ++listRequest; loadingBatches.value = false; void selectBatch('')
  }
  function dispose() { disposed = true; ++epoch; ++listRequest; ++detailRequest }
  if (choices) watch(choices.rows, rows => { batches.value = activeBatchItems(rows).filter(batch => !retiredIds.has(batch.batch_id)) })
  async function changeTaskPage(page: number, size = taskSize.value) { taskPage.value = page; taskSize.value = size; await refresh() }
  async function searchTasks(value: string) { taskSearch.value = value; taskPage.value = 1; await refresh() }
  async function loadRecommendation() {
    const token = epoch, id = batchId.value
    if (!id || options.waitingForQuality?.()) return
    try { const value = await client.correctionRecommendation(id); if (!disposed && token === epoch) recommendation.value = value }
    catch (cause) { if (!disposed && token === epoch) error.value = (cause as Error).message }
  }
  return { batches, batchId, currentTree, summaries, recommendation, selectedTaskId, tree, quality, loadingBatches, loadingTree, loadingBatch, error, counts, readyTasks, loadBatches, selectBatch, viewTree, refresh, refreshChoices, retireBatches, dispose,
    choices, taskPage, taskSize, taskSearch, taskTotal, readyTaskIds, selectedTaskRow, changeTaskPage, searchTasks, loadRecommendation }
}
