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
  async function loadDateRange(request: FacetRequest) {
    const response = await client.read({ ...filters, level2: '' }, request.signal)
    if (!request.current()) return
    const range = response.filters.date_range
    filters.start_date = range.min_date && range.max_date ? range.min_date : ''
    filters.end_date = range.min_date && range.max_date ? range.max_date : ''
  }
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
  async function loadDependentOptions(request: FacetRequest) {
    if (perspective.value === 'scene') {
      await loadScenes(request)
      if (request.current()) await loadApps(request)
    } else {
      await loadApps(request)
      if (request.current()) await loadScenes(request)
    }
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
    clearMessage()
    perspective.value = view
    Object.assign(filters, emptyOverviewFilters())
    appliedFilters.value = emptyOverviewFilters()
    data.value = null
    sources.value = []
    sceneTree.value = []
    apps.value = []
    loading.value = false
    await runFacets(async request => {
      const response = await client.read({ ...filters }, request.signal)
      if (!request.current()) return
      sources.value = response.filters.sources
      filters.source = 'all'
      await loadDateRange(request)
      if (!request.current()) return
      await loadDependentOptions(request)
      if (request.current()) await search()
    })
  }
  async function changeSource() {
    filters.level1 = ''
    filters.level2 = ''
    filters.app = 'all'
    await runFacets(async request => {
      await loadDateRange(request)
      if (request.current()) await loadDependentOptions(request)
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
