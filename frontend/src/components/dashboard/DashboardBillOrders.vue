<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { query } from '../../api/client'
import { billingLabels, readBillingOrders, type BillingBasis, type BillingCategory } from '../../api/costs'
import type { UsageRange } from '../../api/dashboard'
import { timeLabel } from '../../display'
import { billMoneyLabel } from './format'
import { useDashboardQuery } from './useDashboardQuery'

const props = defineProps<{
  projectId: string
  instanceId?: string
  range: UsageRange
  basis: BillingBasis
  instanceNames: Record<string, string>
}>()
const open = ref(false)
const category = ref<BillingCategory | ''>('')
const offset = ref(0)
const perPage = 50
const kinds: Record<string, string> = {
  OT_POSTPAID_PAYMENT: '按量结算', OT_BUY: '新购', OT_RENEW: '续费', OT_REFUND: '退款',
  OT_UPGRADE: '升级', OT_DOWNGRADE: '降级', OT_ADDITIONAL: '补单',
}
watch(() => [props.projectId, props.instanceId, props.range, props.basis, category.value], () => {
  offset.value = 0
}, { flush: 'sync' })
const path = computed(() => open.value ? '/dashboard/costs/orders' + query({
  project_id: props.projectId, instance_id: props.instanceId, range: props.range,
  basis: props.basis, category: category.value || undefined, limit: String(perPage), offset: String(offset.value),
}) : null)
const { data, fault, refresh } = useDashboardQuery(path, readBillingOrders)

function toggled(event: Event) {
  if (event.currentTarget instanceof HTMLDetailsElement) open.value = event.currentTarget.open
}
</script>

<template>
  <details class="panel" :open="open" @toggle="toggled">
    <summary>逐笔账单</summary>
    <label v-if="open" class="order-filter">
      费用类别
      <select v-model="category">
        <option value="">全部</option>
        <option v-for="(label, key) in billingLabels" :key="key" :value="key">{{ label }}</option>
      </select>
    </label>
    <p v-if="fault" role="alert">{{ fault }} <ElButton link @click="refresh">重试</ElButton></p>
    <p v-else-if="!data" class="muted">正在读取订单…</p>
    <p v-else-if="!data.data.length" class="muted">所选范围暂无已同步订单</p>
    <div v-else class="data-table-scroll orders-table" tabindex="0" role="region" aria-label="逐笔账单">
      <table>
        <thead>
          <tr>
            <th>订单</th><th>资源与归属</th><th>服务期</th><th>订单金额</th><th>现金实付</th>
            <th v-if="basis === 'service_period'">本范围分摊</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in data.data" :key="row.order_no">
            <th scope="row">
              {{ timeLabel(row.created_at) }} · {{ kinds[row.order_type] || row.order_type }}
              <small class="muted">{{ row.order_no }}</small>
            </th>
            <td>
              {{ billingLabels[row.category] }} · {{ row.name || row.resource_id || '资源待核对' }}
              <small class="muted">{{ row.instance_id ? instanceNames[row.instance_id] || row.instance_id : '仅归项目' }}</small>
            </td>
            <td>{{ timeLabel(row.start_at) }} 至 {{ timeLabel(row.end_at) }}</td>
            <td>
              {{ billMoneyLabel(row.amounts.order_amount_cny) }}
              <small v-if="row.amounts.free_credit_cny !== '0.00' && row.amounts.free_credit_cny !== null" class="muted">
                赠送抵扣 {{ billMoneyLabel(row.amounts.free_credit_cny) }}
              </small>
              <small v-if="row.amounts.coupon_cny !== '0.00' && row.amounts.coupon_cny !== null" class="muted">
                代金券抵扣 {{ billMoneyLabel(row.amounts.coupon_cny) }}
              </small>
            </td>
            <td>{{ billMoneyLabel(row.amounts.cash_cny) }}</td>
            <td v-if="basis === 'service_period'">
              {{ billMoneyLabel(row.selected_amounts.cash_cny) }}
              <small v-if="row.allocation === 'order_date'" class="muted">按订单日归集</small>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="offset || (data && data.total > perPage)" class="order-pagination">
      <ElButton size="small" :disabled="!offset" @click="offset = Math.max(0, offset - perPage)">上一页</ElButton>
      <span v-if="data" class="muted">共 {{ data.total }} 笔</span>
      <ElButton size="small" :disabled="!data || offset + perPage >= data.total" @click="offset += perPage">下一页</ElButton>
    </div>
  </details>
</template>

<style scoped>
summary { cursor: pointer; font-weight: 600; }
.order-filter, .order-pagination { display: flex; align-items: center; gap: 8px; }
.orders-table { --table-min-width: 48rem; }
small { display: block; }
</style>
