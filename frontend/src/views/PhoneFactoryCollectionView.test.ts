import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import { phoneFactoryApi, type FactoryBatchSummary } from '@/phoneFactoryApi'
import { ElMessage } from 'element-plus'
import { notifyBatchesPublished } from '@/utils/batchLifecycle'
import PhoneFactoryCollectionView from './PhoneFactoryCollectionView.vue'

vi.mock('element-plus', () => ({ ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn() }, ElMessageBox: {} }))
afterEach(() => vi.restoreAllMocks())

const mock = vi.hoisted(() => ({ state: {} as Record<string, unknown> }))
vi.mock('@/composables/usePhoneCollectionBatches', () => ({ usePhoneCollectionBatches: () => mock.state }))

const batch = (batchId: string, kind: FactoryBatchSummary['kind'], createdAt: string, taskCount: number): FactoryBatchSummary => ({
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

async function render(mode: 'generate' | 'modeliter' = 'generate', capture?: (view: any) => void) {
  mock.state = {
    batches: ref([
      batch('generation-batch', 'task_generation', '2026-09-09T10:20:30+08:00', 12),
      batch('augmentation-batch', 'augmentation', '2026-09-08T09:08:07+08:00', 6),
      { ...batch('manual-batch', 'manual_collection', '2026-09-21T09:08:07+08:00', 2), source_job_id: null },
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
  const app = createSSRApp(PhoneFactoryCollectionView, { mode })
  if (capture) app.mixin({ created() { if (this.$options.__name === 'PhoneFactoryCollectionView') capture(this.$.setupState) } })
  app.use(createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] }))
  const empty = defineComponent({ setup: () => () => h('div') })
  const passthrough = defineComponent({ setup: (_props, { slots }) => () => h('div', null, slots.default?.()) })
  app.component('el-input', empty)
  app.component('el-progress', empty)
  app.component('el-switch', defineComponent({
    props: { modelValue: Boolean },
    setup: props => () => h('input', { type: 'checkbox', checked: props.modelValue }),
  }))
  app.component('el-button', passthrough)
  app.component('el-radio-group', passthrough)
  app.component('el-radio-button', passthrough)
  app.component('el-radio', passthrough)
  app.component('el-table', empty)
  app.component('el-table-column', empty)
  app.component('el-tag', passthrough)
  app.component('el-input-number', empty)
  app.component('el-alert', empty)
  app.component('el-dialog', passthrough)
  app.component('el-drawer', defineComponent({ props: { modelValue: Boolean }, setup: (props, { slots }) => () => props.modelValue ? h('aside', slots.default?.()) : null }))
  app.component('el-pagination', empty)
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
  it('uses server publication state after reload and keeps the release link while blocking mutations', async () => {
    let view: any
    await render('generate', value => { view = value })
    const run = { collection_run_id: 'published-run', batch_id: 'published-result', status: 'running', batch_status: 'published', release_id: 'release-result' }
    const sync = vi.spyOn(phoneFactoryApi, 'syncRun')
    const cancel = vi.spyOn(phoneFactoryApi, 'cancelCollection')
    expect(view.isPublishedRun(run)).toBe(true)
    expect(view.publishedRunLocation(run)).toEqual({ path: '/data-publishing/archive', query: { release_id: 'release-result' } })
    await view.syncRun(run)
    await view.cancelCollectionRun(run)
    expect(sync).not.toHaveBeenCalled()
    expect(cancel).not.toHaveBeenCalled()
    notifyBatchesPublished({ batch_ids: ['cached-result'], release_id: 'cached-release' })
    expect(view.isPublishedRun({ batch_id: 'cached-result' })).toBe(true)
    expect(view.publishedRunLocation({ batch_id: 'cached-result' }).query.release_id).toBe('cached-release')
    expect(view.isPublishedRun({ batch_id: 'cached-result', batch_status: 'active' })).toBe(true)
  })

  it('keeps sibling results visible under their reusable source after a result or source is published', async () => {
    let view: any
    await render('generate', value => { view = value })
    const first = { collection_run_id: 'cr-new', batch_id: 'result-new', task_source_batch_id: 'same-source', batch_role: 'collection_result', status: 'completed', agent_type: 'newagent' }
    const second = { ...first, collection_run_id: 'cr-pipeline', batch_id: 'result-pipeline', agent_type: 'pipelineagent' }
    view.selectedBatchId = 'same-source'
    view.runs = [first, second, { ...first, batch_id: 'other-result', task_source_batch_id: 'other-source' }]
    expect(view.filteredRuns.map((run: { batch_id: string }) => run.batch_id)).toEqual(['result-new', 'result-pipeline'])
    view.onPublished({ batch_ids: ['result-new', 'same-source'], release_id: 'release-one' })
    expect(view.selectedBatchId).toBe('same-source')
    expect(view.filteredRuns.map((run: { batch_id: string }) => run.batch_id)).toEqual(['result-new', 'result-pipeline'])
    expect(view.collectionResultLocation(first).query.batch_id).toBe('result-new')
    expect(view.collectionResultLocation(second).query.batch_id).toBe('result-pipeline')
    expect(view.isManagedTask({ source_batch_id: 'same-source' })).toBe(false)
  })

  it('only shows a stopped run after the server confirms cancellation and respects managed batches', async () => {
    let view: any
    await render('generate', value => { view = value })
    const run = { collection_run_id: 'run-stop', batch_id: 'batch-stop', status: 'running', agent_type: 'pipelineagent' as const }
    view.runs = [run]
    const cancel = vi.spyOn(phoneFactoryApi, 'cancelCollection').mockRejectedValueOnce(new Error('collector unavailable'))
    await view.cancelCollectionRun(run)
    expect(view.runs[0].status).toBe('running')
    expect(ElMessage.error).toHaveBeenCalledWith('collector unavailable')
    cancel.mockResolvedValueOnce({ ...run, status: 'interrupted' })
    await view.cancelCollectionRun(run)
    expect(view.runs[0].status).toBe('interrupted')
    expect(cancel).toHaveBeenCalledTimes(2)
    view.managedBatchIds = ['batch-stop']
    await view.cancelCollectionRun(run)
    expect(cancel).toHaveBeenCalledTimes(2)
    view.managedBatchIds = []
    await view.cancelCollectionRun({ ...run, status: 'completed' })
    expect(cancel).toHaveBeenCalledTimes(2)
  })

  it('offers default-off logprob switches for saved settings and a custom run', async () => {
    const html = await render()
    expect(html).toContain('采集 Logprobs')
    expect(html).toContain('data-placeholder="host:port 或 http(s)://host:port/v1"')
    for (const id of ['capture-logprobs', 'custom-run-capture-logprobs']) {
      const control = html.match(new RegExp(`<input[^>]*data-testid="${id}"[^>]*>`))?.[0]
      expect(control).toBeDefined()
      expect(control).not.toContain('checked')
    }
  })
  it('keeps evaluation runs outside production batch and preprocessing controls', async () => {
    const html = await render('modeliter')
    expect(html).toContain('模型迭代评估')
    expect(html).toContain('评估运行与报告')
    expect(html).toContain('评测模型配置')
    expect(html).toContain('用例评测进度')
    expect(html).toContain('密钥环境变量名')
    expect(html).toContain('已有轨迹单独评测')
    expect(html).toContain('评测规则版本')
    expect(html).not.toContain('采集任务来源')
    expect(html).not.toContain('前往预处理')
  })
  it('uses neutral copy and identifies generation and augmentation batches in the same dropdown', async () => {
    const html = await render()
    expect(html).toContain('采集任务来源')
    expect(html).not.toContain('评测模型配置')
    expect(html).toContain('data-placeholder="选择可复用的任务来源"')
    expect(html).toContain('generation-batch · 任务生成 · 2026-09-09 10:20:30 · 12 条任务')
    expect(html).toContain('augmentation-batch · 泛化扩增 · 2026-09-08 09:08:07 · 6 条任务')
    expect(html).toContain('manual-batch · 手动上传 · 2026-09-21 09:08:07 · 2 条任务')
    expect(html).not.toContain('生成采集批次')
    expect(html).not.toContain('选择已提交的生成批次')
  })
})
