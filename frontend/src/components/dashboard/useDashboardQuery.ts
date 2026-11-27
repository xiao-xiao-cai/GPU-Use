import { onScopeDispose, shallowRef, watch, type Ref } from 'vue'
import { errorMessage } from '../../display'

export function useDashboardQuery<T>(
  path: Ref<string | null>, load: (path: string, signal: AbortSignal) => Promise<T>, pollMs: number | null = 60_000,
) {
  const data = shallowRef<T | null>(null)
  const loading = shallowRef(false)
  const fault = shallowRef('')
  let pending: AbortController | null = null

  async function refresh() {
    if (!path.value || pending) return
    const controller = new AbortController()
    pending = controller
    loading.value = true
    try {
      const result = await load(path.value, controller.signal)
      if (!controller.signal.aborted) {
        data.value = result
        fault.value = ''
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

  function abort() {
    pending?.abort()
    pending = null
  }

  function reload() {
    abort()
    void refresh()
  }

  watch(path, () => {
    abort()
    data.value = null
    fault.value = ''
    loading.value = false
    void refresh()
  }, { immediate: true, flush: 'sync' })

  const timer = pollMs === null ? undefined : window.setInterval(() => {
    if (!document.hidden) void refresh()
  }, pollMs)
  onScopeDispose(() => {
    window.clearInterval(timer)
    abort()
  })
  return { data, loading, fault, refresh, reload }
}
