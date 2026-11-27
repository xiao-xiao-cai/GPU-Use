<script setup lang="ts">
import { computed } from 'vue'
import type { Zone } from '../api/client'
import { useLocationNames } from '../catalog'

const props = defineProps<{ modelValue: Zone; options: Zone[]; disabled?: boolean; ordinal?: number }>()
const emit = defineEmits<{ 'update:modelValue': [value: Zone] }>()
const { regionName, zoneName } = useLocationNames()
const regions = computed(() => [...new Set(props.options.map(item => item.region))].sort())
const zones = computed(() => [...new Set(props.options.filter(item => item.region === props.modelValue.region)
  .map(item => item.zone))].sort())

function selectRegion(region: string) {
  const previousZone = props.modelValue.zone
  const keepZone = props.options.some(item => item.region === region && item.zone === previousZone)
  emit('update:modelValue', { region, zone: keepZone ? previousZone : '' })
}
</script>

<template>
  <ElFormItem :label="ordinal ? '地域 ' + ordinal : '地域'" required>
    <ElSelect :model-value="modelValue.region" :disabled="disabled" filterable placeholder="选择地域" @change="selectRegion">
      <ElOption v-if="modelValue.region && !regions.includes(modelValue.region)" :value="modelValue.region"
        :label="regionName(modelValue.region) + '（原配置）'" disabled />
      <ElOption v-for="region in regions" :key="region" :value="region" :label="regionName(region)" />
    </ElSelect>
  </ElFormItem>
  <ElFormItem :label="ordinal ? '可用区 ' + ordinal : '可用区'" required>
    <ElSelect :model-value="modelValue.zone" :disabled="disabled || !modelValue.region" filterable placeholder="选择可用区"
      @change="(zone: string) => emit('update:modelValue', { region: modelValue.region, zone })">
      <ElOption v-if="modelValue.zone && !zones.includes(modelValue.zone)" :value="modelValue.zone"
        :label="zoneName(modelValue) + '（原配置）'" disabled />
      <ElOption v-for="zone in zones" :key="zone" :value="zone" :label="zoneName({ region: modelValue.region, zone })" />
    </ElSelect>
  </ElFormItem>
</template>
