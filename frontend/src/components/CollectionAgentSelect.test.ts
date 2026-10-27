import { describe, expect, it } from 'vitest'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import CollectionAgentSelect from './CollectionAgentSelect.vue'

describe('collection engine selector', () => {
  it('disables PipelineAgent until the collector declares it available', async () => {
    const render = async (available: boolean) => {
      const app = createSSRApp(CollectionAgentSelect, { modelValue: 'newagent', capabilities: { pipelineagent: { available, collection: available } } })
      app.component('el-select', defineComponent({ setup: (_props, { slots }) => () => h('select', slots.default?.()) }))
      app.component('el-option', defineComponent({ props: ['value', 'label', 'disabled'], setup: props => () => h('option', { value: props.value, disabled: props.disabled }, props.label) }))
      return renderToString(app)
    }
    expect(await render(false)).toContain('value="pipelineagent" disabled')
    expect(await render(true)).not.toContain('value="pipelineagent" disabled')
  })
})
