<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api'
import { pipelineLoopApi } from '@/pipelineLoopApi'
const props = defineProps<{ pipelineId: string; runId: string }>(), router = useRouter(), busy = ref(false), error = ref(''), success = ref('')
let version = 0
let disposed = false
watch(() => [props.pipelineId, props.runId], () => {
  version += 1
  error.value = ''
  success.value = ''
})
onBeforeUnmount(() => { disposed = true; version += 1 })
async function associate() {
  if (!props.runId || busy.value) return
  const run = props.runId, pipelineId = props.pipelineId, current = version
  busy.value = true
  error.value = ''
  try {
    const pipeline = await api.pipeline(pipelineId)
    if (disposed || current !== version) return
    await pipelineLoopApi.associate(pipeline.pipeline_id, run, pipeline.storage_revision)
    if (!disposed && current === version) success.value = '已关联评测 ' + run
  } catch (cause) {
    if (!disposed && current === version) error.value = (cause as Error).message
  } finally { busy.value = false }
}
</script>
<template>
    <section class="evaluation-pipeline">
    <span>来自 Pipeline {{ pipelineId }}，可选择已有评测或新建评测。</span>
    <el-button :disabled="!runId" :loading="busy" @click="associate">关联当前评测</el-button>
    <el-button @click="router.push({ path: '/pipeline', query: { pipeline_id: pipelineId } })">返回 Pipeline</el-button>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="success" role="status">{{ success }}</p>
    </section>
    </template>
<style scoped>.evaluation-pipeline{padding:16px;background:#eff8f5;border:1px solid #acd3c6;border-radius:9px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:14px 0}.evaluation-pipeline span,.evaluation-pipeline p{font-size:12px}</style>
