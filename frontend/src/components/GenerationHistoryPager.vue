<script setup lang="ts">
defineProps<{ page: number; size: number; total: number; search: string; status: string; disabled?: boolean }>()
defineEmits<{ change: [patch: Record<string, string | number>] }>()
</script>
<template>
  <div class="history-controls">
    <el-input :model-value="search" clearable placeholder="搜索作业编号或文件名" :disabled="disabled" @change="$emit('change', { search: $event || '', offset: 0 })" />
    <el-select :model-value="status" clearable placeholder="全部状态" :disabled="disabled" @change="$emit('change', { status: $event || '', offset: 0 })">
      <el-option v-for="[value, label] in [['queued','排队中'],['running','执行中'],['awaiting_confirmation','待确认'],['succeeded','已完成'],['partial','部分完成'],['failed','失败'],['interrupted','已中断']]" :key="value" :value="value" :label="label" />
    </el-select>
    <el-pagination small :current-page="page" :page-size="size" :page-sizes="[10,20,50]" :total="total" :disabled="disabled" layout="total, sizes, prev, next" @update:current-page="$emit('change', { offset: ($event - 1) * size })" @update:page-size="$emit('change', { limit: $event, offset: 0 })" />
  </div>
</template>
<style scoped>.history-controls{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0;min-width:0}.history-controls .el-input{width:240px}.history-controls .el-select{width:130px}.history-controls .el-pagination{flex-wrap:wrap;max-width:100%}</style>
