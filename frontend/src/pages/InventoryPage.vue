<script setup lang="ts">
import { computed, nextTick, onScopeDispose, reactive, ref, shallowRef, watch } from 'vue'
import { query, request, type InstanceStorageSelection, type Spec, type TaskConfig, type Template, type Watch, type WatchSpec } from '../api/client'
import { cardThresholds } from '../api/dashboard'
import { queueCollection, type CollectionRequest } from '../api/refresh'
import { groupInventory, type InventoryEntry, type InventoryGroup } from '../api/inventoryOverview'
import { errorMessage, timeLabel, useSnapshot } from '../display'
import SpecFields from '../components/SpecFields.vue'
import InstanceStorageFields from '../components/InstanceStorageFields.vue'
import InventoryPriceDialog from '../components/InventoryPriceDialog.vue'
import InventoryEntryList from '../components/InventoryEntryList.vue'
import StockRemaining from '../components/StockRemaining.vue'
import { inventoryPriceSources, inventoryRangeLabel } from '../components/inventoryPriceFormat'
import { moneyLabel } from '../components/dashboard/format'
import { comboKey, specLabel, useLocationNames, zoneKey, type CatalogRow, type CatalogSpec, type PoolStockState } from '../catalog'

interface StockSample {
  sampled_at: number
  available: boolean | null
  error: string | null
}

interface Stock extends Watch {
  state: PoolStockState
  remaining_cards: number | null
  provider_updated_at: number | null
  capacity_checked_at: number | null
  available: boolean | null
  success_at: number | null
  error: string | null
  history: StockSample[]
}

interface CatalogFilter {
  gpuModel: string
  region: string
  zone: string
  instanceType: string
}

const props = defineProps<{
  projectId: string; watchId?: string; watchSpec?: string; showWatches?: boolean
  catalogFilter?: CatalogFilter; detailKey?: string; spotLink?: boolean
}>()
const { zoneName, regionName } = useLocationNames()
const { state: catalog, loading: loadingCatalog, fault: catalogError, refresh: refreshCatalog } =
  useSnapshot<CatalogRow>(computed(() => '/catalog' + query({ project_id: props.projectId || undefined })), 30_000)
const initialCatalog = catalog.value
const { state: stocks, loading: loadingStock, fault: stockError, refresh: refreshStock } =
  useSnapshot<Stock>(computed(() => props.projectId ? '/stock' + query({ project_id: props.projectId }) : null), 30_000)
const showSpot = ref(Boolean(props.spotLink || props.catalogFilter?.instanceType === 'spot'))
watch(() => props.spotLink, enabled => { if (enabled) showSpot.value = true })
const { state: poolInventory, loading: loadingPools, fault: poolError, refresh: refreshPools } =
  useSnapshot<InventoryEntry>(computed(() => '/inventory/overview' + query({
    project_id: props.projectId || undefined, include_spot: showSpot.value ? 'true' : undefined,
  })), 30_000)
const tab = ref('pool')
const batchSize = 40
const catalogLimit = ref(batchSize)
const poolLimit = ref(batchSize)

function emptyLocationFilters() {
  return { gpuModel: '', region: '', zone: '' }
}

const poolFilters = reactive(emptyLocationFilters())
const poolGroups = computed(() => groupInventory(poolInventory.value.data.filter(row => showSpot.value || row.pool !== 'spot')))
const detailGroup = computed(() => poolGroups.value.find(row => row.key === props.detailKey))
const poolRegions = computed(() => [...new Set(poolGroups.value.map(row => row.region))].sort())
const poolModels = computed(() => [...new Set(poolGroups.value.map(row => row.gpu_model))].sort())
const poolZones = computed(() => [...new Map(poolGroups.value.filter(row => !poolFilters.region || row.region === poolFilters.region)
  .map(row => [zoneKey(row), row])).values()].sort((left, right) => left.zone.localeCompare(right.zone)))
const hasPoolFilters = computed(() => Object.values(poolFilters).some(Boolean))
const visiblePools = computed(() => poolGroups.value.filter(row => (!poolFilters.region || row.region === poolFilters.region)
  && (!poolFilters.zone || row.zone === poolFilters.zone) && (!poolFilters.gpuModel || row.gpu_model === poolFilters.gpuModel)))
const catalogByPool = computed(() => new Map(catalog.value.data.filter(row => row.specs?.length)
  .map(row => [catalogKey(row), row])))
const priceOpen = ref(false)
const priceTarget = shallowRef<CatalogRow>()
const priceSpecId = ref<string>()
watch(priceOpen, visible => { if (!visible) void refreshPools() })
watch(() => poolFilters.region, () => { poolFilters.zone = '' }, { flush: 'sync' })
watch(poolFilters, () => { poolLimit.value = batchSize })

const providerTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  second: '2-digit', hourCycle: 'h23',
})

function poolLabel(stock: Pick<CatalogSpec, 'state' | 'remaining_cards'> | undefined) {
  const count = stock?.remaining_cards
  if (stock?.state === 'known' && typeof count === 'number' && Number.isSafeInteger(count) && count >= 0) {
    return `${count} 张`
  }
  if (stock?.state === 'stale') return '已过期'
  if (stock?.state === 'unsupported') return '不支持'
  return '平台未返回余量'
}

function poolTimeLabel(stock: Pick<CatalogSpec, 'provider_updated_at'> | undefined) {
  const timestamp = new Date((stock?.provider_updated_at ?? NaN) * 1000)
  return Number.isFinite(timestamp.getTime()) ? providerTime.format(timestamp) : '—'
}

function inventoryHref(item?: string) {
  const filter = props.catalogFilter
  return '#inventory' + query({
    project: props.projectId || undefined, item, tab: tab.value === 'pool' ? undefined : tab.value,
    spot: showSpot.value ? '1' : undefined,
    gpu_model: filter?.gpuModel || undefined, region: filter?.region || undefined,
    zone: filter?.zone || undefined, instance_type: filter?.instanceType || undefined,
  })
}

const heading = ref<HTMLElement>()
let returnEntry: { project: string; tab: string; key: string; scroll: number } | undefined
function rememberEntry(key: string) {
  returnEntry = { project: props.projectId, tab: tab.value, key, scroll: window.scrollY }
}

watch(() => props.detailKey, async (key, previous) => {
  await nextTick()
  if (key) {
    window.scrollTo(0, 0)
    heading.value?.focus({ preventScroll: true })
  } else if (previous && returnEntry?.project === props.projectId) {
    const { tab, key, scroll } = returnEntry
    returnEntry = undefined
    requestAnimationFrame(() => {
      if (props.detailKey) return
      document.getElementById('inventory-' + tab + '-' + encodeURIComponent(key))?.focus({ preventScroll: true })
      window.scrollTo(0, scroll)
    })
  }
}, { immediate: true })

function inventoryTrend(entry: InventoryEntry) {
  const cards = cardThresholds.find(count => entry.card_counts.includes(count))
  if (cards === undefined) return undefined
  return '#dashboard' + query({
    project: props.projectId || undefined, tab: 'availability', source: 'pool',
    key: 'pool-' + JSON.stringify([entry.region, entry.zone, entry.gpu_model, entry.pool]),
    card_threshold: cards > 1 ? cards : undefined,
  })
}
const trendLinks = computed(() => Object.fromEntries(
  (detailGroup.value?.pools ?? []).map(entry => [entry.pool, inventoryTrend(entry)]),
))

function openPrice(row?: CatalogRow, specId?: string) {
  if (!row) return
  priceTarget.value = row
  priceSpecId.value = specId
  priceOpen.value = true
}

function emptyFilters() {
  return {
    ...emptyLocationFilters(), gpuCount: undefined as number | undefined,
    cpuCount: undefined as number | undefined, memoryGb: undefined as number | undefined,
    instanceType: '',
  }
}

const filters = reactive(emptyFilters())
const catalogRows = computed(() => poolInventory.value.data.filter(row => row.listed && (showSpot.value || row.pool !== 'spot')))
const zones = computed(() => [...new Map(catalogRows.value.filter(row => !filters.region || row.region === filters.region)
  .map(row => [row.zone, row])).values()].sort((left, right) => left.zone.localeCompare(right.zone)))
const regions = computed(() => [...new Set(catalogRows.value.map(row => row.region))].sort())
const models = computed(() => [...new Set(catalogRows.value.map(row => row.gpu_model))].sort())
const hasSpecFilter = computed(() => Boolean(filters.gpuCount || filters.cpuCount || filters.memoryGb))
const advancedCount = computed(() => [filters.gpuCount, filters.cpuCount, filters.memoryGb,
  filters.instanceType].filter(Boolean).length)
const hasFilters = computed(() => Object.values(filters).some(Boolean))
const catalogGroups = computed(() => groupInventory(catalogRows.value
  .filter(row => (!filters.region || row.region === filters.region)
    && (!filters.zone || row.zone === filters.zone)
    && (!filters.gpuModel || row.gpu_model === filters.gpuModel)
    && (!filters.instanceType || row.instance_type === filters.instanceType))
  .filter(row => !hasSpecFilter.value || row.specs.some(spec =>
    (!filters.gpuCount || spec.gpu === filters.gpuCount)
    && (!filters.cpuCount || spec.cpu === filters.cpuCount)
    && (!filters.memoryGb || spec.memory_gb === filters.memoryGb)))))

watch(() => filters.region, () => { filters.zone = '' }, { flush: 'sync' })
watch(filters, () => { catalogLimit.value = batchSize })
watch(showSpot, enabled => { if (!enabled && filters.instanceType === 'spot') filters.instanceType = '' })
function showCatalog(preset: CatalogFilter) {
  tab.value = 'catalog'
  if (preset.instanceType === 'spot') showSpot.value = true
  Object.assign(filters, emptyFilters(), preset)
}

watch([() => props.catalogFilter, () => props.watchId, () => props.watchSpec, () => props.showWatches, () => props.detailKey],
  ([preset, id, spec, watchTab, detail], previous) => {
  if (previous?.length && (detail || previous[4])) return
  if (id || spec || watchTab) tab.value = 'watch'
  else if (preset) showCatalog(preset)
  else tab.value = 'pool'
}, { immediate: true })

const stockLoaded = ref(false)
watch(stocks, () => { if (loadingStock.value) stockLoaded.value = true }, { flush: 'sync' })
const stockHeading = ref<HTMLElement>()
const notice = ref('')
const collectionFeedback = ref('')
const refreshError = ref('')
const refreshing = ref(false)
const routeError = ref('')
const editorOpen = ref(false)
const editorError = ref('')
const editing = ref<'watch' | 'template'>('watch')
const name = ref('')
const spec = ref<Spec>(blankSpec())
const submitting = ref(false)
const specFields = ref<InstanceType<typeof SpecFields>>()
const storageFields = ref<InstanceType<typeof InstanceStorageFields>>()
const templateStorage = ref<InstanceStorageSelection>({ disks: [] })
const deleteOpen = ref(false)
const deleteWatch = ref<Stock>()
const deleteError = ref('')
const deletePending = ref(false)
let deletedWatch = false
let projectRequests = new AbortController()
const linkedWatch = computed(() => stocks.value.data.find(item => item.id === props.watchId))
const visibleStocks = computed(() => props.watchId ? (linkedWatch.value ? [linkedWatch.value] : []) : stocks.value.data)
const allWatchesLink = computed(() => '#inventory' + query({ project: props.projectId || undefined, tab: 'watch' }))

watch(() => props.projectId, () => {
  projectRequests.abort()
  projectRequests = new AbortController()
  stockLoaded.value = false
  editorOpen.value = false
  priceOpen.value = false
  deleteOpen.value = false
  submitting.value = false
  deletePending.value = false
  refreshing.value = false
  notice.value = ''
  collectionFeedback.value = ''
  refreshError.value = ''
})
onScopeDispose(() => projectRequests.abort())

function catalogKey(row: Pick<CatalogRow, 'region' | 'zone' | 'gpu_model' | 'instance_type'>) {
  return JSON.stringify([row.region, row.zone, row.gpu_model, row.instance_type ?? 'uhost'])
}

const watchCandidates = computed(() => [...new Map(catalog.value.data.flatMap(row =>
  (row.specs ?? []).flatMap(candidate => candidate.catalog_spec_id ? [{
    row, candidate, id: candidate.catalog_spec_id,
    label: [row.gpu_model, zoneName(row, row.zone_name), row.instance_type === 'spot' ? '抢占池' : '独占池',
      specLabel(candidate)].join(' · '),
  }] : [])).map(item => [item.id, item])).values()])
const pickedWatch = ref('')
const rebindingWatch = ref<Stock>()
const watchChoice = computed(() => watchCandidates.value.find(item => item.id === pickedWatch.value))

function blankSpec(): Spec {
  return {
    region: filters.region, zone: filters.zone, gpu_model: filters.gpuModel,
    gpu: filters.gpuCount || 1, cpu: filters.cpuCount || 0, memory_gb: filters.memoryGb || 0,
    image_id: '', disks: [], charge_type: filters.instanceType === 'spot' ? 'Spot' : 'Postpay',
  }
}

function openEditor(mode: 'watch' | 'template', row?: CatalogRow, candidate?: CatalogSpec, original?: Stock) {
  if (!props.projectId) return
  editorError.value = ''
  notice.value = ''
  editing.value = mode
  spec.value = blankSpec()
  templateStorage.value = { disks: [] }
  pickedWatch.value = candidate?.catalog_spec_id ?? ''
  rebindingWatch.value = original
  name.value = original?.name ?? (row ? row.gpu_model + ' · ' + zoneName(row, row.zone_name) : '')
  if (row && mode === 'template') {
    const option = candidate ?? row.specs?.[0]
    Object.assign(spec.value, {
      gpu_model: row.gpu_model, region: row.region, zone: row.zone,
      charge_type: row.instance_type === 'spot' ? 'Spot' : 'Postpay',
      ...(option ? { gpu: option.gpu, cpu: option.cpu, memory_gb: option.memory_gb } : {}),
    })
  }
  editorOpen.value = true
}

function specificationFromLink(value: string): WatchSpec | undefined {
  if (value.length > 4096) return
  try {
    const input = JSON.parse(value)
    if (!input || typeof input !== 'object' || !['Postpay', 'Day', 'Month', 'Spot'].includes(input.charge_type)) return
    const fields = ['region', 'zone', 'gpu_model'] as const
    if (!fields.every(field => typeof input[field] === 'string' && input[field].trim())) return
    if (![input.gpu, input.cpu, input.memory_gb].every(count => Number.isSafeInteger(count) && count > 0)) return
    return input
  } catch {
    return undefined
  }
}

let handledWatchLink = ''
watch(() => [props.projectId, props.watchId, props.watchSpec, watchCandidates.value] as const,
  ([projectId, watchId, link, options]) => {
    if (!projectId || watchId || !link) {
      handledWatchLink = ''
      routeError.value = ''
      return
    }
    if (catalog.value === initialCatalog) return
    const requestKey = projectId + ':' + link
    if (handledWatchLink === requestKey) return
    handledWatchLink = requestKey
    const legacy = specificationFromLink(link)
    const match = legacy && options.find(({ row, candidate }) =>
      row.region === legacy.region && row.zone === legacy.zone && row.gpu_model === legacy.gpu_model
      && (row.instance_type ?? 'uhost') === (legacy.charge_type === 'Spot' ? 'spot' : 'uhost')
      && comboKey(candidate) === comboKey(legacy))
    if (match) openEditor('watch', match.row, match.candidate)
    else routeError.value = '在当前平台目录中找不到链接中的规格，请重新选择。'
  }, { immediate: true })

async function saveSpec() {
  if (!editorOpen.value || !props.projectId || submitting.value) return
  if (!name.value.trim()) {
    editorError.value = '请填写名称。'
    return
  }
  const specError = editing.value === 'watch'
    ? (watchChoice.value ? '' : '请选择平台目录中的规格。')
    : (specFields.value?.validate() || storageFields.value?.validate() || '')
  if (specError) {
    editorError.value = specError
    return
  }
  const signal = projectRequests.signal
  editorError.value = ''
  submitting.value = true
  const mode = editing.value
  const base = { project_id: props.projectId, name: name.value.trim() }
  try {
    if (mode === 'watch') {
      const original = rebindingWatch.value
      const path = '/watches' + (original ? '/' + encodeURIComponent(original.id) : '')
      const saved = await request<Watch>(path, { method: original ? 'PUT' : 'POST',
        body: { ...base, catalog_spec_id: pickedWatch.value, expected_version: original?.version }, signal })
      if (!signal.aborted) {
        const duplicate = original ? saved.id !== original.id : saved.created === false
        notice.value = duplicate ? '该项目已关注此规格。' : '关注项已保存。'
        void refreshStock()
      }
    } else {
      const { region, zone, ...fields } = spec.value
      const config: TaskConfig = {
        ...fields, zones: [{ region, zone }], zone_storage: [{ region, zone, ...templateStorage.value }], skip_if_match: true,
      }
      await request<Template>('/templates', { method: 'POST', body: { ...base, config }, signal })
    }
    if (!signal.aborted) {
      if (mode === 'template') notice.value = '模板已保存，可在任务中心选择。'
      editorOpen.value = false
    }
  } catch (error) {
    if (!signal.aborted) editorError.value = errorMessage(error)
  } finally {
    if (!signal.aborted) submitting.value = false
  }
}

async function refreshAll(stockOnly = false, watchId?: string) {
  if (refreshing.value) return
  const signal = projectRequests.signal
  refreshing.value = true
  notice.value = ''
  collectionFeedback.value = ''
  refreshError.value = ''
  try {
    const queued = () => { collectionFeedback.value = stockOnly ? '规格探测已排队。' : '库存同步已排队。' }
    const requests: { label: string; body: CollectionRequest }[] = stockOnly
      ? [{ label: '关注规格', body: { kind: 'stock', project_id: props.projectId, watch_id: watchId } }]
      : [{ label: '平台余量', body: { kind: 'pool' } }, { label: '机型目录', body: { kind: 'catalog' } }]
    const receipts = await Promise.allSettled(requests.map(({ body }) => queueCollection(body, signal, queued)))
    signal.throwIfAborted()
    const failures: string[] = []
    let finishedAt = 0
    for (const [index, receipt] of receipts.entries()) {
      const label = requests[index].label
      if (receipt.status === 'rejected') {
        failures.push(label + ' · ' + errorMessage(receipt.reason))
        continue
      }
      for (const outcome of receipt.value.outcomes) {
        finishedAt = Math.max(finishedAt, outcome.finished_at ?? 0)
        if (!outcome.error) continue
        const name = outcome.scope ? stocks.value.data.find(item => item.id === outcome.scope)?.name ?? outcome.scope : label
        failures.push(name + ' · ' + outcome.error)
      }
    }
    collectionFeedback.value = failures.length ? '' : (stockOnly ? '规格探测完成 · ' : '库存同步成功 · ') + timeLabel(finishedAt)
    refreshError.value = failures.join('；')
    await Promise.all([refreshPools(), refreshCatalog(), refreshStock()])
  } catch (error) {
    if (!signal.aborted) {
      collectionFeedback.value = ''
      refreshError.value = errorMessage(error)
    }
  } finally {
    if (!signal.aborted) refreshing.value = false
  }
}

function confirmDelete(item: Stock) {
  deleteError.value = ''
  deleteWatch.value = item
  deletedWatch = false
  deleteOpen.value = true
}

async function removeWatch() {
  if (!deleteOpen.value || !deleteWatch.value || deletePending.value) return
  const signal = projectRequests.signal
  deletePending.value = true
  deleteError.value = ''
  try {
    await request('/watches/' + encodeURIComponent(deleteWatch.value.id), { method: 'DELETE', signal })
    if (signal.aborted) return
    deletedWatch = true
    notice.value = '已取消关注。'
    deleteOpen.value = false
    void refreshStock()
  } catch (error) {
    if (!signal.aborted) deleteError.value = errorMessage(error)
  } finally {
    if (!signal.aborted) deletePending.value = false
  }
}

function afterDeleteDialog() {
  if (deletedWatch) stockHeading.value?.focus()
  deleteWatch.value = undefined
}

function stockLabel(available: boolean | null) {
  return available === true ? '可租' : available === false ? '暂不可租' : '待核对'
}

function stockTag(available: boolean | null) {
  return available === true ? 'success' : available === false ? 'info' : 'warning'
}
</script>

<template>
  <div class="page-heading">
    <h1>GPU 库存</h1>
    <ElButton :loading="refreshing" @click="refreshAll()">同步库存</ElButton>
  </div>
  <ElAlert v-if="notice" :title="notice" type="success" :closable="false" show-icon />
  <p v-if="collectionFeedback" role="status">{{ collectionFeedback }}</p>
  <ElAlert v-if="refreshError" title="库存同步失败" :description="refreshError" type="error" show-icon :closable="false" />
  <ElAlert v-if="routeError" :title="routeError" type="warning" show-icon :closable="false" />

  <section v-show="!detailKey" class="panel inventory-panel" aria-label="库存清单">
    <ElCheckbox v-model="showSpot" label="显示抢占式" />
    <ElTabs v-model="tab">
      <ElTabPane label="平台余量" name="pool" lazy>
        <div class="inventory-section">
          <div class="section-heading">
            <div>
              <h2>各可用区剩余 GPU</h2>
              <p class="muted">平台约每 5 分钟更新余量。租卡前可核对完整规格。</p>
            </div>
            <ElButton v-if="hasPoolFilters" text type="primary" @click="Object.assign(poolFilters, emptyLocationFilters())">清除筛选</ElButton>
          </div>
          <ElAlert v-if="poolError" title="部分或全部平台余量读取失败" type="warning" show-icon :closable="false">
            <p>{{ poolError }}</p>
            <ElButton text type="primary" :loading="loadingPools" @click="refreshPools">重新读取</ElButton>
          </ElAlert>
          <ElForm :model="poolFilters" label-position="top" class="inventory-filters" @submit.prevent>
            <ElFormItem label="GPU 型号">
              <ElSelect v-model="poolFilters.gpuModel" clearable value-on-clear="" placeholder="全部型号">
                <ElOption v-for="item in poolModels" :key="item" :label="item" :value="item" />
              </ElSelect>
            </ElFormItem>
            <ElFormItem label="地域">
              <ElSelect v-model="poolFilters.region" clearable value-on-clear="" placeholder="全部地域">
                <ElOption v-for="item in poolRegions" :key="item" :label="regionName(item)" :value="item" />
              </ElSelect>
            </ElFormItem>
            <ElFormItem label="可用区">
              <ElSelect v-model="poolFilters.zone" clearable value-on-clear="" placeholder="全部可用区">
                <ElOption v-for="item in poolZones" :key="zoneKey(item)" :label="zoneName(item)" :value="item.zone" />
              </ElSelect>
            </ElFormItem>
          </ElForm>
          <ElSkeleton v-if="loadingPools && !poolGroups.length && !poolError" animated :rows="3" />
          <InventoryEntryList v-else-if="visiblePools.length" :rows="visiblePools.slice(0, poolLimit)"
            list-id="inventory-pool" :project-id="projectId" :href="row => inventoryHref(row.key)"
            :stock-label="poolLabel" @open="rememberEntry" />
          <ElEmpty v-else-if="!poolError || poolInventory.success_at" :image-size="64"
            :description="hasPoolFilters ? '没有符合筛选条件的机型。' : poolInventory.success_at ? '平台暂未提供余量。' : '等待后台首次同步平台余量。'" />
          <ElButton v-if="visiblePools.length > poolLimit" class="inventory-show-more" @click="poolLimit += batchSize">
            显示更多（{{ visiblePools.length - poolLimit }}）
          </ElButton>
        </div>
      </ElTabPane>

      <ElTabPane label="规格关注" name="watch">
        <div class="inventory-section">
          <div class="section-heading">
            <div>
              <h2 ref="stockHeading" tabindex="-1">规格关注</h2>
              <p v-if="linkedWatch?.capacity_checked_at" class="muted">规格探测 {{ timeLabel(linkedWatch.capacity_checked_at) }}</p>
            </div>
            <ElButton v-if="!watchId" type="primary" :disabled="!projectId" @click="openEditor('watch')">添加关注</ElButton>
            <a v-else :href="allWatchesLink">查看全部关注</a>
          </div>
          <ElAlert v-if="stockError" title="关注规格读取失败"
            type="warning" show-icon :closable="false">
            <p>{{ stockError }}</p>
            <ElButton text type="primary" :loading="refreshing" @click="refreshAll(true)">重新同步</ElButton>
          </ElAlert>
          <ElSkeleton v-if="loadingStock && !stockLoaded && !stockError" animated :rows="3" />
          <ElTable v-else-if="visibleStocks.length" :data="visibleStocks" row-key="id" table-layout="auto" aria-label="关注的 GPU 规格"
            :scrollbar-tabindex="0"
            :expand-row-keys="watchId ? [watchId] : undefined">
            <!-- @vue-generic {Stock} -->
            <ElTableColumn type="expand" label="详情" width="44">
              <template #default="{ row }">
                <div class="inventory-expanded">
                  <h3>规格探测历史</h3>
                  <ElTable v-if="row.history.length" :data="row.history" row-key="sampled_at" size="small"
                    table-layout="auto" aria-label="规格探测历史" :scrollbar-tabindex="0">
                    <!-- @vue-generic {StockSample} -->
                    <ElTableColumn label="探测时间" width="130">
                      <template #default="{ row: entry }">{{ timeLabel(entry.sampled_at) }}</template>
                    </ElTableColumn>
                    <!-- @vue-generic {StockSample} -->
                    <ElTableColumn label="可租状态" min-width="90">
                      <template #default="{ row: entry }">{{ stockLabel(entry.available) }}</template>
                    </ElTableColumn>
                  </ElTable>
                  <p v-else class="muted">暂无规格探测历史。</p>
                  <ElButton v-if="row.binding_status === 'bound'" :loading="refreshing" @click="refreshAll(true, row.id)">探测此规格</ElButton>
                  <ElButton v-else @click="openEditor('watch', undefined, undefined, row)">重新绑定</ElButton>
                  <ElButton v-if="row.binding_status === 'bound'"
                    @click="openPrice(catalogByPool.get(catalogKey({ ...row.spec, instance_type: row.spec.charge_type === 'Spot' ? 'spot' : 'uhost' })),
                      row.catalog_spec_id)">查看价格</ElButton>
                  <a v-if="row.binding_status === 'duplicate' && row.primary_watch_id"
                    :href="'#inventory' + query({ project: projectId, watch: row.primary_watch_id })">查看已关注规格</a>
                  <ElButton type="danger" plain @click="confirmDelete(row)">取消关注</ElButton>
                </div>
              </template>
            </ElTableColumn>
            <!-- @vue-generic {Stock} -->
            <ElTableColumn label="关注规格" min-width="108">
              <template #default="{ row }">
                <strong>{{ row.name }}</strong>
                <small class="muted">{{ row.spec.gpu_model }} × {{ row.spec.gpu }} · {{ row.spec.cpu }} vCPU · {{ row.spec.memory_gb }} GB</small>
                <small class="muted">{{ zoneName(row.spec) }} · {{ row.spec.charge_type === 'Spot' ? '抢占池' : '独占池' }}</small>
              </template>
            </ElTableColumn>
            <!-- @vue-generic {Stock} -->
            <ElTableColumn label="平台池余量" min-width="120">
              <template #default="{ row }">
                <template v-if="row.binding_status === 'bound'">
                  <strong>{{ poolLabel(row) }}</strong>
                  <small v-if="row.provider_updated_at !== null" class="muted">{{ poolTimeLabel(row) }}</small>
                </template>
                <span v-else>{{ row.binding_status === 'duplicate' ? '重复关注' : '待重新绑定' }}</span>
              </template>
            </ElTableColumn>
            <!-- @vue-generic {Stock} -->
            <ElTableColumn label="规格可租状态" min-width="126">
              <template #default="{ row }">
                <template v-if="row.binding_status === 'bound'">
                  <ElTag :type="stockTag(row.available)" :title="row.error ?? undefined">{{ stockLabel(row.available) }}</ElTag>
                  <small v-if="row.capacity_checked_at" class="muted">{{ timeLabel(row.capacity_checked_at) }}</small>
                </template>
                <span v-else>—</span>
              </template>
            </ElTableColumn>
          </ElTable>
          <ElEmpty v-else-if="watchId" :image-size="64" description="此项目中找不到该关注项。" />
          <ElEmpty v-else-if="!stockError" :image-size="64"
            :description="projectId ? '暂无规格关注。' : '选择项目后可添加规格关注。'">
            <ElButton @click="tab = 'catalog'">浏览机型目录</ElButton>
          </ElEmpty>
        </div>
      </ElTabPane>

      <ElTabPane label="机型目录" name="catalog" lazy>
        <div class="inventory-section">
          <div class="section-heading">
            <div>
              <h2>机型目录</h2>
              <p class="muted">最近成功 {{ timeLabel(catalog.success_at) }}</p>
            </div>
            <ElButton v-if="hasFilters" text type="primary" @click="Object.assign(filters, emptyFilters())">清除筛选</ElButton>
          </div>
          <p v-if="!projectId" class="muted">选择项目后可关注规格或保存模板。</p>
          <ElAlert v-if="poolError" :title="poolError" type="warning" show-icon :closable="false" />
          <ElAlert v-if="catalogError" :title="catalog.data.length ? '目录刷新失败，显示上次成功的数据' : '机型目录读取失败'"
            type="warning" show-icon :closable="false">
            <p>{{ catalogError }}</p>
            <ElButton text type="primary" :loading="loadingCatalog" @click="refreshCatalog">重新读取</ElButton>
          </ElAlert>
          <ElForm :model="filters" label-position="top" @submit.prevent>
            <div class="inventory-filters">
              <ElFormItem label="GPU 型号">
                <ElSelect v-model="filters.gpuModel" clearable value-on-clear="" placeholder="全部型号">
                  <ElOption v-for="item in models" :key="item" :label="item" :value="item" />
                </ElSelect>
              </ElFormItem>
              <ElFormItem label="地域">
                <ElSelect v-model="filters.region" clearable value-on-clear="" placeholder="全部地域">
                  <ElOption v-for="item in regions" :key="item" :label="regionName(item)" :value="item" />
                </ElSelect>
              </ElFormItem>
              <ElFormItem label="可用区">
                <ElSelect v-model="filters.zone" clearable value-on-clear="" placeholder="全部可用区">
                  <ElOption v-for="item in zones" :key="item.zone" :label="zoneName(item, item.zone_name)" :value="item.zone" />
                </ElSelect>
              </ElFormItem>
            </div>
            <ElCollapse>
              <ElCollapseItem name="advanced" :title="advancedCount ? `高级筛选（${advancedCount} 项）` : '高级筛选'">
                <div class="inventory-filters">
                  <ElFormItem label="GPU 张数">
                    <ElInputNumber v-model="filters.gpuCount" :min="1" :precision="0" :controls="false" placeholder="任意张数" />
                  </ElFormItem>
                  <ElFormItem label="CPU 核数">
                    <ElInputNumber v-model="filters.cpuCount" :min="1" :precision="0" :controls="false" placeholder="任意核数" />
                  </ElFormItem>
                  <ElFormItem label="内存（GB）">
                    <ElInputNumber v-model="filters.memoryGb" :min="1" :precision="0" :controls="false" placeholder="任意容量" />
                  </ElFormItem>
                  <ElFormItem label="实例类型">
                    <ElSelect v-model="filters.instanceType" clearable value-on-clear="" placeholder="全部类型">
                      <ElOption label="普通" value="uhost" />
                      <ElOption v-if="showSpot" label="抢占式" value="spot" />
                    </ElSelect>
                  </ElFormItem>
                </div>
              </ElCollapseItem>
            </ElCollapse>
          </ElForm>
          <ElSkeleton v-if="(loadingCatalog || loadingPools) && !catalogGroups.length && !catalogError && !poolError" animated :rows="3" />
          <InventoryEntryList v-else-if="catalogGroups.length" :rows="catalogGroups.slice(0, catalogLimit)"
            list-id="inventory-catalog" :project-id="projectId" :href="row => inventoryHref(row.key)"
            :stock-label="poolLabel" @open="rememberEntry" />
          <ElEmpty v-else-if="!catalogError || catalog.success_at" :image-size="64"
            :description="!catalog.success_at ? '等待后台首次同步机型目录。' : hasFilters ? '没有符合筛选条件的机型。' : '机型目录暂无数据。'" />
          <ElButton v-if="catalogGroups.length > catalogLimit" class="inventory-show-more" @click="catalogLimit += batchSize">
            显示更多（{{ catalogGroups.length - catalogLimit }}）
          </ElButton>
        </div>
      </ElTabPane>
    </ElTabs>
  </section>

  <section v-if="detailKey" class="panel inventory-detail" aria-label="库存详情">
    <a :href="inventoryHref()">返回库存列表</a>
    <h2 ref="heading" tabindex="-1">
      {{ detailGroup ? detailGroup.gpu_model + ' · ' + zoneName(detailGroup, detailGroup.zone_name) : '库存详情' }}
    </h2>
    <ElAlert v-if="poolError" :title="poolError" type="warning" :closable="false" show-icon />
    <ElSkeleton v-if="loadingPools && !detailGroup" animated :rows="3" />
    <ElEmpty v-else-if="!detailGroup && !poolError" description="当前目录中找不到该机型与可用区。" />
    <template v-if="detailGroup">
      <p v-if="detailGroup.graphics_memory_gb !== null" class="muted">单卡显存 {{ detailGroup.graphics_memory_gb }} GB</p>
      <section v-for="entry in detailGroup.pools" :key="entry.pool" class="detail-pool">
        <div class="section-heading">
          <div>
            <h3>{{ entry.pool === 'spot' ? '抢占池' : '普通池' }}</h3>
            <StockRemaining :count="entry.state === 'known' ? entry.remaining_cards : null"
              :reference="entry.reference" :fallback="poolLabel(entry)" />
            <p v-if="entry.provider_updated_at" class="muted">库存更新 {{ poolTimeLabel(entry) }}</p>
          </div>
          <a v-if="trendLinks[entry.pool]" :href="trendLinks[entry.pool]">查看库存趋势</a>
        </div>
        <p v-if="entry.specs.length">
          <strong>{{ entry.specs.length }} 种规格</strong> · {{ inventoryRangeLabel(entry.hourly_range, projectId) }}
          <small v-if="entry.hourly_range.source" class="muted">
            · {{ inventoryPriceSources[entry.hourly_range.source] }}<template v-if="entry.hourly_range.stale"> · 含上次报价</template>
          </small>
        </p>
        <p v-if="entry.specs.length" class="muted">价格为算力费，磁盘和镜像费用另计。</p>
        <ul v-if="entry.specs.length" class="detail-specs">
          <li v-for="candidate in entry.specs" :key="candidate.catalog_spec_id">
            <div>
              <strong>{{ specLabel(candidate) }}</strong>
              <small class="muted" :title="candidate.capacity_error ?? undefined">{{ stockLabel(candidate.available ?? null) }}<template v-if="candidate.capacity_checked_at">
                · 探测于 {{ timeLabel(candidate.capacity_checked_at) }}
              </template></small>
            </div>
            <div>
              <strong>{{ candidate.hourly_rate ? moneyLabel(candidate.hourly_rate.amount) + '/实例小时'
                : candidate.quote_failed ? '暂未取到报价' : '暂无报价' }}</strong>
              <small v-if="candidate.hourly_rate" class="muted">
                {{ candidate.hourly_rate.stale ? '上次' : '' }}{{ inventoryPriceSources[candidate.hourly_rate.source] }}
                · {{ timeLabel(candidate.hourly_rate.observed_at) }}
              </small>
            </div>
            <div class="inventory-actions">
              <ElButton size="small" :disabled="!projectId" @click="openPrice(entry, candidate.catalog_spec_id)">
                {{ entry.pool === 'spot' ? '查看价格' : '查看各计费方式' }}
              </ElButton>
              <ElButton size="small" :disabled="!projectId" @click="openEditor('watch', entry, candidate)">关注规格</ElButton>
              <ElButton size="small" :disabled="!projectId" @click="openEditor('template', entry, candidate)">存为模板</ElButton>
            </div>
          </li>
        </ul>
        <p v-else class="muted">暂无目录规格。</p>
      </section>
    </template>
  </section>

  <InventoryPriceDialog v-if="priceTarget" v-model="priceOpen" :project-id="projectId" :target="priceTarget"
    :initial-spec-id="priceSpecId" />

  <ElDialog v-model="editorOpen" :title="editing === 'watch' ? '添加规格关注' : '从规格创建模板'"
    width="min(44rem, calc(100% - 24px))" top="5vh" destroy-on-close append-to-body :close-on-click-modal="false"
    :show-close="!submitting" :close-on-press-escape="!submitting">
    <ElForm class="inventory-editor" label-position="top" :disabled="submitting" @submit.prevent="saveSpec">
      <ElFormItem label="名称" required>
        <ElInput v-model.trim="name" maxlength="120" required />
      </ElFormItem>
      <ElFormItem v-if="editing === 'watch'" label="平台规格" required>
        <ElSelect v-model="pickedWatch" filterable placeholder="搜索 GPU 型号、可用区或卡数">
          <ElOption v-for="item in watchCandidates" :key="item.id" :value="item.id" :label="item.label" />
        </ElSelect>
        <small v-if="watchChoice" class="muted">平台池余量 {{ poolLabel(watchChoice.candidate) }}</small>
      </ElFormItem>
      <fieldset v-else class="inventory-spec-fields" :disabled="submitting">
        <SpecFields ref="specFields" v-model="spec" :project-id="projectId" :catalog="catalog.data" :catalog-error="catalogError"
          allow-spot require-quantity managed-storage />
        <InstanceStorageFields ref="storageFields" v-model="templateStorage" :project-id="projectId"
          :location="spec" :gpu-model="spec.gpu_model" :charge-type="spec.charge_type" :image-id="spec.image_id" :disabled="submitting" />
      </fieldset>
      <ElAlert v-if="editorError" title="保存失败" :description="editorError" type="error" show-icon :closable="false" />
      <div class="inventory-actions inventory-dialog-actions">
        <ElButton :disabled="submitting" @click="editorOpen = false">返回</ElButton>
        <ElButton type="primary" native-type="submit" :loading="submitting">保存{{ editing === 'watch' ? '关注' : '模板' }}</ElButton>
      </div>
    </ElForm>
  </ElDialog>

  <ElDialog v-model="deleteOpen" title="取消关注" width="min(28rem, calc(100% - 24px))" append-to-body
    destroy-on-close :close-on-click-modal="false" :show-close="!deletePending" :close-on-press-escape="!deletePending"
    @closed="afterDeleteDialog">
    <p>将停止发送「{{ deleteWatch?.name }}」的关注提醒。</p>
    <ElAlert v-if="deleteError" title="取消关注失败" :description="deleteError" type="error" show-icon :closable="false" />
    <template #footer>
      <div class="inventory-actions inventory-dialog-actions">
        <ElButton :disabled="deletePending" @click="deleteOpen = false">返回</ElButton>
        <ElButton type="danger" :loading="deletePending" @click="removeWatch">确认取消关注</ElButton>
      </div>
    </template>
  </ElDialog>
</template>

<style scoped>
.inventory-panel {
  min-width: 0;
  padding-block-start: 8px;
}
.inventory-section, .inventory-expanded, .inventory-editor {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 12px;
  min-width: 0;
}
.inventory-expanded {
  padding: 12px;
  white-space: normal;
}
.inventory-expanded > .el-button, .inventory-show-more { justify-self: start; }
.inventory-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.inventory-actions > .el-button { margin: 0; }
.inventory-dialog-actions { justify-content: flex-end; }
.inventory-filters {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 124px), 1fr));
  gap: 8px 16px;
  max-width: 55rem;
}
.inventory-filters > .el-form-item, .inventory-editor > .el-form-item { margin-bottom: 0; }
.inventory-filters .el-input-number { width: 100%; }
.inventory-panel .el-collapse { margin-top: 12px; }
.inventory-detail { container: inventory-detail / inline-size; }
.inventory-detail h2 { margin-bottom: 0; }
.detail-pool { border-top: 1px solid var(--line); padding-top: 12px; }
.detail-pool h3, .detail-pool .section-heading p { margin: 0; }
.detail-specs { list-style: none; padding: 0; margin: 0; }
.detail-specs > li {
  display: grid; grid-template-columns: minmax(0, 1fr); align-items: center; overflow-wrap: anywhere;
  gap: 10px 14px; border-top: 1px solid var(--line); padding: 12px 0;
}
.detail-specs small { display: block; }
@container inventory-detail (min-width: 30rem) {
  .detail-specs > li { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .detail-specs .inventory-actions { grid-column: 1 / -1; }
}
@container inventory-detail (min-width: 52rem) {
  .detail-specs > li { grid-template-columns: minmax(185px, 1fr) minmax(140px, 1fr) auto; }
  .detail-specs .inventory-actions { grid-column: auto; }
}
.inventory-spec-fields { min-width: 0; margin: 0; padding: 0; border: 0; }
.inventory-section :deep(.el-table) { width: 100%; min-width: 0; }
.inventory-section :deep(.el-table .cell) { overflow-wrap: anywhere; }
.inventory-section :deep(.el-table .cell:has(.el-table__expand-icon)) { padding-inline: 4px; }
.inventory-section :deep(.el-table__expand-icon) { width: 32px; height: 32px; }
.inventory-section :deep(.el-table__expanded-cell) { padding: 0; }
.inventory-section :deep(.el-alert__description p) { overflow-wrap: anywhere; }
@media (max-width: 390px) {
  .inventory-expanded { padding: 8px; }
}
</style>
