import { describe, expect, it } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import CollectionRunProgress from './CollectionRunProgress.vue'
import type { FactoryRun } from '@/phoneFactoryApi'
import { canPreprocessCollection, collectionProgressText } from '@/utils/phoneFactoryRuns'
import { factoryRunActive, factoryRunAbnormal } from '@/utils/factoryHistory'

const run = (): FactoryRun => ({ status: 'running', execution_mode: 'collect_and_evaluate', batch_id: 'result', repeat_times: 1,
  collection_artifacts_version: 1, collection_status: 'partial', transfer_status: 'downloading',
  collection_summary: { original_task_count: 10, planned_trajectory_count: 20, collection_completed_count: 17,
    collection_failed_count: 2, collection_cancelled_count: 1, sealed_trajectory_count: 18, diagnostic_count: 2,
    transferred_trajectory_count: 12, transferred_diagnostic_count: 1 } })

describe('independent collection and transfer progress', () => {
  it('renders original tasks, planned trajectories and actual transfer independently of evaluation', async () => {
    const html = await renderToString(createSSRApp(CollectionRunProgress, { run: run() }))
    for (const expected of ['原始任务 10 条', '计划 20 条', '采集完成 17', '失败 2', '取消 1', '已回传 12/18', '失败诊断已回传 1/2', '部分完成', '正在下载']) expect(html).toContain(expected)
    expect(await renderToString(createSSRApp(CollectionRunProgress, { run: { ...run(), execution_mode: 'evaluate_only' } }))).not.toContain('collection-transfer-progress')
    expect(collectionProgressText({ status: 'completed' })).toBe('')
    expect(collectionProgressText({ status: 'completed', collection_summary: {} } as FactoryRun)).toBe('')
    expect(await renderToString(createSSRApp(CollectionRunProgress, { run: { collection_summary: {} } as FactoryRun }))).not.toContain('collection-transfer-progress')
  })
  it('keeps transfer active after cancellation and gates preprocessing on completed data settlement', () => {
    const value = run()
    expect(canPreprocessCollection(value)).toBe(false)
    expect(factoryRunActive({ ...value, status: 'cancelled' })).toBe(true)
    expect(factoryRunAbnormal(value)).toBe(true)
    value.transfer_status = 'completed'
    expect(canPreprocessCollection(value)).toBe(true) // Evaluation is still running.
    expect(canPreprocessCollection({ ...value, execution_mode: 'evaluate_only' })).toBe(false)
    expect(canPreprocessCollection({ ...value, collection_summary: { ...value.collection_summary!, transferred_trajectory_count: 0 } })).toBe(false)
    expect(canPreprocessCollection({ status: 'completed', batch_id: 'legacy' })).toBe(true)
    expect(factoryRunAbnormal({ status: 'completed', transfer_status: 'remote_failed' })).toBe(true)
  })
})
