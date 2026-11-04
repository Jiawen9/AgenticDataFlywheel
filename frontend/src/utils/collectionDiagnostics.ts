import type { PhoneDiagnosis } from '@/phoneFactoryApi'
import type { PhoneResumeState } from '@/composables/useCollectionDiagnostics'

export function diagnosisPhase(value?: string) {
  return ({ preparing: '准备中', starting: '启动中', collecting: '采集中', planning: '规划中', executing: '执行中',
    evaluating: '评测中', model_request: '等待模型响应', model_call: '等待模型响应', evidence: '保存证据',
    sealing: '封存证据', transferring: '回传中', completed: '已完成', idle: '空闲', stopped: '已停止',
    cleanup: '清理 App', resetting: '重置 App', resetting_app: '复位 App', waiting_model: '等待模型响应', model_error: '模型请求失败', waiting_storage: '等待存储恢复', paused: '已暂停', unknown: '状态待确认' } as Record<string, string>)[value || ''] || value || '等待阶段信息'
}
export function diagnosisStatus(value?: string) {
  return ({ healthy: '正常', warning: '需关注', error: '异常', running: '运行中', blocked: '受阻', failed: '失败',
    completed: '已完成', idle: '空闲', unknown: '待确认', stopped: '已停止', cancelled: '已取消',
    queued: '排队中', pending: '等待中', not_run: '未执行', timeout: '超时', interrupted: '已中断',
    received: '已接收', quarantined: '已隔离', retry_wait: '等待回传', downloading: '下载中',
    accepted: '已受理', uncertain: '等待受理确认', succeeded: '已完成', rejected: '未受理', stopping: '正在停止', preparing: '准备中', starting: '启动中', waiting: '等待中', needs_attention: '需要处理', conflict: '状态已变化' } as Record<string, string>)[value || ''] || value || '等待状态'
}
export function phoneDiagnosisText(row: PhoneDiagnosis) {
  if (row.reason?.startsWith('wait_expired:phone_busy:') || row.historical_blocker_execution_id) return `本次未执行：当时等待运行 ${row.historical_blocker_execution_id || row.reason?.slice('wait_expired:phone_busy:'.length)} 释放超时`
  if (row.process_exit_confirmed && ['failed', 'cancelled', 'not_run', 'error'].includes(row.status) && row.reason_code === 'storage_unavailable') return '此前文件或存储操作失败，执行进程已退出；修复后继续该手机'
  return row.message || row.reason || row.reason_code || diagnosisStatus(row.status)
}
export function diagnosisConfidence(value?: PhoneDiagnosis['confidence']) {
  return ({ confirmed: '已有明确依据', suspected: '疑似原因', needs_confirmation: '需要人工确认' } as Record<string, string>)[value || ''] || '依据待同步'
}
export function resumeStatus(item?: PhoneResumeState, phoneId?: string) {
  if (!item) return ''
  if (item.error && !item.result) return item.request ? '等待确认 · 可重试同一请求' : '未发起续跑'
  if (item.result) return diagnosisStatus(item.result.phones?.find(phone => phone.phone_id === phoneId)?.status || item.result.status)
  return ({ previewing: '准备续跑计划', confirming: '等待手机组确认', submitting: '正在提交', waiting: '等待受理确认', settled: '处理结束', cancelled: '已取消确认' })[item.phase]
}
