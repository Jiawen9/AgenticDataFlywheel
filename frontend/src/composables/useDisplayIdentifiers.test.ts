import { describe, expect, it, vi } from 'vitest'
import { computed } from 'vue'
import { createDisplayIdentifierStore, type DisplayIdentifier, type ResolvedDisplayIdentifier } from './useDisplayIdentifiers'

const mapped = (item: DisplayIdentifier, displayId: string): ResolvedDisplayIdentifier => ({ ...item, scope: item.scope || '', display_id: displayId, mapped: true })
describe('display identifier resolution', () => {
  it('batches one render, coalesces duplicate calls and updates reactive labels without changing identities', async () => {
    const resolver = vi.fn(async (items: DisplayIdentifier[]) => items.map((item, i) => mapped(item, `b261026-${String(i + 1).padStart(3, '0')}`)))
    const store = createDisplayIdentifierStore(resolver, { autoResolve: () => true })
    const original = 'manual_' + 'a'.repeat(64)
    const label = computed(() => store.displayId('batch', original))
    expect(label.value).toBe(original)
    store.displayId('batch', original); store.displayId('batch', 'second')
    await store.resolve([{ kind: 'batch', id: original }, { kind: 'batch', id: 'second' }])
    expect(resolver).toHaveBeenCalledTimes(1)
    expect(resolver.mock.calls[0]![0]).toEqual([{ kind: 'batch', id: original, scope: '' }, { kind: 'batch', id: 'second', scope: '' }])
    expect(label.value).toBe('b261026-001')
    expect(store.matches(original, 'batch', original)).toBe(true)
    expect(store.matches('b261026-001', 'batch', original)).toBe(true)
    expect(store.matches('应用名称', 'batch', original, '', '应用名称')).toBe(true)
  })
  it('uses batch scope and full identity even if the endpoint returns out of order', async () => {
    const store = createDisplayIdentifierStore(async items => items.map(item => mapped(item, item.scope === 'A' ? 'tr261026-001' : 'tr261026-002')).reverse())
    await store.resolve([{ kind: 'trajectory', id: 'step001', scope: 'A' }, { kind: 'trajectory', id: 'step001', scope: 'B' }])
    expect(store.displayId('trajectory', 'step001', 'A')).toBe('tr261026-001')
    expect(store.displayId('trajectory', 'step001', 'B')).toBe('tr261026-002')
  })
  it('keeps missing historical IDs and retries short-lived negative entries after registration', async () => {
    let now = 0
    const resolver = vi.fn().mockResolvedValueOnce([{ kind: 'run', id: 'eval_long', scope: '', display_id: 'eval_long', mapped: false }]).mockResolvedValueOnce([mapped({ kind: 'run', id: 'eval_long' }, 'eval261026-001')])
    const store = createDisplayIdentifierStore(resolver, { clock: () => now })
    const items = [{ kind: 'run' as const, id: 'eval_long' }]
    await store.resolve(items); await store.resolve(items)
    expect(store.displayId('run', 'eval_long')).toBe('eval_long'); expect(resolver).toHaveBeenCalledTimes(1)
    now = 30_001; await store.resolve(items)
    expect(store.displayId('run', 'eval_long')).toBe('eval261026-001')
  })
  it('degrades safely on failure and limits request batches', async () => {
    const resolver = vi.fn(async () => { throw new Error('503') })
    const store = createDisplayIdentifierStore(resolver)
    await store.resolve(Array.from({ length: 401 }, (_, i) => ({ kind: 'batch', id: String(i) })))
    expect(resolver).toHaveBeenCalledTimes(3)
    expect(store.displayId('batch', '40')).toBe('40')
  })
})
