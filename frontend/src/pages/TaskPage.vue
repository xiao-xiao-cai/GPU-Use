<script setup lang="ts">
import { computed, nextTick, onMounted, onScopeDispose, ref, shallowRef, watch } from 'vue'
import AutomationRulesPanel from '../components/AutomationRulesPanel.vue'
import TaskEditor from '../components/TaskEditor.vue'
import {
  notificationHref, query, request, requestId, type AutomationRule, type Channel, type Instance, type Run,
  type Schedule, type Task, type TaskTrashAction, type TaskView, type Template,
} from '../api/client'
import { awaitingInstanceReview, errorMessage, instanceLabels, reviewingShutdown, timeLabel } from '../display'

const props = defineProps<{
  projectId: string
  instances: Instance[]
  selectedRunId: string
  selectedTaskId?: string
  automationInstanceId?: string
  channels?: Record<Channel, boolean>
}>()
const emit = defineEmits<{ selectRun: [id: string]; selectTask: [id: string] }>()
const kindLabels: Record<Run['kind'], string> = {
  stock: '仅通知有货',
  start: '只启动已有实例',
  acquire: '按模板获取实例',
  stop: '关机',
  shutdown: '云端定时关机',
  inspection: '巡检',
  cancel_shutdown: '取消关机预约',
  reboot: '重启实例',
  rename: '重命名实例',
  terminate: '释放实例',
  resize: '更改实例规格',
}
const runLabels: Record<string, string> = {
  queued: '已排队', waiting: '等待中', reconciling: '待核对', manual_review: '待人工核对',
  succeeded: '已完成', reviewed: '已核对', failed: '失败', expired: '窗口已结束', canceled: '已取消', missed: '已错过',
}
const storageStates: Record<string, string> = {
  pending: '等待存储操作', reserved: '等待提交存储订单', accepted: '平台已受理存储操作',
  ready: '共享存储已就绪', attachment_pending: '正在核实挂载', attached: '共享存储已挂载',
  attachment_unverified: '共享存储挂载待核实', uncertain: '存储操作结果待核实', unverified: '存储操作结果待核实',
  rejected: '平台拒绝存储操作', not_dispatched: '存储操作未提交',
}
function reviewPending(run: Run) {
  return reviewingShutdown(run) || run.phase === 'review_shutdown' || run.result?.instance_recheck_pending === true
}
function recheckLabel(run: Run) {
  return run.phase === 'verify_cfs' ? '重新核对共享存储' : '重新核对实例状态'
}
function reviewedRun(run: Run) {
  return run.state === 'reviewed' || (run.result?.shutdown_review?.resolved === true && !run.shutdown_reviewable)
    || run.result?.purchase_review?.outcome === 'not_purchased'
}
function waitingForCapacity(run: Run) {
  return run.state === 'waiting' && !run.phase && Boolean(run.result?.capacity_wait_since)
}
function runStatus(run: Run) {
  if (reviewPending(run)) return '正在核对'
  if (run.result?.storage_state === 'attachment_unverified') return '实例已运行，挂载待核实'
  if (run.business?.label) return run.business.label
  if (run.result?.automation_reminded) return '仅提醒'
  if (waitingForCapacity(run)) return '等待资源'
  if (reviewedRun(run)) return '已核对'
  if (run.result?.shutdown_outcome === 'missed') return '关机结果待处理'
  return runLabels[run.state] ?? run.state
}
function runTagType(run: Run) {
  if (reviewPending(run)) return 'info'
  if (run.result?.storage_state === 'attachment_unverified') return 'warning'
  if (reviewedRun(run)) return 'success'
  if (run.result?.shutdown_outcome === 'missed' || ['reconciling', 'manual_review'].includes(run.state)) return 'warning'
  if (run.state === 'succeeded') return 'success'
  return run.state === 'failed' ? 'danger' : 'info'
}
const taskLabels: Record<Task['status'], string> = {
  active: '已启用', paused: '已暂停', canceled: '已取消', completed: '已完成',
}
const scheduleLabels: Record<Schedule['type'], string> = {
  once: '一次性', daily: '每天', weekly: '每周', interval: '固定间隔',
}
type RunDetail = Run & { operations?: { action: string; state: string }[]; audit?: unknown[]; start_attempts?: number }

const taskViews: Record<TaskView, { label: string; empty: string }> = {
  current: { label: '当前任务', empty: '暂无当前任务。' },
  history: { label: '历史任务', empty: '暂无已归档任务。' },
  trash: { label: '回收站', empty: '回收站为空。' },
}
const activeTab = ref(props.automationInstanceId === undefined ? 'current' : 'automation')
const pageHeading = ref<HTMLElement>()
const runHeading = ref<HTMLElement>()
const runLoading = ref(false)
const runError = ref('')
const runDrawer = ref(false)
let runSource: HTMLElement | undefined
const tasks = shallowRef<Task[]>([])
const trash = shallowRef<Task[]>([])
const taskGroups = computed(() => ({
  current: tasks.value.filter(task => task.view === 'current'),
  history: tasks.value.filter(task => task.view === 'history')
    .sort((a, b) => (b.last_run?.updated_at ?? 0) - (a.last_run?.updated_at ?? 0)),
  trash: trash.value,
}))
const taskDefinitions = computed(() => [...tasks.value, ...trash.value])
const runs = shallowRef<Run[]>([])
const attention = shallowRef<Run[]>()
const templates = shallowRef<Template[]>([])
const selected = shallowRef<RunDetail>()
const loading = ref(false)
const listsLoaded = ref(false)
const listError = ref('')
const attentionError = ref('')
const notice = ref('')
const actionError = ref('')
const workAction = ref('')
const requestingReview = ref('')
const reviewError = shallowRef<{ id: string; message: string }>()
const taskFilter = ref('')
const taskFilterName = ref('')
const taskClock = ref(Date.now() / 1000)
const automationFilter = shallowRef<AutomationRule>()
const reconciliation = ref(false)
const resolutionTarget = ref('')
let requestController: AbortController | undefined
let attentionController: AbortController | undefined
let runController: AbortController | undefined
let disposed = false

async function load() {
  if (!props.projectId || requestController) return
  const controller = new AbortController()
  requestController = controller
  loading.value = true
  try {
    const suffix = query({ project_id: props.projectId })
    const [taskData, trashData, runData, templateData] = await Promise.all([
      request<Task[]>('/tasks' + suffix, { signal: controller.signal }),
      request<Task[]>('/tasks' + query({ project_id: props.projectId, view: 'trash' }), { signal: controller.signal }),
      request<Run[]>('/runs' + query({
        project_id: props.projectId, task_id: taskFilter.value || undefined, automation_rule_id: automationFilter.value?.id,
      }), { signal: controller.signal }),
      request<Template[]>('/templates' + suffix, { signal: controller.signal }),
    ])
    if (controller.signal.aborted) return
    const trashedIds = new Set(trashData.map(task => task.id))
    tasks.value = taskData.filter(task => !trashedIds.has(task.id))
    trash.value = trashData
    taskClock.value = Date.now() / 1000
    runs.value = runData
    templates.value = templateData
    listsLoaded.value = true
    listError.value = ''
  } catch (error) {
    if (!controller.signal.aborted) listError.value = errorMessage(error)
  } finally {
    if (requestController === controller) {
      requestController = undefined
      loading.value = false
    }
  }
}

async function loadAttention() {
  if (!props.projectId || attentionController) return
  const controller = new AbortController()
  attentionController = controller
  try {
    const data = await request<Run[]>('/runs' + query({ project_id: props.projectId, attention: 'true' }), {
      signal: controller.signal,
    })
    if (controller.signal.aborted) return
    attention.value = data
    attentionError.value = ''
  } catch (error) {
    if (!controller.signal.aborted) attentionError.value = errorMessage(error)
  } finally {
    if (attentionController === controller) attentionController = undefined
  }
}

async function loadRun() {
  runController?.abort()
  const id = props.selectedRunId
  if (!id) return
  const controller = new AbortController()
  runController = controller
  runLoading.value = !selected.value
  try {
    const data = await request<RunDetail>('/runs/' + encodeURIComponent(id), { signal: controller.signal })
    if (!controller.signal.aborted) {
      if (data.project_id === props.projectId) {
        selected.value = data
        runError.value = ''
      } else {
        runError.value = '此运行不属于当前项目。切换项目后可重新打开链接。'
      }
    }
  } catch (error) {
    if (!controller.signal.aborted) runError.value = errorMessage(error)
  } finally {
    if (runController === controller) {
      runController = undefined
      runLoading.value = false
    }
  }
}

function openRun(id: string, event?: MouseEvent) {
  if (!runDrawer.value && event?.currentTarget instanceof HTMLElement) runSource = event.currentTarget
  emit('selectRun', id)
}

function closeRun() {
  if (props.selectedRunId) emit('selectRun', '')
}

function restoreRunFocus() {
  const source = runSource
  runSource = undefined
  requestAnimationFrame(() => {
    if (disposed || runDrawer.value || editing.value) return
    const target = source?.isConnected && source.getClientRects().length ? source
      : runHeading.value?.getClientRects().length ? runHeading.value : pageHeading.value
    target?.focus()
  })
}

function filterTask(id: string, name: string) {
  automationFilter.value = undefined
  taskFilterName.value = name
  taskFilter.value = id
  activeTab.value = 'runs'
  void nextTick(() => runHeading.value?.focus())
}

function filterAutomation(rule: AutomationRule) {
  taskFilter.value = ''
  automationFilter.value = rule
  activeTab.value = 'runs'
  void nextTick(() => runHeading.value?.focus())
}

function reloadLists() {
  requestController?.abort()
  requestController = undefined
  void load()
}

function reload() {
  reloadLists()
  attentionController?.abort()
  attentionController = undefined
  void loadAttention()
  if (props.selectedRunId && !runController) void loadRun()
}

watch([taskFilter, automationFilter], () => {
  runs.value = []
  reloadLists()
})
watch(() => props.automationInstanceId, (id, previous) => {
  automationFilter.value = undefined
  if (props.selectedRunId) return
  if (id !== undefined) activeTab.value = 'automation'
  else if (previous !== undefined && activeTab.value === 'automation') activeTab.value = 'current'
})
watch(() => props.selectedRunId, id => {
  selected.value = undefined
  runError.value = ''
  actionError.value = ''
  reconciliation.value = false
  resolutionTarget.value = ''
  runDrawer.value = Boolean(id)
  runController?.abort()
  if (id) {
    if (!runSource) activeTab.value = 'runs'
    void loadRun()
  }
}, { immediate: true })
onMounted(() => {
  void load()
  void loadAttention()
})
const timer = window.setInterval(() => {
  if (document.hidden) return
  void load()
  void loadAttention()
  if (props.selectedRunId && !runController && !workAction.value && !requestingReview.value) void loadRun()
}, 8000)
onScopeDispose(() => {
  disposed = true
  window.clearInterval(timer)
  requestController?.abort()
  attentionController?.abort()
  runController?.abort()
})

const editing = ref(false)
const editingTask = shallowRef<Task>()
const createButton = ref<{ $el: HTMLElement }>()
const routeTask = computed(() => tasks.value.find(task => task.id === props.selectedTaskId && task.status !== 'canceled'))

function openEditor(task?: Task) {
  editingTask.value = task
  editing.value = true
}

async function closeEditor() {
  const id = editingTask.value?.id
  editing.value = false
  if (id && id === props.selectedTaskId) emit('selectTask', '')
  await nextTick()
  const target = id ? document.querySelector<HTMLElement>('[data-task-menu="' + CSS.escape(id) + '"]') : undefined
  const focusTarget = target ?? createButton.value?.$el ?? pageHeading.value
  focusTarget?.focus()
}

watch([() => props.selectedTaskId, listsLoaded], ([id, loaded], [previous]) => {
  if (!id) {
    if (previous && editingTask.value?.id === previous) editing.value = false
    return
  }
  if (loaded && routeTask.value) {
    activeTab.value = routeTask.value.view
    openEditor(routeTask.value)
  } else {
    editing.value = false
  }
}, { immediate: true })

function taskSaved() {
  void closeEditor()
  notice.value = '任务已保存。已排队的运行保留提交时的配置。'
  reload()
}

const confirmation = ref(false)
const dialogError = ref('')
const controls = {
  trigger: '立即执行', pause: '暂停', resume: '启用', trash: '移入回收站', restore: '恢复任务', purge: '彻底删除',
}
type TaskControl = keyof typeof controls
const confirmTarget = ref<{ task: Task; action: TaskControl; key: string }>()

function isTrashAction(action: TaskControl): action is TaskTrashAction {
  return action === 'trash' || action === 'restore' || action === 'purge'
}

function trashExpired(task: Task) {
  return task.purge_at !== null && task.purge_at <= taskClock.value
}

function confirmControl(task: Task, action: TaskControl) {
  dialogError.value = ''
  confirmTarget.value = { task, action, key: requestId() }
  confirmation.value = true
}

async function executeControl() {
  if (!confirmTarget.value || workAction.value) return
  const { task, action, key } = confirmTarget.value
  const path = '/tasks/' + encodeURIComponent(task.id) + (action === 'restore' || action === 'purge' ? '/trash/' : '/') + action
  workAction.value = action
  dialogError.value = ''
  try {
    const result = await request<Run | Task | { purged: true }>(path, {
      method: 'POST',
      body: isTrashAction(action) ? { project_id: props.projectId, expected_version: task.version }
        : action === 'trigger' ? { request_id: key } : undefined,
    })
    if (disposed) return
    confirmation.value = false
    if (action === 'trigger' && 'state' in result) {
      notice.value = result.state === 'missed' ? result.reason || '本次运行已跳过。' : '运行已提交，等待后台处理。'
      openRun(result.id)
    } else {
      if (action === 'trash') notice.value = '任务已移入回收站，7 天内可恢复。'
      else if (action === 'restore' && 'view' in result) {
        notice.value = result.status === 'paused' ? '任务已恢复为暂停，可在当前任务中启用。'
          : result.view === 'history' ? '任务已恢复到历史任务。' : '任务已恢复，可在当前任务查看收尾进度。'
      } else if (action === 'purge') notice.value = '任务配置已彻底删除，历史运行记录已保留。'
      else notice.value = controls[action] + '操作已保存。'
    }
    reload()
  } catch (error) {
    if (!disposed) {
      dialogError.value = errorMessage(error)
      if (isTrashAction(action)) reloadLists()
    }
  } finally {
    workAction.value = ''
  }
}

const resolution = ref<'bind' | 'release'>('bind')
const resolvedInstance = ref('')
const resolutionNote = ref('')
const verifiedOrder = ref(false)
const canResolve = computed(() => selected.value?.kind === 'acquire'
  && ['verify_create', 'manual_bind'].includes(selected.value.phase ?? '')
  && (selected.value.state === 'manual_review'
    || (selected.value.state === 'reconciling' && selected.value.result?.reviewable === true)))
const releaseAvailable = computed(() => canResolve.value
  && selected.value?.operations !== undefined
  && !selected.value.operations.some(operation => operation.action === 'create' && operation.state === 'accepted'))
const startAttempts = computed(() => selected.value?.start_attempts ?? selected.value?.operations?.filter(operation =>
  operation.action === 'start' && operation.state !== 'not_dispatched').length ?? 0)
const owningTask = computed(() => tasks.value.find(task => task.id === selected.value?.task_id))
const selectedSourceTaskId = computed(() => selected.value?.source_task_id || selected.value?.task_id)

function showRelatedRuns() {
  if (!selected.value || !selectedSourceTaskId.value) return
  const id = selectedSourceTaskId.value
  const name = taskDefinitions.value.find(task => task.id === id)?.name ?? selected.value.name
  closeRun()
  filterTask(id, name)
}
const canPausePurchase = computed(() => selected.value?.kind === 'acquire' && !selected.value.stopped
  && ['queued', 'waiting', 'reconciling', 'manual_review'].includes(selected.value.state)
  && owningTask.value && ['active', 'completed'].includes(owningTask.value.status))
const canCancelAction = computed(() => {
  const run = selected.value
  return run && (run.request_id?.startsWith('action:') || run.snapshot?.config?.capacity_retry)
    && !run.stopped && !reviewPending(run) && ['queued', 'waiting', 'reconciling'].includes(run.state)
})

async function cancelAction() {
  if (!selected.value || !canCancelAction.value || workAction.value) return
  const id = selected.value.id
  runController?.abort()
  workAction.value = 'cancel-run'
  actionError.value = ''
  try {
    const run = await request<Run>('/runs/' + encodeURIComponent(id) + '/cancel', { method: 'POST' })
    if (disposed) return
    if (props.selectedRunId === id) selected.value = { ...selected.value, ...run }
    reload()
  } catch (error) {
    if (!disposed && props.selectedRunId === id) actionError.value = errorMessage(error)
  } finally {
    workAction.value = ''
  }
}

function openResolution(action: 'bind' | 'release') {
  if (!selected.value || !canResolve.value || workAction.value) return
  resolutionTarget.value = selected.value.id
  resolution.value = action
  resolvedInstance.value = ''
  resolutionNote.value = ''
  verifiedOrder.value = false
  actionError.value = ''
  reconciliation.value = true
}

async function resolveRun() {
  if (!selected.value || !canResolve.value || workAction.value || !resolutionNote.value.trim()) return
  if (resolution.value === 'release' && (!releaseAvailable.value || !verifiedOrder.value)) return
  if (resolution.value === 'bind' && !resolvedInstance.value.trim()) return
  const id = resolutionTarget.value
  if (id !== props.selectedRunId || id !== selected.value.id) return
  runController?.abort()
  workAction.value = resolution.value
  actionError.value = ''
  try {
    const body = {
      action: resolution.value,
      instance_id: resolution.value === 'bind' ? resolvedInstance.value : undefined,
      confirmed_no_order: resolution.value === 'release' ? verifiedOrder.value : undefined,
      note: resolutionNote.value.trim(),
    }
    const result = await request<RunDetail>('/runs/' + encodeURIComponent(id) + '/reconcile', { method: 'POST', body })
    if (props.selectedRunId === id) selected.value = { ...selected.value, ...result }
    reconciliation.value = false
    reload()
  } catch (error) {
    if (props.selectedRunId === id) actionError.value = errorMessage(error)
  } finally {
    workAction.value = ''
  }
}

function attentionTaskLink(run: Run) {
  const destination = run.business?.destination
  return destination?.page === 'tasks' && destination.task_id && destination.project_id === props.projectId
    ? notificationHref(destination) : ''
}

function canReviewShutdown(run: Run) {
  return !reviewPending(run) && (run.shutdown_reviewable === true
    || (run.kind === 'cancel_shutdown' && run.state === 'manual_review'))
}

const shutdownReview = computed(() => selected.value?.result?.shutdown_review)
const shutdownObservation = computed(() => selected.value?.result?.shutdown_observation)
const instanceReview = computed(() => selected.value?.result?.instance_review)
const originalShutdownAt = computed(() => {
  const run = selected.value
  if (!run) return null
  const time = shutdownReview.value?.original_shutdown_at ?? run.result?.shutdown_at
    ?? (run.kind === 'cancel_shutdown' ? run.snapshot?.config?.expected_stop_at : run.snapshot?.config?.shutdown_at)
    ?? (run.kind === 'shutdown' ? run.scheduled_at : null)
  return typeof time === 'number' && time > 0 ? time : null
})
const shutdownInstanceLink = computed(() => {
  const run = selected.value
  if (!run || (!['shutdown', 'cancel_shutdown'].includes(run.kind)
    && !run.result?.shutdown_confirmation && !shutdownReview.value)) return ''
  const id = run.result?.instance_id ?? run.snapshot?.config?.instance_id
  return typeof id === 'string' && id
    ? notificationHref({ page: 'instances', project_id: run.project_id, instance_id: id }) : ''
})
const createdInstanceLink = computed(() => {
  const run = selected.value
  const id = run?.result?.instance_id
  return run?.kind === 'acquire' && typeof id === 'string' && id
    ? notificationHref({ page: 'instances', project_id: run.project_id, instance_id: id }) : ''
})
const shutdownInstanceLabel = computed(() => {
  const run = selected.value
  if (!run || !canReviewShutdown(run) || run.kind === 'cancel_shutdown') return '查看实例预约'
  if (run.result?.shutdown_outcome === 'missed' || run.state === 'missed') return '前往实例处理关机'
  return '前往实例设置或替换预约'
})
const keepAppointmentAt = computed(() => {
  const run = selected.value
  const review = shutdownReview.value
  const at = review?.cloud_shutdown_at
  return run && canReviewShutdown(run) && review?.outcome === 'different' && at && at > Date.now() / 1000 ? at : null
})

async function submitReview(run: Run, action: 'review-shutdown' | 'recheck', event: MouseEvent, keepAt?: number) {
  if (requestingReview.value || !(action === 'recheck' ? awaitingInstanceReview(run) : canReviewShutdown(run))) return
  const id = run.id
  requestingReview.value = id
  reviewError.value = undefined
  if (!runDrawer.value) openRun(id, event)
  try {
    const result = await request<Run>('/runs/' + encodeURIComponent(id) + '/' + action, {
      method: 'POST', body: keepAt === undefined ? undefined : { keep_stop_at: keepAt },
    })
    if (disposed) return
    runs.value = runs.value.map(item => item.id === id ? { ...item, ...result } : item)
    if (props.selectedRunId === id) {
      runController?.abort()
      runController = undefined
      if (selected.value?.id === id) selected.value = { ...selected.value, ...result, business: result.business }
    }
    reload()
  } catch (error) {
    if (!disposed) reviewError.value = { id, message: errorMessage(error) }
  } finally {
    requestingReview.value = ''
  }
}

const dispositionLabels: Record<string, string> = {
  reused: '复用运行实例',
  started: '启动已有实例',
  created: '新建实例',
}
const shutdownLabels: Record<string, string> = {
  queued: '等待登记',
  unconfirmed: '预约未确认',
  confirmed: '云端预约已确认',
  canceled: '已取消后续预约',
  fulfilled: '已提前关机',
  superseded: '已改期',
  released: '实例已释放',
}
const shutdownOutcomeLabels: Record<string, string> = {
  confirmed: '已确认关机', missed: '错过关机', early: '已提前关机',
  canceled: '已撤销', superseded: '已改期', released: '实例已释放',
}
const shutdownCauseLabels: Record<string, string> = {
  idle_stop: '空载关机', runtime_stop: '运行时长规则', cardless_idle_stop: '无卡 CPU 低负载关机', manual: '手动关机',
  scheduled: '计划关机', observed: '平台回读确认',
}
function resultValue(run: Run, key: string) {
  const value = run.result?.[key]
  return typeof value === 'string' || typeof value === 'number' ? value : ''
}
</script>


<template>
  <header class="page-heading">
    <h1 ref="pageHeading" tabindex="-1">任务中心</h1>
    <ElButton v-if="!editing && !selectedTaskId && activeTab !== 'automation'" ref="createButton" type="primary" :disabled="loading && !tasks.length"
      @click="openEditor()">创建任务</ElButton>
  </header>
  <ElAlert v-if="listError" :title="listError" :closable="false" type="error" show-icon>
    <ElButton size="small" @click="reloadLists">重试</ElButton>
  </ElAlert>
  <ElAlert v-if="notice" :title="notice" type="success" show-icon @close="notice = ''" />

  <TaskEditor v-if="editing" :key="editingTask?.id" :project-id="projectId" :instances="instances" :channels="channels"
    :templates="templates" :task="editingTask" @close="closeEditor" @saved="taskSaved" @template-saved="reloadLists" />
  <ElSkeleton v-else-if="selectedTaskId && !listsLoaded && !listError" animated :rows="2" aria-label="正在读取链接中的任务" />
  <ElAlert v-else-if="selectedTaskId && listsLoaded && !routeTask" title="当前项目中没有可编辑的对应任务。"
    type="warning" :closable="false" show-icon>
    <ElButton size="small" @click="emit('selectTask', '')">返回任务列表</ElButton>
  </ElAlert>
  <div v-show="!editing" class="task-content">
    <section class="panel" aria-labelledby="task-attention-heading">
      <h2 id="task-attention-heading">待处理事项</h2>
      <ElAlert v-if="attentionError" title="待处理事项读取失败" :description="attentionError"
        type="error" :closable="false" show-icon>
        <ElButton size="small" @click="loadAttention">重新读取</ElButton>
      </ElAlert>
      <ElSkeleton v-if="!attention && !attentionError" animated :rows="1" aria-label="正在读取待处理事项" />
      <p v-else-if="attention?.length === 0 && !attentionError" class="muted">当前项目没有待处理事项。</p>
      <ul v-else-if="attention?.length" class="row-list">
        <li v-for="run in attention" :key="run.id" class="attention-item">
          <div class="attention-text">
            <p><strong>{{ run.name }}</strong> · {{ runStatus(run) }}</p>
            <p v-if="run.business?.detail ?? run.reason">{{ run.business?.detail ?? run.reason }}</p>
            <p class="mono muted">更新于 {{ timeLabel(run.business?.observed_at ?? run.updated_at) }}</p>
          </div>
          <div class="run-actions">
            <a v-if="attentionTaskLink(run)" :href="attentionTaskLink(run)">编辑任务</a>
            <ElButton link type="primary" @click="openRun(run.id, $event)">查看运行</ElButton>
            <ElButton v-if="canReviewShutdown(run)" size="small" plain :loading="requestingReview === run.id"
              :disabled="!!requestingReview" @click="submitReview(run, 'review-shutdown', $event)">重新核对云端预约</ElButton>
            <ElButton v-else-if="awaitingInstanceReview(run)" size="small" plain :loading="requestingReview === run.id"
              :disabled="!!requestingReview" @click="submitReview(run, 'recheck', $event)">{{ recheckLabel(run) }}</ElButton>
          </div>
          <p v-if="reviewError?.id === run.id" class="review-error" role="alert">{{ reviewError.message }}</p>
        </li>
      </ul>
    </section>

    <section class="panel task-lists">
      <ElTabs v-model="activeTab" aria-label="任务中心视图">
        <ElTabPane v-for="(view, group) in taskViews" :key="group" :name="group" :label="view.label">
          <ElSkeleton v-if="activeTab === group && loading && !listsLoaded" animated :rows="3" aria-label="正在读取任务" />
          <ElTable v-else-if="activeTab === group && listsLoaded" :data="taskGroups[group]" row-key="id"
            :empty-text="view.empty" :aria-label="view.label" :scrollbar-tabindex="0">
            <!-- @vue-generic {Task} -->
            <ElTableColumn label="任务" min-width="128">
              <template #default="{ row: task }">
                <strong>{{ task.name }}</strong>
                <small class="muted">{{ kindLabels[task.kind] }} · {{ scheduleLabels[task.schedule.type] }}
                  <template v-if="task.kind === 'start'"> · {{ task.config.boot_mode === 'without_gpu' ? `无卡 ${task.config.without_gpu_spec} 档` : '有卡' }}</template>
                </small>
                <small v-if="group === 'current' && task.next_at" class="muted">下次 {{ timeLabel(task.next_at) }}</small>
                <small v-else-if="group === 'history' && task.last_run" class="muted">
                  最后运行 {{ timeLabel(task.last_run.updated_at) }}
                </small>
                <template v-else-if="group === 'trash'">
                  <small class="muted">删除于 {{ timeLabel(task.deleted_at) }}</small>
                  <small class="muted">到期于 {{ timeLabel(task.purge_at) }}</small>
                </template>
              </template>
            </ElTableColumn>
            <!-- @vue-generic {Task} -->
            <ElTableColumn label="状态" min-width="118">
              <template #default="{ row: task }">
                <template v-if="group === 'trash'">
                  <ElTag size="small" :type="task.pending_reason ? 'warning' : 'info'">
                    {{ trashExpired(task) ? (task.pending_reason ? '已到期，等待核对' : '已到期，等待清理')
                      : task.pending_reason ? '正在收尾' : '可恢复' }}
                  </ElTag>
                </template>
                <ElButton v-else-if="group === 'history' && task.last_run" link type="primary"
                  @click="openRun(task.last_run.id, $event)">
                  {{ runLabels[task.last_run.state] ?? task.last_run.state }}
                </ElButton>
                <ElTag v-else size="small" :type="task.status === 'active' ? 'success' : 'info'">
                  {{ task.status === 'completed' && task.pending_reason ? '正在收尾' : taskLabels[task.status] }}
                </ElTag>
                <small v-if="task.pending_reason" class="muted">{{ task.pending_reason }}</small>
              </template>
            </ElTableColumn>
            <!-- @vue-generic {Task} -->
            <ElTableColumn label="操作" min-width="80">
              <template #default="{ row: task }">
                <ElDropdown trigger="click" placement="bottom-end">
                  <ElButton :data-task-menu="task.id" size="small" :aria-label="'操作 ' + task.name">操作</ElButton>
                  <template #dropdown>
                    <ElDropdownMenu>
                      <template v-if="group === 'trash'">
                        <ElDropdownItem v-if="!trashExpired(task)" @click="confirmControl(task, 'restore')">恢复任务</ElDropdownItem>
                        <ElDropdownItem :disabled="!!task.pending_reason" @click="confirmControl(task, 'purge')">彻底删除</ElDropdownItem>
                      </template>
                      <template v-else>
                        <ElDropdownItem v-if="task.status !== 'canceled'" @click="openEditor(task)">
                          {{ group === 'history' ? '重新排期' : '编辑' }}
                        </ElDropdownItem>
                        <ElDropdownItem v-if="task.status === 'active' || task.status === 'completed'"
                          @click="confirmControl(task, 'trigger')">立即执行</ElDropdownItem>
                        <ElDropdownItem v-if="task.status === 'active'" @click="confirmControl(task, 'pause')">暂停</ElDropdownItem>
                        <ElDropdownItem v-if="task.status === 'paused'" @click="confirmControl(task, 'resume')">启用</ElDropdownItem>
                        <ElDropdownItem @click="confirmControl(task, 'trash')">移入回收站</ElDropdownItem>
                      </template>
                      <ElDropdownItem @click="filterTask(task.id, task.name)">运行记录</ElDropdownItem>
                    </ElDropdownMenu>
                  </template>
                </ElDropdown>
              </template>
            </ElTableColumn>
          </ElTable>
        </ElTabPane>
        <ElTabPane name="automation" label="自动化规则" lazy>
          <AutomationRulesPanel v-if="activeTab === 'automation'" :key="automationInstanceId" :project-id="projectId" :instance-id="automationInstanceId"
            :instances="instances" :channels="channels" @history="filterAutomation" />
        </ElTabPane>
        <ElTabPane name="runs" label="运行记录" lazy>
          <div class="section-heading run-filters">
            <h2 ref="runHeading" tabindex="-1">运行记录</h2>
            <div v-if="automationFilter" class="run-actions">
              <span>规则 {{ automationFilter.name }}</span>
              <ElButton size="small" @click="automationFilter = undefined">清除筛选</ElButton>
            </div>
            <ElSelect v-else v-model="taskFilter" :empty-values="[null, undefined]" aria-label="按任务筛选运行">
              <ElOption label="全部任务" value="" />
              <ElOption v-for="task in taskDefinitions" :key="task.id" :label="task.name" :value="task.id" />
              <ElOption v-if="taskFilter && !taskDefinitions.some(task => task.id === taskFilter)"
                :label="taskFilterName || taskFilter" :value="taskFilter" />
            </ElSelect>
          </div>
          <ElSkeleton v-if="loading && !runs.length" animated :rows="3" aria-label="正在读取运行记录" />
          <ElTable v-else :data="runs" row-key="id" empty-text="暂无运行记录。"
            aria-label="运行记录" :scrollbar-tabindex="0">
            <!-- @vue-generic {Run} -->
            <ElTableColumn label="运行" min-width="160">
              <template #default="{ row: run }">
                <ElButton link type="primary" class="run-name" @click="openRun(run.id, $event)">{{ run.name }}</ElButton>
                <small class="mono muted">{{ timeLabel(run.scheduled_at) }}</small>
                <small v-if="run.snapshot?.trigger" class="muted">{{ run.reason }}</small>
              </template>
            </ElTableColumn>
            <!-- @vue-generic {Run} -->
            <ElTableColumn label="状态" min-width="144">
              <template #default="{ row: run }">
                <ElTag size="small" :type="runTagType(run)">
                  {{ runStatus(run) }}
                </ElTag>
                <small v-if="run.state === 'waiting' && run.next_check" class="muted">下次尝试 {{ timeLabel(run.next_check) }}</small>
                <small v-if="run.result?.storage_state && run.result.storage_state !== 'attachment_unverified'" class="muted">
                  {{ storageStates[run.result.storage_state] }}
                </small>
              </template>
            </ElTableColumn>
          </ElTable>
        </ElTabPane>
      </ElTabs>
    </section>
  </div>

  <ElDrawer v-model="runDrawer" append-to-body size="min(540px, 100vw)" title="运行详情" destroy-on-close
    @close="closeRun" @closed="restoreRunFocus">
    <div class="run-drawer">
      <ElSkeleton v-if="runLoading" animated :rows="3" aria-label="正在读取运行详情" />
      <ElAlert v-if="runError" :title="runError" :closable="false" type="error" show-icon>
        <ElButton size="small" @click="loadRun">重试</ElButton>
      </ElAlert>
      <template v-if="selected">
        <div>
          <h2>{{ selected.name }}</h2>
          <p class="muted mono">{{ selected.id }}</p>
        </div>
        <ElAlert v-if="selected.business?.detail ?? selected.reason" :title="selected.business?.detail ?? selected.reason ?? ''"
          :closable="false" show-icon
          :type="reviewedRun(selected) ? 'success' : ['manual_review', 'failed'].includes(selected.state) ? 'warning' : 'info'" />
        <ElAlert v-if="actionError" :title="actionError" :closable="false" type="error" show-icon />
        <div v-if="canReviewShutdown(selected) || awaitingInstanceReview(selected)
          || shutdownInstanceLink || attentionTaskLink(selected)" class="run-actions">
          <ElButton v-if="canReviewShutdown(selected)" type="primary" :loading="requestingReview === selected.id"
            :disabled="!!requestingReview" @click="submitReview(selected, 'review-shutdown', $event)">重新核对云端预约</ElButton>
          <ElButton v-if="awaitingInstanceReview(selected)" type="primary" :loading="requestingReview === selected.id"
            :disabled="!!requestingReview" @click="submitReview(selected, 'recheck', $event)">{{ recheckLabel(selected) }}</ElButton>
          <ElButton v-if="keepAppointmentAt" plain :disabled="!!requestingReview"
            @click="submitReview(selected, 'review-shutdown', $event, keepAppointmentAt ?? undefined)">
            保留 {{ timeLabel(keepAppointmentAt) }} 的预约
          </ElButton>
          <a v-if="shutdownInstanceLink" :href="shutdownInstanceLink">{{ shutdownInstanceLabel }}</a>
          <a v-if="attentionTaskLink(selected)" :href="attentionTaskLink(selected)">编辑原任务</a>
        </div>
        <p v-if="shutdownInstanceLink && canReviewShutdown(selected)" class="muted">
          处理实例后，返回此运行核对云端结果。
        </p>
        <p v-if="reviewPending(selected)" class="muted" role="status">后台正在读取云端状态。</p>
        <ElAlert v-if="reviewError?.id === selected.id" :title="reviewError.message" :closable="false" type="error" show-icon />
        <div v-if="canCancelAction || canPausePurchase" class="run-actions">
          <ElButton :disabled="!!workAction"
            @click="canPausePurchase && owningTask ? confirmControl(owningTask, 'pause') : cancelAction()">
            停止后续尝试
          </ElButton>
          <p class="muted">后台会继续核对已提交的云端请求。</p>
        </div>
        <p v-else-if="selected.stopped && selected.phase && !reviewPending(selected)
          && ['reconciling', 'manual_review'].includes(selected.state)" class="muted">
          后续尝试已停止，已提交的云端请求仍可查看。
        </p>
        <div v-if="canResolve" class="run-actions">
          <ElButton :disabled="!!workAction" @click="openResolution('bind')">绑定已核实实例</ElButton>
          <ElButton v-if="releaseAvailable" type="danger" plain :disabled="!!workAction" @click="openResolution('release')">
            确认未成交并释放预留
          </ElButton>
          <p v-else-if="selected.operations?.some(operation => operation.action === 'create' && operation.state === 'accepted')"
            class="muted">平台已确认成交，可绑定已核实实例。</p>
        </div>
        <ElDescriptions border :column="1">
          <ElDescriptionsItem label="当前状态">{{ runStatus(selected) }}</ElDescriptionsItem>
          <ElDescriptionsItem v-if="originalShutdownAt"
            :label="selected.kind === 'cancel_shutdown' ? '原关机预约（北京时间）' : '目标关机时间（北京时间）'">
            {{ timeLabel(originalShutdownAt) }}
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="selected.kind === 'shutdown' && resultValue(selected, 'cloud_shutdown_at')" label="原预约登记回执">
            {{ timeLabel(Number(resultValue(selected, 'cloud_shutdown_at'))) }}
          </ElDescriptionsItem>
          <template v-if="shutdownReview">
            <ElDescriptionsItem label="当前云端预约（北京时间）">
              {{ (shutdownReview.schedule_known ?? shutdownReview.outcome !== 'unknown')
                ? (shutdownReview.cloud_shutdown_at ? timeLabel(shutdownReview.cloud_shutdown_at) : '无预约')
                : '未能确认' }}
            </ElDescriptionsItem>
            <ElDescriptionsItem v-if="shutdownReview.instance_state" label="实例状态">
              {{ instanceLabels[shutdownReview.instance_state] ?? shutdownReview.instance_state }}
            </ElDescriptionsItem>
            <ElDescriptionsItem v-if="shutdownReview.stop_time" label="回读关机时间">{{ timeLabel(shutdownReview.stop_time) }}</ElDescriptionsItem>
            <ElDescriptionsItem v-if="shutdownReview.detail" label="预约核对结果">{{ shutdownReview.detail }}</ElDescriptionsItem>
            <ElDescriptionsItem label="预约核对时间（北京时间）">{{ timeLabel(shutdownReview.checked_at) }}</ElDescriptionsItem>
          </template>
          <ElDescriptionsItem v-if="selected.result?.purchase_review" label="订单核对">
            已确认未成交，预留已释放 · {{ timeLabel(selected.result.purchase_review.checked_at) }}
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="selected.result?.task_config_resolution" label="配置核对">
            已保存任务版本 {{ selected.result.task_config_resolution.task_version }} · {{ timeLabel(selected.result.task_config_resolution.checked_at) }}
          </ElDescriptionsItem>
          <template v-if="instanceReview">
            <ElDescriptionsItem label="回读实例状态">
              {{ instanceReview.state ? instanceLabels[instanceReview.state] ?? instanceReview.state : '未能确认' }}
            </ElDescriptionsItem>
            <ElDescriptionsItem v-if="instanceReview.name" label="回读实例名称">{{ instanceReview.name }}</ElDescriptionsItem>
            <ElDescriptionsItem v-if="instanceReview.start_time" label="回读开机时间">{{ timeLabel(instanceReview.start_time) }}</ElDescriptionsItem>
            <ElDescriptionsItem v-if="instanceReview.stop_time" label="回读关机时间">{{ timeLabel(instanceReview.stop_time) }}</ElDescriptionsItem>
            <ElDescriptionsItem v-if="instanceReview.detail" label="实例核对结果">{{ instanceReview.detail }}</ElDescriptionsItem>
            <ElDescriptionsItem label="实例核对时间（北京时间）">{{ timeLabel(instanceReview.checked_at) }}</ElDescriptionsItem>
          </template>
          <ElDescriptionsItem label="计划执行">{{ timeLabel(selected.scheduled_at) }}</ElDescriptionsItem>
          <ElDescriptionsItem v-if="selected.kind === 'start'" label="目标开机模式">
            {{ selected.snapshot?.config?.boot_mode === 'without_gpu' ? `无卡 ${selected.snapshot.config.without_gpu_spec} 档` : '有卡' }}
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="selected.snapshot?.config?.shutdown_after_seconds" label="每轮运行时长">
            {{ Number((selected.snapshot.config.shutdown_after_seconds / 60).toFixed(2)) }} 分钟
          </ElDescriptionsItem>
          <ElDescriptionsItem :label="selected.snapshot?.config?.capacity_retry ? '首次执行窗口截止' : '执行截止'">
            {{ timeLabel(selected.deadline_at) }}
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="selected.snapshot?.config?.capacity_retry" label="开机尝试次数">{{ startAttempts }}</ElDescriptionsItem>
          <template v-if="selected.result?.capacity_wait_since">
            <ElDescriptionsItem label="首次资源不足">{{ timeLabel(selected.result.capacity_wait_since) }}</ElDescriptionsItem>
            <ElDescriptionsItem label="资源不足次数">{{ selected.result.capacity_attempts }}</ElDescriptionsItem>
          </template>
          <ElDescriptionsItem v-if="selected.state === 'waiting' && selected.next_check" label="下次尝试">
            {{ timeLabel(selected.next_check) }}
          </ElDescriptionsItem>
          <template v-if="selected.snapshot?.trigger">
            <ElDescriptionsItem label="触发条件">
              {{ selected.snapshot.trigger.kind === 'runtime_stop' ? '最长运行时间' :
                selected.snapshot.trigger.kind === 'cardless_idle_stop' ? '无卡 CPU 低负载关机' : '连续低负载关机' }}
            </ElDescriptionsItem>
            <ElDescriptionsItem v-if="selected.snapshot.trigger.started_at" label="本次开机">
              {{ timeLabel(selected.snapshot.trigger.started_at) }}
            </ElDescriptionsItem>
            <ElDescriptionsItem v-if="selected.snapshot.trigger.since_at && selected.snapshot.trigger.kind !== 'runtime_stop'"
              label="低负载始于">{{ timeLabel(selected.snapshot.trigger.since_at) }}</ElDescriptionsItem>
            <ElDescriptionsItem v-if="selected.snapshot.trigger.sampled_at" label="判定所用最近样本">
              {{ timeLabel(selected.snapshot.trigger.sampled_at) }}
            </ElDescriptionsItem>
          </template>
          <ElDescriptionsItem label="更新时间">{{ timeLabel(selected.updated_at) }}</ElDescriptionsItem>
          <ElDescriptionsItem v-if="resultValue(selected, 'instance_id')" label="实例 ID">
            <a v-if="createdInstanceLink" :href="createdInstanceLink" class="mono">{{ resultValue(selected, 'instance_id') }}</a>
            <span v-else class="mono">{{ resultValue(selected, 'instance_id') }}</span>
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="selected.result?.storage_state" label="共享存储">
            {{ storageStates[selected.result.storage_state] || '共享存储状态待核实' }}
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="selected.result?.storage_size_gib != null" label="共享存储容量">
            {{ selected.result.storage_size_gib }} GiB
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="selected.result?.storage_quote != null" label="共享存储报价">
            ¥{{ selected.result.storage_quote }}
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="dispositionLabels[resultValue(selected, 'disposition')]" label="取得方式">
            {{ dispositionLabels[resultValue(selected, 'disposition')] }}
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="resultValue(selected, 'shutdown_confirmation')" label="关联关机预约">
            {{ shutdownLabels[resultValue(selected, 'shutdown_confirmation')] }}
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="resultValue(selected, 'shutdown_outcome')" label="原计划关机结果">
            {{ shutdownOutcomeLabels[resultValue(selected, 'shutdown_outcome')] }}
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="selected.result?.actual_stop_at" label="实际关机">
            {{ timeLabel(selected.result.actual_stop_at) }}
            <template v-if="selected.result.shutdown_cause"> · {{ shutdownCauseLabels[selected.result.shutdown_cause] }}</template>
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="selected.result?.shutdown_cleanup_pending" label="云端预约">
            旧预约清理待确认
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="shutdownObservation" label="原计划关机观测">
            {{ timeLabel(shutdownObservation.checked_at) }}
            <template v-if="shutdownObservation.instance_state">
              · {{ instanceLabels[shutdownObservation.instance_state] ?? shutdownObservation.instance_state }}
            </template>
          </ElDescriptionsItem>
        </ElDescriptions>
        <ElButton v-if="selected.shutdown_followup_id" type="primary" link
          @click="openRun(selected.shutdown_followup_id)">查看关联关机运行</ElButton>
        <ElButton v-if="selectedSourceTaskId && taskFilter !== selectedSourceTaskId" type="primary" link
          @click="showRelatedRuns">查看此任务的运行记录</ElButton>
        <details v-if="selected.result || selected.operations?.length || selected.audit?.length">
          <summary>{{ selected.start_attempts && selected.start_attempts > (selected.operations?.length ?? 0)
            ? '运行回执（含最近 100 条操作）' : '完整运行回执' }}</summary>
          <pre class="record">{{ JSON.stringify({ state: selected.state, reason: selected.reason, result: selected.result, operations: selected.operations, audit: selected.audit }, null, 2) }}</pre>
        </details>
      </template>
    </div>
  </ElDrawer>

  <ElDialog v-model="confirmation" append-to-body width="min(440px, calc(100vw - 32px))"
    :title="confirmTarget ? controls[confirmTarget.action] : '确认任务操作'"
    :close-on-click-modal="!workAction" :close-on-press-escape="!workAction" :show-close="!workAction">
    <div v-if="confirmTarget" class="task-dialog">
      <p>「{{ confirmTarget.task.name }}」</p>
      <p v-if="confirmTarget.action === 'trigger' && confirmTarget.task.kind === 'acquire'">
        后台将使用本项目与任务配置申请实例。{{ confirmTarget.task.config.skip_if_match ? '先检查已有匹配实例。' : '已允许新建相同规格实例。' }}报价上限 ¥{{ confirmTarget.task.config.max_price }}。
      </p>
      <p v-if="confirmTarget.action === 'pause'">后台会暂停后续尝试，继续核对在途申请，并保留本轮已登记的兜底预约。</p>
      <p v-if="confirmTarget.action === 'trash'">
        删除后停止后续调度，后台会核对在途操作并撤销本任务持有的预约。任务可在 7 天内恢复。
      </p>
      <p v-if="confirmTarget.action === 'restore'">有未来计划的任务会恢复为暂停，已结束的任务回到历史任务。</p>
      <p v-if="confirmTarget.action === 'purge'">任务配置将永久删除，历史运行和审计记录会保留。</p>
      <ElAlert v-if="dialogError" :title="dialogError" type="error" :closable="false" show-icon />
    </div>
    <template #footer>
      <ElButton :disabled="!!workAction" @click="confirmation = false">返回</ElButton>
      <ElButton v-if="confirmTarget" :type="confirmTarget.action === 'purge' || confirmTarget.action === 'trash' ? 'danger' : 'primary'"
        :loading="!!workAction" @click="executeControl">确认{{ controls[confirmTarget.action] }}</ElButton>
    </template>
  </ElDialog>

  <ElDialog v-model="reconciliation" append-to-body width="min(480px, calc(100vw - 32px))"
    :title="resolution === 'bind' ? '绑定已核实实例' : '释放采购预留'"
    :close-on-click-modal="!workAction" :close-on-press-escape="!workAction" :show-close="!workAction">
    <ElForm label-position="top" @submit.prevent="resolveRun">
      <ElFormItem v-if="resolution === 'bind'" label="经核实的实例 ID">
        <ElInput v-model.trim="resolvedInstance" required />
      </ElFormItem>
      <ElCheckbox v-else v-model="verifiedOrder">我已从平台订单确认此次申请未成交</ElCheckbox>
      <ElFormItem label="核对说明">
        <ElInput v-model.trim="resolutionNote" maxlength="500" required />
      </ElFormItem>
      <ElAlert v-if="actionError" :title="actionError" type="error" :closable="false" show-icon />
      <div class="task-dialog-actions">
        <ElButton :disabled="!!workAction" @click="reconciliation = false">返回</ElButton>
        <ElButton :type="resolution === 'bind' ? 'primary' : 'danger'" :loading="!!workAction"
          :disabled="!resolutionNote.trim() || (resolution === 'release' ? !verifiedOrder : !resolvedInstance.trim())"
          native-type="submit">确认提交</ElButton>
      </div>
    </ElForm>
  </ElDialog>
</template>

<style scoped>
.task-content { display: grid; gap: 16px; min-width: 0; }
.attention-item { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 16px; }
.attention-text { flex: 1 1 260px; min-width: 0; }
.attention-text p + p { margin-top: 2px; }
.attention-item .review-error { flex-basis: 100%; }
.task-lists .el-table { width: 100%; }
.task-lists :deep(.el-table .cell) { overflow-wrap: anywhere; }
.task-lists .run-name { height: auto; max-width: 100%; text-align: start; }
.task-lists .run-name :deep(span) { white-space: normal; overflow-wrap: anywhere; }
.run-filters { margin-bottom: 12px; }
.run-filters .el-select { width: min(220px, 100%); }
.run-drawer, .task-dialog { display: grid; gap: 16px; min-width: 0; }
.run-drawer :deep(.el-descriptions__content) { overflow-wrap: anywhere; }
.run-actions, .task-dialog-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.run-actions :deep(.el-button + .el-button), .task-dialog-actions :deep(.el-button + .el-button) { margin: 0; }
.run-actions p { margin: 0; font-size: 13px; }
.review-error { color: var(--danger); }
.task-dialog-actions { justify-content: end; margin-top: 16px; }
@media (max-width: 767px) {
  .task-lists :deep(.el-table .cell) { padding-inline: 6px; }
  .task-lists { padding: 8px; }
  .run-actions :deep(.el-button) { min-height: 44px; }
}
</style>
