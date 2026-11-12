/** Small, versioned display snapshots. Never use a cached summary to authorize a write. */
export interface SummaryPage<T> {
  items: T[]; total: number | null; filtered_total?: number | null
  scope_counts: { total: number | null; active: number | null; abnormal: number | null } | null
  latest: T[]; offset: number; limit: number; has_more: boolean
  index_state: string; counts_complete: boolean; updated_at?: string; stale?: boolean; refresh_error?: string
}
export type SummaryQuery = { offset?: number; limit?: number; search?: string; status?: string; [key: string]: string | number | boolean | undefined }
const prefix = 'adf.summary.v1:'
const namespace = encodeURIComponent((import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, ''))
const snapshots = new Map<string, { value: unknown; at: number }>()
const pending = new Map<string, Promise<unknown>>()
let revision = 0
const maxEntries = 32, maxBytes = 256 * 1024, maxAge = 30 * 60 * 1000
export function summaryKey(path: string, query: SummaryQuery = {}) {
  const params = new URLSearchParams()
  for (const key of Object.keys(query).sort()) if (query[key] !== undefined && query[key] !== '' && !(key === 'status' && query[key] === 'all')) params.set(key, String(query[key]))
  return `${namespace}:${path}?${params}`
}
export function summaryParams(query: SummaryQuery = {}) { const params = new URLSearchParams({ view: 'summary' }); for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== '' && !(key === 'status' && value === 'all')) params.set(key, String(value)); return params.toString() }
const rowKeys = new Set(['pipeline_id', 'run_id', 'collection_run_id', 'batch_id', 'task_source_batch_id', 'source_batch_id', 'filename', 'name', 'status', 'mode', 'created_at', 'updated_at', 'current_step', 'agent_type', 'execution_mode', 'collection_status', 'transfer_status', 'archive_status', 'batch_status', 'release_id', 'collector_data_state', 'repeat_times', 'receipt_policy_version', 'receipt_closed', 'collection_artifacts_version', 'trajectory_count', 'storage_revision', 'collection_summary', 'evaluation_summary'])
function displayRow(value: unknown): unknown {
  if (!value || typeof value !== 'object') return value
  return Object.fromEntries(Object.entries(value).filter(([key]) => rowKeys.has(key)).map(([key, val]) => [key, key.endsWith('_summary') && val && typeof val === 'object' ? Object.fromEntries(Object.entries(val).filter(([, n]) => typeof n === 'number')) : val]))
}
function persist(key: string, value: unknown, at: number) {
  // Only list envelopes are persisted. Detail/configuration/error payloads stay out.
  if (!value || typeof value !== 'object' || !('items' in value) || !('latest' in value)) return
  const page = value as SummaryPage<unknown>
  const safe = { items: page.items.slice(0, 50).map(displayRow), latest: page.latest.slice(0, 3).map(displayRow), total: page.total, filtered_total: page.filtered_total, scope_counts: page.scope_counts, offset: page.offset, limit: page.limit, has_more: page.has_more, index_state: page.index_state, counts_complete: page.counts_complete, updated_at: page.updated_at }
  try {
    const storage = sessionStorage, item = JSON.stringify({ at, value: safe })
    if (item.length > maxBytes) return
    storage.setItem(prefix + key, item)
    const records = Object.keys(storage).filter(k => k.startsWith(prefix)).map(k => ({ key: k, text: storage.getItem(k) || '' }))
    records.sort((a, b) => (JSON.parse(a.text).at || 0) - (JSON.parse(b.text).at || 0))
    let bytes = records.reduce((sum, entry) => sum + entry.text.length, 0)
    while (records.length > maxEntries || bytes > maxBytes) { const oldest = records.shift()!; bytes -= oldest.text.length; storage.removeItem(oldest.key) }
  } catch { /* Storage is optional (private browsing, quota, invalid stale entry). */ }
}
export function cachedSummary<T>(key: string): { value: T; at: number } | undefined {
  const hit = snapshots.get(key)
  if (hit && Date.now() - hit.at < maxAge) return hit as { value: T; at: number }
  try { const record = JSON.parse(sessionStorage.getItem(prefix + key) || 'null'); if (record && Date.now() - record.at < maxAge) { snapshots.set(key, record); return record } } catch { /* optional cache */ }
}
export function sharedRead<T>(key: string, load: () => Promise<T>, options: { persist?: boolean; freshMs?: number } = {}): Promise<T> {
  const hit = snapshots.get(key)
  if (hit && Date.now() - hit.at < (options.freshMs ?? 800)) return Promise.resolve(hit.value as T)
  const inFlight = pending.get(key)
  if (inFlight) return inFlight as Promise<T>
  const epoch = revision
  const work = load().then(value => {
    if (epoch === revision) {
      const at = Date.now(); snapshots.delete(key); snapshots.set(key, { value, at })
      while (snapshots.size > maxEntries) snapshots.delete(snapshots.keys().next().value!)
      if (options.persist) persist(key, value, at)
    }
    return value
  }).finally(() => { if (pending.get(key) === work) pending.delete(key) })
  pending.set(key, work); return work
}
export function invalidateSummaryCache() {
  revision++; snapshots.clear(); pending.clear()
  try { for (const key of Object.keys(sessionStorage)) if (key.startsWith(prefix)) sessionStorage.removeItem(key) } catch { /* optional cache */ }
}
