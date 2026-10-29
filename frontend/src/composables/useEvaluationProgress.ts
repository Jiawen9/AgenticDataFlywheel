import { computed, onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { modelIterationApi, type EvaluationCase, type EvaluationResults, type FactoryRun } from '@/phoneFactoryApi'
import { useFactoryPolling } from './useFactoryPolling'
import { newestFactoryRuns } from '@/utils/factoryHistory'

/** A single page-owned progress session shared by the summary and details drawer. */
export function useEvaluationProgress(runs: Ref<FactoryRun[]>, onChanged: (runId: string) => void | Promise<void>) {
  const selectedRun = ref(''), results = ref<EvaluationResults | null>(null), resultError = ref('')
  const cancelling = ref(false), downloading = ref('')
  let disposed = false, mounted = false, selectionEpoch = 0
  const visible = () => typeof document === 'undefined' || !document.hidden
  const selectedRunRecord = computed(() => runs.value.find(run => run.run_id === selectedRun.value))
  const canCancel = computed(() => !!selectedRunRecord.value && !['completed', 'succeeded', 'partial', 'failed', 'interrupted', 'cancelled'].includes(selectedRunRecord.value.status))
  const resultMetadata = computed(() => ({ ...selectedRunRecord.value, ...results.value }))
  const busy = computed(() => cancelling.value || !!downloading.value)
  const poller = useFactoryPolling(async (signal, current) => {
    const run = selectedRun.value, epoch = selectionEpoch
    if (!run) return
    try {
      const value = await modelIterationApi.evaluations(run, signal)
      if (!disposed && current() && epoch === selectionEpoch && run === selectedRun.value) {
        results.value = value
        resultError.value = value.stale ? `显示上次结果：${value.sync_error || '同步暂不可用'}` : ''
      }
    } catch (cause) {
      if (!disposed && current() && epoch === selectionEpoch && run === selectedRun.value) resultError.value = (cause as Error).message
    }
  })
  watch(selectedRun, () => {
    selectionEpoch++; results.value = null; resultError.value = ''
    if (mounted && visible()) poller.start()
  }, { flush: 'sync' })
  watch(runs, values => {
    // A just-created run may not be present in the next list snapshot yet.
    if (!selectedRun.value) selectedRun.value = newestFactoryRuns(values)[0]?.run_id || ''
  }, { immediate: true })
  function selectRun(runId: string) {
    if (!busy.value && runId !== selectedRun.value) selectedRun.value = runId
  }
  async function cancelRun() {
    if (!canCancel.value || busy.value) return
    const run = selectedRun.value
    cancelling.value = true
    try {
      const message = selectedRunRecord.value?.execution_mode === 'evaluate_only' ? '将停止这个运行中尚未完成的评测，保留已有轨迹和报告。' : '将停止这个运行的采集及尚未完成的评测，保留已有结果。'
      try { await ElMessageBox.confirm(message, '停止本次运行', { type: 'warning', confirmButtonText: '停止运行', cancelButtonText: '继续运行' }) }
      catch { return }
      if (disposed) return
      await modelIterationApi.cancelEvaluation(run)
      if (disposed) return
      if (run === selectedRun.value) { results.value = null; resultError.value = '' }
      await onChanged(run)
      if (!disposed && visible()) poller.start()
    } catch (cause) { if (!disposed && run === selectedRun.value) resultError.value = (cause as Error).message }
    finally { if (!disposed) cancelling.value = false }
  }
  async function download(row: EvaluationCase) {
    const run = selectedRun.value
    if (!row.report_file_id || busy.value || !run) return
    downloading.value = row.case_key
    try {
      const name = row.report_name || 'evaluation-report.json'
      const blob = await modelIterationApi.reportDownload({ index: 0, dir_name: run, run_id: run, files: [] }, { file_id: row.report_file_id, name, size: 0 })
      if (disposed) return
      const url = URL.createObjectURL(blob), link = document.createElement('a')
      link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 0)
    } catch (cause) { if (!disposed && run === selectedRun.value) resultError.value = (cause as Error).message }
    finally { if (!disposed) downloading.value = '' }
  }
  onMounted(() => { mounted = true })
  onBeforeUnmount(() => { disposed = true; selectionEpoch++ })
  return { selectedRun, results, resultError, cancelling, downloading, canCancel, resultMetadata, busy, selectRun, cancelRun, download }
}

export type EvaluationProgress = ReturnType<typeof useEvaluationProgress>
