import type { AgentCapabilities, AgentType, ModelOverrides, PipelineModelDefaults } from '@/phoneFactoryApi'

export interface PipelineModelDraft {
  execution_model: string
  planner_custom: boolean
  planner_base_url: string
  planner_model: string
  planner_api_key_env: string
}

export function modelDraft(defaults?: PipelineModelDefaults): PipelineModelDraft {
  return {
    execution_model: defaults?.execution_model || '',
    planner_custom: Boolean(defaults?.planner_config),
    planner_base_url: defaults?.planner_config?.base_url || '',
    planner_model: defaults?.planner_config?.model || '',
    planner_api_key_env: defaults?.planner_config?.api_key_env || '',
  }
}

export function modelValidationIssue(draft: PipelineModelDraft): string {
  if (!draft.planner_custom) return ''
  if (!draft.planner_base_url.trim() || !draft.planner_model.trim()) return '请同时填写规划模型的接口地址和模型名'
  try {
    const value = draft.planner_base_url.trim()
    const url = new URL(value.includes('://') ? value : `http://${value}`)
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.search || url.hash) throw new Error()
  } catch { return '规划接口必须为有效的 HTTP(S) 地址，且不包含认证信息、查询参数或片段' }
  const key = draft.planner_api_key_env.trim()
  if (key && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) return '密钥环境变量名格式无效，请勿填写密钥值'
  return ''
}

export function modelOptions(agent: AgentType, draft: PipelineModelDraft): ModelOverrides {
  if (agent !== 'pipelineagent') return {}
  const issue = modelValidationIssue(draft)
  if (issue) throw new Error(issue)
  return {
    ...(draft.execution_model.trim() ? { execution_model: draft.execution_model.trim() } : {}),
    ...(draft.planner_custom ? { planner_config: {
      base_url: draft.planner_base_url.trim(), model: draft.planner_model.trim(),
      ...(draft.planner_api_key_env.trim() ? { api_key_env: draft.planner_api_key_env.trim() } : {}),
    } } : {}),
  }
}

export function modelDefaults(vla: string, draft: PipelineModelDraft): PipelineModelDefaults {
  const options = modelOptions('pipelineagent', draft)
  return { vla: vla.trim(), execution_model: options.execution_model || '', planner_config: options.planner_config || null }
}

export function modelBlockedReason(capabilities: AgentCapabilities | null, agent: AgentType, draft: PipelineModelDraft): string {
  if (agent !== 'pipelineagent') return ''
  const issue = modelValidationIssue(draft)
  if (issue) return issue
  if ((draft.execution_model.trim() || draft.planner_custom) && !capabilities?.pipelineagent?.model_overrides) return '采集端尚未声明模型覆盖能力，请升级 9011 与内置 PipelineAgent 后刷新引擎'
  return ''
}
