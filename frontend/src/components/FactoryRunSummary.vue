<script setup lang="ts">
import { computed } from 'vue'
import type { FactoryRun } from '@/phoneFactoryApi'
import DisplayId from './DisplayId.vue'
import { agentLabel } from '@/utils/collectionAgents'
import { runDisplayStatus, runMessage, runStatus, collectionProgressText, hasCollectionProgress } from '@/utils/phoneFactoryRuns'
import { factoryRunAbnormal, factoryRunActive, factoryRunId, newestFactoryRuns } from '@/utils/factoryHistory'

const props = defineProps<{ runs: FactoryRun[]; evaluation: boolean; error?: string }>()
const emit = defineEmits<{ open: [filter: 'all' | 'active' | 'abnormal', runId?: string]; reports: [] }>()
const latest = computed(() => newestFactoryRuns(props.runs).slice(0, 3))
const active = computed(() => props.runs.filter(factoryRunActive).length)
const abnormal = computed(() => props.runs.filter(factoryRunAbnormal).length)
</script>

<template>
  <section class="run-overview" data-testid="run-overview">
    <div class="overview-heading">
      <h3>{{ evaluation ? '评估运行与报告' : '采集运行与回传' }}</h3>
      <div><el-button v-if="evaluation" link @click="emit('reports')">查看报告</el-button><el-button type="primary" plain @click="emit('open', 'all')">查看全部</el-button></div>
    </div>
    <div class="overview-counts" aria-label="当前范围运行统计">
      <button @click="emit('open', 'all')">共 <b>{{ runs.length }}</b> 次运行</button>
      <button @click="emit('open', 'active')">活动 <b>{{ active }}</b></button>
      <button :class="{ abnormal: abnormal > 0 }" @click="emit('open', 'abnormal')">异常 <b>{{ abnormal }}</b></button>
      <span>最近 3 次</span>
    </div>
    <el-alert v-if="error" :title="error" type="warning" :closable="false" />
    <p v-if="!latest.length" class="overview-empty">暂无运行记录</p>
    <div v-for="run in latest" :key="factoryRunId(run)" class="latest-run">
      <span class="run-ident"><DisplayId kind="run" :id="factoryRunId(run)" /><small>{{ run.created_at?.slice(0, 19).replace('T', ' ') || '时间未记录' }} · {{ agentLabel(run.agent_type) }}</small></span>
      <span :class="{ abnormal: factoryRunAbnormal(run) }">{{ runDisplayStatus(run) }}</span>
      <span class="run-message" :title="runMessage(run)">{{ runMessage(run) || '查看详情' }}</span>
      <el-button link type="primary" @click="emit('open', 'all', factoryRunId(run))">详情</el-button>
      <span v-if="hasCollectionProgress(run)" class="run-progress">采集 {{ runStatus(run.collection_status || 'pending') }} · 回传 {{ runStatus(run.transfer_status || 'waiting') }} · {{ collectionProgressText(run) }}</span>
    </div>
  </section>
</template>

<style scoped>
.run-progress{grid-column:1/-1;color:var(--muted);line-height:1.6;overflow-wrap:anywhere}
.run-overview{margin-top:24px;padding-top:18px;border-top:1px solid var(--line)}.overview-heading{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.overview-heading h3{margin:0;font-size:15px}.overview-counts{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:14px 0;font-size:12px}.overview-counts button{border:1px solid var(--line);background:#f8fafc;border-radius:7px;padding:7px 10px;color:var(--muted);cursor:pointer}.overview-counts>span{margin-left:auto;color:var(--muted)}.abnormal{color:#b45309!important}.latest-run{display:grid;grid-template-columns:minmax(120px,1.3fr) 84px minmax(90px,1fr) 42px;gap:10px;align-items:center;width:100%;border:0;border-top:1px solid var(--line);background:transparent;text-align:left;padding:12px 0;color:var(--ink);font:inherit;font-size:12px}.latest-run:hover{background:#f0fdfa}.run-ident{display:grid;gap:5px;min-width:0}.run-ident small,.run-message,.overview-empty{color:var(--muted);font-size:12px}.run-message{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}@media(max-width:760px){.latest-run{grid-template-columns:minmax(0,1fr) 80px 42px}.run-message{display:none}.run-ident small{font-size:10px}}
</style>
