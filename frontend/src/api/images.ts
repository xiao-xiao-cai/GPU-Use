import { computed, onScopeDispose, ref, shallowRef, watch, type Ref } from 'vue'
import { query, request, type Zone } from './client'
import { readCollectionReceipt } from './refresh'
import { commonValues, imageKinds, zoneKey, type ImageCatalog } from '../catalog'
import { errorMessage } from '../display'

export function useImages(projectId: Ref<string>, scopes: Ref<Zone[]>) {
  const snapshots = shallowRef<Record<string, ImageCatalog>>({})
  const pending = ref(false)
  const error = ref('')
  let controller: AbortController | undefined

  const scopesKey = computed(() => JSON.stringify([projectId.value, scopes.value.map(zoneKey)]))
  const images = computed(() => commonValues(scopes.value.map(scope => {
    const catalog = snapshots.value[zoneKey(scope)]
    return catalog ? imageKinds.flatMap(kind => catalog[kind].data) : []
  }), image => image.id))
  const complete = computed(() => scopes.value.length > 0 && scopes.value.every(scope => {
    const catalog = snapshots.value[zoneKey(scope)]
    return catalog && imageKinds.every(kind => catalog[kind].success_at !== null && !catalog[kind].error)
  }))

  async function load(force = false) {
    controller?.abort()
    const active = new AbortController()
    controller = active
    const { signal } = active
    const project_id = projectId.value
    const locations = scopes.value.filter(scope => scope.region && scope.zone)
    pending.value = Boolean(project_id && locations.length)
    error.value = ''
    const refreshes: { path: string; location: Zone; id: string }[] = []
    async function read(path: string, location: Zone) {
      const result = await request<ImageCatalog>(path, { signal })
      signal.throwIfAborted()
      snapshots.value = { ...snapshots.value, [zoneKey(location)]: result }
      return result
    }
    if (!pending.value) return
    try {
      for (const location of locations) {
        const body = { project_id, region: location.region, zone: location.zone }
        const path = '/images' + query(body)
        const current = await read(path, location)
        let id = current.refresh_id
        if (force || (!id && imageKinds.every(kind => current[kind].success_at === null && !current[kind].error))) {
          id = (await request<{ id: string }>('/images/refresh', { method: 'POST', body, signal })).id
        }
        if (id) refreshes.push({ path, location, id })
      }
      for (const refresh of refreshes) {
        await readCollectionReceipt(refresh.id, signal)
        await read(refresh.path, refresh.location)
      }
    } catch (failure) {
      if (!signal.aborted) error.value = errorMessage(failure)
    } finally {
      if (controller === active) pending.value = false
    }
  }

  watch(scopesKey, () => {
    snapshots.value = {}
    void load()
  }, { immediate: true })
  onScopeDispose(() => controller?.abort())
  return { snapshots, pending, error, images, complete, refresh: () => load(true) }
}
