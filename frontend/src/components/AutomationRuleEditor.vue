<script setup lang="ts">
import { computed, onScopeDispose, reactive, ref } from 'vue'
import { request, type AutomationRule, type AutomationRuleDraft, type AutomationWindow, type Channel, type Instance } from '../api/client'
import { errorMessage, localInput, unixTime } from '../display'

const props = defineProps<{
  projectId: string
  instances: Instance[]
  instanceId?: string
  rule?: AutomationRule
  channels?: Record<Channel, boolean>
}>()
const emit = defineEmits<{ saved: [rule: AutomationRule]; close: []; pending: [pending: boolean] }>()
const weekdays = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']
const schedule = props.rule?.config.schedule
const now = Math.floor(Date.now() / 1000)
const form = reactive({
  name: props.rule?.name ?? '',
  instanceId: props.rule?.instance_id ?? props.instanceId ?? '',
  kind: props.rule?.kind ?? 'idle_stop',
  duration: props.rule?.config.duration_minutes ?? 60,
  cpu: props.rule?.config.cpu_threshold ?? 10,
  gpu: props.rule?.config.gpu_threshold ?? 10,
  vram: props.rule?.config.vram_threshold ?? 10,
  mode: schedule?.mode ?? 'weekly',
  windows: schedule?.mode === 'weekly' ? schedule.windows.map(window => ({ ...window })) : [] as AutomationWindow[],
  start: localInput(schedule?.mode === 'once' ? schedule.start_at : now + 3600),
  end: localInput(schedule?.mode === 'once' ? schedule.end_at : now + 8 * 3600),
  channels: props.rule?.channels.slice() ?? ['site'] as Channel[],
  email: props.rule?.config.email ?? '',
})
if (!form.windows.length) form.windows.push({ weekday: 1, start: '22:00', end: '07:00' })

const instance = computed(() => props.instances.find(item => item.id === form.instanceId))
const runtime = computed(() => form.kind === 'runtime_stop')
const cardless = computed(() => form.kind === 'cardless_idle_stop')
const runtimeOverdue = computed(() => runtime.value && instance.value?.state === 'Running' && instance.value.start_time
  && Date.now() / 1000 - instance.value.start_time >= Number(form.duration) * 60)
const pending = ref(false)
const error = ref('')
let controller: AbortController | undefined
onScopeDispose(() => controller?.abort())

function draft(): AutomationRuleDraft {
  const result: AutomationRuleDraft = {
    project_id: props.projectId,
    instance_id: form.instanceId,
    name: form.name.trim(),
    kind: form.kind,
    channels: form.channels,
    config: {
      cpu_threshold: Number(form.cpu),
      ...(!cardless.value ? { gpu_threshold: Number(form.gpu), vram_threshold: Number(form.vram) } : {}),
      duration_minutes: Number(form.duration),
    },
  }
  if (!result.name || !result.instance_id) throw new Error('请填写规则名称并选择实例。')
  const { cpu_threshold, gpu_threshold, vram_threshold, duration_minutes } = result.config
  const thresholds = cardless.value ? [cpu_threshold] : [cpu_threshold, gpu_threshold, vram_threshold]
  if (!thresholds.every(value => value != null && Number.isFinite(value) && value > 0 && value <= 100)) {
    throw new Error('百分比阈值必须大于 0 且不超过 100。')
  }
  if (!Number.isInteger(duration_minutes) || duration_minutes < (runtime.value ? 5 : 1)
    || duration_minutes > (runtime.value ? 10080 : 1440)) throw new Error('请输入有效的分钟数。')
  if (form.channels.includes('email')) result.config.email = form.email.trim()
  if (runtime.value) return result
  if (form.mode === 'once') {
    const start_at = unixTime(form.start)
    const end_at = unixTime(form.end)
    if (!Number.isFinite(start_at) || !Number.isFinite(end_at) || end_at <= start_at) {
      throw new Error('请选择有效日期，结束时间须晚于开始时间。')
    }
    result.config.schedule = { mode: 'once', start_at, end_at }
  } else {
    const time = /^([01]\d|2[0-3]):[0-5]\d$/
    if (!form.windows.length || form.windows.some(window => !time.test(window.start) || !time.test(window.end) || window.start === window.end)) {
      throw new Error('请填写至少一个有效时段，每个时段的开始与结束时间须不同。')
    }
    result.config.schedule = { mode: 'weekly', windows: form.windows }
  }
  return result
}

async function save() {
  if (pending.value) return
  error.value = ''
  let body: AutomationRuleDraft
  try {
    body = draft()
  } catch (failure) {
    error.value = errorMessage(failure)
    return
  }
  const current = new AbortController()
  controller = current
  pending.value = true
  emit('pending', true)
  const path = '/automation-rules' + (props.rule ? '/' + encodeURIComponent(props.rule.id) : '')
  try {
    const saved = await request<AutomationRule>(path, { method: props.rule ? 'PUT' : 'POST', body, signal: current.signal })
    if (!current.signal.aborted) emit('saved', saved)
  } catch (failure) {
    if (!current.signal.aborted) error.value = errorMessage(failure)
  } finally {
    if (!current.signal.aborted) {
      controller = undefined
      pending.value = false
      emit('pending', false)
    }
  }
}
</script>

<template>
  <ElForm :model="form" label-position="top" :disabled="pending" @submit.prevent="save">
    <div class="rule-form-grid">
      <ElFormItem label="规则名称" for="automation-name">
        <ElInput id="automation-name" v-model="form.name" maxlength="200" required />
      </ElFormItem>
      <ElFormItem label="实例" for="automation-instance">
        <ElSelect id="automation-instance" v-model="form.instanceId" :disabled="!!rule" placeholder="选择项目中的实例">
          <ElOption v-if="form.instanceId && !instance" :label="form.instanceId" :value="form.instanceId" disabled />
          <ElOption v-for="item in instances" :key="item.id" :value="item.id" :label="item.name || item.id" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem label="规则类型" for="automation-kind">
        <ElSelect id="automation-kind" v-model="form.kind" :disabled="!!rule">
          <ElOption value="idle_stop" label="连续低负载关机" />
          <ElOption value="runtime_stop" label="最长运行时间" />
          <ElOption value="cardless_idle_stop" label="无卡实例仅 CPU 低负载关机" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem :label="runtime ? '最长运行时间（分钟）' : '连续低负载时长（分钟）'" for="automation-duration">
        <ElInput id="automation-duration" v-model.number="form.duration" type="number"
          :min="runtime ? 5 : 1" :max="runtime ? 10080 : 1440" step="1" required />
      </ElFormItem>
    </div>
    <p class="muted" v-if="runtime">从实例本次开机起算。达到上限时，最近 5 分钟都满足下方阈值才关机。忙碌或数据不足时仅提醒一次。</p>
    <p class="muted" v-else-if="cardless">仅在确认实例无卡运行，且 CPU 使用率持续低于阈值时关机。数据缺失、有卡运行或模式未确认时不会执行。</p>
    <p class="muted" v-else>CPU 使用率、每张 GPU 的利用率与显存使用率都须持续低于阈值。计时从规则生效后的有效采样开始。</p>
    <div class="rule-form-grid thresholds">
      <ElFormItem label="CPU 利用率上限（%）" for="automation-cpu">
        <ElInput id="automation-cpu" v-model.number="form.cpu" type="number" max="100" step="any" required />
      </ElFormItem>
      <ElFormItem v-if="!cardless" label="GPU 利用率上限（%）" for="automation-gpu">
        <ElInput id="automation-gpu" v-model.number="form.gpu" type="number" max="100" step="any" required />
      </ElFormItem>
      <ElFormItem v-if="!cardless" label="显存使用率上限（%）" for="automation-vram">
        <ElInput id="automation-vram" v-model.number="form.vram" type="number" max="100" step="any" required />
      </ElFormItem>
    </div>
    <template v-if="!runtime">
      <ElFormItem label="生效时段（北京时间）" for="automation-schedule">
        <ElSelect id="automation-schedule" v-model="form.mode">
          <ElOption label="按星期重复" value="weekly" />
          <ElOption label="一次性" value="once" />
        </ElSelect>
      </ElFormItem>
      <div v-if="form.mode === 'once'" class="rule-form-grid">
        <ElFormItem label="开始时间" for="automation-start">
          <ElDatePicker id="automation-start" v-model="form.start" type="datetime" format="YYYY-MM-DD HH:mm"
            value-format="YYYY-MM-DD[T]HH:mm" :clearable="false" />
        </ElFormItem>
        <ElFormItem label="结束时间" for="automation-end">
          <ElDatePicker id="automation-end" v-model="form.end" type="datetime" format="YYYY-MM-DD HH:mm"
            value-format="YYYY-MM-DD[T]HH:mm" :clearable="false" />
        </ElFormItem>
      </div>
      <template v-else>
        <div v-for="(window, index) in form.windows" :key="index" class="rule-window">
          <ElFormItem label="生效星期" :for="'automation-weekday-' + index">
            <ElSelect :id="'automation-weekday-' + index" v-model="window.weekday">
              <ElOption v-for="(day, dayIndex) in weekdays" :key="day" :value="dayIndex + 1" :label="day" />
            </ElSelect>
          </ElFormItem>
          <ElFormItem label="开始" :for="'automation-window-start-' + index">
            <ElInput :id="'automation-window-start-' + index" v-model="window.start" type="time" required />
          </ElFormItem>
          <ElFormItem label="结束" :for="'automation-window-end-' + index">
            <ElInput :id="'automation-window-end-' + index" v-model="window.end" type="time" required />
          </ElFormItem>
          <ElButton :disabled="form.windows.length === 1" :aria-label="'移除时段 ' + (index + 1)"
            @click="form.windows.splice(index, 1)">移除</ElButton>
        </div>
        <ElButton :disabled="form.windows.length >= 70" @click="form.windows.push({ weekday: 1, start: '22:00', end: '07:00' })">
          添加时段
        </ElButton>
        <p class="muted">结束时间早于开始时间时，时段延续至次日。</p>
      </template>
    </template>
    <ElFormItem label="通知渠道">
      <ElCheckboxGroup v-model="form.channels" aria-label="规则通知渠道">
        <ElCheckbox value="site" disabled>站内</ElCheckbox>
        <ElCheckbox value="feishu" :disabled="channels?.feishu === false && !form.channels.includes('feishu')">飞书</ElCheckbox>
        <ElCheckbox value="email" :disabled="channels?.email === false && !form.channels.includes('email')">邮件</ElCheckbox>
      </ElCheckboxGroup>
    </ElFormItem>
    <ElFormItem v-if="form.channels.includes('email')" label="收件邮箱" for="automation-email">
      <ElInput id="automation-email" v-model="form.email" type="email" maxlength="254" required />
    </ElFormItem>
    <ElAlert v-if="runtimeOverdue" title="此实例本次运行已达到上限。规则生效后，后台会检查本次开机是否需要判定。"
      :closable="false" type="warning" show-icon />
    <p v-else-if="runtime" class="muted">规则生效时已达上限的实例，由后台检查本次开机是否需要判定。</p>
    <ElAlert v-if="error" :title="error" :closable="false" type="error" show-icon />
    <div class="rule-form-actions">
      <ElButton :disabled="pending" @click="emit('close')">取消</ElButton>
      <ElButton type="primary" native-type="submit" :loading="pending">保存规则</ElButton>
    </div>
  </ElForm>
</template>

<style scoped>
.rule-form-grid, .rule-window { display: grid; gap: 0 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.rule-form-grid.thresholds { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.rule-window { grid-template-columns: repeat(3, minmax(0, 1fr)) auto; align-items: center; }
.rule-form-actions { display: flex; justify-content: flex-end; margin-top: 16px; }
.el-date-editor { --el-date-editor-width: 100%; }
@media (max-width: 520px) {
  .rule-form-grid.thresholds, .rule-window { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .rule-form-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
