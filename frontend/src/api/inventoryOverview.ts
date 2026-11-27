import type { CatalogRow, CatalogSpec, PoolStockState } from '../catalog'
import type { StockReference } from './dashboard'

export interface HourlyRate {
  amount: string
  source: 'account' | 'catalog'
  observed_at: number
  stale: boolean
}

export interface HourlyRange {
  quoted: number
  total: number
  failed: number
  minimum: string | null
  maximum: string | null
  source: 'account' | 'catalog' | 'mixed' | null
  stale: boolean
}

export interface InventorySpec extends CatalogSpec {
  catalog_spec_id: string
  hourly_rate: HourlyRate | null
  quote_failed: boolean
}

export interface InventoryEntry extends CatalogRow {
  pool: 'exclusive' | 'spot'
  listed: boolean
  state: PoolStockState
  remaining_cards: number | null
  reference?: StockReference | null
  provider_updated_at: number | null
  graphics_memory_gb: number | null
  card_counts: number[]
  specs: InventorySpec[]
  hourly_range: HourlyRange
}

export interface InventoryGroup {
  key: string
  region: string
  zone: string
  gpu_model: string
  zone_name?: string
  graphics_memory_gb: number | null
  pools: InventoryEntry[]
}

export function inventoryKey(entry: Pick<InventoryEntry, 'region' | 'zone' | 'gpu_model'>) {
  return JSON.stringify([entry.region, entry.zone, entry.gpu_model])
}

export function groupInventory(entries: InventoryEntry[]): InventoryGroup[] {
  const groups = new Map<string, InventoryGroup>()
  for (const { region, zone, gpu_model, zone_name, graphics_memory_gb, ...rest } of entries) {
    const key = inventoryKey({ region, zone, gpu_model })
    const group = groups.get(key) ?? { key, region, zone, gpu_model, zone_name, graphics_memory_gb, pools: [] }
    group.graphics_memory_gb ??= graphics_memory_gb
    group.pools.push({ region, zone, gpu_model, zone_name, graphics_memory_gb, ...rest })
    groups.set(key, group)
  }
  return [...groups.values()]
}
