<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue'
import { request, type FeishuEvent, type FeishuEventPolicy, type Settings } from '../api/client'
import { errorMessage } from '../display'

const props = defineProps<{ policy: FeishuEventPolicy }>()
const emit = defineEmits<{ saved: [settings: Settings] }>()

const groups = [
  {
    title: '开关机与异常',
    items: [
      { id: 'instance_started', label: '开机确认', detail: '回读确认实例有卡运行。' },
      { id: 'instance_stopped', label: '关机确认', detail: '回读确认实例已经关机。' },
      { id: 'operation_failed', label: '操作失败或过期', detail: '实例操作最终失败或已超过执行窗口。' },
      { id: 'operation_review', label: '操作待人工核对', detail: '回读不足以确认结果，需要打开运行详情处理。' },
      { id: 'shutdown_attention', label: '关机预约异常', detail: '预约登记失败或到点后仍未确认关机。' },
    ],
  },
  {
    title: '其他操作与提醒',
    items: [
      { id: 'instance_rebooted', label: '重启确认', detail: '回读确认实例已重启。' },
      { id: 'instance_created', label: '新建确认', detail: '回读确认新实例有卡运行。' },
      { id: 'instance_released', label: '释放确认', detail: '平台已受理且回读确认实例已移除。' },
      { id: 'shutdown_registered', label: '关机预约已登记', detail: '平台确认已登记预约。' },
      { id: 'capacity_wait', label: '预约开机等卡过久', detail: '平台无资源且持续等待时，每次运行提醒一次。' },
    ],
  },
] satisfies { title: string; items: { id: FeishuEvent; label: string; detail: string }[] }[]

const events = ref<FeishuEvent[]>([])
const waitMinutes = ref<number>()
const pending = ref(false)
const error = ref('')
const saved = ref(false)
const baseline = ref<FeishuEventPolicy>()
let saveRequest: AbortController | undefined

const waitsEnabled = computed(() => events.value.includes('capacity_wait'))
const threshold = computed(() => waitsEnabled.value ? waitMinutes.value : baseline.value?.capacity_wait_minutes)
const dirty = computed(() => {
  const before = baseline.value
  return Boolean(before && (threshold.value !== before.capacity_wait_minutes
    || events.value.length !== before.events.length
    || events.value.some(event => !before.events.includes(event))))
})

function fill(policy: FeishuEventPolicy) {
  baseline.value = { events: [...policy.events], capacity_wait_minutes: policy.capacity_wait_minutes }
  events.value = [...policy.events]
  waitMinutes.value = policy.capacity_wait_minutes
}

watch(() => props.policy, policy => {
  if (!saveRequest && !dirty.value) fill(policy)
}, { immediate: true })

async function save() {
  if (saveRequest || !dirty.value) return
  const minutes = threshold.value
  saved.value = false
  if (minutes === undefined || !Number.isInteger(minutes) || minutes < 1 || minutes > 1440) {
    error.value = '请输入 1 至 1440 分钟的整数。'
    return
  }
  error.value = ''
  const controller = new AbortController()
  saveRequest = controller
  pending.value = true
  const policy: FeishuEventPolicy = { events: [...events.value], capacity_wait_minutes: minutes }
  try {
    const result = await request<Settings>('/settings', {
      method: 'PATCH', body: { feishu_event_policy: policy }, signal: controller.signal,
    })
    if (controller.signal.aborted) return
    fill(result.feishu_event_policy)
    emit('saved', result)
    saved.value = true
  } catch (fault) {
    if (!controller.signal.aborted) error.value = errorMessage(fault)
  } finally {
    saveRequest = undefined
    pending.value = false
  }
}

onScopeDispose(() => saveRequest?.abort())
</script>

<template>
  <ElForm class="feishu-policy" label-position="top" novalidate aria-labelledby="feishu-policy-heading"
    @submit.prevent="save">
    <h2 id="feishu-policy-heading">全局飞书规则</h2>
    <p class="muted">
      适用于所有项目中由网页、MCP、定时任务及自动化发起的操作。保存后对新事件生效。
      任务、自动化和库存的单独订阅继续生效，同一事件只生成一条飞书投递记录。
    </p>
    <fieldset v-for="group in groups" :key="group.title">
      <legend>{{ group.title }}</legend>
      <ElCheckboxGroup v-model="events" :aria-label="group.title" :disabled="pending">
        <div v-for="item in group.items" :key="item.id" class="feishu-policy-row">
          <ElCheckbox :value="item.id" :aria-describedby="'feishu-event-' + item.id">{{ item.label }}</ElCheckbox>
          <span :id="'feishu-event-' + item.id" class="muted">{{ item.detail }}</span>
          <ElFormItem v-if="item.id === 'capacity_wait' && waitsEnabled" class="wait-threshold"
            label="等待阈值（分钟）" for="feishu-wait-minutes">
            <ElInputNumber id="feishu-wait-minutes" v-model="waitMinutes" :min="1" :max="1440"
              :precision="0" :step="1" :disabled="pending" />
          </ElFormItem>
        </div>
      </ElCheckboxGroup>
    </fieldset>
    <ElAlert v-if="error" title="保存飞书规则失败" :description="error" type="error" show-icon :closable="false" />
    <ElAlert v-else-if="saved && !dirty" title="飞书规则已保存" type="success" show-icon :closable="false" />
    <ElButton native-type="submit" type="primary" :loading="pending" :disabled="!dirty">保存飞书规则</ElButton>
  </ElForm>
</template>

<style scoped>
.feishu-policy { display: grid; gap: 16px; }
.feishu-policy > p { margin: 0; }
.feishu-policy fieldset { min-width: 0; margin: 0; padding: 8px 12px; border: 1px solid var(--line); border-radius: 6px; }
.feishu-policy legend { padding-inline: 4px; color: var(--ink); }
.feishu-policy-row { display: grid; grid-template-columns: minmax(185px, auto) minmax(0, 1fr); align-items: center; gap: 2px 12px; }
.feishu-policy-row + .feishu-policy-row { border-top: 1px solid var(--line); }
.feishu-policy-row > :deep(.el-checkbox) { margin: 0; }
.wait-threshold { grid-column: 1 / -1; margin-bottom: 8px; }
.feishu-policy > :deep(.el-button) { justify-self: start; }
@media (max-width: 600px) {
  .feishu-policy-row { grid-template-columns: minmax(0, 1fr); padding: 4px 0 8px; }
  .wait-threshold { margin: 8px 0 0; }
}
</style>
