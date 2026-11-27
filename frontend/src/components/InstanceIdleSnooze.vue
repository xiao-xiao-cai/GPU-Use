<script setup lang="ts">
import { computed, onScopeDispose, ref } from 'vue'
import { query, request, type IdleSnooze } from '../api/client'
import { errorMessage, localInput, timeLabel, unixTime } from '../display'

const props = defineProps<{ projectId: string; instanceId: string; current: IdleSnooze; now: number }>()
const emit = defineEmits<{ changed: [state: IdleSnooze] }>()
const paused = computed(() => props.current.paused && (props.current.until === null || props.current.until > props.now))
const open = ref(false)
const mode = ref<'until' | 'manual'>('until')
const until = ref('')
const error = ref('')
const pending = ref(false)
let active: AbortController | undefined
onScopeDispose(() => active?.abort())

function show() {
  error.value = ''
  mode.value = paused.value && props.current.until === null ? 'manual' : 'until'
  until.value = localInput(paused.value && props.current.until ? props.current.until : props.now + 3600)
  open.value = true
}

async function update(resume = false) {
  if (pending.value) return
  const timestamp = mode.value === 'until' ? unixTime(until.value) : null
  if (!resume && timestamp !== null && (!Number.isFinite(timestamp) || timestamp <= Math.floor(Date.now() / 1000))) {
    error.value = '请选择晚于当前时间的恢复时间。'
    return
  }
  error.value = ''
  active = new AbortController()
  const { signal } = active
  const path = '/instances/' + encodeURIComponent(props.instanceId) + '/idle-snooze' + query({ project_id: props.projectId })
  pending.value = true
  try {
    const state = await request<IdleSnooze>(path, { method: resume ? 'DELETE' : 'PUT', body: resume ? undefined : { until: timestamp }, signal })
    if (!signal.aborted) {
      emit('changed', state)
      open.value = false
    }
  } catch (failure) {
    if (!signal.aborted) error.value = errorMessage(failure)
  } finally {
    pending.value = false
    active = undefined
  }
}
</script>

<template>
  <div class="idle-snooze">
    <ElButton @click="show">{{ paused ? '调整空闲通知暂停' : '暂停空闲通知' }}</ElButton>
    <ElButton v-if="paused" type="primary" link :loading="pending && !open" @click="update(true)">恢复空闲通知</ElButton>
    <span v-if="paused" role="status" class="muted">{{ current.until ? '已暂停至 ' + timeLabel(current.until) : '已暂停，等待手动恢复' }}</span>
    <p v-if="!open && error" role="alert">{{ error }}</p>
  </div>
  <ElDialog v-model="open" title="暂停此实例的空闲通知" width="min(28rem, calc(100% - 24px))" append-to-body
    :show-close="!pending" :close-on-press-escape="!pending" :close-on-click-modal="false">
    <ElForm label-position="top" @submit.prevent="update()">
      <ElFormItem label="恢复方式">
        <ElSelect v-model="mode" :disabled="pending">
          <ElOption value="until" label="在指定时间恢复" />
          <ElOption value="manual" label="由我手动恢复" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem v-if="mode === 'until'" label="恢复时间（北京时间）" required>
        <ElDatePicker v-model="until" type="datetime" :disabled="pending" format="YYYY-MM-DD HH:mm"
          value-format="YYYY-MM-DD[T]HH:mm" />
      </ElFormItem>
      <ElAlert v-if="error" :title="error" type="error" :closable="false" />
      <ElButton :disabled="pending" @click="open = false">取消</ElButton>
      <ElButton type="primary" native-type="submit" :loading="pending">保存暂停</ElButton>
    </ElForm>
  </ElDialog>
</template>

<style scoped>
.idle-snooze { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.idle-snooze > .el-button { margin: 0; }
.idle-snooze > p { margin: 0; }
</style>
