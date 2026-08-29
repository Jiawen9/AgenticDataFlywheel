<script setup lang="ts">
import { Collection, DataAnalysis, EditPen } from '@element-plus/icons-vue'
</script>

<template>
  <div class="app-shell">
    <aside class="app-sidebar">
      <div class="brand">
        <div class="brand__mark">DF</div>
        <div><strong>Data Flywheel</strong><span>轨迹工作台</span></div>
      </div>
      <nav>
        <router-link to="/collection"><el-icon><Collection /></el-icon><span>轨迹采集</span></router-link>
        <router-link to="/quality"><el-icon><DataAnalysis /></el-icon><span>轨迹质检</span></router-link>
        <router-link to="/correction"><el-icon><EditPen /></el-icon><span>轨迹修正</span></router-link>
      </nav>
      <div class="sidebar-foot"><i></i> 本地数据服务</div>
    </aside>
    <main class="app-main"><router-view /></main>
  </div>
</template>
