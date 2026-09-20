import type { EChartsCoreOption } from 'echarts/core'
import type { OverviewCounts, TrainingDataOverview } from '@/trainingDataOverviewApi'
import type { OverviewPerspective } from '@/composables/useTrainingDataOverview'

const palette = ['#0f766e', '#38bdf8', '#a78bfa', '#fb923c', '#64748b', '#e879a5', '#84cc16']
const countSeries: Array<{ key: keyof OverviewCounts; name: string }> = [
  { key: 'total_trajectories', name: '轨迹总数' },
  { key: 'subtask_trajectories', name: '子任务轨迹数' },
  { key: 'total_steps', name: 'Step总数' },
]
function colors() {
  const tokens = typeof document === 'undefined' ? undefined : getComputedStyle(document.documentElement)
  const token = (name: string, fallback: string) => tokens?.getPropertyValue(name).trim() || fallback
  return { ink: token('--ink', '#0f172a'), muted: token('--muted', '#64748b'), line: token('--line', '#dce5eb'), accent: token('--accent-deep', '#0f766e') }
}
function base(): EChartsCoreOption {
  const theme = colors()
  return { color: [theme.accent, ...palette.slice(1)], textStyle: { color: theme.ink } }
}

export function trendOption(data: TrainingDataOverview['trend']): EChartsCoreOption {
  const theme = colors()
  return {
    ...base(),
    title: { text: '轨迹数量随时间变化趋势（累积）', left: 'center', textStyle: { fontSize: 20, color: theme.ink } },
    tooltip: { trigger: 'axis' },
    legend: { bottom: 10, textStyle: { color: theme.muted } },
    grid: { left: '5%', right: '5%', bottom: '15%', containLabel: true },
    dataZoom: [{ type: 'slider', start: 0, end: 100, bottom: 40 }],
    xAxis: {
      type: 'category', boundaryGap: false, data: data.map(item => item.date),
      axisLabel: { rotate: 35, interval: 'auto', fontSize: 13, margin: 10, color: theme.muted },
      axisLine: { lineStyle: { color: theme.line } },
    },
    yAxis: { type: 'value', name: '累积数量', axisLabel: { color: theme.muted }, splitLine: { lineStyle: { color: theme.line } } },
    series: [
      { name: '总轨迹数', type: 'line', data: data.map(item => item.total_trajectories), smooth: true, areaStyle: { opacity: 0.1 } },
      { name: '子任务轨迹数', type: 'line', data: data.map(item => item.subtask_trajectories), smooth: true },
      { name: 'Step总数', type: 'line', data: data.map(item => item.total_steps), smooth: true, lineStyle: { type: 'dashed' } },
    ],
  }
}

export function comparisonOption(data: TrainingDataOverview['app_stats'], perspective: OverviewPerspective = 'scene'): EChartsCoreOption {
  const theme = colors()
  return {
    ...base(),
    title: { text: perspective === 'scene' ? '各App数据分布明细' : '各二级场景分布明细', left: 'center', textStyle: { fontSize: 20, color: theme.ink } },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { bottom: 10, textStyle: { color: theme.muted } },
    grid: { left: '5%', right: '5%', bottom: '20%', containLabel: true },
    dataZoom: [{ type: 'slider', start: 0, end: 100, bottom: 40 }],
    xAxis: { type: 'category', data: data.map(item => item.name), axisLabel: { rotate: 35, interval: 0, color: theme.muted }, axisLine: { lineStyle: { color: theme.line } } },
    yAxis: { type: 'value', name: '数量', axisLabel: { color: theme.muted }, splitLine: { lineStyle: { color: theme.line } } },
    series: countSeries.map(({ key, name }) => ({ name, type: 'bar', data: data.map(item => item[key]) })),
  }
}

export function distributionOption(data: Array<{ name: string; value: number }>, kind: 'action' | 'steps'): EChartsCoreOption {
  return {
    ...base(),
    tooltip: { trigger: 'item', formatter: kind === 'action' ? '{b}: {c} 个 ({d}%)' : '{b}: {c} 个任务 ({d}%)' },
    legend: { orient: 'vertical', left: 'left', top: 'center', textStyle: { color: colors().muted } },
    series: [{
      name: kind === 'action' ? '操作类型' : '任务步数', type: 'pie', radius: '50%', data,
      label: { show: true, position: 'outside', formatter: '{b}: {c}' },
    }],
  }
}
