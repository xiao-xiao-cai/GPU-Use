<script setup lang="ts">
import { ref } from 'vue'
import { QuestionFilled } from '@element-plus/icons-vue'
import type { TooltipInstance } from 'element-plus'

defineProps<{ label: string; content: string }>()
const tooltip = ref<TooltipInstance>()
const popperStyle = {
  maxWidth: 'min(23rem, calc(100vw - 2rem))',
  whiteSpace: 'pre-line',
  lineHeight: '1.55',
}

function open(event: MouseEvent) {
  if (event.currentTarget instanceof HTMLButtonElement) event.currentTarget.focus({ preventScroll: true })
  tooltip.value?.onOpen()
}
</script>

<template>
  <ElTooltip ref="tooltip" :content="content" :trigger="['hover', 'focus']" :trigger-keys="[]"
    :popper-style="popperStyle" placement="top">
    <button type="button" class="metric-help" :aria-label="label + '说明'"
      @click.stop="open" @keydown.esc.stop="tooltip?.onClose(undefined, 0)">
      <QuestionFilled aria-hidden="true" />
    </button>
  </ElTooltip>
</template>

<style scoped>
.metric-help {
  display: inline-grid;
  place-items: center;
  vertical-align: middle;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}
.metric-help:hover, .metric-help:focus-visible { color: var(--accent); }
.metric-help svg { width: 14px; height: 14px; pointer-events: none; }
</style>
