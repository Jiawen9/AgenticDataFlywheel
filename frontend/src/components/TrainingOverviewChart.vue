<template>
  <div ref="host" class="training-overview-chart" role="img" :aria-label="label" />
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { init, use, type EChartsCoreOption, type EChartsType } from 'echarts/core'
import { LineChart, BarChart, PieChart } from 'echarts/charts'
import { TitleComponent, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, AriaComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

use([LineChart, BarChart, PieChart, TitleComponent, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, AriaComponent, CanvasRenderer])
const props = defineProps<{ option: EChartsCoreOption; label: string }>()
const host = ref<HTMLDivElement>()
let chart: EChartsType | undefined
let observer: ResizeObserver | undefined
onMounted(() => {
  if (!host.value) return
  chart = init(host.value)
  chart.setOption(props.option)
  observer = new ResizeObserver(() => chart?.resize())
  observer.observe(host.value)
})
watch(() => props.option, value => chart?.setOption(value, { notMerge: true }))
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose() })
</script>

