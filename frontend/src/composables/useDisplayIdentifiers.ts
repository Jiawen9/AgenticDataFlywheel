import { shallowReactive } from 'vue'

export type DisplayIdentifierKind = 'batch' | 'run' | 'task' | 'trajectory' | 'pipeline' | 'job' | 'session' | 'export' | 'release'
export interface DisplayIdentifier { kind: DisplayIdentifierKind; id: string; scope?: string }
export interface ResolvedDisplayIdentifier extends DisplayIdentifier { scope: string; display_id: string; mapped: boolean }
type Resolver = (items: DisplayIdentifier[]) => Promise<ResolvedDisplayIdentifier[]>
type Entry = { label: string; expires: number }
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const keyOf = (item: DisplayIdentifier) => JSON.stringify([item.kind, item.scope || '', item.id])

async function requestIdentifiers(items: DisplayIdentifier[]): Promise<ResolvedDisplayIdentifier[]> {
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 3000)
  try {
    const response = await fetch(`${API_BASE}/api/display-identifiers/resolve`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items }), signal: controller.signal,
    })
    if (!response.ok) throw new Error('显示编号暂不可用')
    const body = await response.json() as { items: ResolvedDisplayIdentifier[] }
    if (!Array.isArray(body.items)) throw new Error('显示编号响应无效')
    return body.items
  } finally { clearTimeout(timeout) }
}

/** Resolves labels only. Values sent to business APIs must always remain original IDs. */
export function createDisplayIdentifierStore(resolver: Resolver, options: { clock?: () => number; autoResolve?: () => boolean } = {}) {
  const cache = shallowReactive(new Map<string, Entry>())
  const pending = new Map<string, DisplayIdentifier>()
  const promises = new Map<string, { promise: Promise<void>; finish: () => void }>()
  const clock = options.clock || Date.now
  const autoResolve = options.autoResolve || (() => typeof window !== 'undefined')
  let scheduled = false

  async function flush() {
    scheduled = false
    const items = [...pending.values()].slice(0, 200)
    items.forEach(item => pending.delete(keyOf(item)))
    if (pending.size) schedule()
    if (!items.length) return
    try {
      const result = await resolver(items)
      // Index by the complete identity; never trust response ordering or a shared task number.
      const resolved = new Map(result.map(item => [keyOf(item), item]))
      for (const item of items) {
        const value = resolved.get(keyOf(item))
        const label = typeof value?.display_id === 'string' && value.display_id ? value.display_id : item.id
        cache.set(keyOf(item), { label, expires: value?.mapped ? Infinity : clock() + 30_000 })
      }
    } catch {
      // An unavailable display service must not block operations or erase the original ID.
      for (const item of items) cache.set(keyOf(item), { label: item.id, expires: clock() + 30_000 })
    } finally {
      for (const item of items) {
        const key = keyOf(item), waiter = promises.get(key)
        promises.delete(key); waiter?.finish()
      }
    }
  }
  function schedule() {
    if (scheduled) return
    scheduled = true
    queueMicrotask(() => { void flush() })
  }
  function enqueue(item: DisplayIdentifier): Promise<void> {
    if (!item.id) return Promise.resolve()
    const key = keyOf(item), known = cache.get(key)
    if (known && known.expires > clock()) return Promise.resolve()
    const existing = promises.get(key)
    if (existing) return existing.promise
    let finish!: () => void
    const promise = new Promise<void>(done => { finish = done })
    promises.set(key, { promise, finish }); pending.set(key, item); schedule()
    return promise
  }
  function displayId(kind: DisplayIdentifierKind, id?: string | null, scope?: string | null): string {
    if (!id) return ''
    const item = { kind, id, scope: kind === 'task' || kind === 'trajectory' ? scope || '' : '' }
    const known = cache.get(keyOf(item))
    if ((!known || known.expires <= clock()) && autoResolve()) void enqueue(item)
    return known?.label || id
  }
  function searchText(kind: DisplayIdentifierKind, id?: string | null, scope?: string | null) {
    return `${displayId(kind, id, scope)} ${id || ''}`
  }
  function matches(query: string, kind: DisplayIdentifierKind, id?: string | null, scope?: string | null, ...names: string[]) {
    return [searchText(kind, id, scope), ...names].join(' ').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
  }
  async function resolve(items: DisplayIdentifier[]) {
    await Promise.all(items.map(item => enqueue({ ...item, scope: item.kind === 'task' || item.kind === 'trajectory' ? item.scope || '' : '' })))
  }
  return { displayId, searchText, matches, resolve }
}

const identifiers = createDisplayIdentifierStore(requestIdentifiers)
export const useDisplayIdentifiers = () => identifiers
