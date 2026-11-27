<script setup lang="ts">
import { BarChart, HeatmapChart, LineChart } from 'echarts/charts'
import { AriaComponent, GridComponent, TooltipComponent, VisualMapPiecewiseComponent } from 'echarts/components'
import { init, use, type EChartsCoreOption, type EChartsType } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { onMounted, onUnmounted, ref, watch } from 'vue'

use([BarChart, HeatmapChart, LineChart, AriaComponent, GridComponent, TooltipComponent, VisualMapPiecewiseComponent, CanvasRenderer])

const props = defineProps<{ option: EChartsCoreOption; description: string }>()
const target = ref<HTMLElement>()
let chart: EChartsType | undefined
let observer: ResizeObserver | undefined

function draw() {
  chart?.setOption({
    animation: false,
    aria: { enabled: true, label: { description: props.description } },
    ...props.option,
  }, { replaceMerge: ['series'] })
}

function measure() {
  const element = target.value
  if (!element?.clientWidth || !element.clientHeight) return
  if (chart) chart.resize()
  else {
    chart = init(element)
    draw()
  }
}

onMounted(() => {
  if (!target.value) return
  observer = new ResizeObserver(measure)
  observer.observe(target.value)
  measure()
})
watch([() => props.option, () => props.description], draw, { flush: 'post' })
onUnmounted(() => {
  observer?.disconnect()
  chart?.dispose()
})
</script>

<template>
  <div ref="target" class="dashboard-chart" role="img" :aria-label="description"></div>
</template>

<style scoped>
.dashboard-chart { width: 100%; min-width: 0; height: clamp(230px, 34vw, 330px); }
</style>
