import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { describe, expect, it } from 'vitest'
import ActionImage from './ActionImage.vue'
import type { ActionPayload, CoordinateInfo } from '@/types'

async function draw(action: ActionPayload, coordinateInfo?: CoordinateInfo, actionsBox = '', allowZoom = true) {
  const app = createSSRApp({ render: () => h(ActionImage, { imageUrl: 'screen.jpg', action, coordinateInfo, actionsBox, agentType: 'newagent', allowZoom }) })
  app.component('el-empty', { render: () => h('div') })
  app.component('el-dialog', { render: () => null })
  return renderToString(app)
}
describe('ActionImage overlays', () => {
  it('offers a shared zoom preview and suppresses a nested zoom entry', async () => {
    expect(await draw({ action: 'click', coordinate: [500, 500] })).toContain('放大预览')
    expect(await draw({ action: 'click', coordinate: [500, 500] }, { space: 'normalized' }, '', false)).not.toContain('放大预览')
  })
  it('draws click and long press with independent pixel canvas axes', async () => {
    for (const action of ['click', 'long_press']) {
      const html = await draw({ action, coordinate: [540, 1800] }, { space: 'pixel', width: 1080, height: 2400 })
      expect(html).toContain('cx="540"'); expect(html).toContain('cy="1755"')
    }
  })
  it('draws swipe endpoints without normalizing them by 1000', async () => {
    const html = await draw({ action: 'swipe', start_coordinate: [540, 1800], end_coordinate: [270, 600] }, { space: 'pixel', width: 1080, height: 2400 })
    expect(html).toContain('x1="540"'); expect(html).toContain('y1="1755"')
    expect(html).toContain('x2="270"'); expect(html).toContain('y2="585"')
  })
  it('keeps bbox but does not invent a swipe path for unknown coordinates', async () => {
    const html = await draw({ action: 'swipe' }, { space: 'unknown', reason: 'source_unknown' }, 'swipe(bbox=<bbox>[10,20,110,220]</bbox>, direction=left)')
    expect(html).toContain('x="10"'); expect(html).toContain('y="20"')
    expect(html).not.toContain('<line'); expect(html).toContain('source_unknown')
  })
  it('preserves legacy normalized rendering and bbox fallback', async () => {
    const click = await draw({ action: 'click', coordinate: [500, 250] })
    expect(click).toContain('cx="540"'); expect(click).toContain('cy="585"')
    expect(await draw({ action: 'swipe' }, undefined, 'swipe(bbox=<bbox>[10,20,110,220]</bbox>, direction=left)')).toContain('<line')
  })
})
