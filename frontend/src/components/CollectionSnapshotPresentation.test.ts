import { describe, expect, it } from 'vitest'
import { createSSRApp, defineComponent, h, inject, provide, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import ModelIterationPanel from './ModelIterationPanel.vue'
import FactoryRunDetailsDrawer from './FactoryRunDetailsDrawer.vue'

const stale = { stale_collection_snapshot: true, source_collection_snapshot_id: 'cs_original', current_collection_snapshot_id: 'cs_latest' }
const empty = defineComponent({ render: () => h('div') })
function stubs(app: ReturnType<typeof createSSRApp>, tables = true) {
  const pass = defineComponent({ setup: (_props, { slots }) => () => h('div', slots.default?.()) })
  for (const name of ['el-drawer', 'el-select', 'el-button', 'el-radio-group']) app.component(name, pass)
  for (const name of ['el-option', 'el-alert', 'el-input', 'el-radio', 'el-radio-button', 'el-pagination', 'el-popover', ...(tables ? ['el-table', 'el-table-column'] : [])]) app.component(name, empty)
  app.directive('loading', {})
}

describe('historical collection snapshot labels', () => {
  it('shows the selected evaluation and preview snapshot while keeping result contents intact', async () => {
    const run = { run_id: 'eval-old', status: 'completed', execution_mode: 'evaluate_only', ...stale }
    const progress = { selectedRun: ref(run.run_id), results: ref(null), resultError: ref(''), cancelling: ref(false), canCancel: ref(false), resultMetadata: ref(run), busy: ref(false), selectRun() {}, cancelRun() {} }
    const app = createSSRApp(ModelIterationPanel, { runs: [run] as any, progress: progress as any })
    app.mixin({ created() {
      if (this.$options.__name !== 'ModelIterationPanel') return
      const state = this.$.setupState as any
      state.executionMode = 'evaluate_only'
      state.preview = { ...stale, preview_id: 'preview-old', valid: true, trajectory_count: 1, cases: [], errors: [], source: {}, source_digest: 'old' }
    } })
    stubs(app)
    const html = await renderToString(app)
    expect(html).toContain('data-testid="stale-evaluation-run"')
    expect(html).toContain('data-testid="stale-evaluation-preview"')
    expect(html).toContain('旧采集快照 · cs_original')
    expect(html).not.toContain('旧采集快照 · cs_latest')
    expect(html).toContain('结果与报告保持原样')
    expect(progress.resultMetadata.value).toEqual(run)
  })

  it('labels historical report files without changing their download identity', async () => {
    const folder = { index: 1, dir_name: 'original-folder', run_id: 'eval-old', ...stale, files: [{ file_id: 'report-original', name: 'original.json', size: 1 }] }
    const original = JSON.stringify(folder)
    const app = createSSRApp(FactoryRunDetailsDrawer, { modelValue: true, tab: 'reports', evaluation: true, runs: [], folders: [folder], loadingReports: false, downloadingReport: '' })
    stubs(app, false)
    app.component('el-table', defineComponent({ props: ['data'], setup(props, { slots }) { provide('snapshot-test-rows', () => props.data); return () => h('div', slots.default?.()) } }))
    app.component('el-table-column', defineComponent({ props: ['type', 'prop'], setup(props, { slots }) {
      const rows = inject<() => any[]>('snapshot-test-rows', () => [])
      return () => props.type === 'expand' ? null : h('div', rows().map(row => slots.default?.({ row }) || h('span', props.prop === 'file.name' ? row.file?.name : '')))
    } }))
    const html = await renderToString(app)
    expect(html).toContain('旧采集快照 · cs_original')
    expect(html).toContain('original.json')
    expect(JSON.stringify(folder)).toBe(original)
  })
})
