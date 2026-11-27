<script setup lang="ts">
import { computed } from 'vue'
import { Reading, Timer, TrendCharts } from '@element-plus/icons-vue'
import { query } from '../../api/client'
import { readUsage, type AutomationOutcome, type UsageRange } from '../../api/dashboard'
import { timeLabel } from '../../display'
import DashboardChart from './DashboardChart.vue'
import { durationLabel, rateLabel, valueLabel } from './format'
import { useDashboardQuery } from './useDashboardQuery'

const props = defineProps<{ projectId: string; range: UsageRange }>()
const emit = defineEmits<{ changeRange: [range: UsageRange] }>()
const ranges = [
  { id: 'today', name: '今天' },
  { id: '7d', name: '近 7 天' },
  { id: '30d', name: '近 30 天' },
] as const
const outcomes: Record<AutomationOutcome, string> = {
  confirmed_shutdown: '已确认关机',
  reminded: '仅提醒',
  needs_review: '待核对',
  capacity_waiting: '等待资源',
  capacity_completed: '开机已确认',
  capacity_ended: '等待结束',
}

const path = computed(() => props.projectId ? '/dashboard/usage' + query({
  project_id: props.projectId, range: props.range,
}) : null)
const { data, loading, fault, refresh } = useDashboardQuery(path, readUsage)
const hasUsage = computed(() => Boolean(data.value?.days.some(day => day.gpu_hours != null)))

const trend = computed(() => {
  const days = data.value?.days ?? []
  return {
    grid: { left: 48, right: 47, top: 30, bottom: 32 },
    tooltip: { trigger: 'axis', renderMode: 'richText', confine: true },
    xAxis: {
      type: 'category', data: days.map(day => day.date.slice(5)),
      axisLabel: { color: '#4e666e', hideOverlap: true },
    },
    yAxis: [
      { type: 'value', min: 0, name: 'GPU·小时', nameTextStyle: { color: '#4e666e' } },
      { type: 'value', min: 0, max: 100, axisLabel: { formatter: '{value}%' } },
    ],
    series: [
      {
        id: 'gpu-hours', name: 'GPU·小时', type: 'bar', barMaxWidth: 24,
        itemStyle: { color: '#007e74', borderRadius: [3, 3, 0, 0] },
        data: days.map(day => day.gpu_hours),
      },
      {
        id: 'utilization', name: '平均利用率', type: 'line', yAxisIndex: 1,
        connectNulls: false, symbolSize: 5, lineStyle: { width: 2 }, color: '#a46420',
        tooltip: { valueFormatter: (value: unknown) => typeof value === 'number' ? valueLabel(value, '%') : '未知' },
        data: days.map(day => day.average_utilization),
      },
    ],
  }
})

function detailLink(page: 'instances' | 'tasks', id: string) {
  return '#' + page + query({ project: props.projectId, ...(page === 'instances' ? { instance: id } : { run: id }) })
}
</script>

<template>
  <section v-if="!projectId" class="panel">
    <ElEmpty description="选择项目后可查看用量。" :image-size="64" />
  </section>
  <div v-else class="usage-layout metric-container">
    <section class="panel usage-controls" aria-label="用量筛选">
      <div role="group" class="range-controls" aria-label="用量统计范围">
        <ElButton v-for="item in ranges" :key="item.id" :type="range === item.id ? 'primary' : 'default'"
          size="small" :aria-pressed="range === item.id" @click="emit('changeRange', item.id)">{{ item.name }}</ElButton>
      </div>
      <div class="refresh-controls">
        <small v-if="data" class="muted">统计至 {{ timeLabel(data.as_of) }}</small>
        <ElButton size="small" :loading="loading" @click="refresh">刷新</ElButton>
      </div>
    </section>
    <ElAlert v-if="fault" title="用量读取失败" :description="fault"
      :type="data ? 'warning' : 'error'" show-icon :closable="false" />
    <div v-if="loading && !data" class="panel">
      <ElSkeleton :rows="4" animated aria-label="正在读取项目用量" />
    </div>
    <template v-if="data">
      <dl class="usage-summary metric-grid metric-grid--three" aria-label="项目用量汇总">
        <div class="panel usage-stat">
          <dt><ElIcon aria-hidden="true"><Timer /></ElIcon>已观测用量</dt>
          <dd>{{ valueLabel(data.gpu_hours) }}<span v-if="data.gpu_hours !== null">GPU·小时</span></dd>
        </div>
        <div class="panel usage-stat">
          <dt><ElIcon aria-hidden="true"><TrendCharts /></ElIcon>平均利用率</dt>
          <dd>{{ valueLabel(data.average_utilization, '%') }}</dd>
        </div>
        <div class="panel usage-stat">
          <dt><ElIcon aria-hidden="true"><Reading /></ElIcon>使用实例</dt>
          <dd>{{ data.gpu_hours === null ? '暂无数据' : valueLabel(data.instances_used, ' 台') }}</dd>
        </div>
      </dl>

      <section class="panel" aria-labelledby="dashboard-usage-trend">
        <h2 id="dashboard-usage-trend">每日用量</h2>
        <p v-if="data.recording_since" class="muted">
          开始记录 {{ timeLabel(data.recording_since) }}。采集断档保留为空，已观测卡时按有效区间累计。
        </p>
        <div v-if="hasUsage" class="usage-legend" aria-hidden="true">
          <span>GPU·小时</span><span>平均利用率</span>
        </div>
        <DashboardChart v-if="hasUsage" :option="trend" description="每日 GPU·小时柱状图和平均 GPU 利用率折线图。逐日数据见图表下方。" />
        <ElEmpty v-else description="所选范围暂无可用的用量记录。" :image-size="64" />
        <details v-if="data.days.length">
          <summary>查看每日数据与采集覆盖率</summary>
          <div class="data-table-scroll usage-table-scroll" tabindex="0" role="region" aria-label="每日用量与采集覆盖率">
            <table class="usage-table" aria-label="每日用量数据">
              <thead><tr><th scope="col">日期</th><th scope="col">GPU·小时</th><th scope="col">平均利用率</th><th scope="col">采集覆盖率</th></tr></thead>
              <tbody>
                <tr v-for="day in data.days" :key="day.date">
                  <th scope="row">{{ day.date }}</th><td>{{ valueLabel(day.gpu_hours) }}</td>
                  <td>{{ valueLabel(day.average_utilization, '%') }}</td>
                  <td>
                    <template v-if="day.elapsed_seconds">
                      {{ rateLabel(day.coverage) }}
                      <small v-if="day.covered_seconds != null" class="muted">
                        {{ durationLabel(day.covered_seconds) }} / {{ durationLabel(day.elapsed_seconds) }}
                      </small>
                    </template>
                    <template v-else>{{ day.elapsed_seconds === 0 ? '—' : rateLabel(day.coverage) }}</template>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </details>
      </section>

      <div class="usage-detail">
        <section class="panel" aria-labelledby="dashboard-usage-ranking">
          <h2 id="dashboard-usage-ranking">实例用量排行</h2>
          <ElEmpty v-if="!data.instances.length" description="暂无已观测到卡时的实例。" :image-size="64" />
          <ul v-else class="usage-ranking">
            <li v-for="item in data.instances" :key="item.instance_id">
              <div>
                <a v-if="item.current" :href="detailLink('instances', item.instance_id)">{{ item.name }}</a>
                <strong v-else>{{ item.name }}</strong>
                <small class="muted">{{ [item.gpu_model, item.current ? null : '历史实例'].filter(Boolean).join(' · ') }}</small>
              </div>
              <div class="usage-values">
                <strong>{{ valueLabel(item.gpu_hours, ' GPU·小时') }}</strong>
                <small class="muted">利用率 {{ valueLabel(item.average_utilization, '%') }}</small>
              </div>
            </li>
          </ul>
        </section>

        <section class="panel metric-container" aria-labelledby="dashboard-automation">
          <h2 id="dashboard-automation">自动化与预约</h2>
          <dl class="automation-totals metric-grid metric-grid--three">
            <div><dt>已确认自动关机</dt><dd>{{ data.automation.confirmed_shutdowns }}</dd></div>
            <div><dt>仅提醒</dt><dd>{{ data.automation.reminded }}</dd></div>
            <div><dt>待核对</dt><dd>{{ data.automation.needs_review }}</dd></div>
            <div><dt>曾等待资源</dt><dd>{{ data.automation.capacity_waits }}</dd></div>
            <div><dt>等待后开机成功</dt><dd>{{ data.automation.capacity_completed }}</dd></div>
            <div><dt>成功平均等待</dt><dd>{{ durationLabel(data.automation.average_wait_seconds) }}</dd></div>
          </dl>
          <details v-if="data.automation.recent.length">
            <summary>查看近期回执</summary>
            <ul class="row-list">
              <li v-for="receipt in data.automation.recent" :key="receipt.run_id">
                <a :href="detailLink('tasks', receipt.run_id)">{{ receipt.name }}</a>
                <small class="muted">
                  {{ outcomes[receipt.outcome] }} · {{ timeLabel(receipt.observed_at) }}
                  <template v-if="receipt.wait_seconds !== null"> · 等待 {{ durationLabel(receipt.wait_seconds) }}</template>
                </small>
              </li>
            </ul>
          </details>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped>
.usage-layout, .usage-detail { display: grid; gap: 12px; min-width: 0; }
.usage-controls, .range-controls, .refresh-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.usage-controls { justify-content: space-between; }
.range-controls .el-button + .el-button { margin: 0; }
.refresh-controls { margin-top: 0; }
.usage-summary { gap: 12px; margin: 0; }
.usage-stat dt { display: flex; align-items: center; gap: 6px; color: var(--muted); }
.usage-stat dt .el-icon { color: var(--accent); }
.usage-stat dd { margin: .56rem 0 0; font-size: clamp(1.08rem, 2.55vw, 1.8rem); font-weight: 710; line-height: 1.1; overflow-wrap: anywhere; }
.usage-stat dd span { margin-inline-start: .45em; font-size: .7rem; font-weight: 500; color: var(--muted); white-space: nowrap; }
.usage-legend { display: flex; flex-wrap: wrap; gap: 12px; color: var(--muted); font-size: .84rem; }
.usage-legend span { display: inline-flex; align-items: center; gap: 5px; }
.usage-legend span::before { content: ''; width: .65rem; height: .65rem; border-radius: 2px; background: #007e74; }
.usage-legend span + span::before { height: .16rem; background: #a46420; }
.usage-detail { grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; }
.usage-ranking { display: grid; padding: 0; list-style: none; }
.usage-ranking li { display: grid; grid-template-columns: minmax(0, 1fr) minmax(7rem, auto); gap: 10px; padding: .6rem 0; border-top: 1px solid var(--line); overflow-wrap: anywhere; }
.usage-values { text-align: end; }
.automation-totals { gap: .7rem; margin-bottom: 0; }
.automation-totals dt { color: var(--muted); font-size: .85rem; }
.automation-totals dd { margin: 2px 0 0; font-weight: 650; }
.usage-table-scroll { --table-min-width: 26rem; margin-top: 8px; }
.usage-table { width: 100%; border-collapse: collapse; font-size: .87rem; text-align: left; }
.usage-table th, .usage-table td { padding: .4rem; border-bottom: 1px solid var(--line); }
.usage-table th[scope='row'] { font-weight: normal; }
@container metric-panel (max-width: 52rem) { .usage-detail { grid-template-columns: 1fr; } }
@media (max-width: 480px) {
  .usage-summary { gap: 6px; }
  .usage-stat { padding: .6rem; }
  .usage-stat dt { font-size: .76rem; gap: 4px; }
  .usage-stat dd span { display: block; margin: .3em 0 0; }
}
</style>
