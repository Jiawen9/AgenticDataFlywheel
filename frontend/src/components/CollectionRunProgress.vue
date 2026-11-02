<script setup lang="ts">
import type { FactoryRun } from '@/phoneFactoryApi'
import { collectionProgressText, runStatus, hasCollectionProgress } from '@/utils/phoneFactoryRuns'
defineProps<{ run: Partial<FactoryRun> }>()
</script>

<template>
  <div v-if="run.collection_summary && hasCollectionProgress(run)" class="collection-progress" data-testid="collection-transfer-progress">
    <p>采集：{{ runStatus(run.collection_status || 'pending') }} · 回传：{{ runStatus(run.transfer_status || 'waiting') }}<template v-if="run.archive_status"> · 归档：{{ runStatus(run.archive_status) }}</template> · 每任务 {{ run.repeat_times ?? 1 }} 次</p>
    <p>原始任务 {{ run.collection_summary.original_task_count }} 条 · {{ collectionProgressText(run) }}</p>
    <p v-if="run.receipt_policy_version === 2">原件已保存 {{ run.collection_summary.received_artifact_count ?? 0 }} · 已隔离 {{ run.collection_summary.quarantined_count ?? 0 }} · 可评测 {{ run.collection_summary.evaluation_eligible_count ?? 0 }} · 预处理合格 {{ run.collection_summary.preprocessing_eligible_count ?? 0 }} · 未执行 {{ run.collection_summary.collection_not_run_count ?? 0 }}</p>
    <p v-if="run.control_terminal && !run.receipt_closed">手机执行已结束，封存和回传继续。</p>
    <p v-if="run.receipt_storage?.paused">平台存储待恢复，采集端将在用例之间暂停。<template v-if="run.receipt_storage.pending">暂停通知待确认。</template>{{ run.receipt_storage.error }}</p>
    <p v-if="run.control_error">停止对账：{{ run.control_error }}</p>
    <p v-if="run.collection_summary.diagnostic_count">失败诊断已回传 {{ run.collection_summary.transferred_diagnostic_count ?? 0 }}/{{ run.collection_summary.diagnostic_count }} 份</p>
  </div>
</template>

<style scoped>
.collection-progress{font-size:12px;color:var(--muted);line-height:1.6;overflow-wrap:anywhere}.collection-progress p{margin:5px 0}
</style>
