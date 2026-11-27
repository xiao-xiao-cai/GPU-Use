<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue'
import type { Channel, Settings } from '../../api/client'
import { saveReminder, type Reminder, type ReminderMode } from '../../api/dashboard'
import { errorMessage } from '../../display'

const props = defineProps<{ projectId: string; watchId: string; value: Reminder; settings?: Settings }>()
const emit = defineEmits<{ saved: [] }>()
const modes: { id: ReminderMode; label: string }[] = [
  { id: 'off', label: '关闭' },
  { id: 'advance', label: '提前提醒' },
  { id: 'when_available', label: '有货才提醒' },
]
const channels: { id: Channel; label: string }[] = [
  { id: 'site', label: '站内' }, { id: 'feishu', label: '飞书' }, { id: 'email', label: '邮件' },
]
const channelsForEdit = (state: Reminder) => state.channels.length ? [...state.channels] : ['site' as const]
const mode = ref<ReminderMode>(props.value.mode)
const selected = ref<Channel[]>(channelsForEdit(props.value))
const saved = ref(props.value)
const saving = ref(false)
const fault = ref('')
const feedback = ref('')
let pending: AbortController | undefined

function sameChannels(left: Channel[], right: Channel[]) {
  return left.length === right.length && left.every(id => right.includes(id))
}

const dirty = computed(() => mode.value !== saved.value.mode
  || (mode.value !== 'off' && !sameChannels(selected.value, channelsForEdit(saved.value))))
const unavailable = computed(() => mode.value !== 'off' && selected.value.some(id => !channelReady(id)))
const canSave = computed(() => dirty.value && (mode.value === 'off' || selected.value.length > 0 && !unavailable.value))

function accept(state: Reminder) {
  saved.value = state
  mode.value = state.mode
  selected.value = channelsForEdit(state)
}

watch(() => props.value, value => {
  const unchanged = value.mode === saved.value.mode && sameChannels(value.channels, saved.value.channels)
  if (unchanged || saving.value) return
  if (dirty.value) saved.value = value
  else accept(value)
})
watch([mode, selected], () => { fault.value = ''; feedback.value = '' }, { flush: 'sync' })

function channelReady(channel: Channel) {
  if (channel === 'site') return true
  return Boolean(props.settings?.channels[channel]
    && (channel !== 'email' || props.settings.notification_rules.email_recipient))
}

async function submit() {
  if (!canSave.value || pending) return
  const controller = new AbortController()
  pending = controller
  saving.value = true
  fault.value = ''
  feedback.value = ''
  try {
    const value = await saveReminder(props.projectId, props.watchId, {
      mode: mode.value,
      channels: mode.value === 'off' ? [] : [...selected.value],
    }, controller.signal)
    if (controller.signal.aborted) return
    accept(value)
    feedback.value = value.mode === 'off' ? '提醒已关闭。' : '提醒已保存。'
    emit('saved')
  } catch (error) {
    if (!controller.signal.aborted) fault.value = errorMessage(error)
  } finally {
    if (pending === controller) {
      pending = undefined
      saving.value = false
    }
  }
}

onScopeDispose(() => pending?.abort())
</script>

<template>
  <section class="panel reminder-card" aria-labelledby="dashboard-reminder-title">
    <h2 id="dashboard-reminder-title">租卡时段提醒</h2>
    <p class="muted">按当前关注规格的建议时段发送，每天最多提醒一次。</p>
    <ElForm label-position="top" :disabled="saving" @submit.prevent="submit">
      <ElFormItem label="提醒模式">
        <ElRadioGroup v-model="mode" aria-label="提醒模式">
          <ElRadio v-for="item in modes" :key="item.id" :value="item.id">{{ item.label }}</ElRadio>
        </ElRadioGroup>
      </ElFormItem>
      <p v-if="mode !== 'off'" class="reminder-explanation">
        {{ mode === 'advance' ? '在推荐时段开始前 30 分钟内提醒，并附最近一次库存检测状态。' : '在推荐时段内检测到有货才提醒。' }}
      </p>
      <ElFormItem v-if="mode !== 'off'" label="通知渠道">
        <ElCheckboxGroup v-model="selected" aria-label="通知渠道">
          <ElCheckbox v-for="channel in channels" :key="channel.id" :value="channel.id"
            :disabled="!channelReady(channel.id) && !selected.includes(channel.id)">
            {{ channel.label }}<template v-if="!channelReady(channel.id)">（待配置）</template>
          </ElCheckbox>
        </ElCheckboxGroup>
      </ElFormItem>
      <p v-if="unavailable" class="reminder-explanation">可取消未配置的渠道，或前往 <a href="#settings">设置与通知</a>。</p>
      <ElAlert v-if="fault" type="error" :title="fault" :closable="false" show-icon />
      <div class="reminder-actions">
        <ElButton native-type="submit" type="primary" :disabled="!canSave" :loading="saving">保存提醒</ElButton>
        <span v-if="feedback" role="status">{{ feedback }}</span>
      </div>
    </ElForm>
  </section>
</template>

<style scoped>
.reminder-card .el-radio-group, .reminder-card .el-checkbox-group, .reminder-actions { display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: center; }
.reminder-card .el-radio, .reminder-card .el-checkbox { margin: 0; }
.reminder-card .el-form-item { margin-bottom: .5rem; }
.reminder-explanation { color: var(--muted); margin-bottom: .65rem; }
.reminder-actions { margin-top: .8rem; }
.reminder-actions span { color: var(--accent); }
</style>
