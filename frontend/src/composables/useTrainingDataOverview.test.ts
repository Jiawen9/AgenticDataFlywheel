import { afterEach, describe, expect, it, vi } from 'vitest'
import { emptyOverviewFilters, recentOverviewDates, useTrainingDataOverview } from './useTrainingDataOverview'
import type { OverviewFilters, TrainingDataOverview } from '@/trainingDataOverviewApi'

const sample = (count = 10): TrainingDataOverview => ({
  schema_version: 1, version: 'v1', updated_at: '2026-09-19T08:00:00Z',
  overview: { total_trajectories: count, subtask_trajectories: count, total_steps: 142, show_manual_refine_steps: false, manual_refine_steps: 1, manual_known_steps: 5, manual_unknown_steps: 137, level1_scenes: 2, level2_scenes: 4, total_apps: 2, avg_steps_per_trajectory: 14.2 },
  filters: { sources: ['数据飞轮', 'DEV'], scenes: [{ name: '视频', level2_scenes: ['搜索', '播放'] }, { name: '出行', level2_scenes: ['叫车'] }], apps: ['爱奇艺', '高德'], date_range: { min_date: '2026-09-01', max_date: '2026-09-19' } },
  trend: [], app_stats: [], scene_stats: [], action_stats: [], step_stats: [],
  conversions: [], warnings: [], workbook_url: '/api/training-data-overview/workbook',
})
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (value: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function setup() {
  const client = { read: vi.fn(async (_filters: OverviewFilters, _signal?: AbortSignal) => sample()) }
  return { client, view: useTrainingDataOverview(client) }
}
afterEach(() => vi.useRealTimers())

describe('DEV training overview interactions', () => {
  it('initializes the scene perspective with sources, date range, scenes, apps, then data', async () => {
    const { view, client } = setup()
    await view.initialize()
    expect(client.read).toHaveBeenCalledTimes(5)
    expect(client.read.mock.calls[0]?.[0]).toEqual(emptyOverviewFilters())
    expect(client.read.mock.calls[2]?.[0]).toMatchObject({ app: 'all', level2: '', start_date: '2026-09-01' })
    expect(client.read.mock.calls[3]?.[0]).toMatchObject({ app: '', start_date: '2026-09-01' })
    expect(client.read.mock.calls[4]?.[0]).toEqual({ ...emptyOverviewFilters(), start_date: '2026-09-01', end_date: '2026-09-19' })
    expect(view.level2Options.value).toEqual([])
    expect(view.sources.value).toEqual(['数据飞轮', 'DEV'])
    expect(view.apps.value).toEqual(['爱奇艺', '高德'])
    expect(view.successMessage.value).toBe('数据加载完成')
    view.dispose()
  })

  it('initializes the app perspective with apps before scenes and resets on each switch', async () => {
    const { view, client } = setup()
    await view.initialize('scene')
    Object.assign(view.filters, { source: 'DEV', app: '爱奇艺', level1: '视频', level2: '播放' })
    client.read.mockClear()
    await view.initialize('app')
    expect(view.perspective.value).toBe('app')
    expect(client.read.mock.calls[0]?.[0]).toEqual(emptyOverviewFilters())
    expect(client.read.mock.calls[2]?.[0].app).toBe('')
    expect(client.read.mock.calls[3]?.[0].app).toBe('all')
    expect(view.filters).toMatchObject({ source: 'all', app: 'all', level1: '', level2: '' })
    view.filters.app = '高德'
    await view.initialize('scene')
    expect(view.filters.app).toBe('all')
    view.dispose()
  })

  it('scene cascades clear app selections and fetch apps without changing displayed statistics', async () => {
    const { view, client } = setup()
    await view.initialize()
    client.read.mockResolvedValue(sample(77))
    Object.assign(view.filters, { level1: '视频', level2: '旧选择', app: '高德' })
    await view.changeLevel1()
    expect(view.filters).toMatchObject({ app: 'all', level2: '' })
    expect(view.level2Options.value).toEqual(['搜索', '播放'])
    expect(client.read).toHaveBeenLastCalledWith(expect.objectContaining({ level1: '视频', level2: '', app: '' }), expect.any(AbortSignal))
    view.filters.level2 = '播放'
    view.filters.app = '高德'
    await view.changeLevel2()
    expect(client.read).toHaveBeenLastCalledWith(expect.objectContaining({ level1: '视频', level2: '播放', app: '' }), expect.any(AbortSignal))
    view.filters.app = '爱奇艺'
    await view.changeApp()
    expect(client.read).toHaveBeenCalledTimes(7)
    expect(view.data.value?.overview.total_trajectories).toBe(10)
    await view.search()
    expect(view.data.value?.overview.total_trajectories).toBe(77)
    expect(view.appliedFilters.value.app).toBe('爱奇艺')
    view.dispose()
  })

  it('app selection refreshes scenes, while scene changes only update draft filters', async () => {
    const { view, client } = setup()
    await view.initialize('app')
    Object.assign(view.filters, { app: '爱奇艺', level1: '出行', level2: '叫车' })
    await view.changeApp()
    expect(view.filters).toMatchObject({ app: '爱奇艺', level1: '', level2: '' })
    expect(client.read).toHaveBeenLastCalledWith(expect.objectContaining({ app: '爱奇艺', level1: '', level2: '' }), expect.any(AbortSignal))
    view.filters.level1 = '视频'
    await view.changeLevel1()
    view.filters.level2 = '播放'
    await view.changeLevel2()
    expect(view.level2Options.value).toEqual(['搜索', '播放'])
    expect(client.read).toHaveBeenCalledTimes(6)
    expect(view.appliedFilters.value).toMatchObject({ app: 'all', level1: '', level2: '' })
    view.dispose()
  })

  it.each(['scene', 'app'] as const)('source change resets dependent fields and refreshes %s options without searching', async perspective => {
    const { view, client } = setup()
    await view.initialize(perspective)
    Object.assign(view.filters, { source: 'DEV', app: '爱奇艺', level1: '视频', level2: '播放' })
    const narrowed = sample(55)
    narrowed.filters.date_range = { min_date: '2026-09-05', max_date: '2026-09-06' }
    client.read.mockResolvedValue(narrowed)
    await view.changeSource()
    expect(view.filters).toEqual({ source: 'DEV', app: 'all', level1: '', level2: '', start_date: '2026-09-05', end_date: '2026-09-06' })
    expect(client.read).toHaveBeenCalledTimes(8)
    expect(view.data.value?.overview.total_trajectories).toBe(10)
    expect(client.read.mock.calls[6]?.[0].app).toBe(perspective === 'scene' ? 'all' : '')
    view.dispose()
  })

  it('quick date choices only fill dates, using the DEV calendar interval', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-20T02:00:00Z'))
    const { view, client } = setup()
    await view.initialize()
    view.recent(7)
    expect(view.filters).toMatchObject({ start_date: '2026-09-13', end_date: '2026-09-20' })
    view.recent(30)
    expect(view.filters).toMatchObject({ start_date: '2026-08-21', end_date: '2026-09-20' })
    expect(client.read).toHaveBeenCalledTimes(5)
    expect(view.appliedFilters.value.start_date).toBe('2026-09-01')
    expect(recentOverviewDates(7, new Date('2026-12-31T16:30:00Z'))).toEqual({ start_date: '2026-12-24', end_date: '2026-12-31' })
    view.dispose()
  })

  it('a switch cancels a pending search and an old response cannot restore the previous perspective', async () => {
    const { view, client } = setup(), old = deferred<TrainingDataOverview>()
    await view.initialize()
    view.filters.app = '爱奇艺'
    client.read.mockReturnValueOnce(old.promise)
    const previous = view.search()
    const signal = client.read.mock.calls.at(-1)?.[1]
    await view.initialize('app')
    expect(signal?.aborted).toBe(true)
    old.resolve(sample(999))
    await previous
    expect(view.data.value?.overview.total_trajectories).toBe(10)
    expect(view.appliedFilters.value.app).toBe('all')
    expect(view.loading.value).toBe(false)
    view.dispose()
  })

  it('ignores obsolete option responses and errors after a newer source is selected', async () => {
    const { view, client } = setup(), old = deferred<TrainingDataOverview>()
    await view.initialize()
    view.filters.source = '旧来源'
    client.read.mockReturnValueOnce(old.promise)
    const previous = view.changeSource()
    view.filters.source = 'DEV'
    await view.changeSource()
    old.reject(new Error('旧请求错误'))
    await previous
    expect(view.filters.source).toBe('DEV')
    expect(view.successMessage.value).toBe('数据加载完成')
    expect(client.read).toHaveBeenCalledTimes(9)
    view.dispose()
  })

  it('keeps the last successful data on failure, prevents duplicate searches, and clears the DEV toast', async () => {
    vi.useFakeTimers()
    const { view, client } = setup(), pending = deferred<TrainingDataOverview>()
    await view.initialize()
    client.read.mockReturnValueOnce(pending.promise)
    const search = view.search()
    await view.search()
    expect(client.read).toHaveBeenCalledTimes(6)
    pending.reject(new Error('unavailable'))
    await search
    expect(view.data.value?.overview.total_steps).toBe(142)
    expect(view.successMessage.value).toBe('加载失败，请重试')
    expect(view.loading.value).toBe(false)
    await vi.advanceTimersByTimeAsync(2000)
    expect(view.successMessage.value).toBe('')
    view.dispose()
  })

  it('clears missing dates and stale options, and has no maintenance polling', async () => {
    vi.useFakeTimers()
    const { view, client } = setup()
    const empty = sample(0)
    empty.filters = { sources: [], scenes: [], apps: [], date_range: { min_date: null, max_date: null } }
    empty.conversions = [{ release_id: 'r1', name: 'running', status: 'running', error: null, warnings: [], updated_at: 'now' }]
    client.read.mockResolvedValue(empty)
    await view.initialize()
    expect(view.filters.start_date).toBe('')
    expect(view.filters.end_date).toBe('')
    expect(view.level2Options.value).toEqual([])
    await vi.advanceTimersByTimeAsync(20000)
    expect(client.read).toHaveBeenCalledTimes(5)
    view.dispose()
  })

  it('does not update any state after disposal during initialization', async () => {
    const { view, client } = setup(), pending = deferred<TrainingDataOverview>()
    client.read.mockReturnValueOnce(pending.promise)
    const initialize = view.initialize('app')
    const signal = client.read.mock.calls[0]?.[1]
    view.dispose()
    pending.resolve(sample())
    await initialize
    expect(signal?.aborted).toBe(true)
    expect(view.data.value).toBeNull()
    expect(view.sources.value).toEqual([])
    expect(view.successMessage.value).toBe('')
    expect(client.read).toHaveBeenCalledTimes(1)
  })
})
