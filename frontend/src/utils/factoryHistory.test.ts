import { describe, expect, it } from 'vitest'
import { caseErrors, factoryRunAbnormal, factoryRunActive, factoryRunErrors, flatReportFiles, newestFactoryRuns } from './factoryHistory'
import type { EvaluationCase, FactoryRun } from '@/phoneFactoryApi'

describe('factory history summaries', () => {
  it('orders the newest three without changing the API list and counts transfer activity separately from errors', () => {
    const rows: FactoryRun[] = Array.from({ length: 12 }, (_, i) => ({ run_id: `run-${i}`, created_at: `2026-10-${String(i + 1).padStart(2, '0')}`, status: 'completed', trajectory_count: 3 }))
    rows[0]!.transfer_status = 'syncing'
    rows[1]!.transfer_error = 'connection failed'
    rows[2]!.remote_status = 'partial'
    expect(newestFactoryRuns(rows).slice(0, 3).map(row => row.run_id)).toEqual(['run-11', 'run-10', 'run-9'])
    expect(rows[0]!.run_id).toBe('run-0')
    expect(rows.filter(factoryRunActive)).toHaveLength(1)
    expect(rows.filter(factoryRunAbnormal)).toHaveLength(2)
    expect(factoryRunAbnormal(rows[3]!)).toBe(false)
  })
  it('keeps every distinct complete error and includes failed evaluator counts', () => {
    const row = { status: 'completed', transfer_error: 'a\nvery long\nerror', error: 'second', errors: [{ error: 'second' }], remote_errors: [{ message: 'third' }], evaluation_summary: { evaluation_failed: 1 } } as FactoryRun
    expect(factoryRunAbnormal(row)).toBe(true)
    expect(factoryRunErrors(row)).toEqual(['a\nvery long\nerror', 'second', 'third'])
    expect(caseErrors({ failure_reason: 'x', errors: ['x', { message: 'y' }] } as EvaluationCase)).toEqual(['x', 'y'])
  })
  it('flattens files while retaining legacy directory identities and the exact download pair', () => {
    const old = { index: 1, dir_name: 'legacy-round', files: [{ name: 'old.xlsx', size: 1 }] }
    const latest = { index: 2, run_id: 'new-run', dir_name: 'new-dir', modified: 20, files: [{ name: 'new.xlsx', file_id: 'file-a', size: 1 }, { name: 'other.xlsx', file_id: 'file-b', size: 1 }] }
    const files = flatReportFiles([old, latest])
    expect(files).toHaveLength(3)
    expect(files[0]!.folder).toBe(latest)
    expect(files[0]!.file).toBe(latest.files[0])
    expect(files[2]!.folder).toBe(old)
    expect(new Set(files.map(item => item.key)).size).toBe(3)
  })
})
