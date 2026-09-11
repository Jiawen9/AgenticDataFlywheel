import { describe, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import type { TaskGenerationJob, TaskGenerationResult } from '@/types'

const mocks = vi.hoisted(() => ({
  review: {} as Record<string, unknown>,
  submissionProps: undefined as undefined | {
    job: TaskGenerationJob
    resultCount: number
    busy: boolean
    protect: () => Promise<boolean>
    runProtected: (action: (jobId: string) => Promise<unknown>) => Promise<unknown>
  },
}))

vi.mock('@/composables/useAugmentationPreview', () => ({ useAugmentationPreview: () => mocks.review }))
vi.mock('@/components/CollectionBatchSubmission.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      props: {
        job: { type: Object, required: true }, resultCount: { type: Number, required: true },
        busy: { type: Boolean, required: true }, protect: { type: Function, required: true },
        runProtected: { type: Function, required: true },
      },
      setup(props) {
        mocks.submissionProps = props as unknown as NonNullable<typeof mocks.submissionProps>
        return () => h('section', { 'aria-label': '提交轨迹采集' })
      },
    }),
  }
})

import TaskAugmentationView from './TaskAugmentationView.vue'

const job: TaskGenerationJob = {
  job_id: 'augmentation-job', kind: 'augmentation', status: 'succeeded', stage: 'succeeded',
  created_at: '2026-09-09T10:00:00', started_at: null, completed_at: '2026-09-09T10:01:00',
  current_item: null, completed_items: 1, total_items: 1, percent: 100, generate_n: 2,
  input_filename: 'failed-cases.xlsx', result_count: 2, errors: [], warnings: [], error: null,
  knowledge_base_version: 'snapshot-version',
}
const result = (id: string, deleted: boolean): TaskGenerationResult => ({
  result_id: id, task: `变体 ${id}`, app: '示例 App', scene: '场景', capability: '能力',
  sub_capability: '子能力', deleted,
})

describe('augmentation collection submission placement', () => {
  it('shows the selected job submission before the scene preview and review while preserving its bindings', async () => {
    const protect = vi.fn(async () => true)
    const runExternalAction = vi.fn(async () => null)
    mocks.submissionProps = undefined
    mocks.review = {
      jobs: ref([job]), selectedJob: ref(job), preview: ref({
        job_id: job.job_id, available: true, seeds: [],
        stats: { total: 0, matched: 0, unmatched: 0, classification_failed: 0, eligible: 0 },
      }),
      results: ref([result('kept', false), result('deleted', true)]), errors: ref([]),
      listError: ref(''), detailError: ref(''), loading: ref(false), refreshing: ref(false),
      busy: ref(false), active: ref(false), editingId: ref(null), editingText: ref(''), dirty: ref(false),
      protect, runExternalAction, create: vi.fn(), selectJob: vi.fn(), loadJobs: vi.fn(), refresh: vi.fn(),
      start: vi.fn(), startEdit: vi.fn(), saveEdit: vi.fn(), cancelEdit: vi.fn(), toggleDeleted: vi.fn(),
      exportResults: vi.fn(), dispose: vi.fn(),
    }

    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: TaskAugmentationView }] })
    await router.push('/')
    await router.isReady()
    const app = createSSRApp(defineComponent({ setup: () => () => h(RouterView) }))
    app.use(router)
    const passthrough = defineComponent({ setup: (_props, { slots }) => () => h('div', slots.default?.()) })
    const empty = defineComponent({ setup: () => () => h('div') })
    for (const name of ['el-alert', 'el-button', 'el-empty', 'el-icon', 'el-input', 'el-input-number', 'el-pagination', 'el-popover', 'el-progress', 'el-select', 'el-skeleton', 'el-table', 'el-tag']) app.component(name, passthrough)
    for (const name of ['el-option', 'el-table-column']) app.component(name, empty)

    const html = await renderToString(app)
    const metadata = html.indexOf('已完成 · failed-cases.xlsx')
    const submission = html.indexOf('aria-label="提交轨迹采集"')
    const scenePreview = html.indexOf('关联场景树')
    const resultReview = html.indexOf('变体审核')
    const seedPanel = html.indexOf('aria-label="失败用例"')
    expect(metadata).toBeGreaterThanOrEqual(0)
    expect(metadata).toBeLessThan(submission)
    expect(submission).toBeLessThan(scenePreview)
    expect(scenePreview).toBeLessThan(resultReview)
    expect(resultReview).toBeLessThan(seedPanel)
    expect(html.match(/class="variant-group"/g)).toHaveLength(2)
    expect(html.match(/aria-expanded="false"/g)).toHaveLength(3)
    expect(html).toContain('分类失败 0')
    expect(html).not.toContain('class="seed-panel-body"')
    expect(html).not.toContain('class="variant-group-body"')
    expect(html).toContain('未删除 0 / 全部 1 条变体')
    expect(html).toContain('历史来源记录不完整，单独展示')

    expect(mocks.submissionProps?.job).toMatchObject({ job_id: job.job_id, kind: 'augmentation' })
    expect(mocks.submissionProps?.resultCount).toBe(1)
    expect(mocks.submissionProps?.busy).toBe(false)
    expect(mocks.submissionProps?.protect).toBe(protect)
    expect(mocks.submissionProps?.runProtected).toBe(runExternalAction)
  })
})
