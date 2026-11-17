import { request } from '@/api'
import type { KnowledgeBaseSummary, TaskGenerationJob, TaskGenerationResult, TaskGenerationTree } from '@/types'
import { sharedRead, summaryKey, summaryParams, type SummaryPage, type SummaryQuery } from '@/utils/summaryCache'

export function generationHistory(query: SummaryQuery) {
  const path = '/api/task-generation/jobs'
  return sharedRead(summaryKey(path, query), () => request<SummaryPage<TaskGenerationJob>>(`${path}?${summaryParams(query)}`), { persist: true })
}
export interface GenerationResultPage {
  results: TaskGenerationResult[]; errors: TaskGenerationJob['errors']; total: number; offset: number; limit: number; has_more: boolean
  counts: { all: number; active: number; main: number; prerequisites: number; strong: number }
  apps: string[]; types: [string, string][]
}
export function generationResults(jobId: string, query: SummaryQuery) {
  const params = new URLSearchParams({ view: 'groups' })
  for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== '') params.set(key, String(value))
  return request<GenerationResultPage>(`/api/task-generation/jobs/${encodeURIComponent(jobId)}/results?${params}`)
}
interface ResourceView { index_state: string; tree?: TaskGenerationTree; knowledge_bases: KnowledgeBaseSummary[]; error?: string; stale?: boolean }
/** First request only schedules workbook parsing. Subsequent requests read its versioned snapshot. */
export async function generationResources(signal: AbortSignal, retry = false): Promise<ResourceView & { tree: TaskGenerationTree }> {
  let initial = true
  while (!signal.aborted) {
    if (typeof document === 'undefined' || !document.hidden) {
      const path = `/api/task-generation/tree?view=summary${initial && retry ? '&refresh=true' : ''}`
      const value = await sharedRead(summaryKey(path), () => request<ResourceView>(path), { freshMs: 0 })
      initial = false
      if (signal.aborted) break
      if (value.index_state === 'ready' && value.tree) return { ...value, tree: value.tree }
      if (value.index_state === 'failed') throw new Error(value.error || '生成资源读取失败')
    }
    await new Promise<void>(resolve => {
      const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve() }
      const timer = setTimeout(finish, 1000)
      signal.addEventListener('abort', finish, { once: true })
    })
  }
  throw new DOMException('页面已离开', 'AbortError')
}
