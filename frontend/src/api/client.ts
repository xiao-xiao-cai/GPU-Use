export interface Session {
  initialized: boolean
  authenticated: boolean
  csrf_token?: string
}

export interface Snapshot<T> {
  data: T[]
  success_at: number | null
  error: string | null
}

export interface Project {
  id: string
  name?: string
}

export interface Zone {
  region: string
  zone: string
}

export interface Disk {
  purpose: string
  type: string
  size_gb: number
  id?: string
  name?: string
  drive?: string
  encrypted?: boolean
}

export type ChargeType = 'Postpay' | 'Day' | 'Month' | 'Spot'

export type SharedStorage = { mode: 'us3' }
  | { mode: 'cfs_attach'; cfs_id: string }
  | { mode: 'cfs_create'; name: string; size_gib: number; charge_type: string; quantity: number; max_price: string }
  | { mode: 'cfs_resize'; cfs_id: string; size_gib: number; max_price: string }

export interface InstanceStorageSelection {
  disks: Disk[]
  shared_storage?: SharedStorage
}

export interface ZoneStorage extends Zone, InstanceStorageSelection {}

export interface InstanceIPAddress {
  address: string
  type?: string | null
  mode?: string | null
  bandwidth_mbps?: number | null
  ip_id?: string | null
  subnet_id?: string | null
  vpc_id?: string | null
  mac?: string | null
  is_default?: boolean | null
}

export type BootMode = 'with_gpu' | 'without_gpu'
export type CardlessSpec = 'A' | 'B' | 'C'

export interface CardlessProfile {
  spec: CardlessSpec
  cpu: number
  memory_gb: number
  standard_hourly_cny: string | null
}

export interface Instance extends Zone {
  id: string
  project_id: string
  name?: string
  state: string
  gpu_model?: string
  gpu?: number
  cpu?: number
  memory_gb?: number
  cpu_platform?: string
  cpu_arch?: string | null
  instance_type?: string | null
  machine_type?: string | null
  graphics_memory_gb?: number | null
  os_name?: string | null
  os_type?: string | null
  image_id?: string
  disks?: Disk[]
  total_disk_space_gb?: number | null
  total_volume_space_gb?: number | null
  charge_type?: ChargeType
  auto_renew?: boolean | null
  is_expired?: boolean | null
  usage_seconds?: number | null
  instance_price_per_hour?: number | null
  disk_price_per_hour?: number | null
  image_price_per_hour?: number | null
  discount_type?: number | null
  scheduler_stop_time?: number | null
  scheduler_status?: 'known' | 'unknown'
  release_time?: number | null
  create_time?: number | null
  start_time?: number | null
  stop_time?: number | null
  image_name?: string
  image_type?: string | null
  image_status?: string | null
  image_source?: string | null
  image_author?: string | null
  basic_image_id?: string | null
  basic_image_name?: string | null
  utilization?: { cpu?: number; memory?: number; gpus?: unknown }
  expire_time?: number | null
  gpu_mode?: string | boolean | null
  support_without_gpu_start?: boolean | null
  cardless_profiles?: CardlessProfile[]
  without_gpu_spec?: CardlessSpec | null
  cardless_hourly_cny?: string | null
  cardless_rate_source?: 'account' | 'official' | null
  gpu_status?: string | null
  business_group?: string | null
  remark?: string | null
  updated_at?: number | null
  login_mode?: string | null
  ssh?: { user: string; host: string; port: number; command: string } | null
  jupyter?: InstanceJupyter
  ip_addresses?: InstanceIPAddress[] | null
  software_links?: { name: string; url: string }[] | null
  monitor_messages?: Record<string, unknown>[]
  archive_status?: 'released' | 'missing'
  archived_at?: number
  release_run_id?: string | null
  stock?: {
    state: 'known' | 'stale' | 'unknown' | 'unsupported'
    pool: 'exclusive' | 'spot' | null
    required_cards: number | null
    remaining_cards: number | null
    provider_updated_at: number | null
    spec_available: boolean | null
    spec_checked_at: number | null
    uses_saved_spec?: boolean
  } | null
  active_operation?: { kind: string; state: string; capacity_wait: boolean } | null
}

export interface StorageResource extends Zone {
  id: string
  name: string
  kind: 'udisk' | 'cfs'
  type: string
  size: number
  charge_type: string | null
  mounted_instance_ids: string[] | null
  instances: { id: string; name: string; active: boolean }[]
  observed_at: number
  expires_at?: number | null
  status?: string | null
  stale: boolean
  usage?: { percent: number; used_gb: number; sampled_at: number } | null
  usage_state?: 'estimated' | 'unmounted' | 'stopped' | 'mount_unverified' | 'no_sample' | 'stale'
  price?: { source: 'actual' | 'account' | 'standard'; amount: string | number; observed_at: number; stale: boolean } | null
}

export interface ResizeSpec {
  gpu: number
  cpu: number
  memory_gb: number
}

export interface ResizeQuote {
  id: string
  target: ResizeSpec
  state: 'queued' | 'ready' | 'failed' | 'expired'
  amount: string | null
  available: boolean | null
  expires_at: number
  error: string | null
  run_id: string | null
}

export interface InstanceJupyter {
  url: string | null
  source: 'platform' | 'custom' | null
}

export function isSafeInstanceLink(value: unknown): value is string {
  if (typeof value !== 'string' || !/^https?:\/\//i.test(value) || /[\s\\?#]/u.test(value)) return false
  try {
    const url = new URL(value)
    return Boolean(url.hostname && !url.username && !url.password && url.port !== '0')
  } catch {
    return false
  }
}

export interface InstanceDetail {
  shutdown_cleanup_pending?: boolean
  data: Instance
  success_at: number | null
  error: string | null
  last_sample_at: number | null
  jupyter: InstanceJupyter
  idle_snooze: IdleSnooze | null
}

export interface IdleSnooze {
  paused: boolean
  until: number | null
}

export interface Point {
  metric: string
  label: string
  sampled_at: number
  value: number | null
}

export type TaskKind = 'stock' | 'start' | 'acquire' | 'stop' | 'shutdown' | 'inspection'
export type InstanceActionKind = 'start' | 'stop' | 'shutdown' | 'cancel_shutdown' | 'reboot' | 'rename'
export type Channel = 'site' | 'feishu' | 'email'
export type NotificationEvent = 'acquisition_succeeded' | 'acquisition_expired' | 'shutdown_result' | 'task_result' | 'idle'

export type AutomationRuleKind = 'idle_stop' | 'runtime_stop' | 'cardless_idle_stop'

export interface AutomationWindow {
  weekday: number
  start: string
  end: string
}

export type AutomationSchedule = { mode: 'weekly'; windows: AutomationWindow[] }
  | { mode: 'once'; start_at: number; end_at: number }

export interface AutomationRuleDraft {
  project_id: string
  instance_id: string
  name: string
  kind: AutomationRuleKind
  channels: Channel[]
  config: {
    cpu_threshold: number
    gpu_threshold?: number
    vram_threshold?: number
    duration_minutes: number
    schedule?: AutomationSchedule
    email?: string
  }
}

export interface AutomationRule extends AutomationRuleDraft, Zone {
  id: string
  status: 'active' | 'paused'
  version: number
  progress: {
    state: 'waiting' | 'counting' | 'eligible' | 'queued' | 'handled' | 'reminded' | 'outside_window' | 'unavailable'
    reason: string
    started_at: number | null
    since_at: number | null
    sampled_at: number | null
    due_at: number | null
  }
  warning: string | null
}

export interface Schedule {
  type: 'once' | 'daily' | 'weekly' | 'interval'
  at: number
  interval_seconds?: number
}

export interface Spec extends Zone {
  gpu_model: string
  gpu: number
  cpu: number
  memory_gb: number
  cpu_platform?: string
  image_id: string
  disks: Disk[]
  shared_storage?: SharedStorage
  charge_type: ChargeType
  quantity?: number
}

export interface TaskConfig extends Partial<Omit<Spec, 'region' | 'zone'>>, Partial<Zone> {
  zones?: Zone[]
  zone_storage?: ZoneStorage[]
  max_price?: string | number | null
  purchase_limit?: number
  skip_if_match?: boolean
  instance_id?: string
  shutdown_at?: number
  shutdown_after_seconds?: number
  boot_mode?: BootMode
  without_gpu_spec?: CardlessSpec
  capacity_retry?: { interval_seconds: number }
  replace?: boolean
  email?: string
  stock_pool?: boolean
}

export interface TaskDraft {
  name: string
  project_id: string
  kind: TaskKind
  schedule: Schedule
  window_seconds: number
  config: TaskConfig
  channels: Channel[]
  notification_events?: NotificationEvent[]
  template_id?: string
  template_version?: number
}

export type TaskView = 'current' | 'history' | 'trash'
export type TaskTrashAction = 'trash' | 'restore' | 'purge'

export interface Task extends TaskDraft {
  id: string
  version: number
  status: 'active' | 'paused' | 'canceled' | 'completed'
  next_at: number | null
  view: TaskView
  deleted_at: number | null
  purge_at: number | null
  pending_reason: string | null
  last_run: Pick<Run, 'id' | 'state' | 'scheduled_at' | 'updated_at' | 'reason'> | null
}

interface ShutdownReview {
  checked_at: number
  outcome: 'absent' | 'changed' | 'still_present' | 'unknown' | 'matching' | 'stopped' | 'different' | 'kept'
  cloud_shutdown_at: number | null
  original_shutdown_at?: number | null
  instance_state?: string | null
  schedule_known?: boolean
  stop_time?: number | null
  detail?: string | null
  resolved?: boolean
}

interface InstanceReview {
  checked_at: number
  state?: string | null
  name?: string | null
  start_time?: number | null
  stop_time?: number | null
  detail?: string | null
}

export interface Run {
  id: string
  task_id?: string | null
  source_task_id?: string | null
  request_id?: string | null
  project_id: string
  name: string
  kind: TaskKind | InstanceActionKind | 'terminate' | 'resize'
  scheduled_at: number
  deadline_at: number
  next_check?: number
  state: string
  phase?: string | null
  stopped?: number
  reason?: string | null
  result?: (Record<string, unknown> & {
    shutdown_review?: ShutdownReview
    actual_stop_at?: number
    shutdown_cause?: string
    shutdown_cleanup_pending?: boolean
    shutdown_observation?: { checked_at: number; instance_state?: string | null; stop_time?: number | null }
    instance_review?: InstanceReview
    instance_recheck_pending?: boolean
    purchase_review?: { outcome: 'not_purchased'; checked_at: number }
    task_config_resolution?: { task_version: number; checked_at: number }
    capacity_wait_since?: number
    capacity_attempts?: number
    automation_reminded?: boolean
    storage_state?: string
    storage_quote?: string
    storage_size_gib?: number
  }) | null
  snapshot?: {
    config?: Pick<TaskConfig, 'boot_mode' | 'without_gpu_spec' | 'shutdown_after_seconds' | 'instance_id' | 'capacity_retry'>
      & { expected_stop_at?: number | null; shutdown_at?: number | null }
    trigger?: {
      automation_rule_id: string
      kind: AutomationRuleKind
      rule_version: number
      started_at: number | null
      since_at: number | null
      sampled_at: number | null
      due_at: number | null
    }
  }
  updated_at: number
  business?: NotificationBusiness
  shutdown_reviewable?: boolean
  shutdown_followup_id?: string
}

export interface Preview {
  scheduled_at: number | null
  deadline_at: number | null
  matching_instances: Instance[]
  max_price?: string | number | null
  purchase_limit?: number | null
  snapshot_at?: number | null
  snapshot_error?: string | null
  template_id?: string
  template_version?: number
  preview_token?: string
  boot_mode?: BootMode
  without_gpu_spec?: CardlessSpec | null
  shutdown_after_seconds?: number | null
}

export interface InstanceQuote {
  id: string
  project_id: string
  state: 'queued' | 'ready' | 'failed' | 'expired' | 'used'
  price: string | null
  quoted_at: number | null
  expires_at: number | null
  reason: string | null
  run_id: string | null
  spec?: Spec
}

export interface InstancePurchasePreview {
  preview_id: string
  expires_at: number
  project_id: string
  spec: Spec
  max_price: string
  price_unit: 'CNY/hour' | 'CNY/order'
  window_seconds: number
  retry: boolean
  channels: Channel[]
  email?: string | null
}

export interface Template {
  id: string
  version: number
  project_id: string
  name: string
  config: TaskConfig
}

export interface Watch {
  id: string
  project_id: string
  name: string
  spec: WatchSpec
  catalog_spec_id: string
  version: string
  binding_status: 'bound' | 'unlisted' | 'duplicate'
  primary_watch_id: string | null
  created?: boolean
}

export type WatchSpec = Pick<Spec, 'region' | 'zone' | 'gpu_model' | 'gpu' | 'cpu' | 'memory_gb' | 'charge_type'>

export interface Action {
  project_id: string
  instance_id: string
  kind: InstanceActionKind
  boot_mode?: BootMode
  without_gpu_spec?: CardlessSpec
  stop_at?: number
  expected_stop_at?: number
  conflict?: 'keep' | 'replace'
  name?: string
  expected_name?: string
  request_id: string
}

let csrfToken = ''

export function updateSession(session: Session) {
  csrfToken = session.csrf_token ?? ''
}

export function query(values: Record<string, string | number | undefined>) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined) params.set(key, String(value))
  }
  const encoded = params.toString()
  return encoded ? '?' + encoded : ''
}

function errorDetail(value: unknown): string | null {
  if (typeof value === 'string') return value
  if (!Array.isArray(value)) return null
  const messages = value.flatMap((entry: unknown) => {
    if (typeof entry !== 'object' || entry === null || !('msg' in entry) || typeof entry.msg !== 'string') {
      return []
    }
    const field = 'loc' in entry && Array.isArray(entry.loc) ? entry.loc.slice(1).join('.') : ''
    return [field ? field + ' · ' + entry.msg : entry.msg]
  })
  return messages.join('；') || null
}

export class RequestError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
  }
}

export async function request<T>(
  path: string,
  options: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const headers = new Headers({ Accept: 'application/json' })
  if (options.body !== undefined) headers.set('Content-Type', 'application/json')
  if (options.method && options.method !== 'GET' && csrfToken) headers.set('X-CSRF-Token', csrfToken)
  const response = await fetch('/api' + path, {
    method: options.method,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    headers,
    signal: options.signal,
    credentials: 'same-origin',
    cache: 'no-store',
  })
  if (response.status === 401 && path !== '/login' && path !== '/session') {
    window.dispatchEvent(new Event('gpu-session-expired'))
  }
  if (response.status === 204) return undefined as T
  const data: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = typeof data === 'object' && data !== null && 'detail' in data ? errorDetail(data.detail) : null
    throw new RequestError(detail ?? '请求失败（' + response.status + '）', response.status)
  }
  if (data === null) throw new Error('服务器返回的数据无法读取')
  return data as T
}

export async function readSnapshot<T>(path: string, signal: AbortSignal): Promise<Snapshot<T>> {
  const snapshot = await request<Snapshot<T>>(path, { signal })
  if (!Array.isArray(snapshot.data)) throw new Error('服务器返回的采集记录格式无效')
  return snapshot
}

export function requestId() {
  return Array.from(crypto.getRandomValues(new Uint32Array(4)), value => value.toString(16).padStart(8, '0')).join('')
}

export interface SystemStatus {
  component: string
  success_at: number | null
  error: string | null
}

export interface FeishuWebhookSettings {
  mode?: 'webhook'
  signing_configured: boolean
  keyword: string
}

export type FeishuReceiveIdType = 'email' | 'open_id' | 'chat_id'

export interface FeishuAppSettings {
  mode: 'app'
  app_id: string
  receive_id_type: FeishuReceiveIdType
  receive_id: string
}

export type FeishuSettings = FeishuWebhookSettings | FeishuAppSettings

export type FeishuEvent =
  | 'instance_started'
  | 'instance_stopped'
  | 'operation_failed'
  | 'operation_review'
  | 'shutdown_attention'
  | 'instance_rebooted'
  | 'instance_created'
  | 'instance_released'
  | 'shutdown_registered'
  | 'capacity_wait'

export interface FeishuEventPolicy {
  events: FeishuEvent[]
  capacity_wait_minutes: number
}

export interface IdleSchedule {
  mode: 'all_day' | 'scheduled' | 'paused'
  windows: { weekday: number; start: string; end: string }[]
}

export interface NotificationRule {
  enabled: boolean
  channels: Channel[]
}

export interface NotificationRules {
  idle_recovery: NotificationRule
  gpu_memory_high: NotificationRule & { threshold: number; duration_minutes: number }
  monitoring: NotificationRule
  watch_stock: NotificationRule
  email_recipient: string | null
}

export interface Settings {
  collection_seconds: number
  catalog_seconds: number
  stock_seconds: number
  task_stock_seconds: number
  retention_days: number
  idle_minutes: number
  idle_threshold: number
  idle_warmup_minutes: number
  idle_channels: Channel[]
  idle_email_recipient: string | null
  idle_schedule: IdleSchedule
  notification_repeat_minutes: Record<Channel, number>
  notification_rules: NotificationRules
  feishu_event_policy: FeishuEventPolicy
  project_id: string | null
  restore_gate: boolean
  recovery?: { backup_at: number; restored_at: number; reviewed_at: number | null }[]
  status: Record<string, SystemStatus>
  read_only: boolean
  channels: Record<Channel, boolean>
  feishu: FeishuSettings | null
}

export interface Notification {
  id: number
  event_id: string
  channel: Channel
  payload: { title?: string; message?: string; recipient?: string; [field: string]: unknown }
  state: string
  attempts: number
  error: string | null
  created_at: number
  next_at: number | null
  delivered_at: number | null
  read_at: number | null
  event_type?: string
  project_id?: string | null
  business?: NotificationBusiness
}

export interface NotificationBusiness {
  status: string
  label: string
  attention?: boolean
  detail?: string | null
  observed_at?: number | null
  destination?: NotificationDestination | null
}

export interface NotificationPage {
  items: Notification[]
  next_cursor: number | null
}

export type NotificationDestination =
  | { page: 'tasks'; project_id: string; run_id?: string; task_id?: string }
  | { page: 'instances'; project_id: string; instance_id: string }
  | { page: 'inventory'; project_id: string; watch_id?: string; spec?: Spec }
  | { page: 'settings'; project_id?: string | null }

export function notificationHref(destination: NotificationDestination) {
  switch (destination.page) {
    case 'tasks':
      return '#tasks' + query({ project: destination.project_id, run: destination.run_id, task: destination.task_id })
    case 'instances':
      return '#instances' + query({ project: destination.project_id, instance: destination.instance_id })
    case 'inventory':
      return '#inventory' + query({ project: destination.project_id, watch: destination.watch_id,
        spec: destination.spec ? JSON.stringify(destination.spec) : undefined })
    case 'settings':
      return '#settings?tab=status'
  }
}
