<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { TrajectoryRecord, TrajectoryStep, TrajectoryScope } from '@/types'
import { imageUrl } from '@/api'
import ActionImage from './ActionImage.vue'

const props = defineProps<{ taskId: string; trajectory: TrajectoryRecord; scope: TrajectoryScope; disabled?: boolean;
  saveBbox: (step: TrajectoryStep, bbox: [number, number, number, number]) => Promise<void> }>()
const activeIndex = ref(0)
const actionImage = ref<InstanceType<typeof ActionImage> | null>(null)
watch(() => props.trajectory.trajectory_id, () => { activeIndex.value = 0 })
const activeStep = computed<TrajectoryStep | undefined>(() => props.trajectory.steps[activeIndex.value])
const canEditBBox = computed(() =>
  ['click', 'swipe', 'long_press'].includes(String(activeStep.value?.action.action || '').toLowerCase()),
)

async function saveBBox(bbox: [number, number, number, number]) {
  const step = activeStep.value
  if (!step) return
  try {
    await props.saveBbox(step, bbox)
    ElMessage.success(`step ${step.step} 的 bbox 已保存为新标框版本`)
  } catch (error) {
    ElMessage.error((error as Error).message)
    throw error
  }
}

async function protect(): Promise<boolean> {
  const editor = actionImage.value
  if (editor?.saving) return false
  if (!editor?.editing) return true
  try {
    await ElMessageBox.confirm('当前步骤有未保存的 bbox 修改，保存后继续？', '未保存的 bbox', {
      confirmButtonText: '保存并继续', cancelButtonText: '放弃修改', distinguishCancelAndClose: true,
      closeOnClickModal: false, type: 'warning',
    })
    const saved = await editor.saveDrawing()
    if (!saved) ElMessage.warning('请完成有效框选并保存后继续')
    return saved
  } catch (action) {
    if (action === 'cancel') { editor.cancelEditing(); return true }
    return false
  }
}
async function selectStep(row: TrajectoryStep) {
  if (row === activeStep.value || !(await protect())) return
  activeIndex.value = props.trajectory.steps.indexOf(row)
}
const isEditing = computed(() => Boolean(actionImage.value?.editing || actionImage.value?.saving))
defineExpose({ protect, isEditing })
</script>

<template>
  <div v-if="activeStep" class="trajectory-explorer">
    <section class="trajectory-explorer__visual">
      <div class="step-heading">
        <span class="step-heading__number">STEP {{ String(activeStep.step).padStart(3, '0') }}</span>
        <el-button v-if="canEditBBox" size="small" type="primary" plain :disabled="disabled" @click="actionImage?.beginEditing()">
          修改 bbox
        </el-button>
      </div>
      <ActionImage
        ref="actionImage"
        :image-url="imageUrl(activeStep.image, scope)"
        :action="activeStep.action"
        :actions-box="activeStep.actions_box"
        :alt="`${trajectory.trajectory_id} step ${activeStep.step}`"
        :editable="canEditBBox"
        :show-edit-trigger="false"
        :on-save-bbox="saveBBox"
      />
    </section>
    <section class="trajectory-explorer__table">
      <el-table
        :data="trajectory.steps"
        height="660"
        highlight-current-row
        :current-row-key="activeStep.excel_row"
        row-key="excel_row"
        @row-click="selectStep"
      >
        <el-table-column prop="step" label="Step" width="72" />
        <el-table-column label="Action" min-width="180">
          <template #default="scope">
            <div class="action-cell">
              <code>{{ scope.row.action_text }}</code>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="action_summary" label="Action Summary" min-width="240" show-overflow-tooltip />
        <el-table-column prop="actions_box" label="BBox" min-width="230" show-overflow-tooltip />
      </el-table>
    </section>
  </div>
</template>

<style scoped>
.trajectory-explorer { display: grid; grid-template-columns: minmax(300px, 390px) minmax(600px, 1fr); gap: 28px; padding: 6px 0 14px; }
.trajectory-explorer__visual { min-width: 0; }
.trajectory-explorer__visual :deep(.action-image img) { max-height: 580px; image-rendering: auto; }
.trajectory-explorer__table { min-width: 0; border: 1px solid var(--line); border-radius: 14px; overflow: hidden; }
.step-heading { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.step-heading__number { color: var(--muted); font-weight: 800; letter-spacing: .12em; font-size: 12px; }
.action-cell { min-width: 0; line-height: 1.25; }
.action-cell code { display: block; overflow: hidden; color: #334155; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 1050px) { .trajectory-explorer { grid-template-columns: 1fr; } }
</style>
