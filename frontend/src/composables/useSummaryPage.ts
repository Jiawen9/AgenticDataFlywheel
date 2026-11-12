import { computed, onBeforeUnmount, onMounted, ref, shallowRef, toValue, watch, type MaybeRefOrGetter } from 'vue'
import { cachedSummary, invalidateSummaryCache, sharedRead, summaryKey, type SummaryPage, type SummaryQuery } from '@/utils/summaryCache'
import { subscribeBatchLifecycle } from '@/utils/batchLifecycle'

/** Cached summaries are display-only; detail/mutation paths still verify backend revisions. */
export function useSummaryPage<T>(path: string, query: MaybeRefOrGetter<SummaryQuery>, load: (query: SummaryQuery) => Promise<SummaryPage<T>>, options: { enabled?: MaybeRefOrGetter<boolean>; persist?: boolean; prefetch?: boolean } = {}) {
  const data = shallowRef<SummaryPage<T> | null>(null), loading = ref(false), error = ref(''), cached = ref(false)
  const key = computed(() => summaryKey(path, toValue(query)))
  const enabled = () => toValue(options.enabled) !== false && (typeof document === 'undefined' || !document.hidden)
  let disposed = false, mounted = false, failures = 0, epoch = 0, timer: ReturnType<typeof setTimeout> | undefined
  let previousScope = '', shownKey = ''
  let prefetchTimer: ReturnType<typeof setTimeout> | undefined, prefetchedKey = ''
  let inFlight: Promise<void> | undefined, flightKey = ''
  function schedule() {
    clearTimeout(timer)
    if (!disposed && mounted && enabled()) timer = setTimeout(() => void refresh(), Math.min(120000, (data.value?.scope_counts?.active ? 5000 : 30000) * 2 ** Math.min(failures, 3)))
  }
  async function refresh(): Promise<void> {
    if (disposed || !enabled()) return
    const requestedKey = key.value
    if (inFlight && flightKey === requestedKey) return inFlight
    clearTimeout(timer)
    const requestedQuery = { ...toValue(query) }, current = ++epoch
    loading.value = true; flightKey = requestedKey
    inFlight = (async () => {
      try {
        const page = await sharedRead(requestedKey, async () => {
          const value = await load(requestedQuery)
          if (!value || !Array.isArray(value.items) || !Array.isArray(value.latest)) throw new Error('运行摘要格式不兼容，请核对平台前后端版本')
          return value
        }, { persist: options.persist !== false, freshMs: cached.value ? 0 : 800 })
        if (disposed || current !== epoch || requestedKey !== key.value) return
        const keepPrevious = !page.counts_complete && !page.items.length && !!data.value && shownKey === requestedKey
        data.value = keepPrevious ? { ...page, items: data.value!.items, latest: data.value!.latest, stale: true } : page
        shownKey = requestedKey; cached.value = keepPrevious; error.value = ''; failures = 0
        if (options.prefetch && page.has_more && page.counts_complete && !page.stale && prefetchedKey !== requestedKey) {
          clearTimeout(prefetchTimer)
          prefetchTimer = setTimeout(() => {
            if (disposed || current !== epoch || key.value !== requestedKey || !enabled()) return
            prefetchedKey = requestedKey
            const next = { ...requestedQuery, offset: page.offset + page.limit }
            void sharedRead(summaryKey(path, next), () => load(next), { persist: options.persist !== false, freshMs: 30000 }).catch(() => {})
          }, 400)
        }
      } catch (cause) { if (!disposed && current === epoch) { error.value = (cause as Error).message; failures++ } }
      finally { if (!disposed && current === epoch) { loading.value = false; inFlight = undefined; schedule() } }
    })()
    return inFlight
  }
  watch(key, current => {
    epoch++; inFlight = undefined; clearTimeout(prefetchTimer); error.value = ''
    const hit = cachedSummary<SummaryPage<T>>(current)
    const queryNow = toValue(query), scope = JSON.stringify([queryNow.batch_id, queryNow.task_source_batch_id, queryNow.run_id])
    data.value = hit?.value || (scope === previousScope ? data.value : null); cached.value = !!data.value
    if (hit) shownKey = current
    else if (!data.value) shownKey = ''
    previousScope = scope
    if (mounted) void refresh()
  }, { immediate: true })
  watch(() => toValue(options.enabled), value => { if (value === false) { epoch++; inFlight = undefined; loading.value = false; clearTimeout(timer); clearTimeout(prefetchTimer) } else if (mounted) void refresh() })
  function reload() { epoch++; inFlight = undefined; invalidateSummaryCache(); return refresh() }
  function focus() { if (enabled()) void refresh(); else { clearTimeout(timer); clearTimeout(prefetchTimer) } }
  const unsubscribe = subscribeBatchLifecycle(() => { epoch++; inFlight = undefined; cached.value = !!data.value; void refresh() })
  onMounted(() => { mounted = true; if (typeof window !== 'undefined') window.addEventListener('focus', focus); if (typeof document !== 'undefined') document.addEventListener('visibilitychange', focus); void refresh() })
  onBeforeUnmount(() => { disposed = true; epoch++; clearTimeout(timer); clearTimeout(prefetchTimer); unsubscribe(); if (typeof window !== 'undefined') window.removeEventListener('focus', focus); if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', focus) })
  return { data, loading, error, cached, refresh, reload, items: computed(() => data.value?.items || []), latest: computed(() => data.value?.latest || []), counts: computed(() => data.value?.scope_counts), total: computed(() => data.value?.filtered_total ?? data.value?.total ?? 0) }
}
