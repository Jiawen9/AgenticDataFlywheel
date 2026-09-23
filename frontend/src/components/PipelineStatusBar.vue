<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import type { PipelineContext } from '@/composables/usePipelineContext'
import { pipelineStatusLabels, pipelineStepLabels } from '@/types/pipeline'
const props = defineProps<{ context: PipelineContext }>()
const router = useRouter()
const pipeline = computed(() => props.context.pipeline.value)
const step = computed(() => props.context.step.value)
const error = computed(() => props.context.error.value)
function jobId(job: Record<string, unknown>): string { return String(job.job_id || job.collection_run_id || job.run_id || '') }
function progress(job: Record<string, unknown>): number | null {
  const nested = typeof job.progress === 'object' && job.progress ? job.progress as Record<string, unknown> : null
  const value = job.percent ?? nested?.percent ?? nested?.percentage
  return typeof value === 'number' ? Math.max(0, Math.min(100, value)) : null
}
</script>
<template>
  <aside v-if="pipeline || context.isPipelineRoute.value || error" class="pipeline-status-bar" data-testid="pipeline-status-bar">
    <div class="pipeline-status-heading">
      <div><strong>{{ pipeline?.name || 'Pipeline' }}</strong><span v-if="pipeline">{{ pipelineStatusLabels[pipeline.status] }} · {{ pipeline.batch_id }}</span></div>
      <el-button size="small" @click="router.push({ path: '/pipeline', query: { ...(pipeline ? { pipeline_id: pipeline.pipeline_id } : {}) } })">返回 Pipeline</el-button>
    </div>
    <el-alert v-if="context.historyOnly.value" title="流程已结束，仅展示本次运行记录；当前批次结果可能已更新，请从业务页面重新选择批次查看。" type="info" :closable="false" />
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <template v-else-if="step">
      <p>{{ step.label }} · {{ pipelineStepLabels[step.status] }}<span v-if="step.message"> · {{ step.message }}</span></p>
      <el-progress v-if="step.status === 'running' && typeof step.percent === 'number'" :percentage="Math.min(100, Math.max(0, step.percent))" />
      <el-alert v-if="step.error" :title="step.error" type="error" :closable="false" />
      <div v-for="job in step.jobs || []" :key="jobId(job)" class="pipeline-child-job">
        <span>{{ jobId(job) }} · {{ job.status }}</span>
        <el-progress v-if="progress(job) !== null" :percentage="progress(job)!" />
      </div>
      <small v-if="context.canCorrect.value">本步骤允许修正。保存完成后，点击“完成修正并继续”推进流程。</small>
      <small v-else-if="context.readOnly.value">此处展示 Pipeline 的作业与结果，执行由后台推进。</small>
    </template>
    <p v-else>正在核对 Pipeline 状态…</p>
  </aside>
</template>
<style scoped>
.pipeline-status-bar{margin:0 0 18px;padding:15px 18px;border:1px solid #99dace;border-radius:12px;background:#f0fdfa;color:var(--ink,#18332f)}.pipeline-status-heading{display:flex;justify-content:space-between;gap:12px;align-items:center}.pipeline-status-heading>div{display:flex;align-items:center;gap:14px;flex-wrap:wrap}.pipeline-status-heading span,.pipeline-status-bar p,.pipeline-status-bar small{font-size:12px;color:var(--muted,#526e68)}.pipeline-status-bar p{margin:10px 0}.pipeline-child-job{margin:8px 0;font-size:12px}.pipeline-child-job>span{display:block;overflow-wrap:anywhere;margin-bottom:4px}
</style>
