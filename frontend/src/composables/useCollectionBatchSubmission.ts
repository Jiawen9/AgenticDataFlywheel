import { computed, ref, shallowRef, watch } from 'vue'
import type { CollectionBatchDetail, CollectionBatchSummary, collectionBatchesApi } from '@/collectionBatchesApi'
import type { TaskGenerationJob } from '@/types'

type BatchApi = Pick<typeof collectionBatchesApi, 'list' | 'submit' | 'workbook'>
export type RunCollectionBatchAction = (action: (jobId: string) => Promise<CollectionBatchDetail | null>) => Promise<CollectionBatchDetail | null>
interface SubmissionOptions {
  job: () => TaskGenerationJob
  resultCount: () => number
  busy: () => boolean
  runProtected: RunCollectionBatchAction
}

/** Existing batch identity is independent of subsequent source edits or deletion. */
export function useCollectionBatchSubmission(api: BatchApi, options: SubmissionOptions) {
  const batch = shallowRef<CollectionBatchSummary | null>(null)
  const loading = ref(false)
  const submitting = ref(false)
  const downloading = ref(false)
  const error = ref('')
  const loaded = ref(false)
  let epoch = 0
  let requestId = 0
  let disposed = false
  const eligible = computed(() => ['succeeded', 'partial'].includes(options.job().status) && options.resultCount() > 0)
  const canSubmit = computed(() => eligible.value && !batch.value && loaded.value && !error.value && !loading.value && !submitting.value && !options.busy())
  const valid = (id: string, ticket: number) => !disposed && epoch === ticket && options.job().job_id === id

  async function refresh() {
    const id = options.job().job_id, ticket = epoch, request = ++requestId
    loading.value = true; error.value = ''
    try {
      const batches = await api.list(id)
      if (!valid(id, ticket) || request !== requestId) return
      batch.value = batches.find(item => item.source_job_id === id) || null
      loaded.value = true
    } catch (issue) { if (valid(id, ticket) && request === requestId) { loaded.value = false; error.value = (issue as Error).message } }
    finally { if (valid(id, ticket) && request === requestId) loading.value = false }
  }
  const stop = watch(() => options.job().job_id, () => {
    epoch++; requestId++; batch.value = null; loaded.value = false; error.value = ''; submitting.value = false; downloading.value = false
    void refresh()
  }, { immediate: true, flush: 'sync' })

  async function submit() {
    if (!canSubmit.value) return false
    const id = options.job().job_id, ticket = epoch
    submitting.value = true; error.value = ''
    try {
      // The page saves/discards dirty text before acquiring its shared busy lock.
      const created = await options.runProtected(async activeId => {
        if (!valid(id, ticket) || activeId !== id) return null
        return api.submit(activeId)
      })
      if (!created || !valid(id, ticket)) return false
      // Invalidates any older list response which could otherwise erase success.
      requestId++; loading.value = false; batch.value = created; loaded.value = true
      return true
    } catch (issue) { if (valid(id, ticket)) error.value = (issue as Error).message; return false }
    finally { if (valid(id, ticket)) submitting.value = false }
  }
  async function download() {
    const value = batch.value, id = options.job().job_id, ticket = epoch
    if (!value || downloading.value || options.busy()) return null
    downloading.value = true; error.value = ''
    try {
      const blob = await api.workbook(value.batch_id)
      return valid(id, ticket) ? { blob, filename: value.filename } : null
    } catch (issue) { if (valid(id, ticket)) error.value = (issue as Error).message; return null }
    finally { if (valid(id, ticket)) downloading.value = false }
  }
  function dispose() { disposed = true; epoch++; requestId++; stop() }
  return { batch, loading, submitting, downloading, loaded, error, eligible, canSubmit, refresh, submit, download, dispose }
}
