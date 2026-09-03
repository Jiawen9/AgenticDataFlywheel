<template>
  <div class="home-page">
    <header class="home-hero">
      <div class="home-hero__copy">
        <span class="eyebrow">AGENTIC DATA FLYWHEEL</span>
        <h1>让数据生产、模型训练与评估形成持续闭环</h1>
        <p>通过自动 Pipeline 执行端到端流程，或进入专家工作台查看结果、复核质量并进行人工干预。</p>
        <div class="home-actions">
          <router-link class="primary-action" to="/pipeline"><el-icon><Operation /></el-icon>进入自动 Pipeline</router-link>
          <router-link class="secondary-action" to="/collection/tree-building"><el-icon><Collection /></el-icon>进入专家工作台</router-link>
          <router-link class="secondary-action overview-action" to="/data-publishing/overview"><el-icon><DataAnalysis /></el-icon>训练数据总览</router-link>
          <router-link class="secondary-action" to="/phone-factory"><el-icon><Iphone /></el-icon>手机工厂监控</router-link>
        </div>
      </div>
      <div class="flywheel-visual" aria-label="数据飞轮流程">
        <div class="flywheel-visual__core"><strong>DATA</strong><span>FLYWHEEL</span></div>
        <span v-for="(stage, index) in stages" :key="stage.name" class="flywheel-node" :style="nodeStyle(index)">
          <el-icon><component :is="stage.icon" /></el-icon>
          <small>{{ stage.name }}</small>
        </span>
      </div>
    </header>

    <section class="journey">
      <div class="section-heading"><div><span class="eyebrow">WORKFLOW</span><h2>数据飞轮全流程</h2></div><p>每个阶段都可以在专家工作台中单独查看和操作</p></div>
      <div class="journey-grid">
        <router-link v-for="(stage, index) in stages" :key="stage.name" :to="stage.path" class="journey-card">
          <span>{{ String(index + 1).padStart(2, '0') }}</span>
          <el-icon><component :is="stage.icon" /></el-icon>
          <strong>{{ stage.name }}</strong>
        </router-link>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { Collection, Cpu, DataAnalysis, EditPen, Iphone, Plus, Promotion, TrendCharts, Upload, Operation } from '@element-plus/icons-vue'

const stages = [
  { name: '任务生成', path: '/task-generation/scenario-tree', icon: Plus },
  { name: '轨迹采集', path: '/collection/tree-building', icon: Collection },
  { name: '轨迹质检', path: '/quality', icon: DataAnalysis },
  { name: '轨迹纠偏', path: '/correction/expert-action', icon: EditPen },
  { name: '数据发布', path: '/data-publishing/archive', icon: Upload },
  { name: '模型训练', path: '/model-training/launch', icon: Cpu },
  { name: '模型发布', path: '/model-publishing', icon: Promotion },
  { name: '迭代评估', path: '/model-iteration-evaluation', icon: TrendCharts },
]

function nodeStyle(index: number) {
  const angle = (index / stages.length) * Math.PI * 2 - Math.PI / 2
  return {
    left: `${50 + Math.cos(angle) * 43}%`,
    top: `${50 + Math.sin(angle) * 43}%`,
  }
}

</script>

<style scoped>
.home-page { min-height: 100vh; padding: 42px; background: radial-gradient(circle at 85% 0%,rgba(14,165,233,.13),transparent 28%),radial-gradient(circle at 15% 80%,rgba(20,184,166,.1),transparent 24%); }
.home-hero { display: grid; grid-template-columns: minmax(0,1.25fr) minmax(280px,.75fr); align-items: center; gap: 64px; max-width: 1500px; margin: 0 auto; padding: 28px 0 44px; }.home-hero h1 { max-width: 820px; margin: 10px 0 18px; font-size: clamp(38px,4.7vw,68px); line-height: 1.08; letter-spacing: -.055em; }.home-hero__copy > p { max-width: 720px; margin: 0; color: var(--muted); font-size: 17px; line-height: 1.8; }
.home-actions { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 30px; }.home-actions a { display: flex; align-items: center; gap: 9px; padding: 13px 19px; border-radius: 12px; font-weight: 800; }.primary-action { background: #0f766e; color: white; box-shadow: 0 12px 28px rgba(15,118,110,.22); }.secondary-action { border: 1px solid var(--line); background: rgba(255,255,255,.7); color: var(--ink); }
.flywheel-visual { position: relative; width: min(390px,100%); aspect-ratio: 1; margin: auto; border: 1px solid rgba(20,184,166,.18); border-radius: 50%; background: radial-gradient(circle,rgba(20,184,166,.16),rgba(255,255,255,.38) 43%,transparent 44%); }.flywheel-visual::before,.flywheel-visual::after { position: absolute; border: 1px dashed rgba(15,118,110,.22); border-radius: 50%; content: ''; }.flywheel-visual::before { inset: 14%; }.flywheel-visual::after { inset: 27%; }.flywheel-visual__core { position: absolute; inset: 37%; display: grid; z-index: 2; place-content: center; border-radius: 50%; background: #0b1324; color: white; text-align: center; box-shadow: 0 18px 45px rgba(15,23,42,.3); }.flywheel-visual__core strong { font-size: 20px; }.flywheel-visual__core span { color: #5eead4; font-size: 9px; letter-spacing: .2em; }.flywheel-node { position: absolute; display: grid; z-index: 3; width: 66px; height: 66px; transform: translate(-50%,-50%); place-content: center; border: 1px solid white; border-radius: 18px; background: rgba(255,255,255,.92); color: var(--accent-deep); box-shadow: 0 10px 25px rgba(15,23,42,.1); text-align: center; transition: transform .18s ease,box-shadow .18s ease; }.flywheel-node:hover { transform: translate(-50%,-50%) scale(1.08); box-shadow: 0 15px 30px rgba(15,23,42,.16); }.flywheel-node .el-icon { margin: auto; font-size: 20px; }.flywheel-node small { margin-top: 4px; color: var(--ink); font-size: 9px; white-space: nowrap; }
.journey { max-width: 1500px; margin: 42px auto 0; }.journey-grid { display: grid; grid-template-columns: repeat(8,minmax(105px,1fr)); gap: 10px; }.journey-card { display: grid; min-height: 130px; padding: 16px; border: 1px solid var(--line); border-radius: 15px; background: rgba(255,255,255,.68); color: var(--ink); transition: .18s ease; }.journey-card:hover { border-color: #5eead4; background: white; transform: translateY(-2px); }.journey-card > span { color: #94a3b8; font-size: 9px; font-weight: 900; }.journey-card .el-icon { align-self: end; color: var(--accent-deep); font-size: 21px; }.journey-card strong { margin-top: 9px; font-size: 12px; }
@media (max-width: 1100px) { .home-hero { grid-template-columns: 1fr; }.flywheel-visual { display: none; }.journey-grid { grid-template-columns: repeat(4,1fr); } }
@media (max-width: 800px) { .home-page { padding: 28px 22px; }.journey-grid { grid-template-columns: repeat(2,1fr); }.section-heading { align-items: flex-start; flex-direction: column; } }
</style>
