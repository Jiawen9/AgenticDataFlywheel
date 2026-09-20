import { describe, expect, it } from 'vitest'
import { comparisonOption, distributionOption, trendOption } from './trainingOverviewCharts'

const counts = { total_trajectories: 7, subtask_trajectories: 3, total_steps: 42 }

describe('DEV overview chart geometry and content', () => {
  it('keeps the cumulative trend title, bottom legend, slider, smooth lines and dashed Step line', () => {
    expect(trendOption([{ date: '2026-09-20', ...counts }])).toMatchObject({
      title: { text: '轨迹数量随时间变化趋势（累积）', left: 'center', textStyle: { fontSize: 20 } },
      tooltip: { trigger: 'axis' }, legend: { bottom: 10 },
      grid: { left: '5%', right: '5%', bottom: '15%', containLabel: true },
      dataZoom: [{ type: 'slider', start: 0, end: 100, bottom: 40 }],
      xAxis: { boundaryGap: false, data: ['2026-09-20'], axisLabel: { rotate: 35, interval: 'auto', fontSize: 13, margin: 10 } },
      yAxis: { name: '累积数量' },
      series: [
        { name: '总轨迹数', type: 'line', data: [7], smooth: true, areaStyle: { opacity: 0.1 } },
        { name: '子任务轨迹数', type: 'line', data: [3], smooth: true },
        { name: 'Step总数', type: 'line', data: [42], smooth: true, lineStyle: { type: 'dashed' } },
      ],
    })
  })
  it.each([['scene', '各App数据分布明细'], ['app', '各二级场景分布明细']] as const)('uses the original %s distribution title and untruncated full-range bars', (perspective, title) => {
    const option = comparisonOption([{ name: '视频-播放', ...counts }], perspective)
    expect(option).toMatchObject({
      title: { text: title, left: 'center', textStyle: { fontSize: 20 } },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      legend: { bottom: 10 }, grid: { bottom: '20%', containLabel: true },
      dataZoom: [{ type: 'slider', start: 0, end: 100, bottom: 40 }],
      xAxis: { data: ['视频-播放'], axisLabel: { rotate: 35, interval: 0 } },
      series: [
        { name: '轨迹总数', type: 'bar', data: [7] },
        { name: '子任务轨迹数', type: 'bar', data: [3] },
        { name: 'Step总数', type: 'bar', data: [42] },
      ],
    })
    expect(option.series).toEqual([
      { name: '轨迹总数', type: 'bar', data: [7] },
      { name: '子任务轨迹数', type: 'bar', data: [3] },
      { name: 'Step总数', type: 'bar', data: [42] },
    ])
  })
  it.each([['action', '操作类型', '{b}: {c} 个 ({d}%)'], ['steps', '任务步数', '{b}: {c} 个任务 ({d}%)']] as const)('restores the solid %s pie with outside values and left vertical legend', (kind, name, formatter) => {
    expect(distributionOption([{ name: '3 步', value: 2 }], kind)).toMatchObject({
      tooltip: { trigger: 'item', formatter },
      legend: { orient: 'vertical', left: 'left', top: 'center' },
      series: [{ name, type: 'pie', radius: '50%', data: [{ name: '3 步', value: 2 }], label: { show: true, position: 'outside', formatter: '{b}: {c}' } }],
    })
  })
})
