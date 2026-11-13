import { describe, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import QualityView from './QualityView.vue'

const context = vi.hoisted(() => ({ managed: false }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }), useRouter: () => ({ replace: vi.fn(), push: vi.fn() }) }))
vi.mock('@/composables/usePipelineContext', () => ({ usePipelineContext: () => ({ readOnly: ref(context.managed), isPipelineRoute: ref(context.managed), managed: ref(context.managed), historyOnly: ref(false), pipeline: ref(null), refresh: vi.fn() }) }))
vi.mock('@/composables/useBatchLifecycle', () => ({ useBatchLifecycle: () => ({ notice: ref(null) }) }))
vi.mock('@/composables/useDisplayIdentifiers', () => ({ useDisplayIdentifiers: () => ({ displayId: (_kind: string, id: string) => id, matches: () => true }) }))
vi.mock('@/components/PipelineStatusBar.vue', () => ({ default: { render: () => null } }))
vi.mock('@/components/QualityFusionSettings.vue', () => ({ default: defineComponent({ props: { disabled: Boolean }, setup: props => () => h('section', { 'data-quality-settings': 'local', 'data-disabled': String(props.disabled) }, '平台质检模型') }) }))

// Render the actual page template with no selected batch or tree. Global model
// settings must remain reachable even from a read-only automatic Pipeline page.
describe('quality model configuration entry', () => {
  it.each([false, true])('is available without a selected run (managed=%s)', async managed => {
    context.managed = managed
    const app = createSSRApp(QualityView)
    const pass = defineComponent({ setup: (_props, { slots }) => () => h('div', slots.default?.()) })
    for (const tag of ['select', 'option', 'alert', 'empty', 'button', 'progress', 'checkbox', 'tag', 'table', 'table-column']) app.component('el-' + tag, pass)
    app.component('el-drawer', { render: () => null })
    app.component('router-link', pass)
    app.directive('loading', {})
    const html = await renderToString(app)
    expect(html).toContain('data-quality-settings="local"')
    expect(html).toContain('data-disabled="false"')
    expect(html).toContain('平台质检模型')
  })
})
