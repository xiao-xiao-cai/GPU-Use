<script setup lang="ts">
import { computed, onScopeDispose, reactive, ref, shallowRef, watch } from 'vue'
import { query, readSnapshot, request, type Watch } from '../../api/client'
import { planRental, type RentalOption, type RentalPool, type RentalRequest, type RentalResult } from '../../api/costs'
import { useLocationNames } from '../../catalog'
import { errorMessage, localInput, timeLabel, unixTime } from '../../display'
import { durationLabel, moneyLabel, rateLabel } from './format'
import MetricHelp from './MetricHelp.vue'
import { useDashboardQuery } from './useDashboardQuery'

const props = defineProps<{ projectId: string }>()
const help = {
  replay: '在未来 24 小时内比较找卡时机，并回放过去 7 天的同一时间段。某天只要有一次探测确认所需规格可租，该天就算达标。\n历史达标率＝达标天数 ÷ 纳入回放的天数（含结果未知的天）。覆盖率＝有结果的探测次数 ÷ 计划探测次数（每 5 分钟一次）。推荐要求至少 3 天有采样，且覆盖率、达标率均至少 80%。',
  cutoff: '最迟开始找卡＝最迟开训时间 − 准备时长。未填写最迟开训时间时，使用计划开训时间。',
  advance: '假设开始找卡时立即拿到卡。提前持有时长＝计划开训时间 − 找卡时间 − 准备时长，最少为 0。它表示准备完成后到开训前仍需付费的等待时长。',
  cost: '预计费用＝手填整机算力费率（元/实例小时）×（提前持有＋准备＋训练）的小时数，假设开始找卡就能拿到卡。缺价时保留库存比较，费用显示未知。',
  savings: '预计少花的费用＝同一资源池的费率 ×（现在找卡的提前持有小时数 − 推荐时机的提前持有小时数）。',
  stock: '平台最近确认所需规格可租，且还未超过找卡截止时间时，显示可尝试。',
  now: '以当前时间为找卡起点，显示试算费用、提前持有时长和历史达标情况，供对照。是否可立即尝试请看最新库存。',
  later: '每 5 分钟比较一个晚于现在的找卡起点。达标后优先选有费率且预计费用更低的方案，费用相同时依次比较历史达标率和找卡时间，优先达标率高、找卡更晚的方案。',
}
const { zoneName } = useLocationNames()
const poolPath = computed(() => '/inventory/pools')
const watchPath = computed(() => '/watches' + query({ project_id: props.projectId }))
const { data: inventory, fault: poolFault } = useDashboardQuery(poolPath, readSnapshot<RentalPool>)
const { data: watches, fault: watchFault } = useDashboardQuery(watchPath, (path, signal) => request<Watch[]>(path, { signal }))

const gpuModel = ref('')
const cards = ref(1)
const start = ref(localInput(Math.floor(Date.now() / 1000) + 3600))
const latest = ref('')
const training = ref<number | undefined>(120)
const preparation = ref<number | undefined>(15)
const selected = ref<string[]>([])
const rates = reactive<Record<string, string>>({})
const result = shallowRef<RentalResult | null>(null)
const message = ref('')
const running = ref(false)
let pending: AbortController | null = null

const poolType = { exclusive: '普通', spot: '抢占式' } as const
const models = computed(() => [...new Set(inventory.value?.data.map(row => row.gpu_model) ?? [])].sort())
const bound = computed(() => (watches.value ?? []).filter(row => row.binding_status === 'bound'))
function describe(row: Pick<RentalPool, 'region' | 'zone' | 'pool'>) {
  return zoneName(row) + ' · ' + poolType[row.pool]
}
const options = computed(() => [
  ...(inventory.value?.data ?? [])
    .filter(row => row.gpu_model === gpuModel.value && row.state !== 'unsupported')
    .map(row => ({
      key: [row.region, row.zone, row.pool].join('/'), label: describe(row),
      candidate: { region: row.region, zone: row.zone, pool: row.pool },
    })),
  ...bound.value.filter(row => row.spec.gpu_model === gpuModel.value && row.spec.gpu === cards.value)
    .map(row => ({ key: row.id, label: row.name + ' · ' + zoneName(row.spec), candidate: { watch_id: row.id } })),
])
const choices = computed(() => selected.value.flatMap(key => {
  const item = options.value.find(option => option.key === key)
  return item ? [item] : []
}))
const payload = computed<RentalRequest>(() => ({
  gpu_model: gpuModel.value,
  cards: cards.value,
  target_start: unixTime(start.value),
  latest_start: unixTime(latest.value || start.value),
  preparation_minutes: preparation.value ?? -1,
  training_minutes: training.value ?? -1,
  candidates: choices.value.map(({ key, candidate }) => ({
    ...candidate, hourly_rate: rates[key]?.trim() || undefined,
  })),
}))

function fromWatch(id: string) {
  const watch = bound.value.find(item => item.id === id)
  if (!watch) return
  gpuModel.value = watch.spec.gpu_model
  cards.value = watch.spec.gpu
  selected.value = [id]
}

function history(option: RentalOption | null): string {
  if (!option || option.history.coverage === null) return '暂无可比较的历史窗口'
  const item = option.history
  return `${item.hit_days}/${item.days} 天达标 · 覆盖 ${rateLabel(item.coverage)} · ${item.unknown_days} 天结果未知`
}

const resultMessages: Record<RentalResult['status'], string> = {
  ready: '',
  availability_only: '达标方案缺少费率，可补填后比较费用。',
  no_window: '历史窗口中暂无方案达到库存门槛。',
  insufficient: '历史采样不足，暂无法推荐。',
  deadline_too_close: '现在开始已无法留足准备时间。',
}

function invalidate() {
  pending?.abort()
  pending = null
  result.value = null
  running.value = false
  message.value = ''
}

watch(gpuModel, () => { selected.value = [] }, { flush: 'sync' })
watch(cards, () => { selected.value = selected.value.filter(key => options.value.some(item => item.key === key)) }, { flush: 'sync' })
watch(() => JSON.stringify(payload.value), invalidate, { flush: 'sync' })
onScopeDispose(invalidate)

async function submit() {
  if (running.value) return
  const body = payload.value
  if (!Number.isFinite(body.target_start) || !Number.isFinite(body.latest_start) || !body.candidates.length) {
    message.value = '请选择开训时间和至少一个候选资源池。'
    return
  }
  const controller = new AbortController()
  pending = controller
  running.value = true
  message.value = ''
  try {
    const response = await planRental(props.projectId, body, controller.signal)
    if (!controller.signal.aborted) result.value = response
  } catch (error) {
    if (!controller.signal.aborted) message.value = errorMessage(error)
  } finally {
    if (pending === controller) {
      pending = null
      running.value = false
    }
  }
}
</script>

<template>
  <section class="panel" aria-labelledby="rental-heading">
    <div class="planner-heading">
      <h2 id="rental-heading">占卡时机试算</h2>
      <MetricHelp label="占卡试算与历史达标率" :content="help.replay" />
    </div>
    <ElAlert v-if="poolFault || watchFault" title="候选规格读取失败" :description="poolFault || watchFault"
      type="warning" show-icon :closable="false" />
    <form class="planner-form" @submit.prevent="submit">
      <div class="planner-fields">
        <label v-if="bound.length">从关注规格带入
          <ElSelect aria-label="从关注规格带入" placeholder="选择关注规格" @change="fromWatch">
            <ElOption v-for="row in bound" :key="row.id" :label="row.name" :value="row.id" />
          </ElSelect>
        </label>
        <label>GPU 型号
          <ElSelect v-model="gpuModel" aria-label="GPU 型号" placeholder="选择机型">
            <ElOption v-for="model in models" :key="model" :label="model" :value="model" />
          </ElSelect>
        </label>
        <label>单实例卡数
          <ElSelect v-model="cards" aria-label="单实例卡数">
            <ElOption v-for="count in [1, 2, 4, 8]" :key="count" :label="String(count)" :value="count" />
          </ElSelect>
        </label>
        <label class="pool-field">候选可用区与资源池
          <ElSelect v-model="selected" aria-label="候选可用区与资源池" multiple collapse-tags :multiple-limit="8" placeholder="选择候选">
            <ElOption v-for="option in options" :key="option.key" :label="option.label" :value="option.key" />
          </ElSelect>
        </label>
        <label>计划开训（北京时间）
          <ElDatePicker v-model="start" type="datetime" value-format="YYYY-MM-DD[T]HH:mm" format="YYYY-MM-DD HH:mm"
            placeholder="选择计划开训时间" />
        </label>
        <label>最迟开训（北京时间）
          <ElDatePicker v-model="latest" type="datetime" value-format="YYYY-MM-DD[T]HH:mm" format="YYYY-MM-DD HH:mm"
            placeholder="默认同计划开训时间" />
        </label>
        <label>训练分钟
          <ElInputNumber v-model="training" :min="1" :max="43200" :precision="0" aria-label="训练分钟" />
        </label>
        <label>准备分钟
          <ElInputNumber v-model="preparation" :min="0" :max="1440" :precision="0" aria-label="准备分钟" />
        </label>
      </div>
      <fieldset v-if="choices.length">
        <legend>可选算力费率（元/实例小时）<MetricHelp label="试算费用" :content="help.cost" /></legend>
        <div class="planner-fields">
          <label v-for="item in choices" :key="item.key">{{ item.label }}
            <ElInput v-model="rates[item.key]" :aria-label="item.label + ' 费率'" inputmode="decimal" placeholder="缺价时仍可比较库存" />
          </label>
        </div>
      </fieldset>
      <div><ElButton type="primary" native-type="submit" :loading="running" :disabled="!choices.length">比较占卡方案</ElButton></div>
    </form>

    <p v-if="message" role="alert">{{ message }}</p>
    <template v-if="result">
      <p role="status">
        {{ resultMessages[result.status] }} 最迟开始找卡 {{ timeLabel(result.latest_search_at) }}
        <MetricHelp label="最迟开始找卡" :content="help.cutoff" />
      </p>
      <p v-if="result.recommendation">
        <strong>建议 {{ timeLabel(result.recommendation.start_search_at) }} 从{{ describe(result.candidates[result.recommendation.candidate_index]) }}找卡</strong>
        · 提前持有 {{ durationLabel(result.recommendation.advance_hold_seconds) }}
        <MetricHelp label="提前持有" :content="help.advance" />
        · 预计 {{ moneyLabel(result.recommendation.estimated_cost_cny) }}
        <MetricHelp label="预计费用" :content="help.cost" />
        <template v-if="Number(result.recommendation.savings_vs_now_cny) > 0">
          · 比现在占同一资源池少 {{ moneyLabel(result.recommendation.savings_vs_now_cny ?? null) }}
          <MetricHelp label="预计节省" :content="help.savings" />
        </template>
        <template v-if="result.incomplete_prices"> · 部分达标方案缺价</template>
      </p>
      <div class="data-table-scroll planner-table" tabindex="0" role="region" aria-label="占卡方案比较">
        <table>
          <thead>
            <tr>
              <th>候选</th>
              <th>最新库存<MetricHelp label="最新库存" :content="help.stock" /></th>
              <th>现在开始找<MetricHelp label="现在开始找" :content="help.now + '\n' + help.advance" /></th>
              <th>较晚达标方案<MetricHelp label="较晚达标方案" :content="help.later" /></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in result.candidates" :key="row.watch_id ?? [row.region, row.zone, row.pool].join(':')">
              <th scope="row">{{ describe(row) }}</th>
              <td>
                {{ row.remaining_cards === null ? '未知' : row.remaining_cards + ' 卡' }}
                <small>{{ row.can_try_now ? '可尝试' : row.available === false ? '规格暂不可租' : row.available === true ? '已过找卡截止' : '规格待核对' }}</small>
                <small v-if="row.capacity_checked_at">规格探测 {{ timeLabel(row.capacity_checked_at) }}</small>
                <small v-if="row.provider_updated_at">{{ timeLabel(row.provider_updated_at) }} 库存更新</small>
                <small v-if="row.known_samples">近 7 天 {{ row.available_samples }} / {{ row.known_samples }} 次探测可租</small>
              </td>
              <td>
                <template v-if="row.now">
                  {{ moneyLabel(row.now.estimated_cost_cny) }} · 提前持有 {{ durationLabel(row.now.advance_hold_seconds) }}
                  <small>{{ history(row.now) }}</small>
                </template>
                <template v-else>已过尝试截止</template>
              </td>
              <td>
                <template v-if="row.later">
                  {{ timeLabel(row.later.start_search_at) }} · {{ moneyLabel(row.later.estimated_cost_cny) }}
                  <small>{{ history(row.later) }} · 提前持有 {{ durationLabel(row.later.advance_hold_seconds) }}</small>
                </template>
                <template v-else>暂无达标方案</template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>

<style scoped>
.planner-heading { display: flex; align-items: center; }
.planner-form, .planner-fields, .planner-fields label { display: grid; gap: 8px; }
.planner-fields { grid-template-columns: repeat(auto-fit, minmax(195px, 1fr)); }
.planner-fields label { align-content: start; }
.planner-fields :deep(.el-date-editor.el-input), .planner-fields :deep(.el-input-number) { width: 100%; }
.pool-field { min-width: 0; }
fieldset { margin: 0; padding: 8px 12px 12px; border: 1px solid var(--line); border-radius: 6px; }
.planner-table { --table-min-width: 34rem; }
table { width: 100%; border-collapse: collapse; text-align: left; font-size: .9rem; }
th, td { padding: .55rem .35rem; border-bottom: 1px solid var(--line); vertical-align: top; }
td small { display: block; color: var(--muted); }
</style>
