<script setup lang="ts">
import { computed } from 'vue'
import DisplayId from '@/components/DisplayId.vue'
import type { PreprocessingBatch, CollectionSourceRun } from '@/types'
import { isCollectionResult } from '@/utils/collectionResults'
import { agentLabel } from '@/utils/collectionAgents'

const props = defineProps<{ batch: PreprocessingBatch; runs: CollectionSourceRun[] }>()
const result = computed(() => isCollectionResult(props.batch))
const legacyCollection = computed(() => !result.value && props.runs.length > 0 && !['rollout_import', 'existing_trajectories'].includes(props.batch.kind))
const runIds = computed(() => [...new Set([props.batch.collection_run_id, ...props.runs.map(run => run.collection_run_id)].filter((id): id is string => Boolean(id)))])
</script>

<template>
  <div v-if="result || legacyCollection" class="collection-provenance" aria-label="采集结果来源">
    <p v-if="result">采集结果批次 <DisplayId kind="batch" :id="batch.batch_id" /> · 任务来源 <DisplayId kind="batch" :id="batch.task_source_batch_id" /><span v-if="batch.agent_type"> · {{ agentLabel(batch.agent_type) }}</span></p>
    <p v-if="runIds.length">采集运行：<span v-for="id in runIds" :key="id"><DisplayId kind="run" :id="id" /> </span></p>
    <p v-if="result">本批次只处理本次采集运行的轨迹；任务来源及同源的其他采集结果保持独立。</p>
    <p v-else>此历史批次沿用原有合并范围：预处理使用该批次已完成运行的轨迹，历史数据未按运行拆分。</p>
  </div>
</template>

<style scoped>
.collection-provenance{margin-bottom:12px;padding:10px 12px;border-radius:8px;background:var(--surface-muted,#f3f7f6);font-size:12px;color:var(--muted,#526e68)}.collection-provenance p{margin:4px 0;overflow-wrap:anywhere}
</style>
