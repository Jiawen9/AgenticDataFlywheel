import { computed, reactive, ref } from 'vue'
import { trainingDataOverviewApi, type OverviewFilters, type TrainingDataOverview } from '@/trainingDataOverviewApi'

export type OverviewPerspective = 'scene' | 'app'
export const emptyOverviewFilters = (): OverviewFilters => ({ source: 'all', level1: '', level2: '', app: 'all', start_date: '', end_date: '' })

/** Keep the DEV shortcut's calendar subtraction and ISO date formatting. */
export function recentOverviewDates(days: number, now = new Date()) {
  const past = new Date(now)
  past.setDate(now.getDate() - days)
  return { start_date: past.toISOString().slice(0, 10), end_date: now.toISOString().slice(0, 10) }
}

export function useTrainingDataOverview(client: Pick<typeof trainingDataOverviewApi, 'read'> = trainingDataOverviewApi) {
  const perspective = ref<OverviewPerspective>('scene')
  const filters = reactive(emptyOverviewFilters())
  const appliedFilters = ref(emptyOverviewFilters())
  const sources = ref<string[]>([])
  const sceneTree = ref<TrainingDataOverview['filters']['scenes']>([])
  const apps = ref<string[]>([])
  const data = ref<TrainingDataOverview | null>(null)
  const loading = ref(false)
  const successMessage = ref('')
  const level2Options = computed(() => sceneTree.value.find(scene => scene.name === filters.level1)?.level2_scenes ?? [])
  let disposed = false
  let generation = 0
  let facetEpoch = 0
  let searchEpoch = 0
  let facetController: AbortController | undefined
  let searchController: AbortController | undefined
  let messageTimer: ReturnType<typeof setTimeout> | undefined

  function clearMessage() {
    if (messageTimer !== undefined) clearTimeout(messageTimer)
    messageTimer = undefined
    successMessage.value = ''
  }
  function showMessage(message: string) {
    clearMessage()
    successMessage.value = message
    messageTimer = setTimeout(() => { successMessage.value = ''; messageTimer = undefined }, 2000)
  }
  function updateLevel2Options() {
    if (!level2Options.value.includes(filters.level2)) filters.level2 = ''
  }
  type FacetRequest = { signal: AbortSignal; current: () => boolean }
  async function loadScenes(request: FacetRequest) {
    const response = await client.read({ ...filters, level2: '' }, request.signal)
    if (!request.current()) return
    sceneTree.value = response.filters.scenes
    if (filters.level1 && !sceneTree.value.some(scene => scene.name === filters.level1)) filters.level1 = ''
    updateLevel2Options()
  }
  async function loadApps(request: FacetRequest) {
    const response = await client.read({ ...filters, app: '' }, request.signal)
    if (!request.current()) return
    apps.value = response.filters.apps
    if (filters.app !== 'all' && !apps.value.includes(filters.app)) filters.app = 'all'
  }
  async function runFacets(run: (request: FacetRequest) => Promise<void>) {
    if (disposed) return
    facetController?.abort()
    facetController = new AbortController()
    const epoch = ++facetEpoch
    const currentGeneration = generation
    const request = { signal: facetController.signal, current: () => !disposed && generation === currentGeneration && epoch === facetEpoch }
    try { await run(request) }
    catch { if (request.current()) showMessage('加载失败，请重试') }
  }
  async function search() {
    if (disposed || loading.value) return
    const epoch = ++searchEpoch
    const currentGeneration = generation
    searchController?.abort()
    searchController = new AbortController()
    const current = () => !disposed && generation === currentGeneration && epoch === searchEpoch
    const requestedFilters = { ...filters }
    loading.value = true
    try {
      const response = await client.read(requestedFilters, searchController.signal)
      if (!current()) return
      data.value = response
      appliedFilters.value = requestedFilters
      showMessage('数据加载完成')
    } catch { if (current()) showMessage('加载失败，请重试') }
    finally { if (current()) loading.value = false }
  }
  async function initialize(view: OverviewPerspective = 'scene') {
    if (disposed) return
    ++generation
    ++searchEpoch
    searchController?.abort()
    facetController?.abort()
    ++facetEpoch
    clearMessage()
    perspective.value = view
    loading.value = false
    // Perspective changes presentation, not the committed query or draft.
    if (data.value) return
    loading.value = true
    const currentGeneration = generation
    const initialFilters = { ...filters }
    await runFacets(async request => {
      const response = await client.read(initialFilters, request.signal)
      if (!request.current()) return
      const unchanged = JSON.stringify(filters) === JSON.stringify(initialFilters)
      if (unchanged) applyFacets(response)
      data.value = response
      appliedFilters.value = unchanged ? { ...filters } : initialFilters
      showMessage('数据加载完成')
    })
    if (!disposed && generation === currentGeneration) loading.value = false
  }
  function applyFacets(response: TrainingDataOverview) {
    sources.value = response.filters.sources
    sceneTree.value = response.filters.scenes
    apps.value = response.filters.apps
    const range = response.filters.date_range
    filters.start_date = range.min_date && range.max_date ? range.min_date : ''
    filters.end_date = range.min_date && range.max_date ? range.max_date : ''
  }
  async function changeSource() {
    filters.level1 = ''
    filters.level2 = ''
    filters.app = 'all'
    await runFacets(async request => {
      const response = await client.read({ ...filters }, request.signal)
      if (!request.current()) return
      const hadDates = Boolean(filters.start_date || filters.end_date)
      applyFacets(response)
      // Preserve the DEV cascade: an empty date context clears the dates, then
      // repopulates options in that unbounded context (without auto-searching).
      if (hadDates && !filters.start_date && !filters.end_date) {
        const unbounded = await client.read({ ...filters }, request.signal)
        if (!request.current()) return
        sceneTree.value = unbounded.filters.scenes
        apps.value = unbounded.filters.apps
      }
    })
  }
  async function changeLevel1() {
    filters.level2 = ''
    updateLevel2Options()
    if (perspective.value === 'scene') {
      filters.app = 'all'
      await runFacets(loadApps)
    }
  }
  async function changeLevel2() {
    if (perspective.value === 'scene') {
      filters.app = 'all'
      await runFacets(loadApps)
    }
  }
  async function changeApp() {
    if (perspective.value === 'app') {
      filters.level1 = ''
      filters.level2 = ''
      await runFacets(loadScenes)
    }
  }
  function recent(days: number) { Object.assign(filters, recentOverviewDates(days)) }
  function dispose() {
    disposed = true
    ++generation
    ++facetEpoch
    ++searchEpoch
    facetController?.abort()
    searchController?.abort()
    clearMessage()
  }
  return { perspective, filters, appliedFilters, sources, sceneTree, apps, data, loading, successMessage, level2Options, initialize, search, changeSource, changeLevel1, changeLevel2, changeApp, recent, dispose }
}
