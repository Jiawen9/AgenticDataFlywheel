<script setup lang="ts">
import type { TaskGenerationResult } from '@/types'
import { dependencyLabel, resultPath } from '@/utils/taskGeneration'
defineProps<{ row: TaskGenerationResult; editingId: string | null; text: string; busy: boolean }>()
defineEmits<{ edit: [row: TaskGenerationResult]; delete: [row: TaskGenerationResult]; save: []; cancel: []; 'update:text': [value: string] }>()
</script>
<template>
  <div class="result-row" :class="{ deleted: row.deleted }" :data-result-id="row.result_id">
    <div class="row-context"><el-tag size="small" :type="row.dependency_error || row.pre_dependency === 'strong' ? 'danger' : 'info'">{{ dependencyLabel(row) }}</el-tag><strong>{{ row.app }}</strong><span>{{ resultPath(row) }}</span><el-tag v-if="row.deleted" size="small" type="info">已删除</el-tag></div>
    <div class="row-content"><div class="row-text"><el-input v-if="editingId === row.result_id" :model-value="text" type="textarea" :autosize="{ minRows: 3, maxRows: 12 }" :maxlength="4000" show-word-limit aria-label="编辑任务文本" :disabled="busy" @update:model-value="$emit('update:text', $event)" /><p v-else>{{ row.task }}</p><p v-if="row.dependency_error" class="row-error">依赖判定异常：{{ row.dependency_error }}</p></div><div class="row-actions"><template v-if="editingId === row.result_id"><el-button link type="primary" :loading="busy" @click="$emit('save')">保存</el-button><el-button link :disabled="busy" @click="$emit('cancel')">取消</el-button></template><template v-else><el-button link type="primary" :disabled="busy || row.deleted" @click="$emit('edit', row)">编辑</el-button><el-button link :type="row.deleted ? 'success' : 'danger'" :disabled="busy" @click="$emit('delete', row)">{{ row.deleted ? '恢复' : '删除' }}</el-button></template></div></div>
  </div>
</template>
<style scoped>
.result-row{padding:16px;min-width:0}.row-context{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;color:var(--muted)}.row-context strong{color:var(--ink)}.row-context>span{overflow-wrap:anywhere}.row-content{display:flex;align-items:flex-start;gap:20px;margin-top:11px}.row-text{flex:1;min-width:0}.row-text p{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.8;font-size:14px}.row-actions{display:flex;flex-shrink:0;gap:6px;padding-top:3px}.row-actions .el-button+.el-button{margin-left:0}.row-text .row-error{font-size:12px;margin-top:8px;color:#b4533c}.deleted .row-text>p:first-child{color:var(--muted);text-decoration:line-through}@media(max-width:700px){.row-content{flex-direction:column;gap:10px}.row-text{width:100%}.row-actions{align-self:flex-end}}
</style>
