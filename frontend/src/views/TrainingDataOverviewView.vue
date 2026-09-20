<template>
  <div class="training-data-overview" :class="perspective === 'scene' ? 'scene-view' : 'app-view'" data-testid="training-data-overview">
    <h1>VLA训练数据概览 - {{ perspective === 'scene' ? '场景视角' : 'App视角' }}</h1>
    <div class="view-switch">
      <RouterLink to="/data-publishing/overview" class="view-link" :class="{ active: perspective === 'scene' }">场景视角</RouterLink>
      <RouterLink to="/data-publishing/overview?view=app" class="view-link" :class="{ active: perspective === 'app' }">App视角</RouterLink>
    </div>
    <div class="filters">
      <div class="filter-item">
        <label for="overview-source">数据来源：</label>
        <select id="overview-source" v-model="filters.source" data-testid="overview-source" @change="changeSource">
          <option value="all">所有数据</option>
          <option v-for="source in sources" :key="source" :value="source">{{ source }}</option>
        </select>
      </div>
      <div v-for="field in filterOrder" :key="field" class="filter-item">
        <template v-if="field === 'app'">
          <label for="overview-app">选择App：</label>
          <select id="overview-app" v-model="filters.app" data-testid="overview-app" @change="changeApp">
            <option value="all">全部App</option>
            <option v-for="app in apps" :key="app" :value="app">{{ app }}</option>
          </select>
        </template>
        <template v-else-if="field === 'level1'">
          <label for="overview-level1">一级场景：</label>
          <select id="overview-level1" v-model="filters.level1" data-testid="overview-level1" @change="changeLevel1">
            <option value="">全部</option>
            <option v-for="scene in sceneTree" :key="scene.name" :value="scene.name">{{ scene.name }}</option>
          </select>
        </template>
        <template v-else>
          <label for="overview-level2">二级场景：</label>
          <select id="overview-level2" v-model="filters.level2" data-testid="overview-level2" :disabled="!level2Options.length" @change="changeLevel2">
            <option value="">全部</option>
            <option v-for="level2 in level2Options" :key="level2" :value="level2">{{ level2 }}</option>
          </select>
        </template>
      </div>
      <div class="filter-item">
        <label for="overview-start-date">起始日期：</label>
        <input id="overview-start-date" v-model="filters.start_date" type="date" data-testid="overview-start-date" />
      </div>
      <div class="filter-item">
        <label for="overview-end-date">结束日期：</label>
        <input id="overview-end-date" v-model="filters.end_date" type="date" data-testid="overview-end-date" />
      </div>
      <div class="filter-item">
        <label>快捷选择：</label>
        <div style="display: flex; gap: 8px;">
          <button @click="recent(7)">最近一周</button>
          <button @click="recent(30)">最近一个月</button>
        </div>
      </div>
      <div class="filter-item">
        <button class="search-btn" data-testid="overview-search" :disabled="loading" @click="search">搜索</button>
      </div>
    </div>
    <div class="cards">
      <div v-for="item in visibleCardItems" :key="item.title" class="card" :data-testid="'overview-metric-' + item.key">
        <div class="card-title">{{ item.title }}</div>
        <div class="card-value">{{ formatValue(item.key) }}</div>
      </div>
    </div>
    <div class="charts">
      <TrainingOverviewChart class="chart-container" data-testid="overview-trend" label="轨迹数量随时间变化趋势（累积）" :option="trendChart" />
      <TrainingOverviewChart class="chart-container" data-testid="overview-comparison" :label="comparisonTitle" :option="comparisonChart" />
    </div>
    <div class="pie-row">
      <div class="pie-card">
        <h3>操作类型分布</h3>
        <TrainingOverviewChart class="pie-chart" data-testid="overview-action" label="操作类型分布" :option="actionChart" />
      </div>
      <div class="pie-card">
        <h3>任务步数分布</h3>
        <TrainingOverviewChart class="pie-chart" data-testid="overview-steps" label="任务步数分布" :option="stepsChart" />
      </div>
    </div>
    <div v-if="loading" class="loading-overlay">
      <div class="loading-spinner"></div>
      <div>加载中...</div>
    </div>
    <div v-if="successMessage" class="success-toast">{{ successMessage }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import TrainingOverviewChart from '@/components/TrainingOverviewChart.vue'
import { useTrainingDataOverview } from '@/composables/useTrainingDataOverview'
import { comparisonOption, distributionOption, trendOption } from '@/utils/trainingOverviewCharts'

const route = useRoute()
const { perspective, filters, sources, sceneTree, apps, data, loading, successMessage, level2Options, initialize, search, changeSource, changeLevel1, changeLevel2, changeApp, recent, dispose } = useTrainingDataOverview()
const filterOrder = computed(() => perspective.value === 'scene' ? ['level1', 'level2', 'app'] : ['app', 'level1', 'level2'])
const cardItems = [
  { title: '轨迹总数', key: 'total_trajectories' },
  { title: '子任务轨迹数', key: 'subtask_trajectories' },
  { title: 'Step总数', key: 'total_steps' },
  { title: '人工精修步骤', key: 'manual_refine_steps', onlyDataFlywheel: true },
  { title: '1级场景总数', key: 'level1_scenes' },
  { title: '2级场景总数', key: 'level2_scenes' },
  { title: 'App总数', key: 'total_apps' },
  { title: '轨迹平均Step数', key: 'avg_steps_per_trajectory' },
] as const
const visibleCardItems = computed(() => cardItems.filter(item => !('onlyDataFlywheel' in item) || data.value?.overview.show_manual_refine_steps === true))
function formatValue(key: typeof cardItems[number]['key']) {
  const value = data.value?.overview[key] ?? 0
  return key === 'avg_steps_per_trajectory' ? value.toFixed(1) : value.toLocaleString()
}
const comparisonTitle = computed(() => perspective.value === 'scene' ? '各App数据分布明细' : '各二级场景分布明细')
const trendChart = computed(() => trendOption(data.value?.trend ?? []))
const comparisonChart = computed(() => comparisonOption((perspective.value === 'scene' ? data.value?.app_stats : data.value?.scene_stats) ?? [], perspective.value))
const actionChart = computed(() => distributionOption((data.value?.action_stats ?? []).map(item => ({ name: item.category, value: item.count })), 'action'))
const stepsChart = computed(() => distributionOption((data.value?.step_stats ?? []).map(item => ({ name: item.steps + ' 步', value: item.count })), 'steps'))
watch(() => route.query.view === 'app' ? 'app' : 'scene', view => { void initialize(view) }, { immediate: true })
onBeforeUnmount(dispose)
</script>

<style scoped>
/* Scope DEV's reset to this page so the surrounding application keeps its layout. */
:where(.training-data-overview) :deep(*) { margin: 0; padding: 0; }
.filter-item button { font-family: Arial, sans-serif; }

.training-data-overview {
  max-width: 98%;
  margin: 0 auto;
  font-family: 'Microsoft YaHei', sans-serif;
  background-color: #eef3f6;
  padding: 10px;
}
h1 {
  margin: 15px 0;
  color: var(--ink);
  text-align: center;
  font-size: 28px;
}
.view-switch {
  display: flex;
  justify-content: center;
  gap: 20px;
  margin: 10px 0 20px 0;
}
.view-link {
  display: inline-block;
  padding: 8px 20px;
  background-color: #eef3f6;
  color: var(--muted);
  border-radius: 20px;
  text-decoration: none;
  font-size: 16px;
  transition: all 0.2s;
}
.view-link:hover {
  background-color: var(--accent);
  color: white;
}
.view-link.active {
  background-color: var(--accent-deep);
  color: white;
}
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
  align-items: flex-end;
  margin-bottom: 20px;
  background: white;
  padding: 15px 20px;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.05);
}
.filter-item {
  display: flex;
  flex-direction: column;
}
.filter-item label {
  font-size: 14px;
  color: var(--muted);
  margin-bottom: 5px;
}
.filter-item select,
.filter-item input[type=date] {
  padding: 8px 12px;
  border-radius: 4px;
  border: 1px solid var(--line);
  background-color: white;
  font-size: 14px;
  min-width: 160px;
  cursor: pointer;
  outline: none;
  font-family: inherit;
}
.filter-item select:disabled {
  background-color: #eef3f6;
  cursor: not-allowed;
}
.filter-item button {
  padding: 8px 12px;
  border-radius: 4px;
  border: 1px solid var(--line);
  background-color: #eef3f6;
  font-size: 14px;
  cursor: pointer;
}
.filter-item button:hover {
  background-color: var(--line);
}
.search-btn {
  background-color: var(--accent-deep) !important;
  color: white !important;
  border: none !important;
}
.search-btn:hover {
  background-color: var(--accent) !important;
}
.search-btn:disabled {
  background-color: color-mix(in srgb, var(--accent) 45%, white) !important;
  cursor: not-allowed;
}
.cards {
  display: flex;
  flex-wrap: wrap;
  gap: 15px;
  margin-bottom: 20px;
}
.card {
  background: white;
  border-radius: 8px;
  box-shadow: 0 2px 12px rgba(0,0,0,0.1);
  padding: 20px;
  flex: 1 1 200px;
  text-align: center;
  transition: transform 0.2s;
}
.card:hover { transform: translateY(-3px); }
.card-title { font-size: 16px; color: var(--muted); margin-bottom: 10px; }
.card-value { font-size: 28px; font-weight: bold; color: var(--ink); }
.charts {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.chart-container {
  background: white;
  border-radius: 8px;
  box-shadow: 0 2px 12px rgba(0,0,0,0.1);
  padding: 20px;
  width: 100%;
  height: 500px;
}
.pie-row {
  display: flex;
  gap: 20px;
  margin-top: 20px;
}
.pie-card {
  flex: 1;
  background: white;
  border-radius: 8px;
  box-shadow: 0 2px 12px rgba(0,0,0,0.1);
  padding: 20px;
}
.pie-card h3 {
  font-size: 18px;
  color: var(--ink);
  margin-bottom: 10px;
  text-align: center;
}
.pie-chart {
  width: 100%;
  height: 400px;
}
.loading-overlay {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(0,0,0,0.5);
  display: flex;
  justify-content: center;
  align-items: center;
  z-index: 9999;
  color: white;
  font-size: 20px;
  flex-direction: column;
}
.loading-spinner {
  border: 4px solid var(--line);
  border-top: 4px solid var(--accent);
  border-radius: 50%;
  width: 40px;
  height: 40px;
  animation: spin 1s linear infinite;
  margin-bottom: 10px;
}
@keyframes spin {
  0% { transform: rotate(0deg); }
  100% { transform: rotate(360deg); }
}
.success-toast {
  position: fixed;
  top: 20px;
  right: 20px;
  background: var(--accent-deep);
  color: white;
  padding: 12px 20px;
  border-radius: 4px;
  z-index: 10000;
  box-shadow: 0 2px 10px rgba(0,0,0,0.2);
}

</style>
