import type { AugmentationSeed, TaskGenerationResult } from '@/types'

export interface AugmentationResultGroup {
  id: string
  seedId?: string
  sourceRow?: number
  sourceTask: string
  app: string
  incompleteSource: boolean
  results: TaskGenerationResult[]
  activeCount: number
}

function sourceRow(value: unknown): number | undefined {
  if (typeof value !== 'number' && (typeof value !== 'string' || !/^[1-9]\d*$/.test(value))) return undefined
  const row = Number(value)
  return Number.isSafeInteger(row) && row > 0 ? row : undefined
}

export function augmentationResultGroupId(row: TaskGenerationResult): string {
  if (row.seed_id) return `seed:${row.seed_id}`
  const index = sourceRow(row.source_row)
  return index === undefined ? `result:${row.result_id}` : `row:${index}`
}

/** Stable source identities keep duplicate failure texts as separate cases. */
export function groupAugmentationResults(results: TaskGenerationResult[], seeds: AugmentationSeed[]): AugmentationResultGroup[] {
  const seedsById = new Map(seeds.map(seed => [seed.seed_id, seed]))
  const groups = new Map<string, AugmentationResultGroup>()
  for (const row of results) {
    const id = augmentationResultGroupId(row)
    let group = groups.get(id)
    if (!group) {
      const seed = row.seed_id ? seedsById.get(row.seed_id) : undefined
      const index = sourceRow(seed?.source_row ?? row.source_row)
      group = {
        id, seedId: row.seed_id, sourceRow: index,
        sourceTask: seed?.task || row.source_task || row['源失败任务'] || '历史来源未记录',
        app: seed?.app || row.app, incompleteSource: !row.seed_id && index === undefined,
        results: [], activeCount: 0,
      }
      groups.set(id, group)
    }
    group.results.push(row)
    if (!row.deleted) group.activeCount++
  }
  return [...groups.values()]
}

