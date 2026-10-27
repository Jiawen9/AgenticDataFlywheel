import type { AgentCapabilities, AgentType } from '@/phoneFactoryApi'

export const agentLabel = (type?: string) => type === 'pipelineagent' ? 'PipelineAgent' : 'NewAgent'

export function agentBlockedReason(capabilities: AgentCapabilities | null, type: AgentType, logprobs = false, evaluation = false): string {
  const capability = capabilities?.[type]
  if (!capability) return type === 'newagent' ? '' : '请刷新采集引擎能力，确认采集电脑已配置 PipelineAgent'
  if (!capability.available || !capability.collection) return capability.reason || `${agentLabel(type)} 尚不可用`
  if (logprobs && !capability.capture_logprobs) return `${agentLabel(type)} 尚不支持 Logprobs 采集`
  if (evaluation && !capability.model_iteration_evaluation) return `${agentLabel(type)} 尚不支持模型迭代评测`
  return ''
}
