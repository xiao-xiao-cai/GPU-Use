import { computed, onScopeDispose, shallowRef, watch, type Ref } from 'vue'
import { readSnapshot, type Run, type Snapshot } from './api/client'

const date = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})

const localDate = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})

export function timeLabel(timestamp?: number | null) {
  return timestamp && Number.isFinite(timestamp) ? date.format(timestamp * 1000) : '—'
}

export function localInput(timestamp: number) {
  return localDate.format(timestamp * 1000).replace(' ', 'T')
}

export function unixTime(local: string) {
  return Math.floor(Date.parse(local + ':00+08:00') / 1000)
}

export function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '请求失败，请重试'
}

export function reviewingShutdown(run: Run) {
  return run.state === 'reconciling' && run.phase === 'review_cancel_shutdown'
}

export function awaitingInstanceReview(run: Run) {
  return run.state === 'manual_review'
    && ['verify_start', 'verify_stop', 'verify_reboot', 'verify_rename', 'verify_terminate', 'verify_resize', 'verify_create', 'manual_bind', 'verify_cfs']
      .includes(run.phase ?? '')
}

export const instanceLabels: Record<string, string> = {
  Running: '运行中',
  Stopped: '已关机',
  Starting: '启动中',
  Stopping: '关机中',
  Initializing: '初始化中',
  Installing: '部署中',
  Rebooting: '重启中',
  Deleted: '已释放',
  Terminated: '已释放',
  Released: '已释放',
  Missing: '已离开清单',
}

export const chargeLabels: Record<string, string> = {
  Postpay: '按量后付费',
  Dynamic: '包时',
  Day: '包天',
  Month: '包月',
  Year: '包年',
  Spot: '抢占式',
}

export const storagePriceLabels = { actual: '实例费率', account: '账号报价', standard: '标准价' }

export function useSnapshot<T>(path: Ref<string | null>, interval = 15_000) {
  const state = shallowRef<Snapshot<T>>({ data: [], success_at: null, error: null })
  const loading = shallowRef(false)
  const fault = shallowRef<string | null>(null)
  let pending: AbortController | null = null

  async function refresh() {
    if (!path.value || pending) return
    const controller = new AbortController()
    pending = controller
    loading.value = true
    try {
      const snapshot = await readSnapshot<T>(path.value, controller.signal)
      if (!controller.signal.aborted) {
        state.value = snapshot
        fault.value = null
      }
    } catch (error) {
      if (!controller.signal.aborted) fault.value = errorMessage(error)
    } finally {
      if (pending === controller) {
        pending = null
        loading.value = false
      }
    }
  }

  watch(path, () => {
    pending?.abort()
    pending = null
    loading.value = false
    state.value = { data: [], success_at: null, error: null }
    fault.value = null
    void refresh()
  }, { immediate: true })
  const timer = window.setInterval(() => {
    if (!document.hidden) void refresh()
  }, interval)
  onScopeDispose(() => {
    window.clearInterval(timer)
    pending?.abort()
  })

  return {
    state,
    loading,
    fault: computed(() => fault.value ?? state.value.error),
    refresh,
  }
}
