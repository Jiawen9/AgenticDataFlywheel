import { computed, ref } from 'vue'
import { trainingDataOverviewApi, type OverviewConversion } from '@/trainingDataOverviewApi'

/** Release maintenance is independent of both dashboard filters and S3 upload polling. */
export function useReleaseOverview(
  releaseId: string,
  client: Pick<typeof trainingDataOverviewApi, 'read' | 'retry'> = trainingDataOverviewApi,
  pollMs = 3000,
) {
  const conversion = ref<OverviewConversion | null>(null)
  const workbookReady = ref(false)
  const loading = ref(false)
  const retrying = ref(false)
  const error = ref('')
  const active = computed(() => conversion.value?.status === 'queued' || conversion.value?.status === 'running')
  let disposed = false, epoch = 0
  let controller: AbortController | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  function stopTimer() { if (timer !== undefined) clearTimeout(timer); timer = undefined }
  async function load(foreground = true) {
    if (disposed) return
    stopTimer()
    controller?.abort()
    controller = new AbortController()
    const current = ++epoch
    loading.value = foreground
    error.value = ''
    try {
      const result = await client.read({ source: '', app: '', level1: '', level2: '', start_date: '', end_date: '' }, controller.signal)
      if (disposed || current !== epoch) return
      conversion.value = result.conversions.find(item => item.release_id === releaseId) ?? null
      workbookReady.value = Boolean(result.workbook_url)
    } catch (cause) {
      if (!disposed && current === epoch) error.value = cause instanceof Error ? cause.message : '无法读取汇总状态'
    } finally {
      if (!disposed && current === epoch) {
        loading.value = false
        if (active.value) timer = setTimeout(() => { void load(false) }, pollMs)
      }
    }
  }
  async function retry() {
    if (disposed || retrying.value || active.value) return
    retrying.value = true
    stopTimer()
    controller?.abort()
    ++epoch
    loading.value = false
    error.value = ''
    try {
      const result = await client.retry(releaseId)
      if (disposed) return
      conversion.value = result.conversion
      await load(false)
    } catch (cause) {
      if (!disposed) error.value = cause instanceof Error ? cause.message : '汇总重试失败'
    } finally {
      if (!disposed) retrying.value = false
    }
  }
  function dispose() { disposed = true; ++epoch; controller?.abort(); stopTimer() }
  return { conversion, workbookReady, loading, retrying, error, active, load, retry, dispose }
}
