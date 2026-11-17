import { computed, ref, watch } from 'vue'
import { batchChoices } from '@/businessPageApi'
import { useSummaryPage } from './useSummaryPage'
import type { PreprocessingBatch } from '@/types'

/** Only identifiers/filter/page are remembered. The selected detail is reread. */
export function useBusinessBatchChoices(name: string, enabled: () => boolean) {
  const preferenceKey = `adf.business.selection.v1:${import.meta.env.VITE_API_BASE_URL || ''}:${name}`
  let remembered: { batch?: string; search?: string; page?: number; size?: number } = {}
  try { remembered = JSON.parse(sessionStorage.getItem(preferenceKey) || '{}') } catch { /* optional */ }
  const search = ref(remembered.search || ''), page = ref(remembered.page || 1), size = ref([10, 20, 50].includes(remembered.size || 0) ? remembered.size! : 10)
  const selected = ref(remembered.batch || ''), selectedRow = ref<PreprocessingBatch | null>(null)
  const query = computed(() => ({ offset: (page.value - 1) * size.value, limit: size.value, q: search.value }))
  const list = useSummaryPage('/api/trajectory-preprocessing/batches', query, batchChoices, { enabled, persist: true })
  const rows = computed(() => selectedRow.value && !list.items.value.some(r => r.batch_id === selectedRow.value!.batch_id)
    ? [selectedRow.value, ...list.items.value] : list.items.value)
  watch([selected, search, page, size], () => {
    try { sessionStorage.setItem(preferenceKey, JSON.stringify({ batch: selected.value, search: search.value, page: page.value, size: size.value })) } catch { /* optional */ }
  })
  watch([search, size], () => { page.value = 1 })
  let exactRequest = 0
  async function exact(id: string, force = false) {
    const token = ++exactRequest
    if (!id) { selected.value = ''; selectedRow.value = null; return null }
    const cached = list.items.value.find(r => r.batch_id === id)
    if (cached && !force) { selected.value = id; selectedRow.value = cached; return cached }
    const value = await batchChoices({ batch_id: id, limit: 1 })
    if (token !== exactRequest) return
    selected.value = id; selectedRow.value = value.items[0] || null
    return selectedRow.value
  }
  watch(list.items, items => {
    const current = items.find(r => r.batch_id === selected.value)
    if (current) selectedRow.value = current
  })
  return { ...list, rows, search, page, size, selected, exact }
}
