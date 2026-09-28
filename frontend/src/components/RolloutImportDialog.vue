<script setup lang="ts">
import { onBeforeUnmount, watch } from 'vue'
import { useRolloutImport } from '@/composables/useRolloutImport'
import type { RolloutImportResult } from '@/rolloutImportApi'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; imported: [result: RolloutImportResult] }>()
const flow = useRolloutImport(undefined, result => { emit('update:modelValue', false); emit('imported', result) })
const { visible, loading, previewing, committing, error, allowedRoots, preview, draft, taskEdits, stale, canPreview, canCommit } = flow
watch(() => props.modelValue, value => { if (value && !visible.value) void flow.open(); else if (!value && visible.value) flow.close() }, { immediate: true })
function close() { if (flow.close()) emit('update:modelValue', false) }
onBeforeUnmount(flow.dispose)
</script>

<template>
  <el-dialog :model-value="visible" title="导入已有 Rollout" width="min(1000px, 94vw)" append-to-body destroy-on-close :close-on-click-modal="false" :close-on-press-escape="!committing" :show-close="!committing" :before-close="close" data-testid="rollout-import-dialog">
    <el-alert title="登记已有原始轨迹，不会立即调用模型或启动 Pipeline。导入后可开始预处理，或在 Pipeline 中接续已有结果。" type="info" :closable="false" show-icon />
    <el-form label-position="top" :disabled="committing" class="rollout-import-form">
      <el-form-item label="后端本地 Rollout 目录" required>
        <el-input v-model="draft.source_path" :disabled="loading" data-testid="rollout-import-source-path" placeholder="填写运行后端的电脑上已有轨迹的目录" />
        <p class="field-hint">此处读取后端本地目录，不上传浏览器所在电脑的文件。{{ allowedRoots.length ? '允许的目录：' + allowedRoots.join('；') : '' }}</p>
      </el-form-item>
      <div class="rollout-import-grid">
        <el-form-item label="新业务批次编号" required><el-input v-model="draft.batch_id" data-testid="rollout-import-batch-id" /></el-form-item>
        <el-form-item label="批次名称（可选）"><el-input v-model="draft.name" maxlength="100" data-testid="rollout-import-name" placeholder="用于识别这次导入" /></el-form-item>
        <el-form-item label="App（补缺）"><el-input v-model="draft.app" data-testid="rollout-import-app" placeholder="源文件已有信息优先" /></el-form-item>
        <el-form-item label="一级场景（补缺）"><el-input v-model="draft.scene" data-testid="rollout-import-scene" placeholder="未填写保留为未分类" /></el-form-item>
        <el-form-item label="二级场景（补缺）"><el-input v-model="draft.capability" data-testid="rollout-import-capability" placeholder="未填写保留为未分类" /></el-form-item>
      </div>
    </el-form>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <section v-if="preview" class="rollout-import-preview" data-testid="rollout-import-summary">
      <div class="preview-heading"><strong>{{ preview.task_count }} 个任务 · {{ preview.trajectory_count }} 条轨迹 · {{ preview.step_count }} 步</strong><el-tag :type="stale ? 'warning' : preview.valid ? 'success' : 'danger'">{{ stale ? '信息已修改，请重新校验' : preview.valid ? '校验通过' : '校验未通过' }}</el-tag></div>
      <p class="field-hint">任务目录固定关联任务身份。缺少或不正确的任务目标可以在下表补充，修改后重新校验。</p>
      <el-table :data="preview.tasks" max-height="330" border size="small">
        <el-table-column prop="collection_case_id" label="任务目录 / 用例编号" min-width="145" />
        <el-table-column label="任务目标" min-width="250"><template #default="{ row }"><el-input :model-value="taskEdits[row.collection_case_id] ?? row.task" :disabled="committing" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" :data-testid="'rollout-import-task-' + row.collection_case_id" placeholder="请补充该任务需要完成的目标" @update:model-value="(value: string) => taskEdits[row.collection_case_id] = value" /></template></el-table-column>
        <el-table-column label="App / 一级 / 二级场景" min-width="170"><template #default="{ row }">{{ row.app || '未记录 App' }} / {{ row.scene || '未分类' }} / {{ row.capability || '未分类' }}</template></el-table-column>
        <el-table-column label="轨迹 / 步数" width="100"><template #default="{ row }">{{ row.trajectory_count }} / {{ row.step_count }}</template></el-table-column>
      </el-table>
      <ul v-if="preview.errors.length" class="preview-errors"><li v-for="(item, index) in preview.errors" :key="index">{{ item }}</li></ul>
      <ul v-if="preview.warnings.length" class="preview-warnings"><li v-for="(item, index) in preview.warnings" :key="index">{{ item }}</li></ul>
    </section>
    <template #footer>
      <el-button :disabled="committing" @click="close">取消</el-button>
      <el-button :disabled="!canPreview" :loading="previewing" data-testid="rollout-import-preview" @click="flow.validate">校验预览</el-button>
      <el-button type="primary" :disabled="!canCommit" :loading="committing" data-testid="rollout-import-commit" @click="flow.commit">确认导入</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.rollout-import-form { margin-top: 18px; }
.rollout-import-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 18px; }
.field-hint { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.7; margin: 6px 0 0; overflow-wrap: anywhere; }
.rollout-import-preview { margin-top: 20px; }
.preview-heading { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; }
.rollout-import-preview .el-table { margin-top: 10px; }
.preview-errors, .preview-warnings { padding-left: 20px; line-height: 1.7; overflow-wrap: anywhere; }
.preview-errors { color: var(--el-color-danger); }
.preview-warnings { color: var(--el-color-warning-dark-2); }
@media (max-width: 600px) { .rollout-import-grid { grid-template-columns: 1fr; } }
</style>
