<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, onScopeDispose, ref } from 'vue'
import { Filter, Refresh, Search, TopRight } from '@element-plus/icons-vue'
import type { InputInstance } from 'element-plus'
import { isSafeInstanceLink, query, type Channel, type Instance, type Snapshot } from '../api/client'
import { queueCollection } from '../api/refresh'
import { useLocationNames } from '../catalog'
import { copyText } from '../clipboard'
import { chargeLabels, errorMessage, instanceLabels, timeLabel } from '../display'

const CreateInstanceDialog = defineAsyncComponent(() => import('../components/CreateInstanceDialog.vue'))
const InstanceAssetList = defineAsyncComponent(() => import('../components/InstanceAssetList.vue'))

const props = defineProps<{
  projectId: string
  tab: string
  projectName?: string
  snapshot: Snapshot<Instance>
  loading: boolean
  error: string | null
  sampleInterval?: number
  focusInstanceId?: string
  operationBlock: string
  channels?: Record<Channel, boolean>
}>()
const emit = defineEmits<{ refresh: []; showRun: [id: string]; selectTab: [tab: string] }>()
const search = defineModel<string>('search', { required: true })
const stateFilter = defineModel<string>('stateFilter', { required: true })
const { zoneName, regionName } = useLocationNames()
const creating = ref(false)

const heading = ref<HTMLElement>()
const searchField = ref<InputInstance>()
const filtersExpanded = ref(false)
const refreshMessage = ref('')
const refreshError = ref('')
const refreshPending = ref(false)
let refreshController: AbortController | undefined
const copyFeedback = ref<{ id: string; copied: boolean }>()
let copySequence = 0
let copyTimer: number | undefined
const now = ref(Date.now() / 1000)
const timer = window.setInterval(() => { now.value = Date.now() / 1000 }, 5000)
onScopeDispose(() => {
  window.clearInterval(timer)
  window.clearTimeout(copyTimer)
  copySequence++
  refreshController?.abort()
})

const readError = computed(() => props.error ?? props.snapshot.error)
const hasSnapshot = computed(() => Boolean(props.snapshot.success_at || props.snapshot.data.length))
const staleSnapshot = computed(() => Boolean(readError.value || !props.snapshot.success_at
  || props.snapshot.success_at + (props.sampleInterval ?? 60) * 2 < now.value))
const running = computed(() => props.snapshot.data.filter(instance => instance.state === 'Running'))
const stoppedCount = computed(() => props.snapshot.data.filter(instance => instance.state === 'Stopped').length)
const summary = computed(() => [
  { label: '实例总数', value: hasSnapshot.value ? props.snapshot.data.length : '—' },
  { label: '运行中', value: hasSnapshot.value ? running.value.length : '—' },
  { label: '已关机', value: hasSnapshot.value ? stoppedCount.value : '—' },
])
const states = computed(() => new Set(props.snapshot.data.map(instance => instance.state).filter(Boolean)))
const hasFilters = computed(() => Boolean(search.value.trim() || stateFilter.value))
const shownInstances = computed(() => {
  const needle = search.value.trim().toLocaleLowerCase()
  return props.snapshot.data.filter(instance =>
    (!stateFilter.value || instance.state === stateFilter.value)
    && (!needle || instance.id.toLocaleLowerCase().includes(needle) || instance.name?.toLocaleLowerCase().includes(needle)),
  ).map(instance => ({
    instance,
    status: instanceStatus(instance),
    connection: jupyterConnection(instance),
    schedule: scheduleLabel(instance),
  }))
})

function instanceStatus(instance: Instance): { label: string; type: 'info' | 'warning' | 'success'; title?: string } {
  if (instance.state === 'Running') return { label: '正在运行', type: 'success' }
  if (instance.state !== 'Stopped') return { label: instanceLabels[instance.state] || instance.state || '状态未知', type: 'info' }
  const operation = instance.active_operation
  if (operation) {
    if (operation.state === 'manual_review') return { label: '操作待核对', type: 'warning' }
    if (operation.kind === 'resize') return { label: '改配中', type: 'info' }
    if (operation.kind === 'terminate') return { label: '释放中', type: 'info' }
    if (operation.kind === 'start') return operation.capacity_wait
      ? { label: '等待资源', type: 'warning' } : { label: '启动中', type: 'info' }
    return { label: '操作处理中', type: 'info' }
  }
  const stock = instance.stock
  if (!stock) return { label: '已关机', type: 'info' }
  const pool = stock.pool === 'spot' ? '抢占池' : stock.pool === 'exclusive' ? '普通池' : '资源池'
  const title = '已关机 · ' + pool + '更新 ' + timeLabel(stock.provider_updated_at)
    + (stock.spec_checked_at ? ' · 规格探测 ' + timeLabel(stock.spec_checked_at) : '')
  if (staleSnapshot.value) return { label: '库存待同步', type: 'info', title }
  const count = stock.state === 'known' && stock.remaining_cards !== null ? ` · 池余 ${stock.remaining_cards} 张` : ''
  if (stock.spec_available === true) return { label: '有卡' + count, type: 'success', title }
  if (stock.spec_available === false) return { label: '暂无可用资源' + count, type: 'warning', title }
  if (stock.state === 'unsupported') return { label: '资源池不支持', type: 'info', title }
  return { label: '规格待核对' + count, type: 'info', title }
}

function jupyterConnection(instance: Instance) {
  if (instance.state !== 'Running') return { label: 'JupyterLab 待运行' }
  if (staleSnapshot.value) return { label: 'JupyterLab 待同步' }
  if (!isSafeInstanceLink(instance.jupyter?.url)) return { label: 'JupyterLab 未配置' }
  return { label: '打开 JupyterLab', href: instance.jupyter.url }
}

function scheduleLabel(instance: Instance) {
  if (instance.scheduler_status === 'unknown') return '关机预约待核对'
  if (instance.scheduler_stop_time) return '关机预约 ' + timeLabel(instance.scheduler_stop_time)
  if (instance.release_time) return '预计回收 ' + timeLabel(instance.release_time)
  if (instance.expire_time) return '预计到期 ' + timeLabel(instance.expire_time)
  return ''
}

function instanceLink(instance: Instance) {
  return '#instances' + query({ project: props.projectId, instance: instance.id })
}

function clearFilters() {
  search.value = ''
  stateFilter.value = ''
  searchField.value?.focus()
}

async function copySsh(instance: Instance) {
  if (!instance.ssh?.command) return
  const sequence = ++copySequence
  window.clearTimeout(copyTimer)
  copyFeedback.value = undefined
  const copied = await copyText(instance.ssh.command)
  if (sequence !== copySequence) return
  copyFeedback.value = { id: instance.id, copied }
  if (copied) copyTimer = window.setTimeout(() => { copyFeedback.value = undefined }, 3500)
}

function selectSshCommand(event: FocusEvent) {
  if (event.target instanceof HTMLInputElement) event.target.select()
}

async function requestRefresh() {
  if (refreshPending.value) return
  const controller = new AbortController()
  refreshController = controller
  refreshPending.value = true
  refreshMessage.value = ''
  refreshError.value = ''
  try {
    const receipt = await queueCollection({ kind: 'instances', project_id: props.projectId }, controller.signal, () => {
      refreshMessage.value = '当前项目同步已排队，等待后台读取。'
    })
    if (!controller.signal.aborted) {
      emit('refresh')
      const outcome = receipt.outcomes[0]
      refreshError.value = outcome?.error ?? ''
      refreshMessage.value = receipt.state === 'succeeded' ? '当前项目实例同步成功 · ' + timeLabel(outcome?.finished_at) : ''
    }
  } catch (error) {
    if (!controller.signal.aborted) {
      refreshMessage.value = ''
      refreshError.value = errorMessage(error)
    }
  } finally {
    refreshController = undefined
    refreshPending.value = false
  }
}

onMounted(() => {
  if (props.focusInstanceId) {
    const target = document.getElementById('instance-card-' + props.focusInstanceId) ?? heading.value
    target?.focus()
  }
})
</script>

<template>
  <section class="overview" aria-labelledby="overview-title">
    <header class="overview-heading">
      <h1 id="overview-title" ref="heading" tabindex="-1">实例总览</h1>
      <p v-if="tab === 'current'" class="overview-sync-time">最近同步 {{ timeLabel(snapshot.success_at) }}
        <span v-if="hasSnapshot && staleSnapshot"> · 数据待同步</span>
      </p>
      <div v-if="tab === 'current'" class="overview-heading-actions">
        <ElButton :icon="Refresh" :loading="refreshPending" @click="requestRefresh">同步实例</ElButton>
        <ElButton type="primary" :disabled="!!operationBlock" :title="operationBlock" @click="creating = true">创建实例</ElButton>
      </div>
    </header>

    <ElTabs :model-value="tab" @tab-change="name => emit('selectTab', String(name))">
      <ElTabPane label="当前实例" name="current">
        <div v-if="tab === 'current'" class="overview-current metric-container">
          <div v-if="readError" class="overview-read-error">
            <ElAlert :title="hasSnapshot ? '刷新失败，以下为上次同步结果' : '暂时无法读取实例'" :description="readError"
              :type="hasSnapshot ? 'warning' : 'error'" :closable="false" show-icon />
            <ElButton :loading="refreshPending" @click="requestRefresh">重新同步</ElButton>
          </div>
          <ElAlert v-if="refreshError" title="当前项目实例同步失败" :description="refreshError" type="error" :closable="false" show-icon />
          <p v-if="refreshMessage" class="overview-sync-feedback" role="status">{{ refreshMessage }}</p>

          <dl class="overview-summary metric-grid metric-grid--three" aria-label="实例数量">
            <div v-for="item in summary" :key="item.label">
              <dt>{{ item.label }}</dt>
              <dd :class="{ 'overview-summary-stale': item.value === '待同步' }">{{ item.value }}</dd>
            </div>
          </dl>

          <slot></slot>
          <section class="overview-list" aria-label="实例清单">
            <div class="overview-filters">
              <ElInput ref="searchField" v-model="search" class="overview-search" aria-label="搜索名称或实例 ID"
                placeholder="搜索名称或实例 ID" :prefix-icon="Search" clearable />
              <ElButton class="overview-filter-toggle" :icon="Filter" :aria-expanded="filtersExpanded"
                aria-controls="overview-state-filter" @click="filtersExpanded = !filtersExpanded">
                {{ stateFilter ? '筛选 · 1' : '筛选' }}
              </ElButton>
              <div id="overview-state-filter" class="overview-state-filter" :class="{ 'is-expanded': filtersExpanded }">
                <ElSelect v-model="stateFilter" aria-label="按实例状态筛选" :empty-values="[undefined, null]">
                  <ElOption label="全部状态" value="" />
                  <ElOption v-if="stateFilter && !states.has(stateFilter)" :label="instanceLabels[stateFilter] ?? stateFilter" :value="stateFilter" />
                  <ElOption v-for="state in states" :key="state" :label="instanceLabels[state] ?? state" :value="state" />
                </ElSelect>
              </div>
            </div>

            <ul v-if="shownInstances.length" class="overview-grid">
              <li v-for="{ instance, status, connection, schedule } in shownInstances" :key="instance.id">
                <ElCard class="overview-instance" shadow="never">
                  <div class="overview-instance-head">
                    <h3><a :id="'instance-card-' + instance.id" :href="instanceLink(instance)">{{ instance.name || instance.id }}</a></h3>
                    <ElTag :type="status.type" :title="status.title || status.label" effect="light" size="small" class="overview-instance-status">{{ status.label }}</ElTag>
                  </div>
                  <div class="overview-instance-gpu">
                    <strong>{{ instance.gpu_model || 'GPU 型号未知' }}<span v-if="instance.gpu != null"> × {{ instance.gpu }}</span></strong>
                    <span v-if="schedule" class="overview-instance-schedule">{{ schedule }}</span>
                  </div>
                  <div class="overview-connections">
                    <ElButton v-if="connection.href" tag="a" type="primary" :href="connection.href" target="_blank" rel="noopener noreferrer">
                      {{ connection.label }} <TopRight class="overview-external-icon" aria-hidden="true" />
                    </ElButton>
                    <ElButton v-else disabled>{{ connection.label }}</ElButton>
                    <ElButton :disabled="!instance.ssh?.command" @click="copySsh(instance)">
                      {{ instance.ssh?.command ? '复制 SSH 命令' : '暂无 SSH 命令' }}
                    </ElButton>
                  </div>
                  <p class="overview-copy-feedback" :class="{ 'has-copy-error': copyFeedback?.id === instance.id && !copyFeedback.copied }"
                    role="status">{{ copyFeedback?.id === instance.id
                    ? (copyFeedback.copied ? '已复制 SSH 命令。' : '复制失败，可手动选择命令复制。') : '' }}</p>
                  <ElInput v-if="copyFeedback?.id === instance.id && !copyFeedback.copied && instance.ssh?.command"
                    class="overview-manual-copy" :model-value="instance.ssh.command" aria-label="手动复制 SSH 命令" readonly @focus="selectSshCommand" />
                  <dl class="overview-instance-facts metric-grid">
                    <div v-if="instance.name && instance.name !== instance.id"><dt>实例 ID</dt><dd class="mono">{{ instance.id }}</dd></div>
                    <div><dt>计算规格</dt><dd>{{ instance.cpu ?? '未知' }} vCPU · {{ instance.memory_gb ?? '未知' }} GB</dd></div>
                    <div><dt>可用区</dt><dd :title="instance.zone">{{ zoneName(instance) || regionName(instance.region) || '未知' }}</dd></div>
                    <div><dt>计费</dt><dd>{{ chargeLabels[instance.charge_type ?? ''] ?? '未知' }}</dd></div>
                  </dl>
                </ElCard>
              </li>
            </ul>
            <div v-else-if="loading && !hasSnapshot" class="overview-grid" role="status" aria-label="正在读取实例">
              <ElCard v-for="item in 3" :key="item" shadow="never"><ElSkeleton :rows="3" animated /></ElCard>
            </div>
            <ElEmpty v-else-if="!hasSnapshot && !readError" description="等待后台首次同步实例。" :image-size="64" />
            <ElEmpty v-else-if="hasSnapshot && !snapshot.data.length" description="此项目暂无实例。" :image-size="64">
              <ElButton :loading="refreshPending" @click="requestRefresh">同步实例</ElButton>
            </ElEmpty>
            <ElEmpty v-else-if="hasFilters && hasSnapshot" description="没有符合筛选条件的实例。" :image-size="64">
              <ElButton @click="clearFilters">清除筛选</ElButton>
            </ElEmpty>
          </section>
        </div>
      </ElTabPane>
      <ElTabPane label="已释放" name="history" lazy>
        <InstanceAssetList v-if="tab === 'history'" :project-id="projectId" view="history" />
      </ElTabPane>
      <ElTabPane label="独立存储" name="storage" lazy>
        <InstanceAssetList v-if="tab === 'storage'" :project-id="projectId" view="storage" />
      </ElTabPane>
    </ElTabs>
    <CreateInstanceDialog v-if="creating" :project-id="projectId" :project-name="projectName" :operation-block="operationBlock" :channels="channels"
      @close="creating = false" @show-run="emit('showRun', $event)" @created="emit('refresh')" />
  </section>
</template>

<style scoped>
.overview,
.overview-current,
.overview-list { display: grid; gap: 16px; min-width: 0; }
.overview-heading {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 4px 12px;
}
.overview-heading h1 { min-width: 0; }
.overview-heading-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.overview-heading-actions > .el-button { margin: 0; }
.overview-sync-time { margin: 0; color: var(--muted); font-size: .875rem; }
.overview-sync-time span { color: var(--warning); }
.overview-read-error { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.overview-read-error :deep(.el-alert) { flex: 1 1 16rem; min-width: 0; overflow-wrap: anywhere; }
.overview-sync-feedback { color: var(--accent); font-size: .875rem; }
.overview-summary {
  gap: 1px;
  margin: 0;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--line);
  overflow: hidden;
}
.overview-summary > div { display: grid; gap: 4px; padding: 12px 16px; background: var(--panel); }
@container metric-panel (19rem <= width < 30rem) {
  .overview-summary > div:last-child { grid-column: 1 / -1; }
}
.overview-summary dt { color: var(--muted); font-size: .875rem; }
.overview-summary dd { margin: 0; font: 650 1.65rem/1.2 var(--mono); font-variant-numeric: tabular-nums; }
.overview-summary .overview-summary-stale { align-self: center; color: var(--warning); font-family: inherit; font-size: .95rem; }
.overview-filters { display: flex; gap: 8px; align-items: start; min-width: 0; }
.overview-search { width: min(100%, 310px); min-width: 0; }
.overview-state-filter { flex: 0 0 142px; }
.overview-filters .overview-filter-toggle { display: none; }
.overview-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr));
  gap: 12px;
  padding: 0;
  margin: 0;
  list-style: none;
}
.overview-grid > li { min-width: 0; }
.overview-instance { height: 100%; --el-card-border-color: var(--line); --el-card-border-radius: 8px; }
.overview-instance:hover, .overview-instance:focus-within { --el-card-border-color: var(--accent); }
.overview-instance :deep(.el-card__body) { padding: 16px; container: metric-panel / inline-size; }
.overview-instance-head,
.overview-instance-gpu {
  display: flex;
  justify-content: space-between;
  align-items: start;
  gap: 4px 8px;
  min-width: 0;
}
.overview-instance-head h3 { min-width: 0; font-size: 1.125rem; overflow-wrap: anywhere; }
.overview-instance-head a { color: var(--ink); text-decoration: none; }
.overview-instance-head a:hover { color: var(--accent); text-decoration: underline; }
.overview-instance-status { flex-shrink: 0; max-width: 56%; margin-top: 1px; }
.overview-instance-status::before { content: '•'; margin-right: 4px; }
.overview-instance-status :deep(.el-tag__content) { overflow: hidden; text-overflow: ellipsis; }
.overview-instance-gpu { flex-wrap: wrap; align-items: baseline; margin-top: 4px; font-family: var(--mono); overflow-wrap: anywhere; }
.overview-instance-gpu strong { font-weight: 650; }
.overview-instance-schedule { color: var(--warning); font-size: .8rem; }
.overview-connections {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
  align-items: start;
  gap: 8px;
  margin-top: 12px;
}
.overview-connections :deep(.el-button) {
  width: 100%;
  min-height: 44px;
  margin: 0;
  padding: 4px;
  white-space: normal;
  text-align: center;
  text-decoration: none;
}
.overview-external-icon { width: .9em; height: .9em; flex: none; margin-left: 3px; }
.overview-copy-feedback:not(:empty) { margin-top: 8px; color: var(--accent); font-size: .875rem; }
.overview-copy-feedback.has-copy-error { color: var(--danger); }
.overview-manual-copy { margin-top: 8px; font-family: var(--mono); }
.overview-instance-facts {
  gap: 8px;
  margin: 12px 0 0;
  padding-top: 12px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: .85rem;
}
.overview-instance-facts > div { display: grid; gap: 2px; }
.overview-instance-facts dd { margin: 0; color: var(--ink); overflow-wrap: anywhere; }

@media (max-width: 767px) {
  .overview, .overview-list { gap: 12px; }
  .overview-heading { grid-template-columns: minmax(0, 1fr) auto; }
  .overview-sync-time { grid-area: 2 / 1 / 3 / -1; }
  .overview-summary > div { padding: 8px 2px; text-align: center; }
  .overview-summary dt { font-size: .8rem; }
  .overview-summary dd { font-size: 1.3rem; }
  .overview-summary .overview-summary-stale { font-size: .8rem; }
  .overview-filters { flex-wrap: wrap; }
  .overview-search { flex: 1 1 0; }
  .overview-filters .overview-filter-toggle { display: inline-flex; margin: 0; }
  .overview-state-filter { display: none; flex: 1 1 100%; }
  .overview-state-filter.is-expanded { display: block; }
  .overview-grid { grid-template-columns: minmax(0, 1fr); }
  .overview-instance :deep(.el-card__body) { padding: 12px; }
  .overview-connections { margin-top: 8px; }
}
</style>
