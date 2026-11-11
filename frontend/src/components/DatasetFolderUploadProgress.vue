<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { DatasetFolderUpload } from '@/types'
import { byteProgress, directoryProgress } from '@/utils/datasetFolderUpload'
import { datasetUploadMessage } from '@/utils/datasetUploadMessage'

const props = defineProps<{ upload: DatasetFolderUpload; stage?: string }>()
const page = ref(1)
const detailsOpen = ref(false)
const items = computed(() => props.upload.item_results || [])
const pages = computed(() => Math.max(1, Math.ceil(items.value.length / 10)))
const visibleItems = computed(() => items.value.slice((page.value - 1) * 10, page.value * 10))
watch(pages, value => { page.value = Math.min(page.value, value) })
const percent = computed(() => directoryProgress(props.upload))
const bytePercent = computed(() => byteProgress(props.upload))
const labels = { pending: '等待上传', uploading: '上传中', succeeded: '已确认接收', failed: '失败', interrupted: '已中断' }
function size(value: number | undefined) {
  if (value === undefined) return '—'
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`
  return `${(value / 1024 ** 3).toFixed(2)} GB`
}
</script>

<template>
  <div class="folder-progress">
    <p v-if="upload.destination" class="destination">云道目标：<code>s3://{{ upload.destination.bucket }}/{{ upload.destination.prefix }}</code></p>
    <p v-if="stage === 'resolving'">正在定位发布表包含的轨迹目录…</p>
    <p v-else-if="stage === 'recovering'">正在核对上次上传进程，避免重复启动。</p>
    <p v-if="upload.source_summary?.trajectory_count !== undefined">实际上传范围：{{ upload.source_summary.case_count }} 个用例 · {{ upload.source_summary.trajectory_count }} 条完整轨迹 · {{ upload.source_summary.excel_count }} 份发布表格</p>
    <template v-if="upload.total_items && upload.total_items > 0">
      <div class="directory-count">已完成目录：{{ upload.completed_items || 0 }}/{{ upload.total_items }} <span>目录进度 {{ percent }}%</span></div>
      <progress :value="percent" max="100" aria-label="目录上传进度" />
    </template>
    <p v-else-if="stage !== 'resolving'">正在准备上传目录…</p>
    <p v-if="upload.current_item" class="current-item">当前目录：{{ upload.current_item }}</p>
    <p v-if="upload.progress?.speed_text" class="speed">当前上传速度：{{ upload.progress.speed_text }}</p>
    <template v-if="bytePercent !== null">
      <p>当前脚本字节进度：{{ size(upload.progress?.transferred_bytes) }} / {{ size(upload.progress?.total_bytes) }}（{{ bytePercent }}%）</p>
      <progress :value="bytePercent" max="100" aria-label="当前脚本字节上传进度" />
    </template>
    <p v-if="upload.progress?.message" class="output-message">{{ upload.progress.message }}</p>
    <button v-if="items.length" class="details-toggle" :aria-expanded="detailsOpen" @click="detailsOpen = !detailsOpen">{{ detailsOpen ? '收起' : '查看' }}目录上传结果（{{ items.length }}）</button>
    <section v-if="detailsOpen" aria-label="目录上传结果">
      <ul>
        <li v-for="item in visibleItems" :key="item.idempotency_key">
          <div><b>{{ item.label || item.filename }}</b><span :class="item.status">{{ labels[item.status] }}</span></div>
          <code>{{ item.bucket_path }}</code>
          <span v-if="item.error" class="failed">{{ datasetUploadMessage(item.error) }}</span>
        </li>
      </ul>
      <div class="pagination"><button :disabled="page === 1" @click="page--">上一页</button><span>{{ page }} / {{ pages }} · 每页 10 个目录</span><button :disabled="page === pages" @click="page++">下一页</button></div>
    </section>
    <details v-if="upload.recent_output?.length"><summary>最近传输输出</summary><pre>{{ upload.recent_output.slice(-20).join('\n') }}</pre></details>
  </div>
</template>

<style scoped>
.folder-progress { display: grid; gap: 10px; font-size: 13px; min-width: 0; }
p { margin: 0; overflow-wrap: anywhere; }
.destination, .current-item, .output-message { color: var(--muted); }
.directory-count, li > div, .pagination { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
progress { width: 100%; height: 12px; accent-color: var(--accent); }
button { cursor: pointer; color: var(--accent-deep); border: 1px solid var(--line); border-radius: 6px; background: var(--panel); padding: 6px 10px; }
button:disabled { cursor: default; opacity: .5; }
.details-toggle { justify-self: start; }
ul { list-style: none; padding: 0; display: grid; gap: 8px; }
li { display: grid; gap: 6px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; overflow-wrap: anywhere; }
li code { color: var(--muted); }
.succeeded { color: var(--accent-deep); }
.failed, .interrupted { color: #b91c1c; }
pre { max-height: 200px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
summary { cursor: pointer; }
</style>
