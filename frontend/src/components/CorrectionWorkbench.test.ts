import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api'
import { useCorrectionWorkspace } from '@/composables/useCorrectionWorkspace'
import type { CorrectionGroup, CorrectionRow, CorrectionSession } from '@/types'
import CorrectionWorkbench from './CorrectionWorkbench.vue'

function groupFixture(): CorrectionGroup {
  return { group_id: 'group_1', storage_revision: 7, rows: [{
    excel_row: 39, step: 1, agent_type: 'pipelineagent', image: 'case/trajectory/step001_vla_input.jpg',
    actions: '{"action":"click","coordinate":[540,1800]}', actions_box: '', original_actions_box: '',
    coordinate_info: { space: 'pixel', width: 1080, height: 2400 }, coordinate_info_version: 1, coordinate_info_revision: 7,
  } as CorrectionRow] } as CorrectionGroup
}
async function draw(group: CorrectionGroup) {
  const app = createSSRApp({ render: () => h(CorrectionWorkbench, {
    sessionId: 's261109-001', group, row: group.rows[0]!, revision: 0,
    storageRevision: group.storage_revision, saving: false,
  }) })
  for (const name of ['el-tag', 'el-select', 'el-option', 'el-input', 'el-alert', 'el-button', 'el-table', 'el-table-column', 'el-empty']) {
    app.component(name, { setup: (_, { attrs, slots }) => () => h(name === 'el-button' ? 'button' : 'div', attrs,
      name === 'el-table' ? undefined : slots.default?.()) })
  }
  return renderToString(app)
}
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('correction detail compatibility', () => {
  it('renders original and saved pixels through existing task/PATCH routes while the new route returns HTML 404', async () => {
    const group = groupFixture()
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/tasks/group_1')) return Response.json({ group })
      if (url.endsWith('/rows/39') && init?.method === 'PATCH') {
        const patch = JSON.parse(String(init.body))
        return Response.json({ storage_revision: 8, group: { group_id: group.group_id, storage_revision: 8 }, row: {
          ...group.rows[0], actions: patch.actions, coordinate_info_revision: 8,
        } })
      }
      return new Response('<html>Not Found</html>', { status: 404 })
    })
    vi.stubGlobal('fetch', fetcher)
    const ws = useCorrectionWorkspace(api, { error: vi.fn(), actionDecision: async () => 'save' })
    ws.setSession({ session_id: 's261109-001', storage_revision: 7, groups: [group] } as CorrectionSession)
    await ws.loadGroup('group_1')
    expect(await draw(ws.activeGroup.value!)).toContain('cy="1755"')
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(await ws.saveAction('{"action":"long_press","coordinate":[540,1800]}')).toBe(true)
    expect(await draw(ws.activeGroup.value!)).toContain('cy="1755"')
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(fetcher.mock.calls.some(([url]) => url.includes('coordinate-info'))).toBe(false)
  })
  it('keeps authoritative unknown inline without making another request', async () => {
    const group = groupFixture()
    group.rows[0]!.coordinate_info = { space: 'unknown', reason: 'old_manual_coordinate_unknown' }
    const lookup = vi.spyOn(api, 'correctionCoordinateInfo')
    const html = await draw(group)
    expect(html).toContain('old_manual_coordinate_unknown')
    expect(html).not.toContain('<circle')
    expect(lookup).not.toHaveBeenCalled()
  })
  it.each(['absent', 'legacy-contract', 'legacy-string'] as const)('uses the old lookup fallback for %s metadata', async (kind) => {
    const group = groupFixture(), row = group.rows[0]!
    group.storage_revision = { absent: 71, 'legacy-contract': 72, 'legacy-string': 73 }[kind]
    delete row.coordinate_info_version
    if (kind === 'absent') delete row.coordinate_info
    if (kind === 'legacy-string') Object.assign(row, { coordinate_info: '{"space":"pixel","width":1080,"height":2400}' })
    const lookup = vi.spyOn(api, 'correctionCoordinateInfo').mockResolvedValue({ coordinate_info: { space: 'pixel', width: 1080, height: 2400 }, storage_revision: group.storage_revision! })
    await draw(group)
    expect(lookup).toHaveBeenCalledWith('s261109-001', 39, group.storage_revision)
  })
})
