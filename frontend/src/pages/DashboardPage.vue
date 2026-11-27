<script setup lang="ts">
import { computed } from 'vue'
import { query, type Settings } from '../api/client'
import { cardThresholds, type AvailabilitySource, type CardThreshold, type UsageRange } from '../api/dashboard'
import DashboardUsage from '../components/dashboard/DashboardUsage.vue'
import DashboardAvailability from '../components/dashboard/DashboardAvailability.vue'
import DashboardCost from '../components/dashboard/DashboardCost.vue'

const props = defineProps<{ projectId: string; routeQuery: URLSearchParams; settings?: Settings }>()
const tab = computed(() => {
  const value = props.routeQuery.get('tab')
  return value === 'availability' || value === 'cost' ? value : 'usage'
})
const range = computed<UsageRange>(() => {
  const value = props.routeQuery.get('range')
  return value === 'today' || value === '30d' ? value : '7d'
})
const storageOpen = computed(() => props.routeQuery.get('storage') === '1')
const source = computed<AvailabilitySource>(() => props.routeQuery.get('source') === 'watch' ? 'watch' : 'pool')
const cardThreshold = computed(() => cardThresholds.find(count => String(count) === props.routeQuery.get('card_threshold')) ?? 1)
const selected = computed(() => {
  const key = props.routeQuery.get('key') ?? ''
  return source.value === 'watch' || key.startsWith('pool-') ? key : ''
})

function openTab(value: unknown) {
  if (value !== 'usage' && value !== 'availability' && value !== 'cost') return
  location.hash = 'dashboard' + query({
    project: props.projectId || undefined, tab: value,
    range: range.value, source: source.value,
    key: value === 'availability' ? selected.value || undefined : undefined,
    card_threshold: source.value === 'pool' && cardThreshold.value > 1 ? cardThreshold.value : undefined,
  })
}

function changeRange(value: UsageRange) {
  location.hash = 'dashboard' + query({
    project: props.projectId || undefined, tab: tab.value, range: value, source: source.value,
    storage: tab.value === 'cost' && storageOpen.value ? '1' : undefined,
    card_threshold: source.value === 'pool' && cardThreshold.value > 1 ? cardThreshold.value : undefined,
  })
}

function changeAvailability(nextSource: AvailabilitySource, key = '', cards: CardThreshold = cardThreshold.value) {
  location.hash = 'dashboard' + query({
    project: props.projectId || undefined, tab: 'availability', range: range.value,
    source: nextSource, key: key || undefined,
    card_threshold: nextSource === 'pool' && cards > 1 ? cards : undefined,
  })
}
</script>

<template>
  <div class="dashboard-page">
    <div class="page-heading">
      <h1>数据看板</h1>
      <span class="muted">北京时间</span>
    </div>
    <ElTabs :model-value="tab" @update:model-value="openTab">
      <ElTabPane label="我的用量" name="usage">
        <DashboardUsage v-if="tab === 'usage'" :project-id="projectId" :range="range" @change-range="changeRange" />
      </ElTabPane>
      <ElTabPane label="成本与占卡" name="cost">
        <DashboardCost v-if="tab === 'cost'" :key="projectId" :project-id="projectId" :range="range"
          :storage-open="storageOpen" @change-range="changeRange" />
      </ElTabPane>
      <ElTabPane label="库存趋势" name="availability">
        <DashboardAvailability v-if="tab === 'availability'" :project-id="projectId" :source="source"
          :selected="selected" :card-threshold="cardThreshold" :settings="settings" @select="changeAvailability" />
      </ElTabPane>
    </ElTabs>
  </div>
</template>

<style scoped>
.dashboard-page { display: grid; gap: .4rem; min-width: 0; }
.dashboard-page :deep(.el-tabs__nav-wrap::after) { background: var(--line); }
.dashboard-page :deep(.el-tabs__item) { font-weight: 650; }
.dashboard-page :deep(.el-tabs__content) { overflow: visible; }
</style>
