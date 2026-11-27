<script setup lang="ts">
import { computed, nextTick, onScopeDispose, ref, shallowRef, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import {
  notificationHref, query, request, type Channel, type Notification, type NotificationBusiness,
  type NotificationDestination, type NotificationPage, type Project,
} from '../api/client'
import { errorMessage, timeLabel } from '../display'

const props = defineProps<{ active: boolean; mode: 'site' | 'deliveries'; projects: Project[] }>()
const testEventId = defineModel<string>('eventId', { required: true })
const site = computed(() => props.mode === 'site')
const titleId = computed(() => site.value ? 'site-notifications' : 'delivery-records')
const title = computed(() => site.value ? '站内通知' : '外部投递记录')
const titleElement = ref<HTMLElement>()
const channel = ref<Exclude<Channel, 'site'> | 'external'>('external')
const project = ref('')
const eventType = ref('')
const eventLabels: Record<string, string> = {
  result: '任务结果', review: '人工核对', 'shutdown-pending': '关机预约待核对',
  'shutdown-outcome': '关机执行结果', idle: '持续空闲', stock: '有货通知', test: '测试消息', legacy: '历史通知',
  'idle-recovery': 'GPU 空闲恢复', 'gpu-memory-high': '显存持续高占用',
  'monitoring-interrupted': '监控采样中断', 'monitoring-restored': '监控采样恢复', 'watch-stock': '关注规格当前可租',
  'rental-window': '租卡时段提醒', 'capacity-wait': '开机等卡过久',
}
const records = shallowRef<Notification[]>([])
const cursors = shallowRef<(number | null)[]>([null])
const nextCursor = ref<number | null>(null)
const loading = ref(false)
const hasResponse = ref(false)
const loadError = ref('')
const operation = ref<number | null>(null)
const operationError = ref('')
const operationMessage = ref('')
let currentRequest: AbortController | undefined
let operationRequest: AbortController | undefined

const projectNames = computed(() => new Map(props.projects.map(item => [item.id, item.name || item.id])))
const linkedRecords = computed(() => records.value.map(item => {
  const runId = item.payload.run_id
  const destination = item.business?.destination ?? (!site.value && item.project_id && typeof runId === 'string' && runId ? {
    page: 'tasks' as const, project_id: item.project_id, run_id: runId,
  } : undefined)
  return {
    ...item,
    href: destination ? notificationHref(destination) : undefined,
    linkLabel: destination ? destinationLabel(destination) : '',
  }
}))
const deliveryLabels: Record<Channel, string> = { site: '站内', feishu: '飞书', email: '邮件' }
const deliveryStates: Record<string, string> = { pending: '等待投递', delivered: '已送达', failed: '投递失败', skipped: '已结束投递' }

function destinationLabel(destination: NotificationDestination) {
  switch (destination.page) {
    case 'tasks': return destination.task_id ? '编辑任务' : '查看运行详情'
    case 'instances': return '查看实例'
    case 'inventory': return '查看规格'
    case 'settings': return '查看后台状态'
  }
}

function businessTone(business?: NotificationBusiness): 'warning' | 'success' | 'info' {
  if (business?.attention) return 'warning'
  if (business?.status === 'resolved' || business?.status === 'completed') return 'success'
  return 'info'
}

async function readNotifications(pageCursors = cursors.value) {
  if (!props.active || currentRequest) return
  const controller = new AbortController()
  currentRequest = controller
  loading.value = true
  const eventId = testEventId.value
  const path = '/notifications' + query({
    paged: 'true',
    channel: site.value ? 'site' : eventId ? 'external' : channel.value,
    project_id: eventId ? undefined : project.value || undefined,
    event_type: eventId ? undefined : eventType.value || undefined,
    event_id: eventId || undefined,
    cursor: pageCursors.at(-1) ?? undefined,
    limit: 20,
  })
  try {
    const result = await request<NotificationPage>(path, { signal: controller.signal })
    if (!Array.isArray(result?.items)) throw new Error('服务器返回的通知记录格式无效')
    if (controller.signal.aborted) return
    records.value = result.items
    nextCursor.value = result.next_cursor
    cursors.value = pageCursors
    hasResponse.value = true
    loadError.value = ''
  } catch (error) {
    if (!controller.signal.aborted) loadError.value = errorMessage(error)
  } finally {
    if (currentRequest === controller) {
      currentRequest = undefined
      loading.value = false
    }
  }
}

function cancelRead() {
  currentRequest?.abort()
  currentRequest = undefined
  loading.value = false
}

function nextPage() {
  if (nextCursor.value !== null) void readNotifications([...cursors.value, nextCursor.value])
}

function previousPage() {
  if (cursors.value.length > 1) void readNotifications(cursors.value.slice(0, -1))
}

async function changeNotification(id: number) {
  if (operationRequest) return
  const controller = new AbortController()
  operationRequest = controller
  operation.value = id
  operationError.value = ''
  operationMessage.value = ''
  const action = site.value ? 'read' : 'retry'
  try {
    await request('/notifications/' + id + '/' + action, { method: 'POST', signal: controller.signal })
    if (controller.signal.aborted) return
    operationMessage.value = action === 'retry' ? '已重新排队投递。' : '已标为已读。'
    cancelRead()
    await readNotifications()
  } catch (error) {
    if (!controller.signal.aborted) operationError.value = errorMessage(error)
  } finally {
    operationRequest = undefined
    operation.value = null
  }
}

watch([() => props.active, testEventId, channel, project, eventType], ([active, eventId]) => {
  cancelRead()
  if (!active) return
  records.value = []
  cursors.value = [null]
  nextCursor.value = null
  hasResponse.value = false
  loadError.value = ''
  operationMessage.value = ''
  operationError.value = ''
  void readNotifications()
  if (eventId) void nextTick(() => { if (props.active) titleElement.value?.focus() })
}, { immediate: true })

watch(() => props.active, (active, _, onCleanup) => {
  if (!active) return
  const timer = window.setInterval(() => {
    if (!document.hidden && !operationRequest) void readNotifications()
  }, 10_000)
  onCleanup(() => window.clearInterval(timer))
}, { immediate: true })

onScopeDispose(() => {
  cancelRead()
  operationRequest?.abort()
})
</script>

<template>
  <section class="notification-feed" :aria-labelledby="titleId">
    <div class="feed-heading">
      <h2 :id="titleId" ref="titleElement" tabindex="-1">{{ title }}</h2>
      <ElButton :icon="Refresh" :loading="loading" @click="readNotifications()">刷新</ElButton>
    </div>
    <div v-if="testEventId" class="test-event">
      <span>{{ site ? '站内测试消息已生成' : '测试消息已提交' }} <span class="mono">{{ testEventId }}</span></span>
      <ElButton link type="primary" @click="testEventId = ''">返回{{ site ? '通知列表' : '投递列表' }}</ElButton>
    </div>
    <div v-else class="feed-filters" role="group" aria-label="通知筛选">
      <ElSelect v-if="!site" v-model="channel" aria-label="按投递渠道筛选">
        <ElOption label="全部外部渠道" value="external" />
        <ElOption label="飞书" value="feishu" />
        <ElOption label="邮件" value="email" />
      </ElSelect>
      <ElSelect v-model="project" aria-label="按项目筛选" :empty-values="[null, undefined]">
        <ElOption label="全部项目" value="" />
        <ElOption v-for="item in projects" :key="item.id" :label="item.name || item.id" :value="item.id" />
      </ElSelect>
      <ElSelect v-model="eventType" aria-label="按事件类型筛选" :empty-values="[null, undefined]">
        <ElOption label="全部事件类型" value="" />
        <ElOption v-for="(label, value) in eventLabels" :key="value" :label="label" :value="value" />
      </ElSelect>
    </div>
    <ElAlert v-if="operationError" title="通知操作失败" :description="operationError" type="error" show-icon :closable="false" />
    <p v-if="operationMessage" class="operation-message" role="status">{{ operationMessage }}</p>
    <ElAlert v-if="loadError" :title="hasResponse ? '读取失败，正在显示上次读取的记录' : `暂时无法读取${title}`"
      :description="loadError" type="error" show-icon :closable="false" />
    <ElSkeleton v-if="loading && !hasResponse" animated :rows="4" :aria-label="`正在读取${title}`" />
    <ul v-else-if="site && linkedRecords.length" class="site-notices">
      <li v-for="item in linkedRecords" :key="item.id" class="site-notice">
        <header class="notice-heading">
          <h3>{{ item.payload.title || '通知' }}</h3>
          <ElTag :type="item.read_at ? 'info' : 'primary'" size="small">{{ item.read_at ? '已读' : '未读' }}</ElTag>
        </header>
        <p class="notice-meta muted">
          <span v-if="item.project_id">{{ projectNames.get(item.project_id) || item.project_id }}</span>
          <span v-if="item.event_type">{{ eventLabels[item.event_type] || item.event_type }}</span>
          <span>通知时间 {{ timeLabel(item.created_at) }}</span>
          <span v-if="item.read_at">已读时间 {{ timeLabel(item.read_at) }}</span>
        </p>
        <div class="business-state">
          <div class="notice-meta">
            <span>当前状态</span>
            <ElTag :type="businessTone(item.business)" size="small">{{ item.business?.label || '历史通知' }}</ElTag>
            <span v-if="item.business?.observed_at" class="muted">状态时间 {{ timeLabel(item.business.observed_at) }}</span>
          </div>
          <p v-if="item.business?.detail">{{ item.business.detail }}</p>
        </div>
        <p v-if="item.payload.message" class="original-message"><span class="muted">原始消息</span> {{ item.payload.message }}</p>
        <div v-if="item.href || !item.read_at" class="notice-actions">
          <a v-if="item.href" :href="item.href">{{ item.linkLabel }}</a>
          <ElButton v-if="!item.read_at" :loading="operation === item.id" :disabled="operation !== null" size="small"
            @click="changeNotification(item.id)">标为已读</ElButton>
        </div>
      </li>
    </ul>
    <ElTable v-else-if="linkedRecords.length" :data="linkedRecords" row-key="id" aria-label="外部投递记录" table-layout="fixed">
      <ElTableColumn type="expand" width="36">
        <template #default="{ row }">
          <dl class="record-details">
            <div><dt>事件 ID</dt><dd class="mono">{{ row.event_id }}</dd></div>
            <div v-if="row.project_id"><dt>项目</dt><dd>{{ projectNames.get(row.project_id) || row.project_id }}</dd></div>
            <div><dt>创建时间</dt><dd>{{ timeLabel(row.created_at) }}</dd></div>
            <div v-if="row.payload.message"><dt>原始消息</dt><dd>{{ row.payload.message }}</dd></div>
            <div v-if="row.error"><dt>失败原因</dt><dd>{{ row.error }}</dd></div>
            <div v-if="row.attempts"><dt>尝试次数</dt><dd>{{ row.attempts }}</dd></div>
            <div v-if="row.delivered_at"><dt>送达时间</dt><dd>{{ timeLabel(row.delivered_at) }}</dd></div>
            <div v-if="row.href"><dt>详情</dt><dd><a :href="row.href">{{ row.linkLabel }}</a></dd></div>
          </dl>
        </template>
      </ElTableColumn>
      <ElTableColumn label="消息" min-width="92">
        <template #default="{ row }">
          <strong class="record-name">{{ row.payload.title || '通知' }}</strong>
          <small class="record-channel muted">{{ deliveryLabels[row.channel as Channel] }} · {{ timeLabel(row.created_at) }}</small>
        </template>
      </ElTableColumn>
      <ElTableColumn label="投递状态" width="104">
        <template #default="{ row }">
          <div class="record-state">
            <ElTag :type="row.state === 'failed' ? 'danger' : row.state === 'delivered' ? 'success' : 'info'" size="small">
              {{ deliveryStates[row.state] || row.state }}
            </ElTag>
            <ElButton v-if="row.state === 'failed'" type="primary" link size="small"
              :loading="operation === row.id" :disabled="operation !== null" @click="changeNotification(row.id)">重试投递</ElButton>
          </div>
        </template>
      </ElTableColumn>
    </ElTable>
    <ElEmpty v-else-if="hasResponse" :description="testEventId ? '暂未读取到测试消息。' : `暂无符合筛选条件的${title}。`"
      :image-size="64" />
    <nav v-if="cursors.length > 1 || nextCursor !== null" class="feed-pages" :aria-label="`${title}分页`">
      <ElButton :disabled="loading || cursors.length === 1" @click="previousPage">上一页</ElButton>
      <ElButton :disabled="loading || nextCursor === null" @click="nextPage">下一页</ElButton>
    </nav>
  </section>
</template>

<style scoped>
.notification-feed {
  display: grid;
  gap: 16px;
  min-width: 0;
  padding: clamp(12px, 2vw, 24px);
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}
.feed-heading, .feed-filters, .feed-pages, .test-event, .notice-heading, .notice-meta, .notice-actions {
  display: flex;
  align-items: center;
  gap: 8px 12px;
  flex-wrap: wrap;
}
.feed-heading, .test-event, .notice-heading { justify-content: space-between; }
.feed-filters :deep(.el-select) { width: min(100%, 164px); }
.feed-pages { justify-content: end; }
.feed-pages :deep(.el-button + .el-button) { margin-left: 0; }
.test-event { padding: 8px 12px; border-radius: 6px; background: var(--accent-bg); overflow-wrap: anywhere; }
.test-event > span, .notice-heading > h3 { min-width: 0; overflow-wrap: anywhere; }
.operation-message { color: var(--accent); }
.site-notices { display: grid; gap: 12px; margin: 0; padding: 0; list-style: none; }
.site-notice { display: grid; gap: 8px; padding: 12px; border: 1px solid var(--line); border-radius: 6px; overflow-wrap: anywhere; }
.notice-meta { gap: 4px 12px; font-size: .875rem; }
.business-state { display: grid; gap: 4px; padding: 8px 12px; border-inline-start: 2px solid var(--line); }
.original-message { white-space: pre-wrap; }
.original-message > span { margin-inline-end: 4px; }
.record-name, .record-details dd { overflow-wrap: anywhere; }
.record-channel { display: block; }
.record-state { display: grid; justify-items: start; gap: 4px; }
.record-details { display: grid; gap: 4px; margin: 0; padding: 8px 16px; }
.record-details > div { display: grid; grid-template-columns: 4.75rem minmax(0, 1fr); gap: 8px; }
.record-details dt { color: var(--muted); }
.record-details dd { margin: 0; }
@media (max-width: 767px) {
  .feed-filters :deep(.el-select) { flex: 1 1 104px; min-width: 0; }
  .notification-feed :deep(.el-table .cell) { padding-inline: 5px; }
}
</style>
