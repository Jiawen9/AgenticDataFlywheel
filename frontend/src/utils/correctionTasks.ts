import type { CorrectionGroupSummary, CorrectionRecommendation, CorrectionTaskItem } from '@/types'

/** Join against the session's saved selection, never the latest recommendation. */
export function correctionTasks(selection: CorrectionRecommendation, groups: CorrectionGroupSummary[]): CorrectionTaskItem[] {
  const byTrajectory = new Map(groups.map((group) => [group.meta_task, group]))
  const tasks = new Map<string, CorrectionTaskItem>()
  const usedGroups = new Set<string>()
  for (const item of selection.tasks) {
    const group = byTrajectory.get(item.trajectory_id)
    if (!group || usedGroups.has(group.group_id)) continue
    usedGroups.add(group.group_id)
    let task = tasks.get(item.task_id)
    if (!task) {
      task = { task_id: item.task_id, goal: item.goal, trajectories: [], edited_row_count: 0, export_count: 0 }
      tasks.set(item.task_id, task)
    }
    task.trajectories.push({ trajectory_id: item.trajectory_id, rank: 0, global_score: item.global_score, passed_threshold: item.passed_threshold, group })
    task.edited_row_count += group.edited_row_count
    task.export_count += Number(group.export)
  }
  for (const task of tasks.values()) {
    // Stable sorting preserves selection/workbook order for tied scores.
    task.trajectories.sort((a, b) => b.global_score - a.global_score)
    task.trajectories.forEach((trajectory, index) => { trajectory.rank = index + 1 })
  }
  return [...tasks.values()]
}
