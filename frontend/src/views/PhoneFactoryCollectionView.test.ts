import { describe, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { CollectionBatchSummary } from '@/collectionBatchesApi'
import PhoneFactoryCollectionView from './PhoneFactoryCollectionView.vue'

const mock = vi.hoisted(() => ({ state: {} as Record<string, unknown> }))
vi.mock('@/composables/usePhoneCollectionBatches', () => ({ usePhoneCollectionBatches: () => mock.state }))

const batch = (batchId: string, kind: CollectionBatchSummary['kind'], createdAt: string, taskCount: number): CollectionBatchSummary => ({
  schema_version: 1,
  batch_id: batchId,
  source_job_id: batchId,
  kind,
  job_status: 'succeeded',
  knowledge_base_version: null,
  created_at: createdAt,
  task_count: taskCount,
  apps: ['示例App'],
  filename: `collection-batch-${batchId}.xlsx`,
  download_url: `/api/task-generation/collection-batches/${batchId}/workbook`,
})

async function render() {
  mock.state = {
    batches: ref([
      batch('generation-batch', 'task_generation', '2026-09-09T10:20:30+08:00', 12),
      batch('augmentation-batch', 'augmentation', '2026-09-08T09:08:07+08:00', 6),
    ]),
    selectedBatchId: ref(''),
    selectedBatch: ref(null),
    loadingBatches: ref(false),
    loadingBatch: ref(false),
    busy: ref(false),
    downloading: ref(false),
    error: ref(''),
    selectBatch: vi.fn(),
    loadBatches: vi.fn(),
    runBatch: vi.fn(),
    downloadBatch: vi.fn(),
    dispose: vi.fn(),
  }
  const app = createSSRApp(PhoneFactoryCollectionView)
  app.use(createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] }))
  const empty = defineComponent({ setup: () => () => h('div') })
  const passthrough = defineComponent({ setup: (_props, { slots }) => () => h('div', null, slots.default?.()) })
  app.component('el-input', empty)
  app.component('el-button', passthrough)
  app.component('el-table', empty)
  app.component('el-table-column', empty)
  app.component('el-tag', passthrough)
  app.component('el-input-number', empty)
  app.component('el-alert', empty)
  app.component('el-dialog', empty)
  app.component('el-select', defineComponent({
    props: { placeholder: String },
    setup: (props, { slots }) => () => h('section', { 'data-placeholder': props.placeholder }, slots.default?.()),
  }))
  app.component('el-option', defineComponent({
    props: { label: String },
    setup: props => () => h('div', { class: 'batch-option' }, props.label),
  }))
  return renderToString(app)
}

describe('phone collection batch picker', () => {
  it('uses neutral copy and identifies generation and augmentation batches in the same dropdown', async () => {
    const html = await render()
    expect(html).toContain('采集任务批次')
    expect(html).toContain('data-placeholder="选择已提交的采集批次"')
    expect(html).toContain('generation-batch · 任务生成 · 2026-09-09 10:20:30 · 12 条任务')
    expect(html).toContain('augmentation-batch · 泛化扩增 · 2026-09-08 09:08:07 · 6 条任务')
    expect(html).not.toContain('生成采集批次')
    expect(html).not.toContain('选择已提交的生成批次')
  })
})
