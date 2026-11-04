import { describe, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import PhoneRunDiagnostics from './PhoneRunDiagnostics.vue'
import payload from '@/test-fixtures/collector-diagnostics.json'
import type { CollectionDiagnosticsState } from '@/composables/useCollectionDiagnostics'

async function render(allowResume: boolean, supported = true, control = false, detailed = true, revision = false) {
  // The phone objects come from real DiagnosticService._phone/RuntimeTelemetry,
  // not a parallel JS fixture that invents flattened backend fields.
  const state = { phoneControlSupported: ref(control), selectedRun: ref({ agent_type: 'pipelineagent' }), ownershipFor: () => ({ availability: 'free', stale: false, owners: [{ execution_id: 'released-owner', availability: 'free', supported_actions: [] }, { execution_id: 'old-owner', availability: 'free', supported_actions: ['reconcile'], state_revision: 2, ownership_token: 'proof' }] }), controlFor: () => undefined, checkOwnership: vi.fn(), controlPhone: vi.fn(), selectedRunId: ref('run-contract'), snapshot: ref({ ...payload, collection_resume: { version: 1, device_config_revision: revision ? 1 : 0 }, phones: payload.phones.map(phone => ({ ...phone, io_error: { operation: 'replace', path: 'workers/active_child.json', winerror: 32 } })), supported, run_id: 'run-contract', summary: { total: 2, healthy: 0, warning: 1, error: 1 } }), error: ref(''), stale: ref(false), loading: ref(false), now: ref(Date.parse(payload.observed_at) + 5000), refresh: vi.fn(), selectRun: vi.fn(), operationFor: () => undefined, continuePhones: vi.fn() } as unknown as CollectionDiagnosticsState
  const app = createSSRApp(PhoneRunDiagnostics, { state, runs: [{ run_id: 'run-contract', status: 'partial' }], allowResume, detailed })
  const passthrough = defineComponent({ setup: (_p, { slots }) => () => h('div', slots.default?.()) })
  for (const name of ['el-select', 'el-option', 'el-button', 'el-alert', 'el-input', 'el-checkbox']) app.component(name, passthrough)
  return renderToString(app)
}
describe('real collector diagnostic payload presentation', () => {
  it('shows nested model role, exact reason, confidence, case repeat and evidence instead of hiding them behind stale generic text', async () => {
    const html = await render(true)
    expect(html).toContain('模型请求失败'); expect(html).toContain('planner'); expect(html).toContain('qwen3-8b')
    expect(html).toContain('已有明确依据'); expect(html).toContain('需要人工确认')
    expect(html).toContain('case-7'); expect(html).toContain('第 3 / 10 次')
    expect(html).toContain('HTTP 502'); expect(html).toContain('phone_state：model_http'); expect(html).toContain('30 秒')
    expect(html).toContain('阶段与模型信息超过 20 秒未更新或尚未提供')
    expect(html.match(/继续该手机/g)).toHaveLength(2)
    expect(html).not.toMatch(/<details[^>]*open/)
  })
  it('shows current ownership separately and only exposes advertised scoped actions', async () => {
    const html = await render(true, true, true)
    expect(html).toContain('当前占用：空闲'); expect(html).toContain('old-owner'); expect(html).toContain('对账释放')
    expect(html).not.toContain('停止该手机')
    expect(await render(true)).not.toContain('对账释放')
  })
  it('keeps released history out of the summary and gates device revisions by capability', async () => {
    const summary = await render(true, true, true, false)
    expect(summary).not.toContain('released-owner'); expect(summary).toContain('old-owner')
    const detail = await render(true, true, true)
    expect(detail).toContain('已退出的历史执行：released-owner'); expect(detail).toContain('WinError 32')
    expect(detail).toContain('workers/active_child.json'); expect(detail).not.toContain('修订设备配置（可选）')
    expect(await render(true, true, true, true, true)).toContain('修订设备配置（可选）')
  })
  it('does not expose collection continuation for evaluation or unsupported collectors', async () => {
    expect(await render(false)).not.toContain('继续该手机')
    expect(await render(true, false)).not.toContain('继续该手机')
  })
})
