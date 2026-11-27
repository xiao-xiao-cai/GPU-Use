<script setup lang="ts">
import { computed, nextTick, onMounted, onScopeDispose, ref, watch } from 'vue'
import type { FormInstance, FormItemRule } from 'element-plus'
import { query, request, type Channel, type IdleSchedule, type NotificationRules, type Project, type Settings } from '../api/client'
import FeishuEventPolicyPanel from '../components/FeishuEventPolicyPanel.vue'
import FeishuSettingsDialog from '../components/FeishuSettingsDialog.vue'
import McpSettingsPanel from '../components/McpSettingsPanel.vue'
import NotificationFeed from '../components/NotificationFeed.vue'
import { errorMessage, timeLabel } from '../display'

const props = defineProps<{ settings?: Settings; projects: Project[]; routeQuery: URLSearchParams }>()
const emit = defineEmits<{ saved: [settings: Settings]; reload: [] }>()
const tabs = ['status', 'collection', 'feishu', 'site', 'deliveries', 'mcp']
const tab = computed<string | number>({
  get: () => tabs.find(name => name === props.routeQuery.get('tab')) ?? 'status',
  set: name => {
    if (typeof name === 'string' && tabs.includes(name)) location.hash = 'settings' + query({ tab: name })
  },
})
const recoveryHeading = ref<HTMLElement>()
const lastRecovery = computed(() => props.settings?.recovery?.[0])
const recoveryOpen = ref(false)
watch([tab, () => props.routeQuery.get('section'), recoveryHeading], ([name, section]) => {
  if (name === 'status' && section === 'recovery') {
    recoveryOpen.value = true
    void nextTick(() => recoveryHeading.value?.focus())
  }
}, { immediate: true, flush: 'post' })
const compactViewport = window.matchMedia('(max-width: 767px)')
const compact = ref(compactViewport.matches)
const updateViewport = (event: MediaQueryListEvent) => { compact.value = event.matches }

const activeFeishuApp = computed(() => {
  const feishu = props.settings?.feishu
  return feishu?.mode === 'app' ? feishu : null
})
const feishuRecipientLabel = computed(() => ({
  email: '个人邮箱', open_id: 'Open ID', chat_id: '会话 ID',
})[activeFeishuApp.value?.receive_id_type ?? 'chat_id'])
const channelLabels = computed<Record<Channel, string>>(() => ({
  site: '站内通知',
  feishu: activeFeishuApp.value ? '飞书应用机器人' : props.settings?.channels.feishu ? '飞书群自定义机器人' : '飞书',
  email: '阿里云邮件推送',
}))
const shortChannelLabels: Record<Channel, string> = { site: '站内', feishu: '飞书', email: '邮件' }
const externalChannels = ['feishu', 'email'] as const
const noticeChannels = ['site', ...externalChannels] as const
const weekdays = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']
const ruleItems = [
  { id: 'idle_recovery', label: 'GPU 空闲恢复' },
  { id: 'gpu_memory_high', label: 'GPU 显存持续高占用' },
  { id: 'monitoring', label: '监控采样中断与恢复' },
  { id: 'watch_stock', label: '关注规格当前可租' },
] as const
const knownStatus = [
  { key: 'worker', name: '任务调度心跳' },
  { key: 'collection', name: '采集后台心跳' },
  { key: 'projects', name: '项目同步' },
  { key: 'instances', name: '实例同步' },
  { key: 'metrics', name: '指标采样' },
  { key: 'pool', name: '平台池余量' },
  { key: 'catalog', name: '机型目录' },
  { key: 'notifications', name: '通知投递后台' },
  { key: 'cloud', name: '优云智算凭据' },
] as const
const settingFields = [
  { id: 'collection_seconds', name: '实例与指标（秒）', min: 15, max: 3600, step: 1 },
  { id: 'catalog_seconds', name: '项目与机型目录（秒）', min: 60, max: 86400, step: 1 },
  { id: 'task_stock_seconds', name: '窗口内检测（秒）', min: 5, max: 300, step: 1 },
  { id: 'retention_days', name: '指标与库存保留（天）', min: 1, max: 365, step: 1 },
] as const
const idleFields = [
  { id: 'idle_threshold', name: '全部 GPU 利用率低于（%）', min: 0, max: 100, step: 'any' },
  { id: 'idle_minutes', name: '连续空闲（分钟）', min: 1, max: 1440, step: 1 },
  { id: 'idle_warmup_minutes', name: '开机预热（分钟）', min: 0, max: 120, step: 1 },
] as const
const numericFields = [...settingFields, ...idleFields]
type Field = typeof numericFields[number]['id']
type Draft = Record<Field, string> & {
  project_id: string
  idle_channels: Channel[]
  idle_email_recipient: string
  idle_schedule: IdleSchedule
  notification_repeat_minutes: Record<Channel, number | undefined>
  notification_rules: Omit<NotificationRules, 'email_recipient'> & { email_recipient: string }
}
const form = ref<Draft>()
const formControl = ref<FormInstance>()
const lastSaved = ref('')
const dirty = computed(() => Boolean(form.value && JSON.stringify(form.value) !== lastSaved.value))
const pending = ref(false)
const formError = ref('')
const saved = ref(false)
const rulesUseEmail = computed(() => form.value && ruleItems.some(({ id }) =>
  form.value?.notification_rules[id].enabled && form.value.notification_rules[id].channels.includes('email')))
let saveRequest: AbortController | undefined

const now = ref(Date.now() / 1000)
const statusRows = computed(() => knownStatus.map(item => {
  const status = props.settings?.status[item.key]
  const heartbeat = item.key === 'worker' || item.key === 'collection'
  const stale = heartbeat && status?.success_at && status.success_at < now.value - 90
  const health = status?.error ? { label: '异常', type: 'danger' } as const
    : stale ? { label: '心跳已过期', type: 'warning' } as const
    : status?.success_at ? { label: heartbeat ? '运行中' : '正常', type: 'success' } as const
    : { label: '等待记录', type: 'info' } as const
  return { ...item, ...health, success_at: status?.success_at, error: status?.error }
}))

const numericRules = Object.fromEntries(numericFields.map(field => [field.id, [{
  required: true,
  validator: (_rule, value: string, done) => {
    const parsed = Number(value)
    if (value !== '' && Number.isFinite(parsed) && parsed >= field.min && parsed <= field.max
      && (field.step === 'any' || Number.isInteger(parsed))) {
      done()
    } else {
      done(new Error('请输入 ' + field.min + ' 至 ' + field.max + (field.step === 'any' ? ' 的数值' : ' 的整数')))
    }
  },
}] satisfies FormItemRule[]]))
const emailRules: FormItemRule[] = [{ required: true, type: 'email', message: '请输入有效的收件地址' }]

function fill(config: Settings) {
  const values = Object.fromEntries(numericFields.map(({ id }) => [id, String(config[id])])) as Record<Field, string>
  const data: Draft = {
    ...values,
    project_id: config.project_id ?? '',
    idle_channels: config.idle_channels.slice(),
    idle_email_recipient: config.idle_email_recipient ?? '',
    idle_schedule: { mode: config.idle_schedule.mode, windows: config.idle_schedule.windows.map(window => ({ ...window })) },
    notification_repeat_minutes: { ...config.notification_repeat_minutes },
    notification_rules: {
      idle_recovery: { ...config.notification_rules.idle_recovery, channels: [...config.notification_rules.idle_recovery.channels] },
      gpu_memory_high: { ...config.notification_rules.gpu_memory_high, channels: [...config.notification_rules.gpu_memory_high.channels] },
      monitoring: { ...config.notification_rules.monitoring, channels: [...config.notification_rules.monitoring.channels] },
      watch_stock: { ...config.notification_rules.watch_stock, channels: [...config.notification_rules.watch_stock.channels] },
      email_recipient: config.notification_rules.email_recipient ?? '',
    },
  }
  lastSaved.value = JSON.stringify(data)
  form.value = data
}

watch(() => props.settings, config => {
  if (config && !saveRequest && !dirty.value) fill(config)
}, { immediate: true })

async function validate(control: FormInstance) {
  return control.validate().catch((errors: Record<string, unknown>) => {
    const field = control.getField(Object.keys(errors)[0] ?? '')
    const input = field?.$el?.querySelector('input')
    if (input instanceof HTMLInputElement) input.focus()
    return false
  })
}

async function save() {
  if (!form.value || !formControl.value || saveRequest || !dirty.value) return
  const controller = new AbortController()
  saveRequest = controller
  formError.value = ''
  saved.value = false
  try {
    if (!await validate(formControl.value) || controller.signal.aborted) return
    const draft = form.value
    const { mode, windows } = draft.idle_schedule
    if (mode === 'scheduled' && (!windows.length || windows.some(window => !window.start || !window.end || window.start === window.end))) {
      formError.value = '请填写至少一个时段，每个时段的开始和结束时间须不同。'
      return
    }
    if (noticeChannels.some(channel => !Number.isInteger(draft.notification_repeat_minutes[channel]))) {
      formError.value = '请为每个渠道填写 0 至 10080 分钟。'
      return
    }
    const rules = draft.notification_rules
    if (ruleItems.some(({ id }) => rules[id].enabled && !rules[id].channels.length)) {
      formError.value = '请为每个已启用的通知项选择至少一个渠道。'
      return
    }
    if (!Number.isInteger(rules.gpu_memory_high.threshold) || !Number.isInteger(rules.gpu_memory_high.duration_minutes)) {
      formError.value = '请填写显存阈值与持续时间。'
      return
    }
    pending.value = true
    const body = {
      ...Object.fromEntries(numericFields.map(({ id }) => [id, Number(draft[id])])),
      project_id: draft.project_id || null,
      idle_channels: draft.idle_channels,
      idle_email_recipient: draft.idle_channels.includes('email') ? draft.idle_email_recipient.trim() : null,
      idle_schedule: { mode, windows: mode === 'scheduled' ? windows : windows.filter(window => window.start && window.end && window.start !== window.end) },
      notification_repeat_minutes: draft.notification_repeat_minutes,
      notification_rules: { ...rules, email_recipient: rules.email_recipient.trim() || null },
    }
    const result = await request<Settings>('/settings', { method: 'PATCH', body, signal: controller.signal })
    if (controller.signal.aborted) return
    fill(result)
    emit('saved', result)
    saved.value = true
  } catch (error) {
    if (!controller.signal.aborted) formError.value = errorMessage(error)
  } finally {
    saveRequest = undefined
    pending.value = false
  }
}

function channelEnabled(id: Channel) {
  return id === 'site' || Boolean(props.settings?.channels[id])
}

const testDraft = ref<{ channel: Channel; recipient: string }>({ channel: 'site', recipient: '' })
const testForm = ref<FormInstance>()
const testError = ref('')
const testing = ref(false)
const siteTestEventId = ref('')
const deliveryTestEventId = ref('')
let testRequest: AbortController | undefined

const feishuOpen = ref(false)
const feishuSaved = ref(false)

function configureFeishu() {
  feishuSaved.value = false
  feishuOpen.value = true
}

function savedFeishu(config: Settings) {
  emit('saved', config)
  testDraft.value.channel = 'feishu'
  testError.value = ''
  feishuSaved.value = true
}

async function testChannel(source: 'form' | 'feishu') {
  const channel = source === 'feishu' ? 'feishu' : testDraft.value.channel
  const control = source === 'form' ? testForm.value : undefined
  if (testRequest || (source === 'form' && !control) || !channelEnabled(channel)) return
  const controller = new AbortController()
  testRequest = controller
  testError.value = ''
  try {
    if ((control && !await validate(control)) || controller.signal.aborted) return
    testing.value = true
    const { recipient } = testDraft.value
    const result = await request<{ event_id: string }>('/notifications/test', {
      method: 'POST', body: { channel, recipient: channel === 'email' ? recipient.trim() : null }, signal: controller.signal,
    })
    if (controller.signal.aborted) return
    if (channel === 'site') {
      siteTestEventId.value = result.event_id
      tab.value = 'site'
    } else {
      deliveryTestEventId.value = result.event_id
      if (source === 'feishu' && tab.value === 'feishu') {
        testDraft.value.channel = 'feishu'
        tab.value = 'deliveries'
      }
    }
  } catch (error) {
    if (!controller.signal.aborted) testError.value = errorMessage(error)
  } finally {
    testRequest = undefined
    testing.value = false
  }
}

onMounted(() => {
  compactViewport.addEventListener('change', updateViewport)
  emit('reload')
})
const timer = window.setInterval(() => {
  if (document.hidden) return
  now.value = Date.now() / 1000
  emit('reload')
}, 10_000)
onScopeDispose(() => {
  window.clearInterval(timer)
  compactViewport.removeEventListener('change', updateViewport)
  testRequest?.abort()
  saveRequest?.abort()
})
</script>

<template>
  <section class="settings-page" aria-labelledby="settings-title">
    <h1 id="settings-title">设置与通知</h1>
    <ElTabs v-model="tab" class="settings-tabs">
      <ElTabPane label="后台状态" name="status">
        <ElSkeleton v-if="!settings" animated :rows="5" aria-label="正在读取后台状态" />
        <ElDescriptions v-else class="settings-status" :column="1" :label-width="compact ? 100 : 170" border>
          <ElDescriptionsItem v-for="item in statusRows" :key="item.key" :label="item.name">
            <div class="settings-status-primary">
              <ElTag :type="item.type" effect="light" size="small">{{ item.label }}</ElTag>
              <span class="muted">最近成功 <span class="mono">{{ timeLabel(item.success_at) }}</span></span>
            </div>
            <p v-if="item.error" class="settings-status-error">{{ item.error }}</p>
          </ElDescriptionsItem>
        </ElDescriptions>
        <section v-if="settings" class="settings-panel recovery-panel" aria-labelledby="recovery-heading">
          <h2 id="recovery-heading" ref="recoveryHeading" tabindex="-1">备份恢复核对</h2>
          <p v-if="settings.restore_gate" role="status">正在核对恢复后的云端状态，云端写操作已暂停。</p>
          <p v-else class="muted">{{ lastRecovery?.reviewed_at ? '已核对 · ' + timeLabel(lastRecovery.reviewed_at) : '当前没有待核对的恢复。' }}</p>
          <p v-if="lastRecovery">备份 {{ timeLabel(lastRecovery.backup_at) }} · 恢复 {{ timeLabel(lastRecovery.restored_at) }}</p>
          <details :open="recoveryOpen" @toggle="recoveryOpen = ($event.target as HTMLDetailsElement).open">
            <summary>恢复核对流程</summary>
            <ol>
              <li>按恢复命令输出的时间范围核对云端实例、运行备注、平台订单和预约，同时盘点已释放的实例。</li>
              <li>暂停有疑问的任务。在宿主机记入已核实且尚未计数的补充采购次数。<br>
                <code>docker compose exec web gpu-admin restore-purchases &lt;任务ID&gt; &lt;补充次数&gt;</code></li>
              <li>完成核对后，在宿主机记录核对结果并开启恢复后的任务调度。<br>
                <code>docker compose exec web gpu-admin confirm-restore --note '记录已核对的订单、实例和预约'</code></li>
            </ol>
          </details>
        </section>
      </ElTabPane>

      <ElTabPane label="采集与提醒" name="collection" lazy>
        <ElSkeleton v-if="!form" animated :rows="5" aria-label="正在读取采集设置" />
        <ElForm v-else ref="formControl" class="settings-panel settings-form" :model="form" label-position="top"
          novalidate scroll-to-error @submit.prevent="save">
          <ElFormItem label="后台关注项目" prop="project_id" class="settings-project">
            <ElSelect v-model="form.project_id" :empty-values="[null, undefined]" :disabled="pending">
              <ElOption label="跟随可用项目" value="" />
              <ElOption v-if="form.project_id && !projects.some(project => project.id === form?.project_id)"
                :label="form.project_id" :value="form.project_id" />
              <ElOption v-for="project in projects" :key="project.id" :value="project.id" :label="project.name || project.id" />
            </ElSelect>
          </ElFormItem>
          <fieldset>
            <legend>采集频率</legend>
            <div class="settings-fields">
              <ElFormItem v-for="field in settingFields" :key="field.id" :prop="field.id" :label="field.name" :rules="numericRules[field.id]">
                <ElInput v-model="form[field.id]" type="number" inputmode="numeric" :min="field.min" :max="field.max" :step="field.step"
                  :disabled="pending" :validate-event="false" />
              </ElFormItem>
            </div>
          </fieldset>
          <fieldset>
            <legend>持续空闲提醒</legend>
            <div class="settings-fields">
              <ElFormItem v-for="field in idleFields" :key="field.id" :prop="field.id" :label="field.name" :rules="numericRules[field.id]">
                <ElInput v-model="form[field.id]" type="number" :inputmode="field.step === 'any' ? 'decimal' : 'numeric'"
                  :min="field.min" :max="field.max" :step="field.step" :disabled="pending" :validate-event="false" />
              </ElFormItem>
            </div>
            <ElFormItem label="接收时段（北京时间）" prop="idle_schedule.mode">
              <ElSelect v-model="form.idle_schedule.mode" :disabled="pending">
                <ElOption value="all_day" label="全天" />
                <ElOption value="scheduled" label="按星期自定义" />
                <ElOption value="paused" label="暂停所有实例的空闲通知" />
              </ElSelect>
            </ElFormItem>
            <div v-if="form.idle_schedule.mode === 'scheduled'" class="idle-schedule">
              <p class="muted">结束早于开始时，延续到次日。开窗时已达到空闲阈值的实例会在下一次有效采样后提醒。</p>
              <div v-for="(window, index) in form.idle_schedule.windows" :key="index" class="idle-window">
                <ElFormItem :label="'时段 ' + (index + 1) + ' · 星期'">
                  <ElSelect v-model="window.weekday" :disabled="pending">
                    <ElOption v-for="(day, dayIndex) in weekdays" :key="dayIndex" :value="dayIndex + 1" :label="day" />
                  </ElSelect>
                </ElFormItem>
                <ElFormItem label="开始"><ElInput v-model="window.start" type="time" :disabled="pending" /></ElFormItem>
                <ElFormItem label="结束"><ElInput v-model="window.end" type="time" :disabled="pending" /></ElFormItem>
                <ElButton :disabled="pending" @click="form.idle_schedule.windows.splice(index, 1)">移除时段</ElButton>
              </div>
              <ElButton :disabled="pending || form.idle_schedule.windows.length >= 70"
                @click="form.idle_schedule.windows.push({ weekday: 1, start: '09:00', end: '18:00' })">添加通知时段</ElButton>
            </div>
            <ElFormItem label="提醒渠道" prop="idle_channels">
              <ElCheckboxGroup v-model="form.idle_channels" :disabled="pending">
                <ElCheckbox v-for="id in noticeChannels" :key="id" :label="shortChannelLabels[id]" :value="id"
                  :disabled="!form.idle_channels.includes(id) && !channelEnabled(id)" />
              </ElCheckboxGroup>
            </ElFormItem>
            <ElFormItem v-if="form.idle_channels.includes('email')" label="收件地址" prop="idle_email_recipient" :rules="emailRules">
              <ElInput v-model.trim="form.idle_email_recipient" type="email" maxlength="254" :disabled="pending" />
            </ElFormItem>
          </fieldset>
          <fieldset>
            <legend>渠道重复提醒间隔</legend>
            <p class="muted">同一持续状态首次符合条件时发送。填写 0 表示本轮只提醒一次。</p>
            <div class="settings-fields">
              <ElFormItem v-for="id in noticeChannels" :key="id" :label="shortChannelLabels[id] + '（分钟）'">
                <ElInputNumber v-model="form.notification_repeat_minutes[id]" :min="0" :max="10080" :step="1" :precision="0" :disabled="pending" />
              </ElFormItem>
            </div>
          </fieldset>
          <fieldset>
            <legend>GPU 与资源通知</legend>
            <div v-for="rule in ruleItems" :key="rule.id" class="notice-rule">
              <ElSwitch v-model="form.notification_rules[rule.id].enabled" :active-text="rule.label" :aria-label="rule.label" :disabled="pending" />
              <ElCheckboxGroup v-if="form.notification_rules[rule.id].enabled" v-model="form.notification_rules[rule.id].channels" :disabled="pending"
                :aria-label="rule.label + '渠道'">
                <ElCheckbox v-for="id in noticeChannels" :key="id" :label="shortChannelLabels[id]" :value="id"
                  :disabled="!form.notification_rules[rule.id].channels.includes(id) && !channelEnabled(id)" />
              </ElCheckboxGroup>
              <div v-if="rule.id === 'gpu_memory_high' && form.notification_rules.gpu_memory_high.enabled" class="settings-fields notice-rule-fields">
                <ElFormItem label="显存阈值（%）">
                  <ElInputNumber v-model="form.notification_rules.gpu_memory_high.threshold" :min="6" :max="100" :step="1" :precision="0" :disabled="pending" />
                </ElFormItem>
                <ElFormItem label="持续时间（分钟）">
                  <ElInputNumber v-model="form.notification_rules.gpu_memory_high.duration_minutes" :min="1" :max="1440" :step="1" :precision="0" :disabled="pending" />
                </ElFormItem>
              </div>
            </div>
            <ElFormItem v-if="rulesUseEmail" label="资源通知收件地址" prop="notification_rules.email_recipient" :rules="emailRules">
              <ElInput v-model.trim="form.notification_rules.email_recipient" type="email" maxlength="254" :disabled="pending" />
            </ElFormItem>
          </fieldset>
          <ElAlert v-if="formError" title="保存设置失败" :description="formError" type="error" show-icon :closable="false" />
          <ElAlert v-else-if="saved && !dirty" title="设置已保存。" type="success" show-icon :closable="false" />
          <ElButton native-type="submit" type="primary" :loading="pending" :disabled="!dirty">保存设置</ElButton>
        </ElForm>
      </ElTabPane>

      <ElTabPane label="飞书规则" name="feishu" lazy>
        <ElSkeleton v-if="!settings" animated :rows="4" aria-label="正在读取飞书规则" />
        <div v-else class="settings-notifications">
          <section class="settings-panel" aria-labelledby="feishu-target-heading">
            <div class="feishu-actions">
              <h2 id="feishu-target-heading">飞书接收设置</h2>
              <ElTag :type="channelEnabled('feishu') ? 'success' : 'info'" size="small">
                {{ channelEnabled('feishu') ? '已配置' : '未配置' }}
              </ElTag>
            </div>
            <p v-if="channelEnabled('feishu')" class="settings-feishu-target">
              {{ channelLabels.feishu }} ·
              {{ activeFeishuApp ? feishuRecipientLabel + ' ' + activeFeishuApp.receive_id : 'Webhook 对应的飞书群' }}
            </p>
            <p v-else class="muted settings-feishu-target">配置接收会话后即可接收飞书通知。</p>
            <div class="feishu-actions">
              <ElButton :disabled="testing" @click="configureFeishu">配置飞书</ElButton>
              <ElButton :loading="testing" :disabled="testing || !channelEnabled('feishu')"
                @click="testChannel('feishu')">发送测试消息</ElButton>
              <a href="#settings?tab=deliveries" @click="deliveryTestEventId = ''">查看投递记录</a>
            </div>
            <ElAlert v-if="feishuSaved" title="飞书配置已保存" description="可发送测试消息确认是否送达。"
              type="success" show-icon :closable="false" />
            <ElAlert v-if="testError" title="发送测试消息失败" :description="testError" type="error" show-icon :closable="false" />
          </section>
          <section class="settings-panel">
            <FeishuEventPolicyPanel v-if="settings.feishu_event_policy" :policy="settings.feishu_event_policy"
              @saved="emit('saved', $event)" />
            <ElAlert v-else title="当前服务未返回飞书规则" type="warning" show-icon :closable="false">
              <ElButton size="small" @click="emit('reload')">重新读取设置</ElButton>
            </ElAlert>
            <nav class="feishu-actions" aria-label="其他通知设置">
              <a href="#settings?tab=collection">空闲、显存、监控与补货提醒</a>
              <a href="#dashboard?tab=availability&source=watch">租卡时段提醒</a>
            </nav>
          </section>
        </div>
      </ElTabPane>

      <ElTabPane label="站内通知" name="site" lazy>
        <NotificationFeed v-model:event-id="siteTestEventId" mode="site" :active="tab === 'site'" :projects="projects" />
      </ElTabPane>

      <ElTabPane label="投递记录" name="deliveries" lazy>
        <div class="settings-notifications">
          <section class="settings-panel" aria-labelledby="notification-channels">
            <h2 id="notification-channels">通知渠道</h2>
            <ul class="settings-channels">
              <li v-for="(label, id) in channelLabels" :key="id">
                <span>{{ label }}</span>
                <ElTag :type="channelEnabled(id) ? 'success' : 'info'" size="small">
                  {{ channelEnabled(id) ? (id === 'feishu' ? '已配置' : '可用') : settings ? '未配置' : '读取中' }}
                </ElTag>
                <ElButton v-if="id === 'feishu'" link type="primary" :disabled="!settings || testing"
                  @click="configureFeishu">配置飞书</ElButton>
              </li>
            </ul>
            <ElAlert v-if="feishuSaved" title="飞书配置已保存" description="可以发送测试消息，并在下方查看投递结果。"
              type="success" show-icon :closable="false" />
            <ElForm ref="testForm" class="settings-test" :model="testDraft" label-position="top" novalidate @submit.prevent="testChannel('form')">
              <ElFormItem label="测试渠道" prop="channel">
                <ElSelect v-model="testDraft.channel" :disabled="testing">
                  <ElOption v-for="(label, id) in channelLabels" :key="id" :value="id" :label="label" :disabled="!channelEnabled(id)" />
                </ElSelect>
              </ElFormItem>
              <ElFormItem v-if="testDraft.channel === 'email'" label="收件地址" prop="recipient" :rules="emailRules">
                <ElInput v-model.trim="testDraft.recipient" type="email" maxlength="254" :disabled="testing" />
              </ElFormItem>
              <ElButton type="primary" native-type="submit" :loading="testing"
                :disabled="testing || !channelEnabled(testDraft.channel)">发送测试消息</ElButton>
            </ElForm>
            <p v-if="testDraft.channel === 'feishu' && activeFeishuApp" class="settings-feishu-target muted">
              测试消息将发送至 {{ activeFeishuApp.receive_id }}。
            </p>
            <ElAlert v-if="testError" title="发送测试消息失败" :description="testError" type="error" show-icon :closable="false" />
          </section>

          <NotificationFeed v-model:event-id="deliveryTestEventId" mode="deliveries" :active="tab === 'deliveries'" :projects="projects" />
        </div>
      </ElTabPane>
      <ElTabPane label="MCP 配置" name="mcp" lazy>
        <McpSettingsPanel v-if="tab === 'mcp'" />
      </ElTabPane>
    </ElTabs>
    <FeishuSettingsDialog v-if="settings" v-model="feishuOpen" :config="settings.feishu ?? null"
      :legacy="settings.channels.feishu && !settings.feishu" @saved="savedFeishu" />
  </section>
</template>

<style scoped>
.settings-page { display: grid; gap: 8px; }
.recovery-panel { margin-top: 16px; overflow-wrap: anywhere; }
.recovery-panel li + li { margin-top: 10px; }
.settings-tabs { min-width: 0; }
.settings-status { background: var(--panel); }
.settings-status-primary { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; }
.settings-status-error { margin-top: 4px; color: var(--danger); overflow-wrap: anywhere; }
.settings-panel {
  display: grid;
  gap: 16px;
  min-width: 0;
  padding: clamp(12px, 2vw, 24px);
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}
.settings-form :deep(.el-form-item) { min-width: 0; }
.settings-form > .settings-project { width: min(100%, 360px); margin-bottom: 0; }
.settings-form fieldset { min-width: 0; margin: 0; padding: 8px 12px 0; border: 1px solid var(--line); border-radius: 6px; }
.settings-form legend { padding-inline: 4px; color: var(--ink); }
.settings-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 180px), 1fr)); gap: 4px 16px; }
.settings-form > :deep(.el-button) { justify-self: start; }
.settings-notifications { display: grid; gap: 16px; }
.settings-channels { display: flex; flex-wrap: wrap; gap: 8px 16px; margin: 0; padding: 0; list-style: none; }
.settings-channels li { display: flex; align-items: center; gap: 8px; }
.settings-feishu-target { margin: 0; overflow-wrap: anywhere; }
.feishu-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.feishu-actions > :deep(.el-button + .el-button) { margin-left: 0; }
.settings-test { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: start; }
.settings-test :deep(.el-form-item) { flex: 0 1 220px; margin-bottom: 16px; }
.settings-test > :deep(.el-button) { margin-top: 28px; }
.idle-schedule { display: grid; gap: 8px; margin-bottom: 16px; }
.idle-schedule > p { margin: 0; }
.idle-schedule > .el-button { justify-self: start; }
.idle-window { display: grid; grid-template-columns: minmax(9rem, 1fr) repeat(2, minmax(6rem, 1fr)) auto; gap: 8px; align-items: end; }
.idle-window > :deep(.el-form-item) { margin-bottom: 0; }
.notice-rule { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 20px; padding-block: 6px; }
.notice-rule-fields { width: 100%; }
.notice-rule-fields :deep(.el-form-item) { margin-bottom: 0; }
@media (max-width: 767px) {
  .settings-tabs :deep(.el-tabs__item) { padding-inline: 12px; }
  .settings-fields { grid-template-columns: minmax(0, 1fr); }
  .idle-window { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .idle-window > .el-button { justify-self: start; }
  .settings-test { display: grid; }
  .settings-test > :deep(.el-button) { justify-self: start; margin-top: 0; }
}
</style>
