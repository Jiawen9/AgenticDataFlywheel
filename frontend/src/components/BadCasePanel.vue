<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api'
import { pipelineLoopApi, loopRequestId, type BadCase, type BadCasePage } from '@/pipelineLoopApi'
import { pipelineStepLocation } from '@/types/pipeline'
const props = defineProps<{ runId: string; parentPipelineId?: string }>()
const router = useRouter(), visible = ref(false), loading = ref(false), submitting = ref(false), error = ref('')
const page = ref(1), size = ref(10), status = ref('candidate'), data = ref<BadCasePage | null>(null)
const selected = ref(new Set<string>()), edit = ref<BadCase | null>(null), editTask = ref(''), editApp = ref('')
const nextName = ref(''), nextPipelineId = ref('')
const rows = computed(() => data.value?.items || [])
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false
let epoch = 0, controller: AbortController | undefined
async function load() {
  if (timer) clearTimeout(timer)
  const ticket = ++epoch, run = props.runId
  controller?.abort(); controller = new AbortController()
  if (!run || !visible.value) return
  loading.value = true; error.value = ''
  try { const value = await pipelineLoopApi.badcases(run, (page.value - 1) * size.value, size.value, status.value, controller.signal); if (ticket === epoch) { data.value = value; if (value.index_state === 'pending') timer = setTimeout(() => void load(), 2000); if (value.error) error.value = value.error } }
  catch (cause) { if (ticket === epoch && !controller.signal.aborted) error.value = (cause as Error).message }
  finally { if (ticket === epoch) loading.value = false }
}
async function extract() { const run = props.runId; try { await pipelineLoopApi.refresh(run); if (!disposed && props.runId === run && visible.value) await load() } catch (cause) { if (!disposed && props.runId === run) error.value = (cause as Error).message } }
function open() { visible.value = true; void extract() }
function toggle(row: BadCase, value: unknown) { const ids = new Set(selected.value); if (value) ids.add(row.candidate_id); else ids.delete(row.candidate_id); selected.value = ids }
function choosePage() { selected.value = new Set([...selected.value, ...rows.value.filter(row => !row.missing_fields?.length && status.value === 'candidate').map(row => row.candidate_id)]) }
function beginEdit(row: BadCase) { edit.value = row; editTask.value = row.task; editApp.value = row.app }
async function save() {
  if (!edit.value || !editTask.value.trim() || !editApp.value.trim()) return
  const run = props.runId, id = edit.value.candidate_id
  loading.value = true
  try { await pipelineLoopApi.patch(id, { task: editTask.value.trim(), app: editApp.value.trim() }); if (!disposed && props.runId === run) { if (edit.value?.candidate_id === id) edit.value = null; await load() } }
  catch (cause) { if (!disposed && props.runId === run) error.value = (cause as Error).message }
  finally { if (!disposed && props.runId === run) loading.value = false }
}
function evidence(row: BadCase) { return row.failures.map(item => JSON.stringify(item, null, 2)).join('\n\n') || '暂无详细证据；保留原评测结论。' }
async function feedback() {
  if (!selected.value.size || submitting.value) return
  const runId = props.runId, candidateIds = [...selected.value].sort(), parent = props.parentPipelineId, name = nextName.value.trim() || 'BadCase 回流 · ' + props.runId.slice(-10)
  submitting.value = true; error.value = ''
  try {
    const frozen = await pipelineLoopApi.feedback(runId, candidateIds, loopRequestId('feedback.' + runId, candidateIds))
    if (disposed || props.runId !== runId) return
    const body = { name, start_mode: 'generate' as const, generation_kind: 'augmentation' as const, mode: 'manual' as const, export_profile: 'corrected_selection_v1' as const, cot_policy: 'generate' as const, feedback_id: frozen.feedback_id, ...(parent ? { parent_pipeline_id: parent } : {}) }
    const created = await api.createPipeline({ ...body, request_id: loopRequestId('feedback-pipeline.' + frozen.feedback_id, body) })
    if (disposed || props.runId !== runId) return
    nextPipelineId.value = created.pipeline_id
    await router.push(pipelineStepLocation(created, 'generation'))
  } catch (cause) { if (!disposed && props.runId === runId) error.value = (cause as Error).message }
  finally { submitting.value = false }
}
watch(() => props.runId, () => { epoch++; controller?.abort(); selected.value = new Set(); data.value = null; page.value = 1; error.value = ''; edit.value = null; nextPipelineId.value = ''; if (visible.value) void load() })
watch([page, size, status, visible], () => { void load() })
onBeforeUnmount(() => { disposed = true; if (timer) clearTimeout(timer); epoch++; controller?.abort() })
</script>
<template>
  <div class="badcase-entry">
    <el-button :disabled="!runId" @click="open">提取 BadCase／回流</el-button>
    <span v-if="nextPipelineId">已创建下一轮 {{ nextPipelineId }}</span>
    </div>
  <el-drawer v-model="visible" title="BadCase 提取与回流" size="min(1100px, 100vw)" :close-on-click-modal="!submitting">
    <p>默认纳入有效评测的失败、部分成功；同一原任务的重复失败合并为一个扩增种子。</p>
    <p v-if="data" class="summary">候选 {{ data.summary.candidate || 0 }} · 技术问题 {{ data.summary.technical || 0 }} · 待补信息 {{ data.summary.needs_completion || 0 }} · 已选 {{ selected.size }}</p>
    <p v-if="data?.index_state === 'pending'" role="status">正在后台整理评测结果，已有候选可继续查看…</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <div class="tools">
    <el-select v-model="status" aria-label="BadCase 类型" @change="page = 1">
    <el-option value="candidate" label="回流候选" />
    <el-option value="technical" label="技术问题" />
    <el-option value="needs_completion" label="待补信息" />
    </el-select>
    <el-button :loading="loading" @click="extract">刷新／提取</el-button>
    <el-button :disabled="status !== 'candidate'" @click="choosePage">选择本页</el-button>
    <el-button @click="selected = new Set()">清除选择</el-button>
    </div>
    <el-table :data="rows" v-loading="loading" empty-text="当前没有匹配的结果；评测尚未同步完成时可稍后刷新。">
      <el-table-column width="48">
    <template #default="{ row }">
    <el-checkbox :model-value="selected.has(row.candidate_id)" :disabled="status !== 'candidate' || Boolean(row.missing_fields?.length) || submitting" :aria-label="'选择 ' + row.case_id" @update:model-value="toggle(row, $event)" />
    </template>
    </el-table-column>
      <el-table-column label="原任务" min-width="220">
    <template #default="{ row }">
    <b>{{ row.case_id }}</b>
    <p>{{ row.task || '缺少任务文本' }}</p>
    <small>{{ row.app || '缺少 App' }}</small>
    </template>
    </el-table-column>
      <el-table-column label="结论／重复证据" width="155">
    <template #default="{ row }">{{ row.verdicts?.map((v: string) => v === 'fail' ? '失败' : v === 'partial_success' ? '部分成功' : v).join('、') }}<p>{{ row.source_count }} 条来源</p>
    </template>
    </el-table-column>
      <el-table-column label="失败依据" min-width="220">
    <template #default="{ row }">
    <details>
    <summary>检查项、理由及来源</summary>
    <pre>{{ evidence(row) }}</pre>
    </details>
    </template>
    </el-table-column>
      <el-table-column label="信息" width="100">
    <template #default="{ row }">
    <el-button v-if="row.missing_fields?.length" link @click="beginEdit(row)">补充信息</el-button>
    <span v-else>已具备</span>
    </template>
    </el-table-column>
    </el-table>
    <el-pagination v-model:current-page="page" v-model:page-size="size" :page-sizes="[10,20,50]" :total="data?.total || 0" layout="total, sizes, prev, pager, next" />
    <div v-if="edit" class="edit">
    <el-input v-model="editApp" placeholder="涉及 App" />
    <el-input v-model="editTask" type="textarea" placeholder="原任务文本" />
    <el-button :loading="loading" @click="save">保存回流信息</el-button>
    <el-button @click="edit = null">取消</el-button>
    </div>
    <template #footer>
    <div class="next">
    <el-input v-model="nextName" placeholder="下一轮名称（可选）" />
    <el-button type="primary" :loading="submitting" :disabled="!selected.size" @click="feedback">回流并创建下一轮（{{ selected.size }}）</el-button>
    </div>
    </template>
  </el-drawer>
</template>
<style scoped>.badcase-entry,.tools,.next{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.badcase-entry span,.summary{font-size:12px;color:#526b63}.tools{margin:18px 0}.tools .el-select{width:170px}p{line-height:1.6}pre{max-height:240px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px;color:#3f5267}.el-pagination{margin-top:18px}.edit{display:grid;gap:10px;margin-top:18px;padding:16px;border:1px solid #b9d7cb}.next .el-input{flex:1;min-width:180px}</style>
