import { describe, expect, it } from 'vitest'
import { collectionStatus, evaluationStatus, formatSuccessRate, verdictLabel } from './modelIteration'

describe('evaluation presentation', () => {
  it('does not display an unscored run as zero success', () => {
    expect(formatSuccessRate(null)).toBe('—')
    expect(formatSuccessRate(0)).toBe('0.0%')
    expect(formatSuccessRate(1 / 3)).toBe('33.3%')
  })
  it('distinguishes technical review and collection failure from model verdict failure', () => {
    expect(evaluationStatus('needs_review')).toBe('待复核')
    expect(evaluationStatus('retry_wait')).toBe('等待重试')
    expect(collectionStatus('failed')).toBe('采集失败')
    expect(verdictLabel('fail')).toBe('失败')
    expect(verdictLabel('unchecked')).toBe('未判定')
  })
})
