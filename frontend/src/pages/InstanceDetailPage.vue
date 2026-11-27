<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, onScopeDispose, ref, shallowRef, watch } from 'vue'
import { ArrowDown, Refresh } from '@element-plus/icons-vue'
import type { ButtonInstance, DatePickerInstance, InputInstance } from 'element-plus'
import { query, request, requestId, type Action, type IdleSnooze, type Instance, type InstanceDetail, type InstanceJupyter, type Point, type Run, type Snapshot } from '../api/client'
import { queueCollection } from '../api/refresh'
import type { UsageRange } from '../api/dashboard'
import { useLocationNames } from '../catalog'
import { copyText } from '../clipboard'
import InstanceConnections from '../components/InstanceConnections.vue'
import InstanceIdleSnooze from '../components/InstanceIdleSnooze.vue'
import InstanceNetwork from '../components/InstanceNetwork.vue'
import { awaitingInstanceReview, chargeLabels, errorMessage, instanceLabels, localInput, timeLabel, unixTime } from '../display'

const MetricChart = defineAsyncComponent(() => import('../components/MetricChart.vue'))
const DashboardCost = defineAsyncComponent(() => import('../components/dashboard/DashboardCost.vue'))
const InstanceResizeDialog = defineAsyncComponent(() => import('../components/InstanceResizeDialog.vue'))
const props = defineProps<{
  projectId: string
  instanceId: string
  snapshot: Snapshot<Instance>
  loading: boolean
  error: string | null
  sampleInterval?: number
  operationBlock: string
}>()
const emit = defineEmits<{ refresh: []; showRun: [id: string] }>()
const { zoneName, regionName } = useLocationNames()
const metricNames = {
  cloudwatch_gpu_util: 'GPU 利用率',
  cloudwatch_gpu_memory_usage: '显存使用率',
  uhost_cpu_used: 'CPU 利用率',
  cloudwatch_memory_usage: '主机内存使用率',
  cloudwatch_sys_disk_used_per: '系统盘使用率',
  cloudwatch_data_disk_used_per: '数据盘使用率',
}
const activeTab = ref('monitor')
const costRange = ref<UsageRange>('7d')
const heading = ref<HTMLElement>()
const copyStatus = ref('')
const now = ref(Math.floor(Date.now() / 1000))
const selectedMetric = ref<keyof typeof metricNames>('cloudwatch_gpu_util')
const chosenRange = ref<number | 'last-run' | null>(null)
const begin = ref(Math.floor(Date.now() / 1000) - 3600)
const end = ref(Math.floor(Date.now() / 1000))
const samples = shallowRef<Point[]>([])
const metricError = ref('')
const lastMetricSync = ref<number | null>(null)
const frozenUntil = ref<number | null>(null)
const chartRevision = ref(0)
const metricLoading = ref(false)
const metricsReady = ref(false)
const chartVisited = ref(false)
const refreshMessage = ref('')
const refreshError = ref('')
const refreshPending = ref(false)
let refreshRequest: AbortController | undefined
const detailResponse = shallowRef<InstanceDetail>()
const detailError = ref('')
const detailLoading = ref(false)
const detailPath = '/instances/' + encodeURIComponent(props.instanceId) + query({ project_id: props.projectId })
const detailCurrent = computed(() => detailResponse.value
  && (detailResponse.value.data.archive_status
    || (detailResponse.value.success_at ?? 0) >= (props.snapshot.success_at ?? 0)) ? detailResponse.value : undefined)
const details = computed(() => detailCurrent.value?.data ?? props.snapshot.data.find(instance => instance.id === props.instanceId))
const archived = computed(() => details.value?.archive_status)
const jupyter = computed<InstanceJupyter>(() => detailCurrent.value?.jupyter
  ?? details.value?.jupyter
  ?? (detailResponse.value?.jupyter?.source === 'custom' ? detailResponse.value.jupyter : { url: null, source: null }))
const snapshotSync = computed(() => detailCurrent.value?.success_at ?? props.snapshot.success_at)
const snapshotError = computed(() => archived.value ? detailError.value : props.error || detailCurrent.value?.error || detailError.value)
const staleSnapshot = computed(() => !archived.value && Boolean(snapshotError.value || !snapshotSync.value
  || snapshotSync.value + (props.sampleInterval ?? 60) * 2 < now.value))
const overviewLink = computed(() => '#instances' + query({ project: props.projectId, view: archived.value ? 'history' : undefined }))
const automationLink = computed(() => '#tasks' + query({ project: props.projectId, automation: props.instanceId }))
const missingDetails = computed(() => snapshotSync.value && !props.error ? '该项目的清单中没有此实例。'
  : snapshotError.value ? '暂时无法读取实例详情。' : '等待后台首次同步实例。')
const lastPoint = computed(() => samples.value.reduce((last, point) => Math.max(last, point.sampled_at), 0))
const lastSample = computed(() => Math.max(detailResponse.value?.last_sample_at ?? 0, lastPoint.value) || null)
const metricsStale = computed(() => Boolean(details.value?.state === 'Running' && lastSample.value
  && lastSample.value + (props.sampleInterval ?? 60) * 2 < now.value))
const lastRunAnchor = computed(() => details.value?.stop_time
  || (details.value?.state === 'Stopped' || archived.value ? detailResponse.value?.last_sample_at : null))
const lookback = computed({
  get: () => chosenRange.value ?? ((details.value?.state === 'Stopped' || archived.value) && lastRunAnchor.value ? 'last-run' : 3600),
  set: value => { chosenRange.value = value },
})
const filtered = computed(() => samples.value.filter(point => point.metric === selectedMetric.value))
watch(filtered, points => { if (points.length) chartVisited.value = true })
const stateLabel = computed(() => {
  const instance = details.value
  if (!instance?.state) return '状态未知'
  const state = instanceLabels[instance.state] ?? instance.state
  if (instance.state !== 'Running') return state
  if (instance.gpu_mode === 'with_gpu') return '有卡运行'
  if (instance.gpu_mode === 'without_gpu') return instance.without_gpu_spec ? `无卡运行 · ${instance.without_gpu_spec} 档` : '无卡运行'
  return '正在运行 · 模式待确认'
})
const appointment = computed(() => detailCurrent.value?.shutdown_cleanup_pending ? '旧预约清理待确认'
  : details.value?.scheduler_stop_time ? timeLabel(details.value.scheduler_stop_time)
    : details.value?.scheduler_status === 'known' ? '未设置' : '待核对')
const lifecycle = computed(() => [
  { label: '创建', at: details.value?.create_time },
  { label: '最近开机', at: details.value?.start_time },
  { label: '最近关机', at: details.value?.stop_time },
  { label: '预约关机', at: details.value?.scheduler_stop_time },
  { label: '到期', at: details.value?.expire_time },
  { label: '回收', at: details.value?.release_time },
  { label: archived.value === 'missing' ? '发现离开清单' : '确认释放', at: details.value?.archived_at },
  { label: '平台更新', at: details.value?.updated_at },
].filter(item => item.at != null).sort((a, b) => a.at! - b.at!))
const runLabels: Record<string, string> = {
  queued: '已排队', waiting: '等待中', reconciling: '待核对', manual_review: '待人工核对',
  succeeded: '已完成', reviewed: '已核对', failed: '失败', expired: '窗口已结束', canceled: '已取消', missed: '已错过',
}
function runStatus(run: Run) {
  if (run.result?.automation_reminded) return '仅提醒'
  if (run.state === 'waiting' && !run.phase && run.result?.capacity_wait_since) return '等待资源'
  return runLabels[run.state] ?? run.state
}
function runTagType(state: string) {
  if (state === 'succeeded' || state === 'reviewed') return 'success'
  if (state === 'failed') return 'danger'
  if (['reconciling', 'manual_review', 'expired'].includes(state)) return 'warning'
  return 'info'
}
function runAlertType(state: string) {
  const type = runTagType(state)
  return type === 'danger' ? 'error' : type
}
const runs = shallowRef<Run[]>([])
const historyError = ref('')
const historySynced = ref(false)
let detailRequest: AbortController | undefined
let metricRequest: AbortController | undefined
let historyRequest: AbortController | undefined

function field(value?: string | number | null, suffix = '') {
  return value == null || value === '' ? '平台未返回' : String(value) + suffix
}

function flag(value?: boolean | null) {
  return value == null ? '平台未返回' : value ? '是' : '否'
}

function usage(seconds?: number | null) {
  if (seconds == null) return '平台未返回'
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor(seconds / 60) % 60
  return (hours ? hours + ' 小时 ' : '') + minutes + ' 分 ' + (seconds % 60) + ' 秒'
}

async function copyInstanceId() {
  copyStatus.value = await copyText(props.instanceId) ? '已复制实例 ID。' : '无法自动复制，请选中实例 ID。'
}

async function loadDetail() {
  if (detailRequest) return
  const controller = new AbortController()
  detailRequest = controller
  detailLoading.value = true
  try {
    const response = await request<InstanceDetail>(detailPath, { signal: controller.signal })
    if (response.data?.id !== props.instanceId || response.data?.project_id !== props.projectId) {
      throw new Error('实例详情与当前项目不符')
    }
    if (!controller.signal.aborted) {
      detailResponse.value = response
      detailError.value = ''
    }
  } catch (error) {
    if (!controller.signal.aborted) detailError.value = errorMessage(error)
  } finally {
    if (detailRequest === controller) {
      detailRequest = undefined
      detailLoading.value = false
    }
  }
}

function updateJupyter(jupyter: InstanceJupyter) {
  detailRequest?.abort()
  detailRequest = undefined
  detailLoading.value = false
  if (detailResponse.value) detailResponse.value = { ...detailResponse.value, jupyter }
  void loadDetail()
  emit('refresh')
}

function updateSnooze(idle_snooze: IdleSnooze) {
  detailRequest?.abort()
  detailRequest = undefined
  detailLoading.value = false
  if (detailResponse.value) detailResponse.value = { ...detailResponse.value, idle_snooze }
}

function metricBounds() {
  const now = Math.floor(Date.now() / 1000)
  if (lookback.value === 'last-run') {
    const until = frozenUntil.value ?? lastRunAnchor.value ?? now
    const start = details.value?.start_time
    return { until, since: start && start < until ? Math.max(start, until - 86400) : until - 3600 }
  }
  const until = frozenUntil.value ?? details.value?.archived_at ?? now
  return { until, since: until - lookback.value }
}

async function loadMetrics(reset = false) {
  if (reset) {
    metricRequest?.abort()
    metricRequest = undefined
    samples.value = []
    metricError.value = ''
    lastMetricSync.value = null
    frozenUntil.value = null
    metricLoading.value = false
    metricsReady.value = false
  }
  if (!details.value || metricRequest || activeTab.value !== 'monitor') return
  const controller = new AbortController()
  metricRequest = controller
  metricLoading.value = true
  const { since, until } = metricBounds()
  try {
    const response = await request<{ data: Point[]; success_at?: number | null; error?: string | null }>('/instances/' + encodeURIComponent(props.instanceId) + '/metrics' + query({
      project_id: props.projectId,
      since,
      until,
    }), { signal: controller.signal })
    if (!Array.isArray(response.data)) throw new Error('指标响应格式无效')
    if (!controller.signal.aborted && (frozenUntil.value === null || frozenUntil.value === until)) {
      samples.value = response.data
      begin.value = since
      end.value = until
      lastMetricSync.value = response.success_at ?? null
      metricError.value = response.error ?? ''
      metricsReady.value = true
    }
  } catch (error) {
    if (!controller.signal.aborted) metricError.value = errorMessage(error)
  } finally {
    if (metricRequest === controller) {
      metricRequest = undefined
      metricLoading.value = false
    }
  }
}
async function loadHistory() {
  if (!details.value || historyRequest) return
  const controller = new AbortController()
  historyRequest = controller
  try {
    const items = await request<Run[]>('/runs' + query({ project_id: props.projectId, instance_id: props.instanceId }), { signal: controller.signal })
    if (!Array.isArray(items)) throw new Error('操作记录格式无效')
    if (!controller.signal.aborted) {
      runs.value = items.filter(run => run.project_id === props.projectId
        && (run.snapshot?.config?.instance_id === props.instanceId || run.result?.instance_id === props.instanceId
          || (Array.isArray(run.result?.submitted_ids) && run.result.submitted_ids.includes(props.instanceId))))
      historyError.value = ''
      historySynced.value = true
    }
  } catch (error) {
    if (!controller.signal.aborted) historyError.value = errorMessage(error)
  } finally {
    if (historyRequest === controller) historyRequest = undefined
  }
}

watch(() => details.value?.id, () => void loadHistory(), { immediate: true })
watch(() => props.snapshot.success_at, () => void loadDetail())
watch([() => details.value?.id, lookback], () => {
  chartRevision.value += 1
  void loadMetrics(true)
}, { immediate: true })
watch(activeTab, tab => { if (tab === 'monitor') void loadMetrics() })

function returnToLatest() {
  metricRequest?.abort()
  metricRequest = undefined
  frozenUntil.value = null
  chartRevision.value += 1
  void loadMetrics()
}
const timer = window.setInterval(() => {
  if (!document.hidden) {
    void loadDetail()
    void loadMetrics()
  }
}, 30_000)
const historyTimer = window.setInterval(() => {
  now.value = Math.floor(Date.now() / 1000)
  if (!document.hidden) void loadHistory()
}, 10_000)
onMounted(() => {
  heading.value?.focus()
  void loadDetail()
})
onScopeDispose(() => {
  window.clearInterval(timer)
  window.clearInterval(historyTimer)
  detailRequest?.abort()
  metricRequest?.abort()
  historyRequest?.abort()
  actionRequest?.abort()
  refreshRequest?.abort()
})

async function requestRefresh() {
  if (refreshPending.value) return
  const controller = new AbortController()
  refreshRequest = controller
  refreshPending.value = true
  refreshMessage.value = ''
  refreshError.value = ''
  try {
    const receipt = await queueCollection({ kind: 'instances', project_id: props.projectId }, controller.signal, () => {
      refreshMessage.value = '当前项目同步已排队，等待后台读取。'
    })
    if (controller.signal.aborted) return
    const outcome = receipt.outcomes[0]
    refreshError.value = outcome?.error ?? ''
    refreshMessage.value = receipt.state === 'succeeded' ? '当前项目实例同步成功 · ' + timeLabel(outcome?.finished_at) : ''
    emit('refresh')
    void loadDetail()
    void loadHistory()
  } catch (error) {
    if (!controller.signal.aborted) {
      refreshMessage.value = ''
      refreshError.value = errorMessage(error)
    }
  } finally {
    refreshRequest = undefined
    refreshPending.value = false
  }
}

const actionLabels: Record<Action['kind'], string> = {
  start: '有卡开机', stop: '立即关机', shutdown: '设置关机预约', cancel_shutdown: '取消关机预约',
  reboot: '重启', rename: '重命名',
}
const actionOpen = ref(false)
const menuTrigger = ref<ButtonInstance>()
const dateInput = ref<DatePickerInstance>()
const nameInput = ref<InputInstance>()
let actionTrigger: HTMLElement | undefined
const action = ref<Action | null>(null)
const actionTitle = computed(() => !action.value ? '' : action.value.boot_mode === 'without_gpu' ? '无卡开机' : actionLabels[action.value.kind])
const cardlessProfiles = computed(() => details.value?.cardless_profiles ?? [])
const stopAt = ref('')
const originalAppointment = ref<number | null>(null)
const replace = ref(false)
const renameTo = ref('')
const actionPending = ref(false)
const actionError = ref('')
const actionFieldError = ref('')
const submittedRun = shallowRef<Run>()
const resizeOpen = ref(false)
let actionRequest: AbortController | undefined
const trackedRun = computed(() => submittedRun.value
  && (runs.value.find(run => run.id === submittedRun.value?.id) ?? submittedRun.value))
function isActiveRun(run: Run) {
  return awaitingInstanceReview(run) || (['queued', 'waiting', 'reconciling'].includes(run.state)
    && (['acquire', 'terminate', 'resize'].includes(run.kind) || Object.hasOwn(actionLabels, run.kind)))
}

function isUnsentScheduledStart(run: Run) {
  return run.kind === 'start' && Boolean(run.task_id) && !run.phase && ['queued', 'waiting'].includes(run.state)
}

const activeRuns = computed(() => {
  const all = trackedRun.value ? [trackedRun.value, ...runs.value.filter(run => run.id !== trackedRun.value?.id)] : runs.value
  return all.filter(isActiveRun)
})
const activeRun = computed(() => activeRuns.value[0])
const unsentScheduledStart = computed(() => activeRuns.value.find(isUnsentScheduledStart))
const stopBlockRun = computed(() => activeRuns.value.find(run => !isUnsentScheduledStart(run)))
const baseActionBlock = computed(() => (archived.value ? '归档实例仅供查看。' : '') || props.operationBlock
  || (staleSnapshot.value ? '实例信息待同步，请刷新后操作。' : '')
  || (historyError.value ? '无法确认当前操作状态，请刷新后重试。' : '')
  || (!historySynced.value ? '正在检查是否有操作在途。' : ''))
function operationBlockFor(kind?: Action['kind']) {
  if (baseActionBlock.value) return baseActionBlock.value
  const run = kind === 'stop' ? stopBlockRun.value : activeRun.value
  if (!run) return ''
  if (awaitingInstanceReview(run)) return '已有实例操作等待核对。'
  if (isUnsentScheduledStart(run)) return run.result?.capacity_wait_since ? '预约开机正在等待资源。' : '预约开机正在等待执行。'
  return '已有实例操作在途，请等待云端确认。'
}
const operationBlocked = computed(() => operationBlockFor())
const stopBlocked = computed(() => operationBlockFor('stop'))
const actionBlocked = computed(() => operationBlockFor(action.value?.kind))
const mayStop = computed(() => details.value?.state === 'Running' || Boolean(unsentScheduledStart.value))
const operationMenuBlocked = computed(() => !!operationBlocked.value && (!mayStop.value || !!stopBlocked.value))
const schedulerUnknown = computed(() => details.value?.scheduler_status !== 'known')
const hasAppointmentActions = computed(() => details.value?.charge_type !== 'Spot'
  && (details.value?.state === 'Running' || details.value?.scheduler_stop_time))

watch(() => trackedRun.value?.state, state => {
  if (state === 'succeeded') void requestRefresh()
})

function openAction(kind: Action['kind'], trigger?: EventTarget | null, cardless = false) {
  if (!details.value || operationBlockFor(kind)) return
  actionTrigger = trigger instanceof HTMLElement ? trigger : undefined
  actionError.value = ''
  actionFieldError.value = ''
  replace.value = false
  stopAt.value = localInput(Math.floor(Date.now() / 1000) + 3600)
  originalAppointment.value = details.value.scheduler_stop_time ?? null
  renameTo.value = details.value.name || details.value.id
  action.value = { project_id: props.projectId, instance_id: details.value.id, kind, request_id: requestId() }
  if (cardless) {
    const profile = cardlessProfiles.value[0]
    if (!profile) return
    action.value.boot_mode = 'without_gpu'
    action.value.without_gpu_spec = profile.spec
  }
  if (kind === 'cancel_shutdown' && originalAppointment.value) {
    action.value.expected_stop_at = originalAppointment.value
  }
  if (kind === 'rename') {
    action.value.expected_name = renameTo.value
  }
  actionOpen.value = true
}

function chooseAction(command: unknown) {
  if (command === 'stop' || command === 'reboot' || command === 'rename') openAction(command, menuTrigger.value?.ref)
  if (command === 'resize' && !operationBlocked.value && details.value?.state === 'Stopped') resizeOpen.value = true
}

function resizeSubmitted(run: Run) {
  resizeOpen.value = false
  submittedRun.value = run
  void loadHistory()
  emit('refresh')
}

function restoreActionFocus() {
  actionTrigger?.focus()
  if (!actionTrigger || document.activeElement !== actionTrigger) heading.value?.focus()
  actionTrigger = undefined
}

async function submitAction() {
  if (!action.value || actionPending.value) return
  if (actionBlocked.value) {
    actionError.value = actionBlocked.value
    return
  }
  actionError.value = ''
  actionFieldError.value = ''
  if (action.value.boot_mode === 'without_gpu' && !cardlessProfiles.value.some(profile => profile.spec === action.value?.without_gpu_spec)) {
    actionFieldError.value = '请选择已核实的无卡档位。'
    return
  }
  if (action.value.kind === 'shutdown') {
    const at = unixTime(stopAt.value)
    if (!Number.isFinite(at) || at < Date.now() / 1000 + 300) {
      actionFieldError.value = '请选择至少 5 分钟后的时间。'
      dateInput.value?.focus()
      return
    }
    action.value.stop_at = at
    action.value.conflict = replace.value ? 'replace' : 'keep'
    action.value.expected_stop_at = replace.value ? originalAppointment.value ?? undefined : undefined
  }
  if (action.value.kind === 'rename') {
    const name = renameTo.value.trim()
    if (!/^[a-zA-Z0-9_.,: \u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff-]{1,63}$/u.test(name)) {
      actionFieldError.value = '名称须为 1 至 63 个字符，可使用中文、英文、数字、空格及 _ . , : -。'
      nameInput.value?.focus()
      return
    }
    action.value.name = name
  }
  const controller = new AbortController()
  actionRequest = controller
  actionPending.value = true
  try {
    const result = await request<Run>('/actions', { method: 'POST', body: action.value, signal: controller.signal })
    if (controller.signal.aborted) return
    submittedRun.value = result
    actionOpen.value = false
    historyRequest?.abort()
    historyRequest = undefined
    void loadHistory()
  } catch (error) {
    if (!controller.signal.aborted) actionError.value = errorMessage(error)
  } finally {
    actionRequest = undefined
    actionPending.value = false
  }
}
</script>

<template>
  <div class="instance-breadcrumb">
    <a :href="overviewLink">实例总览</a>
    <span aria-hidden="true">/</span>
    <span>实例详情</span>
  </div>
  <ElCard shadow="never" class="detail-card">
    <div class="detail-heading">
      <div>
        <div class="name-line">
          <h1 ref="heading" tabindex="-1" class="instance-detail-title">{{ details?.name || instanceId }}</h1>
          <ElTag v-if="details" :type="staleSnapshot ? 'warning' : details.state === 'Running' ? 'success' : 'info'">{{ stateLabel }}</ElTag>
        </div>
        <div class="detail-meta muted">
          <code>{{ instanceId }}</code>
          <ElButton link type="primary" @click="copyInstanceId">复制实例 ID</ElButton>
          <span v-if="copyStatus" role="status">{{ copyStatus }}</span>
        </div>
      </div>
      <ElButton v-if="!archived" :loading="refreshPending" :icon="Refresh" @click="requestRefresh">同步</ElButton>
    </div>
    <p class="muted sync-note">{{ archived ? '归档时间' : '实例同步' }} {{ timeLabel(snapshotSync) }}<span v-if="snapshotSync && staleSnapshot"> · 数据待刷新</span></p>
    <ElButton v-if="details?.release_run_id" link type="primary" @click="emit('showRun', details.release_run_id)">查看释放回执</ElButton>
    <template v-if="details && !archived">
      <div class="appointment-line">
        <span>最晚关机 <strong>{{ appointment }}</strong></span>
        <template v-if="hasAppointmentActions">
          <ElButton v-if="details.state === 'Running'" link type="primary"
            :disabled="!!operationBlocked || schedulerUnknown" @click="openAction('shutdown', $event.currentTarget)">
            {{ details.scheduler_stop_time ? '编辑预约' : '设置预约' }}
          </ElButton>
          <ElButton v-if="details.scheduler_stop_time" link type="primary"
            :disabled="!!operationBlocked || schedulerUnknown" @click="openAction('cancel_shutdown', $event.currentTarget)">取消预约</ElButton>
        </template>
        <a :href="automationLink">实例自动化规则</a>
      </div>
      <div v-if="details.state === 'Stopped'" class="operation-area">
        <ElButton type="primary" class="start-action" :disabled="!!operationBlocked"
          @click="openAction('start', $event.currentTarget)">有卡开机</ElButton>
        <ElButton v-if="cardlessProfiles.length" class="start-action" :disabled="!!operationBlocked"
          @click="openAction('start', $event.currentTarget, true)">无卡开机</ElButton>
        <span v-else-if="details.support_without_gpu_start && details.charge_type === 'Postpay'" class="muted">无卡档位尚未核实。</span>
      </div>
      <InstanceConnections :instance="details" :jupyter="jupyter" :stale="staleSnapshot" @jupyter-change="updateJupyter" />
      <div v-if="['Running', 'Stopped'].includes(details.state)" class="operation-area">
        <ElDropdown trigger="click" placement="bottom-start" :disabled="operationMenuBlocked" @command="chooseAction">
          <ElButton ref="menuTrigger" :disabled="operationMenuBlocked" :icon="ArrowDown">更多操作</ElButton>
          <template #dropdown>
            <ElDropdownMenu>
              <ElDropdownItem v-if="details.state === 'Running'" command="reboot" :disabled="!!operationBlocked">重启</ElDropdownItem>
              <ElDropdownItem v-if="mayStop" command="stop" :disabled="!!stopBlocked">立即关机</ElDropdownItem>
              <ElDropdownItem v-if="details.state === 'Stopped'" command="resize" :disabled="!!operationBlocked">更改规格</ElDropdownItem>
              <ElDropdownItem command="rename" :disabled="!!operationBlocked">重命名</ElDropdownItem>
            </ElDropdownMenu>
          </template>
        </ElDropdown>
        <p v-if="operationBlocked" class="muted" role="status">
          {{ operationBlocked }}
          <ElButton v-if="activeRun && activeRun.id !== trackedRun?.id" type="primary" link @click="emit('showRun', activeRun.id)">
            {{ awaitingInstanceReview(activeRun) ? '查看待核对操作' : '查看在途操作' }}
          </ElButton>
        </p>
      </div>
      <p v-if="details.charge_type === 'Spot'" class="muted">抢占式实例无法使用云端关机预约。</p>
      <p v-else-if="hasAppointmentActions && schedulerUnknown" class="muted" role="status">关机预约状态待核对，同步后可再试。</p>
    </template>
  </ElCard>
  <InstanceResizeDialog v-if="resizeOpen && details && !archived" :project-id="projectId" :instance-id="instanceId"
    :instance-name="details.name || instanceId" :operation-block="operationBlocked"
    @close="resizeOpen = false" @submitted="resizeSubmitted" />
  <ElAlert v-if="snapshotError" :title="snapshotError" type="warning" :closable="false" show-icon />
  <ElAlert v-if="refreshError" :title="refreshError" type="error" :closable="false" show-icon />
  <p v-if="refreshMessage" role="status">{{ refreshMessage }}</p>
  <ElAlert v-if="trackedRun" :title="trackedRun.name + ' · ' + runStatus(trackedRun)"
    :type="runAlertType(trackedRun.state)" :closable="false" show-icon>
    <p v-if="trackedRun.reason">{{ trackedRun.reason }}</p>
    <ElButton type="primary" link @click="emit('showRun', trackedRun.id)">查看运行</ElButton>
  </ElAlert>
  <ElCard v-if="!details" shadow="never">
    <ElSkeleton v-if="loading || detailLoading" animated :rows="3" aria-label="正在读取实例" />
    <ElEmpty v-else :description="missingDetails">
      <a :href="overviewLink">返回实例总览</a>
    </ElEmpty>
  </ElCard>
  <ElTabs v-if="details" v-model="activeTab" class="detail-tabs" stretch>
    <ElTabPane label="资源监控" name="monitor" lazy>
      <ElCard shadow="never" class="detail-card">
        <InstanceIdleSnooze v-if="detailResponse?.idle_snooze" :project-id="projectId" :instance-id="instanceId"
          :current="detailResponse.idle_snooze" :now="now" @changed="updateSnooze" />
        <div class="monitor-toolbar">
          <ElSelect v-model="selectedMetric" aria-label="监控指标">
            <ElOption v-for="(label, metric) in metricNames" :key="metric" :label="label" :value="metric" />
          </ElSelect>
          <ElSelect v-model="lookback" aria-label="采样时间范围">
            <ElOption label="最近 1 小时" :value="3600" />
            <ElOption label="最近 6 小时" :value="21600" />
            <ElOption label="最近 24 小时" :value="86400" />
            <ElOption v-if="lastRunAnchor" label="上次运行（最多 24 小时）" value="last-run" />
          </ElSelect>
          <ElButton v-if="frozenUntil !== null" type="primary" plain @click="returnToLatest">回到最新</ElButton>
        </div>
        <p class="muted">本实例最近采样 {{ timeLabel(lastSample) }} · 指标同步 {{ timeLabel(lastMetricSync) }}</p>
        <p v-if="metricsReady" class="muted">采样范围 {{ timeLabel(begin) }} 至 {{ timeLabel(end) }}（北京时间）</p>
        <ElAlert v-if="metricsStale" title="本实例的采样已过期" type="warning" :closable="false" show-icon />
        <ElAlert v-if="metricError" title="监控读取失败" type="warning" :closable="false" show-icon>
          <p>{{ metricError }}</p>
          <ElButton link type="primary" @click="loadMetrics()">重试</ElButton>
        </ElAlert>
        <ElSkeleton v-if="metricLoading && !metricsReady" animated :rows="3" aria-label="正在读取监控" />
        <ElEmpty v-if="metricsReady && !metricError && !chartVisited" description="所选时间段暂无采样" :image-size="72" />
        <MetricChart v-if="chartVisited" v-show="metricsReady" :points="filtered" :title="metricNames[selectedMetric]"
          :since="begin" :until="end" :reset-version="chartRevision" @zoomed="frozenUntil = end" />
      </ElCard>
    </ElTabPane>
    <ElTabPane label="成本与用量" name="cost" lazy>
      <DashboardCost v-if="activeTab === 'cost'" :project-id="projectId" :instance-id="instanceId"
        :range="costRange" @change-range="costRange = $event" />
    </ElTabPane>
    <ElTabPane label="规格与网络" name="specs" lazy>
      <div class="detail-grid">
        <ElCard shadow="never" class="detail-card" body-class="metric-container">
          <h2 id="instance-basic-title">基本信息</h2>
          <dl class="facts metric-grid metric-grid--five">
            <div><dt>项目</dt><dd class="mono">{{ projectId }}</dd></div>
            <div><dt>地域</dt><dd>{{ field(regionName(details.region)) }}</dd></div>
            <div><dt>可用区</dt><dd>{{ field(zoneName(details)) }}</dd></div>
            <div><dt>平台地域 / 可用区编号</dt><dd class="mono">{{ field(details.region) }} / {{ field(details.zone) }}</dd></div>
            <div><dt>实例类型</dt><dd>{{ field(details.instance_type) }}</dd></div>
            <div><dt>机器类型</dt><dd>{{ field(details.machine_type) }}</dd></div>
            <div><dt>业务组</dt><dd>{{ field(details.business_group) }}</dd></div>
            <div v-if="details.remark"><dt>备注</dt><dd>{{ details.remark }}</dd></div>
          </dl>
        </ElCard>
        <ElCard shadow="never" class="detail-card" body-class="metric-container">
          <h2 id="instance-compute-title">计算与系统</h2>
          <dl class="facts metric-grid metric-grid--five">
            <div><dt>GPU 型号</dt><dd>{{ field(details.gpu_model) }}</dd></div>
            <div><dt>GPU 数量</dt><dd>{{ field(details.gpu) }}</dd></div>
            <div><dt>显存规格</dt><dd>{{ field(details.graphics_memory_gb, ' GB') }}</dd></div>
            <div><dt>CPU 核数</dt><dd>{{ field(details.cpu, ' vCPU') }}</dd></div>
            <div><dt>内存</dt><dd>{{ field(details.memory_gb, ' GB') }}</dd></div>
            <div><dt>CPU 平台</dt><dd>{{ field(details.cpu_platform) }}</dd></div>
            <div><dt>CPU 架构</dt><dd>{{ field(details.cpu_arch) }}</dd></div>
            <div><dt>支持无卡开机</dt><dd>{{ flag(details.support_without_gpu_start) }}</dd></div>
            <div><dt>操作系统</dt><dd>{{ field(details.os_name) }}</dd></div>
            <div><dt>系统类型</dt><dd>{{ field(details.os_type) }}</dd></div>
            <div><dt>镜像名称</dt><dd>{{ field(details.image_name) }}</dd></div>
            <div><dt>镜像 ID</dt><dd class="mono">{{ field(details.image_id) }}</dd></div>
            <div v-if="details.image_type"><dt>镜像类型</dt><dd>{{ details.image_type }}</dd></div>
            <div v-if="details.image_status"><dt>镜像状态</dt><dd>{{ details.image_status }}</dd></div>
            <div v-if="details.image_source"><dt>镜像来源</dt><dd>{{ details.image_source }}</dd></div>
            <div v-if="details.image_author"><dt>镜像作者</dt><dd>{{ details.image_author }}</dd></div>
            <div v-if="details.basic_image_name"><dt>基础镜像</dt><dd>{{ details.basic_image_name }}</dd></div>
            <div v-if="details.basic_image_id"><dt>基础镜像 ID</dt><dd class="mono">{{ details.basic_image_id }}</dd></div>
          </dl>
        </ElCard>

        <ElCard shadow="never" class="detail-card" body-class="metric-container">
          <h2 id="instance-disk-title">磁盘</h2>
          <dl class="facts metric-grid">
            <div><dt>磁盘总容量</dt><dd>{{ field(details.total_disk_space_gb, ' GB') }}</dd></div>
            <div><dt>共享存储容量</dt><dd>{{ field(details.total_volume_space_gb, ' GB') }}</dd></div>
          </dl>
          <ul v-if="details.disks?.length" class="row-list">
            <li v-for="(disk, index) in details.disks" :key="disk.id || index">
              <strong>{{ disk.name || (disk.purpose === 'system' ? '系统盘' : disk.purpose === 'data' ? '数据盘' : field(disk.purpose)) }}</strong>
              <p>{{ field(disk.type) }} · {{ field(disk.size_gb, ' GB') }}<template v-if="disk.drive"> · {{ disk.drive }}</template>
                <template v-if="disk.encrypted != null"> · 加密 {{ flag(disk.encrypted) }}</template></p>
              <small v-if="disk.id" class="muted mono">{{ disk.id }}</small>
            </li>
          </ul>
          <p v-else class="muted">{{ details.disks == null ? '平台未返回磁盘记录。' : '暂无磁盘记录。' }}</p>
        </ElCard>
        <InstanceNetwork :instance="details" :stale="staleSnapshot" />
      </div>
    </ElTabPane>
    <ElTabPane label="生命周期与记录" name="history" lazy>
      <ElCard shadow="never" class="detail-card" body-class="metric-container">
        <h2 id="instance-lifecycle-title">计费与生命周期</h2>
        <dl class="facts metric-grid metric-grid--five">
          <div><dt>计费方式</dt><dd>{{ chargeLabels[details.charge_type ?? ''] ?? field(details.charge_type) }}</dd></div>
          <div><dt>累计用量</dt><dd>{{ usage(details.usage_seconds) }}</dd></div>
          <div><dt>自动续费</dt><dd>{{ flag(details.auto_renew) }}</dd></div>
          <div><dt>已过期</dt><dd>{{ flag(details.is_expired) }}</dd></div>
          <div><dt>关机预约</dt><dd>{{ appointment }}</dd></div>
          <div v-if="details.gpu_mode === 'without_gpu'">
            <dt>无卡算力单价</dt><dd>{{ details.cardless_hourly_cny == null ? '待核价' : field(details.cardless_hourly_cny, ' 元/小时') }}
              <span v-if="details.cardless_rate_source"> · {{ details.cardless_rate_source === 'account' ? '账号价' : '官方标准价' }}</span>
            </dd>
          </div>
          <div v-else-if="details.instance_price_per_hour != null"><dt>实例单价</dt><dd>{{ field(details.instance_price_per_hour, ' 元/小时') }}</dd></div>
          <div v-if="details.disk_price_per_hour != null"><dt>磁盘单价</dt><dd>{{ field(details.disk_price_per_hour, ' 元/小时') }}</dd></div>
          <div v-if="details.image_price_per_hour != null"><dt>镜像单价</dt><dd>{{ field(details.image_price_per_hour, ' 元/小时') }}</dd></div>
          <div v-if="details.discount_type != null"><dt>折扣类型代码</dt><dd>{{ details.discount_type }}</dd></div>
        </dl>
        <ol v-if="lifecycle.length" class="instance-timeline">
          <li v-for="entry in lifecycle" :key="entry.label"><span>{{ entry.label }}</span><span class="mono">{{ timeLabel(entry.at) }}</span></li>
        </ol>
        <p v-else class="muted">平台未返回生命周期时间。</p>
        <h3>近期操作</h3>
        <ElAlert v-if="historyError" title="操作记录读取失败" type="warning" :closable="false" show-icon>
          <p>{{ historyError }}</p>
          <ElButton link type="primary" @click="loadHistory">重试</ElButton>
        </ElAlert>
        <ul v-if="runs.length" class="row-list">
          <li v-for="run in runs.slice(0, 8)" :key="run.id" class="instance-run">
            <ElButton link type="primary" class="run-link" @click="emit('showRun', run.id)">{{ run.name }}</ElButton>
            <ElTag :type="runTagType(run.state)">{{ runStatus(run) }}</ElTag>
            <span class="muted">{{ timeLabel(run.updated_at) }}<template v-if="run.reason"> · {{ run.reason }}</template></span>
          </li>
        </ul>
        <p v-else-if="!historyError" class="muted">{{ historySynced ? '暂无操作记录。' : '正在读取操作记录…' }}</p>
      </ElCard>
    </ElTabPane>
  </ElTabs>
  <ElDialog v-model="actionOpen" :title="actionTitle" width="min(32rem, calc(100% - 24px))"
    append-to-body destroy-on-close :show-close="!actionPending" :close-on-press-escape="!actionPending"
    :close-on-click-modal="false" @closed="restoreActionFocus">
    <ElForm v-if="action" label-position="top" :disabled="actionPending" class="action-form" @submit.prevent="submitAction">
      <p><strong>{{ details?.name || instanceId }}</strong> <code>{{ instanceId }}</code></p>
      <template v-if="action.boot_mode === 'without_gpu'">
        <p>以 CPU 和内存规格运行，不分配 GPU。算力与存储仍可能产生费用。</p>
        <ElFormItem label="无卡档位" required :error="actionFieldError">
          <ElSelect v-model="action.without_gpu_spec">
            <ElOption v-for="profile in cardlessProfiles" :key="profile.spec" :value="profile.spec"
              :label="`${profile.spec} · ${profile.cpu} 核 / ${profile.memory_gb} GB${profile.standard_hourly_cny == null ? ' · 待核价' : ` · 官方 ¥${profile.standard_hourly_cny}/时`}`" />
          </ElSelect>
        </ElFormItem>
      </template>
      <p v-if="action.kind === 'stop' && details?.state === 'Running'">关机会中断此实例上的训练。确认后提交关机申请。</p>
      <p v-if="action.kind === 'stop' && unsentScheduledStart">确认后将停止此实例当前尚未提交的预约开机尝试。</p>
      <p v-if="action.kind === 'reboot'">重启会中断此实例上的训练和连接。确认后提交重启申请。</p>
      <p v-if="action.kind === 'cancel_shutdown' && originalAppointment">将取消 {{ timeLabel(originalAppointment) }} 的关机预约。</p>
      <ElFormItem v-if="action.kind === 'rename'" label="新名称" required :error="actionFieldError">
        <ElInput ref="nameInput" v-model="renameTo" maxlength="63" @input="actionFieldError = ''" />
      </ElFormItem>
      <template v-if="action.kind === 'shutdown'">
        <ElFormItem label="关机时间（北京时间）" required :error="actionFieldError">
          <ElDatePicker ref="dateInput" v-model="stopAt" type="datetime" format="YYYY-MM-DD HH:mm" value-format="YYYY-MM-DD[T]HH:mm"
            placeholder="选择关机时间" @change="actionFieldError = ''" />
        </ElFormItem>
        <ElFormItem v-if="originalAppointment" :label="'当前云端预约 ' + timeLabel(originalAppointment)">
          <ElRadioGroup v-model="replace" class="appointment-options">
            <ElRadio :value="false">保留当前预约</ElRadio>
            <ElRadio :value="true">替换为选定时间</ElRadio>
          </ElRadioGroup>
        </ElFormItem>
      </template>
      <ElAlert v-if="actionError" :title="actionError" type="error" :closable="false" show-icon />
      <ElAlert v-else-if="actionBlocked" :title="actionBlocked" type="warning" :closable="false" show-icon />
      <div class="action-footer">
        <ElButton :disabled="actionPending" @click="actionOpen = false">返回</ElButton>
        <ElButton native-type="submit" :type="action.kind === 'stop' || action.kind === 'reboot' ? 'danger' : 'primary'"
          :loading="actionPending" :disabled="!!actionBlocked">{{ actionTitle }}</ElButton>
      </div>
    </ElForm>
  </ElDialog>
</template>

<style scoped>
.instance-breadcrumb, .detail-meta, .name-line, .operation-area, .appointment-line, .instance-run {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
  min-width: 0;
}
.instance-breadcrumb { color: var(--muted); font-size: .875rem; }
.detail-card :deep(.el-card__body) { display: grid; gap: 12px; }
.detail-card p { margin: 0; }
.detail-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px 12px; min-width: 0; }
.detail-heading > div { min-width: 0; }
.instance-detail-title { overflow-wrap: anywhere; }
.detail-meta { margin-top: 4px; }
.sync-note { font-size: .875rem; }
.appointment-line { min-height: 30px; }
.appointment-line strong { margin-left: 4px; font-weight: 650; }
.appointment-line .el-button + .el-button { margin-left: 0; }
.operation-area { margin-top: 4px; }
.start-action { justify-self: start; }
.detail-tabs { min-width: 0; }
.detail-tabs :deep(.el-tabs__nav) { width: 100%; }
.detail-tabs :deep(.el-tabs__item) { flex: 1; min-width: 0; padding-inline: 4px; }
.detail-tabs :deep(.el-tabs__content) { padding: 4px; margin: -4px; }
.monitor-toolbar { display: flex; flex-wrap: wrap; gap: 8px; }
.monitor-toolbar > .el-select { width: min(100%, 14rem); }
.detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 26rem), 1fr)); gap: 16px; align-items: start; }
.facts { gap: 8px 12px; margin: 0; padding: 12px; background: #f1f6f5; }
.facts dt { color: var(--muted); font-size: .875rem; }
.facts dd { margin: 4px 0 0; overflow-wrap: anywhere; }
.row-list { display: grid; gap: 8px; padding: 0; margin: 0; list-style: none; }
.row-list li { padding: 8px 12px; background: #f2f6f5; border-left: 2px solid var(--line); overflow-wrap: anywhere; }
.run-link { height: auto; min-height: 24px; text-align: left; white-space: normal; }
.instance-timeline { display: grid; gap: 8px; margin: 0; padding-inline-start: 16px; list-style: none; border-inline-start: 2px solid var(--line); }
.instance-timeline li { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 8px; position: relative; }
.instance-timeline li::before { content: ''; position: absolute; inset-inline-start: -21px; inset-block-start: 6px; width: 8px; height: 8px; background: var(--accent); border-radius: 50%; }
.action-form { display: grid; gap: 12px; }
.action-form p { overflow-wrap: anywhere; }
.appointment-options { gap: 0 12px; }
.appointment-options .el-radio { margin: 0; }
.action-footer { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.action-footer > .el-button + .el-button { margin-left: 0; }
@media (max-width: 767px) {
  .detail-heading .el-button, .start-action, .operation-area > :deep(.el-dropdown) .el-button { min-height: 44px; }
  .detail-tabs :deep(.el-tabs__item) { font-size: .875rem; }
}
</style>
