import { describe, expect, it } from 'vitest'
import { collectionStatus, evaluationStatus, formatSuccessRate, verdictLabel, checklistSourceLabel, evaluationSourceLabel } from './modelIteration'

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
  it('labels stored checklist provenance and preserves the actual historical source', () => {
    expect(checklistSourceLabel('gold_id')).toBe('固定清单 · 用例编号')
    expect(checklistSourceLabel('gold_task')).toBe('固定清单 · 任务文本')
    expect(checklistSourceLabel('llm')).toBe('自动生成')
    expect(checklistSourceLabel()).toBe('—')
    expect(evaluationSourceLabel({ execution_mode: 'evaluate_only', source: { source_run_id: 'source-1' } })).toBe('source-1')
    expect(evaluationSourceLabel({ source: { path: 'D:/旧轨迹' } })).toBe('D:/旧轨迹')
  })
})
