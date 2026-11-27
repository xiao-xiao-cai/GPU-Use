<script setup lang="ts">
import { computed } from 'vue'
import { rateLabel } from './dashboard/format'

const props = defineProps<{ ratio: number }>()
const percent = computed(() => Math.max(0, Math.min(1, props.ratio)) * 100 + '%')
</script>

<template>
  <span class="ratio-ring">
    <span class="ratio-ring-track" :style="{ '--fill': percent }" aria-hidden="true"></span>
    <span>{{ rateLabel(ratio) }}</span>
  </span>
</template>

<style scoped>
.ratio-ring { display: inline-flex; align-items: center; gap: .35rem; white-space: nowrap; font-variant-numeric: tabular-nums; }
.ratio-ring-track {
  inline-size: 1.22rem;
  block-size: 1.22rem;
  border-radius: 50%;
  background: conic-gradient(var(--accent) var(--fill), var(--line) 0);
  mask: radial-gradient(farthest-side, transparent calc(100% - 3px), black calc(100% - 2px));
}
</style>
