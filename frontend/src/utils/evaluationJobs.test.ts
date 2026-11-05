import { describe, expect, it } from 'vitest'
import { evaluationJobCounts, evaluationJobPhase, evaluationPreviewSession, evaluationSourceKey } from './evaluationJobs'

function memoryStorage() {
  const values = new Map<string, string>()
  return { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => { values.set(key, value) } }
}
describe('offline evaluation browser session', () => {
  it('restores frozen source and request identities without storing trajectory content', () => {
    const storage = memoryStorage(), source = { source_path: 'D:/轨迹/用例01' }, key = evaluationSourceKey(source)
    const entry = { source, request_id: 'same-request', preview_id: 'preview-a', updated_at: 1 }
    evaluationPreviewSession(storage, 'api-a').put(key, entry)
    expect(evaluationPreviewSession(storage, 'api-a').selected()).toMatchObject({ source, request_id: 'same-request', preview_id: 'preview-a' })
    expect(evaluationPreviewSession(storage, 'api-b').selected()).toBeUndefined()
    expect(evaluationSourceKey({ source_path: ' D:/轨迹/用例01 ', request_id: 'other' })).toBe(key)
  })
  it('stores late acknowledgements without replacing the current source selection', () => {
    const storage = memoryStorage(), store = evaluationPreviewSession(storage), a = { source_run_id: 'a' }, b = { source_run_id: 'b' }
    store.put(evaluationSourceKey(a), { source: a, request_id: 'request-a', updated_at: 1 })
    store.put(evaluationSourceKey(b), { source: b, request_id: 'request-b', updated_at: 1 })
    store.put(evaluationSourceKey(a), { source: a, request_id: 'request-a', preview_id: 'accepted-a', updated_at: 1 }, false)
    expect(evaluationPreviewSession(storage).selected()?.request_id).toBe('request-b')
    expect(evaluationPreviewSession(storage).get(evaluationSourceKey(a))?.preview_id).toBe('accepted-a')
  })
  it('retains in-page identities when session persistence is unavailable', () => {
    const store = evaluationPreviewSession({ getItem() { throw new Error('blocked') }, setItem() { throw new Error('quota') } })
    const source = { source_run_id: 'a' }
    store.put(evaluationSourceKey(source), { source, request_id: 'same', updated_at: 1 })
    expect(store.selected()?.request_id).toBe('same')
    expect(evaluationPreviewSession({ getItem: () => '{broken', setItem() {} }).selected()).toBeUndefined()
  })
  it('shows measured counters and stages without inventing a scan percentage', () => {
    expect(evaluationJobPhase('queued', 'preparing')).toBe('等待后台处理')
    expect(evaluationJobPhase('scanning')).toBe('扫描轨迹目录')
    expect(evaluationJobPhase('validating_inputs')).toBe('校验原件并登记评测输入')
    expect(evaluationJobPhase('preparing_inputs')).toBe('准备历史评测副本')
    expect(evaluationJobCounts({ discovered: 23, checked: 5, eligible: 4, excluded: 1 })).toBe('已发现 23 条 · 已检查 5 条 · 可评测 4 条 · 已排除 1 条')
    expect(evaluationJobCounts({ discovered: 23, total: 300 })).not.toContain('%')
    expect(evaluationJobCounts()).toBe('')
  })
})
