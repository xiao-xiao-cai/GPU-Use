<script setup lang="ts">
import { computed } from 'vue'
import { query, type Instance, type StorageResource } from '../api/client'
import { useLocationNames } from '../catalog'
import { chargeLabels, storagePriceLabels, timeLabel, useSnapshot } from '../display'
import RatioRing from './RatioRing.vue'
import { valueLabel } from './dashboard/format'

const props = defineProps<{ projectId: string; view: 'history' | 'storage' }>()
const { zoneName } = useLocationNames()
const projectQuery = computed(() => query({ project_id: props.projectId }))
const history = useSnapshot<Instance>(computed(() => props.view === 'history' ? '/instance-history' + projectQuery.value : null), 60_000)
const storage = useSnapshot<StorageResource>(computed(() => props.view === 'storage' ? '/instance-storage' + projectQuery.value : null), 30_000)
const source = computed(() => props.view === 'history' ? history : storage)
const snapshot = computed(() => source.value.state.value)
const error = computed(() => source.value.fault.value || snapshot.value.error)
const periods: Record<string, string> = { Postpay: '小时', Dynamic: '小时', Day: '天', Month: '月', Year: '年' }
const usageLabels: Partial<Record<NonNullable<StorageResource['usage_state']>, string>> = {
  stopped: '实例已关机', mount_unverified: '用量待核对', no_sample: '暂无用量数据', stale: '监控已过期',
}

function instanceLink(id: string) {
  return '#instances' + query({ project: props.projectId, instance: id })
}

function mountStatus(row: StorageResource) {
  return ({ Attaching: '挂载中', Detaching: '卸载中', Failed: '创建失败' } as Record<string, string>)[row.status || '']
    || (row.mounted_instance_ids === null ? '挂载待核对' : row.mounted_instance_ids.length ? '已挂载' : '未挂载')
}

function usageStatus(row: StorageResource) {
  return (row.usage_state && usageLabels[row.usage_state]) || (row.mounted_instance_ids?.length ? '暂无用量数据' : '')
}
</script>

<template>
  <div class="asset-list">
    <div class="asset-toolbar">
      <span v-if="view === 'storage'" class="muted">最近完整同步 {{ timeLabel(snapshot.success_at) }}</span>
      <a v-if="view === 'storage'" :href="'#dashboard' + query({ project: projectId, tab: 'cost', storage: '1' })">
        查看存储账单
      </a>
      <ElButton :loading="source.loading.value" @click="source.refresh">刷新显示</ElButton>
    </div>
    <ElAlert v-if="error" :title="view === 'storage' ? '部分存储数据待同步' : '归档读取失败'"
      :description="error" type="warning" :closable="false" show-icon />
    <ElSkeleton v-if="source.loading.value && !snapshot.data.length && !snapshot.success_at" animated :rows="3" />
    <template v-else-if="view === 'history'">
      <ul v-if="history.state.value.data.length" class="row-list">
        <li v-for="instance in history.state.value.data" :key="instance.id">
          <div class="asset-line">
            <a :href="instanceLink(instance.id)"><strong>{{ instance.name || instance.id }}</strong></a>
            <ElTag size="small" :type="instance.archive_status === 'missing' ? 'warning' : 'info'">
              {{ instance.archive_status === 'missing' ? '已离开清单' : '已释放' }}
            </ElTag>
          </div>
          <div class="asset-line muted">
            <span v-if="instance.gpu_model">{{ instance.gpu_model }}<template v-if="instance.gpu"> × {{ instance.gpu }}</template></span>
            <span v-if="instance.cpu">{{ instance.cpu }} vCPU · {{ instance.memory_gb ?? '未知' }} GB</span>
            <span v-if="instance.zone">{{ zoneName(instance) }}</span>
            <span>{{ timeLabel(instance.archived_at) }}</span>
          </div>
        </li>
      </ul>
      <ElEmpty v-else-if="!error" description="暂无已释放实例" :image-size="64" />
    </template>
    <template v-else>
      <ul v-if="storage.state.value.data.length" class="row-list">
        <li v-for="row in storage.state.value.data" :key="[row.zone, row.kind, row.id].join(':')" class="storage-row">
          <div>
            <div class="asset-line">
              <strong>{{ row.name }}</strong>
              <ElTag size="small" type="info">{{ row.kind === 'cfs' ? '云存储 Pro' : '云硬盘' }}</ElTag>
              <ElTag size="small" :type="row.stale ? 'warning' : 'info'">{{ row.stale ? '待同步 · ' : '' }}{{ mountStatus(row) }}</ElTag>
            </div>
            <div class="asset-line muted">
              <span>{{ row.size }} {{ row.kind === 'cfs' ? 'GiB' : 'GB' }}</span>
              <span v-if="row.kind === 'udisk'">{{ row.type }}</span>
              <span>{{ zoneName(row) }}</span>
              <span>{{ chargeLabels[row.charge_type || ''] || row.charge_type || '计费待核对' }}</span>
              <span v-if="row.expires_at">到期 {{ timeLabel(row.expires_at) }}</span>
            </div>
            <div class="asset-line">
              <span v-if="row.price" :title="'报价时间 ' + timeLabel(row.price.observed_at)">
                ¥{{ row.price.amount }}<template v-if="periods[row.charge_type || '']">/{{ periods[row.charge_type || ''] }}</template>
                · {{ storagePriceLabels[row.price.source] }}{{ row.price.stale ? '待更新' : '' }}
              </span>
              <span v-else class="muted">价格待获取</span>
              <template v-for="target in row.instances" :key="target.id">
                <a v-if="target.active" :href="instanceLink(target.id)">{{ target.name }}</a>
                <span v-else class="muted">{{ target.name }} · 实例待核对</span>
              </template>
            </div>
          </div>
          <div v-if="row.usage" class="storage-usage"
            :title="'按云硬盘容量和平台使用率估算 · 采样 ' + timeLabel(row.usage.sampled_at)">
            <div class="asset-line">
              <strong>已用约 {{ valueLabel(row.usage.used_gb) }} / {{ valueLabel(row.size, ' GB') }}</strong>
              <RatioRing :ratio="row.usage.percent / 100" />
            </div>
            <small class="muted">监控采样 {{ timeLabel(row.usage.sampled_at) }}</small>
          </div>
          <small v-else-if="usageStatus(row)" class="muted">{{ usageStatus(row) }}</small>
        </li>
      </ul>
      <ElEmpty v-else-if="!error" :description="snapshot.success_at ? '暂无独立存储' : '等待后台同步存储'" :image-size="64" />
    </template>
  </div>
</template>

<style scoped>
.asset-list { display: grid; gap: 12px; }
.asset-toolbar, .asset-line { display: flex; align-items: baseline; gap: 6px 12px; flex-wrap: wrap; }
.asset-toolbar .el-button { margin-inline-start: auto; }
.asset-line + .asset-line { margin-top: 5px; }
.asset-line strong, .asset-line a { overflow-wrap: anywhere; }
.storage-row { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: .65rem 1rem; }
.storage-row > div { min-width: 0; max-width: 100%; }
.storage-usage .asset-line { flex-wrap: nowrap; align-items: center; }
.storage-usage strong { min-width: 0; }
.storage-usage .asset-line > :last-child { flex-shrink: 0; }
.storage-usage small { display: block; margin-top: .2rem; }
</style>
