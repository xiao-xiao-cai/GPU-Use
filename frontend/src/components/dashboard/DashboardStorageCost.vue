<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { query } from '../../api/client'
import { readStorageCosts, type BillingBasis } from '../../api/costs'
import type { UsageRange } from '../../api/dashboard'
import { billMoneyLabel } from './format'
import { useDashboardQuery } from './useDashboardQuery'

const props = defineProps<{
  projectId: string; instanceId?: string; range: UsageRange; basis: BillingBasis
  initiallyOpen?: boolean; instanceNames: Record<string, string>
}>()
const resourceLabels: Record<string, string> = {
  udisk: '云硬盘', udisk_ssd: 'SSD 数据盘', rssd: 'RSSD 数据盘',
  udisk_system_disk: '系统盘', compsharecvolumn: '容器系统盘', ufs: '文件存储', ufile: '对象存储',
}
const open = ref(Boolean(props.initiallyOpen))
watch(() => props.initiallyOpen, value => { if (value) open.value = true })
const path = computed(() => open.value ? '/dashboard/costs/storage' + query({
  project_id: props.projectId, instance_id: props.instanceId, range: props.range, basis: props.basis,
}) : null)
const { data, fault, refresh } = useDashboardQuery(path, readStorageCosts)

function toggled(event: Event) {
  if (event.currentTarget instanceof HTMLDetailsElement) open.value = event.currentTarget.open
}
</script>

<template>
  <details class="panel storage-cost" :open="open" @toggle="toggled">
    <summary>存储账单明细</summary>
    <p v-if="fault" role="alert">{{ fault }} <ElButton link @click="refresh">重试</ElButton></p>
    <p v-else-if="!data" class="muted">正在读取存储费用…</p>
    <p v-else-if="!data.data.length" class="muted">
      {{ data.coverage.status === 'ready' ? '所选范围没有已入账的存储费用' : '所选范围暂无已同步的存储账单' }}
    </p>
    <template v-else>
      <p v-if="!instanceId && data.project_only.order_count" class="muted">
        仅归项目的现金实付 {{ billMoneyLabel(data.project_only.cash_cny) }}
      </p>
      <div class="data-table-scroll storage-cost-table" tabindex="0" role="region" aria-label="逐项存储费用">
        <table>
          <thead>
            <tr><th>存储资源</th><th>现金实付</th><th>订单金额</th><th>费用归属</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in data.data" :key="[row.resource_type, row.resource_id].join(':')">
              <th scope="row" :title="row.resource_id ?? undefined">
                {{ row.name ?? '资源待核对' }}
                <small class="muted">
                  {{ resourceLabels[row.resource_type] || row.resource_type }}
                </small>
              </th>
              <td>
                {{ billMoneyLabel(row.totals.cash_cny) }}
                <small v-if="row.totals.review_order_count" class="muted">
                  {{ row.totals.review_order_count }} 笔待核对
                </small>
              </td>
              <td>{{ billMoneyLabel(row.totals.order_amount_cny) }}</td>
              <td>
                <div v-for="item in row.allocations" :key="item.instance_id ?? ''">
                  {{ item.instance_id ? instanceNames[item.instance_id] || item.instance_id : '仅归项目' }}
                  · {{ billMoneyLabel(item.totals.cash_cny) }}
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </details>
</template>

<style scoped>
.storage-cost summary { cursor: pointer; font-weight: 600; }
.storage-cost-table { --table-min-width: 32rem; }
.storage-cost-table small { display: block; }
</style>
