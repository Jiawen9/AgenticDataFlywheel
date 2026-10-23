import type { EvaluatorConfig, EvaluationIssue, EvaluationRunMetadata } from '@/phoneFactoryApi'

export function defaultEvaluatorConfig(): EvaluatorConfig {
  return { text: { base_url: '', model: '', api_key_env: 'PHONE_FACTORY_EVALUATOR_TEXT_API_KEY' }, vision: { base_url: '', model: '', api_key_env: 'PHONE_FACTORY_EVALUATOR_VISION_API_KEY' } }
}
export const formatSuccessRate = (value: number | null) => value == null || !Number.isFinite(value) ? '—' : `${(value * 100).toFixed(1)}%`
export const evaluationStatus = (value: string) => ({ pending: '待评测', running: '评测中', retry_wait: '等待重试', completed: '已评测', needs_review: '待复核', collection_failed: '采集失败' }[value] || value)
export const collectionStatus = (value: string) => ({ pending: '待采集', running: '采集中', completed: '已采集', failed: '采集失败' }[value] || value)
export const verdictLabel = (value?: string | null) => ({ success: '成功', partial_success: '部分成功', fail: '失败', unchecked: '未判定' }[value || ''] || '—')
export const checklistSourceLabel = (value?: string) => ({ gold: '固定清单', gold_id: '固定清单 · 用例编号', gold_task: '固定清单 · 任务文本', fixed: '固定清单', llm: '自动生成', generated: '自动生成', auto: '固定优先 / 自动生成' }[value || ''] || value || '—')
export const evaluationIssueText = (value: EvaluationIssue) => typeof value === 'string' ? value : [value.case_id || value.path, value.message || value.error || '数据校验失败'].filter(Boolean).join('：')
export const evaluationModeLabel = (value?: string) => value === 'evaluate_only' ? '已有轨迹评测' : '采集并评测'
export function evaluationSourceLabel(run?: EvaluationRunMetadata) {
  if (!run) return '—'
  const source = run.source
  return source?.run_id || source?.source_run_id || source?.path || source?.source_path || evaluationModeLabel(run.execution_mode)
}
