import { describe, expect, it, vi } from 'vitest'
import { createSSRApp, defineComponent, h, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { CollectionBatchSummary } from '@/collectionBatchesApi'
import type { TaskGenerationJob } from '@/types'
import CollectionBatchSubmission from './CollectionBatchSubmission.vue'

const mock = vi.hoisted(() => ({ state: {} as Record<string, unknown> }))
vi.mock('@/composables/useCollectionBatchSubmission', () => ({ useCollectionBatchSubmission: () => mock.state }))

async function render(options: { batch?: CollectionBatchSummary; error?: string; canSubmit?: boolean; busy?: boolean; count?: number } = {}) {
  mock.state = { batch: ref(options.batch || null), loading: ref(false), submitting: ref(false), downloading: ref(false), error: ref(options.error || ''), canSubmit: ref(Boolean(options.canSubmit)), refresh: vi.fn(), submit: vi.fn(), download: vi.fn(), dispose: vi.fn() }
  const app = createSSRApp(CollectionBatchSubmission, { job: { job_id: 'A', status: 'succeeded' } as TaskGenerationJob, resultCount: options.count ?? 2, busy: Boolean(options.busy), protect: async () => true, runProtected: async () => null })
  app.use(createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] }))
  app.component('el-button', defineComponent({ props: { disabled: Boolean }, setup: (props, { slots }) => () => h('button', { disabled: props.disabled }, slots.default?.()) }))
  return renderToString(app)
}

describe('collection batch submission presentation', () => {
  it('explains the first freeze and that submitting does not execute phones', async () => {
    const html = await render({ canSubmit: true })
    expect(html).toContain('提交轨迹采集')
    expect(html).toContain('首次提交会保存全部未删除任务及前置任务的固定副本')
    expect(html).toContain('每个作业只创建一个采集批次')
    expect(html).toContain('提交仅准备采集数据')
    expect(html).not.toContain('下载采集表')
  })

  it('keeps frozen download and navigation controls after every source row has been deleted', async () => {
    const value = { batch_id: 'frozen', task_count: 2, apps: ['App甲'], created_at: '2026-09-09T10:00:00' } as CollectionBatchSummary
    const html = await render({ batch: value, count: 0 })
    expect(html).toContain('已提交轨迹采集')
    expect(html).toContain('2 条任务')
    expect(html).toContain('下载采集表')
    expect(html).toContain('前往手机采集')
    expect(html).toContain('后续修改、删除或恢复不会改变此批次')
  })

  it('displays lookup failures and leaves first submission disabled', async () => {
    const html = await render({ error: '读取提交状态失败' })
    expect(html).toContain('role="alert"')
    expect(html).toContain('读取提交状态失败')
    expect(html).toContain('重试读取')
    expect(html).toMatch(/<button[^>]*disabled[^>]*>提交轨迹采集<\/button>/)
  })
})
