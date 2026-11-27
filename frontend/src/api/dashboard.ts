import { query, request, type Channel, type WatchSpec } from './client'

export type UsageRange = 'today' | '7d' | '30d'

export interface UsageDay {
  date: string
  gpu_hours: number | null
  average_utilization: number | null
  coverage: number
  covered_seconds?: number
  elapsed_seconds?: number
}

export type AutomationOutcome = 'confirmed_shutdown' | 'reminded' | 'needs_review'
  | 'capacity_waiting' | 'capacity_completed' | 'capacity_ended'

export interface UsageSummary {
  as_of: number
  since: number
  range: UsageRange
  recording_since: number | null
  gpu_hours: number | null
  average_utilization: number | null
  instances_used: number
  days: UsageDay[]
  instances: {
    instance_id: string
    name: string
    gpu_model: string | null
    gpu_hours: number
    average_utilization: number | null
    current: boolean
  }[]
  automation: {
    confirmed_shutdowns: number
    reminded: number
    needs_review: number
    capacity_waits: number
    capacity_completed: number
    average_wait_seconds: number | null
    recent: {
      run_id: string
      name: string
      observed_at: number
      outcome: AutomationOutcome
      wait_seconds: number | null
    }[]
  }
}

export type AvailabilitySource = 'pool' | 'watch'
export type PoolStockState = 'known' | 'unknown' | 'stale' | 'unsupported'
export const cardThresholds = [1, 2, 4, 8] as const
export type CardThreshold = typeof cardThresholds[number]
export type ReminderMode = 'off' | 'advance' | 'when_available'

export interface Reminder {
  mode: ReminderMode
  channels: Channel[]
}

export interface StockCounts {
  available_count: number
  known_count: number
  expected_count: number
}

export interface StockReference {
  cards: number
  source: 'platform_total' | 'observed_peak'
  sample_count: number | null
  peak_at?: number | null
}

export interface AvailabilityItem extends StockCounts {
  key: string
  name: string
  region: string
  zone: string
  gpu_model: string
  instance_type: string | null
  watch_id: string | null
  spec: WatchSpec | null
  binding_status?: 'bound' | 'unlisted' | 'duplicate'
  required_cards?: number | null
  available: boolean | null
  capacity_checked_at?: number | null
  capacity_error?: string | null
  observed_at: number | null
  pool?: 'exclusive' | 'spot' | null
  remaining_cards?: number | null
  reference?: StockReference | null
  state?: PoolStockState
  provider_updated_at?: number | null
  median_cards?: number | null
}

export interface AvailabilityHour extends StockCounts {
  start: number
  median_cards?: number | null
}

export interface RentalHour {
  hour: number
  rate: number
  days: number
}

export interface AvailabilityDetail {
  item: AvailabilityItem
  hours: AvailabilityHour[]
  recommended: RentalHour[]
  tight: RentalHour[]
  recommendation_status: 'insufficient' | 'stable' | 'ready'
  reminder: Reminder | null
}

export interface AvailabilitySummary {
  as_of: number
  since: number
  source: AvailabilitySource
  items: AvailabilityItem[]
  total: number
  detail: AvailabilityDetail | null
}

export async function readUsage(path: string, signal: AbortSignal) {
  const value = await request<UsageSummary>(path, { signal })
  if (!Number.isFinite(value.as_of) || !Array.isArray(value.days) || !Array.isArray(value.instances)
    || !Array.isArray(value.automation?.recent)) throw new Error('服务器返回的用量汇总格式无效')
  return value
}

export async function readAvailability(path: string, signal: AbortSignal) {
  const value = await request<AvailabilitySummary>(path, { signal })
  const detail = value.detail
  if ((value.source !== 'pool' && value.source !== 'watch') || !Number.isFinite(value.as_of)
    || !Number.isFinite(value.total) || !Array.isArray(value.items)
    || (detail && (!Array.isArray(detail.hours) || !Array.isArray(detail.recommended) || !Array.isArray(detail.tight)))) {
    throw new Error('服务器返回的库存趋势格式无效')
  }
  if (value.source === 'pool' && (value.items.some(item => !isPoolStock(item))
    || (detail && (!isPoolStock(detail.item) || detail.hours.some(hour => !isCardCount(hour.median_cards)))))) {
    throw new Error('服务器返回的平台余量格式无效')
  }
  return value
}

function isCardCount(value: unknown): boolean {
  return value === null || (typeof value === 'number' && Number.isFinite(value) && value >= 0)
}

function isPoolStock(item: AvailabilityItem): boolean {
  return (item.pool === 'exclusive' || item.pool === 'spot')
    && (item.state === 'known' || item.state === 'unknown' || item.state === 'stale' || item.state === 'unsupported')
    && (item.remaining_cards === null ? item.state !== 'known' : isCardCount(item.remaining_cards) && Number.isInteger(item.remaining_cards))
    && (item.provider_updated_at === null || Number.isFinite(item.provider_updated_at))
    && isCardCount(item.median_cards)
}

export async function saveReminder(projectId: string, watchId: string, reminder: Reminder, signal: AbortSignal) {
  return request<Reminder>('/dashboard/watches/' + encodeURIComponent(watchId) + '/reminder' + query({ project_id: projectId }),
    { method: 'PUT', body: reminder, signal })
}
