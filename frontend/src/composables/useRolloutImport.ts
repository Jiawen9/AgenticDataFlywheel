import { computed, reactive, ref, watch } from 'vue'
import { newRunRequestId } from '@/phoneFactoryApi'
import { rolloutImportApi, type RolloutImportInput, type RolloutImportPreview, type RolloutImportResult } from '@/rolloutImportApi'

export function useRolloutImport(client = rolloutImportApi, imported: (result: RolloutImportResult) => void = () => {}) {
  const visible = ref(false), loading = ref(false), previewing = ref(false), committing = ref(false)
  const error = ref(''), allowedRoots = ref<string[]>([]), preview = ref<RolloutImportPreview | null>(null)
  const draft = reactive({ source_path: '', batch_id: '', name: '', app: '', scene: '', capability: '' })
  const taskEdits = reactive<Record<string, string>>({})
  const input = computed<RolloutImportInput>(() => ({
    source_path: draft.source_path.trim(), batch_id: draft.batch_id.trim(), name: draft.name.trim(),
    app: draft.app.trim(), scene: draft.scene.trim(), capability: draft.capability.trim(),
    task_overrides: Object.entries(taskEdits).sort(([a], [b]) => a.localeCompare(b)).map(([collection_case_id, task]) => ({ collection_case_id, task: task.trim() })),
  }))
  const fingerprint = computed(() => JSON.stringify(input.value))
  const reviewedFingerprint = ref('')
  const stale = computed(() => Boolean(preview.value && fingerprint.value !== reviewedFingerprint.value))
  const canPreview = computed(() => Boolean(draft.source_path.trim() && draft.batch_id.trim() && !loading.value && !committing.value))
  const canCommit = computed(() => Boolean(preview.value?.valid && !preview.value.errors.length && !stale.value && !previewing.value && !committing.value))
  let epoch = 0, disposed = false, optionsController: AbortController | undefined, previewController: AbortController | undefined, commitController: AbortController | undefined
  const requestIds = new Map<string, string>()
  const clearEdits = () => { Object.keys(taskEdits).forEach(key => delete taskEdits[key]) }
  const current = (token: number) => !disposed && visible.value && token === epoch
  // Synchronous invalidation prevents a just-finished older response from re-enabling import.
  const stopWatch = watch(fingerprint, () => { epoch++; previewController?.abort(); previewing.value = false; error.value = '' }, { flush: 'sync' })
  const stopPathWatch = watch(() => draft.source_path, clearEdits, { flush: 'sync' })
  async function open() {
    epoch++; optionsController?.abort(); previewController?.abort()
    visible.value = true; preview.value = null; reviewedFingerprint.value = ''; clearEdits(); error.value = ''
    const today = new Date(), date = `${today.getFullYear()}${String(today.getMonth() + 1).padStart(2, '0')}${String(today.getDate()).padStart(2, '0')}`
    Object.assign(draft, { source_path: '', batch_id: `rollout-${date}-${newRunRequestId().slice(0, 8)}`, name: '', app: '', scene: '', capability: '' })
    allowedRoots.value = []; loading.value = true; previewing.value = false
    optionsController = new AbortController()
    const controller = optionsController
    try {
      const result = await client.options(controller.signal)
      if (disposed || !visible.value || optionsController !== controller || controller.signal.aborted) return
      allowedRoots.value = result.allowed_roots
      if (!draft.source_path) draft.source_path = result.default_source_path
    } catch (cause) { if (!disposed && visible.value && optionsController === controller && !controller.signal.aborted) error.value = (cause as Error).message }
    finally { if (!disposed && optionsController === controller) loading.value = false }
  }
  function close() {
    if (committing.value) return false
    visible.value = false; epoch++; optionsController?.abort(); previewController?.abort(); loading.value = false; previewing.value = false
    return true
  }
  async function validate() {
    if (!canPreview.value || previewing.value) return
    previewController?.abort(); previewController = new AbortController()
    const token = ++epoch, key = fingerprint.value
    previewing.value = true; error.value = ''
    try {
      const result = await client.preview(input.value, previewController.signal)
      if (!current(token) || key !== fingerprint.value) return
      preview.value = result; reviewedFingerprint.value = key
    } catch (cause) { if (current(token) && (cause as Error).name !== 'AbortError') error.value = (cause as Error).message }
    finally { if (current(token)) previewing.value = false }
  }
  async function commit() {
    if (!canCommit.value || !preview.value) return
    const reviewed = preview.value
    if (Date.parse(reviewed.expires_at) <= Date.now()) { error.value = '校验预览已过期，请重新校验'; reviewedFingerprint.value = ''; return }
    const request_id = requestIds.get(reviewed.import_id) || newRunRequestId()
    requestIds.set(reviewed.import_id, request_id)
    const token = ++epoch
    committing.value = true; error.value = ''; commitController = new AbortController()
    try {
      const result = await client.commit({ import_id: reviewed.import_id, request_id }, commitController.signal)
      if (!current(token)) return
      requestIds.delete(reviewed.import_id); committing.value = false; close(); preview.value = null; clearEdits()
      imported(result)
    } catch (cause) { if (current(token)) error.value = `${(cause as Error).message}。可重试确认导入；重试会复用同一请求编号。` }
    finally { if (!disposed) committing.value = false }
  }
  function dispose() { disposed = true; epoch++; optionsController?.abort(); previewController?.abort(); commitController?.abort(); stopWatch(); stopPathWatch() }
  return { visible, loading, previewing, committing, error, allowedRoots, preview, draft, taskEdits, stale, canPreview, canCommit, open, close, validate, commit, dispose }
}
