import { computed, onBeforeUnmount, onMounted, reactive, ref, watch, type Ref } from 'vue'
import { newRunRequestId, type CollectionDiagnostics, type PhoneOwner, type PhoneOwnership, type PhoneControlRequest, type PhoneControlOperation, type DeviceConfigRevision, type FactoryRun, type ResumeOperation, type ResumeRequest, type createFactoryApi } from '@/phoneFactoryApi'
import { factoryRunActive, factoryRunId, newestFactoryRuns } from '@/utils/factoryHistory'
import { useFactoryPolling } from './useFactoryPolling'

type DiagnosticsApi = Pick<ReturnType<typeof createFactoryApi>, 'diagnostics' | 'resumePreview' | 'resumeCollection' | 'resumeOperation'> & Partial<Pick<ReturnType<typeof createFactoryApi>, 'phoneOwnership' | 'phoneControl' | 'phoneControlOperation'>>
export interface PhoneControlState { runId: string; phoneId: string; action: 'reconcile' | 'stop'; request: PhoneControlRequest; result?: PhoneControlOperation; sending: boolean; error: string }
export interface PhoneResumeState {
  runId: string; phoneIds: string[]; requestId: string; request?: ResumeRequest; confirmedUnknown: boolean
  phase: 'previewing' | 'confirming' | 'submitting' | 'waiting' | 'settled' | 'cancelled'; error: string; result?: ResumeOperation
}
const terminal = (status?: string) => ['completed', 'succeeded', 'failed', 'cancelled', 'rejected', 'interrupted'].includes(status || '')
export function diagnosisAge(timestamp: string | undefined, now = Date.now()): number | null {
  if (!timestamp) return null
  const value = Date.parse(timestamp)
  return Number.isFinite(value) ? Math.max(0, (now - value) / 1000) : null
}

/** Page-owned state: drawer lifecycle and device probes never own this clock. */
export function useCollectionDiagnostics(runs: Ref<FactoryRun[]>, api: DiagnosticsApi, options: {
  canResume: (run: FactoryRun) => boolean
  confirmUnknown: (phones: string[]) => Promise<boolean>
}) {
  const selectedRunId = ref(''), snapshots = ref<Record<string, CollectionDiagnostics>>({})
  const errors = ref<Record<string, string>>({}), loading = ref(false), now = ref(Date.now())
  const sampleSequence = ref(0)
  const operations = reactive<PhoneResumeState[]>([])
  const phoneControls = reactive<PhoneControlState[]>([])
  const ownershipSnapshots = ref<Record<string, PhoneOwnership>>({})
  const phoneControlSupported = computed(() => snapshot.value?.phone_control?.version === 1 || snapshot.value?.phone_control_supported === true)
  const controlTerminal = (status?: string) => ['completed', 'conflict', 'failed', 'rejected'].includes(status || '')
  function ownershipFor(phone: string): PhoneOwnership | undefined {
    const manual = ownershipSnapshots.value[`${selectedRunId.value}:${phone}`]
    const observed = snapshot.value?.phones.find(item => item.phone_id === phone)?.ownership
    if (!manual) return observed
    if (!observed) return manual
    const date = (value: PhoneOwnership) => Date.parse(value.checked_at || value.received_at || '') || 0
    return date(observed) >= date(manual) ? observed : manual
  }
  function controlFor(phone: string) { return phoneControls.filter(item => item.runId === selectedRunId.value && item.phoneId === phone).at(-1) }
  async function checkOwnership(phone: string) {
    if (!api.phoneOwnership || !phoneControlSupported.value) return
    const id = selectedRunId.value, epoch = generation
    try {
      const result = await bounded(signal => api.phoneOwnership!(id, phone, signal))
      if (disposed || epoch !== generation || id !== selectedRunId.value) return
      ownershipSnapshots.value = { ...ownershipSnapshots.value, [`${id}:${phone}`]: result }
    } catch (cause) { if (!disposed && epoch === generation) errors.value = { ...errors.value, [id]: `占用暂未同步：${(cause as Error).message}` } }
  }
  async function pollControl(item: PhoneControlState, signal?: AbortSignal) {
    if (!api.phoneControlOperation) return
    try {
      const result = await bounded(abort => api.phoneControlOperation!(item.runId, item.request.request_id, abort), signal)
      if (disposed || signal?.aborted) return
      item.result = result; item.error = result.error || ''
      if (item.runId === selectedRunId.value && result.ownership) ownershipSnapshots.value = { ...ownershipSnapshots.value, [`${item.runId}:${item.phoneId}`]: result.ownership }
    } catch (cause) {
      if (disposed || signal?.aborted) return
      item.error = `等待确认手机控制：${(cause as Error).message}`
      if ((cause as Error & { status?: number }).status === 404 && api.phoneControl) {
        try {
          const result = await bounded(abort => api.phoneControl!(item.runId, item.phoneId, item.action, item.request, abort), signal)
          if (!disposed && !signal?.aborted) { item.result = result; item.error = result.error || '' }
        } catch (error) { if (!disposed && !signal?.aborted) item.error = `等待同一请求受理：${(error as Error).message}` }
      }
    }
  }
  async function controlPhone(phone: string, action: 'reconcile' | 'stop', owner: PhoneOwner) {
    if (!api.phoneControl || !phoneControlSupported.value || !owner.supported_actions.includes(action) || ownershipFor(phone)?.stale) return
    const id = selectedRunId.value
    const pending = phoneControls.find(item => item.runId === id && item.phoneId === phone && !controlTerminal(item.result?.status))
    if (pending) { if (!pending.sending) await pollControl(pending); return }
    const item = reactive<PhoneControlState>({ runId: id, phoneId: phone, action, sending: true, error: '', request: { request_id: newRunRequestId(), execution_id: owner.execution_id, expected_state_revision: owner.state_revision, ownership_token: owner.ownership_token } })
    phoneControls.push(item)
    try {
      const result = await bounded(signal => api.phoneControl!(id, phone, action, item.request, signal))
      if (disposed) return
      item.result = result; item.error = result.error || ''
    } catch (cause) {
      if (!disposed) {
        const issue = cause as Error & { status?: number }
        item.error = issue.message
        if (issue.status && issue.status >= 400 && issue.status < 500 && ![408,429].includes(issue.status))
          item.result = { operation_id: item.request.request_id, execution_id: item.request.execution_id, phone_id: phone, action, status: 'rejected', error: issue.message }
        else await pollControl(item)
      }
    }
    finally { item.sending = false }
  }
  const selectedRun = computed(() => runs.value.find(run => factoryRunId(run) === selectedRunId.value))
  const snapshot = computed(() => snapshots.value[selectedRunId.value])
  const error = computed(() => errors.value[selectedRunId.value] || '')
  const selectedOperations = computed(() => operations.filter(item => item.runId === selectedRunId.value))
  const stale = computed(() => !!snapshot.value?.stale || (diagnosisAge(snapshot.value?.observed_at, now.value) ?? Infinity) > 20)
  let mounted = false, disposed = false, generation = 0, requestSequence = 0, diagnosticController: AbortController | undefined, clock: ReturnType<typeof setInterval> | undefined
  const controls = new Set<AbortController>()

  async function bounded<T>(work: (signal: AbortSignal) => Promise<T>, outer?: AbortSignal): Promise<T> {
    const controller = new AbortController(); controls.add(controller)
    const abort = () => controller.abort()
    outer?.addEventListener('abort', abort, { once: true })
    if (outer?.aborted) controller.abort()
    const timer = setTimeout(abort, 15000)
    try { return await work(controller.signal) }
    finally { clearTimeout(timer); controls.delete(controller); outer?.removeEventListener('abort', abort) }
  }
  function selectRun(id: string) {
    const ordered = newestFactoryRuns(runs.value)
    selectedRunId.value = id || factoryRunId(ordered.find(factoryRunActive) || ordered[0] || { status: '' })
  }
  watch(runs, values => {
    // A just-created or temporarily absent run must survive a lagging snapshot.
    if (selectedRunId.value) return
    const ordered = newestFactoryRuns(values)
    selectedRunId.value = factoryRunId(ordered.find(factoryRunActive) || ordered[0] || { status: '' })
  }, { immediate: true })

  async function refresh(signal?: AbortSignal, current: () => boolean = () => true) {
    const id = selectedRunId.value, epoch = generation, sequence = ++requestSequence
    if (!id) return
    if (selectedRun.value?.execution_mode === 'evaluate_only') {
      snapshots.value = { ...snapshots.value, [id]: { supported: false, run_id: id, phones: [], summary: { total: 0, healthy: 0, warning: 0, error: 0 }, message: '独立评测不占用手机；进度与异常请查看用例明细中的当前评测快照。' } }
      return
    }
    diagnosticController?.abort()
    const request = diagnosticController = new AbortController()
    const abort = () => request.abort()
    signal?.addEventListener('abort', abort, { once: true })
    if (signal?.aborted) request.abort()
    loading.value = true
    try {
      const value = await bounded(abort => api.diagnostics(id, abort), request.signal)
      if (disposed || !current() || request.signal.aborted || epoch !== generation || sequence !== requestSequence || id !== selectedRunId.value) return
      snapshots.value = { ...snapshots.value, [id]: value }
      errors.value = { ...errors.value, [id]: '' }; now.value = Date.now(); sampleSequence.value++
    } catch (cause) {
      if (disposed || !current() || epoch !== generation || sequence !== requestSequence || request.signal.aborted) return
      const issue = cause as Error & { status?: number }
      // Older platform/collector versions retain all existing record actions.
      if ([404, 405, 501].includes(issue.status || 0)) snapshots.value = { ...snapshots.value, [id]: { supported: false, run_id: id, phones: [], summary: { total: 0, healthy: 0, warning: 0, error: 0 }, message: '当前采集端未提供运行诊断与续跑能力；原记录和下载仍可使用。' } }
      else errors.value = { ...errors.value, [id]: `诊断暂未同步：${issue.message}` }
    } finally { signal?.removeEventListener('abort', abort); if (epoch === generation && sequence === requestSequence) loading.value = false }
  }
  async function lookupOperation(item: PhoneResumeState, signal?: AbortSignal) {
    try {
      const result = await bounded(abort => api.resumeOperation(item.runId, item.result?.operation_id || item.requestId, abort), signal)
      if (disposed || signal?.aborted) return
      item.result = result; item.error = result.error || ''; item.phase = terminal(result.status) ? 'settled' : 'waiting'
    } catch (cause) {
      if (!disposed && !signal?.aborted) item.error = `等待确认续跑状态：${(cause as Error).message}`
    }
  }
  const poller = useFactoryPolling(async (signal, current) => {
    await Promise.allSettled([refresh(signal, current), ...operations.filter(item => item.phase === 'waiting').map(item => lookupOperation(item, signal)), ...phoneControls.filter(item => !item.sending && !controlTerminal(item.result?.status)).map(item => pollControl(item, signal))])
  }, 5000)
  watch(selectedRunId, () => { generation++; diagnosticController?.abort(); loading.value = false; if (mounted) poller.start() }, { flush: 'sync' })

  async function submit(item: PhoneResumeState) {
    if (!item.request) return
    item.phase = 'submitting'; item.error = ''
    try {
      const result = await bounded(signal => api.resumeCollection(item.runId, item.request!, signal))
      if (disposed) return
      item.result = result; item.error = result.error || ''; item.phase = terminal(result.status) ? 'settled' : 'waiting'
      if (item.runId === selectedRunId.value) void refresh()
    } catch (cause) {
      if (disposed) return
      const issue = cause as Error & { status?: number; code?: string }
      if (issue.status && issue.status >= 400 && issue.status < 500 && ![408, 429].includes(issue.status)) {
        item.phase = 'settled'; item.error = issue.message; return
      }
      item.phase = 'waiting'; item.error = `请求结果尚未确认：${(cause as Error).message}`
      // An acknowledgement may be lost after acceptance. Never create a new ID.
      await lookupOperation(item)
    }
  }
  async function continuePhones(phoneIds: string[], deviceRevision?: DeviceConfigRevision) {
    const run = selectedRun.value, id = selectedRunId.value
    const phones = [...new Set(phoneIds)].sort()
    if (!run || !id || !phones.length || !options.canResume(run) || snapshot.value?.supported !== true) return
    const existing = operations.find(item => item.runId === id && item.phase !== 'settled' && item.phase !== 'cancelled'
      && item.phoneIds.some(phone => phones.includes(phone)))
    if (existing) {
      if (existing.phase !== 'waiting') return
      if (existing.result) await lookupOperation(existing)
      else if (existing.request) await submit(existing)
      return
    }
    const item = reactive<PhoneResumeState>({ runId: id, phoneIds: phones, requestId: newRunRequestId(), confirmedUnknown: false, phase: 'previewing', error: '' })
    operations.push(item)
    try {
      let preview = await bounded(signal => api.resumePreview(id, phones, signal))
      if (disposed) return
      let unknown = preview.requires_unknown_phone_ids || []
      if (!preview.instances?.length) {
        item.phase = 'settled'; item.error = preview.blocked_phones?.map(phone => typeof phone === 'string' ? phone : `${phone.phone_id}：${phone.message || phone.reason || '当前不可续跑'}`).join('；') || '所选手机没有可继续的实例，刷新诊断后查看原因。'; return
      }
      if (unknown.length) {
        item.phase = 'confirming'
        const confirmed = await options.confirmUnknown(unknown)
        if (disposed) return
        if (confirmed) item.confirmedUnknown = true
        else {
          // Declining uncertain devices does not cancel other known stopped
          // phones. No POST has occurred, so the same request ID can safely
          // adopt a new frozen preview with a strictly smaller phone set.
          while (unknown.length) {
            const knownPhones = [...new Set(preview.instances.map(row => row.phone_id)
              .filter((phone): phone is string => typeof phone === 'string' && item.phoneIds.includes(phone) && !unknown.includes(phone)))].sort()
            if (!knownPhones.length) { item.phase = 'cancelled'; return }
            if (knownPhones.length >= item.phoneIds.length) throw new Error('续跑预览的手机范围不一致，请刷新后重试。')
            item.phoneIds = knownPhones; item.phase = 'previewing'
            preview = await bounded(signal => api.resumePreview(id, knownPhones, signal))
            if (disposed) return
            unknown = preview.requires_unknown_phone_ids || []
            // Newly uncertain phones are excluded too, without another dialog.
          }
          if (!preview.instances?.length) { item.phase = 'settled'; item.error = '其余手机暂时没有可继续的实例，请刷新诊断。'; return }
        }
      }
      if (disposed) return
      const selectedPhones = [...new Set(preview.instances.map(row => row.phone_id)
        .filter((phone): phone is string => typeof phone === 'string' && item.phoneIds.includes(phone)))].sort()
      if (selectedPhones.length) item.phoneIds = selectedPhones
      item.request = { request_id: item.requestId, preview_id: preview.preview_id, selection_digest: preview.selection_digest,
        expected_revision: preview.expected_revision, ...(deviceRevision ? { device_config_revision: deviceRevision } : {}), ...(unknown.length ? { confirm_unknown_phone_ids: unknown } : {}) }
      await submit(item)
    } catch (cause) {
      if (!disposed) { item.phase = 'settled'; item.error = `无法准备续跑：${(cause as Error).message}` }
    }
  }
  function operationFor(phoneId: string) { return selectedOperations.value.filter(item => item.phoneIds.includes(phoneId)).at(-1) }
  onMounted(() => { mounted = true; clock = setInterval(() => { now.value = Date.now() }, 1000) })
  onBeforeUnmount(() => { disposed = true; generation++; clearInterval(clock); controls.forEach(controller => controller.abort()) })
  return { phoneControlSupported, ownershipFor, controlFor, checkOwnership, controlPhone, phoneControls, selectedRunId, selectedRun, snapshot, error, loading, stale, now, sampleSequence, operations, selectedOperations, selectRun, refresh, continuePhones, operationFor }
}
export type CollectionDiagnosticsState = ReturnType<typeof useCollectionDiagnostics>
