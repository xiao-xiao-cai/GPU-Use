<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { query, request, type ChargeType, type Disk, type InstanceStorageSelection, type SharedStorage, type Zone } from '../api/client'
import { waitForNextRead } from '../api/refresh'
import type { CatalogDisk } from '../catalog'
import { errorMessage } from '../display'

const props = defineProps<{
  projectId: string
  location: Zone
  gpuModel: string
  chargeType: ChargeType
  imageId: string
  disabled?: boolean
  paid?: boolean
  heading?: string
}>()
const selection = defineModel<InstanceStorageSelection>({ required: true })

interface CreateOptions {
  system_disk: Disk | null
  system_disk_backing?: 'cloud' | 'local' | null
  data_disks: CatalogDisk[]
  storage: 'none' | 'us3' | 'cfs'
  cfs: { id: string; name: string; size_gib: number; charge_type: string } | null
  cfs_known: boolean
  reason: string | null
  can_mount: boolean
}

const path = computed(() => {
  const { projectId, location, gpuModel, imageId, chargeType } = props
  if (!projectId || !location.region || !location.zone || !gpuModel || !imageId) return ''
  return '/instance-create-options' + query({
    project_id: projectId, region: location.region, zone: location.zone, gpu_model: gpuModel,
    instance_type: chargeType === 'Spot' ? 'spot' : 'uhost', image_id: imageId,
  })
})
const options = ref<CreateOptions>()
const loading = ref(false)
const fault = ref('')
const reload = ref(0)

watch([path, reload], async ([url], _previous, onCleanup) => {
  const controller = new AbortController()
  const { signal } = controller
  onCleanup(() => controller.abort())
  options.value = undefined
  fault.value = ''
  loading.value = Boolean(url)
  if (!url) return
  try {
    for (let attempts = 0; attempts < 10; attempts++) {
      const result = await request<CreateOptions>(url, { signal })
      signal.throwIfAborted()
      options.value = result
      loading.value = false
      if (result.storage !== 'cfs' || !result.can_mount || result.cfs_known) return
      await waitForNextRead(signal, 1800)
    }
  } catch (error) {
    if (!signal.aborted) fault.value = errorMessage(error)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}, { immediate: true, flush: 'sync' })

const boot = computed(() => options.value?.system_disk)
const ready = computed(() => Boolean(boot.value))
const systemBacking = computed(() => options.value?.system_disk_backing)
const dataDisks = computed(() => (options.value?.data_disks ?? []).filter(disk =>
  disk.purpose === 'data' && typeof disk.min_size_gb === 'number' && typeof disk.max_size_gb === 'number'
  && disk.min_size_gb > 0 && disk.min_size_gb <= disk.max_size_gb))
const dataBounds = (type: string) => dataDisks.value.find(disk => disk.type === type)
const selectedStorage = computed(() => selection.value.shared_storage)
const cfsReady = computed(() => options.value?.storage === 'cfs' && options.value.can_mount && options.value.cfs_known)
const cfs = computed(() => cfsReady.value ? options.value?.cfs : null)
const storageModes = computed(() => {
  const modes: { value: SharedStorage['mode']; label: string }[] = []
  if (options.value?.storage === 'us3' && options.value.can_mount) modes.push({ value: 'us3', label: '自动挂载 US3' })
  if (cfsReady.value && cfs.value) {
    modes.push({ value: 'cfs_attach', label: '挂载已有 CFS' })
    if (props.paid && cfs.value.size_gib < 2048) modes.push({ value: 'cfs_resize', label: '扩容后挂载 CFS' })
  } else if (cfsReady.value && props.paid) {
    modes.push({ value: 'cfs_create', label: '购买并挂载 CFS' })
  }
  return modes
})
const units = { Month: '包月', Year: '包年', Day: '按天', Dynamic: '按时' }

function sizeInBounds(disk: Disk) {
  const bounds = dataBounds(disk.type)
  return bounds && Number.isInteger(disk.size_gb) && disk.size_gb >= (bounds.min_size_gb ?? 0)
    && disk.size_gb <= (bounds.max_size_gb ?? 0)
}

function setDataDiskType(disk: Disk, type: string) {
  disk.type = type
  if (!sizeInBounds(disk)) disk.size_gb = dataBounds(type)?.min_size_gb ?? 0
}

function addDataDisk() {
  const first = dataDisks.value[0]
  if (!first || selection.value.disks.length >= 9) return
  selection.value.disks.push({ purpose: 'data', type: first.type, size_gb: first.min_size_gb ?? 0 })
}

function selectStorage(mode: SharedStorage['mode'] | '') {
  let shared_storage: SharedStorage | undefined
  if (mode === 'us3') shared_storage = { mode }
  else if (mode === 'cfs_attach' && cfs.value) shared_storage = { mode, cfs_id: cfs.value.id }
  else if (mode === 'cfs_create') {
    shared_storage = { mode, name: '', size_gib: 50, charge_type: 'Month', quantity: 1, max_price: '' }
  } else if (mode === 'cfs_resize' && cfs.value) {
    shared_storage = { mode, cfs_id: cfs.value.id, size_gib: Math.max(50, cfs.value.size_gib + 1), max_price: '' }
  }
  selection.value = { ...selection.value, shared_storage }
}

function validate() {
  if (!boot.value) return fault.value || options.value?.reason || '请选择完整规格并等待读取系统盘配置。'
  if (selection.value.disks.length > 9 || selection.value.disks.some(disk => disk.purpose !== 'data' || !sizeInBounds(disk))) {
    return '请核对当前配置支持的数据盘型号和容量，或移除不支持的数据盘。'
  }
  const storage = selectedStorage.value
  if (!storage) return ''
  if (!storageModes.value.some(mode => mode.value === storage.mode)) return '请重新选择当前配置支持的共享存储。'
  if ('cfs_id' in storage && storage.cfs_id !== cfs.value?.id) return '当前可挂载的 CFS 已变化，请重新选择。'
  if (storage.mode === 'cfs_create' || storage.mode === 'cfs_resize') {
    const minimum = storage.mode === 'cfs_resize' ? Math.max(50, (cfs.value?.size_gib ?? 0) + 1) : 50
    if (!Number.isInteger(storage.size_gib) || storage.size_gib < minimum || storage.size_gib > 2048) {
      return `请输入 ${minimum} 至 2048 GiB 的 CFS 容量。`
    }
    if (!/^\d+(\.\d{1,6})?$/.test(storage.max_price) || Number(storage.max_price) > 10000000) return '请填写有效的 CFS 金额上限。'
  }
  if (storage.mode === 'cfs_create' && (!storage.name.trim() || storage.name.trim().length > 63 || !Number.isInteger(storage.quantity)
    || storage.quantity < 1 || storage.quantity > 3650)) return '请填写 CFS 名称与购买周期。'
  return ''
}

function resolvedDisks(): Disk[] {
  return boot.value ? [boot.value, ...selection.value.disks].map(disk => ({ ...disk })) : []
}

defineExpose({ ready, systemBacking, validate, resolvedDisks })
</script>

<template>
  <fieldset class="storage-fields" :disabled="disabled">
    <legend>{{ heading || '磁盘与存储' }}</legend>
    <p v-if="!path" class="muted">选择可用区、GPU 型号及镜像后查看磁盘配置。</p>
    <p v-else-if="loading" role="status">正在读取系统盘和可选存储。</p>
    <ElAlert v-else-if="!boot" type="warning" :title="fault || options?.reason || '当前系统盘配置无法核实'" :closable="false">
      <ElButton size="small" :disabled="disabled" @click="reload++">重新读取</ElButton>
    </ElAlert>
    <template v-if="boot">
      <p><strong>系统盘</strong> {{ systemBacking === 'local' ? '本地盘' : boot.type }} · {{ boot.size_gb }} GB <span class="muted">平台配置</span></p>
      <div v-for="(disk, index) in selection.disks" :key="index" class="form-grid storage-disk">
        <ElFormItem label="数据盘类型">
          <ElSelect :model-value="disk.type" :disabled="disabled || !dataDisks.length"
            @change="(type: string) => setDataDiskType(disk, type)">
            <ElOption v-if="!dataBounds(disk.type)" :value="disk.type" :label="disk.type + '（不可用）'" disabled />
            <ElOption v-for="option in dataDisks" :key="option.type" :value="option.type" :label="option.type" />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="容量（GB）" :error="sizeInBounds(disk) ? undefined : '请核对容量范围'">
          <ElInputNumber v-model="disk.size_gb" :min="dataBounds(disk.type)?.min_size_gb ?? 1"
            :max="dataBounds(disk.type)?.max_size_gb ?? undefined" :step="1" :disabled="disabled || !dataBounds(disk.type)" />
        </ElFormItem>
        <ElButton :disabled="disabled" @click="selection.disks.splice(index, 1)">移除数据盘</ElButton>
      </div>
      <div v-if="dataDisks.length">
        <ElButton :disabled="disabled || selection.disks.length >= 9" @click="addDataDisk">添加数据盘</ElButton>
      </div>
      <p v-else class="muted">当前配置暂无可选数据盘。</p>
      <p v-if="options?.storage === 'cfs' && options.can_mount && !options.cfs_known" role="status">
        {{ fault || 'CFS 状态待同步，读取完成后即可选择。' }}
        <ElButton text size="small" :disabled="disabled" @click="reload++">重新读取</ElButton>
      </p>
      <ElFormItem v-if="storageModes.length || selectedStorage" label="共享存储（可选）">
        <ElSelect :model-value="selectedStorage?.mode ?? ''" :disabled="disabled" @change="selectStorage">
          <ElOption label="不挂载" value="" />
          <ElOption v-if="selectedStorage && !storageModes.some(item => item.value === selectedStorage?.mode)"
            label="原挂载方式不可用" :value="selectedStorage.mode" disabled />
          <ElOption v-for="item in storageModes" :key="item.value" :value="item.value" :label="item.label" />
        </ElSelect>
      </ElFormItem>
      <p v-if="cfs" class="muted">已有 CFS {{ cfs.name || cfs.id }} · {{ cfs.size_gib }} GiB · {{ cfs.charge_type }}</p>
      <p v-if="options?.reason" class="muted">{{ options.reason }}</p>
      <p v-if="options?.storage === 'us3' && !options.can_mount" class="muted">
        <a href="https://www.compshare.cn/docs/gpus/data/attachus3" target="_blank" rel="noopener noreferrer">平台 US3 挂载说明</a>
      </p>
      <div v-if="selectedStorage?.mode === 'cfs_create' || selectedStorage?.mode === 'cfs_resize'" class="form-grid">
        <template v-if="selectedStorage.mode === 'cfs_create'">
          <ElFormItem label="CFS 名称"><ElInput v-model.trim="selectedStorage.name" :disabled="disabled" maxlength="63" /></ElFormItem>
          <ElFormItem label="CFS 计费方式">
            <ElSelect v-model="selectedStorage.charge_type" :disabled="disabled">
              <ElOption v-for="(label, value) in units" :key="value" :value="value" :label="label" />
            </ElSelect>
          </ElFormItem>
          <ElFormItem label="购买周期数">
            <ElInputNumber v-model="selectedStorage.quantity" :min="1" :max="3650" :step="1" :disabled="disabled" />
          </ElFormItem>
        </template>
        <ElFormItem label="CFS 容量（GiB）">
          <ElInputNumber v-model="selectedStorage.size_gib"
            :min="selectedStorage.mode === 'cfs_resize' ? Math.max(50, (cfs?.size_gib ?? 0) + 1) : 50"
            :max="2048" :step="1" :disabled="disabled" />
        </ElFormItem>
        <ElFormItem label="CFS 金额上限（元）">
          <ElInput v-model.trim="selectedStorage.max_price" type="number" min="0" max="10000000" step="0.000001" :disabled="disabled" />
        </ElFormItem>
      </div>
    </template>
  </fieldset>
</template>

<style scoped>
.storage-fields { display: grid; gap: 10px; min-width: 0; }
.storage-fields p { margin: 0; }
.storage-fields :deep(.el-form-item) { margin-bottom: 0; min-width: 0; }
.storage-fields :deep(.el-input-number) { max-width: 100%; }
.storage-disk { align-items: end; }
.storage-disk > .el-button { justify-self: start; }
@media (max-width: 767px) {
  .storage-fields .form-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
