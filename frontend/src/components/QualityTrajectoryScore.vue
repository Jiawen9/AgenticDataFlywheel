<script setup lang="ts">
import { computed } from 'vue'
import type { DimensionEvaluation, TrajectoryQualityEvaluation } from '@/types'
import { checklistRate, detailText, fusionScore, scoreText } from '@/utils/qualityScores'

const props = defineProps<{ evaluation?: TrajectoryQualityEvaluation | null }>()
const fused = computed(() => fusionScore(props.evaluation))
const checklist = computed(() => props.evaluation?.checklist)
const rate = computed(() => checklistRate(checklist.value))
const dimensions = computed<DimensionEvaluation[]>(() => {
  const scores = props.evaluation?.dimension_global_scores
  return Array.isArray(scores) ? scores : Object.entries(scores ?? {}).map(([dimension_name, score]) => ({ dimension_name, score }))
})
</script>

<template>
  <div class="trajectory-score" data-testid="trajectory-score">
    <template v-if="evaluation">
      <div class="fused"><span>本条轨迹融合分 F</span><strong>{{ fused === null ? (evaluation.fusion ? '暂无有效评分' : '未评测') : fused.toFixed(3) }}</strong></div>
      <p v-if="evaluation.fusion?.invalid_reasons?.length" class="score-note">{{ evaluation.fusion.invalid_reasons.join('；') }}</p>
      <p v-else-if="!evaluation.fusion" class="score-note">历史结果尚未进行 Checklist 融合评分。</p>
      <div class="score-section"><b>Rubric 总分 G</b><strong>{{ scoreText(evaluation.global_score, 2) }} / 5</strong><span>{{ evaluation.passed_threshold ? 'Rubric 达标' : 'Rubric 未达标' }}</span></div>
      <div v-for="item in dimensions" :key="item.dimension_id || item.dimension_name" class="dimension"><span>{{ item.dimension_name }}</span><b>{{ scoreText(item.score, 2) }}</b><p v-if="item.rationale">{{ item.rationale }}</p></div>
      <div class="score-section"><b>Checklist 完成率</b><strong>{{ rate === null ? '未评测' : `${checklist?.passed} / ${checklist?.total} · ${(rate * 100).toFixed(1)}%` }}</strong></div>
      <template v-if="checklist">
        <p class="score-note">状态：{{ checklist.status }} · 未通过 {{ checklist.failed ?? '—' }} · 未检查 {{ checklist.unchecked ?? '—' }}</p>
        <p class="verdict"><b>评测结论</b><span>{{ detailText(checklist.verdict) }}</span></p>
        <p v-if="checklist.error" class="score-error">{{ checklist.error }}</p>
        <el-collapse v-if="checklist.items?.length"><el-collapse-item title="Checklist 明细"><article v-for="(item, index) in checklist.items" :key="index" class="checklist-item"><b>{{ detailText(item.description || item.text || item.id || `检查项 ${index + 1}`) }}</b><dl><dt>检查结果</dt><dd>{{ detailText(item.status ?? item.result) }}</dd><template v-if="item.reason"><dt>理由</dt><dd>{{ detailText(item.reason) }}</dd></template><template v-if="item.evidence"><dt>证据</dt><dd>{{ detailText(item.evidence) }}</dd></template></dl></article></el-collapse-item></el-collapse>
        <small v-if="checklist.definition_digest" class="score-note">规则摘要：{{ checklist.definition_digest }}</small>
      </template>
      <p v-else class="score-note">Checklist 未评测；Rubric 达标不代表任务成功。</p>
      <el-collapse><el-collapse-item title="逐步 Rubric 评价"><div v-for="step in evaluation.step_evaluations" :key="step.step_id" class="step"><b>Step {{ step.step_id }}</b><small>{{ step.step_quality_summary }}</small><p v-for="item in step.dimension_scores" :key="item.dimension_id || item.dimension_name"><span>{{ item.dimension_name }} · {{ scoreText(item.score, 2) }}</span>{{ item.rationale }}</p></div></el-collapse-item></el-collapse>
    </template>
    <p v-else class="score-note">本条轨迹待评分。</p>
  </div>
</template>

<style scoped>
.trajectory-score :deep(.el-collapse){--el-collapse-border-color:#334155;--el-collapse-header-bg-color:transparent;--el-collapse-content-bg-color:transparent;--el-collapse-header-text-color:#dce7f5;--el-collapse-content-text-color:#dce7f5}
.trajectory-score{display:grid;gap:8px;font-size:12px}.fused,.score-section{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:5px}.fused{margin-top:10px}.fused strong{font-size:25px;color:#67e8f9}.score-section{padding-top:10px;border-top:1px solid #334155}.score-section>span{width:100%;color:#94a3b8;font-size:11px}.score-note{margin:0;color:#94a3b8;font-size:11px;line-height:1.6;overflow-wrap:anywhere}.score-error{color:#fda4af;overflow-wrap:anywhere}.dimension{display:grid;grid-template-columns:1fr auto;gap:4px}.dimension b{color:#67e8f9}.dimension p{grid-column:1/-1;margin:0;color:#94a3b8;font-size:11px}.verdict{display:grid;gap:4px;margin:0}.verdict span{overflow-wrap:anywhere;white-space:pre-wrap}.checklist-item{border-bottom:1px solid #334155;padding:6px 0}.checklist-item dl{display:grid;grid-template-columns:minmax(60px,.35fr) 1fr;gap:4px;margin:7px 0}.checklist-item dt{color:#94a3b8;overflow-wrap:anywhere}.checklist-item dd{margin:0;overflow-wrap:anywhere;white-space:pre-wrap}.step{margin-bottom:9px}.step small{display:block}.step p{display:grid;color:#94a3b8;font-size:11px}.step p span{color:#dce7f5}
</style>
