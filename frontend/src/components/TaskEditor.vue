<script setup lang="ts">
import { computed, nextTick, onMounted, onScopeDispose, ref, watch } from 'vue'
import SpecFields from './SpecFields.vue'
import LocationFields from './LocationFields.vue'
import InstanceStorageFields from './InstanceStorageFields.vue'
import { uniqueLocations, useLocationNames, zoneKey, type CatalogRow } from '../catalog'
import {
  request, type BootMode, type CardlessSpec, type Channel, type Instance, type InstanceStorageSelection, type NotificationEvent, type Preview, type Schedule,
  type Spec, type Task, type TaskConfig, type TaskDraft, type TaskKind, type Template, type Zone,
} from '../api/client'
import { cardlessProfileLabel, cardlessUnavailable, chargeLabels, errorMessage, instanceLabels, localInput, timeLabel, unixTime, useSnapshot } from '../display'

const props = defineProps<{
  projectId: string
  instances: Instance[]
  templates: Template[]
  channels?: Record<Channel, boolean>
  task?: Task
}>()
const emit = defineEmits<{ close: []; saved: []; templateSaved: [] }>()
const kindLabels: Record<TaskKind, string> = {
  stock: '仅通知有货',
  start: '只启动已有实例',
  acquire: '按模板获取实例',
  stop: '关机',
  shutdown: '云端定时关机',
  inspection: '巡检',
}
const scheduleLabels: Record<Schedule['type'], string> = {
  once: '一次性', daily: '每天', weekly: '每周', interval: '固定间隔',
}
const eventOptions: { value: NotificationEvent; label: string }[] = [
  { value: 'acquisition_succeeded', label: '发现库存或取得实例' },
  { value: 'acquisition_expired', label: '窗口结束仍未取得资源' },
  { value: 'shutdown_result', label: '关机与预约结果' },
  { value: 'task_result', label: '巡检、失败及待核对' },
  { value: 'idle', label: '本次启动或新建实例持续空闲' },
]
const scheduleAt = ref('')
const shutdownAt = ref('')
const shutdownChoice = ref<'none' | 'absolute' | 'duration'>('none')
const shutdownMinutes = ref(120)
const bootMode = ref<BootMode>('with_gpu')
const cardlessSpec = ref<CardlessSpec>()
const intervalMinutes = ref(60)
const windowMinutes = ref(120)
const capacityRetry = ref(true)
const capacityRetrySeconds = ref(60)
const kind = ref<TaskKind>('stock')
const name = ref('')
const scheduleType = ref<Schedule['type']>('once')
const spec = ref<Spec>(blankSpec())
const zones = ref<Zone[]>([{ region: '', zone: '' }])
const specFields = ref<InstanceType<typeof SpecFields>>()
const storageFields = ref<InstanceType<typeof InstanceStorageFields>[]>([])
const zoneStorageEnabled = ref(true)
const zoneStorage = ref<Record<string, InstanceStorageSelection>>({})
const legacySpec = ref('')
const maxPrice = ref<string | number>('')
const purchaseLimit = ref(1)
const skipIfMatch = ref(true)
const channels = ref<Channel[]>(['site'])
const notificationEvents = ref<NotificationEvent[]>([])
const email = ref('')
const templateId = ref('')
const templateVersion = ref<number>()
const followsTemplate = ref(false)
const templateNote = ref('')
const instanceId = ref('')
const preview = ref<Preview>()
const previewedBody = ref('')
const editorError = ref('')
const editorPending = ref(false)
const saving = ref(false)
const changes = computed(() => ['stock', 'acquire'].includes(kind.value))
const managedStorage = computed(() => kind.value === 'acquire' && zoneStorageEnabled.value)
const { state: catalog, fault: catalogError } = useSnapshot<CatalogRow>(computed(() => changes.value ? '/catalog' : null), 30_000)
const locations = computed(() => uniqueLocations(catalog.value.data))
const { zoneName } = useLocationNames()
const legacyKey = computed(() => JSON.stringify({ spec: spec.value, zones: zones.value }))
const instance = computed(() => props.instances.find(item => item.id === instanceId.value))
const cardlessProfiles = computed(() => instance.value?.cardless_profiles ?? [])
const hasCapacityRetry = computed(() => kind.value === 'start' && bootMode.value === 'with_gpu' && capacityRetry.value)
const hasSpot = computed(() => changes.value ? spec.value.charge_type === 'Spot' : instance.value?.charge_type === 'Spot')
const hasInstance = computed(() => ['start', 'stop', 'shutdown'].includes(kind.value))
const linkedShutdown = computed(() => ['start', 'acquire'].includes(kind.value) && !hasSpot.value)
const availableEventOptions = computed(() => eventOptions.filter(event => event.value !== 'idle'
  || (kind.value === 'start' && bootMode.value === 'with_gpu') || kind.value === 'acquire'))
const liveTemplate = computed(() => changes.value && followsTemplate.value)
const budgetLabel = computed(() => ['Day', 'Month'].includes(spec.value.charge_type) ? '整单金额上限（元）' : '每小时报价上限（元）')
const step = ref(0)
const stepHeading = ref<HTMLElement>()
const fieldError = ref<'instance' | 'cardless' | 'schedule' | 'shutdown' | 'capacity-retry' | ''>('')
let previewRequest: AbortController | undefined
let disposed = false

function blankSpec(): Spec {
  return { region: '', zone: '', gpu_model: '', gpu: 1, cpu: 0, memory_gb: 0, image_id: '', disks: [], charge_type: 'Postpay' }
}

function useSpec(config: TaskConfig) {
  const { gpu_model, gpu, cpu, memory_gb, image_id, disks, charge_type, quantity } = config
  spec.value = {
    region: '', zone: '', gpu_model: gpu_model ?? '', gpu: gpu ?? 1, cpu: cpu ?? 0, memory_gb: memory_gb ?? 0,
    image_id: image_id ?? '', disks: disks?.map(disk => ({ ...disk })) ?? [],
    charge_type: kind.value === 'stock' && charge_type !== 'Spot' ? 'Postpay' : charge_type ?? 'Postpay', quantity,
  }
  zoneStorageEnabled.value = Array.isArray(config.zone_storage) || !config.gpu_model
  zoneStorage.value = Object.fromEntries((config.zone_storage ?? []).map(item => [zoneKey(item), {
    disks: item.disks.filter(disk => disk.purpose === 'data').map(disk => ({ ...disk })),
    shared_storage: item.shared_storage && { ...item.shared_storage },
  }]))
  zones.value = config.zones?.length ? config.zones.map(({ region, zone }) => ({ region, zone })) : [{ region: '', zone: '' }]
  legacySpec.value = config.gpu_model && config.zones?.length ? legacyKey.value : ''
}

watch(() => zones.value.map(zoneKey), keys => {
  const current = zoneStorage.value
  zoneStorage.value = Object.fromEntries(keys.map(key => [key, current[key] ?? { disks: [] }]))
}, { flush: 'sync', immediate: true })

function setZone(index: number, location: Zone) {
  zones.value[index] = location
  void specFields.value?.changeScope()
}

function removeZone(index: number) {
  zones.value.splice(index, 1)
  void specFields.value?.changeScope()
}

function useConfig(config: TaskConfig) {
  useSpec(config)
  instanceId.value = config.instance_id ?? ''
  maxPrice.value = config.max_price ?? ''
  skipIfMatch.value = config.skip_if_match ?? true
  purchaseLimit.value = config.purchase_limit ?? 1
  email.value = config.email ?? ''
  shutdownAt.value = config.shutdown_at ? localInput(config.shutdown_at) : ''
  shutdownChoice.value = config.shutdown_at ? 'absolute' : config.shutdown_after_seconds ? 'duration' : 'none'
  shutdownMinutes.value = (config.shutdown_after_seconds ?? 7200) / 60
  bootMode.value = config.boot_mode ?? 'with_gpu'
  cardlessSpec.value = config.without_gpu_spec
}

function initialize(task?: Task) {
  kind.value = task?.kind ?? 'stock'
  name.value = task?.name ?? ''
  const now = Math.floor(Date.now() / 1000)
  scheduleAt.value = localInput(task?.next_at ?? task?.schedule.at ?? now + 900)
  scheduleType.value = task?.schedule.type ?? 'once'
  intervalMinutes.value = (task?.schedule.interval_seconds ?? 3600) / 60
  windowMinutes.value = (task?.window_seconds ?? 7200) / 60
  capacityRetry.value = !task || Boolean(task.config.capacity_retry)
  capacityRetrySeconds.value = task?.config.capacity_retry?.interval_seconds ?? 60
  channels.value = task?.channels.slice() ?? ['site']
  notificationEvents.value = task?.notification_events?.slice() ?? eventOptions.map(event => event.value)
  templateId.value = task?.template_id ?? ''
  templateVersion.value = undefined
  followsTemplate.value = false
  templateName.value = ''
  templateNote.value = ''
  previewedBody.value = ''
  editorError.value = ''
  useConfig(task?.config ?? {})
  const template = props.templates.find(item => item.id === templateId.value)
  if (template && task) {
    const billingChanged = template.config.charge_type !== task.config.charge_type
      || template.config.quantity !== task.config.quantity
    useSpec(template.config)
    followsTemplate.value = true
    templateVersion.value = template.version
    templateName.value = template.name
    if (billingChanged && task.kind === 'acquire') {
      maxPrice.value = ''
      templateNote.value = '模板的计费方式或时长已更新，请重新填写报价上限。'
    }
  } else if (task?.template_id) {
    templateNote.value = '原模板已不存在。请选择现有模板，或核对当前规格后预览。'
  }
}

function selectTemplate() {
  const template = props.templates.find(item => item.id === templateId.value)
  templateNote.value = ''
  followsTemplate.value = Boolean(template)
  templateVersion.value = template?.version
  templateName.value = template?.name ?? ''
  if (!template) return
  if (maxPrice.value === '') maxPrice.value = template.config.max_price ?? ''
  else if (spec.value.charge_type !== template.config.charge_type || spec.value.quantity !== template.config.quantity) {
    maxPrice.value = ''
    templateNote.value = '计费方式或时长已修改，请核对报价上限。'
  }
  if (purchaseLimit.value === 1) purchaseLimit.value = template.config.purchase_limit ?? 1
  skipIfMatch.value = template.config.skip_if_match ?? true
  useSpec(template.config)
  if (!name.value) name.value = template.name
}

function detachTemplate() {
  followsTemplate.value = false
  templateNote.value = '规格已复制到当前任务，可独立修改。'
}

function useManagedStorage() {
  zoneStorage.value = Object.fromEntries(zones.value.map(zone => [zoneKey(zone), {
    disks: spec.value.disks.filter(disk => disk.purpose === 'data').map(disk => ({ ...disk })),
  }]))
  zoneStorageEnabled.value = true
  templateNote.value = '请核对各可用区的系统盘与数据盘配置。'
}

function draft(): TaskDraft {
  const config: TaskConfig = {}
  if (changes.value) {
    const { region: _region, zone: _zone, ...fields } = spec.value
    if (kind.value === 'stock') {
      const { gpu_model, gpu, cpu, memory_gb, charge_type } = fields
      Object.assign(config, { gpu_model, gpu, cpu, memory_gb, charge_type, zones: zones.value })
    } else {
      Object.assign(config, fields, { zones: zones.value })
    }
    if (kind.value === 'acquire') {
      Object.assign(config, { max_price: maxPrice.value, purchase_limit: purchaseLimit.value, skip_if_match: skipIfMatch.value })
      if (managedStorage.value) {
        config.disks = []
        config.zone_storage = zones.value.map(zone => ({ ...zone, ...(zoneStorage.value[zoneKey(zone)] ?? { disks: [] }) }))
      }
    }
  }
  if (hasInstance.value) {
    if (!instance.value) throw new Error('请选择项目中的实例。')
    if (kind.value === 'shutdown' && hasSpot.value) throw new Error('抢占式实例不支持定时关机。')
    Object.assign(config, { instance_id: instance.value.id, region: instance.value.region, zone: instance.value.zone })
  }
  if (kind.value === 'start' && bootMode.value === 'without_gpu') {
    if (!cardlessProfiles.value.some(profile => profile.spec === cardlessSpec.value)) throw new Error('请选择可用的无卡开机配置。')
    config.boot_mode = bootMode.value
    config.without_gpu_spec = cardlessSpec.value
  }
  if (linkedShutdown.value && kind.value === 'start' && shutdownChoice.value === 'duration') {
    config.shutdown_after_seconds = Math.round(shutdownMinutes.value * 60)
  }
  if (linkedShutdown.value && shutdownAt.value && (kind.value !== 'start' || shutdownChoice.value === 'absolute')) {
    const original = props.task?.config.shutdown_at
    config.shutdown_at = original && shutdownAt.value === localInput(original) ? original : unixTime(shutdownAt.value)
  }
  if (hasCapacityRetry.value) {
    config.capacity_retry = { interval_seconds: capacityRetrySeconds.value }
  }
  if (channels.value.includes('email')) config.email = email.value
  const schedule: Schedule = {
    type: scheduleType.value,
    at: unixTime(scheduleAt.value),
    interval_seconds: scheduleType.value === 'interval' ? Math.round(intervalMinutes.value * 60) : undefined,
  }
  const task = props.task
  if (task && schedule.type === task.schedule.type && schedule.interval_seconds === task.schedule.interval_seconds
    && scheduleAt.value === localInput(task.next_at ?? task.schedule.at)) schedule.at = task.schedule.at
  return {
    name: name.value,
    project_id: props.projectId,
    kind: kind.value,
    schedule,
    window_seconds: Math.round(windowMinutes.value * 60),
    config,
    channels: channels.value,
    notification_events: availableEventOptions.value.map(event => event.value)
      .filter(event => notificationEvents.value.includes(event)),
    template_id: liveTemplate.value ? templateId.value : undefined,
    template_version: liveTemplate.value ? templateVersion.value : undefined,
  }
}

const draftPayload = computed(() => {
  try {
    return JSON.stringify(draft())
  } catch {
    return ''
  }
})
watch(draftPayload, invalidatePreview, { flush: 'sync' })

function invalidatePreview() {
  previewRequest?.abort()
  previewRequest = undefined
  editorPending.value = false
  preview.value = undefined
  previewedBody.value = ''
  editorError.value = ''
}

async function focusStep() {
  await nextTick()
  stepHeading.value?.focus()
  stepHeading.value?.scrollIntoView({ block: 'nearest' })
}

function previousStep() {
  invalidatePreview()
  fieldError.value = ''
  step.value--
  void focusStep()
}

function invalidField(field: typeof fieldError.value, message: string) {
  fieldError.value = field
  editorError.value = message
  void nextTick(() => document.getElementById('task-' + field)?.focus())
}

function advance() {
  fieldError.value = ''
  editorError.value = ''
  if (step.value === 0) {
    if (hasInstance.value && (!instance.value || (kind.value === 'shutdown' && hasSpot.value))) {
      invalidField('instance', '请选择支持此项操作的项目实例。')
      return
    }
    if (kind.value === 'start' && bootMode.value === 'without_gpu'
      && !cardlessProfiles.value.some(profile => profile.spec === cardlessSpec.value)) {
      invalidField('cardless', '请选择可用的无卡开机配置。')
      return
    }
    const specError = changes.value ? specFields.value?.validate() : ''
    if (specError) {
      editorError.value = specError
      return
    }
    if (managedStorage.value) {
      const storageError = storageFields.value.length !== zones.value.length ? '请选择完整的可用区。'
        : storageFields.value.map(field => field.validate()).find(Boolean)
      if (storageError) {
        editorError.value = storageError
        return
      }
    }
    step.value = 1
  } else if (step.value === 1) {
    if (!Number.isFinite(unixTime(scheduleAt.value))) {
      invalidField('schedule', '请选择北京时间。')
      return
    }
    if (linkedShutdown.value && (kind.value === 'start' ? shutdownChoice.value === 'absolute' : Boolean(shutdownAt.value))
      && !Number.isFinite(unixTime(shutdownAt.value))) {
      invalidField('shutdown', '请选择有效的关机时间。')
      return
    }
    if (linkedShutdown.value && kind.value === 'start' && shutdownChoice.value === 'duration'
      && (!Number.isFinite(shutdownMinutes.value) || shutdownMinutes.value < 15 || shutdownMinutes.value > 10080)) {
      invalidField('shutdown', '运行时长须为 15 至 10080 分钟。')
      return
    }
    if (hasCapacityRetry.value && (!Number.isInteger(capacityRetrySeconds.value)
      || capacityRetrySeconds.value < 30 || capacityRetrySeconds.value > 3600)) {
      invalidField('capacity-retry', '请输入 30 至 3600 秒的整数。')
      return
    }
    step.value = 2
    void previewTask()
  }
  void focusStep()
}

const currentTemplate = computed(() => props.templates.find(item => item.id === templateId.value))
const templateChanged = computed(() => liveTemplate.value && currentTemplate.value?.version !== templateVersion.value)

watch(templateChanged, changed => { if (changed) invalidatePreview() })

async function previewTask() {
  const payload = draftPayload.value
  if (!payload || editorPending.value || saving.value || templateChanged.value) return
  const controller = new AbortController()
  previewRequest = controller
  editorPending.value = true
  editorError.value = ''
  try {
    const draft = JSON.parse(payload) as TaskDraft
    const body = props.task ? { ...draft, task_id: props.task.id, task_version: props.task.version } : draft
    const result = await request<Preview>('/tasks/preview', {
      method: 'POST', body, signal: controller.signal,
    })
    if (previewRequest === controller && draftPayload.value === payload) {
      preview.value = result
      previewedBody.value = payload
    }
  } catch (error) {
    if (previewRequest === controller) editorError.value = errorMessage(error)
  } finally {
    if (previewRequest === controller) {
      previewRequest = undefined
      editorPending.value = false
    }
  }
}

const canSave = computed(() => Boolean(preview.value) && !templateChanged.value
  && draftPayload.value === previewedBody.value && !editorPending.value && !saving.value)

async function saveTask() {
  if (!canSave.value) return
  saving.value = true
  editorError.value = ''
  try {
    const id = props.task?.id
    const path = id ? '/tasks/' + encodeURIComponent(id) : '/tasks'
    const body = { ...(JSON.parse(draftPayload.value) as TaskDraft), preview_token: id ? preview.value?.preview_token : undefined }
    await request<Task>(path, { method: id ? 'PUT' : 'POST', body })
    if (!disposed) emit('saved')
  } catch (error) {
    if (!disposed) {
      if (preview.value?.preview_token) invalidatePreview()
      editorError.value = errorMessage(error)
    }
  } finally {
    saving.value = false
  }
}

const templateName = ref('')
const templateMessage = ref('')
async function saveTemplate() {
  if (!canSave.value || !templateName.value.trim()) return
  saving.value = true
  editorError.value = ''
  try {
    const body = draft()
    const { email: _email, shutdown_at: _shutdownAt, ...config } = body.config
    const existing = currentTemplate.value?.name === templateName.value ? currentTemplate.value : undefined
    const result = await request<Template>(existing ? '/templates/' + encodeURIComponent(existing.id) : '/templates', {
      method: existing ? 'PUT' : 'POST', body: { project_id: props.projectId, name: templateName.value, config },
    })
    if (disposed) return
    templateId.value = result.id
    templateVersion.value = result.version
    templateMessage.value = existing ? '模板已更新。' : '模板已保存。'
    emit('templateSaved')
  } catch (error) {
    if (!disposed) editorError.value = errorMessage(error)
  } finally {
    saving.value = false
  }
}

initialize(props.task)
onMounted(() => { void focusStep() })
onScopeDispose(() => {
  disposed = true
  previewRequest?.abort()
  previewRequest = undefined
})
</script>

<template>
  <section class="panel task-editor" aria-labelledby="task-editor-title">
    <header class="section-heading">
      <h2 id="task-editor-title">{{ task ? '编辑任务' : '创建任务' }}</h2>
      <ElButton :disabled="saving" @click="emit('close')">关闭编辑</ElButton>
    </header>
    <ElSteps :active="step" finish-status="success" simple class="task-steps" aria-label="编辑进度">
      <ElStep title="对象与规格" />
      <ElStep title="安排与通知" />
      <ElStep title="预览与确认" />
    </ElSteps>
    <h3 ref="stepHeading" tabindex="-1">{{ ['对象与规格', '安排与通知', '预览与确认'][step] }}</h3>

    <ElForm v-if="step < 2" label-position="top" class="editor-fields" @submit.prevent="advance">
      <template v-if="step === 0">
        <div class="form-grid">
          <ElFormItem label="任务名称">
            <ElInput v-model.trim="name" maxlength="200" required />
          </ElFormItem>
          <ElFormItem label="任务类型">
            <ElSelect v-model="kind">
              <ElOption v-for="(label, value) in kindLabels" :key="value" :value="value" :label="label" />
            </ElSelect>
          </ElFormItem>
          <ElFormItem v-if="changes && templates.length" label="选用模板">
            <ElSelect v-model="templateId" :empty-values="[null, undefined]" @change="selectTemplate">
              <ElOption value="" label="从平台目录选择" />
              <ElOption v-for="template in templates" :key="template.id" :value="template.id" :label="template.name" />
            </ElSelect>
          </ElFormItem>
          <ElFormItem v-if="hasInstance" label="目标实例" for="task-instance"
            :error="fieldError === 'instance' ? editorError : undefined">
            <ElSelect id="task-instance" v-model="instanceId" placeholder="选择项目实例">
              <ElOption v-for="item in instances" :key="item.id" :value="item.id"
                :disabled="kind === 'shutdown' && item.charge_type === 'Spot'"
                :label="(item.name || item.id) + ' · ' + (instanceLabels[item.state] || item.state)
                  + ' · ' + (chargeLabels[item.charge_type ?? ''] || '计费未知')" />
            </ElSelect>
          </ElFormItem>
          <ElFormItem v-if="kind === 'start'" label="开机模式">
            <ElSelect v-model="bootMode" @change="mode => { if (mode === 'without_gpu') cardlessSpec = cardlessProfiles[0]?.spec }">
              <ElOption value="with_gpu" label="有卡开机" />
              <ElOption value="without_gpu" label="无卡开机" :disabled="!cardlessProfiles.length" />
            </ElSelect>
            <span v-if="instance && !cardlessProfiles.length" class="muted">{{ cardlessUnavailable(instance) }}</span>
          </ElFormItem>
          <ElFormItem v-if="kind === 'start' && bootMode === 'without_gpu'" label="无卡开机配置" for="task-cardless"
            :error="fieldError === 'cardless' ? editorError : undefined">
            <ElSelect id="task-cardless" v-model="cardlessSpec">
              <ElOption v-for="profile in cardlessProfiles" :key="profile.spec" :value="profile.spec"
                :label="cardlessProfileLabel(profile)" />
            </ElSelect>
          </ElFormItem>
        </div>

        <template v-if="changes">
          <ElAlert v-if="templateNote" :title="templateNote" :closable="false" type="info" />
          <div v-if="liveTemplate" class="section-heading">
            <p class="muted">引用模板第 {{ templateVersion }} 版，后续触发读取最新兼容版本。</p>
            <ElButton @click="detachTemplate">复制规格到任务</ElButton>
          </div>
          <ElAlert v-if="templateChanged" title="模板已更新，请重新载入规格并检查计费与预算。" type="warning" :closable="false">
            <ElButton v-if="currentTemplate" size="small" @click="selectTemplate">载入最新版本</ElButton>
          </ElAlert>
          <fieldset class="editor-fields" :disabled="liveTemplate">
            <legend>可用区优先顺序</legend>
            <div v-for="(item, index) in zones" :key="index" class="form-grid zone-fields">
              <LocationFields :model-value="item" :options="locations" :ordinal="index + 1" :disabled="liveTemplate"
                @update:model-value="setZone(index, $event)" />
              <ElButton :disabled="liveTemplate || zones.length === 1" @click="removeZone(index)">移除此区</ElButton>
            </div>
            <div><ElButton :disabled="liveTemplate || zones.length >= 50" @click="zones.push({ region: '', zone: '' })">添加可用区</ElButton></div>
          </fieldset>
          <SpecFields ref="specFields" v-model="spec" :project-id="projectId" :catalog="catalog.data" :catalog-error="catalogError" :pool-only="kind === 'stock'"
            :zones="zones" allow-spot require-quantity :require-system-disk="kind === 'acquire'"
            :disabled="liveTemplate" :legacy="!managedStorage && legacySpec === legacyKey" :managed-storage="managedStorage" />
          <div v-if="kind === 'acquire' && !managedStorage && !liveTemplate">
            <ElButton @click="useManagedStorage">使用平台系统盘与按区配置</ElButton>
          </div>
          <template v-if="managedStorage">
            <InstanceStorageFields v-for="(item, index) in zones" :key="zoneKey(item) + ':' + index" ref="storageFields"
              v-model="zoneStorage[zoneKey(item)]" :project-id="projectId" :location="item"
              :gpu-model="spec.gpu_model" :charge-type="spec.charge_type" :image-id="spec.image_id"
              :disabled="liveTemplate" :heading="(item.zone ? zoneName(item) : `可用区 ${index + 1}`) + '磁盘与存储'" />
          </template>
          <fieldset v-if="kind === 'acquire'">
            <legend>采购预算</legend>
            <div class="form-grid">
              <ElFormItem :label="budgetLabel">
                <ElInput v-model="maxPrice" type="number" min="0" max="10000000" step="0.000001" required />
              </ElFormItem>
              <ElFormItem label="总购买次数">
                <ElInput v-model.number="purchaseLimit" type="number" min="1" max="1000" step="1" required />
              </ElFormItem>
              <ElCheckbox v-model="skipIfMatch">已有匹配实例时跳过新建</ElCheckbox>
            </div>
          </fieldset>
        </template>
      </template>

      <template v-else>
        <fieldset class="editor-fields">
          <legend>预约安排</legend>
          <div class="form-grid">
            <ElFormItem label="周期">
              <ElSelect v-model="scheduleType">
                <ElOption v-for="(label, value) in scheduleLabels" :key="value" :value="value" :label="label" />
              </ElSelect>
            </ElFormItem>
            <ElFormItem :label="(kind === 'shutdown' ? '关机' : '首次执行') + '时间（北京时间）'"
              for="task-schedule" :error="fieldError === 'schedule' ? editorError : undefined">
              <ElDatePicker id="task-schedule" v-model="scheduleAt" type="datetime" format="YYYY-MM-DD HH:mm"
                value-format="YYYY-MM-DD[T]HH:mm" :clearable="false" />
            </ElFormItem>
            <ElFormItem v-if="scheduleType === 'interval'" label="间隔（分钟）">
              <ElInput v-model.number="intervalMinutes" type="number" :min="kind === 'shutdown' ? 6 : 1"
                max="525600" step="1" required />
            </ElFormItem>
            <ElFormItem v-if="kind !== 'shutdown'"
              :label="hasCapacityRetry ? '首次执行窗口（分钟）' : '执行窗口（分钟）'">
              <ElInput v-model.number="windowMinutes" type="number" min="0.0167" max="10080" step="any" required />
            </ElFormItem>
            <ElFormItem v-if="linkedShutdown && kind === 'start'" label="开机后关机安排">
              <ElSelect v-model="shutdownChoice">
                <ElOption value="none" label="不设置关机预约" />
                <ElOption value="duration" label="每轮实际开机后按运行时长关机" />
                <ElOption v-if="scheduleType === 'once' || shutdownChoice === 'absolute'" value="absolute" label="固定日期时间关机" />
              </ElSelect>
            </ElFormItem>
            <ElFormItem v-if="linkedShutdown && kind === 'start' && shutdownChoice === 'duration'" label="运行时长（分钟）"
              :error="fieldError === 'shutdown' ? editorError : undefined">
              <ElInput id="task-shutdown" v-model.number="shutdownMinutes" type="number" min="15" max="10080" step="any" required />
            </ElFormItem>
            <ElFormItem v-if="linkedShutdown && (kind !== 'start' || shutdownChoice === 'absolute')" label="实际取得实例后关机（北京时间）"
              for="task-shutdown" :error="fieldError === 'shutdown' ? editorError : undefined">
              <ElDatePicker id="task-shutdown" v-model="shutdownAt" type="datetime" format="YYYY-MM-DD HH:mm"
                value-format="YYYY-MM-DD[T]HH:mm" value-on-clear="" />
            </ElFormItem>
          </div>
          <template v-if="kind === 'start' && bootMode === 'with_gpu'">
            <ElCheckbox v-model="capacityRetry">资源不足时持续重试</ElCheckbox>
            <ElFormItem v-if="capacityRetry" label="重试间隔（秒）" for="task-capacity-retry"
              :error="fieldError === 'capacity-retry' ? editorError : undefined">
              <ElInput id="task-capacity-retry" v-model.number="capacityRetrySeconds" type="number" min="30" max="3600"
                step="1" required />
            </ElFormItem>
            <p v-if="capacityRetry" class="muted">平台明确返回资源不足后持续尝试，直到有卡开机或停止该次运行。</p>
          </template>
          <p v-if="kind === 'shutdown'" class="muted">周期关机由后台逐次登记。发现其它关机时间时，在实例详情选择保留或替换。</p>
        </fieldset>

        <fieldset class="editor-fields">
          <legend>结果通知</legend>
          <ElFormItem label="通知事件">
            <ElCheckboxGroup v-model="notificationEvents">
              <ElCheckbox v-for="event in availableEventOptions" :key="event.value" :value="event.value">
                {{ event.label }}
              </ElCheckbox>
            </ElCheckboxGroup>
          </ElFormItem>
          <ElFormItem label="渠道">
            <ElCheckboxGroup v-model="channels">
              <ElCheckbox value="site" disabled>站内通知</ElCheckbox>
              <ElCheckbox value="feishu" :disabled="!channels.includes('feishu') && props.channels?.feishu === false">飞书</ElCheckbox>
              <ElCheckbox value="email" :disabled="!channels.includes('email') && props.channels?.email === false">邮件</ElCheckbox>
            </ElCheckboxGroup>
          </ElFormItem>
          <ElFormItem v-if="channels.includes('email')" label="收件地址">
            <ElInput v-model.trim="email" type="email" maxlength="254" required />
          </ElFormItem>
        </fieldset>
      </template>

      <footer class="step-actions">
        <ElButton v-if="step === 1" @click="previousStep">上一步</ElButton>
        <ElButton type="primary" native-type="submit" :disabled="templateChanged">
          {{ step === 0 ? '下一步' : '预览任务' }}
        </ElButton>
      </footer>
    </ElForm>

    <div v-else class="editor-fields">
      <ElAlert v-if="editorError" :title="editorError" :closable="false" type="error" show-icon />
      <ElAlert v-if="templateChanged" title="模板版本已变化，请返回更新规格后重新预览。" :closable="false" type="warning" />
      <ElSkeleton v-if="editorPending" animated :rows="3" aria-label="正在读取服务端预览" />
      <template v-if="preview">
        <ElDescriptions :column="2" border>
          <ElDescriptionsItem label="下一次执行">
            {{ preview.scheduled_at === null ? '暂无后续执行' : timeLabel(preview.scheduled_at) }}
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="preview.deadline_at !== null"
            :label="kind === 'shutdown' ? '登记截止' : hasCapacityRetry ? '首次执行窗口截止' : '窗口截止'">
            {{ timeLabel(preview.deadline_at) }}
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="preview.boot_mode" label="开机模式">
            {{ preview.boot_mode === 'without_gpu' ? `无卡 · ${cardlessProfileLabel(cardlessProfiles.find(profile => profile.spec === preview?.without_gpu_spec))}` : '有卡' }}
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="preview.shutdown_after_seconds" label="每轮关机规则">
            本轮实际开机后运行 {{ Number((preview.shutdown_after_seconds / 60).toFixed(2)) }} 分钟；已运行时保留原预约
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="hasCapacityRetry" label="资源不足时">
            每 {{ capacityRetrySeconds }} 秒重试，直至有卡开机或停止该次运行
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="preview.max_price != null" label="报价上限">
            ¥{{ preview.max_price }} · 共 {{ preview.purchase_limit }} 次
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="hasInstance || kind === 'acquire'" label="实例快照时间">
            {{ timeLabel(preview.snapshot_at) }}
          </ElDescriptionsItem>
          <ElDescriptionsItem v-if="preview.template_version" label="模板版本">{{ preview.template_version }}</ElDescriptionsItem>
        </ElDescriptions>
        <ElAlert v-if="preview.snapshot_error" :title="preview.snapshot_error" :closable="false" type="warning" />
        <ul v-if="managedStorage" class="row-list" aria-label="各可用区磁盘与存储">
          <li v-for="item in zones" :key="zoneKey(item)">
            <strong>{{ zoneName(item) }}</strong> · 平台系统盘
            <template v-for="(disk, index) in zoneStorage[zoneKey(item)]?.disks" :key="index"> · {{ disk.type }} {{ disk.size_gb }} GB</template>
            <template v-if="zoneStorage[zoneKey(item)]?.shared_storage?.mode === 'us3'"> · 自动挂载 US3</template>
            <template v-else-if="zoneStorage[zoneKey(item)]?.shared_storage?.mode === 'cfs_attach'"> · 挂载已有 CFS</template>
          </li>
        </ul>
        <p v-if="hasInstance || kind === 'acquire'">
          可能匹配实例 {{ preview.matching_instances.length }} 台。执行前由后台重新读取云端状态。
        </p>
        <ul v-if="preview.matching_instances.length" class="row-list">
          <li v-for="item in preview.matching_instances" :key="item.id">
            {{ item.name || item.id }} · {{ zoneName(item) }} · {{ instanceLabels[item.state] || item.state }}
          </li>
        </ul>
        <ElForm v-if="kind === 'acquire' && !liveTemplate" class="template-save" label-position="top" @submit.prevent="saveTemplate">
          <ElFormItem label="模板名称">
            <ElInput v-model.trim="templateName" maxlength="120" />
          </ElFormItem>
          <ElButton native-type="submit" :disabled="!canSave || !templateName">保存模板</ElButton>
          <p v-if="templateMessage" role="status">{{ templateMessage }}</p>
        </ElForm>
      </template>

      <footer class="step-actions">
        <ElButton :disabled="saving" @click="previousStep">上一步</ElButton>
        <ElButton v-if="!preview && !editorPending && !templateChanged" type="primary" @click="previewTask">重试预览</ElButton>
        <ElButton v-else type="primary" :disabled="!canSave" :loading="saving" @click="saveTask">保存任务</ElButton>
      </footer>
    </div>
  </section>
</template>

<style scoped>
.editor-fields { display: grid; gap: 16px; min-width: 0; }
.editor-fields :deep(.el-form-item), .template-save :deep(.el-form-item) { margin-bottom: 0; min-width: 0; }
.editor-fields :deep(.el-date-editor) { width: 100%; min-width: 0; }
.editor-fields :deep(.el-checkbox) { margin-inline-end: 16px; }
.step-actions, .template-save {
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 8px;
}
.step-actions { justify-content: end; padding-top: 8px; border-top: 1px solid var(--line); }
.step-actions :deep(.el-button + .el-button), .template-save :deep(.el-button) { margin: 0; }
.zone-fields { align-items: end; }
.zone-fields :deep(.el-button) { justify-self: start; }
.task-editor h3:focus { outline: 2px solid var(--accent); outline-offset: 4px; }
@media (max-width: 767px) {
  .editor-fields .form-grid { grid-template-columns: minmax(0, 1fr); }
  .task-steps { padding: 8px; }
  .task-steps :deep(.el-step__title) { font-size: 12px; white-space: nowrap; }
  .task-steps :deep(.el-step__arrow) { scale: .65; }
  .step-actions { justify-content: space-between; }
  .step-actions :deep(.el-button) { min-height: 44px; }
}
</style>
