import type { ChecklistQualityEvaluation, QualityJob, QualityNodeScore, TaskQualityResult, TrajectoryQualityEvaluation, TrajectoryTreeNode } from '@/types'

export function unitScore(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1 ? value : null
}

export function fusionScore(evaluation?: TrajectoryQualityEvaluation | null): number | null {
  return evaluation?.fusion?.valid ? unitScore(evaluation.fusion.score) : null
}

export interface HighestFusionTrajectories {
  trajectoryIds: string[]
  score: number | null
  scoredCount: number
  totalCount: number
  partial: boolean
}

/** Only trajectories that really end in the current task's tree participate. */
export function highestFusionTrajectories(tree: TrajectoryTreeNode | null, quality: TaskQualityResult | null, taskId: string): HighestFusionTrajectories {
  const terminalOrder: string[] = []
  function collect(node: TrajectoryTreeNode) {
    terminalOrder.push(...node.terminal_trajectories)
    node.children.forEach(collect)
  }
  if (tree) collect(tree)
  const terminals = new Set(terminalOrder)
  const result: HighestFusionTrajectories = { trajectoryIds: [], score: null, scoredCount: 0, totalCount: terminals.size, partial: true }
  if (!tree || !taskId || (tree.task_id && tree.task_id !== taskId) || !quality || quality.task_id !== taskId) return result
  const order = [...new Set([...(quality.fusion_order ?? []), ...(quality.trajectory_order ?? []),
    ...(tree.source_trajectories ?? []).map(item => item.trajectory), ...terminalOrder])].filter(id => terminals.has(id))
  for (const id of order) {
    const value = fusionScore(quality.evaluations[id])
    if (value === null) continue
    ++result.scoredCount
    // Compare the original F, not the three-decimal display or node Q.
    if (result.score === null || value > result.score) {
      result.score = value
      result.trajectoryIds = [id]
    } else if (value === result.score) result.trajectoryIds.push(id)
  }
  result.partial = quality.result_state === 'partial' || quality.fusion_complete !== true || result.scoredCount < result.totalCount
  return result
}

/** Follow a real ancestor chain, including a trajectory ending before a child. */
export function trajectoryPath(root: TrajectoryTreeNode, trajectoryId?: string): { nodeIds: number[]; edgeIds: string[]; terminalNodeId: number | null } {
  function find(node: TrajectoryTreeNode, ancestors: number[]): number[] | null {
    const path = [...ancestors, node.id]
    if (node.terminal_trajectories.includes(trajectoryId ?? '')) return path
    for (const child of node.children) {
      const found = find(child, path)
      if (found) return found
    }
    return null
  }
  const nodeIds = trajectoryId ? find(root, []) ?? [] : []
  return { nodeIds, edgeIds: nodeIds.slice(1).map((id, index) => `${nodeIds[index]}-${id}`), terminalNodeId: nodeIds.at(-1) ?? null }
}

/** Shared nodes can represent another trajectory's image; prefer the highlighted one. */
export function preferredTrajectorySelection(node: TrajectoryTreeNode, trajectoryId?: string): { occurrenceIndex: number; terminalTrajectoryId: string } {
  const index = node.occurrences.findIndex(item => item.trajectory === trajectoryId)
  return { occurrenceIndex: index < 0 ? 0 : index,
    terminalTrajectoryId: trajectoryId && node.terminal_trajectories.includes(trajectoryId) ? trajectoryId : node.terminal_trajectories[0] ?? '' }
}

export function treeTopology(root: TrajectoryTreeNode): string {
  const structure: [number, number[]][] = []
  function visit(node: TrajectoryTreeNode) {
    structure.push([node.id, node.children.map(child => child.id)])
    node.children.forEach(visit)
  }
  visit(root)
  return JSON.stringify(structure)
}

export function nodeScoreLabel(score?: QualityNodeScore, root = false): string {
  const q = unitScore(score?.q), prefix = root ? '任务均分 Q' : 'Q'
  if (!score) return `${prefix} 待评分`
  if (q === null || !score.scored_count) return `${prefix} 暂无有效评分`
  const coverage = score.scored_count < score.total_count ? ` · ${score.scored_count}/${score.total_count}` : ''
  return `${prefix} ${q.toFixed(3)}${coverage}`
}

export function checklistRate(checklist?: ChecklistQualityEvaluation): number | null {
  if (!checklist || typeof checklist.total !== 'number' || checklist.total <= 0 || typeof checklist.passed !== 'number' || checklist.passed < 0 || checklist.passed > checklist.total) return null
  return checklist.passed / checklist.total
}

export function scoreText(value: unknown, digits = 3): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '未评测'
}

export function detailText(value: unknown): string {
  if (value === undefined || value === null || value === '') return '—'
  return typeof value === 'string' ? value : JSON.stringify(value)
}

export function qualityJobStageLabel(job: Pick<QualityJob, 'status' | 'stage'>): string {
  if (job.status === 'queued') return '排队等待'
  if (job.status === 'succeeded') return '评分作业已结束'
  if (job.status === 'interrupted') return '服务重启中断'
  if (job.status === 'failed') return '质检失败'
  const labels: Partial<Record<QualityJob['stage'], string>> = {
    preparing: '准备质检输入', generating_rubric: '生成 Rubric', evaluating: 'Rubric 评分中',
    checklist_queued: 'Checklist 排队', checklist_preparing: '准备 Checklist', checklist_definition: '生成 Checklist 检查项', checklist_evaluating: 'Checklist 评测中',
    fusing: '融合评分与节点 Q', publishing: '保存质检结果',
  }
  return labels[job.stage] ?? '质检执行中'
}

export function qualityJobProgressText(job: QualityJob): string {
  if (job.total_checklist_trajectories === undefined) return `${job.completed_trajectories} / ${job.total_trajectories || '—'} 条轨迹`
  const parts = [
    `Rubric ${job.completed_rubric_trajectories ?? 0}/${job.total_rubric_trajectories ?? job.total_trajectories}`,
    `Checklist 已结束 ${job.completed_checklist_trajectories ?? 0}/${job.total_checklist_trajectories}`,
    `融合有效 ${job.fusion_eligible_count ?? 0}/${job.total_trajectories}`,
  ]
  if (job.failed_checklist_trajectories) parts.push(`Checklist 失败 ${job.failed_checklist_trajectories}`)
  if (job.partial_results_available) parts.push('已保留部分结果')
  return parts.join(' · ')
}

export function qualityModelProgressText(job: QualityJob): string {
  const event = job.checklist_event, parts: string[] = []
  const role = event?.role || job.current_model_role, model = event?.model || job.current_model
  if (role) parts.push(`角色 ${role}`)
  if (model) parts.push(`模型 ${model}`)
  if (event?.attempt !== undefined) parts.push(`第 ${event.attempt} 次请求`)
  if (event?.elapsed_seconds !== undefined) parts.push(`耗时 ${event.elapsed_seconds.toFixed(1)} 秒`)
  if (event?.timeout_seconds !== undefined) parts.push(`超时预算 ${event.timeout_seconds} 秒`)
  return parts.join(' · ')
}
