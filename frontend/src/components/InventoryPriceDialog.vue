<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue'
import type { ChargeType } from '../api/client'
import { requestInventoryPrice, type InventoryPrice } from '../api/inventoryPrices'
import { specLabel, useLocationNames, type CatalogRow } from '../catalog'
import { errorMessage, timeLabel } from '../display'
import { moneyLabel } from './dashboard/format'

const props = defineProps<{ projectId: string; target: CatalogRow; initialSpecId?: string }>()
const open = defineModel<boolean>({ required: true })
const { zoneName } = useLocationNames()
const options = computed(() => props.target.specs?.filter(item => item.catalog_spec_id) ?? [])
const specId = ref('')
const charge = ref<ChargeType>('Postpay')
const result = ref<InventoryPrice | null>(null)
const fault = ref('')
const waiting = ref(false)
const unitLabels = { hour: '小时', day: '天', month: '月' }
let pending: AbortController | null = null

watch([open, () => props.target, () => props.initialSpecId], ([visible]) => {
  if (!visible) return
  specId.value = props.initialSpecId ?? (options.value.length === 1 ? options.value[0].catalog_spec_id! : '')
  charge.value = props.target.instance_type === 'spot' ? 'Spot' : 'Postpay'
}, { immediate: true })

async function loadPrice() {
  pending?.abort()
  pending = null
  result.value = null
  fault.value = ''
  waiting.value = false
  if (!open.value || !props.projectId || !specId.value) return
  const controller = new AbortController()
  pending = controller
  waiting.value = true
  try {
    const price = await requestInventoryPrice(props.projectId, specId.value, charge.value, controller.signal)
    if (!controller.signal.aborted) result.value = price
  } catch (error) {
    if (!controller.signal.aborted) fault.value = errorMessage(error)
  } finally {
    if (pending === controller) {
      pending = null
      waiting.value = false
    }
  }
}

watch([open, () => props.projectId, specId, charge], loadPrice, { immediate: true })
onScopeDispose(() => pending?.abort())
</script>

<template>
  <ElDialog v-model="open" title="规格价格" width="min(34rem, calc(100% - 24px))" destroy-on-close append-to-body>
    <p class="price-location">{{ target.gpu_model }} · {{ zoneName(target, target.zone_name) }}</p>
    <ElForm label-position="top" @submit.prevent>
      <ElFormItem label="GPU、CPU 与内存">
        <ElSelect v-model="specId" aria-label="报价规格" placeholder="选择完整规格">
          <ElOption v-for="item in options" :key="item.catalog_spec_id" :label="specLabel(item)" :value="item.catalog_spec_id!" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem label="计费方式">
        <ElSelect v-model="charge" aria-label="报价计费方式">
          <ElOption v-if="target.instance_type === 'spot'" label="抢占式" value="Spot" />
          <template v-else>
            <ElOption label="按时" value="Postpay" />
            <ElOption label="按天" value="Day" />
            <ElOption label="包月" value="Month" />
          </template>
        </ElSelect>
      </ElFormItem>
    </ElForm>
    <p v-if="!projectId" class="muted">请选择项目以查询价格。</p>
    <p v-else-if="waiting" role="status">正在查询价格…</p>
    <ElAlert v-else-if="fault" title="价格暂不可用" :description="fault" type="warning" show-icon :closable="false" />
    <dl v-else-if="result" class="quote" aria-live="polite">
      <dt>{{ result.stale ? '上次' : '' }}{{ result.source === 'account' ? '账户报价' : '平台目录价' }}</dt>
      <dd>{{ moneyLabel(result.amount) }}<small> / 实例{{ unitLabels[result.billing_unit] }}</small></dd>
    </dl>
    <p v-if="result?.stale" class="muted">等待后台更新价格。</p>
    <p v-else-if="result?.source === 'catalog'" class="muted">当前账户报价不可用，已显示平台目录价。</p>
    <p v-if="result" class="muted">{{ timeLabel(result.observed_at) }} 查询 · 算力价格，磁盘和镜像费用另计。</p>
    <template v-if="fault || result?.stale" #footer>
      <ElButton type="primary" @click="loadPrice">重新读取</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.price-location { margin-top: 0; font-weight: 650; }
.quote { margin: .75rem 0; }
.quote dt { color: var(--muted); }
.quote dd { margin: .25rem 0 0; font-size: 1.4rem; font-weight: 650; }
.quote small { font-size: .85rem; font-weight: 500; }
</style>
