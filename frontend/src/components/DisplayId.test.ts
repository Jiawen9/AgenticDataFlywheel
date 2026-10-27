import { describe, expect, it, vi } from 'vitest'
import { createRenderer, createSSRApp, defineComponent, h, nextTick, ssrContextKey } from 'vue'
import { renderToString } from '@vue/server-renderer'
import DisplayId from './DisplayId.vue'

vi.mock('@/composables/useDisplayIdentifiers', () => ({ useDisplayIdentifiers: () => ({ displayId: () => 'cr261026-001' }) }))
describe('display identifier control', () => {
  it('shows the alias with accessible full ID inspection and a separate copy control', async () => {
    const app = createSSRApp(DisplayId, { kind: 'run', id: 'cr_full_internal_id' })
    app.component('el-popover', defineComponent({ setup: (_props, { slots }) => () => h('span', [slots.reference?.(), slots.default?.()]) }))
    const html = await renderToString(app)
    expect(html).toContain('cr261026-001')
    expect(html).toContain('查看完整编号：cr_full_internal_id')
    expect(html).toContain('复制完整编号 cr261026-001')
  })
  it('copies the original ID, never the displayed alias', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    const renderer = createRenderer<object, object>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {}, insert() {}, remove() {} })
    const app = renderer.createApp({ ...DisplayId, render: () => h('div') }, { kind: 'run', id: 'cr_full_internal_id' })
    app.provide(ssrContextKey, { modules: new Set() }); app.mount({})
    try {
      await (app._instance!.setupState as Record<string, any>).copy(); await nextTick()
      expect(writeText).toHaveBeenCalledWith('cr_full_internal_id')
    } finally { app.unmount(); vi.unstubAllGlobals() }
  })
})
