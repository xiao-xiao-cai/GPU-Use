<script setup lang="ts">
import { computed } from 'vue'
import type { StockReference } from '../api/dashboard'
import RatioRing from './RatioRing.vue'

const props = defineProps<{
  count?: number | null
  reference?: StockReference | null
  fallback: string
  hideReferenceNote?: boolean
}>()
const reference = computed(() => {
  const { count, reference } = props
  return reference && count != null && Number.isSafeInteger(count) && count >= 0
    && Number.isSafeInteger(reference.cards) && reference.cards > 0 && reference.cards >= count ? reference : null
})
const peakTime = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'short' })
const note = computed(() => {
  if (reference.value?.source !== 'observed_peak') return '平台资源池总卡数'
  const at = reference.value.peak_at
  return '同可用区、同机型、同资源池的历史最高剩余卡数，出现更高值时更新。'
    + (at ? ' 峰值时间 ' + peakTime.format(at * 1000) + '（北京时间）' : '')
})
</script>

<template>
  <span class="stock-value">
    <template v-if="reference && count != null">
      <span class="stock-amount"><strong>剩余 {{ count }} / {{ reference.cards }} 张</strong>
        <RatioRing :ratio="count / reference.cards" />
      </span>
      <small v-if="!hideReferenceNote" class="muted" :title="note">{{ reference.source === 'observed_peak' ? '历史峰值参照' : '平台总量' }}</small>
    </template>
    <template v-else>
      <strong>{{ count != null ? '剩余 ' + count + ' 张' : fallback }}</strong>
      <small v-if="count != null && !hideReferenceNote" class="muted">暂无历史峰值</small>
    </template>
  </span>
</template>

<style scoped>
.stock-value { display: grid; gap: .13rem; }
.stock-amount { display: flex; align-items: center; gap: .25rem .45rem; flex-wrap: wrap; }
.stock-value small { font-weight: 400; }
</style>
