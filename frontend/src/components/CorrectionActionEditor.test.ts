import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { describe, expect, it } from 'vitest'
import CorrectionActionEditor from './CorrectionActionEditor.vue'
import type { CoordinateInfo, CorrectionRow } from '@/types'

async function draw(actions: string, coordinateInfo: CoordinateInfo) {
  const row = { excel_row: 2, step: 1, actions, actions_box: 'click(bbox=<bbox>[10,20,110,220]</bbox>)', original_actions_box: '' } as CorrectionRow
  const app = createSSRApp({ render: () => h(CorrectionActionEditor, { row, imageUrl: 'screen.jpg', coordinateInfo }) })
  for (const name of ['el-tag', 'el-select', 'el-option', 'el-input', 'el-alert', 'el-button']) {
    app.component(name, { setup: (_, { attrs, slots }) => () => h(name === 'el-button' ? 'button' : 'div', attrs, slots.default?.()) })
  }
  return renderToString(app)
}
describe('correction editor rendering', () => {
  it('projects source pixels while keeping bbox in image coordinates', async () => {
    const html = await draw('{"action":"long_press","coordinate":[540,1800]}', { space: 'pixel', width: 1080, height: 2400 })
    expect(html).toContain('cx="540"'); expect(html).toContain('cy="1755"')
    expect(html).toContain('x="10"'); expect(html).toContain('y="20"')
    expect(html).toContain('坐标 540, 1800')
  })
  it('retains image and bbox when coordinates are unknown without disabling the form', async () => {
    const html = await draw('{"action":"click","coordinate":[1200,2000]}', { space: 'unknown', reason: 'old_manual_coordinate_unknown' })
    expect(html).not.toContain('<circle'); expect(html).toContain('x="10"')
    expect(html).toContain('old_manual_coordinate_unknown')
    expect(html).not.toContain('disabled')
    expect(html).toContain('坐标 1200, 2000')
  })
  it('continues to offer text input and save for unknown coordinates', async () => {
    const html = await draw('{"action":"type","text":"中文"}', { space: 'unknown' })
    expect(html).toContain('placeholder="输入内容"')
    expect(html).toContain('保存动作'); expect(html).not.toContain('disabled')
  })
})
