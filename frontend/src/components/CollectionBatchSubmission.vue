<script setup lang="ts">
import { onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { Download, Right } from '@element-plus/icons-vue'
import { collectionBatchesApi } from '@/collectionBatchesApi'
import type { TaskGenerationJob } from '@/types'
import { useCollectionBatchSubmission, type RunCollectionBatchAction } from '@/composables/useCollectionBatchSubmission'

const props = defineProps<{ job: TaskGenerationJob; resultCount: number; busy: boolean; protect: () => Promise<boolean>; runProtected: RunCollectionBatchAction }>()
const router = useRouter()
const state = useCollectionBatchSubmission(collectionBatchesApi, { job: () => props.job, resultCount: () => props.resultCount, busy: () => props.busy, runProtected: action => props.runProtected(action) })
const { batch, loading, submitting, downloading, error, canSubmit } = state
async function download() {
  const result = await state.download()
  if (!result) return
  const url = URL.createObjectURL(result.blob), link = document.createElement('a')
  link.href = url; link.download = result.filename; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function openCollection() {
  const id = batch.value?.batch_id, jobId = props.job.job_id
  if (!id || !(await props.protect()) || jobId !== props.job.job_id || id !== batch.value?.batch_id) return
  await router.push({ path: '/collection/phone-factory', query: { collection_batch_id: id } })
}
onBeforeUnmount(state.dispose)
</script>

<template>
  <section class="collection-submission" aria-label="提交轨迹采集" :aria-busy="loading || submitting">
    <div class="collection-copy"><h3>{{ batch ? '已提交轨迹采集' : '提交轨迹采集' }}</h3>
      <template v-if="batch"><p>{{ batch.task_count }} 条任务 · {{ batch.apps.join('、') }} · {{ batch.created_at.slice(0, 19).replace('T', ' ') }}</p><p>批次 {{ batch.batch_id }}。已冻结首次提交的数据，后续修改、删除或恢复不会改变此批次。</p></template>
      <template v-else><p>首次提交会保存全部未删除任务及前置任务的固定副本，不受页面筛选影响。每个作业只创建一个采集批次。</p><p>请先完成审核；提交后继续编辑源任务，不会更新这个批次。提交仅准备采集数据，手机与执行配置在采集页选择。</p></template>
      <p v-if="loading" role="status">正在读取提交状态…</p>
      <p v-if="error" class="collection-error" role="alert">{{ error }} <el-button link type="primary" :disabled="busy || loading || submitting" @click="state.refresh">重试读取</el-button></p>
    </div>
    <div class="collection-actions"><template v-if="batch"><el-button plain :icon="Download" :loading="downloading" :disabled="busy || submitting" @click="download">下载采集表</el-button><el-button type="primary" :icon="Right" :disabled="busy || submitting" @click="openCollection">前往手机采集</el-button></template><el-button v-else type="primary" :loading="submitting" :disabled="!canSubmit" @click="state.submit">提交轨迹采集</el-button></div>
  </section>
</template>

<style scoped>
.collection-submission{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:16px 18px;margin:18px 0;border:1px solid #cce2dc;border-radius:9px;background:#f6faf8}.collection-copy{min-width:0}.collection-copy h3{margin:0;color:var(--ink);font-size:14px}.collection-copy p{margin:6px 0 0;color:var(--muted);font-size:12px;line-height:1.65;overflow-wrap:anywhere}.collection-copy .collection-error{color:#b4533c}.collection-actions{display:flex;flex-shrink:0;gap:8px;flex-wrap:wrap}.collection-actions .el-button+.el-button{margin-left:0}@media(max-width:1000px){.collection-submission{align-items:flex-start;flex-direction:column;gap:12px}}@media(max-width:600px){.collection-actions{width:100%}.collection-actions .el-button{flex:1}}
</style>
