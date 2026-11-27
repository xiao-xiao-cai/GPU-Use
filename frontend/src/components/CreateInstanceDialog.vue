<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue'
import { request, RequestError, requestId, type Channel, type InstancePurchasePreview, type InstanceQuote,
  type InstanceStorageSelection, type Run, type Spec } from '../api/client'
import { waitForNextRead } from '../api/refresh'
import { useLocationNames, type CatalogRow } from '../catalog'
import { chargeLabels, errorMessage, timeLabel, useSnapshot } from '../display'
import SpecFields from './SpecFields.vue'
import InstanceStorageFields from './InstanceStorageFields.vue'

const props = defineProps<{
  projectId: string; projectName?: string; operationBlock: string; channels?: Record<Channel, boolean>
}>()
const emit = defineEmits<{ close: []; showRun: [id: string]; created: [] }>()
const open = ref(true)
const spec = ref<Spec>({
  region: '', zone: '', gpu_model: '', gpu: 0, cpu: 0, memory_gb: 0,
  image_id: '', disks: [], charge_type: 'Postpay',
})
const form = ref<InstanceType<typeof SpecFields>>()
const storageForm = ref<InstanceType<typeof InstanceStorageFields>>()
const storage = ref<InstanceStorageSelection>({ disks: [] })
const retry = ref(false)
const windowMinutes = ref(1440)
const maxPrice = ref('')
const channels = ref<Channel[]>(['site'])
const email = ref('')
const { state: catalog, fault: catalogError, loading: catalogLoading, refresh: readCatalog } = useSnapshot<CatalogRow>(ref('/catalog'), 30_000)
const { zoneName } = useLocationNames()
const quote = ref<InstanceQuote>()
const preview = ref<InstancePurchasePreview>()
const quotedSpec = ref<Spec>()
const quotedSystemBacking = ref<'cloud' | 'local' | null>()
const run = ref<Run>()
const error = ref('')
const quoting = ref(false)
const submitting = ref(false)
const uncertainSubmit = ref(false)
const tracking = ref(false)
const now = ref(Math.floor(Date.now() / 1000))
const timer = window.setInterval(() => { now.value = Math.floor(Date.now() / 1000) }, 1000)
let quoteRequest: AbortController | undefined
let submitRequest: AbortController | undefined
let trackingRequest: AbortController | undefined
let confirmationId = ''

const resultLabels: Record<string, string> = {
  queued: '创建已排队', waiting: '等待后台处理', reconciling: '核对云端创建结果', manual_review: '需要人工核对',
  succeeded: '实例已创建并确认有卡', failed: '本次创建未完成', expired: '本次申请已过期',
  canceled: '本次申请已停止', missed: '本次申请已跳过', reviewed: '创建结果已核对',
}
const finalStates = ['succeeded', 'failed', 'expired', 'canceled', 'missed', 'manual_review', 'reviewed']
const requoteStates = ['failed', 'expired', 'canceled', 'missed']
const paidStorage = computed(() => storage.value.shared_storage?.mode === 'cfs_create'
  || storage.value.shared_storage?.mode === 'cfs_resize')
const needsPreview = computed(() => retry.value || paidStorage.value)
const quoteValid = computed(() => preview.value ? preview.value.expires_at > now.value
  : quote.value?.state === 'ready' && (quote.value.expires_at ?? 0) > now.value)
const confirmation = computed(() => preview.value || quote.value?.state === 'ready' ? quotedSpec.value : undefined)
const specLocked = computed(() => submitting.value || uncertainSubmit.value || Boolean(run.value && !requoteStates.includes(run.value.state)))
const canQuote = computed(() => !props.operationBlock && !specLocked.value && !quoting.value && !quoteValid.value && storageForm.value?.ready)
const quoteUnit = computed(() => ['Day', 'Month'].includes(quotedSpec.value?.charge_type ?? spec.value.charge_type) ? '元 / 整单' : '元 / 小时')
const channelLabels: Record<Channel, string> = { site: '站内通知', feishu: '飞书', email: '邮件' }
const invalidEmail = computed(() => channels.value.includes('email') && !/^[^\s@]+@[^\s@]+$/.test(email.value))
const diskSummary = computed(() => confirmation.value?.disks.map(disk => {
  if (disk.purpose === 'system') {
    const type = quotedSystemBacking.value === 'local' ? '本地盘' : disk.type
    return `系统盘 ${type} ${disk.size_gb} GB`
  }
  return `数据盘 ${disk.type} ${disk.size_gb} GB`
}).join('、'))
const storageSummary = computed(() => {
  const selected = confirmation.value?.shared_storage
  if (selected?.mode === 'us3') return '自动挂载 US3'
  if (selected?.mode === 'cfs_attach') return '挂载已有 CFS ' + selected.cfs_id
  if (selected?.mode === 'cfs_create') return `购买并挂载 CFS ${selected.name} · ${selected.size_gib} GiB · 上限 ¥${selected.max_price}`
  if (selected?.mode === 'cfs_resize') return `扩容并挂载 CFS ${selected.cfs_id} · ${selected.size_gib} GiB · 上限 ¥${selected.max_price}`
  return ''
})

function resetQuote() {
  quoteRequest?.abort()
  quoteRequest = undefined
  quoting.value = false
  quote.value = undefined
  preview.value = undefined
  quotedSpec.value = undefined
  quotedSystemBacking.value = undefined
  confirmationId = ''
  error.value = ''
}

watch(() => JSON.stringify([spec.value, storage.value, retry.value,
  needsPreview.value ? [windowMinutes.value, maxPrice.value, channels.value, email.value] : null]), () => {
  if (specLocked.value) return
  resetQuote()
  run.value = undefined
}, { flush: 'sync' })

function changeRetry(value: boolean | string | number) {
  const enabled = value === true
  if (enabled && !maxPrice.value && quoteValid.value && quote.value?.price != null) maxPrice.value = quote.value.price
  retry.value = enabled
}

onScopeDispose(() => {
  window.clearInterval(timer)
  quoteRequest?.abort()
  submitRequest?.abort()
  trackingRequest?.abort()
})

async function getQuote() {
  if (!canQuote.value) return
  const validation = form.value?.validate() || storageForm.value?.validate()
  if (validation) {
    error.value = validation
    return
  }
  const requestPreview = needsPreview.value
  if (requestPreview && (!/^\d+(\.\d{1,6})?$/.test(maxPrice.value) || Number(maxPrice.value) > 10000000)) {
    error.value = '请填写有效的实例报价上限。'
    return
  }
  if (retry.value && (!Number.isInteger(windowMinutes.value) || windowMinutes.value < 1 || windowMinutes.value > 10080)) {
    error.value = '请输入 1 至 10080 分钟的整数。'
    return
  }
  if (invalidEmail.value) {
    error.value = '请填写有效的收件地址。'
    return
  }
  resetQuote()
  run.value = undefined
  const active = new AbortController()
  quoteRequest = active
  const { signal } = active
  const snapshot = JSON.parse(JSON.stringify({
    ...spec.value, disks: storageForm.value?.resolvedDisks(), shared_storage: storage.value.shared_storage,
  })) as Spec
  const systemBacking = storageForm.value?.systemBacking
  quoting.value = true
  try {
    if (requestPreview) {
      const result = await request<InstancePurchasePreview>('/instance-purchase-requests/preview', {
        method: 'POST', signal,
        body: {
          project_id: props.projectId, spec: snapshot, retry: retry.value, max_price: maxPrice.value,
          window_seconds: retry.value ? windowMinutes.value * 60 : undefined, channels: channels.value.slice(),
          email: channels.value.includes('email') ? email.value : undefined,
        },
      })
      if (quoteRequest !== active || signal.aborted) return
      if (result.project_id !== props.projectId) throw new Error('预览所属项目与当前项目不符。')
      preview.value = result
      quotedSpec.value = result.spec
      quotedSystemBacking.value = systemBacking
      return
    }
    let result = await request<InstanceQuote>('/instance-quotes', { method: 'POST', body: { project_id: props.projectId, spec: snapshot }, signal })
    while (result.state === 'queued') {
      await waitForNextRead(signal, 1500)
      result = await request<InstanceQuote>('/instance-quotes/' + encodeURIComponent(result.id), { signal })
    }
    if (quoteRequest !== active || signal.aborted) return
    if (result.project_id !== props.projectId) throw new Error('报价所属项目与当前项目不符。')
    quote.value = result
    quotedSpec.value = result.spec ?? snapshot
    quotedSystemBacking.value = systemBacking
    if (result.state !== 'ready') error.value = result.reason || '未取得有效报价，请重新询价。'
  } catch (failure) {
    if (!signal.aborted) error.value = errorMessage(failure)
  } finally {
    if (quoteRequest === active) {
      quoteRequest = undefined
      quoting.value = false
    }
  }
}

async function track(id: string) {
  trackingRequest?.abort()
  const active = new AbortController()
  trackingRequest = active
  const { signal } = active
  tracking.value = true
  error.value = ''
  try {
    while (!signal.aborted) {
      const current = await request<Run>('/runs/' + encodeURIComponent(id), { signal })
      signal.throwIfAborted()
      if (current.project_id !== props.projectId) throw new Error('创建记录所属项目与当前项目不符。')
      run.value = current
      if (finalStates.includes(current.state)) {
        if (current.state === 'succeeded') emit('created')
        if (requoteStates.includes(current.state)) resetQuote()
        break
      }
      await waitForNextRead(signal, 2500)
    }
  } catch (failure) {
    if (!signal.aborted) error.value = errorMessage(failure)
  } finally {
    if (trackingRequest === active) {
      trackingRequest = undefined
      tracking.value = false
    }
  }
}

async function confirm() {
  if ((!quote.value && !preview.value) || submitting.value
    || (!uncertainSubmit.value && (!quoteValid.value || props.operationBlock))) return
  if (invalidEmail.value && !uncertainSubmit.value) {
    error.value = '请填写有效的收件地址。'
    return
  }
  const active = new AbortController()
  submitRequest = active
  const { signal } = active
  confirmationId ||= requestId()
  const path = preview.value ? '/instance-purchase-requests' : '/instances'
  const body = preview.value ? { preview_id: preview.value.preview_id, request_id: confirmationId }
    : { quote_id: quote.value?.id, request_id: confirmationId, channels: channels.value.slice(),
      email: channels.value.includes('email') ? email.value : undefined }
  submitting.value = true
  error.value = ''
  try {
    const result = await request<Run>(path, { method: 'POST', body, signal })
    signal.throwIfAborted()
    run.value = result
    uncertainSubmit.value = false
    void track(result.id)
  } catch (failure) {
    if (signal.aborted) return
    if (failure instanceof RequestError && [400, 401, 403, 404, 409, 422].includes(failure.status)) {
      uncertainSubmit.value = false
      resetQuote()
      error.value = errorMessage(failure)
    } else {
      uncertainSubmit.value = true
      error.value = '提交回执待确认。可用同一请求重新核对。'
    }
  } finally {
    submitting.value = false
    submitRequest = undefined
  }
}
</script>

<template>
  <ElDialog v-model="open" title="创建实例" width="min(48rem, calc(100% - 24px))" top="5vh" append-to-body
    :show-close="!submitting" :close-on-press-escape="!submitting" :close-on-click-modal="false" @closed="emit('close')">
    <ElForm class="create-instance-form" label-position="top" @submit.prevent>
      <p class="muted">当前项目 {{ projectName || projectId }} · 创建 1 台实例</p>
      <ElAlert v-if="operationBlock && !run" :title="operationBlock" type="warning" :closable="false" />
      <ElSkeleton v-if="catalogLoading && !catalog.success_at" animated :rows="3" aria-label="正在读取机型目录" />
      <div v-if="!catalogLoading && !catalog.data.length">
        <p>暂无可选机型。后台同步后可重新读取目录。</p>
        <ElButton @click="readCatalog">重新读取目录</ElButton>
      </div>
      <SpecFields ref="form" v-model="spec" :project-id="projectId" :catalog="catalog.data" :catalog-error="catalogError"
        :disabled="specLocked" allow-spot require-quantity managed-storage />
      <InstanceStorageFields ref="storageForm" v-model="storage" :project-id="projectId" :location="spec"
        :gpu-model="spec.gpu_model" :charge-type="spec.charge_type" :image-id="spec.image_id" :disabled="specLocked" paid />
      <fieldset class="purchase-fields" :disabled="specLocked">
        <legend>创建方式</legend>
        <ElCheckbox :model-value="retry" :disabled="specLocked" @update:model-value="changeRetry">库存不足时继续尝试创建</ElCheckbox>
        <template v-if="needsPreview">
          <div class="form-grid">
            <ElFormItem :label="`实例报价上限（${quoteUnit}）`">
              <ElInput v-model.trim="maxPrice" type="number" min="0" max="10000000" step="0.000001" :disabled="specLocked" />
            </ElFormItem>
            <ElFormItem v-if="retry" label="最长等待（分钟）">
              <ElInputNumber v-model="windowMinutes" :min="1" :max="10080" :step="1" :disabled="specLocked" />
            </ElFormItem>
          </div>
          <p v-if="retry" class="muted">从确认时开始计时。后台在等待期内核对库存和报价，最多购买 1 台。</p>
        </template>
        <ElFormItem label="结果通知">
          <ElCheckboxGroup v-model="channels" :disabled="specLocked">
            <ElCheckbox value="site" disabled>站内通知</ElCheckbox>
            <ElCheckbox value="feishu" :disabled="!channels.includes('feishu') && !props.channels?.feishu">飞书</ElCheckbox>
            <ElCheckbox value="email" :disabled="!channels.includes('email') && !props.channels?.email">邮件</ElCheckbox>
          </ElCheckboxGroup>
        </ElFormItem>
        <ElFormItem v-if="channels.includes('email')" label="收件地址">
          <ElInput v-model.trim="email" type="email" maxlength="254" :disabled="specLocked" />
        </ElFormItem>
      </fieldset>
      <p v-if="quoting" role="status">{{ needsPreview ? '后台正在核对配置和预算。' : '后台正在查库存和实时报价。' }}</p>
      <section v-if="confirmation && !run" class="quote-summary" aria-label="待确认报价">
        <h3>{{ preview ? (preview.retry ? '等待创建预览' : '创建预览') : '平台实时报价' }}</h3>
        <p class="quote-price">{{ preview ? `实例上限 ¥${preview.max_price}` : quote?.price }} {{ quoteUnit }}</p>
        <p>{{ zoneName(confirmation) }} · {{ confirmation.gpu_model }} × {{ confirmation.gpu }} ·
          {{ confirmation.cpu }} vCPU · {{ confirmation.memory_gb }} GB · {{ chargeLabels[confirmation.charge_type] }}
          <template v-if="confirmation.quantity"> · {{ confirmation.quantity }} {{ confirmation.charge_type === 'Day' ? '天' : '月' }}</template>
        </p>
        <p v-if="diskSummary">磁盘 {{ diskSummary }}</p>
        <p v-if="storageSummary">{{ storageSummary }}</p>
        <p>通知 {{ (preview?.channels ?? channels).map(channel => channelLabels[channel]).join('、') }}
          <template v-if="preview?.email || (channels.includes('email') && email)"> · {{ preview?.email || email }}</template>
        </p>
        <template v-if="preview">
          <p v-if="preview.retry">确认后开始等待，最长 {{ preview.window_seconds / 60 }} 分钟 · 最多 1 台</p>
          <p class="muted">{{ quoteValid ? '预览有效至 ' + timeLabel(preview.expires_at) : '预览已过期' }}</p>
        </template>
        <template v-else>
          <p class="muted">询价 {{ timeLabel(quote?.quoted_at) }} · {{ quoteValid ? '有效至 ' + timeLabel(quote?.expires_at) : '报价已过期' }}</p>
          <p class="muted">创建前会重新询价，报价上涨时停止。实际金额以平台订单为准。</p>
        </template>
      </section>
      <ElAlert v-if="run" :title="resultLabels[run.state] || run.state" :description="run.reason || undefined"
        :type="run.state === 'succeeded' ? 'success' : requoteStates.includes(run.state) || run.state === 'manual_review' ? 'warning' : 'info'"
        :closable="false" show-icon />
      <p v-if="run && preview?.retry && !finalStates.includes(run.state)" class="muted">
        等待截止 {{ timeLabel(run.deadline_at) }}<template v-if="run.next_check"> · 下次检测 {{ timeLabel(run.next_check) }}</template>
      </p>
      <ElAlert v-if="error" :title="error" type="warning" :closable="false" show-icon />
      <div class="create-instance-actions">
        <ElButton :disabled="submitting" @click="open = false">关闭</ElButton>
        <ElButton v-if="run" type="primary" link @click="emit('showRun', run.id)">查看任务中心</ElButton>
        <ElButton v-if="run && error && !tracking" @click="track(run.id)">重新读取结果</ElButton>
        <ElButton v-if="uncertainSubmit || (!run && quoteValid)" type="primary" :loading="submitting"
          :disabled="!!operationBlock && !uncertainSubmit" @click="confirm">
          {{ uncertainSubmit ? '核对提交回执' : preview?.retry ? '确认并等待创建' : '确认创建' }}
        </ElButton>
        <ElButton v-else-if="!run || requoteStates.includes(run.state)" type="primary" :loading="quoting"
          :disabled="!canQuote || !catalog.data.length" @click="getQuote">
          {{ needsPreview ? (preview ? '重新预览' : '预览创建') : quote || run ? '重新询价' : '获取报价' }}
        </ElButton>
      </div>
    </ElForm>
  </ElDialog>
</template>

<style scoped>
.create-instance-form { display: grid; gap: 14px; }
.create-instance-form p { margin: 0; }
.purchase-fields { display: grid; gap: 10px; min-width: 0; }
.purchase-fields :deep(.el-form-item) { min-width: 0; margin-bottom: 0; }
.quote-summary { display: grid; gap: 6px; border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
.quote-summary h3 { margin: 0; }
.quote-price { font-size: 1.3rem; font-weight: 650; }
.create-instance-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.create-instance-actions > .el-button { margin: 0; }
@media (max-width: 767px) {
  .create-instance-actions { justify-content: flex-start; }
}
</style>
