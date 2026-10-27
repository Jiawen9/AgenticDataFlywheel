import { describe, expect, it } from 'vitest'
import { agentBlockedReason } from './collectionAgents'
import type { AgentCapabilities } from '@/phoneFactoryApi'

const available = { available: true, collection: true, capture_logprobs: true, model_iteration_evaluation: true, offline_evaluation: true }

describe('collection engine capability checks', () => {
  it('keeps historical newagent selectable but requires explicit pipeline support', () => {
    expect(agentBlockedReason(null, 'newagent')).toBe('')
    expect(agentBlockedReason(null, 'pipelineagent')).toContain('PipelineAgent')
    expect(agentBlockedReason({ pipelineagent: { ...available, available: false, reason: 'Python 未配置' } }, 'pipelineagent')).toBe('Python 未配置')
  })
  it('checks capabilities for the selected engine and the requested operation', () => {
    const capabilities: AgentCapabilities = { newagent: available, pipelineagent: { ...available, capture_logprobs: false, model_iteration_evaluation: false } }
    expect(agentBlockedReason(capabilities, 'newagent', true, true)).toBe('')
    expect(agentBlockedReason(capabilities, 'pipelineagent')).toBe('')
    expect(agentBlockedReason(capabilities, 'pipelineagent', true)).toContain('Logprobs')
    expect(agentBlockedReason(capabilities, 'pipelineagent', false, true)).toContain('模型迭代评测')
  })
})
