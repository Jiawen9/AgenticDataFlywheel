import type { EvaluationJobProgress, EvaluationSourceInput, StartEvaluationOptions } from '@/phoneFactoryApi'

export interface PreviewSessionEntry {
  source: EvaluationSourceInput
  request_id: string
  preview_id?: string
  updated_at: number
  start_request?: StartEvaluationOptions
  control?: { action: 'cancel' | 'retry'; request_id: string; expected_revision: number }
}
interface PreviewSession { version: 1; selected: string; entries: Record<string, PreviewSessionEntry> }
export function evaluationSourceKey(source: EvaluationSourceInput): string {
  return JSON.stringify(source.source_path !== undefined ? { source_path: source.source_path.trim() } : { source_run_id: source.source_run_id })
}

/** Only request identities and their frozen inputs are stored, never trajectory files. */
export function evaluationPreviewSession(storage?: Pick<Storage, 'getItem' | 'setItem'>, scope = import.meta.env.VITE_API_BASE_URL || 'same-origin') {
  const storageKey = `adf:offline-evaluation:v1:${scope}`
  let value: PreviewSession = { version: 1, selected: '', entries: {} }
  try {
    storage ??= globalThis.sessionStorage
    const saved = JSON.parse(storage?.getItem(storageKey) || 'null') as PreviewSession | null
    if (saved?.version === 1 && saved.entries && typeof saved.entries === 'object') {
      for (const [key, entry] of Object.entries(saved.entries)) {
        if (entry?.source && typeof entry.request_id === 'string' && typeof entry.updated_at === 'number'
          && evaluationSourceKey(entry.source) === key) value.entries[key] = entry
      }
      if (typeof saved.selected === 'string') value.selected = saved.selected
    }
  } catch { /* A disabled or corrupt browser store must not block a new preview. */ }
  function persist() {
    const ordered = Object.entries(value.entries).sort((a, b) => b[1].updated_at - a[1].updated_at)
    value.entries = Object.fromEntries(ordered.slice(0, 32))
    try { storage?.setItem(storageKey, JSON.stringify(value)) } catch { /* The active page still retains request identities. */ }
  }
  return {
    selected: () => value.entries[value.selected],
    get: (key: string) => value.entries[key],
    put(key: string, entry: PreviewSessionEntry, select = true) {
      value.entries[key] = { ...entry, updated_at: Date.now() }
      if (select) value.selected = key
      persist()
    },
  }
}

export function evaluationJobPhase(phase?: string, status?: string): string {
  const phases: Record<string, string> = { queued: '等待后台处理', scanning: '扫描轨迹目录', discovering: '扫描轨迹目录', validating: '校验轨迹原件', validating_inputs: '校验原件并登记评测输入', validating_source: '校验轨迹原件', registering_inputs: '登记评测输入', referencing: '登记评测输入', checking: '检查轨迹证据', hashing: '校验文件摘要', freezing: '保存评测规则快照', freezing_profile: '保存评测规则快照', preparing: '登记评测输入', copying: '准备历史评测副本', ready: '输入准备完成', evaluating: '评测中', running: '评测中', completed: '检查完成', cancelled: '已取消', failed: '准备失败', paused: '准备已暂停，等待恢复', waiting_recovery: '等待恢复' }
  if (phase === 'cancelling') return '正在取消检查'
  if (phase === 'retrying') return '正在重新排队'
  if (phase === 'syncing') return '同步预览结果'
  if (phase === 'preparing_inputs') return '准备历史评测副本'
  return phases[phase || ''] || phases[status || ''] || '后台处理中'
}
export function evaluationJobCounts(progress?: EvaluationJobProgress): string {
  if (!progress) return ''
  const labels: Array<[keyof EvaluationJobProgress, string]> = [['discovered', '已发现'], ['checked', '已检查'], ['eligible', '可评测'], ['excluded', '已排除'], ['prepared', '输入已准备']]
  return labels.flatMap(([key, label]) => typeof progress[key] === 'number' ? [`${label} ${progress[key]} 条`] : []).join(' · ')
}
