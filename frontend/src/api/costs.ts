import { query, request } from './client'
import type { PoolStockState } from './dashboard'

export interface CostTotals {
  gpu_hours: number | null
  recovered_gpu_hours: number
  weighted_gpu_hours: number | null
  monitored_gpu_hours: number
  unmonitored_gpu_hours: number
  low_gpu_hours: number | null
  idle_instance_hours: number | null
  low_load_cost_cny: string | null
  low_load_cost_ratio: number | null
  idle_cost_cny: string | null
  cost_per_weighted_gpu_hour_cny: string | null
  matched_compute_cost_cny: string | null
  matched_gpu_hours: number
  matched_monitored_gpu_hours: number
}

export type BillingBasis = 'order_date' | 'service_period'
export const billingLabels = { compute: '算力', storage: '存储', image: '镜像', other: '其他' }
export type BillingCategory = keyof typeof billingLabels

export interface BillAmounts {
  cash_cny: string | null
  order_amount_cny: string | null
  coupon_cny: string | null
  free_credit_cny: string | null
}

export interface BillTotals extends BillAmounts {
  order_count: number
  review_order_count: number
}

interface BillGroup extends BillTotals {
  by_category: Record<BillingCategory, BillTotals>
}

export interface BillingCoverage {
  status: 'pending' | 'partial' | 'ready'
  synced_at: number | null
  first_synced_cycle: string | null
  last_synced_cycle: string | null
  expected_since_cycle: string
  pending_cycles: string[]
  failed_cycles: { cycle: string; error: string }[]
  open_cycle: string
}

export interface BillingOrder {
  order_no: string
  order_type: string
  category: BillingCategory
  resource_type: string
  resource_id: string | null
  name: string | null
  instance_id: string | null
  created_at: number | null
  start_at: number | null
  end_at: number | null
  amounts: BillAmounts
  selected_amounts: BillAmounts
  allocation: BillingBasis | 'review'
}

export interface CostEstimate {
  compute_cny: string | null
  cardless_compute_cny?: string | null
  cardless_unpriced_hours?: number
  cardless_rate_sources?: ('account' | 'official')[]
  storage_cny: string | null
  total_cny: string | null
  low_load_cost_cny: string | null
  low_load_cost_ratio: number | null
  idle_cost_cny: string | null
  cost_per_weighted_gpu_hour_cny: string | null
  unpriced_instance_hours: number
  unpriced_storage_hours: number
  review_resource_count: number
  status: 'ready' | 'partial' | 'pending' | 'review'
}

export interface CostSummary extends CostTotals, CostDetails {
  as_of: number
  recording_since: number | null
  data_through: number | null
  aggregation_pending: boolean
  billing: {
    basis: BillingBasis
    totals: BillGroup
    project_only: BillGroup
    coverage: BillingCoverage
    latest_order_at: number | null
    project_latest_order_at: number | null
    provisional_days: string[]
    days: Record<string, BillGroup>
    instances: Record<string, BillGroup>
  }
  today_estimate: CostEstimate & {
    date: string
    compute_data_through: number | null
    storage_data_through: number | null
    project_only_storage_cny: string | null
    instances: Record<string, CostEstimate>
  }
  days: (CostTotals & { date: string })[]
  instances: (CostTotals & {
    instance_id: string
    name: string
    gpu_model: string | null
    current: boolean
  })[]
}

export interface StorageCostDetails {
  data: {
    resource_id: string | null
    resource_type: string
    name: string | null
    totals: BillTotals
    allocations: {
      instance_id: string | null
      totals: BillTotals
    }[]
  }[]
  project_only: BillTotals
  coverage: BillingCoverage
}

export interface CostDetails {
  low_load_windows: {
    instance_id: string
    name: string
    start: number
    end: number
    low_cards: number
    busy_cards: number
    gpu: number
    whole_idle: boolean
    cpu_load: boolean | null
    gpu_memory_load: boolean | null
  }[]
  boot_waits: {
    instance_id: string
    name: string
    boot_at: number
    first_load_at: number | null
    wait_seconds: number | null
    complete_before_load: boolean
  }[]
}

export interface RentalPool {
  region: string
  zone: string
  gpu_model: string
  pool: 'exclusive' | 'spot'
  state: PoolStockState
  remaining_cards: number | null
  provider_updated_at: number | null
}

type RentalCandidate = (
  Pick<RentalPool, 'region' | 'zone' | 'pool'> | { watch_id: string }
) & { hourly_rate?: string }

export interface RentalRequest {
  gpu_model: string
  cards: number
  target_start: number
  latest_start: number
  preparation_minutes: number
  training_minutes: number
  candidates: RentalCandidate[]
}

export interface RentalOption {
  start_search_at: number
  estimated_cost_cny: string | null
  savings_vs_now_cny?: string | null
  advance_hold_seconds: number
  training_start_if_acquired_immediately: number
  eligible: boolean
  history: {
    hit_days: number
    miss_days: number
    unknown_days: number
    days: number
    coverage: number | null
  }
}

export interface RentalResult {
  status: 'ready' | 'availability_only' | 'no_window' | 'insufficient' | 'deadline_too_close'
  latest_search_at: number
  incomplete_prices: boolean
  recommendation: (RentalOption & { candidate_index: number }) | null
  candidates: (Omit<RentalPool, 'gpu_model'> & {
    watch_id: string | null
    known_samples: number
    available_samples: number
    available: boolean | null
    capacity_checked_at: number | null
    can_try_now: boolean
    now: RentalOption | null
    later: RentalOption | null
  })[]
}

export async function readCosts(path: string, signal: AbortSignal) {
  const data = await request<CostSummary>(path, { signal })
  if (!Number.isFinite(data.as_of) || !Array.isArray(data.days) || !Array.isArray(data.instances)
    || !Array.isArray(data.low_load_windows) || !Array.isArray(data.boot_waits) || !data.billing?.coverage
    || !Array.isArray(data.billing.provisional_days) || typeof data.today_estimate?.date !== 'string'
    || !data.today_estimate.instances) {
    throw new Error('服务器返回的成本汇总格式无效')
  }
  return data
}

export async function readCostDetails(path: string, signal: AbortSignal) {
  const data = await request<CostDetails>(path, { signal })
  if (!Array.isArray(data.low_load_windows) || !Array.isArray(data.boot_waits)) {
    throw new Error('服务器返回的成本明细格式无效')
  }
  return data
}

export async function readStorageCosts(path: string, signal: AbortSignal) {
  const data = await request<StorageCostDetails>(path, { signal })
  if (!Array.isArray(data.data) || !data.coverage) throw new Error('服务器返回的存储账单格式无效')
  return data
}

export async function readBillingOrders(path: string, signal: AbortSignal) {
  const data = await request<{ data: BillingOrder[]; total: number }>(path, { signal })
  if (!Array.isArray(data.data) || !Number.isSafeInteger(data.total)) throw new Error('服务器返回的订单明细格式无效')
  return data
}

export async function planRental(projectId: string, body: RentalRequest, signal: AbortSignal) {
  const data = await request<RentalResult>('/dashboard/rental-plan' + query({ project_id: projectId }),
    { method: 'POST', body, signal })
  if (!Number.isFinite(data.latest_search_at) || !Array.isArray(data.candidates)) {
    throw new Error('服务器返回的占卡试算格式无效')
  }
  return data
}
