import { computed, ref, toValue, watch, type MaybeRefOrGetter } from 'vue'
import { api, ApiError } from '@/api'
import type { CoordinateInfo, CoordinateSource, TrajectoryScope } from '@/types'

const cache = new Map<string, CoordinateInfo>()
const pending = new Map<string, Promise<CoordinateInfo>>()
const MAX_CACHE = 256

export function batchCoordinateSource(scope: TrajectoryScope | undefined, trajectory: string, image: string): CoordinateSource | undefined {
  return scope?.batch_id && trajectory && image
    ? { kind: 'batch', ...scope, trajectory_id: trajectory, image }
    : undefined
}

export function coordinateSourceKey(source: CoordinateSource): string {
  return source.kind === 'batch'
    ? JSON.stringify(['batch', source.batch_id, source.annotation_version ?? '', source.trajectory_id, source.image])
    : JSON.stringify(['correction', source.session_id, source.excel_row, source.expected_revision ?? ''])
}

async function load(source: CoordinateSource): Promise<CoordinateInfo> {
  const key = coordinateSourceKey(source)
  const cached = cache.get(key)
  if (cached) return cached
  const active = pending.get(key)
  if (active) return active
  const task = (async () => {
    const response = source.kind === 'batch'
      ? await api.coordinateInfo(source)
      : await api.correctionCoordinateInfo(source.session_id, source.excel_row, source.expected_revision)
    if (source.kind === 'batch' && source.annotation_version && 'annotation_version' in response
        && response.annotation_version !== source.annotation_version) {
      throw new ApiError('标框版本已变化，请刷新步骤后重试', 409)
    }
    if (source.kind === 'correction' && source.expected_revision !== undefined && 'storage_revision' in response
        && response.storage_revision !== source.expected_revision) {
      throw new ApiError('修正会话已变化，请刷新步骤后重试', 409)
    }
    const info = response.coordinate_info
    // A request for the latest version is not an immutable cache identity.
    if ((source.kind === 'batch' && source.annotation_version) || (source.kind === 'correction' && source.expected_revision !== undefined)) {
      cache.set(key, info)
      while (cache.size > MAX_CACHE) cache.delete(cache.keys().next().value!)
    }
    return info
  })()
  pending.set(key, task)
  try { return await task } finally { if (pending.get(key) === task) pending.delete(key) }
}

/** Only mounted, visible steps request metadata. Selection changes invalidate late replies. */
export function useCoordinateInfo(source: MaybeRefOrGetter<CoordinateSource | undefined>, agentType: MaybeRefOrGetter<string | undefined> = undefined) {
  const resolved = ref<CoordinateInfo | null>(null)
  const loading = ref(false)
  const error = ref('')
  const conflict = ref(false)
  const retry = ref(0)
  watch(() => [toValue(source) ? coordinateSourceKey(toValue(source)!) : '', toValue(agentType), retry.value], async (_, __, cleanup) => {
    let current = true
    cleanup(() => { current = false })
    resolved.value = null
    error.value = ''
    conflict.value = false
    const requested = toValue(source)
    loading.value = Boolean(requested)
    if (!requested) return
    try {
      const info = await load({ ...requested })
      if (current) resolved.value = info
    } catch (cause) {
      if (current) {
        conflict.value = cause instanceof ApiError && cause.status === 409
        error.value = conflict.value ? '步骤版本已变化，请刷新当前页面后重试' : `坐标信息读取失败：${(cause as Error).message}`
      }
    } finally { if (current) loading.value = false }
  }, { immediate: true })
  const coordinateInfo = computed<CoordinateInfo>(() => {
    if (resolved.value) return resolved.value
    if (conflict.value) return { space: 'unknown', reason: error.value }
    const agent = (toValue(agentType) || '').toLowerCase()
    // Only explicit NewAgent evidence can use the old contract before lookup.
    // Missing source/agent fields may also describe external or edited Pipeline data.
    if (agent === 'newagent') return { space: 'normalized' }
    return { space: 'unknown', reason: error.value || (loading.value ? '正在读取坐标信息' : '坐标信息不足') }
  })
  return { coordinateInfo, loading, error, conflict, reload: () => { retry.value++ } }
}
