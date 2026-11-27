<script setup lang="ts">
import { computed, onScopeDispose, ref } from 'vue'
import { query } from '../../api/client'
import { billingLabels, readCostDetails, readCosts, type BillingBasis, type BillingCategory, type CostEstimate } from '../../api/costs'
import type { UsageRange } from '../../api/dashboard'
import { localInput, timeLabel, unixTime } from '../../display'
import { billMoneyLabel, durationLabel, moneyLabel, rateLabel, valueLabel } from './format'
import MetricHelp from './MetricHelp.vue'
import DashboardBillOrders from './DashboardBillOrders.vue'
import DashboardStorageCost from './DashboardStorageCost.vue'
import RentalPlanner from './RentalPlanner.vue'
import { useDashboardQuery } from './useDashboardQuery'

const props = defineProps<{ projectId: string; range: UsageRange; instanceId?: string; storageOpen?: boolean }>()
const emit = defineEmits<{ changeRange: [range: UsageRange] }>()
const ranges = [{ id: 'today', label: '今天' }, { id: '7d', label: '近 7 天' }, { id: '30d', label: '近 30 天' }] as const
const bases = [{ id: 'order_date', label: '订单日' }, { id: 'service_period', label: '服务期' }] as const
const basis = ref<BillingBasis>('order_date')
const categories = Object.keys(billingLabels) as BillingCategory[]
const help = {
  basis: '订单日按平台订单创建日期归集整笔金额。服务期按订单服务起止时间分摊已入账金额，退款和其他调整仍按订单日归集。日期均用北京时间。\n空转成本、整机低负载成本和每加权 GPU·小时费用始终按服务期计算。',
  cash: '汇总所选范围内算力、存储、镜像和其他类别的实付金额，退款冲减合计。赠送余额和代金券抵扣另行列示。',
  compute: '按所选账单归集方式汇总算力类订单的现金实付。空转和加权费用只使用能与实例占卡时段匹配的算力实付。',
  storage: '按所选账单归集方式汇总存储类订单的现金实付，并计入现金实付合计。实例视图只显示能归属到该实例的费用。',
  estimate: '把今天已观测的占卡时长和独立存储计费时长乘以对应费率，扣除已支付订单覆盖的服务时段。估算仅覆盖按时或按量费用，使用时间按北京时间归集。',
  gpuHours: '将已观测到的占卡时段按卡数 × 小时数加总，低负载也照计。例如 2 张卡占用 3 小时，计 6 GPU·小时。',
  weighted: '逐卡将平均 GPU 利用率 × 小时数，再加总。平均值取每段起止两次采样，只统计逐卡采样完整的时段。\n例如 1 张卡以 50% 的平均利用率运行 2 小时，折合 1 个加权 GPU·小时。',
  lowGpuHours: '逐卡累计 GPU 利用率低于 10% 且持续至少 15 分钟的时长。每段起止采样都须低于 10%，同一实例里其他卡在忙也照计。',
  missingMonitoring: '已观测到 GPU 被占用，但缺少完整逐卡利用率采样的时长，按卡数 × 小时数累计。这些时段仍可能产生算力费用。',
  idleHours: '所有 GPU 同时低于 10% 且持续至少 15 分钟的时长。每台实例按整机计时，单位为实例小时。',
  lowLoadCost: '按账单服务时长分摊算力实付，再将各时段的金额 × 低负载卡数 ÷ 总卡数后加总。单卡低于 10% 且持续至少 15 分钟才计入。\n空转占比＝空转成本 ÷ 同一范围内已匹配的算力实付，分母含缺监控的匹配时段。\n例如 4 卡实例某时段分摊 ¥16，其中 3 卡全程符合低负载条件，空转成本是 ¥12，占该时段算力实付的 75%。',
  idleCost: '汇总所有 GPU 同时低于 10% 且持续至少 15 分钟的时段所分摊的算力实付。这部分费用已计入空转成本。',
  unitCost: '只看账单和完整 GPU 监控重合的时段，用这部分算力实付 ÷ 同一时段的利用率加权 GPU·小时。分母为 0 时显示金额未知。\n对比同一机型时，数值越低，相同加权用量的花费越少。',
  coverage: '占卡记录按实例和账单服务起止时间匹配，金额只分摊到重合时段。只有匹配后同时具备完整逐卡监控的时段，才能用于判断低负载或计算加权费用。',
  windows: '列出最近 20 个符合逐卡低负载条件的时段。CPU 或显存占用采样达到 10% 时会提示，便于辨别可能仍在准备或运行的实例。',
  bootWait: '从开机到首次持续负载开始。持续负载须连续至少 15 分钟，期间每段起止采样都至少有一张 GPU 利用率达到 10%。开机后有采样缺口时会标注。',
}
const scope = computed(() => ({ project_id: props.projectId, range: props.range, instance_id: props.instanceId }))
const path = computed(() => props.projectId ? '/dashboard/costs' + query({
  ...scope.value, basis: basis.value, include_details: 'false',
}) : null)
const { data, fault, loading, refresh, reload } = useDashboardQuery(path, readCosts)
const lowLoadOpen = ref(false)
const bootWaitOpen = ref(false)
const detailPath = computed(() => props.projectId && (lowLoadOpen.value || bootWaitOpen.value)
  ? '/dashboard/costs/details' + query(scope.value) : null)
const { data: details, fault: detailFault, refresh: refreshDetails } = useDashboardQuery(detailPath, readCostDetails, null)
const coverage = computed(() => data.value?.billing.coverage)
const instanceNames = computed(() => Object.fromEntries(data.value?.instances.map(row => [row.instance_id, row.name]) ?? []))
const today = ref('')
const estimate = computed(() => data.value?.today_estimate.date === today.value ? data.value.today_estimate : null)
const provisionalDays = computed(() => new Set(data.value?.billing.provisional_days))
let rollover: number
function updateToday() {
  const now = Date.now()
  today.value = localInput(now / 1000).slice(0, 10)
  const tomorrow = unixTime(today.value + 'T00:00') * 1000 + 86_400_000
  rollover = window.setTimeout(() => {
    updateToday()
    reload()
  }, tomorrow - now)
}
updateToday()
onScopeDispose(() => window.clearTimeout(rollover))

function estimateLabel(value: CostEstimate | undefined | null) {
  if (!value) return '—'
  if (value.total_cny !== null) return moneyLabel(value.total_cny)
  return value.status === 'review' ? '待核对' : '待采样或费率'
}

function dayCashLabel(date: string, category?: BillingCategory) {
  const group = data.value?.billing.days[date]
  const amounts = category ? group?.by_category[category] : group
  if (!amounts?.order_count && provisionalDays.value.has(date)) return '待入账'
  if (!amounts?.order_count && date === today.value) return '暂无订单'
  return billMoneyLabel(amounts?.cash_cny)
}

function refreshView() {
  void refresh()
  void refreshDetails()
}

function isOpen(event: Event) {
  return event.currentTarget instanceof HTMLDetailsElement && event.currentTarget.open
}
</script>

<template>
  <section v-if="!projectId" class="panel">
    <ElEmpty description="选择项目后可查看成本与占卡。" :image-size="64" />
  </section>
  <div v-else class="cost-layout">
    <section class="panel cost-controls" aria-label="成本统计筛选">
      <div class="cost-buttons" aria-label="统计范围" role="group">
        <ElButton v-for="item in ranges" :key="item.id" :type="range === item.id ? 'primary' : 'default'"
          :aria-pressed="range === item.id" size="small" @click="emit('changeRange', item.id)">{{ item.label }}</ElButton>
      </div>
      <div class="cost-buttons" aria-label="账单归集方式" role="group">
        <ElButton v-for="item in bases" :key="item.id" :type="basis === item.id ? 'primary' : 'default'"
          :aria-pressed="basis === item.id" size="small" @click="basis = item.id">{{ item.label }}</ElButton>
        <MetricHelp label="账单归集方式" :content="help.basis" />
      </div>
      <ElButton size="small" :loading="loading" @click="refreshView">刷新</ElButton>
    </section>
    <ElAlert v-if="fault" title="成本数据读取失败" :description="fault" type="error" show-icon :closable="false" />
    <ElSkeleton v-if="loading && !data" :rows="4" animated />

    <template v-if="data && coverage">
      <section class="panel metric-container">
        <h2>{{ basis === 'service_period' ? '服务期分摊' : '平台已支付账单' }}</h2>
        <dl class="cost-totals cost-headline">
          <div>
            <dt>现金实付合计<MetricHelp label="现金实付合计" :content="help.cash" /></dt>
            <dd>{{ range === 'today' && !data.billing.totals.order_count ? '暂无已入账订单' : billMoneyLabel(data.billing.totals.cash_cny) }}</dd>
          </div>
          <div>
            <dt>今日待入账估算<MetricHelp label="今日待入账估算" :content="help.estimate" /></dt>
            <dd>{{ estimate ? estimateLabel(estimate) : '等待今日采样' }}</dd>
            <dd v-if="estimate" class="cost-ratio">
              算力 {{ moneyLabel(estimate.compute_cny) }} · 独立存储 {{ moneyLabel(estimate.storage_cny) }}
            </dd>
            <dd v-if="estimate && (estimate.cardless_compute_cny != null || estimate.cardless_unpriced_hours)" class="cost-ratio">
              其中无卡算力 {{ estimate.cardless_compute_cny == null ? '待核价' : moneyLabel(estimate.cardless_compute_cny) }}
              <template v-if="estimate.cardless_rate_sources?.includes('account')"> · 账号价</template>
              <template v-if="estimate.cardless_rate_sources?.includes('official')"> · 官方标准价</template>
              <template v-if="estimate.cardless_unpriced_hours && estimate.cardless_compute_cny != null"> · 部分时段待核价</template>
            </dd>
            <dd v-if="estimate?.status === 'partial'" class="cost-ratio">部分采样或费率缺失</dd>
            <dd v-if="estimate?.status === 'review'" class="cost-ratio">
              {{ estimate.review_resource_count }} 个资源的账单服务期待核对
            </dd>
          </div>
        </dl>
        <dl class="cost-totals metric-grid metric-grid--five">
          <div v-for="category in categories" :key="category">
            <dt>
              {{ billingLabels[category] }}
              <MetricHelp v-if="category === 'compute' || category === 'storage'"
                :label="billingLabels[category] + '实付'" :content="help[category]" />
            </dt>
            <dd>{{ billMoneyLabel(data.billing.totals.by_category[category].cash_cny) }}</dd>
          </div>
        </dl>
        <p class="muted">
          订单金额 {{ billMoneyLabel(data.billing.totals.order_amount_cny) }} ·
          赠送余额抵扣 {{ billMoneyLabel(data.billing.totals.free_credit_cny) }} ·
          代金券抵扣 {{ billMoneyLabel(data.billing.totals.coupon_cny) }}
          <template v-if="!instanceId && data.billing.project_only.order_count">
            · 仅归项目 {{ billMoneyLabel(data.billing.project_only.cash_cny) }}
          </template>
        </p>
        <div class="muted" role="status">
          <p>
            <template v-if="coverage.synced_at">账单同步 {{ timeLabel(coverage.synced_at) }} · </template>
            <template v-if="data.billing.project_latest_order_at">项目最新订单 {{ timeLabel(data.billing.project_latest_order_at) }} · </template>
            <template v-if="coverage.status === 'pending'">等待账单同步</template>
            <template v-else-if="coverage.status === 'partial'">账单覆盖不完整</template>
            <template v-else>{{ coverage.open_cycle }} 账期持续入账</template>
            <template v-if="data.billing.totals.review_order_count">
              · {{ data.billing.totals.review_order_count }} 笔金额或日期待核对
            </template>
          </p>
          <p v-if="coverage.pending_cycles.length">待同步账期 {{ coverage.pending_cycles.join('、') }}</p>
          <p v-if="coverage.failed_cycles.length">
            {{ coverage.failed_cycles.map(item => item.cycle + ' ' + item.error).join('；') }}
          </p>
          <p v-if="basis === 'service_period'">
            账期回填自 {{ coverage.expected_since_cycle }} 起，较早订单的服务期可能未覆盖。
          </p>
          <p v-if="estimate && (estimate.compute_data_through || estimate.storage_data_through)">
            今日估算采样截至 · 算力 {{ timeLabel(estimate.compute_data_through) }} · 独立存储 {{ timeLabel(estimate.storage_data_through) }}
            <template v-if="Number(estimate.project_only_storage_cny)">
              · 仅归项目 {{ moneyLabel(estimate.project_only_storage_cny) }}
            </template>
          </p>
        </div>
      </section>
    </template>

    <DashboardBillOrders :project-id="projectId" :instance-id="instanceId" :range="range" :basis="basis"
      :instance-names="instanceNames" />
    <DashboardStorageCost :project-id="projectId" :instance-id="instanceId" :range="range" :basis="basis"
      :instance-names="instanceNames" :initially-open="storageOpen" />

    <template v-if="data">
      <section class="panel metric-container">
        <h2>占卡与低负载</h2>
        <dl class="cost-totals metric-grid metric-grid--five">
          <div>
            <dt>占卡 GPU·小时<MetricHelp label="占卡 GPU·小时" :content="help.gpuHours" /></dt>
            <dd>{{ valueLabel(data.gpu_hours) }}</dd>
          </div>
          <div>
            <dt>利用率加权 GPU·小时<MetricHelp label="利用率加权 GPU·小时" :content="help.weighted" /></dt>
            <dd>{{ valueLabel(data.weighted_gpu_hours) }}</dd>
          </div>
          <div>
            <dt>低负载 GPU·小时<MetricHelp label="低负载 GPU·小时" :content="help.lowGpuHours" /></dt>
            <dd>{{ valueLabel(data.low_gpu_hours) }}</dd>
          </div>
          <div>
            <dt>缺监控 GPU·小时<MetricHelp label="缺监控 GPU·小时" :content="help.missingMonitoring" /></dt>
            <dd>{{ valueLabel(data.unmonitored_gpu_hours) }}</dd>
          </div>
          <div v-if="instanceId">
            <dt>整机低负载小时<MetricHelp label="整机低负载小时" :content="help.idleHours" /></dt>
            <dd>{{ valueLabel(data.idle_instance_hours) }}</dd>
          </div>
          <div>
            <dt>空转成本<MetricHelp label="空转成本与占比" :content="help.lowLoadCost" /></dt>
            <dd>{{ moneyLabel(data.low_load_cost_cny) }}</dd>
            <dd class="cost-ratio">占已匹配算力实付 {{ rateLabel(data.low_load_cost_ratio) }}</dd>
            <dd v-if="estimate?.low_load_cost_cny != null" class="cost-ratio">
              今日未入账估算 {{ moneyLabel(estimate.low_load_cost_cny) }} · {{ rateLabel(estimate.low_load_cost_ratio) }}
            </dd>
          </div>
          <div>
            <dt>整机低负载成本<MetricHelp label="整机低负载成本" :content="help.idleCost" /></dt>
            <dd>{{ moneyLabel(data.idle_cost_cny) }}</dd>
            <dd v-if="estimate?.idle_cost_cny != null" class="cost-ratio">今日未入账估算 {{ moneyLabel(estimate.idle_cost_cny) }}</dd>
          </div>
          <div>
            <dt>每加权 GPU·小时费用<MetricHelp label="每加权 GPU·小时费用" :content="help.unitCost" /></dt>
            <dd>{{ moneyLabel(data.cost_per_weighted_gpu_hour_cny) }}</dd>
            <dd v-if="estimate?.cost_per_weighted_gpu_hour_cny != null" class="cost-ratio">
              今日未入账估算 {{ moneyLabel(estimate.cost_per_weighted_gpu_hour_cny) }}
            </dd>
          </div>
        </dl>
        <p class="muted" v-if="data.data_through != null">用量观测截至 {{ timeLabel(data.data_through) }}</p>
        <p class="muted">
          <template v-if="data.matched_gpu_hours > 0">
            指标按账单服务期计算 · 已匹配 {{ valueLabel(data.matched_gpu_hours, ' GPU·小时') }}
            和 {{ moneyLabel(data.matched_compute_cost_cny) }} 算力实付 ·
            其中有监控 {{ valueLabel(data.matched_monitored_gpu_hours, ' GPU·小时') }}
          </template>
          <template v-else>所选范围暂无能与 GPU 占用时段匹配的算力账单。</template>
          <MetricHelp label="账单与监控覆盖" :content="help.coverage" />
        </p>
        <details>
          <summary>每日费用与占卡</summary>
          <div class="data-table-scroll daily-cost-table" tabindex="0" role="region" aria-label="每日费用与占卡">
            <table>
              <thead>
                <tr>
                  <th>日期</th><th>现金实付合计</th><th>算力</th><th>存储</th><th>今日待入账估算</th>
                  <th>占卡 GPU·小时<MetricHelp label="占卡 GPU·小时" :content="help.gpuHours" /></th>
                  <th>低负载 GPU·小时<MetricHelp label="低负载 GPU·小时" :content="help.lowGpuHours" /></th>
                  <th>空转成本<MetricHelp label="空转成本与占比" :content="help.lowLoadCost" /></th>
                  <th>整机低负载成本<MetricHelp label="整机低负载成本" :content="help.idleCost" /></th>
                  <th>每加权 GPU·小时费用<MetricHelp label="每加权 GPU·小时费用" :content="help.unitCost" /></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="day in data.days" :key="day.date">
                  <th scope="row">{{ day.date }}</th>
                  <td>
                    {{ dayCashLabel(day.date) }}
                    <small v-if="provisionalDays.has(day.date) && data.billing.days[day.date]?.order_count" class="muted cost-ratio">
                      可能继续入账
                    </small>
                  </td>
                  <td>{{ dayCashLabel(day.date, 'compute') }}</td>
                  <td>{{ dayCashLabel(day.date, 'storage') }}</td>
                  <td>{{ day.date === today ? estimateLabel(estimate) : '—' }}</td>
                  <td>{{ valueLabel(day.gpu_hours) }}</td><td>{{ valueLabel(day.low_gpu_hours) }}</td>
                  <td>
                    {{ moneyLabel(day.low_load_cost_cny) }}
                    <small v-if="day.low_load_cost_ratio != null" class="muted cost-ratio">{{ rateLabel(day.low_load_cost_ratio) }}</small>
                  </td>
                  <td>{{ moneyLabel(day.idle_cost_cny) }}</td>
                  <td>{{ moneyLabel(day.cost_per_weighted_gpu_hour_cny) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </details>
        <details v-if="!instanceId && data.instances.length">
          <summary>实例费用与占卡</summary>
          <div class="data-table-scroll daily-cost-table" tabindex="0" role="region" aria-label="实例费用与占卡">
            <table>
              <thead>
                <tr>
                  <th>实例</th><th>现金实付合计</th><th>算力</th><th>存储</th><th>今日待入账估算</th>
                  <th>占卡 GPU·小时<MetricHelp label="占卡 GPU·小时" :content="help.gpuHours" /></th>
                  <th>低负载 GPU·小时<MetricHelp label="低负载 GPU·小时" :content="help.lowGpuHours" /></th>
                  <th>空转成本<MetricHelp label="空转成本与占比" :content="help.lowLoadCost" /></th>
                  <th>整机低负载成本<MetricHelp label="整机低负载成本" :content="help.idleCost" /></th>
                  <th>每加权 GPU·小时费用<MetricHelp label="每加权 GPU·小时费用" :content="help.unitCost" /></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in data.instances" :key="row.instance_id">
                  <th scope="row">{{ row.name }} <small class="muted">{{ row.gpu_model }}{{ row.current ? '' : ' · 历史实例' }}</small></th>
                  <td>{{ billMoneyLabel(data.billing.instances[row.instance_id]?.cash_cny) }}</td>
                  <td>{{ billMoneyLabel(data.billing.instances[row.instance_id]?.by_category.compute.cash_cny) }}</td>
                  <td>{{ billMoneyLabel(data.billing.instances[row.instance_id]?.by_category.storage.cash_cny) }}</td>
                  <td>
                    {{ estimateLabel(estimate?.instances[row.instance_id]) }}
                    <small v-if="estimate?.instances[row.instance_id]?.status === 'review'" class="muted cost-ratio">服务期待核对</small>
                  </td>
                  <td>{{ valueLabel(row.gpu_hours) }}</td><td>{{ valueLabel(row.low_gpu_hours) }}</td>
                  <td>
                    {{ moneyLabel(row.low_load_cost_cny) }}
                    <small v-if="row.low_load_cost_ratio != null" class="muted cost-ratio">{{ rateLabel(row.low_load_cost_ratio) }}</small>
                  </td>
                  <td>{{ moneyLabel(row.idle_cost_cny) }}</td>
                  <td>{{ moneyLabel(row.cost_per_weighted_gpu_hour_cny) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </details>
      </section>

      <section class="panel">
        <h2>低负载与开机等待</h2>
        <details class="cost-detail" :open="lowLoadOpen" @toggle="lowLoadOpen = isOpen($event)">
          <summary>最近低负载时段<MetricHelp label="最近低负载时段" :content="help.lowGpuHours + '\n' + help.windows" /></summary>
          <p v-if="detailFault" role="alert">{{ detailFault }} <ElButton link @click="refreshDetails">重试</ElButton></p>
          <p v-else-if="!details" class="muted">正在读取明细…</p>
          <p v-else-if="!details.low_load_windows.length" class="muted">暂无符合时长门槛的时段。</p>
          <ul v-else class="row-list">
            <li v-for="item in details.low_load_windows" :key="item.instance_id + ':' + item.start">
              <strong>{{ item.name }}</strong> {{ timeLabel(item.start) }} 至 {{ timeLabel(item.end) }} ·
              {{ item.whole_idle ? '整机低负载' : item.low_cards + '/' + item.gpu + ' 卡持续低负载' }}
              <small class="muted">
                <template v-if="item.busy_cards"> · {{ item.busy_cards }} 卡有负载</template>
                <template v-if="item.cpu_load"> · CPU 有负载</template>
                <template v-if="item.gpu_memory_load"> · 显存仍占用</template>
                <template v-if="item.cpu_load === null || item.gpu_memory_load === null"> · CPU 或显存采样缺失</template>
              </small>
            </li>
          </ul>
        </details>
        <details class="cost-detail" :open="bootWaitOpen" @toggle="bootWaitOpen = isOpen($event)">
          <summary>最近开机至首次持续负载<MetricHelp label="开机至首次持续负载" :content="help.bootWait" /></summary>
          <p v-if="detailFault" role="alert">{{ detailFault }} <ElButton link @click="refreshDetails">重试</ElButton></p>
          <p v-else-if="!details" class="muted">正在读取明细…</p>
          <p v-else-if="!details.boot_waits.length" class="muted">暂无开机观测。</p>
          <ul v-else class="row-list">
            <li v-for="item in details.boot_waits" :key="item.instance_id + ':' + item.boot_at">
              <strong>{{ item.name }}</strong> {{ timeLabel(item.boot_at) }} ·
              {{ item.wait_seconds === null ? '尚未观测到持续负载' : durationLabel(item.wait_seconds) }}
              <small v-if="item.wait_seconds !== null && !item.complete_before_load" class="muted"> · 开机后部分采样缺失</small>
            </li>
          </ul>
        </details>
      </section>
    </template>

    <RentalPlanner v-if="!instanceId" :project-id="projectId" />
  </div>
</template>

<style scoped>
.cost-layout { display: grid; gap: 12px; min-width: 0; }
.cost-controls, .cost-buttons { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.cost-controls { gap: 12px; }
.cost-controls > * + *, .cost-buttons .el-button + .el-button { margin: 0; }
.cost-headline { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 15rem), 1fr)); gap: 12px; }
.cost-totals dt { color: var(--muted); }
.cost-totals dd { margin: 4px 0 0; font-size: 1.25rem; font-weight: 650; }
.cost-detail { margin-top: 12px; }
.cost-totals dd.cost-ratio { font-size: .78rem; font-weight: 400; color: var(--muted); }
small.cost-ratio { display: block; }
.daily-cost-table { --table-min-width: 63rem; }
table { margin-top: 8px; width: 100%; border-collapse: collapse; text-align: left; font-size: .9rem; }
th, td { padding: .55rem .35rem; border-bottom: 1px solid var(--line); }
</style>
