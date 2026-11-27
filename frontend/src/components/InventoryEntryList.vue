<script setup lang="ts">
import { ArrowRight } from '@element-plus/icons-vue'
import type { InventoryGroup } from '../api/inventoryOverview'
import { inventoryPriceSources, inventoryRangeLabel } from './inventoryPriceFormat'
import { useLocationNames, type CatalogSpec } from '../catalog'
import { timeLabel } from '../display'
import StockRemaining from './StockRemaining.vue'

defineProps<{
  rows: InventoryGroup[]
  listId: string
  projectId: string
  href: (row: InventoryGroup) => string
  stockLabel: (row: Pick<CatalogSpec, 'state' | 'remaining_cards'>) => string
}>()
const emit = defineEmits<{ open: [key: string] }>()
const { zoneName } = useLocationNames()
</script>

<template>
  <ul class="inventory-entries">
    <li v-for="row in rows" :key="row.key" class="inventory-entry">
      <a :id="listId + '-' + encodeURIComponent(row.key)" :href="href(row)" @click.exact="emit('open', row.key)">
        <div class="entry-model">
          <strong>{{ row.gpu_model }}</strong>
          <span>{{ zoneName(row, row.zone_name) }}</span>
          <small v-if="row.graphics_memory_gb !== null" class="muted">{{ row.graphics_memory_gb }} GB / 卡</small>
        </div>
        <div class="entry-pools metric-container">
          <div v-for="entry in row.pools" :key="entry.pool" class="entry-pool metric-grid metric-grid--three">
            <div>
              <small class="muted">{{ entry.pool === 'spot' ? '抢占池' : '普通池' }}</small>
              <StockRemaining :count="entry.state === 'known' ? entry.remaining_cards : null"
                :reference="entry.reference" :fallback="stockLabel(entry)" />
              <small v-if="entry.provider_updated_at" class="muted">{{ timeLabel(entry.provider_updated_at) }} 更新</small>
            </div>
            <div>
              <strong>{{ entry.specs.length ? entry.specs.length + ' 种规格' : '暂无目录规格' }}</strong>
              <small v-if="entry.card_counts.length" class="muted">{{ entry.card_counts.join(' / ') }} 卡</small>
            </div>
            <div>
              <strong>{{ inventoryRangeLabel(entry.hourly_range, projectId) }}</strong>
              <small v-if="entry.hourly_range.source" class="muted">
                {{ inventoryPriceSources[entry.hourly_range.source] }}<template v-if="entry.hourly_range.stale"> · 含上次报价</template>
              </small>
            </div>
          </div>
        </div>
        <ElIcon aria-hidden="true" class="entry-arrow"><ArrowRight /></ElIcon>
      </a>
    </li>
  </ul>
</template>

<style scoped>
.inventory-entries { container: inventory-entries / inline-size; list-style: none; display: grid; gap: 8px; margin: 0; padding: 0; }
.inventory-entry > a {
  display: grid; align-items: center; grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px; padding: 14px; color: inherit; text-decoration: none; border: 1px solid var(--line); border-radius: 8px;
}
.inventory-entry > a:hover, .inventory-entry > a:focus-visible { border-color: var(--accent); background: var(--el-fill-color-light); }
.entry-model, .entry-pools, .entry-pool > div { display: grid; gap: 3px; overflow-wrap: anywhere; }
.entry-model > strong { font-size: 1.12rem; }
.entry-pools { grid-row: 2; grid-column: 1 / -1; }
.entry-pool { gap: 10px; }
.entry-pool > div:last-child { grid-column: 1 / -1; }
.entry-pool + .entry-pool { border-top: 1px solid var(--line); margin-top: 6px; padding-top: 8px; }
.entry-arrow { grid-column: 2; grid-row: 1; color: var(--accent); }
@container inventory-entries (min-width: 42rem) {
  .inventory-entry > a { grid-template-columns: minmax(150px, 1fr) minmax(0, 3fr) auto; gap: 16px; }
  .entry-pools { grid-row: auto; grid-column: auto; }
  .entry-arrow { grid-column: auto; grid-row: auto; }
}
@container metric-panel (min-width: 30rem) { .entry-pool > div:last-child { grid-column: auto; } }
</style>
