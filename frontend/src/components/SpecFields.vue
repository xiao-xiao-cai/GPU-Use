<script setup lang="ts">
import { computed, nextTick, ref, toRef, watch } from 'vue'
import type { ChargeType, Disk, Spec, Zone } from '../api/client'
import { useImages } from '../api/images'
import { billingMatches, comboKey, commonValues, diskBounds, imageKinds, imageLabels, specLabel,
  uniqueLocations, useLocationNames, zoneKey, type CatalogRow } from '../catalog'
import { chargeLabels, timeLabel } from '../display'
import ImagePicker from './ImagePicker.vue'
import LocationFields from './LocationFields.vue'

const spec = defineModel<Spec>({ required: true })
const props = defineProps<{
  projectId: string
  catalog: CatalogRow[]
  catalogError?: string | null
  zones?: Zone[]
  allowSpot?: boolean
  requireQuantity?: boolean
  requireSystemDisk?: boolean
  managedStorage?: boolean
  disabled?: boolean
  legacy?: boolean
  poolOnly?: boolean
}>()

const { zoneName } = useLocationNames()
const locations = computed(() => uniqueLocations(props.catalog))
const scopes = computed(() => props.zones ?? [{ region: spec.value.region, zone: spec.value.zone }])
const scopeGroups = computed(() => scopes.value.map(location =>
  props.catalog.filter(row => row.region === location.region && row.zone === location.zone)))
const allScopes = computed(() => scopes.value.length > 0 && scopes.value.every(({ region, zone }) => region && zone))
const termBilling = computed(() => ['Day', 'Month'].includes(spec.value.charge_type))
const charges = computed(() => (Object.keys(chargeLabels) as ChargeType[]).filter(charge =>
  (props.allowSpot || charge !== 'Spot') && (!props.poolOnly || charge === 'Postpay' || charge === 'Spot') && allScopes.value
  && scopeGroups.value.every(group => group.some(row => billingMatches(row, charge)))))
const billingRows = computed(() => scopeGroups.value.map(rows => rows.filter(row => billingMatches(row, spec.value.charge_type))))
const gpuModels = computed(() => commonValues(billingRows.value.map(rows => rows.map(row => row.gpu_model)), value => value).sort())
const modelRows = computed(() => billingRows.value.map(rows => rows.filter(row => row.gpu_model === spec.value.gpu_model)))
const combinations = computed(() => commonValues(modelRows.value.map(rows => rows.flatMap(row => row.specs ?? [])), comboKey))
const combination = computed(() => comboKey(spec.value))
const selectedZones = computed(() => uniqueLocations(scopes.value.filter(item => item.region && item.zone)))
const images = useImages(toRef(props, 'projectId'), computed(() => props.poolOnly ? [] : selectedZones.value))
const imageOptions = computed(() => allScopes.value ? images.images.value : [])
const imageStatuses = computed(() => selectedZones.value.flatMap(location => {
  const batch = images.snapshots.value[zoneKey(location)]
  if (!batch) return []
  return imageKinds.map(kind => ({
    key: zoneKey(location) + kind,
    label: (selectedZones.value.length > 1 ? zoneName(location) + ' · ' : '') + imageLabels[kind],
    at: batch[kind].success_at,
    count: batch[kind].data.length,
    error: batch[kind].error,
  }))
}))
const note = ref('')
const checkImage = ref(false)
watch([images.complete, checkImage], ([complete, check]) => {
  if (!complete || !check) return
  checkImage.value = false
  if (spec.value.image_id && !imageOptions.value.some(item => item.id === spec.value.image_id)) {
    spec.value.image_id = ''
    note.value = '原镜像在当前可用区不可用，请重新选择。'
  }
})

function setCombination(key: string) {
  const option = combinations.value.find(item => comboKey(item) === key)
  const fields = option ? { gpu: option.gpu, cpu: option.cpu, memory_gb: option.memory_gb } : { gpu: 0, cpu: 0, memory_gb: 0 }
  Object.assign(spec.value, fields)
}

function diskTypes(purpose: string) {
  return commonValues(modelRows.value.map(rows => rows.flatMap(row => (row.disks ?? [])
    .filter(item => item.purpose === purpose).map(item => item.type))), value => value)
    .filter(type => diskBounds(modelRows.value, { purpose, type, size_gb: 0 })).sort()
}

function addDisk(purpose: 'system' | 'data') {
  const type = diskTypes(purpose)[0]
  if (!type) return
  const disk = { purpose, type, size_gb: 0 }
  changeDisk(disk, type)
  spec.value.disks.push(disk)
}

function changeDisk(disk: Disk, type: string) {
  disk.type = type
  const bounds = diskBounds(modelRows.value, disk)
  if (bounds && (disk.size_gb < bounds.min || disk.size_gb > bounds.max)) disk.size_gb = bounds.min
}

function reconcile() {
  const before = JSON.stringify(spec.value)
  if (!allScopes.value) return
  if (!charges.value.includes(spec.value.charge_type) && charges.value[0]) spec.value.charge_type = charges.value[0]
  if (!gpuModels.value.includes(spec.value.gpu_model)) spec.value.gpu_model = ''
  setCombination(combination.value)
  for (const disk of props.managedStorage ? [] : spec.value.disks) {
    const bounds = diskBounds(modelRows.value, disk)
    if (!bounds) disk.type = ''
    if (!bounds || disk.size_gb < bounds.min || disk.size_gb > bounds.max) disk.size_gb = 0
  }
  if (JSON.stringify(spec.value) !== before) note.value = '和当前目录冲突的规格已清除，请检查并重新选择。'
}

async function changeScope(loc?: Zone) {
  if (loc) Object.assign(spec.value, loc)
  await nextTick()
  reconcile()
  checkImage.value = true
}

function changeCharge(charge: ChargeType) {
  note.value = ''
  spec.value.charge_type = charge
  spec.value.quantity = termBilling.value ? (spec.value.quantity ?? 1) : undefined
  reconcile()
}

function changeModel(model: string) {
  note.value = ''
  spec.value.gpu_model = model
  reconcile()
  if (!props.managedStorage && props.requireSystemDisk && !spec.value.disks.some(disk => disk.purpose === 'system')) addDisk('system')
}

function validate() {
  if (!allScopes.value) return '请选择地域和可用区。'
  if (new Set(scopes.value.map(zoneKey)).size !== scopes.value.length) return '可用区不能重复。'
  if (!gpuModels.value.includes(spec.value.gpu_model) || !combinations.value.some(item => comboKey(item) === combination.value)) {
    return '请选择所有可用区均支持的 GPU 和计算规格。'
  }
  if (props.poolOnly) return ''
  if (props.legacy) return ''
  if (!imageOptions.value.some(item => item.id === spec.value.image_id)) return '请选择所有可用区可见的镜像。'
  if (props.requireQuantity && termBilling.value && (!Number.isInteger(spec.value.quantity) || !spec.value.quantity)) {
    return '请填写购买时长。'
  }
  if (props.managedStorage) return ''
  if (props.requireSystemDisk && spec.value.disks.filter(item => item.purpose === 'system').length !== 1) {
    return '请选择一个系统盘。'
  }
  for (const disk of spec.value.disks) {
    const bounds = diskBounds(modelRows.value, disk)
    if (!bounds || !Number.isInteger(disk.size_gb) || disk.size_gb < bounds.min || disk.size_gb > bounds.max) {
      return '请核对磁盘类型和容量范围。'
    }
  }
  return ''
}

defineExpose({ changeScope, validate })
</script>

<template>
  <fieldset class="spec-fields" :disabled="disabled">
    <legend>实验规格</legend>
    <ElAlert v-if="catalogError" title="目录同步异常" :description="catalogError" type="warning" :closable="false" />
    <div class="form-grid">
      <LocationFields v-if="!zones" :model-value="spec" :options="locations" :disabled="disabled" @update:model-value="changeScope" />
      <ElFormItem :label="poolOnly ? '资源池' : '计费方式'" required>
        <ElSelect :model-value="spec.charge_type" :disabled="disabled || !allScopes" @change="changeCharge">
          <ElOption v-if="!charges.includes(spec.charge_type)" :value="spec.charge_type" :label="chargeLabels[spec.charge_type]" disabled />
          <ElOption v-for="charge in charges" :key="charge" :value="charge" :label="poolOnly ? (charge === 'Spot' ? '抢占池' : '独占池') : chargeLabels[charge]" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem v-if="requireQuantity && termBilling" :label="`购买时长（${spec.charge_type === 'Day' ? '天' : '月'}）`" required>
        <ElInputNumber v-model="spec.quantity" :min="1" :max="3650" :step="1" :disabled="disabled" />
      </ElFormItem>
      <ElFormItem label="GPU 型号" required>
        <ElSelect :model-value="spec.gpu_model || undefined" :disabled="disabled || !gpuModels.length" filterable
          placeholder="选择 GPU 型号" @change="changeModel">
          <ElOption v-if="spec.gpu_model && !gpuModels.includes(spec.gpu_model)" :value="spec.gpu_model"
            :label="spec.gpu_model + '（原配置）'" disabled />
          <ElOption v-for="model in gpuModels" :key="model" :value="model" :label="model" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem label="GPU 张数 · CPU 核数 · 内存" required>
        <ElSelect :model-value="spec.cpu ? combination : undefined" :disabled="disabled || !combinations.length"
          placeholder="选择计算规格" filterable @change="setCombination">
          <ElOption v-if="spec.cpu && !combinations.some(item => comboKey(item) === combination)" :value="combination"
            :label="specLabel(spec) + '（原配置）'" disabled />
          <ElOption v-for="option in combinations" :key="comboKey(option)" :value="comboKey(option)" :label="specLabel(option)" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem v-if="!poolOnly" label="镜像" required>
        <ImagePicker v-model="spec.image_id" :images="imageOptions" :disabled="disabled" :loading="images.pending.value" />
      </ElFormItem>
    </div>
    <div v-if="!poolOnly && allScopes && projectId" class="image-status">
      <p v-if="images.pending.value" role="status">正在同步镜像目录，可先选择其他配置。</p>
      <p v-else-if="images.complete.value && !imageOptions.length" role="status">所选可用区没有共同镜像，请调整可用区。</p>
      <p v-if="images.error.value" role="alert">{{ images.error.value }}</p>
      <ElButton size="small" :disabled="disabled" :loading="images.pending.value" @click="images.refresh">同步镜像</ElButton>
      <details v-if="imageStatuses.length">
        <summary>镜像同步状态<span v-if="imageStatuses.some(item => item.error)"> · 部分同步失败</span></summary>
        <ul>
          <li v-for="item in imageStatuses" :key="item.key">{{ item.label }} · {{ item.at ? item.count + ' 个 · ' + timeLabel(item.at) : '未同步' }}
            <span v-if="item.error"> · {{ item.error }}</span>
          </li>
        </ul>
      </details>
    </div>
    <p v-if="allScopes && charges.length && !gpuModels.length" role="status">所选可用区没有共同 GPU 型号，请调整可用区或计费方式。</p>
    <p v-else-if="spec.gpu_model && allScopes && !combinations.length" role="status">没有共同计算规格，请调整可用区或 GPU 型号。</p>
    <p v-if="note" class="muted" role="status">{{ note }}</p>
    <template v-if="!poolOnly && !managedStorage">
    <div v-for="(disk, index) in spec.disks" :key="index" class="form-grid spec-disk">
      <ElFormItem :label="disk.purpose === 'system' ? '系统盘类型' : '数据盘类型'" required>
        <ElSelect :model-value="disk.type || undefined" :disabled="disabled || !diskTypes(disk.purpose).length"
          placeholder="选择磁盘类型" @change="(type: string) => changeDisk(disk, type)">
          <ElOption v-if="disk.type && !diskTypes(disk.purpose).includes(disk.type)" :value="disk.type" :label="disk.type + '（原配置）'" disabled />
          <ElOption v-for="type in diskTypes(disk.purpose)" :key="type" :value="type" :label="type" />
        </ElSelect>
      </ElFormItem>
      <ElFormItem label="容量（GB）" required>
        <ElInputNumber v-model="disk.size_gb" :min="diskBounds(modelRows, disk)?.min"
          :max="diskBounds(modelRows, disk)?.max" :step="1" :disabled="disabled || !disk.type" />
      </ElFormItem>
      <ElButton :disabled="disabled" @click="spec.disks.splice(index, 1)">移除磁盘</ElButton>
    </div>
    <div>
      <ElButton v-if="!spec.disks.some(item => item.purpose === 'system')" :disabled="disabled || !diskTypes('system').length"
        @click="addDisk('system')">添加系统盘</ElButton>
      <ElButton :disabled="disabled || spec.disks.length >= 10 || !diskTypes('data').length" @click="addDisk('data')">添加数据盘</ElButton>
    </div>
    </template>
  </fieldset>
</template>

<style scoped>
.spec-fields {
  display: grid;
  gap: 12px;
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
}
.spec-fields > legend { margin-bottom: 8px; padding: 0; font-weight: 650; }
.spec-fields :deep(.el-form-item) { min-width: 0; margin-bottom: 0; }
.spec-fields :deep(.el-input-number) { max-width: 100%; }
.spec-disk { align-items: end; }
.spec-disk > .el-button { justify-self: start; }
.image-status { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 12px; }
.image-status p, .image-status ul { margin: 0; }
.image-status details { flex-basis: 100%; }
@media (max-width: 767px) {
  .spec-fields .form-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
