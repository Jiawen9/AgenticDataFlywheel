<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { trainingDataOverviewApi } from '@/trainingDataOverviewApi'
import { useReleaseOverview } from '@/composables/useReleaseOverview'

const props = defineProps<{ releaseId: string }>()
const { conversion, workbookReady, loading, retrying, error, active, load, retry: retryConversion, dispose } = useReleaseOverview(props.releaseId)
const downloading = ref(false)
const downloadError = ref('')
const statusLabel = computed(() => conversion.value
  ? ({ queued: '等待转换', running: '转换中', succeeded: '已汇总', failed: '转换失败' }[conversion.value.status])
  : '尚未登记汇总')
const statusType = computed(() => conversion.value?.status === 'failed' ? 'danger' : conversion.value?.status === 'succeeded' ? 'success' : 'info')
let disposed = false
async function retry() { downloadError.value = ''; await retryConversion() }
async function download() {
  if (disposed || downloading.value || !workbookReady.value) return
  downloading.value = true
  downloadError.value = ''
  try {
    const blob = await trainingDataOverviewApi.workbook()
    if (disposed) return
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'all_data.xlsx'
    document.body.appendChild(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (cause) {
    if (!disposed) downloadError.value = (cause instanceof Error ? cause.message : '下载失败') + '；请重试转换或校验汇总后再下载。'
  } finally {
    if (!disposed) downloading.value = false
  }
}
onMounted(() => { void load() })
onBeforeUnmount(() => { disposed = true; dispose() })
</script>

<template>
  <section class="release-overview" data-testid="release-overview-status" :data-release-id="releaseId">
    <div class="release-overview-heading">
      <h4>训练数据汇总</h4>
      <el-tag :type="statusType" size="small" data-testid="release-overview-state">{{ loading && !conversion ? '正在读取…' : statusLabel }}</el-tag>
      <el-button text size="small" :loading="loading" :disabled="retrying" data-testid="release-overview-refresh" @click="load()">刷新状态</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-alert v-if="conversion?.error" :title="conversion.error" type="error" :closable="false" show-icon data-testid="release-overview-error" />
    <el-alert v-for="warning in conversion?.warnings ?? []" :key="warning" :title="warning" type="warning" :closable="false" show-icon />
    <el-alert v-if="downloadError" :title="downloadError" type="error" :closable="false" show-icon data-testid="release-overview-download-error" />
    <div class="release-overview-actions">
      <el-button :loading="retrying" :disabled="active || loading" data-testid="release-overview-retry" @click="retry()">{{ conversion?.status === 'succeeded' ? '校验汇总' : '重试转换' }}</el-button>
      <el-button :loading="downloading" :disabled="!workbookReady" data-testid="release-overview-download" @click="download()">下载完整汇总表（全部发布）</el-button>
    </div>
  </section>
</template>

<style scoped>
.release-overview { display: grid; gap: 10px; margin-top: 20px; padding: 14px; border: 1px solid var(--line); border-radius: 9px; background: var(--panel); }
.release-overview-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.release-overview-heading h4 { margin: 0; font-size: 14px; color: var(--ink); }
.release-overview-actions { display: flex; flex-wrap: wrap; gap: 10px; }
.release-overview-actions :deep(.el-button + .el-button) { margin-left: 0; }
</style>
