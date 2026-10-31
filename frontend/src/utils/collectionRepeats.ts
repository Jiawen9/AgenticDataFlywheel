import type { AgentCapabilities, AgentType } from '@/phoneFactoryApi'

export const MAX_REPEAT_TIMES = 100
export function repeatValidationIssue(value: number): string {
  return Number.isInteger(value) && value >= 1 && value <= MAX_REPEAT_TIMES ? '' : '每任务采集次数必须为 1 到 100 的整数'
}
export function repeatBlockedReason(capabilities: AgentCapabilities | null, agent: AgentType, value: number): string {
  const invalid = repeatValidationIssue(value)
  if (invalid) return invalid
  if (value === 1) return ''
  const capability = capabilities?.[agent]
  if (capability?.repeat_times !== true) return '采集端尚未声明重复采集能力，请升级采集端并刷新引擎'
  if (value > (capability.max_repeat_times ?? MAX_REPEAT_TIMES)) return `采集端最多支持每任务采集 ${capability.max_repeat_times} 次`
  return ''
}
