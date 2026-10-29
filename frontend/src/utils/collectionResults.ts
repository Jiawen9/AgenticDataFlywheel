import type { CollectionBatchLineage, FactoryRun } from '@/phoneFactoryApi'

export function isCollectionResult(value: CollectionBatchLineage): boolean {
  return value.batch_role === 'collection_result'
}

export function collectionTaskSource(run: FactoryRun): string {
  return run.task_source_batch_id || run.batch_id || ''
}

export function collectionRunBelongsToSource(run: FactoryRun, sourceId: string): boolean {
  return !sourceId || collectionTaskSource(run) === sourceId
}

export function collectionResultLocation(run: FactoryRun) {
  return { path: '/collection/tree-building', query: { batch_id: run.batch_id || '' } }
}
