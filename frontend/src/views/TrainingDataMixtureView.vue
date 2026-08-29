<template>
  <div class="training-mixture-page">
    <section class="placeholder-card">
      <el-icon class="placeholder-card__icon"><PieChart /></el-icon>
      <h1>训练数据配比</h1>
      <p>功能开发中，后续将在这里配置不同任务、能力和数据类型的训练样本配比。</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { PieChart } from '@element-plus/icons-vue'
</script>

<style scoped>
.training-mixture-page { display: grid; min-height: calc(100vh - 48px); padding: 24px; place-items: center; }.placeholder-card { width: min(520px,100%); padding: 52px 32px; border: 1px solid var(--line); border-radius: 20px; background: white; box-shadow: 0 18px 42px rgba(15,23,42,.08); text-align: center; }.placeholder-card__icon { width: 56px; height: 56px; border-radius: 16px; background: rgba(20,184,166,.12); color: var(--accent); font-size: 28px; }.placeholder-card h1 { margin: 20px 0 10px; font-size: 28px; }.placeholder-card p { margin: 0; color: var(--muted); line-height: 1.7; }
</style>
