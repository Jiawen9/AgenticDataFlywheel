<template>
  <div class="page phone-factory-view">
    <header class="page-hero">
      <div>
        <span class="eyebrow">PHONE FACTORY</span>
        <h1>手机工厂监控</h1>
        <p>查看手机工厂设备、运行状态和采集资源。</p>
      </div>
      <el-tag type="success" effect="light">查看模式</el-tag>
    </header>

    <section class="factory-overview">
      <div class="overview-card overview-card--active">
        <span class="card-label">工厂状态</span>
        <strong>运行中</strong>
        <small>设备连接正常</small>
      </div>
      <div class="overview-card">
        <span class="card-label">在线设备</span>
        <strong>—</strong>
        <small>等待设备状态接入</small>
      </div>
      <div class="overview-card">
        <span class="card-label">采集任务</span>
        <strong>—</strong>
        <small>可从“手机工厂采集”创建任务</small>
      </div>
    </section>

    <section class="empty-card">
      <el-icon><Iphone /></el-icon>
      <h2>手机工厂监控</h2>
      <p>这里用于查看手机工厂的设备与运行资源。需要执行采集任务时，请进入“手机工厂采集”。</p>
      <router-link class="factory-link" to="/collection/phone-factory">进入手机工厂采集</router-link>
    </section>
  </div>
</template>

<script setup lang="ts">
import { Iphone } from '@element-plus/icons-vue'
</script>

<style scoped>
.phone-factory-view{min-height:100vh}.factory-overview{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:22px}.overview-card{display:grid;gap:8px;padding:18px 20px;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.82);box-shadow:0 10px 28px rgba(15,23,42,.04)}.overview-card--active{border-color:#99f6e4;background:#f0fdfa}.card-label{color:#64748b;font-size:11px;font-weight:800;letter-spacing:.06em}.overview-card strong{color:#0f766e;font-size:24px;font-weight:700}.overview-card small{color:#94a3b8;font-size:11px}.empty-card{display:grid;justify-items:center;gap:10px;margin-top:16px;padding:58px 24px;border:1px solid var(--line);border-radius:16px;background:rgba(255,255,255,.72);text-align:center}.empty-card>.el-icon{display:grid;place-items:center;width:52px;height:52px;border-radius:15px;background:#ecfdf5;color:#0f766e;font-size:26px}.empty-card h2{margin:4px 0 0;color:#1e293b;font-size:20px}.empty-card p{max-width:560px;margin:0;color:#64748b;font-size:13px;line-height:1.7}.factory-link{margin-top:8px;color:#0f766e;font-size:13px;font-weight:700}@media(max-width:760px){.factory-overview{grid-template-columns:1fr}}
</style>
