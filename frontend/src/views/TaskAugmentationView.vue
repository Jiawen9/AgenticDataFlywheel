<template>
  <div class="task-augmentation-page">
    <section class="placeholder-card">
      <el-icon class="placeholder-card__icon"><MagicStick /></el-icon>
      <h1>任务扩增</h1>
      <p>功能开发中，后续将在这里基于场景与能力节点生成和扩增任务。</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { MagicStick } from '@element-plus/icons-vue'
</script>

<style scoped>
.task-augmentation-page { display: grid; min-height: calc(100vh - 48px); padding: 24px; place-items: center; }.placeholder-card { width: min(520px,100%); padding: 52px 32px; border: 1px solid var(--line); border-radius: 20px; background: white; box-shadow: 0 18px 42px rgba(15,23,42,.08); text-align: center; }.placeholder-card__icon { width: 56px; height: 56px; border-radius: 16px; background: rgba(20,184,166,.12); color: var(--accent); font-size: 28px; }.placeholder-card h1 { margin: 20px 0 10px; font-size: 28px; }.placeholder-card p { margin: 0; color: var(--muted); line-height: 1.7; }
</style>
