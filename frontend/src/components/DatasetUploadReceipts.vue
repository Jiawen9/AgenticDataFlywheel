<script setup lang="ts">
import type { DatasetUploadFileResult } from '@/types'
import { datasetUploadMessage } from '@/utils/datasetUploadMessage'
defineProps<{ files?: DatasetUploadFileResult[] }>()
const labels = { pending: '等待上传', uploading: '上传中', succeeded: '已确认接收', failed: '失败', interrupted: '已中断' }
</script>

<template>
  <ul v-if="files?.length" class="upload-receipts" aria-label="逐文件上传结果">
    <li v-for="file in files" :key="file.idempotency_key">
      <div><b>{{ file.filename }}</b><span :class="file.status">{{ labels[file.status] }}</span></div>
      <span v-if="file.remote_id">远端记录编号：{{ file.remote_id }}</span>
      <a v-if="file.url" :href="file.url" target="_blank" rel="noopener noreferrer">查看云道S3记录</a>
      <span v-if="file.error" class="failed">{{ datasetUploadMessage(file.error) }}</span>
    </li>
  </ul>
</template>

<style scoped>
.upload-receipts { list-style: none; padding: 0; display: grid; gap: 8px; font-size: 13px; }
li { display: grid; gap: 5px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; overflow-wrap: anywhere; }
li > div { display: flex; justify-content: space-between; gap: 12px; }
span { color: var(--muted); }
a, .succeeded { color: var(--accent-deep); }
.failed, .interrupted { color: #b91c1c; }
</style>
