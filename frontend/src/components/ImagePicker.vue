<script setup lang="ts">
import { computed } from 'vue'
import type { CascaderNode, CascaderOption, CascaderProps } from 'element-plus'
import { imageLabels, imageOptionLabel, type PlatformImage } from '../catalog'

const model = defineModel<string>({ required: true })
const props = defineProps<{ images: PlatformImage[]; disabled?: boolean; loading?: boolean }>()
const cascadeProps: CascaderProps = { emitPath: false }
const imagesById = computed(() => new Map(props.images.map(image => [image.id, image])))
const selected = computed(() => imagesById.value.get(model.value))

interface ImageChoice extends CascaderOption {
  value: string
  label: string
  children?: ImageChoice[]
  disabled?: boolean
}

function imagePath(image: PlatformImage) {
  if (image.runtime !== 'container' || image.kind === 'custom' || image.kind === 'shared') {
    return [imageLabels[image.kind], image.name]
  }
  const family = image.framework || '其他容器镜像'
  if (!image.framework || !image.os_version || !image.cuda_version || !image.framework_version || !image.python_version) {
    return [family, imageOptionLabel(image)]
  }
  return [family, image.os_version, 'CUDA ' + image.cuda_version, image.framework_version, 'Python ' + image.python_version]
}

const options = computed(() => {
  const roots: ImageChoice[] = []
  const paths = props.images.map(image => ({ image, path: imagePath(image) }))
  const pathCounts = new Map<string, number>()
  for (const { path } of paths) {
    const key = JSON.stringify(path)
    pathCounts.set(key, (pathCounts.get(key) ?? 0) + 1)
  }
  for (const { image, path } of paths) {
    let choices = roots
    for (const [index, label] of path.slice(0, -1).entries()) {
      const value = 'path:' + JSON.stringify(path.slice(0, index + 1))
      const existing = choices.find(choice => choice.value === value)
      if (existing?.children) {
        choices = existing.children
      } else {
        const children: ImageChoice[] = []
        choices.push({ value, label, children })
        choices = children
      }
    }
    const duplicate = (pathCounts.get(JSON.stringify(path)) ?? 0) > 1
    const suffix = duplicate ? ' · ' + image.id : ''
    choices.push({ value: image.id, label: path.at(-1)! + (image.paid ? '（收费）' : '') + suffix })
  }
  const sourceLabels = Object.values(imageLabels) as string[]
  function groupOrder(label: string) {
    if (label === imageLabels.custom || label === imageLabels.shared) return 0
    return label.toLowerCase() === 'pytorch' ? 1 : sourceLabels.includes(label) ? 3 : 2
  }
  roots.sort((first, second) => groupOrder(first.label) - groupOrder(second.label))
  if (model.value && !selected.value) {
    roots.push({ value: model.value, label: model.value + '（原配置）', disabled: true })
  }
  return roots
})

function filterImage(node: CascaderNode, keyword: string) {
  const image = typeof node.value === 'string' ? imagesById.value.get(node.value) : undefined
  if (!image) return false
  const label = [node.pathLabels.join(' '), imageOptionLabel(image), image.name, image.id].join(' ').toLocaleLowerCase()
  return keyword.toLocaleLowerCase().trim().split(/\s+/).every(term => label.includes(term))
}
</script>

<template>
  <ElCascader v-model="model" :options="options" :props="cascadeProps" :filter-method="filterImage"
    :disabled="disabled || !images.length" :placeholder="loading ? '正在同步镜像' : '选择镜像或搜索版本'"
    class="image-cascader" popper-class="image-cascader-popup" separator=" / " filterable />
  <p v-if="selected?.runtime === 'container'" class="muted image-kind">容器实例，实例内无法再次安装 Docker。</p>
</template>

<style>
.image-cascader { width: 100%; }
.image-cascader-popup { max-width: calc(100vw - 24px); }
.image-cascader-popup .el-cascader-panel { overflow-x: auto; }
.image-cascader-popup .el-cascader-menu { flex: 0 0 auto; }
.image-kind { margin: 4px 0 0; line-height: 1.4; }
</style>
