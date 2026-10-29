import { describe, expect, it } from 'vitest'
import { modelDraft, modelDefaults, modelOptions, modelValidationIssue, modelBlockedReason } from './pipelineModels'
import type { AgentCapabilities } from '@/phoneFactoryApi'

describe('PipelineAgent model configuration', () => {
  it('preserves legacy omission and does not send retained pipeline settings to NewAgent', () => {
    expect(modelOptions('pipelineagent', modelDraft())).toEqual({})
    const draft = { ...modelDraft(), execution_model: 'execute-v2', planner_custom: true }
    expect(modelOptions('newagent', draft)).toEqual({})
    expect(modelBlockedReason(null, 'newagent', draft)).toBe('')
    expect(() => modelOptions('pipelineagent', draft)).toThrow('同时填写')
  })
  it('copies defaults, trims values and only serializes explicitly selected roles', () => {
    const defaults = { vla: 'http://execute/v1', execution_model: 'execute-v1', planner_config: { base_url: 'http://plan/v1', model: 'plan-v1', api_key_env: 'PLAN_KEY' } }
    const draft = modelDraft(defaults)
    draft.execution_model = ' execute-v2 '
    draft.planner_model = ' plan-v2 '
    const options = modelOptions('pipelineagent', draft)
    expect(options).toEqual({ execution_model: 'execute-v2', planner_config: { base_url: 'http://plan/v1', model: 'plan-v2', api_key_env: 'PLAN_KEY' } })
    expect(defaults.execution_model).toBe('execute-v1')
    expect(defaults.planner_config.model).toBe('plan-v1')
    draft.planner_custom = false
    expect(modelDefaults(' http://execute/v2 ', draft)).toEqual({ vla: 'http://execute/v2', execution_model: 'execute-v2', planner_config: null })
  })
  it('requires a valid URL and an environment variable name instead of credentials', () => {
    const draft = { ...modelDraft(), planner_custom: true, planner_base_url: 'host:8000/v1/chat/completions', planner_model: 'actual-model' }
    expect(modelValidationIssue(draft)).toBe('')
    for (const address of ['ftp://host/v1', 'http://user:secret@host/v1', 'https://host/v1?key=secret']) {
      draft.planner_base_url = address
      expect(modelValidationIssue(draft)).toContain('HTTP(S)')
    }
    draft.planner_base_url = 'https://host/v1/'
    draft.planner_api_key_env = 'sk-secret'
    expect(modelValidationIssue(draft)).toContain('环境变量名')
  })
  it('requires the collector override capability only when overrides are requested', () => {
    const draft = modelDraft()
    expect(modelBlockedReason({}, 'pipelineagent', draft)).toBe('')
    draft.execution_model = 'actual-model'
    expect(modelBlockedReason({}, 'pipelineagent', draft)).toContain('模型覆盖能力')
    expect(modelBlockedReason({ pipelineagent: { model_overrides: true } } as AgentCapabilities, 'pipelineagent', draft)).toBe('')
  })
})
