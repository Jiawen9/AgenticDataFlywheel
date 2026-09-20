import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '@/api'
import { notifyBatchesPublished, publishedBatch, subscribeBatchLifecycle, type PublishedBatchEvent } from '@/utils/batchLifecycle'

export function useBatchLifecycle(options: {
  currentBatch: () => string
  onPublished: (event: PublishedBatchEvent) => void
  refreshChoices?: () => Promise<unknown>
}) {
  const notice = ref<PublishedBatchEvent | null>(null)
  let disposed = false, checking = false
  const unsubscribe = subscribeBatchLifecycle(options.onPublished)
  async function checkBatch(id: string): Promise<boolean> {
    if (!id) return true
    const known = publishedBatch(id)
    if (known) { if (!disposed) options.onPublished(known); return false }
    const state = await api.batchLifecycle(id)
    if (disposed) return false
    if (state.status === 'published') {
      notifyBatchesPublished({ batch_ids: [id], release_id: state.release_id, published_at: state.published_at })
      return false
    }
    return !publishedBatch(id)
  }
  async function revalidate() {
    if (disposed || checking) return
    checking = true
    try { await checkBatch(options.currentBatch()); if (!disposed) await options.refreshChoices?.() }
    catch { /* A network failure is not evidence that a batch was published. */ }
    finally { checking = false }
  }
  function visible() { if (document.visibilityState === 'visible') void revalidate() }
  onMounted(() => { window.addEventListener('focus', revalidate); document.addEventListener('visibilitychange', visible) })
  onBeforeUnmount(() => { disposed = true; unsubscribe(); window.removeEventListener('focus', revalidate); document.removeEventListener('visibilitychange', visible) })
  return { notice, checkBatch, revalidate }
}
