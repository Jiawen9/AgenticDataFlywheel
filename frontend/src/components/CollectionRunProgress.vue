<script setup lang="ts">
import type { FactoryRun } from '@/phoneFactoryApi'
import { collectionProgressText, runStatus, hasCollectionProgress } from '@/utils/phoneFactoryRuns'
defineProps<{ run: Partial<FactoryRun> }>()
</script>

<template>
  <div v-if="run.collection_summary && hasCollectionProgress(run)" class="collection-progress" data-testid="collection-transfer-progress">
    <p>采集：{{ runStatus(run.collection_status || 'pending') }} · 回传：{{ runStatus(run.transfer_status || 'waiting') }} · 每任务 {{ run.repeat_times ?? 1 }} 次</p>
    <p>原始任务 {{ run.collection_summary.original_task_count }} 条 · {{ collectionProgressText(run) }}</p>
    <p v-if="run.collection_summary.diagnostic_count">失败诊断已回传 {{ run.collection_summary.transferred_diagnostic_count ?? 0 }}/{{ run.collection_summary.diagnostic_count }} 份</p>
  </div>
</template>

<style scoped>
.collection-progress{font-size:12px;color:var(--muted);line-height:1.6;overflow-wrap:anywhere}.collection-progress p{margin:5px 0}
</style>
