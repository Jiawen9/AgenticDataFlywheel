<script setup lang="ts">
import type { CorrectionGroup, CorrectionRow } from '@/types'
import { correctionAssetUrl } from '@/api'
import CorrectionActionEditor from '@/components/CorrectionActionEditor.vue'

const props = defineProps<{ sessionId: string; group: CorrectionGroup; row: CorrectionRow | null; revision: number; saving: boolean }>()
const emit = defineEmits<{
  select: [row: CorrectionRow]
  delete: [row: CorrectionRow]
  action: [actions: string]
  draft: [actions: string | null]
}>()
function rowClassName({ row }: { row: CorrectionRow }) {
  return [row.deleted ? 'is-deleted' : '', row.excel_row === props.row?.excel_row ? 'is-active-step' : ''].join(' ')
}
</script>

<template>
  <section class="workbench" aria-label="轨迹修正台">
    <aside class="editor-pane">
      <template v-if="row">
        <CorrectionActionEditor :key="`${sessionId}:${group.group_id}:${row.excel_row}:${revision}`" :row="row" :image-url="correctionAssetUrl(sessionId, row.image)" :saving="saving" @save="emit('action', $event)" @draft="emit('draft', $event)" />
      </template>
      <el-empty v-else description="该轨迹没有可修正步骤" :image-size="80" />
    </aside>
    <div class="steps">
      <div class="steps-heading"><b>轨迹步骤</b><span>点击步骤查看截图并修正</span></div>
      <el-table :data="group.rows" height="clamp(420px, 70vh, 760px)" row-key="excel_row"
        :row-class-name="rowClassName" @row-click="(item: CorrectionRow) => emit('select', item)">
        <el-table-column prop="step" label="Step" width="65" />
        <el-table-column label="Action" min-width="150"><template #default="scope"><code class="action-text">{{ scope.row.actions }}</code></template></el-table-column>
        <el-table-column label="BBox" min-width="170" show-overflow-tooltip><template #default="scope"><code class="bbox-text">{{ scope.row.actions_box || '未标框' }}</code></template></el-table-column>
        <el-table-column prop="summary" label="Action Summary" min-width="140" show-overflow-tooltip />
        <el-table-column prop="thought" label="Thought" min-width="180" show-overflow-tooltip />
        <el-table-column label="状态" width="105"><template #default="scope"><el-tag v-if="scope.row.deleted" type="danger" size="small">已删除</el-tag><el-tag v-else-if="scope.row.edited" type="warning" size="small">{{ scope.row.edit_status }}</el-tag><span v-else>—</span></template></el-table-column>
        <el-table-column label="操作" width="65" fixed="right"><template #default="scope"><el-button link :type="scope.row.deleted ? 'primary' : 'danger'" :disabled="saving" @click.stop="emit('delete', scope.row)">{{ scope.row.deleted ? '恢复' : '删除' }}</el-button></template></el-table-column>
      </el-table>
    </div>
  </section>
</template>

<style scoped>
.steps :deep(.is-active-step td.el-table__cell){background:#ecf5ff}
.workbench{display:grid;grid-template-columns:minmax(400px,.95fr) minmax(520px,1.25fr);gap:28px;padding:6px 0 14px;align-items:start}
.editor-pane,.steps{min-width:0}.editor-pane{display:grid;gap:12px}.steps{border:1px solid var(--line);border-radius:14px;background:white;overflow:hidden}.steps-heading{display:flex;justify-content:space-between;gap:10px;padding:14px;font-size:13px}.steps-heading span{color:#64748b;font-size:12px}
.action-text,.bbox-text{display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#334155;font-size:11px}.bbox-text{color:#0f766e}.steps :deep(.is-deleted){opacity:.5;background:#fff1f2}.steps :deep(.el-table__row){cursor:pointer}
@media(max-width:1400px){.workbench{grid-template-columns:1fr;gap:16px}}
</style>
