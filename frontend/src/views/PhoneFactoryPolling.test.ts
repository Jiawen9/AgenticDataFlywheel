import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSSRApp, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import PhoneFactoryCollectionView from './PhoneFactoryCollectionView.vue'
import { phoneFactoryApi } from '@/phoneFactoryApi'

const polls = vi.hoisted(() => ({ work: [] as Array<(signal: AbortSignal, current: () => boolean) => Promise<void>> }))
vi.mock('@/composables/useFactoryPolling', () => ({ useFactoryPolling: (work: any) => { polls.work.push(work); return { start: vi.fn(), stop: vi.fn() } } }))
afterEach(() => { vi.restoreAllMocks(); polls.work = [] })
describe('independent factory observation', () => {
  it('updates run snapshots while device status is stalled without clearing draft or history selection', async () => {
    let view: any, release!: (value: any) => void
    const pending = new Promise<any>(done => { release = done })
    const state = { apps: ['App'], phones: ['phone'], phoneApps: [{ phone_id: 'phone', app: 'App', status: '未知' }], tasks: [], vla: [] }
    vi.spyOn(phoneFactoryApi, 'state').mockResolvedValue(state)
    vi.spyOn(phoneFactoryApi, 'remoteStatus').mockReturnValue(pending)
    vi.spyOn(phoneFactoryApi, 'adbDevices').mockResolvedValue({ ok: true, devices: [] })
    vi.spyOn(phoneFactoryApi, 'runs').mockResolvedValue({ runs: [{ run_id: 'updated', status: 'running', task_source_batch_id: 'history' }] })
    const component = { ...PhoneFactoryCollectionView, ssrRender(_ctx: unknown, push: (html: string) => void) { push('<div></div>') } }
    const app = createSSRApp(component)
    app.mixin({ created() { if (this.$options.__name === 'PhoneFactoryCollectionView') view = this.$.setupState } })
    app.use(createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] }))
    await renderToString(app)
    view.newVla = 'draft-vla'; view.historySourceId = 'history'; view.selectedBatchId = 'draft-source'
    const jobs = polls.work.map(work => work(new AbortController().signal, () => true))
    await nextTick(); await Promise.resolve(); await nextTick()
    expect(view.runs[0].run_id).toBe('updated')
    expect(view.historySourceId).toBe('history'); expect(view.selectedBatchId).toBe('draft-source'); expect(view.newVla).toBe('draft-vla')
    expect(phoneFactoryApi.remoteStatus).toHaveBeenCalledTimes(1)
    release({ ok: true, statuses: [] }); await Promise.all(jobs)
  })
})
