import type { CorrectionSession } from '@/types'

/** A requested identity must resolve exactly. Defaults apply only to unscoped pages. */
export async function resolveBatchSelection(
  batchIds: string[],
  options: { batchId?: string; legacyRunId?: string; defaultBatchId?: string },
  getRun: (runId: string) => Promise<{ batch_id?: string }>,
): Promise<string> {
  if (options.batchId === undefined && options.legacyRunId === undefined) {
    return options.defaultBatchId && batchIds.includes(options.defaultBatchId) ? options.defaultBatchId : batchIds[0] ?? ''
  }
  const requested = options.batchId !== undefined ? options.batchId : (await getRun(options.legacyRunId!)).batch_id
  if (!requested || !batchIds.includes(requested)) throw new Error('指定批次不存在或当前不可用，请重新选择批次')
  return requested
}

export function resolveSessionSelection(
  sessions: Pick<CorrectionSession, 'session_id' | 'batch_id'>[],
  options: { batchId?: string; sessionId?: string },
): string {
  if (options.batchId === undefined && options.sessionId === undefined) return sessions[0]?.session_id ?? ''
  const selected = options.batchId !== undefined
    ? sessions.find(session => session.batch_id === options.batchId)
    : sessions.find(session => session.session_id === options.sessionId)
  if (!selected) throw new Error('指定批次或纠偏记录不存在或已失效，请重新选择批次')
  return selected.session_id
}
