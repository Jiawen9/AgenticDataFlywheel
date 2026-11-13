import type { ChecklistQualityEvaluation, QualityJob, QualityNodeScore, TrajectoryQualityEvaluation } from '@/types'

export function unitScore(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1 ? value : null
}

export function fusionScore(evaluation?: TrajectoryQualityEvaluation | null): number | null {
  return evaluation?.fusion?.valid ? unitScore(evaluation.fusion.score) : null
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
    checklist_preparing: '准备 Checklist', checklist_evaluating: 'Checklist 评测中',
    fusing: '融合评分与节点 Q', publishing: '保存质检结果',
  }
  return labels[job.stage] ?? '质检执行中'
}
