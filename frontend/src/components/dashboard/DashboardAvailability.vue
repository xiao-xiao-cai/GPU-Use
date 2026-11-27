<script setup lang="ts">
import { computed, inject, nextTick, ref, watch } from 'vue'
import { query, RequestError, type Settings } from '../../api/client'
import { cardThresholds, readAvailability, type AvailabilityItem, type AvailabilitySource, type CardThreshold } from '../../api/dashboard'
import { locationKey, specLabel, useLocationNames, zoneKey, type CatalogRow } from '../../catalog'
import { timeLabel, useSnapshot } from '../../display'
import DashboardAvailabilityChart from './DashboardAvailabilityChart.vue'
import DashboardReminder from './DashboardReminder.vue'
import StockRemaining from '../StockRemaining.vue'
import MetricHelp from './MetricHelp.vue'
import { coverageRate, hourLabel, rateLabel, stockCoverageName, stockRate, stockRateName, valueLabel } from './format'
import { useDashboardQuery } from './useDashboardQuery'

const props = defineProps<{
  projectId: string; source: AvailabilitySource; selected: string; cardThreshold: CardThreshold; settings?: Settings
}>()
const emit = defineEmits<{ select: [source: AvailabilitySource, key?: string, cards?: CardThreshold] }>()
const source = computed(() => props.source)
const isPool = computed(() => source.value === 'pool')
const canQuery = computed(() => isPool.value || Boolean(props.projectId))
const sources = [{ id: 'pool', label: '平台余量' }, { id: 'watch', label: '关注规格' }] as const
const { regionName, zoneName } = useLocationNames()
const locations = inject(locationKey, ref([]))
const { state: catalog } = useSnapshot<CatalogRow>(computed(() => isPool.value ? '/catalog' : null), 60_000)
const catalogSpecs = computed(() => new Set(catalog.value.data.filter(item => item.specs?.length)
  .map(item => specKey(item))))
const cardSpecLabels = { 1: '单卡规格', 2: '双卡规格', 4: '4 卡规格', 8: '8 卡规格' }
const emptyFilters = () => ({ gpu_model: '', region: '', zone: '', instance_type: '' })
const filters = ref({ ...emptyFilters(), card_threshold: props.cardThreshold })
const applied = ref(emptyFilters())
const rateHelp = computed(() => isPool.value
  ? `近七天内，可租次数 ÷ 有明确结果的探测次数。每次探测中，恰好 ${props.cardThreshold} 卡的组合至少有一个可租，就记为可租。所有组合均返回不可租才记为不可租。`
  : '近七天内，可租次数 ÷ 有明确结果的探测次数。平台按关注的 GPU 卡数、CPU 核数与内存组合探测。')
const coverageHelp = '近七天内，有明确结果的探测次数 ÷ 计划探测次数。可租和不可租都算有明确结果。每 5 分钟预计探测一次。'
const remainingCardsHelp = '斜杠前是当前剩余卡数。斜杠后以同一可用区、机型和资源池有记录以来的最高剩余卡数为参照，出现新高时更新。平台提供总卡数时，以平台总量为准。圆环显示当前余卡占参照值的比例。'
const offset = ref(0)
const pageSize = 12
const heading = ref<HTMLElement>()
let focusResult = false

const locationChoices = computed(() => [...new Map([...catalog.value.data, ...locations.value].map(item => [zoneKey(item), item])).values()])
const regions = computed(() => [...new Set(locationChoices.value.map(item => item.region))].sort())
const zones = computed(() => locationChoices.value
  .filter(item => !filters.value.region || item.region === filters.value.region)
  .sort((a, b) => a.zone.localeCompare(b.zone)))
const scopedQuery = computed(() => ({
  source: source.value, project_id: props.projectId || undefined,
  card_threshold: isPool.value ? props.cardThreshold : undefined,
}))
const listPath = computed(() => canQuery.value ? '/dashboard/availability' + query({
  ...scopedQuery.value,
  ...(isPool.value ? {
    gpu_model: applied.value.gpu_model || undefined,
    region: applied.value.region || undefined,
    zone: applied.value.zone || undefined,
    instance_type: applied.value.instance_type || undefined,
  } : {}),
  limit: pageSize, offset: offset.value,
}) : null)
const list = useDashboardQuery(listPath, readAvailability)
const { data: overview, fault: overviewError, loading: overviewLoading } = list
const models = computed(() => [...new Set([...catalog.value.data, ...(overview.value?.items ?? [])]
  .map(item => item.gpu_model))].sort())
const selectedKey = computed(() => props.selected || overview.value?.items[0]?.key || '')
const detailPath = computed(() => canQuery.value && selectedKey.value ? '/dashboard/availability' + query({
  ...scopedQuery.value, key: selectedKey.value, limit: 1,
}) : null)
const detailQuery = useDashboardQuery(detailPath, async (path, signal) => {
  try {
    return await readAvailability(path, signal)
  } catch (error) {
    if (error instanceof RequestError && error.status === 404) return { detail: null }
    throw error
  }
})
const { data: detailsResponse, fault: detailError, loading: detailLoading } = detailQuery
const detail = computed(() => detailsResponse.value?.detail ?? null)
const inventoryLanding = computed(() => '#inventory' + query({ project: props.projectId || undefined, tab: 'watch' }))
const providerTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})

function refresh() {
  void list.refresh()
  void detailQuery.refresh()
}

watch(() => filters.value.region, () => { filters.value.zone = '' }, { flush: 'sync' })
watch(() => props.cardThreshold, cards => {
  filters.value.card_threshold = cards
  offset.value = 0
})
watch([source, () => props.projectId], () => {
  focusResult = false
  offset.value = 0
  filters.value = { ...emptyFilters(), card_threshold: props.cardThreshold }
  applied.value = emptyFilters()
})
watch(detail, async value => {
  if (!value || !focusResult) return
  focusResult = false
  await nextTick()
  heading.value?.focus()
})

function applyFilters() {
  const { card_threshold: cards, ...draft } = filters.value
  const next = { ...draft, gpu_model: draft.gpu_model.trim() }
  const previous = applied.value
  const cardsChanged = cards !== props.cardThreshold
  if (cardsChanged || next.gpu_model !== previous.gpu_model || next.region !== previous.region
    || next.zone !== previous.zone || next.instance_type !== previous.instance_type) {
    offset.value = 0
    if (cardsChanged || props.selected) emit('select', source.value, undefined, cards)
  }
  applied.value = next
}

function resetFilters() {
  filters.value = { ...emptyFilters(), card_threshold: 1 }
  applyFilters()
}

function selectItem(item: AvailabilityItem) {
  if (item.key === selectedKey.value && detail.value) {
    heading.value?.focus()
    return
  }
  focusResult = true
  emit('select', source.value, item.key)
}

function changePage(position: number) {
  offset.value = Math.max(0, position)
  if (props.selected) emit('select', source.value)
}

function stockState(item: AvailabilityItem) {
  if (item.binding_status === 'unlisted') return '待重新绑定'
  if (item.binding_status === 'duplicate') return '重复关注'
  if (item.state === 'known') return valueLabel(item.remaining_cards ?? null, ' 张')
  if (item.state === 'stale') return '已过期'
  return item.state === 'unsupported' ? '不支持' : '平台未返回余量'
}

function capacityLabel(item: AvailabilityItem) {
  if (item.binding_status && item.binding_status !== 'bound') return ''
  const prefix = isPool.value ? cardSpecLabels[props.cardThreshold] : '规格'
  return prefix + (item.available === true ? '可租' : item.available === false ? '暂不可租' : '待核对')
}

function capacityTime(item: AvailabilityItem) {
  return item.capacity_checked_at == null ? '' : '探测 ' + providerTime.format(item.capacity_checked_at * 1000)
}

function stockTime(item: AvailabilityItem) {
  return item.provider_updated_at == null ? '' : '平台 ' + providerTime.format(item.provider_updated_at * 1000)
}

function itemType(item: AvailabilityItem) {
  return item.instance_type === 'spot' ? '抢占池' : '独占池'
}

function specKey(item: Pick<CatalogRow, 'region' | 'zone' | 'gpu_model'> & { instance_type?: string | null }) {
  return JSON.stringify([item.region, item.zone, item.gpu_model, item.instance_type ?? 'uhost'])
}

function inventoryLink(item: AvailabilityItem) {
  return '#inventory' + query({
    project: props.projectId || undefined,
    ...(item.watch_id ? { watch: item.watch_id } : {
      item: JSON.stringify([item.region, item.zone, item.gpu_model]),
      spot: item.instance_type === 'spot' ? '1' : undefined,
    }),
  })
}
</script>

<template>
  <div class="availability-layout">
    <section class="panel" aria-labelledby="dashboard-availability-heading">
      <div class="section-heading">
        <div>
          <h2 id="dashboard-availability-heading">近七天库存</h2>
          <p class="muted">
            {{ isPool ? '显示目录里有此卡数规格的资源池。' : '按关注的 GPU、CPU 和内存组合查看探测结果。' }}
          </p>
        </div>
        <ElButton size="small" :loading="overviewLoading || detailLoading" :disabled="!canQuery" @click="refresh">刷新</ElButton>
      </div>
      <div class="availability-source" role="group" aria-label="库存数据来源">
        <ElButton v-for="item in sources" :key="item.id" :type="source === item.id ? 'primary' : 'default'" size="small"
          :aria-pressed="source === item.id" @click="emit('select', item.id)">{{ item.label }}</ElButton>
        <a v-if="source === 'watch'" :href="inventoryLanding">管理规格关注</a>
      </div>
      <ElForm v-if="isPool" label-position="top" class="availability-filters" @submit.prevent="applyFilters">
        <ElFormItem label="GPU 型号" for="dashboard-filter-model">
          <ElSelect id="dashboard-filter-model" v-model="filters.gpu_model" aria-label="GPU 型号" placeholder="全部"
            filterable allow-create clearable value-on-clear="">
            <ElOption v-for="model in models" :key="model" :value="model" :label="model" />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="地域" for="dashboard-filter-region">
          <ElSelect id="dashboard-filter-region" v-model="filters.region" aria-label="地域" placeholder="全部" clearable value-on-clear="">
            <ElOption v-for="region in regions" :key="region" :value="region" :label="regionName(region)" />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="可用区" for="dashboard-filter-zone">
          <ElSelect id="dashboard-filter-zone" v-model="filters.zone" aria-label="可用区" placeholder="全部" clearable value-on-clear="">
            <ElOption v-for="zone in zones" :key="zone.zone" :value="zone.zone" :label="zoneName(zone, zone.zone_name)" />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="资源池" for="dashboard-filter-type">
          <ElSelect id="dashboard-filter-type" v-model="filters.instance_type" aria-label="资源池" placeholder="全部" clearable value-on-clear="">
            <ElOption label="独占池" value="uhost" />
            <ElOption label="抢占池" value="spot" />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="所需卡数" for="dashboard-filter-card-threshold">
          <ElSelect id="dashboard-filter-card-threshold" v-model="filters.card_threshold" aria-label="所需卡数">
            <ElOption v-for="count in cardThresholds" :key="count" :value="count" :label="count + ' 张'" />
          </ElSelect>
        </ElFormItem>
        <div class="filter-actions">
          <ElButton native-type="submit" :loading="overviewLoading">筛选</ElButton>
          <ElButton text type="primary" @click="resetFilters">清除</ElButton>
        </div>
      </ElForm>
      <ElAlert v-if="overviewError" title="库存列表读取失败" :description="overviewError" show-icon :closable="false"
        :type="overview ? 'warning' : 'error'" />
      <ElSkeleton v-if="overviewLoading && !overview" :rows="4" animated aria-label="正在读取库存历史" />
      <ElEmpty v-else-if="!canQuery" description="选择项目后可查看关注规格。" :image-size="64" />
      <template v-else-if="overview?.items.length">
        <div class="data-table-scroll availability-table-scroll" tabindex="0" role="region" aria-label="库存对比">
          <table class="availability-table" aria-label="库存规格对比">
            <thead>
              <tr>
                <th scope="col">{{ isPool ? 'GPU 型号' : '关注规格' }}</th><th scope="col">可用区</th>
                <th scope="col">剩余卡数<MetricHelp label="剩余卡数" :content="remainingCardsHelp" /></th>
                <th scope="col">{{ stockRateName }}<MetricHelp :label="stockRateName" :content="rateHelp" /></th>
                <th scope="col">{{ stockCoverageName }}<MetricHelp :label="stockCoverageName" :content="coverageHelp" /></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in overview.items" :key="item.key" :class="{ 'selected-row': item.key === selectedKey }"
                @click="selectItem(item)">
                <th scope="row">
                  <ElButton link type="primary" :aria-pressed="item.key === selectedKey">
                    {{ isPool ? item.gpu_model : item.name }}
                  </ElButton>
                  <small class="muted">{{ item.spec ? item.gpu_model + ' · ' + item.spec.gpu + ' 卡 · ' : '' }}{{ itemType(item) }}</small>
                </th>
                <td>{{ zoneName(item) }}<small class="muted">{{ regionName(item.region) }}</small></td>
                <td>
                  <StockRemaining :count="item.state === 'known' ? item.remaining_cards : null"
                    :reference="item.reference" :fallback="stockState(item)" hide-reference-note />
                  <small v-if="stockTime(item)" class="muted">{{ stockTime(item) }}</small>
                </td>
                <td>{{ rateLabel(stockRate(item)) }}<small class="muted">{{ item.available_count }} / {{ item.known_count }} 次探测</small>
                  <small v-if="capacityLabel(item)" :title="item.capacity_error ?? undefined">{{ capacityLabel(item) }}</small>
                </td>
                <td>{{ rateLabel(coverageRate(item)) }}<small class="muted">{{ item.known_count }} / {{ item.expected_count }} 次计划探测</small></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="overview.total > pageSize" class="stock-page-controls" aria-label="库存分页">
          <ElButton size="small" :disabled="offset === 0" @click="changePage(offset - pageSize)">上一页</ElButton>
          <span class="muted">{{ offset + 1 }}–{{ Math.min(offset + overview.items.length, overview.total) }} / {{ overview.total }}</span>
          <ElButton size="small" :disabled="offset + pageSize >= overview.total" @click="changePage(offset + pageSize)">下一页</ElButton>
        </div>
      </template>
      <ElEmpty v-else-if="overview" :description="isPool ? '目录中没有符合当前筛选条件的规格。' : '当前项目暂无关注规格。'" :image-size="64" />
    </section>

    <ElAlert v-if="detailError" title="库存详情读取失败" :description="detailError" show-icon :closable="false"
      :type="detail ? 'warning' : 'error'" />
    <section v-if="detailLoading && !detail" class="panel">
      <ElSkeleton :rows="4" animated aria-label="正在读取所选规格的库存明细" />
    </section>
    <section v-else-if="detailsResponse && selectedKey && !detail" class="panel">
      <ElEmpty description="所选规格已找不到，请从库存列表重新选择。" :image-size="64" />
    </section>
    <template v-if="detail">
      <section class="panel stock-focus metric-container" aria-labelledby="dashboard-stock-focus">
        <div class="section-heading">
          <div>
            <h2 id="dashboard-stock-focus" ref="heading" tabindex="-1">
              {{ isPool ? detail.item.gpu_model + ' · ' + zoneName(detail.item) : detail.item.name }}
            </h2>
            <p class="muted">
              {{ detail.item.spec ? [detail.item.gpu_model, zoneName(detail.item), specLabel(detail.item.spec)].join(' · ') : itemType(detail.item) }}
            </p>
          </div>
          <a v-if="!isPool || catalogSpecs.has(specKey(detail.item))" :href="inventoryLink(detail.item)">
            {{ isPool ? '核对完整规格' : '去 GPU 库存' }}
          </a>
        </div>
        <dl class="focus-metrics metric-grid metric-grid--three">
          <div>
            <dt>剩余卡数<MetricHelp label="剩余卡数" :content="remainingCardsHelp" /></dt>
            <dd>
              <StockRemaining :count="detail.item.state === 'known' ? detail.item.remaining_cards : null"
                :reference="detail.item.reference" :fallback="stockState(detail.item)" hide-reference-note />
              <small v-if="stockTime(detail.item)" class="muted">{{ stockTime(detail.item) }}</small>
            </dd>
          </div>
          <div>
            <dt>{{ stockRateName }}<MetricHelp :label="stockRateName" :content="rateHelp" /></dt>
            <dd>{{ rateLabel(stockRate(detail.item)) }}<small class="muted">{{ detail.item.available_count }} / {{ detail.item.known_count }} 次探测</small>
              <small v-if="capacityLabel(detail.item)" :title="detail.item.capacity_error ?? undefined">{{ capacityLabel(detail.item) }}</small>
              <small v-if="capacityTime(detail.item)" class="muted">{{ capacityTime(detail.item) }}</small>
            </dd>
          </div>
          <div>
            <dt>{{ stockCoverageName }}<MetricHelp :label="stockCoverageName" :content="coverageHelp" /></dt>
            <dd>{{ rateLabel(coverageRate(detail.item)) }}<small class="muted">{{ detail.item.known_count }} / {{ detail.item.expected_count }} 次计划探测</small></dd>
          </div>
        </dl>
      </section>

      <section class="panel" aria-labelledby="dashboard-stock-recommendations">
        <h2 id="dashboard-stock-recommendations">时段建议</h2>
        <p v-if="detail.recommendation_status === 'insufficient'" class="muted">
          样本不足。一个小时至少要有 3 天的记录，每天至少 8 个已知样本。
        </p>
        <p v-else-if="detail.recommendation_status === 'stable'" class="muted">各小时的样本表现接近，暂无明显时段差异。</p>
        <template v-else>
          <p class="muted">按北京时间比较七天内同一小时的规格可租率。</p>
          <div class="recommendations">
            <article>
              <h3>较易租到</h3>
              <p v-if="!detail.recommended.length" class="muted">暂无达到 70% 的时段。</p>
              <ul v-else>
                <li v-for="item in detail.recommended" :key="item.hour">
                  <strong>{{ hourLabel(item.hour) }}–{{ hourLabel(item.hour + 1) }}</strong>
                  <span>{{ rateLabel(item.rate) }} · {{ item.days }} 天</span>
                </li>
              </ul>
            </article>
            <article>
              <h3>较难租到</h3>
              <p v-if="!detail.tight.length" class="muted">暂无降至 30% 的时段。</p>
              <ul v-else>
                <li v-for="item in detail.tight" :key="item.hour">
                  <strong>{{ hourLabel(item.hour) }}–{{ hourLabel(item.hour + 1) }}</strong>
                  <span>{{ rateLabel(item.rate) }} · {{ item.days }} 天</span>
                </li>
              </ul>
            </article>
          </div>
        </template>
      </section>

      <section class="panel" aria-labelledby="dashboard-stock-trend">
        <h2 id="dashboard-stock-trend">{{ isPool ? '逐小时余量' : stockRateName + '热力图' }}</h2>
        <p class="muted">{{ isPool ? '折线显示每小时卡数中位数，缺少数据的时段留空。' : '过去 168 小时。灰色表示缺少已知样本，空白表示时段在统计范围之外。' }}</p>
        <DashboardAvailabilityChart :key="detail.item.key" :hours="detail.hours"
          :mode="isPool ? 'pool-line' : 'watch-heatmap'" />
      </section>

      <section v-if="isPool" class="panel" aria-labelledby="dashboard-stock-heatmap">
        <h2 id="dashboard-stock-heatmap">余量热力图</h2>
        <p class="muted">按北京时间查看每天各小时的余量中位数。灰色表示缺少样本，空白表示超出统计范围。</p>
        <DashboardAvailabilityChart :key="detail.item.key" :hours="detail.hours" mode="pool-heatmap" />
      </section>

      <DashboardReminder v-if="detail.reminder && detail.item.watch_id && detail.item.binding_status === 'bound'"
        :key="projectId + ':' + detail.item.watch_id"
        :project-id="projectId" :watch-id="detail.item.watch_id" :value="detail.reminder" :settings="settings"
        @saved="detailQuery.reload" />
    </template>
  </div>
</template>

<style scoped>
.availability-layout { display: grid; gap: 12px; min-width: 0; }
.availability-source, .filter-actions, .stock-page-controls { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.availability-source .el-button + .el-button, .filter-actions .el-button + .el-button { margin: 0; }
.availability-source a { margin-inline-start: auto; }
.availability-filters { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)) auto; align-items: end; gap: .6rem; }
.availability-filters .el-form-item { margin: 0; min-width: 0; }
.filter-actions { margin-bottom: 0; }
.availability-table-scroll { --table-min-width: 540px; }
.availability-table { width: 100%; border-collapse: collapse; text-align: left; }
.availability-table th, .availability-table td { border-bottom: 1px solid var(--line); padding: .55rem .45rem; overflow-wrap: anywhere; }
.availability-table thead { color: var(--muted); }
.availability-table tbody tr { cursor: pointer; }
.availability-table tbody tr:not(.selected-row):hover { background: var(--el-fill-color-light); }
.availability-table tbody th { font-weight: 600; }
.availability-table small { margin-top: .23rem; }
.availability-table .el-button { white-space: normal; height: auto; min-height: 1.7rem; text-align: left; }
.availability-table .selected-row { background: #eff7f3; }
.stock-page-controls { justify-content: flex-end; font-size: .86rem; }
.stock-page-controls .el-button + .el-button { margin: 0; }
.stock-focus .section-heading a { flex-shrink: 0; }
.focus-metrics { gap: .6rem; margin: .9rem 0 0; }
.focus-metrics div, .recommendations article { padding: .62rem; border: 1px solid var(--line); border-radius: .35rem; background: #f7faf9; }
.focus-metrics dt { color: var(--muted); font-size: .84rem; }
.focus-metrics dd { font-size: 1.12rem; font-weight: 680; margin: .2rem 0 0; overflow-wrap: anywhere; }
.focus-metrics small { margin-top: .2rem; font-weight: 400; }
.recommendations { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .6rem; }
.recommendations article:first-child { border-left: 3px solid #19786e; }
.recommendations article:last-child { border-left: 3px solid #b46b35; }
.recommendations article p { margin-top: .45rem; }
.recommendations ul { padding: 0; margin: .45rem 0 0; list-style: none; }
.recommendations li { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 4px 9px; margin: .3rem 0; }
.recommendations li span { color: var(--muted); font-size: .86rem; }
@media (max-width: 930px) { .availability-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 440px) {
  .focus-metrics { gap: 4px; }
  .focus-metrics div { padding: .4rem; }
  .focus-metrics dd { font-size: 1rem; }
  .focus-metrics small { font-size: .7rem; }
}
</style>
