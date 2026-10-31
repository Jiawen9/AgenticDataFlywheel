import { describe, expect, it } from 'vitest'
import { repeatBlockedReason, repeatValidationIssue } from './collectionRepeats'
import type { AgentCapabilities } from '@/phoneFactoryApi'

describe('collection repetitions', () => {
  it('validates exact integer bounds and prevents unsupported multi-trajectory dispatch', () => {
    for (const value of [1, 2, 100]) expect(repeatValidationIssue(value)).toBe('')
    for (const value of [0, -1, 1.5, 101, NaN, Infinity]) expect(repeatValidationIssue(value)).not.toBe('')
    expect(repeatBlockedReason(null, 'pipelineagent', 1)).toBe('')
    expect(repeatBlockedReason(null, 'pipelineagent', 2)).toContain('升级采集端')
    const capabilities = { pipelineagent: { repeat_times: true, max_repeat_times: 10 } } as AgentCapabilities
    expect(repeatBlockedReason(capabilities, 'pipelineagent', 10)).toBe('')
    expect(repeatBlockedReason(capabilities, 'pipelineagent', 11)).toContain('10')
    expect(repeatBlockedReason(capabilities, 'newagent', 2)).toContain('升级采集端')
  })
})
