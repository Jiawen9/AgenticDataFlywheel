<template>
  <div class="model-training-page">
    <section class="placeholder-card">
      <el-icon class="placeholder-card__icon"><Cpu /></el-icon>
      <h1>训练启动</h1>
      <p>功能开发中，后续将在这里配置并启动训练任务，查看训练进度与结果。</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { Cpu } from '@element-plus/icons-vue'
</script>

<style scoped>
.model-training-page {
  display: grid;
  min-height: calc(100vh - 48px);
  padding: 24px;
  place-items: center;
}

.placeholder-card {
  width: min(520px, 100%);
  padding: 52px 32px;
  border: 1px solid var(--line);
  border-radius: 20px;
  background: var(--panel);
  box-shadow: var(--shadow);
  text-align: center;
}

.placeholder-card__icon {
  width: 56px;
  height: 56px;
  border-radius: 16px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 28px;
}

h1 {
  margin: 20px 0 10px;
  font-size: 28px;
}

p {
  margin: 0;
  color: var(--muted);
  line-height: 1.7;
}
</style>
