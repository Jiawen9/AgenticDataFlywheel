import type { LocationQuery, LocationQueryRaw } from 'vue-router'
export interface PublishedBatchEvent {
  type: 'batches-published'
  event_id: string
  batch_ids: string[]
  release_id: string | null
  published_at?: string | null
  source_path?: string
}
const listeners = new Set<(event: PublishedBatchEvent) => void>()
const published = new Map<string, PublishedBatchEvent>()
const seen = new Set<string>()
let channel: BroadcastChannel | null = null
function accept(event: PublishedBatchEvent) {
  if (event?.type !== 'batches-published' || !Array.isArray(event.batch_ids) || !event.batch_ids.every(id => typeof id === 'string') || typeof event.event_id !== 'string' || seen.has(event.event_id)) return
  seen.add(event.event_id)
  if (seen.size > 200) seen.delete(seen.values().next().value!)
  for (const id of event.batch_ids) published.set(id, event)
  for (const listener of listeners) listener(event)
}
function openChannel() {
  if (!channel && typeof window !== 'undefined' && typeof BroadcastChannel !== 'undefined') {
    channel = new BroadcastChannel('agentic-data-flywheel.batch-lifecycle.v1')
    channel.onmessage = message => accept(message.data)
  }
}
export function notifyBatchesPublished(value: Omit<PublishedBatchEvent, 'type' | 'event_id'>) {
  const event: PublishedBatchEvent = { ...value, batch_ids: [...new Set(value.batch_ids)], type: 'batches-published', event_id: Date.now() + ':' + Math.random().toString(36).slice(2) }
  accept(event)
  openChannel()
  channel?.postMessage(event)
  if (!listeners.size) { channel?.close(); channel = null }
}
export function subscribeBatchLifecycle(listener: (event: PublishedBatchEvent) => void) {
  listeners.add(listener); openChannel()
  return () => { listeners.delete(listener); if (!listeners.size) { channel?.close(); channel = null } }
}
export function publishedBatch(id?: string | null) { return id ? published.get(id) : undefined }
export function activeBatchItems<T extends { batch_id?: string | null }>(items: T[]): T[] { return items.filter(item => !publishedBatch(item.batch_id)) }
export function eventMatchesRoute(event: PublishedBatchEvent, batchId: string, query: Record<string, unknown>) {
  if (event.batch_ids.includes(batchId) || (typeof query.batch_id === 'string' && event.batch_ids.includes(query.batch_id)) || (typeof query.collection_batch_id === 'string' && event.batch_ids.includes(query.collection_batch_id))) return true
  const run = query.tree_run_id ?? query.run, session = query.session_id ?? query.session
  return (typeof run === 'string' && event.source_path === '/api/tree-runs/' + encodeURIComponent(run)) || (typeof session === 'string' && event.source_path === '/api/correction/sessions/' + encodeURIComponent(session))
}
export function withoutBatchQuery(query: LocationQuery): LocationQueryRaw {
  return Object.fromEntries(Object.entries(query).filter(([key]) => !['batch_id', 'collection_batch_id', 'tree_run_id', 'run', 'session_id', 'session'].includes(key)))
}
