import { activeBatchItems, publishedBatch } from '@/utils/batchLifecycle'
import { computed, ref } from 'vue'
import type { api } from '@/api'
import { resolveBatchSelection } from '@/utils/batchSelection'
import type { CorrectionRecommendation, PreprocessingBatch, QualityTaskSummary, TaskQualityResult, TrajectoryTreeNode, TreeRun } from '@/types'

type Client = Pick<typeof api, 'preprocessingBatches' | 'batchTree' | 'runQuality' | 'tree' | 'taskQuality' | 'correctionRecommendation' | 'treeRun'>
export function useBatchQualityWorkspace(client: Client, options: {
  historyOnly?: () => boolean
  waitingForTree?: () => boolean
  waitingForQuality?: () => boolean
} = {}) {
  const retiredIds = new Set<string>()
  const batches = ref<PreprocessingBatch[]>([]), batchId = ref('')
  const currentTree = ref<TreeRun | null>(null), summaries = ref<Record<string, QualityTaskSummary>>({})
  const recommendation = ref<CorrectionRecommendation | null>(null)
  const selectedTaskId = ref(''), tree = ref<TrajectoryTreeNode | null>(null), quality = ref<TaskQualityResult | null>(null)
  const loadingBatches = ref(false), loadingTree = ref(false), loadingBatch = ref(false), error = ref('')
  let epoch = 0, detailRequest = 0, listRequest = 0, disposed = false
  const counts = computed(() => {
    const tasks = currentTree.value?.tasks ?? []
    const completed = tasks.filter(task => summaries.value[task.task_id]?.status === 'succeeded').length
    const stale = tasks.filter(task => ['stale', 'invalidated'].includes(summaries.value[task.task_id]?.status ?? task.tree_status ?? task.status ?? '')).length
    return { completed, stale, pending: tasks.length - completed - stale }
  })
  const readyTasks = computed(() => (currentTree.value?.tasks ?? []).filter(task => !['pending', 'stale', 'invalidated', 'failed', 'running'].includes(task.tree_status ?? task.status ?? 'succeeded')))
  function clearDetail() { ++detailRequest; selectedTaskId.value = ''; tree.value = null; quality.value = null; loadingTree.value = false }
  async function selectBatch(id: string) {
    const token = ++epoch
    batchId.value = id; clearDetail(); currentTree.value = null; summaries.value = {}; recommendation.value = null; error.value = ''
    if (!id || options.historyOnly?.() || publishedBatch(id) || retiredIds.has(id)) { batchId.value = ''; loadingBatch.value = false; return }
    if (batches.value.length && !batches.value.some(batch => batch.batch_id === id)) {
      batchId.value = ''; loadingBatch.value = false; error.value = '指定批次不存在或当前不可用，请重新选择批次'; return
    }
    if (options.waitingForTree?.()) { loadingBatch.value = false; return }
    loadingBatch.value = true
    const results = await Promise.allSettled([client.batchTree(id), options.waitingForQuality?.() ? Promise.resolve(null) : client.runQuality(id), options.waitingForQuality?.() ? Promise.resolve(null) : client.correctionRecommendation(id)])
    if (disposed || epoch !== token) return
    const [built, reviewed, suggested] = results
    if (built.status === 'fulfilled') currentTree.value = built.value
    else error.value = (built.reason as Error).message
    if (reviewed.status === 'fulfilled' && reviewed.value) summaries.value = Object.fromEntries(reviewed.value.tasks.map(task => [task.task_id, task]))
    else if (reviewed.status === 'rejected' && !error.value) error.value = (reviewed.reason as Error).message
    if (suggested.status === 'fulfilled') recommendation.value = suggested.value
    loadingBatch.value = false
  }
  async function loadBatches(preferredId?: string, legacyRunId?: string) {
    if (options.historyOnly?.()) { await selectBatch(''); return }
    const request = ++listRequest
    loadingBatches.value = true
    await selectBatch('')
    const token = epoch
    try {
      const result = await client.preprocessingBatches()
      if (disposed || request !== listRequest) return
      batches.value = activeBatchItems(result).filter(batch => !retiredIds.has(batch.batch_id))
      const selected = await resolveBatchSelection(batches.value.map(batch => batch.batch_id), { batchId: preferredId, legacyRunId }, client.treeRun)
      if (disposed || request !== listRequest || token !== epoch) return
      await selectBatch(selected)
    } catch (cause) { if (!disposed && request === listRequest && token === epoch) error.value = (cause as Error).message }
    finally { if (!disposed && request === listRequest) loadingBatches.value = false }
  }
  async function viewTree(taskId: string) {
    const id = batchId.value, token = epoch, request = ++detailRequest
    selectedTaskId.value = taskId; tree.value = null; quality.value = null; loadingTree.value = true
    try {
      const [built, reviewed] = await Promise.all([client.tree(id, taskId), summaries.value[taskId]?.status === 'succeeded' ? client.taskQuality(id, taskId) : Promise.resolve(null)])
      if (disposed || token !== epoch || request !== detailRequest) return
      tree.value = built; quality.value = reviewed
    } catch (cause) { if (!disposed && token === epoch && request === detailRequest) error.value = (cause as Error).message }
    finally { if (!disposed && token === epoch && request === detailRequest) loadingTree.value = false }
  }
  async function refresh() {
    const id = batchId.value, taskId = selectedTaskId.value
    await selectBatch(id)
    if (!disposed && batchId.value === id && taskId && readyTasks.value.some(task => task.task_id === taskId)) await viewTree(taskId)
  }
  async function refreshChoices() {
    if (options.historyOnly?.()) return
    const request = ++listRequest
    const result = await client.preprocessingBatches()
    if (!disposed && request === listRequest) batches.value = activeBatchItems(result).filter(batch => !retiredIds.has(batch.batch_id))
  }
  function retireBatches(ids: string[], forceReset = false) {
    ids.forEach(id => retiredIds.add(id))
    batches.value = batches.value.filter(batch => !ids.includes(batch.batch_id))
    if (!forceReset && !ids.includes(batchId.value)) return
    ++listRequest; loadingBatches.value = false; void selectBatch('')
  }
  function dispose() { disposed = true; ++epoch; ++listRequest; ++detailRequest }
  return { batches, batchId, currentTree, summaries, recommendation, selectedTaskId, tree, quality, loadingBatches, loadingTree, loadingBatch, error, counts, readyTasks, loadBatches, selectBatch, viewTree, refresh, refreshChoices, retireBatches, dispose }
}
