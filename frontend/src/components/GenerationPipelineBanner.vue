<script setup lang="ts">
import type { Pipeline } from '@/types/pipeline'
import { pipelineStatusLabels } from '@/types/pipeline'
defineProps<{ pipelineId: string; pipeline: Pipeline | null; error?: string }>()
defineEmits<{ back: [] }>()
</script>
<template>
    <section v-if="pipelineId" class="pipeline-banner">
    <div>
    <b>自动 Pipeline · {{ pipeline?.name || pipelineId }}</b>
    <p>{{ pipeline ? pipelineStatusLabels[pipeline.status] : '正在读取流程状态…' }}。审核全部未删除任务后，返回流程确认采集配置。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    </div>
    <el-button @click="$emit('back')">返回 Pipeline</el-button>
    </section>
    </template>
<style scoped>.pipeline-banner{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:14px 18px;background:#eef8f4;border:1px solid #a9d8ca;border-radius:10px;margin-bottom:18px}.pipeline-banner p{margin:6px 0;font-size:12px;color:#47645c}@media(max-width:650px){.pipeline-banner{align-items:flex-start;flex-direction:column}}</style>
