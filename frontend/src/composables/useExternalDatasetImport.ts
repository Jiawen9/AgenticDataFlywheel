import { computed, reactive, ref, shallowRef, watch } from 'vue'
import { ApiError } from '@/api'
import { datasetImportApi, MAX_DATASET_IMPORT_BYTES, type DatasetImportFields, type DatasetImportPreview } from '@/datasetImportApi'

export function localImportDate(now = new Date()) {
  return [now.getFullYear(), String(now.getMonth() + 1).padStart(2, '0'), String(now.getDate()).padStart(2, '0')].join('-')
}
export const defaultImportFields = (): DatasetImportFields => ({ data_source: '人工采集', data_date: localImportDate(), app: '', level1: '', level2: '', sheet_name: '' })
export function useExternalDatasetImport(client: typeof datasetImportApi = datasetImportApi) {
  const visible = ref(false)
  const name = ref('')
  const file = shallowRef<File | null>(null)
  const fields = reactive(defaultImportFields())
  const preview = ref<DatasetImportPreview | null>(null)
  const sheets = ref<string[]>([])
  const previewing = ref(false)
  const publishing = ref(false)
  const error = ref('')
  const requestId = ref('')
  const canPublish = computed(() => !previewing.value && !publishing.value && Boolean(preview.value?.valid && preview.value.import_id && name.value.trim()))
  let disposed = false
  let epoch = 0
  let controller: AbortController | undefined

  function invalidate() {
    ++epoch
    controller?.abort()
    controller = undefined
    previewing.value = false
    preview.value = null
    requestId.value = ''
    error.value = ''
  }
  const stopFields = watch(fields, invalidate, { flush: 'sync' })
  const stopName = watch(name, () => { requestId.value = '' }, { flush: 'sync' })
  function selectFile(next: File | null) {
    if (disposed || publishing.value) return
    invalidate()
    sheets.value = []
    fields.sheet_name = ''
    file.value = next
    if (next && next.size > MAX_DATASET_IMPORT_BYTES) error.value = '文件不能超过 50 MiB'
    else if (next && !/\.(xlsx|xlsm)$/i.test(next.name)) error.value = '仅支持 .xlsx、.xlsm 文件'
  }
  function open() { if (!disposed && !publishing.value) visible.value = true }
  function close() {
    if (publishing.value) return false
    ++epoch
    controller?.abort()
    controller = undefined
    previewing.value = false
    visible.value = false
    return true
  }
  function resetDraft() {
    invalidate()
    name.value = ''
    file.value = null
    sheets.value = []
    Object.assign(fields, defaultImportFields())
    visible.value = false
  }
  async function prepare() {
    if (disposed || publishing.value || !visible.value) return
    invalidate()
    if (!file.value) { error.value = '请选择 Excel 文件'; return }
    if (file.value.size > MAX_DATASET_IMPORT_BYTES) { error.value = '文件不能超过 50 MiB'; return }
    if (!/\.(xlsx|xlsm)$/i.test(file.value.name)) { error.value = '仅支持 .xlsx、.xlsm 文件'; return }
    if (!fields.data_source.trim()) { error.value = '请输入数据来源'; return }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(fields.data_date)) { error.value = '请选择数据日期'; return }
    const current = epoch
    controller = new AbortController()
    previewing.value = true
    try {
      const result = await client.preview(file.value, { ...fields, data_source: fields.data_source.trim() }, controller.signal)
      if (disposed || current !== epoch) return
      preview.value = result
      sheets.value = result.sheets
    } catch (cause) { if (!disposed && current === epoch) error.value = cause instanceof Error ? cause.message : '表格预览失败' }
    finally { if (!disposed && current === epoch) previewing.value = false }
  }
  async function publish() {
    if (disposed || !canPublish.value || !preview.value?.import_id) return null
    requestId.value ||= globalThis.crypto?.randomUUID?.() ?? ('import-' + Date.now() + '-' + Math.random().toString(36).slice(2))
    const payload = { import_id: preview.value.import_id, name: name.value.trim(), request_id: requestId.value }
    publishing.value = true
    error.value = ''
    try {
      const release = await client.publish(payload)
      if (disposed) return null
      resetDraft()
      return release
    } catch (cause) {
      if (!disposed) {
        const message = cause instanceof Error ? cause.message : '发布失败，请重试'
        const needsReview = cause instanceof ApiError && (cause.status === 410 || (cause.status === 409 && ['import_changed', 'duplicate_metadata'].includes(cause.detail?.code ?? '')))
        if (needsReview) invalidate()
        error.value = needsReview ? message + '；请重新预览并校验后发布' : message
      }
      return null
    } finally { if (!disposed) publishing.value = false }
  }
  function dispose() { disposed = true; ++epoch; controller?.abort(); stopFields(); stopName() }
  return { visible, name, file, fields, preview, sheets, previewing, publishing, error, requestId, canPublish, selectFile, open, close, prepare, publish, dispose }
}
