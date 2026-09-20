<script setup lang="ts">
import type { PublishedBatchEvent } from '@/utils/batchLifecycle'
defineProps<{ notice: PublishedBatchEvent | null }>()
</script>
<template><el-alert v-if="notice" class="batch-published-notice" title="该批次已发布，处理已结束" type="success" :closable="false" show-icon><span>当前批次的选择和页面缓存已清空，过程件保留在后端。</span> <router-link :to="{ path: '/data-publishing/archive', query: notice.release_id ? { release_id: notice.release_id } : {} }">查看发布记录</router-link></el-alert></template>
