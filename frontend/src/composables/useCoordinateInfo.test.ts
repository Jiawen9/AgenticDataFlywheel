import { effectScope, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '@/api'
import type { CoordinateInfo, CoordinateSource } from '@/types'
import { useCoordinateInfo } from './useCoordinateInfo'

const scopes: ReturnType<typeof effectScope>[] = []
function setup(source: CoordinateSource | undefined, agent = 'pipelineagent') {
  const selection = ref(source)
  const scope = effectScope(); scopes.push(scope)
  const result = scope.run(() => useCoordinateInfo(selection, () => agent))!
  return { selection, ...result }
}
const flush = async () => { await Promise.resolve(); await Promise.resolve(); await nextTick(); await Promise.resolve() }
const source = (name: string, version = 'v1'): CoordinateSource => ({ kind: 'batch', batch_id: name, annotation_version: version, trajectory_id: 't1', image: 'step1.jpg' })
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()); vi.restoreAllMocks() })

describe('visible step coordinate lookup', () => {
  it('deduplicates visible requests and caches by batch, annotation version, trajectory and image', async () => {
    const request = vi.spyOn(api, 'coordinateInfo').mockImplementation(async src => ({ coordinate_info: { space: 'pixel', width: 1080, height: 2400 }, annotation_version: src.annotation_version! }))
    const a = setup(source('cache')), b = setup(source('cache'))
    expect(a.coordinateInfo.value.space).toBe('unknown')
    await flush()
    expect(request).toHaveBeenCalledTimes(1)
    expect(b.coordinateInfo.value.width).toBe(1080)
    setup(source('cache')); await flush(); expect(request).toHaveBeenCalledTimes(1)
    a.selection.value = source('cache', 'v2'); await flush(); expect(request).toHaveBeenCalledTimes(2)
    a.selection.value = { ...source('cache', 'v2'), image: 'step2.jpg' } as CoordinateSource
    await flush(); expect(request).toHaveBeenCalledTimes(3)
  })
  it('does not overwrite a newly selected step with a late response', async () => {
    let resolveFirst!: (value: { coordinate_info: CoordinateInfo; annotation_version: string }) => void
    vi.spyOn(api, 'coordinateInfo').mockImplementation(src => src.batch_id === 'old'
      ? new Promise(resolve => { resolveFirst = resolve })
      : Promise.resolve({ coordinate_info: { space: 'pixel', width: 720, height: 1600 }, annotation_version: 'v1' }))
    const view = setup(source('old'))
    view.selection.value = source('new'); await flush()
    resolveFirst({ coordinate_info: { space: 'normalized' }, annotation_version: 'v1' }); await flush()
    expect(view.coordinateInfo.value).toEqual({ space: 'pixel', width: 720, height: 1600 })
  })
  it('keeps explicit NewAgent display on an old server but never guesses sourced rows without an agent', async () => {
    vi.spyOn(api, 'coordinateInfo').mockRejectedValue(new ApiError('Not Found', 404))
    const native = setup(source('old-server-native'), 'newagent'), pipeline = setup(source('old-server-pipeline'))
    const missingAgent = setup(source('old-server-missing-agent'), '')
    expect(missingAgent.coordinateInfo.value.space).toBe('unknown')
    await flush()
    expect(native.coordinateInfo.value.space).toBe('normalized')
    expect(pipeline.coordinateInfo.value.space).toBe('unknown')
    expect(missingAgent.coordinateInfo.value.space).toBe('unknown')
    expect(pipeline.error.value).toContain('Not Found')
  })
  it('requires evidence even when no source lookup is available', () => {
    expect(setup(undefined, '').coordinateInfo.value.space).toBe('unknown')
    expect(setup(undefined, 'newagent').coordinateInfo.value.space).toBe('normalized')
  })
  it('uses authoritative unknown even for rows without an agent field', async () => {
    vi.spyOn(api, 'coordinateInfo').mockResolvedValue({ coordinate_info: { space: 'unknown', reason: 'source_unknown' }, annotation_version: 'v1' })
    const view = setup(source('external'), ''); await flush()
    expect(view.coordinateInfo.value).toEqual({ space: 'unknown', reason: 'source_unknown' })
  })
  it('queries the exact correction revision and rejects a conflict without reusing old coordinates', async () => {
    const request = vi.spyOn(api, 'correctionCoordinateInfo')
      .mockResolvedValueOnce({ coordinate_info: { space: 'pixel', width: 1440, height: 3200 }, storage_revision: 3 })
      .mockRejectedValueOnce(new ApiError('revision conflict', 409))
    const view = setup({ kind: 'correction', session_id: 's1', excel_row: 2, expected_revision: 3 })
    await flush(); expect(view.coordinateInfo.value.space).toBe('pixel')
    view.selection.value = { kind: 'correction', session_id: 's1', excel_row: 2, expected_revision: 4 }
    await flush()
    expect(request).toHaveBeenLastCalledWith('s1', 2, 4)
    expect(view.coordinateInfo.value.space).toBe('unknown')
    expect(view.conflict.value).toBe(true)
    expect(view.error.value).toContain('刷新')
  })
  it('does not cache a mutable latest version', async () => {
    const request = vi.spyOn(api, 'coordinateInfo').mockResolvedValue({ coordinate_info: { space: 'normalized' }, annotation_version: 'latest-resolved' })
    const latest = { ...source('latest'), annotation_version: undefined } as CoordinateSource
    setup(latest); await flush(); setup(latest); await flush()
    expect(request).toHaveBeenCalledTimes(2)
  })
})
