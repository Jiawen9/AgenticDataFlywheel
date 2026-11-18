<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { modelIterationApi, type FactoryRun } from '@/phoneFactoryApi'
import { pipelineLoopApi } from '@/pipelineLoopApi'
import type { Pipeline } from '@/types/pipeline'
import BadCasePanel from './BadCasePanel.vue'
const props = defineProps<{ pipeline: Pipeline }>(), emit = defineEmits<{ updated: [Pipeline] }>()
const router = useRouter(), runId = ref(props.pipeline.evaluation_run_id || ''), runs = ref<FactoryRun[]>([]), loading = ref(false), error = ref(''), expanded = ref(false)
const page = ref(1), total = ref(0), search = ref('')
let epoch = 0, disposed = false
async function load() { const ticket = ++epoch; loading.value = true; try { const value = await modelIterationApi.runPage({ offset: (page.value - 1) * 10, limit: 10, search: search.value }); if (ticket === epoch) { runs.value = value.items; total.value = value.total || 0; error.value = '' } } catch (cause) { if (ticket === epoch) error.value = (cause as Error).message } finally { if (ticket === epoch) loading.value = false } }
async function associate() { if (!runId.value || loading.value) return; const target = props.pipeline.pipeline_id, run = runId.value; loading.value = true; try { const updated = await pipelineLoopApi.associate(target, run, props.pipeline.storage_revision); if (disposed || props.pipeline.pipeline_id !== target) return; emit('updated', updated); error.value = ''; expanded.value = false } catch (cause) { if (!disposed && props.pipeline.pipeline_id === target) error.value = (cause as Error).message } finally { if (!disposed) loading.value = false } }
onBeforeUnmount(() => { disposed = true; epoch++ })
watch(page, () => { if (expanded.value) void load() })
watch(() => props.pipeline.evaluation_run_id, value => { runId.value = value || '' })
</script>
<template>
    <section class="evaluation-link">
    <div class="link-heading">
    <b>迭代评估关联</b>
    <span>{{ pipeline.evaluation_run_id || '尚未关联评测，不影响本轮数据流程结算' }}</span>
    <el-button size="small" @click="expanded = !expanded; expanded && load()">选择已有评测</el-button>
    <el-button size="small" @click="router.push({ path: '/model-iteration-evaluation', query: { evaluation_pipeline_id: pipeline.pipeline_id, ...(pipeline.evaluation_run_id ? { run_id: pipeline.evaluation_run_id } : {}) } })">前往迭代评估</el-button>
    <BadCasePanel v-if="pipeline.evaluation_run_id" :run-id="pipeline.evaluation_run_id" :parent-pipeline-id="pipeline.pipeline_id" />
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <div v-if="expanded" class="associate">
    <el-input v-model="search" placeholder="搜索评测运行" @change="page = 1; load()" />
    <el-select v-model="runId" :loading="loading" placeholder="选择要关联的评测">
    <el-option v-for="run in runs" :key="run.run_id" :value="run.run_id" :label="`${run.run_id} · ${run.filename || run.status}`" />
    </el-select>
    <el-button :disabled="!runId" :loading="loading" @click="associate">关联本轮</el-button>
    <el-pagination v-model:current-page="page" :page-size="10" :total="total" layout="prev, pager, next" />
    </div>
    <p v-if="pipeline.evaluation?.badcases">回流候选 {{ pipeline.evaluation.badcases.candidate || 0 }} · 待补信息 {{ pipeline.evaluation.badcases.needs_completion || 0 }} · 技术问题 {{ pipeline.evaluation.badcases.technical || 0 }}</p>
    <p v-if="pipeline.next_rounds?.length">BadCase 回流 → <span v-for="next in pipeline.next_rounds" :key="next.pipeline_id">扩增 {{ next.generated_count == null ? '待生成' : next.generated_count + ' 条' }} → <el-button link @click="router.push({ path: '/pipeline', query: { pipeline_id: next.pipeline_id } })">{{ next.pipeline_id }}</el-button>
    </span>
    </p>
    <p v-else-if="pipeline.next_pipeline_ids?.length">BadCase 回流 → <el-button v-for="id in pipeline.next_pipeline_ids" :key="id" link @click="router.push({ path: '/pipeline', query: { pipeline_id: id } })">{{ id }}</el-button>
    </p>
    <p v-if="pipeline.parent_pipeline_id">来源轮次：<el-button link @click="router.push({ path: '/pipeline', query: { pipeline_id: pipeline.parent_pipeline_id } })">{{ pipeline.parent_pipeline_id }}</el-button>
    </p>
    </section>
    </template>
<style scoped>.evaluation-link{padding:18px;background:#f1f7f6;color:#264e43}.link-heading,.associate{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.link-heading>span{font-size:12px;overflow-wrap:anywhere}.associate{margin-top:14px}.associate .el-input{width:180px}.associate .el-select{width:min(420px,100%)}p{font-size:12px;margin:12px 0 0}</style>
