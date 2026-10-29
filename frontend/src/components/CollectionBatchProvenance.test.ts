import { describe, expect, it, vi } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import type { CollectionSourceRun, PreprocessingBatch } from '@/types'
import CollectionBatchProvenance from './CollectionBatchProvenance.vue'

vi.mock('@/components/DisplayId.vue', () => ({ default: { props: ['id'], template: '<code>{{ id }}</code>' } }))
const batch = (values: Partial<PreprocessingBatch> = {}) => ({ batch_id: 'result-a', kind: 'task_generation', ...values } as PreprocessingBatch)
const run = (id: string) => ({ collection_run_id: id } as CollectionSourceRun)
const render = (value: PreprocessingBatch, runs: CollectionSourceRun[] = []) => renderToString(createSSRApp(CollectionBatchProvenance, { batch: value, runs }))

describe('preprocessing collection provenance', () => {
  it('shows the independent result, original source, engine and one deduplicated run', async () => {
    const html = await render(batch({ batch_role: 'collection_result', task_source_batch_id: 'source-one', agent_type: 'pipelineagent', collection_run_id: 'cr-one' }), [run('cr-one')])
    for (const text of ['result-a', 'source-one', 'PipelineAgent', '本次采集运行']) expect(html).toContain(text)
    expect(html.match(/cr-one/g)).toHaveLength(1)
    expect(html).not.toContain('历史批次')
  })

  it('explains the unchanged aggregate scope of a historical multi-run batch', async () => {
    const html = await render(batch(), [run('cr-old-a'), run('cr-old-b')])
    expect(html).toContain('历史数据未按运行拆分')
    expect(html).toContain('cr-old-a')
    expect(html).toContain('cr-old-b')
    expect(html).not.toContain('任务来源')
  })

  it('does not call a new source or imported rollout a historical merged collection', async () => {
    expect(await render(batch())).not.toContain('历史批次')
    expect(await render(batch({ kind: 'rollout_import' }), [run('imported-run')])).not.toContain('历史批次')
  })
})
