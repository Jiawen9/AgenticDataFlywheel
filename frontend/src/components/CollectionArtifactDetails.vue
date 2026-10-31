<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import type { FactoryRun } from '@/phoneFactoryApi'
import { runStatus } from '@/utils/phoneFactoryRuns'

const props = defineProps<{ run: FactoryRun }>()
const controls = reactive({ query: '', status: 'all', page: 1, size: 10 })
const rows = computed(() => (props.run.collection_artifacts || []).filter(row => {
  const abnormal = !!row.error || !!row.transfer_error || row.transfer_status === 'failed' || ['failed', 'timeout', 'interrupted'].includes(row.execution_status || '')
  return (controls.status === 'all' || (controls.status === 'abnormal' ? abnormal : row.transfer_status === 'completed'))
    && [row.collection_case_id, row.phone_id, row.expanded_case_id, row.source_trajectory_id, row.error, row.transfer_error]
      .join(' ').toLocaleLowerCase().includes(controls.query.trim().toLocaleLowerCase())
}))
const pageRows = computed(() => rows.value.slice((controls.page - 1) * controls.size, controls.page * controls.size))
watch([() => controls.query, () => controls.status, () => controls.size, () => props.run.collection_run_id || props.run.run_id], () => { controls.page = 1 }, { flush: 'sync' })
watch(() => rows.value.length, length => { controls.page = Math.max(1, Math.min(controls.page, Math.ceil(length / controls.size))) }, { flush: 'sync' })
</script>

<template>
  <section v-if="run.collection_artifacts && run.execution_mode !== 'evaluate_only'" class="artifact-details" data-testid="collection-artifact-details">
    <h4>已发现的轨迹与诊断</h4>
    <p class="hint">仅显示已封存并同步到平台清单的条目；尚未完成的采集不会提前列入。</p>
    <div class="artifact-toolbar"><el-input v-model="controls.query" clearable placeholder="搜索用例、手机、轨迹或错误" aria-label="搜索采集轨迹" /><el-select v-model="controls.status" aria-label="轨迹回传筛选"><el-option value="all" label="全部条目" /><el-option value="abnormal" label="仅异常" /><el-option value="completed" label="回传完成" /></el-select></div>
    <el-table :data="pageRows" row-key="artifact_id" border>
      <el-table-column type="expand"><template #default="{ row }"><div class="artifact-full"><p>扩展用例：{{ row.expanded_case_id || '等待清单' }}</p><p>轨迹编号：{{ row.source_trajectory_id || '等待清单' }}</p><p v-if="row.error">采集：{{ row.error }}</p><p v-if="row.transfer_error">回传：{{ row.transfer_error }}</p></div></template></el-table-column>
      <el-table-column prop="collection_case_id" label="原用例" min-width="110" show-overflow-tooltip />
      <el-table-column prop="phone_id" label="手机" min-width="150" show-overflow-tooltip />
      <el-table-column label="采集次数" width="90"><template #default="{ row }">{{ row.repeat_index ? `${row.repeat_index} / ${row.repeat_times}` : '等待清单' }}</template></el-table-column>
      <el-table-column label="内容" width="90"><template #default="{ row }">{{ row.kind === 'diagnostic' ? '失败诊断' : '采集轨迹' }}</template></el-table-column>
      <el-table-column label="采集 / 回传" min-width="150"><template #default="{ row }">{{ runStatus(row.execution_status || 'pending') }} / {{ runStatus(row.transfer_error ? 'failed' : row.transfer_status || 'pending') }}</template></el-table-column>
      <el-table-column label="错误" min-width="160" show-overflow-tooltip><template #default="{ row }">{{ row.transfer_error || row.error || '—' }}</template></el-table-column>
    </el-table>
    <el-pagination v-model:current-page="controls.page" v-model:page-size="controls.size" :page-sizes="[10, 20, 50]" :total="rows.length" layout="total, sizes, prev, pager, next" />
  </section>
</template>

<style scoped>
.artifact-details{margin-top:12px}.artifact-details h4{margin:6px 0}.hint{font-size:12px;color:var(--muted)}.artifact-toolbar{display:flex;gap:8px;margin:10px 0}.artifact-toolbar>.el-input{flex:1}.artifact-toolbar>.el-select{width:150px}.artifact-full{padding:10px 18px;white-space:pre-wrap;overflow-wrap:anywhere}.artifact-details .el-pagination{margin-top:10px;max-width:100%;overflow-x:auto}@media(max-width:760px){.artifact-toolbar{flex-wrap:wrap}.artifact-toolbar>.el-select{width:100%}}
</style>
