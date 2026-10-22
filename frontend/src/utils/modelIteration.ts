import type { EvaluatorConfig } from '@/phoneFactoryApi'

export function defaultEvaluatorConfig(): EvaluatorConfig {
  return { text: { base_url: '', model: '', api_key_env: 'PHONE_FACTORY_EVALUATOR_TEXT_API_KEY' }, vision: { base_url: '', model: '', api_key_env: 'PHONE_FACTORY_EVALUATOR_VISION_API_KEY' } }
}
export const formatSuccessRate = (value: number | null) => value == null || !Number.isFinite(value) ? '—' : `${(value * 100).toFixed(1)}%`
export const evaluationStatus = (value: string) => ({ pending: '待评测', running: '评测中', retry_wait: '等待重试', completed: '已评测', needs_review: '待复核', collection_failed: '采集失败' }[value] || value)
export const collectionStatus = (value: string) => ({ pending: '待采集', running: '采集中', completed: '已采集', failed: '采集失败' }[value] || value)
export const verdictLabel = (value?: string | null) => ({ success: '成功', partial_success: '部分成功', fail: '失败', unchecked: '未判定' }[value || ''] || '—')
