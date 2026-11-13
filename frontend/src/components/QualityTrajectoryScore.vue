<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import type { DimensionEvaluation, TrajectoryQualityEvaluation } from '@/types'
import { checklistRate, detailText, fusionScore, scoreText } from '@/utils/qualityScores'

const props = defineProps<{ evaluation?: TrajectoryQualityEvaluation | null; trajectoryKey?: string }>()
const fused = computed(() => fusionScore(props.evaluation))
const checklist = computed(() => props.evaluation?.checklist)
const rate = computed(() => checklistRate(checklist.value))
const dimensions = computed<DimensionEvaluation[]>(() => {
  const scores = props.evaluation?.dimension_global_scores
  return Array.isArray(scores) ? scores : Object.entries(scores ?? {}).map(([dimension_name, score]) => ({ dimension_name, score }))
})
const detailId = useId()
const expandedItem = ref<string | null>(null)
const trajectoryIdentity = computed(() => props.trajectoryKey ?? props.evaluation?.trajectory_id ?? '')
watch(trajectoryIdentity, () => { expandedItem.value = null })

function hasDetail(value: unknown): boolean {
  if (value === undefined || value === null) return false
  if (typeof value === 'string') return value.trim().length > 0
  if (Array.isArray(value)) return value.length > 0
  if (typeof value === 'object') return Object.keys(value).length > 0
  return true
}
function itemStatus(item: Record<string, unknown>) {
  const value = item.checked === false ? 'unchecked' : (hasDetail(item.status) ? item.status : item.result)
  const labels: Record<string, { text: string; tone: string }> = {
    passed: { text: '通过', tone: 'passed' }, failed: { text: '未通过', tone: 'failed' },
    unchecked: { text: '未检查', tone: 'pending' }, not_checked: { text: '未检查', tone: 'pending' },
    pending: { text: '待检查', tone: 'pending' }, skipped: { text: '已跳过', tone: 'pending' },
    error: { text: '检查异常', tone: 'failed' },
  }
  return typeof value === 'string' && Object.hasOwn(labels, value)
    ? labels[value]!
    : { text: hasDetail(value) ? detailText(value) : '未知', tone: 'unknown' }
}
const checklistItems = computed(() => {
  const used = new Map<string, number>()
  return (checklist.value?.items ?? []).map((item, index) => {
    const identity = hasDetail(item.id) ? `id:${detailText(item.id)}` : `index:${index}`
    const duplicate = used.get(identity) ?? 0
    used.set(identity, duplicate + 1)
    return {
      key: `${identity}:${duplicate}`, index,
      description: detailText(item.description || item.text || item.id || `检查项 ${index + 1}`),
      status: itemStatus(item),
      reason: hasDetail(item.reason) ? detailText(item.reason) : '',
      evidence: hasDetail(item.evidence)
        ? (typeof item.evidence === 'object' ? JSON.stringify(item.evidence, null, 2) : detailText(item.evidence))
        : '',
    }
  })
})
watch(checklistItems, items => {
  if (expandedItem.value !== null && !items.some(item => item.key === expandedItem.value)) expandedItem.value = null
})
function statusText(value: string) {
  const labels: Record<string, string> = { complete: '已完成', succeeded: '已完成', running: '评测中', queued: '等待评测', preparing: '准备中', error: '评测失败', failed: '评测失败', cancelled: '已取消', unchecked: '未评测' }
  return Object.hasOwn(labels, value) ? labels[value] : value
}
function verdictText(value: unknown) {
  const labels: Record<string, string> = { success: '成功', partial_success: '部分成功', fail: '失败' }
  return typeof value === 'string' && Object.hasOwn(labels, value) ? labels[value] : detailText(value)
}
</script>

<template>
  <div class="trajectory-score" data-testid="trajectory-score">
    <template v-if="evaluation">
      <div class="fused"><span>本条轨迹融合分 F</span><strong>{{ fused === null ? (evaluation.fusion ? '暂无有效评分' : '未评测') : fused.toFixed(3) }}</strong></div>
      <p v-if="evaluation.fusion?.invalid_reasons?.length" class="score-note">{{ evaluation.fusion.invalid_reasons.join('；') }}</p>
      <p v-else-if="!evaluation.fusion" class="score-note">历史结果尚未进行 Checklist 融合评分。</p>
      <div class="score-section"><b>Rubric 总分 G</b><strong>{{ scoreText(evaluation.global_score, 2) }} / 5</strong><span>{{ evaluation.passed_threshold ? 'Rubric 达标' : 'Rubric 未达标' }}</span></div>
      <div v-for="item in dimensions" :key="item.dimension_id || item.dimension_name" class="dimension"><span>{{ item.dimension_name }}</span><b>{{ scoreText(item.score, 2) }}</b><p v-if="item.rationale">{{ item.rationale }}</p></div>
      <div class="score-section"><b>Checklist 通过数</b><strong>{{ rate === null ? '未评测' : `${checklist?.passed} / ${checklist?.total} · ${(rate * 100).toFixed(1)}%` }}</strong></div>
      <template v-if="checklist">
        <p class="score-note">状态：{{ statusText(checklist.status) }} · 未通过 {{ checklist.failed ?? '—' }} · 未检查 {{ checklist.unchecked ?? '—' }}</p>
        <p class="verdict"><b>评测结论</b><span>{{ verdictText(checklist.verdict) }}</span></p>
        <p v-if="checklist.error" class="score-error">{{ checklist.error }}</p>
        <div v-if="checklistItems.length" class="checklist-items" aria-label="Checklist 检查项">
          <article v-for="item in checklistItems" :key="item.key" class="checklist-item">
            <button type="button" class="checklist-toggle" :aria-expanded="expandedItem === item.key" :aria-controls="`${detailId}-${item.index}`" :aria-label="`检查项 ${item.index + 1}：${item.description}，${item.status.text}，${expandedItem === item.key ? '收起' : '展开'}依据`" @click="expandedItem = expandedItem === item.key ? null : item.key">
              <span class="checklist-index">{{ item.index + 1 }}</span>
              <span class="checklist-description" :title="item.description">{{ item.description }}</span>
              <span class="checklist-result" :class="`checklist-result--${item.status.tone}`">{{ item.status.text }}</span>
              <span class="checklist-chevron" aria-hidden="true">{{ expandedItem === item.key ? '−' : '+' }}</span>
            </button>
            <div v-if="expandedItem === item.key" :id="`${detailId}-${item.index}`" class="checklist-evidence">
              <p class="checklist-full-description">{{ item.description }}</p>
              <dl>
                <template v-if="item.reason"><dt>理由</dt><dd>{{ item.reason }}</dd></template>
                <dt>证据</dt><dd v-if="item.evidence"><pre>{{ item.evidence }}</pre></dd><dd v-else class="score-note">未提供证据</dd>
              </dl>
            </div>
          </article>
        </div>
        <p v-else class="score-note">暂无检查项明细。</p>
        <small v-if="checklist.definition_digest" class="score-note">规则摘要：{{ checklist.definition_digest }}</small>
      </template>
      <p v-else class="score-note">Checklist 未评测；Rubric 达标不代表任务成功。</p>
      <el-collapse><el-collapse-item title="逐步 Rubric 评价"><div v-for="step in evaluation.step_evaluations" :key="step.step_id" class="step"><b>Step {{ step.step_id }}</b><small>{{ step.step_quality_summary }}</small><p v-for="item in step.dimension_scores" :key="item.dimension_id || item.dimension_name"><span>{{ item.dimension_name }} · {{ scoreText(item.score, 2) }}</span>{{ item.rationale }}</p></div></el-collapse-item></el-collapse>
    </template>
    <p v-else class="score-note">本条轨迹待评分。</p>
  </div>
</template>

<style scoped>
.trajectory-score :deep(.el-collapse){--el-collapse-border-color:#334155;--el-collapse-header-bg-color:#111c30;--el-collapse-content-bg-color:#111c30;--el-collapse-header-text-color:#e2e8f0;--el-collapse-content-text-color:#e2e8f0;background:#111c30;border-color:#334155;border-radius:8px}
.trajectory-score :deep(.el-collapse-item__header){min-height:42px;padding:8px 12px;line-height:1.5;height:auto}
.trajectory-score :deep(.el-collapse-item__content){padding:8px 12px 12px}
.trajectory-score{display:grid;gap:8px;min-width:0;color:#e2e8f0;font-size:12px;line-height:1.6}
.fused,.score-section{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:5px}.fused{margin-top:10px}.fused strong{font-size:25px;color:#67e8f9}.score-section{padding-top:10px;border-top:1px solid #334155}.score-section>span{width:100%;color:#aebed2;font-size:11px}
.score-note{margin:0;color:#aebed2;font-size:11px;line-height:1.6;overflow-wrap:anywhere}.score-error{color:#fda4af;overflow-wrap:anywhere}
.dimension{display:grid;grid-template-columns:1fr auto;gap:4px}.dimension b{color:#67e8f9}.dimension p{grid-column:1/-1;margin:0;color:#aebed2;font-size:11px}
.verdict{display:grid;gap:4px;margin:0;padding:10px 12px;border:1px solid #334155;border-radius:8px;background:#17243a;color:#f1f5f9}.verdict b{color:#cbd5e1;font-size:11px}.verdict span{font-size:14px;font-weight:650;overflow-wrap:anywhere;white-space:pre-wrap}
.checklist-items{min-width:0;border:1px solid #334155;border-radius:8px;overflow:hidden;background:#111c30}
.checklist-item+.checklist-item{border-top:1px solid #334155}
.checklist-toggle{display:grid;grid-template-columns:18px minmax(0,1fr) auto 12px;align-items:start;gap:8px;width:100%;padding:10px 9px;border:0;border-radius:0;background:transparent;color:#e2e8f0;text-align:left;font:inherit;cursor:pointer}
.checklist-toggle:hover{background:#1b2a42}.checklist-toggle:focus-visible{outline:2px solid #67e8f9;outline-offset:-2px}.checklist-index,.checklist-chevron{color:#aebed2}.checklist-chevron{font-size:16px;line-height:1.2}
.checklist-description{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;overflow-wrap:anywhere;line-height:1.6}
.checklist-result{max-width:90px;padding:1px 6px;border:1px solid #475569;border-radius:5px;color:#e2e8f0;font-size:11px;overflow-wrap:anywhere}
.checklist-result--passed{background:#133d35;border-color:#23745e;color:#a7f3d0}.checklist-result--failed{background:#452133;border-color:#804157;color:#fecdd3}.checklist-result--pending{background:#3c3421;border-color:#76623a;color:#fde68a}
.checklist-evidence{padding:0 12px 12px 35px;overflow-wrap:anywhere}.checklist-full-description{margin:0 0 8px;color:#e2e8f0;white-space:pre-wrap}.checklist-evidence dl{display:grid;gap:3px;margin:0}.checklist-evidence dt{color:#aebed2}.checklist-evidence dd{min-width:0;margin:0 0 6px;white-space:pre-wrap}.checklist-evidence pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;color:#dce7f5}
.step{margin-bottom:9px}.step small{display:block}.step p{display:grid;color:#aebed2;font-size:11px}.step p span{color:#e2e8f0}
</style>
