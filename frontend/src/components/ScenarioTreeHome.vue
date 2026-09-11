<script setup lang="ts">
import { computed } from 'vue'
import { Connection, EditPen, Share } from '@element-plus/icons-vue'
import type { TaskGenerationTreeNode } from '@/types'
import CapabilityTree from '@/components/CapabilityTree.vue'
import { scenarioMetrics } from '@/utils/scenarioOverview'

const props = defineProps<{
  tree: TaskGenerationTreeNode[]
  loading: boolean
  error: string
}>()
const emit = defineEmits<{
  (event: 'open-editor', nodeId?: string): void
  (event: 'retry'): void
}>()
const metrics = computed(() => scenarioMetrics(props.tree))
const sceneNodes = computed(() => props.tree.filter(node => node.kind === 'scene'))
const statItems = computed(() => [
  { label: '一级场景', value: metrics.value.sceneCount },
  { label: '能力', value: metrics.value.capabilityCount },
  { label: '子能力', value: metrics.value.subCapabilityCount },
  { label: 'App', value: metrics.value.appCount },
])
const values = [
  { icon: Share, title: '统一能力模型', description: '让场景、能力与 App 使用同一套定义。' },
  { icon: EditPen, title: '可视化维护', description: '在编辑工作台中持续完善能力结构。' },
  { icon: Connection, title: '能力底座复用', description: '连接任务生成、评测构建与轨迹质检。' },
]
</script>

<template>
  <div class="tree-home" v-loading="props.loading" :aria-busy="props.loading">
    <section class="home-intro" aria-labelledby="home-intro-title">
      <div class="intro-copy">
        <h2 id="home-intro-title">场景能力全貌</h2>
        <p>将场景、能力与应用，整理成一张清晰的知识地图。</p>
      </div>
      <button class="home-primary" type="button" :disabled="props.loading || Boolean(props.error)" @click="emit('open-editor', sceneNodes[0]?.id)">
        进入编辑工作台 <span aria-hidden="true">↗</span>
      </button>
      <dl class="home-metrics" aria-label="体系规模">
        <div v-for="item in statItems" :key="item.label" class="metric-item">
          <dd>{{ props.loading || props.error ? '—' : item.value }}</dd><dt>{{ item.label }}</dt>
        </div>
      </dl>
    </section>

    <section class="tree-frame" aria-label="场景能力树">
      <header class="tree-frame__heading">
        <div class="tree-frame__title"><span class="atlas-eyebrow">SCENARIO ATLAS</span><h3>能力索引</h3></div>
        <span class="tree-frame__legend">从场景进入工作台 <span aria-hidden="true">↗</span></span>
      </header>
      <div v-if="props.error" class="tree-state tree-state--error" role="alert">
        <strong>场景树暂时无法加载</strong><p>{{ props.error }}</p>
        <button type="button" @click="emit('retry')">重新加载 <span aria-hidden="true">↻</span></button>
      </div>
      <CapabilityTree v-else-if="sceneNodes.length" :tree="props.tree" @select-l1="emit('open-editor', $event)" />
      <div v-else-if="!props.loading" class="tree-state">
        <el-icon aria-hidden="true"><Share /></el-icon>
        <strong>能力树尚未建立</strong><p>创建第一个一级场景，开始整理你的能力体系。</p>
        <button type="button" @click="emit('open-editor')">进入编辑工作台 →</button>
      </div>
      <div v-else class="tree-state" role="status"><span>正在读取场景能力…</span></div>
      <p v-if="sceneNodes.length && !props.error" class="tree-frame__hint"><span class="hover-hint">悬停查看代表性能力<span aria-hidden="true"> · </span></span>点击场景，进入对应编辑工作台</p>
    </section>

    <section class="home-values" aria-label="能力体系价值">
      <article v-for="item in values" :key="item.title">
        <el-icon class="value-icon" aria-hidden="true"><component :is="item.icon" /></el-icon>
        <div><h3>{{ item.title }}</h3><p>{{ item.description }}</p></div>
      </article>
    </section>
  </div>
</template>

<style scoped>
.tree-home {
  --atlas-serif: "Noto Serif SC", "Source Han Serif SC", "Songti SC", "STSong", "SimSun", Georgia, serif;
  padding: 0 26px 22px;
  color: var(--studio-ink, var(--ink));
  background: transparent;
}
.home-intro { display: flex; align-items: center; flex-wrap: wrap; gap: 20px 24px; padding: 25px 0 22px; }
.intro-copy { flex: 1; min-width: 0; }
.intro-copy h2 { margin: 0 0 9px; font: 500 clamp(25px, 2.4vw, 32px)/1.3 var(--atlas-serif); letter-spacing: .05em; }
.intro-copy p { margin: 0; color: var(--studio-muted, var(--muted)); font-size: 13px; line-height: 1.7; }
.home-primary { display: inline-flex; align-items: center; justify-content: center; gap: 17px; min-height: 40px; padding: 9px 15px; border: 1px solid var(--studio-accent-deep, var(--accent-deep)); border-radius: 5px; background: transparent; color: var(--studio-accent-deep, var(--accent-deep)); font: inherit; font-size: 12px; font-weight: 600; cursor: pointer; transition: background .2s ease, color .2s ease; }
.home-primary:hover:not(:disabled) { background: var(--studio-accent-deep, var(--accent-deep)); color: #fff; }
.home-primary:disabled { opacity: .5; cursor: not-allowed; }
.home-primary span { font-size: 18px; font-weight: 400; }
.home-primary:focus-visible, .tree-state button:focus-visible { outline: 2px solid var(--studio-accent, var(--accent)); outline-offset: 4px; }
.home-metrics { display: flex; flex: 0 0 100%; gap: 0; margin: 0; }
.metric-item { display: flex; align-items: baseline; gap: 10px; padding: 0 26px; border-left: 1px solid var(--studio-line, var(--line)); }
.metric-item:first-child { padding-left: 0; border: 0; }
.metric-item dd { margin: 0; font-size: 28px; font-weight: 500; line-height: 1.15; letter-spacing: -.04em; font-variant-numeric: tabular-nums; }
.metric-item dt { color: var(--studio-muted, var(--muted)); font-size: 12px; }
.tree-frame { padding: 20px 20px 14px; border: 1px solid var(--studio-line, var(--line)); border-radius: 9px; background: transparent; }
.tree-frame__heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px 16px; }
.tree-frame__title { display: grid; gap: 7px; }
.atlas-eyebrow { color: var(--studio-accent-deep, var(--accent-deep)); font-size: 9px; font-weight: 600; letter-spacing: .27em; }
.tree-frame__title h3 { margin: 0; font: 500 17px/1.4 var(--atlas-serif); letter-spacing: .06em; }
.tree-frame__legend { color: var(--studio-muted, var(--muted)); font-size: 11px; }
.tree-frame__legend>span { padding-left: 8px; }
.tree-frame__hint { display: flex; align-items: center; justify-content: center; flex-wrap: wrap; gap: 4px; margin: 8px 0 0; color: var(--studio-muted, var(--muted)); font-size: 11px; line-height: 1.6; text-align: center; }
.tree-frame__hint::before, .tree-frame__hint::after { content: ''; width: 45px; height: 1px; margin: 0 12px; background: var(--studio-line, var(--line)); }
.tree-state { display: flex; min-height: 310px; align-items: center; justify-content: center; flex-direction: column; gap: 12px; padding: 32px 16px; color: var(--studio-muted, var(--muted)); text-align: center; }
.tree-state>.el-icon { font-size: 28px; color: var(--studio-accent-deep, var(--accent-deep)); }
.tree-state strong { color: var(--studio-ink, var(--ink)); font-size: 15px; font-weight: 600; }
.tree-state p { max-width: 600px; margin: 0; font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.tree-state button { padding: 8px 14px; border: 1px solid var(--studio-line, var(--line)); border-radius: 5px; background: transparent; color: var(--studio-accent-deep, var(--accent-deep)); font-size: 12px; cursor: pointer; }
.tree-state--error strong { color: #b4533c; }
.home-values { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px; margin-top: 21px; padding-top: 20px; border-top: 1px solid var(--studio-line, var(--line)); }
.home-values article { display: flex; gap: 11px; align-items: flex-start; }
.value-icon { margin-top: 2px; color: var(--studio-accent-deep, var(--accent-deep)); font-size: 21px; }
.home-values h3 { margin: 0 0 5px; color: var(--studio-ink, var(--ink)); font: 500 14px/1.4 var(--atlas-serif); }
.home-values p { margin: 0; color: var(--studio-muted, var(--muted)); font-size: 11px; line-height: 1.6; }
@media (max-width: 780px) {
  .tree-home { padding: 0 16px 20px; }
  .home-intro { padding: 20px 0; gap: 16px; }
  .intro-copy { flex-basis: 100%; }
  .home-primary { min-height: 38px; }
  .home-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .metric-item { flex-direction: column; gap: 5px; padding: 0 14px; }
  .tree-frame { padding: 16px 12px 12px; }
  .tree-frame__hint::before, .tree-frame__hint::after { display: none; }
  .home-values { grid-template-columns: 1fr; gap: 15px; padding-top: 18px; }
}
@media (max-width: 480px) {
  .metric-item { padding: 0 9px; }
  .metric-item dt { font-size: 11px; }
  .metric-item dd { font-size: 26px; }
  .tree-frame__legend { font-size: 10px; }
}
@media (hover: none) { .hover-hint { display: none; } }
@media (prefers-reduced-motion: reduce) { .home-primary { transition: none; } }
</style>
