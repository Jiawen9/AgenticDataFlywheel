<template>
  <div class="model-publishing-page">
    <section class="placeholder-card">
      <el-icon class="placeholder-card__icon"><Promotion /></el-icon>
      <h1>模型发布</h1>
      <p>功能开发中，后续将在这里校验模型版本、配置发布目标并跟踪发布状态。</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { Promotion } from '@element-plus/icons-vue'
</script>

<style scoped>
.model-publishing-page {
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
