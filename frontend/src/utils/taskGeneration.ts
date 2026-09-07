import type { TaskGenerationJob, TaskGenerationResult, TaskGenerationTreeNode } from '@/types'
import { appNodes, findNode, leaves, nodePath } from './scenarioTree'

export type AppSelection = Record<string, string[]>
export function generationDirectory(tree: TaskGenerationTreeNode[]): TaskGenerationTreeNode[] {
  return tree.map(scene => ({ ...scene, children: (scene.children || []).filter(n => n.kind === 'capability').map(n => ({ ...n, children: [] })) }))
}
export function generationCandidates(tree: TaskGenerationTreeNode[], scope: string, query: string) {
  const root = scope ? findNode(tree, scope) : undefined
  const candidates = scope ? (root ? leaves([root]) : []) : leaves(tree)
  const search = query.trim().toLocaleLowerCase()
  return candidates.filter(n => [...nodePath(tree, n.id), ...appNodes(n).map(a => a.app || a.label)].join(' ').toLocaleLowerCase().includes(search))
}
export function reconcileApps(tree: TaskGenerationTreeNode[], selected: AppSelection) {
  const next: AppSelection = {}
  let removed = 0
  const allowed = new Map(leaves(tree).map(n => [n.id, new Set(appNodes(n).map(a => a.app || a.label))]))
  for (const [id, names] of Object.entries(selected)) {
    const kept = [...new Set(names)].filter(name => allowed.get(id)?.has(name))
    removed += names.length - kept.length
    if (kept.length) next[id] = kept
  }
  return { selected: next, removed }
}
export function selectCandidates(selected: AppSelection, nodes: TaskGenerationTreeNode[]): AppSelection {
  return { ...selected, ...Object.fromEntries(nodes.map(n => [n.id, appNodes(n).map(a => a.app || a.label)])) }
}
export const jobLabels: Record<TaskGenerationJob['status'], string> = {
  queued: '排队中', running: '生成中', succeeded: '已完成', partial: '部分成功', failed: '失败', interrupted: '已中断',
}
export function stageLabel(stage: string) {
  return ({ ...jobLabels, preparing: '准备知识库快照', generating: '生成任务与判定依赖', validation: '任务输出校验', dependency: '依赖判定', exporting: '生成导出文件', classifying: '匹配场景', augmenting: '扩增任务' } as Record<string, string>)[stage] || stage || '等待执行'
}
export const isGenerationActive = (job: TaskGenerationJob) => job.status === 'queued' || job.status === 'running'
export function unitLabel(job: TaskGenerationJob, id?: string | null) {
  if (!id) return '等待执行'
  const unit = job.execution_units?.find(unit => unit.execution_unit_id === id)
  return unit ? `${unit.scene} / ${unit.capability} / ${unit.sub_capability} · ${unit.app}` : id
}
export const resultTypeKey = (row: TaskGenerationResult) => JSON.stringify([row.scene, row.capability, row.sub_capability])
export const resultPath = (row: TaskGenerationResult) => [row.scene, row.capability, row.sub_capability].filter(Boolean).join(' / ')
export function dependencyLabel(row: TaskGenerationResult) {
  if (row.dependency_error) return '依赖判定异常'
  return ({ zero: '无依赖', weak: '弱依赖', strong: '强依赖', pre_node: '前置任务' } as Record<string, string>)[row.pre_dependency || ''] || '未标注依赖'
}
export interface ResultGroup { main: TaskGenerationResult; prerequisites: TaskGenerationResult[]; orphan: boolean }
export function groupGenerationResults(rows: TaskGenerationResult[]): ResultGroup[] {
  const prerequisites = rows.filter(row => row.pre_dependency === 'pre_node')
  const attached = new Set<string>()
  const groups: ResultGroup[] = rows.filter(row => row.pre_dependency !== 'pre_node').map(main => {
    const pre = prerequisites.filter(row => Boolean(main.pre_task_uuid && row.task_uuid === main.pre_task_uuid) || Boolean(main.dependency_group_id && row.dependency_group_id === main.dependency_group_id))
    pre.forEach(row => attached.add(row.result_id))
    return { main, prerequisites: pre, orphan: false }
  })
  for (const row of prerequisites) if (!attached.has(row.result_id)) groups.push({ main: row, prerequisites: [], orphan: true })
  return groups
}
export interface ResultFilters { app: string; type: string; dependency: string; showDeleted: boolean }
export function filterResultGroups(groups: ResultGroup[], filter: ResultFilters) {
  return groups.filter(({ main }) => (filter.showDeleted || !main.deleted)
    && (!filter.app || main.app === filter.app) && (!filter.type || resultTypeKey(main) === filter.type)
    && (!filter.dependency || (filter.dependency === 'error' ? Boolean(main.dependency_error) : !main.dependency_error && main.pre_dependency === filter.dependency)))
}
export function resultCounts(rows: TaskGenerationResult[]) {
  const active = rows.filter(row => !row.deleted)
  return { main: active.filter(row => row.pre_dependency !== 'pre_node').length, prerequisites: active.filter(row => row.pre_dependency === 'pre_node').length, strong: active.filter(row => row.pre_dependency === 'strong').length }
}
