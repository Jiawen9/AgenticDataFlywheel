import { computed, onBeforeUnmount, onMounted, ref, toValue, watch, type MaybeRefOrGetter } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '@/api'
import { notifyBatchesPublished, publishedBatch, subscribeBatchLifecycle } from '@/utils/batchLifecycle'
import { PIPELINE_TERMINAL, type Pipeline, type PipelineStepId } from '@/types/pipeline'

/** Resolves authoritative backend bindings; route identifiers never authorize writes. */
export function usePipelineContext(options: { batchId?: MaybeRefOrGetter<string>; stepId?: PipelineStepId } = {}) {
  const route = useRoute()
  const pipeline = ref<Pipeline | null>(null)
  const error = ref('')
  const loading = ref(false)
  const resolved = ref(false)
  const controlling = ref(false)
  const routePipelineId = computed(() => typeof route?.query?.pipeline_id === 'string' ? route.query.pipeline_id : '')
  const routeBatchId = computed(() => typeof route?.query?.batch_id === 'string' ? route.query.batch_id : '')
  const batchId = computed(() => toValue(options.batchId) || routeBatchId.value || '')
  const isPipelineRoute = computed(() => Boolean(routePipelineId.value))
  const routeStepId = computed(() => typeof route?.query?.step_id === 'string' ? route.query.step_id : options.stepId)
  const selectedStepId = computed(() => routeStepId.value || pipeline.value?.current_step)
  const step = computed(() => pipeline.value?.steps.find(value => value.id === selectedStepId.value) ?? null)
  const historyOnly = computed(() => Boolean(isPipelineRoute.value && pipeline.value && !pipeline.value.release_id && (pipeline.value.history_only || ['terminated', 'no_publishable_data'].includes(pipeline.value.status))))
  const managed = computed(() => Boolean(pipeline.value && !PIPELINE_TERMINAL.includes(pipeline.value.status)))
  const canCorrect = computed(() => Boolean(pipeline.value?.mode === 'manual' && pipeline.value.status === 'waiting_for_correction' && selectedStepId.value === 'correction' && (options.stepId === 'correction' || route?.path === '/correction/expert-action') && !error.value && !controlling.value))
  const readOnly = computed(() => Boolean((isPipelineRoute.value || managed.value || error.value || (!resolved.value && batchId.value)) && !canCorrect.value))
  const isWaiting = computed(() => Boolean(isPipelineRoute.value && (!step.value || step.value.status === 'pending')))
  const jobIds = computed(() => step.value?.job_ids || [])
  const sessionId = computed(() => pipeline.value?.session_id || '')
  const releaseId = computed(() => pipeline.value?.release_id || '')
  let generation = 0, disposed = false
  let controller: AbortController | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  let pending: Promise<void> | undefined
  function clearTimer() { if (timer) clearTimeout(timer); timer = undefined }
  function publishNotice(value: Pipeline) {
    if (value.release_id && !publishedBatch(value.batch_id)) notifyBatchesPublished({ batch_ids: [value.batch_id], release_id: value.release_id })
  }
  function schedule() {
    clearTimer()
    if (!disposed && (routePipelineId.value || batchId.value) && (typeof document === 'undefined' || document.visibilityState !== 'hidden')) timer = setTimeout(() => { void refresh() }, 1500)
  }
  function invalidate() { generation++; controller?.abort(); controller = undefined; pending = undefined; clearTimer() }
  async function refresh(): Promise<void> {
    if (disposed) return
    if (pending) return pending
    const current = generation, requestedId = routePipelineId.value, requestedBatch = batchId.value
    if (!requestedId && !requestedBatch) { pipeline.value = null; error.value = ''; resolved.value = true; loading.value = false; return }
    // Some isolated existing module tests stub only their own APIs.
    if (typeof api.listPipelines !== 'function' || typeof api.pipeline !== 'function') { resolved.value = true; return }
    controller = new AbortController()
    const signal = controller.signal
    loading.value = true
    pending = (async () => {
      try {
        const value = requestedId ? await api.pipeline(requestedId, signal) : (await api.listPipelines({ batch_id: requestedBatch, active_only: true }, signal))[0] || null
        if (disposed || current !== generation || signal.aborted) return
        if (value && requestedBatch && value.batch_id !== requestedBatch) throw new Error('Pipeline 与当前批次不匹配，请返回 Pipeline 重新进入')
        if (value && selectedStepId.value && !value.steps.some(item => item.id === selectedStepId.value)) throw new Error('Pipeline 步骤不存在，请返回 Pipeline 重新进入')
        if (value && pipeline.value?.pipeline_id === value.pipeline_id && pipeline.value.storage_revision > value.storage_revision) return
        pipeline.value = value
        error.value = ''
        resolved.value = true
        if (value) publishNotice(value)
      } catch (cause) {
        if (!disposed && current === generation && !signal.aborted) { error.value = (cause as Error).message; resolved.value = true }
      } finally {
        if (!disposed && current === generation) { loading.value = false; pending = undefined; schedule() }
      }
    })()
    return pending
  }
  async function confirmCorrection(sessionRevision?: number) {
    if (!pipeline.value || !canCorrect.value || controlling.value) return
    controlling.value = true
    invalidate()
    const current = generation
    try {
      const updated = await api.controlPipeline(pipeline.value.pipeline_id, 'confirm-correction', pipeline.value.storage_revision, sessionRevision)
      if (!disposed && current === generation) { pipeline.value = updated; error.value = ''; publishNotice(updated) }
    } catch (cause) { if (!disposed) { error.value = (cause as Error).message; await refresh() }; throw cause }
    finally { controlling.value = false; schedule() }
  }
  const unwatch = watch([routePipelineId, batchId, routeStepId], () => {
    invalidate(); pipeline.value = null; error.value = ''; resolved.value = false; void refresh()
  }, { immediate: true, flush: 'sync' })
  function focus() { if (!disposed) void refresh() }
  function visibility() { if (document.visibilityState === 'hidden') { invalidate(); loading.value = false } else void refresh() }
  const unsubscribe = subscribeBatchLifecycle(event => { if (event.batch_ids.includes(pipeline.value?.batch_id || batchId.value)) void refresh() })
  function dispose() { if (disposed) return; disposed = true; invalidate(); unwatch(); unsubscribe(); window.removeEventListener('focus', focus); document.removeEventListener('visibilitychange', visibility) }
  onMounted(() => { window.addEventListener('focus', focus); document.addEventListener('visibilitychange', visibility) })
  onBeforeUnmount(dispose)
  return { pipeline, step, batchId, historyOnly, managed, readOnly, canCorrect, isWaiting, jobIds, sessionId, releaseId, isPipelineRoute, error, loading, resolved, controlling, refresh, dispose, confirmCorrection }
}
export type PipelineContext = ReturnType<typeof usePipelineContext>
