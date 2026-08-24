<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { TrajectoryRecord, TrajectoryStep } from '@/types'
import { api, imageUrl } from '@/api'
import ActionImage from './ActionImage.vue'

const props = defineProps<{ taskId: string; trajectory: TrajectoryRecord }>()
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
    step.actions_box = await api.updateBBox(
      props.taskId,
      props.trajectory.trajectory_id,
      step.step,
      step.excel_row,
      bbox,
    )
    ElMessage.success(`step ${step.step} 的 bbox 已写入标注文件`)
  } catch (error) {
    ElMessage.error((error as Error).message)
    throw error
  }
}
</script>

<template>
  <div v-if="activeStep" class="trajectory-explorer">
    <section class="trajectory-explorer__visual">
      <div class="step-heading">
        <span class="step-heading__number">STEP {{ String(activeStep.step).padStart(3, '0') }}</span>
        <el-button v-if="canEditBBox" size="small" type="primary" plain @click="actionImage?.beginEditing()">
          修改 bbox
        </el-button>
      </div>
      <ActionImage
        ref="actionImage"
        :image-url="imageUrl(activeStep.image)"
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
        @row-click="(_row: TrajectoryStep, _column: unknown, event: Event) => { activeIndex = trajectory.steps.indexOf(_row); event.preventDefault() }"
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
