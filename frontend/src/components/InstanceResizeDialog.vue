<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue'
import { query, request, requestId, type ResizeQuote, type ResizeSpec, type Run } from '../api/client'
import { errorMessage, timeLabel } from '../display'

const props = defineProps<{ projectId: string; instanceId: string; instanceName: string; operationBlock: string }>()
const emit = defineEmits<{ close: []; submitted: [run: Run] }>()
type Option = ResizeSpec & { id: string }
const path = '/instances/' + encodeURIComponent(props.instanceId)
const scope = query({ project_id: props.projectId })
const options = ref<Option[]>([])
const current = ref<ResizeSpec>()
const selected = ref('')
const quote = ref<ResizeQuote>()
const ready = ref(false)
const pending = ref(false)
const submitting = ref(false)
const error = ref('')
const now = ref(Date.now() / 1000)
let optionRequest: AbortController | undefined
let quoteRequest: AbortController | undefined
let submitRequest: AbortController | undefined
let receiptId = ''
const clock = window.setInterval(() => { now.value = Date.now() / 1000 }, 1000)

const canSubmit = computed(() => quote.value?.state === 'ready' && quote.value.available === true
  && quote.value.expires_at > now.value && !props.operationBlock && !submitting.value)
const money = computed(() => {
  const amount = quote.value?.amount
  if (amount == null) return '待报价'
  if (Number(amount) === 0) return '¥0'
  return amount.startsWith('-') ? '预计退款 ¥' + amount.slice(1) : '预计补差价 ¥' + amount
})

function specLabel(spec: ResizeSpec) {
  return spec.gpu + ' 张 GPU · ' + spec.cpu + ' vCPU · ' + spec.memory_gb + ' GB'
}

async function load() {
  optionRequest?.abort()
  const controller = new AbortController()
  optionRequest = controller
  error.value = ''
  try {
    const result = await request<{ options: Option[]; current: ResizeSpec }>(path + '/resize-options' + scope, {
      signal: controller.signal,
    })
    if (controller.signal.aborted) return
    options.value = result.options
    current.value = result.current
    ready.value = true
  } catch (failure) {
    if (!controller.signal.aborted) {
      error.value = errorMessage(failure)
      ready.value = true
    }
  }
}

watch(selected, () => {
  quoteRequest?.abort()
  quote.value = undefined
  pending.value = false
  error.value = ''
}, { flush: 'sync' })

async function preview() {
  if (pending.value || submitting.value || !selected.value) return
  const controller = new AbortController()
  quoteRequest = controller
  pending.value = true
  quote.value = undefined
  receiptId = requestId()
  error.value = ''
  try {
    let result = await request<ResizeQuote>(path + '/resize-quotes' + scope, {
      method: 'POST', body: { spec_id: selected.value }, signal: controller.signal,
    })
    while (!controller.signal.aborted && result.state === 'queued') {
      await new Promise(resolve => window.setTimeout(resolve, 1000))
      if (controller.signal.aborted) return
      result = await request<ResizeQuote>(path + '/resize-quotes/' + result.id + scope, { signal: controller.signal })
    }
    if (!controller.signal.aborted) quote.value = result
  } catch (failure) {
    if (!controller.signal.aborted) error.value = errorMessage(failure)
  } finally {
    if (quoteRequest === controller) {
      quoteRequest = undefined
      pending.value = false
    }
  }
}

async function submit() {
  if (!canSubmit.value || !quote.value || submitRequest) return
  const controller = new AbortController()
  submitRequest = controller
  submitting.value = true
  error.value = ''
  try {
    const run = await request<Run>(path + '/resize' + scope, {
      method: 'POST', body: { quote_id: quote.value.id, request_id: receiptId }, signal: controller.signal,
    })
    if (!controller.signal.aborted) emit('submitted', run)
  } catch (failure) {
    if (!controller.signal.aborted) error.value = errorMessage(failure)
  } finally {
    submitting.value = false
  }
}

onMounted(load)
onScopeDispose(() => {
  optionRequest?.abort()
  quoteRequest?.abort()
  submitRequest?.abort()
  window.clearInterval(clock)
})
</script>

<template>
  <ElDialog :model-value="true" title="更改实例规格" width="min(32rem, calc(100% - 24px))"
    :show-close="!submitting" :close-on-click-modal="false" :close-on-press-escape="!submitting"
    @update:model-value="visible => { if (!visible) emit('close') }">
    <ElSkeleton v-if="!ready" animated :rows="3" />
    <p v-if="current" class="muted">{{ instanceName }} · 当前 {{ specLabel(current) }}</p>
    <ElForm v-if="options.length" label-position="top" @submit.prevent="preview">
      <ElFormItem label="目标规格">
        <ElSelect v-model="selected" placeholder="选择规格" :disabled="submitting">
          <ElOption v-for="option in options" :key="option.id" :label="specLabel(option)" :value="option.id" />
        </ElSelect>
      </ElFormItem>
      <ElButton native-type="submit" :loading="pending" :disabled="!selected || submitting">查询差价与库存</ElButton>
    </ElForm>
    <ElEmpty v-else-if="ready && !error" description="当前目录没有其他可用规格" :image-size="64" />
    <ElAlert v-if="error || quote?.error" :title="error || quote?.error || ''" type="error" :closable="false" />
    <div v-if="quote?.state === 'ready' && quote.expires_at > now" class="resize-quote">
      <strong>{{ money }}</strong>
      <p>{{ quote.available === true ? '已核对目标规格库存' : quote.available === false ? '目标规格暂无资源' : '目标规格库存待核对' }}
        · 报价有效至 {{ timeLabel(quote.expires_at) }}</p>
      <p>改配完成后，实例保持关机。</p>
    </div>
    <p v-if="quote && (quote.state === 'expired' || (quote.state === 'ready' && quote.expires_at <= now))" role="status">
      报价已过期，请重新查询。
    </p>
    <template #footer>
      <ElButton v-if="!current && error" @click="load">重新加载</ElButton>
      <ElButton :disabled="submitting" @click="emit('close')">关闭</ElButton>
      <ElButton type="primary" :loading="submitting" :disabled="!canSubmit" @click="submit">确认改配</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.resize-quote { margin-top: 16px; padding: 12px; border: 1px solid var(--line); border-radius: 8px; }
.resize-quote p { margin: 6px 0 0; }
.el-alert { margin-top: 12px; }
</style>
