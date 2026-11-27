<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref, shallowRef } from 'vue'
import AutomationRuleEditor from './AutomationRuleEditor.vue'
import { query, request, type AutomationRule, type Channel, type Instance } from '../api/client'
import { errorMessage, timeLabel } from '../display'

const props = defineProps<{
  projectId: string
  instanceId?: string
  instances: Instance[]
  channels?: Record<Channel, boolean>
}>()
const emit = defineEmits<{ history: [rule: AutomationRule] }>()
const kindLabels = { idle_stop: '连续低负载关机', runtime_stop: '最长运行时间', cardless_idle_stop: '无卡实例仅 CPU 低负载关机' }
const progressLabels: Record<AutomationRule['progress']['state'], string> = {
  waiting: '等待监测', counting: '正在计时', eligible: '满足触发条件', queued: '已排队',
  handled: '已有执行记录', reminded: '已提醒', outside_window: '时段外', unavailable: '数据待确认',
}
const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
const instancesById = computed(() => new Map(props.instances.map(instance => [instance.id, instance])))
const rules = shallowRef<AutomationRule[]>()
const loading = ref(false)
const listError = ref('')
const notice = ref('')
const saveWarning = ref('')
const editorOpen = ref(false)
const editorPending = ref(false)
const editing = shallowRef<AutomationRule>()
const controlOpen = ref(false)
const controlTarget = shallowRef<AutomationRule>()
const controlError = ref('')
const controlPending = ref(false)
let listRequest: AbortController | undefined
let controlRequest: AbortController | undefined
let dialogSource: HTMLElement | undefined

function instanceName(id: string) {
  return instancesById.value.get(id)?.name || id
}

function ruleSchedule(rule: AutomationRule) {
  const schedule = rule.config.schedule
  if (schedule?.mode === 'once') return `${timeLabel(schedule.start_at)} 至 ${timeLabel(schedule.end_at)}`
  if (schedule?.mode === 'weekly') return schedule.windows.map(window => {
    const next = window.start > window.end ? '次日 ' : ''
    return `${weekdays[window.weekday - 1]} ${window.start} 至 ${next}${window.end}`
  }).join('、')
  return '每次开机分别计时'
}

async function load() {
  if (listRequest) return
  const current = new AbortController()
  listRequest = current
  loading.value = true
  try {
    const result = await request<AutomationRule[]>('/automation-rules' + query({
      project_id: props.projectId, instance_id: props.instanceId || undefined,
    }), { signal: current.signal })
    if (current.signal.aborted) return
    rules.value = result
    listError.value = ''
  } catch (failure) {
    if (!current.signal.aborted) listError.value = errorMessage(failure)
  } finally {
    if (listRequest === current) {
      listRequest = undefined
      loading.value = false
    }
  }
}

function reload() {
  listRequest?.abort()
  listRequest = undefined
  void load()
}

function applyRule(rule: AutomationRule) {
  const previous = rules.value ?? []
  if (props.instanceId && props.instanceId !== rule.instance_id) return
  rules.value = previous.some(item => item.id === rule.id)
    ? previous.map(item => item.id === rule.id ? rule : item) : [rule, ...previous]
}

function openEditor(event: MouseEvent, rule?: AutomationRule) {
  dialogSource = event.currentTarget instanceof HTMLElement ? event.currentTarget : undefined
  editing.value = rule
  editorOpen.value = true
}

function saved(rule: AutomationRule) {
  applyRule(rule)
  editorOpen.value = false
  notice.value = '规则已保存。'
  saveWarning.value = rule.warning ?? ''
  reload()
}

function openControl(event: MouseEvent, rule: AutomationRule) {
  dialogSource = event.currentTarget instanceof HTMLElement ? event.currentTarget : undefined
  controlTarget.value = rule
  controlError.value = ''
  controlOpen.value = true
}

function restoreFocus() {
  if (dialogSource?.isConnected) dialogSource.focus()
  dialogSource = undefined
}

async function control() {
  const rule = controlTarget.value
  if (!rule || controlPending.value) return
  const pause = rule.status === 'active'
  const current = new AbortController()
  controlRequest = current
  controlPending.value = true
  controlError.value = ''
  try {
    const result = await request<AutomationRule>('/automation-rules/' + encodeURIComponent(rule.id) + (pause ? '/pause' : '/resume'), {
      method: 'POST', signal: current.signal,
    })
    if (current.signal.aborted) return
    applyRule(result)
    notice.value = pause ? '规则已暂停。' : '规则已恢复。'
    saveWarning.value = result.warning ?? ''
    controlOpen.value = false
    reload()
  } catch (failure) {
    if (!current.signal.aborted) controlError.value = errorMessage(failure)
  } finally {
    if (controlRequest === current) {
      controlRequest = undefined
      controlPending.value = false
    }
  }
}

onMounted(() => { void load() })
const timer = window.setInterval(() => { if (!document.hidden) void load() }, 8000)
onScopeDispose(() => {
  window.clearInterval(timer)
  listRequest?.abort()
  controlRequest?.abort()
})
</script>

<template>
  <section class="automation-panel" aria-label="自动化规则">
    <div class="section-heading">
      <div v-if="instanceId" class="rule-scope">
        <span>实例 {{ instanceName(instanceId) }}</span>
        <a :href="'#tasks' + query({ project: projectId, automation: '' })">全部实例</a>
      </div>
      <ElButton type="primary" @click="openEditor($event)">新建规则</ElButton>
    </div>
    <p v-if="notice" role="status">{{ notice }}</p>
    <ElAlert v-if="saveWarning" :title="saveWarning" type="warning" :closable="false" show-icon />
    <ElAlert v-if="listError" :title="listError" type="error" :closable="false" show-icon>
      <ElButton size="small" @click="reload">重试</ElButton>
    </ElAlert>
    <ElSkeleton v-if="loading && !rules" animated :rows="3" aria-label="正在读取自动化规则" />
    <ElEmpty v-else-if="rules && !rules.length" description="暂无自动化规则。" />
    <ul v-else-if="rules" class="rule-list">
      <li v-for="rule in rules" :key="rule.id" class="rule-row">
        <div class="rule-info">
          <div class="rule-title">
            <strong>{{ rule.name }}</strong>
            <ElTag v-if="rule.status === 'paused'" size="small" type="info">已暂停</ElTag>
            <ElTag v-else size="small" :type="rule.progress.state === 'unavailable' ? 'warning' : 'success'">
              {{ progressLabels[rule.progress.state] }}
            </ElTag>
          </div>
          <span>{{ kindLabels[rule.kind] }} · {{ rule.config.duration_minutes }} 分钟 ·
            <a :href="'#instances' + query({ project: projectId, instance: rule.instance_id })">{{ instanceName(rule.instance_id) }}</a>
          </span>
          <span class="muted">{{ ruleSchedule(rule) }}</span>
          <span class="muted">CPU &lt; {{ rule.config.cpu_threshold }}% · GPU &lt; {{ rule.config.gpu_threshold }}% · 显存 &lt; {{ rule.config.vram_threshold }}%</span>
          <span v-if="rule.status === 'active'">{{ rule.progress.reason }}</span>
          <span v-if="rule.warning && rule.warning !== rule.progress.reason && rule.warning !== saveWarning" class="muted">{{ rule.warning }}</span>
          <div v-if="rule.status === 'active'" class="rule-timing muted">
            <span v-if="rule.progress.since_at && rule.kind !== 'runtime_stop'">低负载始于 {{ timeLabel(rule.progress.since_at) }}</span>
            <span v-if="rule.progress.sampled_at">最近样本 {{ timeLabel(rule.progress.sampled_at) }}</span>
            <span v-if="rule.progress.due_at">判定时间 {{ timeLabel(rule.progress.due_at) }}</span>
          </div>
        </div>
        <div class="rule-actions">
          <ElButton size="small" :aria-label="'编辑 ' + rule.name" @click="openEditor($event, rule)">编辑</ElButton>
          <ElButton size="small" :aria-label="(rule.status === 'active' ? '暂停 ' : '恢复 ') + rule.name"
            @click="openControl($event, rule)">{{ rule.status === 'active' ? '暂停' : '恢复' }}</ElButton>
          <ElButton size="small" link type="primary" :aria-label="'运行记录 ' + rule.name" @click="emit('history', rule)">运行记录</ElButton>
        </div>
      </li>
    </ul>
  </section>

  <ElDialog v-model="editorOpen" append-to-body destroy-on-close width="min(660px, calc(100vw - 32px))"
    :title="editing ? '编辑自动化规则' : '新建自动化规则'" :close-on-click-modal="false"
    :close-on-press-escape="!editorPending" :show-close="!editorPending" @closed="restoreFocus">
    <AutomationRuleEditor :project-id="projectId" :instance-id="instanceId" :instances="instances" :rule="editing"
      :channels="channels" @saved="saved" @pending="editorPending = $event" @close="editorOpen = false" />
  </ElDialog>

  <ElDialog v-model="controlOpen" append-to-body width="min(440px, calc(100vw - 32px))"
    :title="controlTarget?.status === 'active' ? '暂停自动化规则' : '恢复自动化规则'"
    :close-on-click-modal="false" :close-on-press-escape="!controlPending" :show-close="!controlPending" @closed="restoreFocus">
    <template v-if="controlTarget">
      <p>{{ controlTarget.name }} · {{ instanceName(controlTarget.instance_id) }}</p>
      <p v-if="controlTarget.status === 'active'">暂停后停止提交新的关机请求，已提交的操作继续核对结果。</p>
      <p v-else-if="controlTarget.kind === 'runtime_stop'">恢复后自动判定此实例的运行时长，已达上限时立即判定负载。</p>
      <p v-else>恢复后从新的有效低负载样本开始计时。</p>
    </template>
    <ElAlert v-if="controlError" :title="controlError" type="error" :closable="false" show-icon />
    <template #footer>
      <ElButton :disabled="controlPending" @click="controlOpen = false">取消</ElButton>
      <ElButton type="primary" :loading="controlPending" @click="control">
        {{ controlTarget?.status === 'active' ? '确认暂停' : '确认恢复' }}
      </ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.automation-panel, .rule-info { display: grid; gap: 8px; min-width: 0; }
.rule-list { display: grid; gap: 0; list-style: none; padding: 0; margin: 0; }
.rule-row { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px; padding: 14px 0; border-bottom: 1px solid var(--el-border-color-light); }
.rule-info { flex: 1 1 260px; overflow-wrap: anywhere; }
.rule-title, .rule-timing, .rule-actions, .rule-scope { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.rule-actions { align-content: start; }
.rule-actions .el-button + .el-button { margin-left: 0; }
</style>
