import { computed, ref } from 'vue'
import type { api as appApi } from '@/api'
import type { CorrectionExport, CorrectionGroup, CorrectionGroupSummary, CorrectionRow, CorrectionSession } from '@/types'
import { correctionTasks } from '@/utils/correctionTasks'

type WorkspaceApi = Pick<typeof appApi, 'correctionGroup' | 'patchCorrectionRow' | 'patchCorrectionExport' | 'correctionExport'>
export type ActionDecision = 'save' | 'discard' | 'cancel'
interface Feedback {
  error: (message: string) => void
  actionDecision: () => Promise<ActionDecision>
}

/** All asynchronous operations retain their session/group identity. */
export function useCorrectionWorkspace(api: WorkspaceApi, feedback: Feedback) {
  const session = ref<CorrectionSession | null>(null)
  const expandedTasks = ref<string[]>([])
  const openGroupId = ref('')
  const activeGroup = ref<CorrectionGroup | null>(null)
  const activeRowId = ref<number | null>(null)
  const actionDraft = ref<string | null>(null)
  const actionRevision = ref(0)
  const loadingGroup = ref(false)
  const groupError = ref('')
  const savingCount = ref(0)
  const guarding = ref(false)
  const cache = new Map<string, CorrectionGroup>()
  const rememberedRows = new Map<string, number>()
  const pendingWrites = new Set<Promise<boolean>>()
  let epoch = 0
  let detailRequest = 0

  const tasks = computed(() => session.value ? correctionTasks(session.value.selection, session.value.groups) : [])
  const activeRow = computed(() => activeGroup.value?.rows.find((row) => row.excel_row === activeRowId.value) ?? null)
  const hasUnsaved = computed(() => actionDraft.value !== null)
  const busy = computed(() => savingCount.value > 0 || guarding.value)
  const key = (sessionId: string, groupId: string) => `${sessionId}:${groupId}`

  function clearDetail() {
    ++detailRequest
    openGroupId.value = ''
    activeGroup.value = null
    activeRowId.value = null
    actionDraft.value = null
    loadingGroup.value = false
    groupError.value = ''
  }

  function setSession(value: CorrectionSession | null) {
    ++epoch
    clearDetail()
    session.value = value
    expandedTasks.value = []
    cache.clear()
  }

  function selectRow(row: CorrectionRow | null) {
    activeRowId.value = row?.excel_row ?? null
    actionDraft.value = null
    ++actionRevision.value
    if (row && session.value && activeGroup.value) rememberedRows.set(key(session.value.session_id, activeGroup.value.group_id), row.excel_row)
  }

  function applySummary(summary: CorrectionGroupSummary) {
    if (!session.value) return
    session.value.groups = session.value.groups.map((group) => group.group_id === summary.group_id ? summary : group)
    const cacheKey = key(session.value.session_id, summary.group_id)
    const previous = cache.get(cacheKey)
    if (previous) {
      const updated = { ...previous, ...summary }
      cache.set(cacheKey, updated)
      if (activeGroup.value?.group_id === summary.group_id) activeGroup.value = updated
    }
  }

  function trackWrite(operation: () => Promise<boolean>): Promise<boolean> {
    savingCount.value++
    const result = operation().catch((error: unknown) => {
      feedback.error(error instanceof Error ? error.message : String(error))
      return false
    })
    pendingWrites.add(result)
    void result.finally(() => { pendingWrites.delete(result); savingCount.value-- })
    return result
  }

  async function patchRow(row: CorrectionRow, patch: { actions?: string; sop?: string; deleted?: boolean }): Promise<boolean> {
    if (!session.value || !activeGroup.value) return false
    const sessionId = session.value.session_id, groupId = activeGroup.value.group_id, version = epoch
    return trackWrite(async () => {
      const result = await api.patchCorrectionRow(sessionId, row.excel_row, patch)
      if (epoch !== version || session.value?.session_id !== sessionId) return false
      if (result.group.group_id !== groupId || result.row.excel_row !== row.excel_row) throw new Error('保存响应与当前轨迹不一致，请重新加载')
      const cacheKey = key(sessionId, groupId)
      const previous = cache.get(cacheKey)
      if (previous) cache.set(cacheKey, { ...previous, rows: previous.rows.map((item) => item.excel_row === row.excel_row ? result.row : item) })
      applySummary(result.group)
      return true
    })
  }

  async function saveAction(actions = actionDraft.value): Promise<boolean> {
    if (!activeRow.value || actions === null) return true
    const success = await patchRow(activeRow.value, { actions })
    if (success) { actionDraft.value = null; ++actionRevision.value }
    return success
  }

  async function prepareTransition(): Promise<boolean> {
    if (guarding.value) return false
    guarding.value = true
    try {
      if ((await Promise.all([...pendingWrites])).some((success) => !success)) return false
      if (actionDraft.value !== null) {
        const decision = await feedback.actionDecision()
        if (decision === 'cancel') return false
        if (decision === 'save' && !await saveAction()) return false
        if (decision === 'discard') { actionDraft.value = null; ++actionRevision.value }
      }
      return true
    } finally { guarding.value = false }
  }

  async function loadGroup(groupId: string) {
    if (!session.value) return
    clearDetail()
    openGroupId.value = groupId
    const sessionId = session.value.session_id, version = epoch, request = ++detailRequest
    const cacheKey = key(sessionId, groupId)
    loadingGroup.value = true
    try {
      const group = cache.get(cacheKey) ?? await api.correctionGroup(sessionId, groupId)
      if (epoch !== version || request !== detailRequest) return
      cache.set(cacheKey, group)
      activeGroup.value = group
      selectRow(group.rows.find((row) => row.excel_row === rememberedRows.get(cacheKey)) ?? group.rows.find((row) => !row.deleted) ?? group.rows[0] ?? null)
    } catch (error) {
      if (epoch === version && request === detailRequest) groupError.value = error instanceof Error ? error.message : String(error)
    } finally {
      if (epoch === version && request === detailRequest) loadingGroup.value = false
    }
  }

  async function toggleTask(taskId: string) {
    if (guarding.value) return
    if (expandedTasks.value.includes(taskId)) {
      const closesEditor = tasks.value.find((task) => task.task_id === taskId)?.trajectories.some((item) => item.group.group_id === openGroupId.value)
      if (closesEditor) {
        if (!await prepareTransition()) return
        clearDetail()
      }
      expandedTasks.value = expandedTasks.value.filter((id) => id !== taskId)
    } else expandedTasks.value.push(taskId)
  }

  async function toggleTrajectory(groupId: string) {
    if (!await prepareTransition()) return
    if (openGroupId.value === groupId) clearDetail()
    else await loadGroup(groupId)
  }

  async function chooseRow(row: CorrectionRow) {
    if (row.excel_row === activeRowId.value || !await prepareTransition()) return
    selectRow(activeGroup.value?.rows.find((item) => item.excel_row === row.excel_row) ?? null)
  }

  async function toggleExport(group: CorrectionGroupSummary) {
    if (!session.value || busy.value) return
    const sessionId = session.value.session_id, version = epoch
    await trackWrite(async () => {
      const summary = await api.patchCorrectionExport(sessionId, group.group_id, !group.export)
      if (epoch !== version || session.value?.session_id !== sessionId) return false
      applySummary(summary)
      return true
    })
  }

  async function toggleDeleted(row: CorrectionRow, confirm: () => Promise<boolean>) {
    if (!await prepareTransition()) return
    const version = epoch, groupId = openGroupId.value
    if (!row.deleted && !await confirm()) return
    if (epoch !== version || openGroupId.value !== groupId) return
    await patchRow(row, { deleted: !row.deleted })
  }

  async function exportData(): Promise<CorrectionExport | null> {
    if (!session.value || !await prepareTransition()) return null
    const current = session.value
    let output: Awaited<ReturnType<WorkspaceApi['correctionExport']>> | null = null
    await trackWrite(async () => {
      output = await api.correctionExport(current.session_id)
      current.exports.unshift(output)
      return true
    })
    return output
  }

  return { session, tasks, expandedTasks, openGroupId, activeGroup, activeRow, actionDraft, actionRevision, loadingGroup, groupError, savingCount, guarding, busy, hasUnsaved, setSession, loadGroup, toggleTask, toggleTrajectory, chooseRow, saveAction, toggleExport, toggleDeleted, prepareTransition, exportData }
}
