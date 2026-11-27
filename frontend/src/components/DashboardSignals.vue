<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue'
import { Clock, WarningFilled } from '@element-plus/icons-vue'
import { notificationHref, query, request, type Run, type Task } from '../api/client'
import { errorMessage, timeLabel } from '../display'

const props = defineProps<{ projectId: string }>()
const emit = defineEmits<{ selectRun: [id: string] }>()
const next = ref<Task>()
const recent = ref<Run>()
const attentionError = ref('')
const taskLink = computed(() => {
  const destination = recent.value?.business?.destination
  if (destination?.page === 'tasks' && destination.task_id && destination.project_id === props.projectId) {
    return notificationHref(destination)
  }
  return ''
})
const exceptionLabels: Record<string, string> = {
  manual_review: '待人工核对',
  reconciling: '正在核对',
  failed: '运行失败',
  expired: '执行已过期',
  missed: '已错过执行',
}
let controller: AbortController | undefined

async function refresh() {
  if (!props.projectId || controller) return
  const pending = new AbortController()
  controller = pending
  const suffix = query({ project_id: props.projectId })
  const [tasks, runResult] = await Promise.allSettled([
    request<Task[]>('/tasks' + suffix, { signal: pending.signal }),
    request<Run[]>('/runs' + query({ project_id: props.projectId, attention: 'true' }), { signal: pending.signal }),
  ])
  if (pending.signal.aborted || controller !== pending) return
  controller = undefined
  if (tasks.status === 'fulfilled') {
    next.value = tasks.value.filter(task => task.status === 'active' && Number.isFinite(task.next_at))
      .sort((a, b) => (a.next_at ?? 0) - (b.next_at ?? 0))[0]
  }
  if (runResult.status === 'fulfilled') {
    recent.value = runResult.value[0]
    attentionError.value = ''
  } else {
    attentionError.value = errorMessage(runResult.reason)
  }
}

watch(() => props.projectId, () => {
  controller?.abort()
  controller = undefined
  next.value = undefined
  recent.value = undefined
  attentionError.value = ''
  void refresh()
}, { immediate: true })
const timer = window.setInterval(() => {
  if (!document.hidden) void refresh()
}, 30_000)
onScopeDispose(() => {
  window.clearInterval(timer)
  controller?.abort()
})
</script>

<template>
  <section v-if="next || recent || attentionError" class="dashboard-signals" aria-label="待处理事项与下次计划">
    <div v-if="attentionError" class="dashboard-run">
      <ElIcon class="dashboard-run-icon" aria-hidden="true"><WarningFilled /></ElIcon>
      <p class="dashboard-run-reason" role="status">读取待处理事项失败 · {{ attentionError }}</p>
      <ElButton type="warning" link @click="refresh">重新读取</ElButton>
    </div>
    <div v-if="recent" class="dashboard-run">
      <ElIcon class="dashboard-run-icon" aria-hidden="true"><WarningFilled /></ElIcon>
      <div class="dashboard-run-text">
        <p class="dashboard-run-summary">
          <strong>{{ recent.business?.label ?? exceptionLabels[recent.state] ?? '待处理' }}</strong> · {{ recent.name }}
        </p>
        <p v-if="recent.business?.detail ?? recent.reason" class="dashboard-run-reason">
          {{ recent.business?.detail ?? recent.reason }}
        </p>
        <p class="mono muted">更新于 {{ timeLabel(recent.business?.observed_at ?? recent.updated_at) }}</p>
      </div>
      <a v-if="taskLink" class="dashboard-run-link" :href="taskLink">编辑任务</a>
      <ElButton v-else type="warning" link @click="emit('selectRun', recent.id)">查看运行</ElButton>
    </div>
    <p v-if="next" class="dashboard-next">
      <ElIcon aria-hidden="true"><Clock /></ElIcon>
      <span>下一次</span>
      <a :href="'#tasks' + query({ project: projectId })">{{ next.name }}</a>
      <span class="mono">{{ timeLabel(next.next_at) }}</span>
    </p>
  </section>
</template>

<style scoped>
.dashboard-signals { display: flex; align-items: stretch; gap: 8px; min-width: 0; font-size: .875rem; }
.dashboard-run,
.dashboard-next { min-width: 0; padding: 8px 12px; border: 1px solid var(--line); border-radius: 8px; }
.dashboard-run {
  display: grid;
  flex: 1 1 auto;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  border-color: #e6cba2;
  color: var(--warning);
  background: var(--warning-bg);
}
.dashboard-run-icon { font-size: 1rem; }
.dashboard-run-text { min-width: 0; }
.dashboard-run-summary, .dashboard-run-reason { overflow-wrap: anywhere; }
.dashboard-run-reason {
  display: -webkit-box;
  margin: 2px 0 0;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
.dashboard-run-link, .dashboard-run :deep(.el-button) { color: var(--warning); text-underline-offset: 3px; }
.dashboard-run :deep(.el-button:hover), .dashboard-run :deep(.el-button:focus-visible) { text-decoration: underline; }
.dashboard-next {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  color: var(--muted);
  background: var(--panel);
}
.dashboard-next span { flex: none; }
.dashboard-next a { color: var(--ink); overflow-wrap: anywhere; text-underline-offset: 3px; }

@media (max-width: 767px) {
  .dashboard-signals { flex-direction: column; gap: 4px; }
  .dashboard-run, .dashboard-next { padding: 6px 8px; }
  .dashboard-run-reason { -webkit-line-clamp: 1; }
}
</style>
