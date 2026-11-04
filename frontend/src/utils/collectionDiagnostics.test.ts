import { describe, expect, it } from 'vitest'
import { phoneDiagnosisText } from './collectionDiagnostics'

describe('historical failure and current execution', () => {
  it('does not translate an expired wait into a current ownership assertion', () => {
    const text = phoneDiagnosisText({ phone_id: 'p', status: 'not_run', reason: 'wait_expired:phone_busy:old', message: '手机被其他运行占用' })
    expect(text).toContain('当时等待运行 old 释放超时')
    expect(text).not.toBe('手机被其他运行占用')
  })
  it('does not describe an exited worker as waiting for automatic storage recovery', () => {
    expect(phoneDiagnosisText({ phone_id: 'p', status: 'failed', reason_code: 'storage_unavailable', process_exit_confirmed: true, message: '存储不可用，已暂停生产' })).toContain('执行进程已退出')
  })
})
