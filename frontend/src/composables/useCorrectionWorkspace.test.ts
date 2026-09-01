import { describe, expect, it, vi } from 'vitest'
import { useCorrectionWorkspace, type ActionDecision } from './useCorrectionWorkspace'
import { correctionTasks } from '@/utils/correctionTasks'
import type { CorrectionGroup, CorrectionGroupSummary, CorrectionRow, CorrectionSession } from '@/types'

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value))
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const tick = async () => { for (let i = 0; i < 8; i++) await Promise.resolve() }

function fixture() {
  const groups: CorrectionGroup[] = [0, 1, 2, 3].map((index) => {
    const trajectory = index < 3 ? `TASK-A-${index + 1}` : 'TASK-B-1'
    const rows: CorrectionRow[] = [0, 1].map((step) => ({
      excel_row: index * 2 + step + 2, step: step + 1, task: index < 3 ? '任务甲' : '任务乙', meta_task: trajectory,
      image: `${trajectory}/step${step}.jpg`, image_url: '', xml: '', actions: '{"action":"click","coordinate":[10,20]}', action: { action: 'click' },
      sop: `原始 SOP ${index}-${step}`, summary: '点击搜索', task_manual_result: '', micro_manual: '', macro_manual: '', micro_pred: '', macro_pred: '',
      Bad_Interval: '', trajectory_quality_type: '', actions_box: '', deleted: false, edited: false, action_edited: false, sop_edited: false, edit_status: '',
    }))
    return { group_id: `group_${index}`, task: rows[0]!.task, meta_task: trajectory, quality: '未知', prefix: '', export: false, row_count: 2, active_row_count: 2, edited_row_count: 0, action_edit_count: 0, rows }
  })
  const summary = (group: CorrectionGroup): CorrectionGroupSummary => { const { rows: _rows, ...value } = group; return clone(value) }
  const saved: CorrectionSession = {
    session_id: 'old-session', tree_run_id: 'old-batch', source_id: 'fixed', source: null, created_at: '', updated_at: '', row_count: 8, group_count: 4,
    groups: groups.map(summary), exports: [], selection: { status: 'ready', tasks: groups.map((group, i) => ({
      task_id: i < 3 ? 'TASK-A' : 'TASK-B', goal: group.task, trajectory_id: group.meta_task, global_score: i === 2 ? 3 : 4,
      passed_threshold: true, trajectory_count: i < 3 ? 3 : 1, step_count: 2,
    })) },
  }
  const api = {
    correctionGroup: vi.fn(async (_session: string, id: string) => clone(groups.find((group) => group.group_id === id)!)),
    patchCorrectionRow: vi.fn(async (_session: string, rowId: number, patch: { actions?: string; sop?: string; deleted?: boolean }) => {
      const group = groups.find((group) => group.rows.some((row) => row.excel_row === rowId))!
      const row = group.rows.find((row) => row.excel_row === rowId)!
      Object.assign(row, patch)
      row.edited = true
      group.edited_row_count = group.rows.filter((item) => item.edited).length
      group.active_row_count = group.rows.filter((item) => !item.deleted).length
      return { group: summary(group), row: clone(row) }
    }),
    patchCorrectionExport: vi.fn(async (_session: string, id: string, exported: boolean) => {
      const group = groups.find((group) => group.group_id === id)!
      group.export = exported
      return summary(group)
    }),
    correctionExport: vi.fn(async (_session: string) => ({ export_id: 'export', filename: 'review.xlsx', created_at: '', download_url: '', sheets: {} })),
  }
  const feedback = { error: vi.fn(), actionDecision: vi.fn(async (): Promise<ActionDecision> => 'save') }
  const ws = useCorrectionWorkspace(api, feedback)
  ws.setSession(clone(saved))
  return { ws, api, feedback, groups, saved }
}

describe('correction task list and protected workbench', () => {
  it('groups three trajectories under one case ID, keeps tie order and old-session metadata', () => {
    const { saved } = fixture()
    saved.groups[1]!.export = true
    saved.groups[2]!.edited_row_count = 2
    const result = correctionTasks(saved.selection, saved.groups)
    expect(result.map((task) => task.task_id)).toEqual(['TASK-A', 'TASK-B'])
    expect(result[0]!.trajectories.map((item) => [item.trajectory_id, item.rank])).toEqual([['TASK-A-1', 1], ['TASK-A-2', 2], ['TASK-A-3', 3]])
    expect(result[0]).toMatchObject({ export_count: 1, edited_row_count: 2 })
    expect(saved.selection.tasks[0]!.trajectory_id).toBe('TASK-A-1')
  })

  it('starts collapsed and only fetches steps after opening a trajectory', async () => {
    const { ws, api } = fixture()
    expect(ws.expandedTasks.value).toEqual([])
    expect(ws.openGroupId.value).toBe('')
    await ws.toggleTask('TASK-A')
    await ws.toggleTask('TASK-B')
    expect(api.correctionGroup).not.toHaveBeenCalled()
    await ws.toggleTrajectory('group_0')
    expect(api.correctionGroup).toHaveBeenCalledTimes(1)
    await ws.chooseRow(ws.activeGroup.value!.rows[1]!)
    await ws.toggleTrajectory('group_1')
    expect(ws.openGroupId.value).toBe('group_1')
    await ws.toggleTrajectory('group_0')
    expect(ws.activeRow.value!.step).toBe(2)
    expect(api.correctionGroup).toHaveBeenCalledTimes(2)
    await ws.toggleTask('TASK-A')
    expect(ws.openGroupId.value).toBe('')
    expect(ws.expandedTasks.value).toEqual(['TASK-B'])
  })

  it('ignores a delayed trajectory response after a faster selection', async () => {
    const { ws, api, groups } = fixture()
    const slow = deferred<CorrectionGroup>()
    api.correctionGroup.mockImplementationOnce(() => slow.promise)
    const first = ws.toggleTrajectory('group_0')
    await tick()
    await ws.toggleTrajectory('group_1')
    slow.resolve(clone(groups[0]!))
    await first
    expect(ws.activeGroup.value!.group_id).toBe('group_1')
    expect(ws.activeRow.value!.meta_task).toBe('TASK-A-2')
  })

  it('ignores delayed details and saves when the session changes, even with the same group ID', async () => {
    const { ws, api, saved, groups } = fixture()
    const detail = deferred<CorrectionGroup>()
    api.correctionGroup.mockImplementationOnce(() => detail.promise)
    const load = ws.toggleTrajectory('group_0')
    await tick()
    ws.setSession({ ...clone(saved), session_id: 'other-session' })
    detail.resolve(clone(groups[0]!))
    await load
    expect(ws.activeGroup.value).toBeNull()
    await ws.toggleTrajectory('group_0')
    const write = deferred<{ group: CorrectionGroupSummary; row: CorrectionRow }>()
    api.patchCorrectionRow.mockImplementationOnce(() => write.promise)
    const saving = ws.saveAction('{"action":"wait"}')
    ws.setSession(clone(saved))
    write.resolve({ group: { ...groups[0]!, edited_row_count: 1 }, row: { ...groups[0]!.rows[0]!, actions: '{"action":"wait"}' } })
    expect(await saving).toBe(false)
    expect(ws.session.value!.groups[0]!.edited_row_count).toBe(0)
  })

  it('shows a failed detail request in place and retries without opening another workbench', async () => {
    const { ws, api } = fixture()
    api.correctionGroup.mockRejectedValueOnce(new Error('截图来源不可读'))
    await ws.toggleTrajectory('group_0')
    expect(ws.groupError.value).toBe('截图来源不可读')
    expect(ws.activeGroup.value).toBeNull()
    await ws.loadGroup('group_0')
    expect(ws.groupError.value).toBe('')
    expect(ws.activeGroup.value!.group_id).toBe('group_0')
  })

  it('waits for an in-flight action save and does not duplicate it on collapse', async () => {
    const { ws, api, groups } = fixture()
    await ws.toggleTrajectory('group_0')
    const write = deferred<{ group: CorrectionGroupSummary; row: CorrectionRow }>()
    api.patchCorrectionRow.mockImplementationOnce(() => write.promise)
    ws.actionDraft.value = '{"action":"wait"}'
    const collapse = ws.toggleTrajectory('group_0')
    await tick()
    expect(ws.openGroupId.value).toBe('group_0')
    write.resolve({ group: groups[0]!, row: { ...groups[0]!.rows[0]!, actions: '{"action":"wait"}' } })
    await collapse
    expect(ws.openGroupId.value).toBe('')
    expect(api.patchCorrectionRow).toHaveBeenCalledTimes(1)
  })

  it('retains the action draft and active step when saving fails before export', async () => {
    const { ws, api } = fixture()
    await ws.toggleTrajectory('group_0')
    ws.actionDraft.value = '{"action":"wait"}'
    api.patchCorrectionRow.mockRejectedValue(new Error('保存失败'))
    await ws.chooseRow(ws.activeGroup.value!.rows[1]!)
    expect(ws.activeRow.value!.step).toBe(1)
    expect(ws.actionDraft.value).toBe('{"action":"wait"}')
    expect(await ws.exportData()).toBeNull()
    expect(api.correctionExport).not.toHaveBeenCalled()
  })

  it.each<ActionDecision>(['save', 'discard', 'cancel'])('protects unsaved actions on collapse: %s', async (decision) => {
    const { ws, api, feedback } = fixture()
    await ws.toggleTask('TASK-A')
    await ws.toggleTrajectory('group_0')
    ws.actionDraft.value = '{"action":"wait"}'
    feedback.actionDecision.mockResolvedValue(decision)
    await ws.toggleTask('TASK-A')
    expect(ws.openGroupId.value).toBe(decision === 'cancel' ? 'group_0' : '')
    expect(api.patchCorrectionRow).toHaveBeenCalledTimes(decision === 'save' ? 1 : 0)
    expect(ws.actionDraft.value).toBe(decision === 'cancel' ? '{"action":"wait"}' : null)
  })

  it('cancels navigation after an action-save failure and rejects overlapping decisions', async () => {
    const { ws, api, feedback } = fixture()
    await ws.toggleTrajectory('group_0')
    const decision = deferred<ActionDecision>()
    feedback.actionDecision.mockReturnValueOnce(decision.promise)
    ws.actionDraft.value = '{"action":"wait"}'
    const navigation = ws.prepareTransition()
    await tick()
    expect(await ws.prepareTransition()).toBe(false)
    api.patchCorrectionRow.mockRejectedValueOnce(new Error('失败'))
    decision.resolve('save')
    expect(await navigation).toBe(false)
    expect(ws.hasUnsaved.value).toBe(true)
    expect(ws.openGroupId.value).toBe('group_0')
  })

  it('keeps edits, deletion and export flags independent across trajectories', async () => {
    const { ws } = fixture()
    await ws.toggleTrajectory('group_0')
    ws.actionDraft.value = '{"action":"wait"}'
    await ws.saveAction()
    await ws.toggleExport(ws.session.value!.groups[0]!)
    await ws.toggleTrajectory('group_1')
    expect(ws.activeRow.value!.actions).toBe('{"action":"click","coordinate":[10,20]}')
    await ws.toggleDeleted(ws.activeRow.value!, async () => true)
    expect(ws.activeRow.value!.deleted).toBe(true)
    await ws.toggleDeleted(ws.activeRow.value!, async () => true)
    expect(ws.activeRow.value!.deleted).toBe(false)
    await ws.toggleTrajectory('group_0')
    expect(ws.activeRow.value!.actions).toBe('{"action":"wait"}')
    expect(ws.activeGroup.value!.export).toBe(true)
    expect(ws.session.value!.groups[1]!.export).toBe(false)
    expect(ws.tasks.value[0]!.export_count).toBe(1)
  })

  it('saves pending action changes before exporting, and reloads saved sessions collapsed', async () => {
    const { ws, api } = fixture()
    await ws.toggleTrajectory('group_0')
    ws.actionDraft.value = '{"action":"wait"}'
    expect(await ws.exportData()).toMatchObject({ filename: 'review.xlsx' })
    expect(api.patchCorrectionRow).toHaveBeenCalledTimes(1)
    expect(api.correctionExport.mock.invocationCallOrder[0]).toBeGreaterThan(api.patchCorrectionRow.mock.invocationCallOrder[0]!)
    const saved = clone(ws.session.value!)
    ws.setSession(saved)
    expect(ws.openGroupId.value).toBe('')
    expect(ws.expandedTasks.value).toEqual([])
    await ws.toggleTrajectory('group_0')
    expect(ws.activeRow.value!.actions).toBe('{"action":"wait"}')
  })
})
