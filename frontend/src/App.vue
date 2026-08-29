<script setup lang="ts">
<<<<<<< Updated upstream
import { Collection, DataAnalysis } from '@element-plus/icons-vue'
=======
import { Collection, DataAnalysis, EditPen, MagicStick } from '@element-plus/icons-vue'
>>>>>>> Stashed changes
</script>

<template>
  <div class="app-shell">
    <aside class="app-sidebar">
      <div class="brand">
        <div class="brand__mark">DF</div>
        <div><strong>Data Flywheel</strong><span>轨迹工作台</span></div>
      </div>
      <nav>
        <router-link to="/task-generation"><el-icon><MagicStick /></el-icon><span>任务生成</span></router-link>
        <router-link to="/collection"><el-icon><Collection /></el-icon><span>轨迹采集</span></router-link>
        <router-link to="/quality"><el-icon><DataAnalysis /></el-icon><span>轨迹质检</span></router-link>
      </nav>
      <div class="sidebar-foot"><i></i> 本地数据服务</div>
    </aside>
    <main class="app-main"><router-view /></main>
  </div>
</template>
