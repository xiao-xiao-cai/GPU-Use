<script setup lang="ts">
import { LineChart, type LineSeriesOption } from 'echarts/charts'
import { AriaComponent, DataZoomComponent, GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { init, use, type EChartsType } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { type Point } from '../api/client'
import { timeLabel } from '../display'

use([LineChart, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, AriaComponent, CanvasRenderer])

const props = defineProps<{ points: Point[]; title: string; since: number; until: number; resetVersion: number }>()
const emit = defineEmits<{ zoomed: [] }>()
const target = ref<HTMLElement>()
const hiddenSeries = reactive(new Set<string>())
let chart: EChartsType | undefined
let observer: ResizeObserver | undefined

const palette = ['#007e7b', '#396eac', '#a85223', '#84549b', '#64712a', '#a33d57']
// Match the collector's threshold for a gap between two points.
const maximumSampleGap = 90
const display = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
const percentage = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 })

const series = computed(() => {
  const groups = new Map<string, Map<number, number | null>>()
  let lastObserved = 0
  for (const point of props.points) {
    const samples = groups.get(point.label) ?? new Map<number, number | null>()
    samples.set(point.sampled_at, point.value)
    groups.set(point.label, samples)
    lastObserved = Math.max(lastObserved, point.sampled_at)
  }

  return [...groups.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([label, samples]) => {
    const times = [...samples.keys()].sort((a, b) => a - b)
    const data: [number, number | null][] = []
    for (const [index, time] of times.entries()) {
      const previous = times[index - 1]
      if (previous !== undefined && time - previous > maximumSampleGap) {
        data.push([((previous + time) / 2) * 1000, null])
      }
      data.push([time * 1000, samples.get(time) ?? null])
    }
    const last = times.at(-1)!
    const value = samples.get(last) ?? null
    return { id: label, label: label || '实例', data, last, value, interrupted: lastObserved - last > maximumSampleGap }
  })
})

function formatValue(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? percentage.format(value) + '%' : '暂无采样'
}

function selectSeries(id: string, event: Event) {
  if (!(event.target instanceof HTMLInputElement)) return
  if (event.target.checked) hiddenSeries.delete(id)
  else hiddenSeries.add(id)
}

function render() {
  const lines: LineSeriesOption[] = series.value.map((item, index) => ({
    id: item.id || 'instance',
    name: item.label,
    type: 'line',
    connectNulls: false,
    showSymbol: item.data.length < 20,
    symbolSize: 4,
    data: item.data,
    itemStyle: { color: palette[index % palette.length] },
    lineStyle: { width: 2, color: palette[index % palette.length] },
    emphasis: { focus: 'series' },
  }))
  chart?.setOption({
    animation: false,
    aria: { enabled: true, label: { description: props.title + '（%），包含 ' + series.value.length + ' 条序列。最近采样见图表下方。' } },
    grid: { top: 16, bottom: 57, left: 50, right: 16 },
    legend: {
      show: false,
      data: series.value.map(item => item.label),
      selected: Object.fromEntries(series.value.map(item => [item.label, !hiddenSeries.has(item.id)])),
    },
    tooltip: { trigger: 'axis', valueFormatter: formatValue },
    xAxis: {
      type: 'time',
      min: props.since * 1000,
      max: props.until * 1000,
      axisLabel: { color: '#49616a', formatter: (value: number) => display.format(value) },
    },
    yAxis: { type: 'value', min: 0, max: 100, axisLabel: { color: '#49616a', formatter: '{value}%' } },
    dataZoom: [{ id: 'range', type: 'slider', realtime: false, height: 16, bottom: 8 }, { id: 'inside', type: 'inside' }],
    series: lines,
  }, { replaceMerge: ['series'] })
}

function measure() {
  const element = target.value
  if (!element?.clientWidth || !element.clientHeight) return
  if (chart) {
    chart.resize()
  } else {
    chart = init(element)
    chart.on('datazoom', () => emit('zoomed'))
    render()
  }
}

onMounted(() => {
  if (!target.value) return
  observer = new ResizeObserver(measure)
  observer.observe(target.value)
  measure()
})
watch([series, () => props.since, () => props.until, () => props.title, () => [...hiddenSeries]], render, { flush: 'post' })
watch(() => props.resetVersion, () => {
  chart?.dispatchAction({ type: 'dataZoom', dataZoomId: 'range', start: 0, end: 100 }, { silent: true })
}, { flush: 'post' })
onUnmounted(() => {
  observer?.disconnect()
  chart?.dispose()
})
</script>

<template>
  <section class="metric-block">
    <h3>{{ title }}（%）</h3>
    <div v-show="series.length" ref="target" class="chart" :aria-label="title + '采样曲线'"></div>
    <p v-if="!series.length" class="muted">所选时间段暂无采样。</p>
    <ul v-else class="metric-legend" aria-label="各序列最近采样">
      <li v-for="(item, index) in series" :key="item.id">
        <label>
          <input type="checkbox" :checked="!hiddenSeries.has(item.id)" @change="selectSeries(item.id, $event)">
          <span class="legend-dot" :style="{ '--dot': palette[index % palette.length] }" aria-hidden="true"></span>
          <span>{{ item.label }}</span>
        </label>
        <strong>{{ formatValue(item.value) }}</strong>
        <span class="muted">{{ timeLabel(item.last) }}</span>
        <span v-if="item.interrupted" class="gap-label">采样中断</span>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.metric-block { min-width: 0; display: grid; gap: 12px; }
.chart { width: 100%; height: clamp(240px, 36vh, 350px); }
.metric-legend { display: flex; flex-wrap: wrap; gap: 8px; padding: 0; margin: 0; list-style: none; }
.metric-legend li, .metric-legend label { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; overflow-wrap: anywhere; }
.metric-legend li { padding: 8px; border: 1px solid var(--line); border-radius: 4px; font-size: .875rem; }
.metric-legend input { width: 16px; height: 16px; margin: 0; accent-color: var(--accent); }
.metric-legend input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.legend-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--dot); }
.gap-label { color: var(--warning); }
</style>
