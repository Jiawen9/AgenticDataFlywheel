<template>
  <div class="task-generation-page">
    <header class="page-hero"><div><span class="eyebrow">TASK GENERATION</span><h1>任务生成</h1><p>从场景能力树组织任务覆盖范围，后续将在这里创建和管理轨迹采集任务。</p></div></header>
    <SceneCapabilityTree />
  </div>
</template>

<script setup lang="ts">
import SceneCapabilityTree from '@/components/SceneCapabilityTree.vue'
</script>

<style scoped>
.task-generation-page { width: min(1680px,100%); min-height: 100vh; margin: 0 auto; padding: 34px 42px 50px; }.page-hero { margin-bottom: 28px; }.page-hero h1 { margin: 6px 0 4px; }
</style>
