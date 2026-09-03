<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ArrowDown, CircleCheck, Collection, Cpu, DataAnalysis, Document, EditPen, House, Iphone, MagicStick, Operation, PieChart, Promotion, Share, TrendCharts, Upload, VideoPlay } from '@element-plus/icons-vue'

const expertExpanded = ref(true)
const collectionExpanded = ref(true)
const correctionExpanded = ref(true)
const modelTrainingExpanded = ref(true)
const route = useRoute()
const collectionActive = computed(() => route.path.startsWith('/collection'))
const correctionActive = computed(() => route.path.startsWith('/correction'))
const modelTrainingActive = computed(() => route.path.startsWith('/model-training'))
</script>

<template>
  <div class="app-shell">
    <aside class="app-sidebar">
      <div class="brand">
        <div class="brand__mark">DF</div>
        <div><strong>Data Flywheel</strong><span>数据飞轮平台</span></div>
      </div>
      <nav class="sidebar-nav">
        <router-link class="nav-primary-link" to="/home"><el-icon><House /></el-icon><span>首页</span></router-link>
        <section class="nav-group">
          <router-link class="nav-primary-link" to="/pipeline"><el-icon><Operation /></el-icon><span>自动 Pipeline</span></router-link>
        </section>
        <section class="nav-group nav-group--expert">
          <button class="nav-group__toggle" type="button" @click="expertExpanded = !expertExpanded">
            <span class="nav-group__toggle-label"><el-icon><Collection /></el-icon><span>专家工作台</span></span>
            <el-icon :class="{ 'is-collapsed': !expertExpanded }"><ArrowDown /></el-icon>
          </button>
          <div v-show="expertExpanded" class="nav-group__items">
            <router-link to="/task-generation/augmentation"><el-icon><MagicStick /></el-icon><span>任务泛化扩增</span></router-link>
            <div class="nav-subgroup">
              <button class="nav-subgroup__toggle" :class="{ active: collectionActive }" type="button" @click="collectionExpanded = !collectionExpanded">
                <span class="nav-group__toggle-label"><el-icon><Collection /></el-icon><span>轨迹采集</span></span>
                <el-icon :class="{ 'is-collapsed': !collectionExpanded }"><ArrowDown /></el-icon>
              </button>
              <div v-show="collectionExpanded" class="nav-tertiary-items">
                <router-link to="/collection/phone-factory"><el-icon><Iphone /></el-icon><span>手机工厂采集</span></router-link>
                <router-link to="/collection/tree-building"><el-icon><Share /></el-icon><span>轨迹树构建</span></router-link>
              </div>
            </div>
            <router-link to="/quality"><el-icon><DataAnalysis /></el-icon><span>轨迹质检</span></router-link>
            <div class="nav-subgroup">
              <button class="nav-subgroup__toggle" :class="{ active: correctionActive }" type="button" @click="correctionExpanded = !correctionExpanded">
                <span class="nav-group__toggle-label"><el-icon><EditPen /></el-icon><span>轨迹纠偏</span></span>
                <el-icon :class="{ 'is-collapsed': !correctionExpanded }"><ArrowDown /></el-icon>
              </button>
              <div v-show="correctionExpanded" class="nav-tertiary-items">
                <router-link to="/correction/expert-action"><el-icon><EditPen /></el-icon><span>专家动作纠偏</span></router-link>
                <router-link to="/correction/cot-generation"><el-icon><Document /></el-icon><span>COT 生成</span></router-link>
              </div>
            </div>
            <router-link to="/data-publishing"><el-icon><Upload /></el-icon><span>数据发布</span></router-link>
            <div class="nav-subgroup">
              <button class="nav-subgroup__toggle" :class="{ active: modelTrainingActive }" type="button" @click="modelTrainingExpanded = !modelTrainingExpanded">
                <span class="nav-group__toggle-label"><el-icon><Cpu /></el-icon><span>模型训练</span></span>
                <el-icon :class="{ 'is-collapsed': !modelTrainingExpanded }"><ArrowDown /></el-icon>
              </button>
              <div v-show="modelTrainingExpanded" class="nav-tertiary-items">
                <router-link to="/model-training/data-mixture"><el-icon><PieChart /></el-icon><span>训练数据配比</span></router-link>
                <router-link to="/model-training/launch"><el-icon><VideoPlay /></el-icon><span>拉起训练</span></router-link>
                <router-link to="/model-training/validation"><el-icon><CircleCheck /></el-icon><span>训练有效性验证</span></router-link>
              </div>
            </div>
            <router-link to="/model-publishing"><el-icon><Promotion /></el-icon><span>模型发布</span></router-link>
            <router-link to="/model-iteration-evaluation"><el-icon><TrendCharts /></el-icon><span>模型迭代评估</span></router-link>
          </div>
        </section>
        <section class="nav-group">
          <router-link class="nav-primary-link" to="/task-generation/scenario-tree"><el-icon><Share /></el-icon><span>GUI操控场景树</span></router-link>
        </section>
        <section class="nav-group">
          <router-link class="nav-primary-link" to="/phone-factory"><el-icon><Iphone /></el-icon><span>手机工厂监控</span></router-link>
        </section>
        <section class="nav-group">
          <router-link class="nav-primary-link" to="/data-publishing/overview"><el-icon><DataAnalysis /></el-icon><span>训练数据总览</span></router-link>
        </section>
      </nav>
      <div class="sidebar-foot"><i></i> 本地数据服务</div>
    </aside>
    <main class="app-main"><router-view /></main>
  </div>
</template>
