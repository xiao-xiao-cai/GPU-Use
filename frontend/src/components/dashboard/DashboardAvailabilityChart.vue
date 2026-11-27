<script setup lang="ts">
import { computed, ref } from 'vue'
import type { AvailabilityHour } from '../../api/dashboard'
import { localInput, timeLabel } from '../../display'
import DashboardChart from './DashboardChart.vue'
import { coverageRate, hourLabel, rateLabel, stockCoverageName, stockRate, stockRateName, valueLabel } from './format'

const props = defineProps<{
  hours: AvailabilityHour[]; mode: 'pool-line' | 'pool-heatmap' | 'watch-heatmap'
}>()
const showTable = ref(false)
const isPool = computed(() => props.mode !== 'watch-heatmap')
const isLine = computed(() => props.mode === 'pool-line')
const prepared = computed(() => props.hours.map(entry => {
  const local = localInput(entry.start)
  return { ...entry, day: local.slice(0, 10), hour: Number(local.slice(11, 13)), rate: stockRate(entry) }
}))
const hasPoolData = computed(() => props.hours.some(entry => entry.median_cards != null))

const unknown = { value: -1, color: '#e3e9e8', label: '未知' }
const poolBands = [
  unknown,
  { value: 0, color: '#e8ac80', label: '0 张' },
  { gt: 0, lt: 2, color: '#d0e7e1', label: '大于 0、少于 2 张' },
  { gte: 2, lt: 4, color: '#8fc7b9', label: '2 至不足 4 张' },
  { gte: 4, lt: 8, color: '#438f83', label: '4 至不足 8 张' },
  { gte: 8, color: '#17584f', label: '8 张及以上' },
]
const watchBands = [
  unknown,
  { gte: 0, lte: 30, color: '#e8ac80', label: '30% 及以下' },
  { gt: 30, lt: 70, color: '#a6cfc1', label: '高于 30%、低于 70%' },
  { gte: 70, color: '#19786e', label: '70% 及以上' },
]
const heatmapBands = computed(() => isPool.value ? poolBands : watchBands)
const description = computed(() => {
  if (isLine.value) return '近七天各小时的 GPU 卡数中位数。规格可租率和规格探测完整率见图表下方。'
  return isPool.value ? '近七天按北京时间排列的 GPU 余量中位数热力图，灰色表示缺少样本。'
    : '近七天各小时的规格可租率热力图。逐小时记录数和完整率见图表下方。'
})

function tooltip(index: number) {
  const entry = prepared.value[index]
  if (!entry) return ''
  return [
    timeLabel(entry.start) + ' 起 · 1 小时',
    ...(isPool.value ? ['余量中位数 ' + valueLabel(entry.median_cards ?? null, ' 张')] : []),
    stockRateName + ' ' + rateLabel(entry.rate),
    '可租 ' + entry.available_count + ' 次 / 有结果 ' + entry.known_count + ' 次 / 计划 ' + entry.expected_count + ' 次探测',
    stockCoverageName + ' ' + rateLabel(coverageRate(entry)),
  ].join('\n')
}

const chartOptions = computed(() => {
  const entries = prepared.value
  const tooltipOptions = { renderMode: 'richText', confine: true,
    formatter: ({ dataIndex }: { dataIndex: number }) => tooltip(dataIndex),
  }
  if (isLine.value) return {
    grid: { left: 54, right: 12, top: 28, bottom: 38 },
    tooltip: tooltipOptions,
    xAxis: {
      type: 'time', min: 'dataMin', max: 'dataMax',
      axisLabel: { hideOverlap: true, color: '#4e666e', formatter: (value: number) => timeLabel(value / 1000) },
    },
    yAxis: { type: 'value', min: 0, name: '张', axisLabel: { color: '#4e666e' } },
    series: [{
      id: 'pool-median', type: 'line', symbol: 'circle', symbolSize: 5, connectNulls: false,
      lineStyle: { width: 2, color: '#19786e' }, itemStyle: { color: '#19786e' },
      data: entries.map(entry => [entry.start * 1000, entry.median_cards ?? null]),
    }],
  }
  const dates = [...new Set(entries.map(entry => entry.day))]
  const index = new Map(dates.map((day, position) => [day, position]))
  return {
    grid: { left: 49, right: 8, top: 12, bottom: 36 },
    tooltip: tooltipOptions,
    visualMap: { type: 'piecewise', show: false, dimension: 2, pieces: heatmapBands.value },
    xAxis: {
      type: 'category', data: Array.from({ length: 24 }, (_, hour) => hourLabel(hour)),
      axisLabel: { interval: 3, color: '#4e666e' }, axisTick: { show: false },
    },
    yAxis: {
      type: 'category', inverse: true, data: dates.map(day => day.slice(5)),
      axisLabel: { color: '#4e666e' }, axisTick: { show: false },
    },
    series: [{
      id: props.mode, type: 'heatmap',
      data: entries.map(entry => [entry.hour, index.get(entry.day),
        isPool.value ? entry.median_cards ?? -1 : entry.rate === null ? -1 : entry.rate * 100]),
      itemStyle: { borderColor: '#fff', borderWidth: 2 },
      emphasis: { itemStyle: { shadowColor: '#204a42', shadowBlur: 4 } },
    }],
  }
})

function readDetails(event: Event) {
  showTable.value = (event.currentTarget as HTMLDetailsElement).open
}
</script>

<template>
  <template v-if="hours.length">
    <ElEmpty v-if="isLine && !hasPoolData" description="此资源池暂无有效余量样本。" :image-size="64" />
    <DashboardChart v-else class="availability-chart" :option="chartOptions" :description="description" />
    <ul v-if="!isLine" class="heatmap-legend" :aria-label="isPool ? '余量色阶' : stockRateName + '色阶'">
      <li v-for="band in heatmapBands" :key="band.label"><i :style="{ background: band.color }" aria-hidden="true"></i>{{ band.label }}</li>
    </ul>
    <details v-if="mode !== 'pool-heatmap'" @toggle="readDetails">
      <summary>查看逐小时数据</summary>
      <div v-if="showTable" class="data-table-scroll heatmap-table-scroll" tabindex="0" role="region" aria-label="逐小时库存数据">
        <table aria-label="逐小时库存样本">
          <thead>
            <tr>
              <th scope="col">时段起点</th><th v-if="isPool" scope="col">余量中位数</th><th scope="col">{{ stockRateName }}</th>
              <th scope="col">探测次数（可租／有结果／计划）</th><th scope="col">{{ stockCoverageName }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in prepared" :key="item.start">
              <th scope="row">{{ timeLabel(item.start) }}</th>
              <td v-if="isPool">{{ valueLabel(item.median_cards ?? null, ' 张') }}</td>
              <td>{{ rateLabel(item.rate) }}</td>
              <td>{{ item.available_count }} / {{ item.known_count }} / {{ item.expected_count }}</td>
              <td>{{ rateLabel(coverageRate(item)) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </details>
  </template>
  <ElEmpty v-else :description="isPool ? '此资源池暂无余量历史。' : '此规格暂无库存历史。'" :image-size="64" />
</template>

<style scoped>
.availability-chart { height: 264px; }
.heatmap-legend { display: flex; flex-wrap: wrap; gap: 7px 13px; padding: 0; list-style: none; color: var(--muted); font-size: .8rem; }
.heatmap-legend li { display: flex; align-items: center; gap: 5px; }
.heatmap-legend i { width: .65rem; height: .65rem; border-radius: 2px; }
.heatmap-table-scroll { --table-min-width: 30rem; max-height: 23rem; margin-top: 8px; }
table { width: 100%; border-collapse: collapse; text-align: left; font-size: .85rem; }
td, th { padding: .4rem; border-bottom: 1px solid var(--line); }
th[scope='row'] { font-weight: normal; }
</style>
