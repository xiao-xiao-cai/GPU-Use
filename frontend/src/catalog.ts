import { computed, inject, type InjectionKey, type Ref } from 'vue'
import type { ChargeType, Disk, Snapshot, Zone } from './api/client'

export interface CatalogSpec {
  gpu: number
  cpu: number
  memory_gb: number
  catalog_spec_id?: string
  remaining_cards?: number | null
  state?: PoolStockState
  provider_updated_at?: number | null
  available?: boolean | null
  capacity_checked_at?: number | null
  capacity_error?: string | null
}

export type PoolStockState = 'known' | 'unknown' | 'stale' | 'unsupported'

export interface CatalogDisk {
  purpose: string
  type: string
  min_size_gb?: number | null
  max_size_gb?: number | null
}

export interface CatalogRow extends Zone {
  zone_name?: string
  gpu_model: string
  status: string
  instance_type?: string
  specs?: CatalogSpec[]
  graphics_memory_gb?: number | null
  disks?: CatalogDisk[]
}

export interface Location extends Zone {
  region_name: string
  zone_name: string
  is_pod: boolean
}

export const locationKey: InjectionKey<Readonly<Ref<Location[]>>> = Symbol('locations')

export function useLocationNames() {
  const locations = inject(locationKey, computed<Location[]>(() => []))
  const regions = computed(() => new Map(locations.value.map(item => [item.region, item.region_name])))
  const zones = computed(() => new Map(locations.value.map(item => [zoneKey(item), item.zone_name])))
  return {
    regionName: (region: string) => regions.value.get(region) || region,
    zoneName: (item: Zone, fallback?: string) => zones.value.get(zoneKey(item)) || fallback || item.zone,
  }
}

export function zoneKey(item: Zone) {
  return JSON.stringify([item.region, item.zone])
}

export function uniqueLocations(items: Zone[]) {
  return [...new Map(items.map(item => [zoneKey(item), { region: item.region, zone: item.zone }])).values()]
}

export function commonValues<T>(groups: T[][], key: (value: T) => string) {
  if (!groups.length) return []
  const remaining = groups.slice(1).map(items => new Set(items.map(key)))
  return [...new Map(groups[0]?.map(item => [key(item), item])).values()]
    .filter(item => remaining.every(group => group.has(key(item))))
}

export function billingMatches(row: CatalogRow, charge: ChargeType) {
  return (row.instance_type ?? 'uhost') === (charge === 'Spot' ? 'spot' : 'uhost')
}

export function comboKey(spec: CatalogSpec) {
  return `${spec.gpu}/${spec.cpu}/${spec.memory_gb}`
}

export function specLabel(spec: CatalogSpec) {
  return `${spec.gpu} 卡 · ${spec.cpu} vCPU · ${spec.memory_gb} GB`
}

export function diskBounds(groups: CatalogRow[][], disk: Disk) {
  const matches = groups.map(rows => rows.flatMap(row => (row.disks ?? [])
    .filter(item => item.purpose === disk.purpose && item.type === disk.type)))
  if (!matches.length || matches.some(items => !items.length)) return undefined
  const min = Math.max(...matches.flat().map(item => item.min_size_gb ?? 1))
  const max = Math.min(...matches.flat().map(item => item.max_size_gb ?? 1_000_000))
  return min <= max ? { min, max } : undefined
}

export const imageLabels = { system: '系统镜像', app: '应用镜像', custom: '自制镜像', shared: '共享镜像' }
export const imageKinds = Object.keys(imageLabels) as (keyof typeof imageLabels)[]

export interface PlatformImage {
  id: string
  name: string
  kind: keyof typeof imageLabels
  status?: string | null
  paid?: boolean | null
  runtime?: 'container' | 'vm' | null
  framework?: string | null
  framework_version?: string | null
  cuda_version?: string | null
  python_version?: string | null
  os_version?: string | null
}

export function imageOptionLabel(image: PlatformImage) {
  const framework = [image.framework, image.framework_version].filter(Boolean).join(' ')
  const versions = [image.os_version, image.cuda_version ? 'CUDA ' + image.cuda_version : null,
    framework, image.python_version ? 'Python ' + image.python_version : null].filter(Boolean)
  return image.runtime === 'container' && framework && versions.length > 1 ? versions.join(' · ') : image.name
}

export type ImageCatalog = Record<keyof typeof imageLabels, Snapshot<PlatformImage>> & { refresh_id: string | null }
