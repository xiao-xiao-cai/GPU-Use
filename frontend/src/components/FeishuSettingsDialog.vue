<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue'
import { request, type FeishuReceiveIdType, type FeishuSettings, type Settings } from '../api/client'
import { errorMessage } from '../display'

const props = defineProps<{ config: FeishuSettings | null; legacy: boolean }>()
const emit = defineEmits<{ saved: [settings: Settings] }>()
const open = defineModel<boolean>({ required: true })
const current = ref<FeishuSettings | null>(null)
const storedMode = ref<'app' | 'webhook' | null>(null)
const mode = ref<'app' | 'webhook'>('app')
const webhook = ref('')
const signingSecret = ref('')
const removeSignature = ref(false)
const keyword = ref('')
const appId = ref('')
const appSecret = ref('')
const receiveIdType = ref<FeishuReceiveIdType>('email')
const receiveId = ref('')
const saveError = ref('')
const saving = ref(false)
let saveRequest: AbortController | undefined

const receiverOptions = {
  email: { label: '个人邮箱', placeholder: '填写接收人的飞书账号邮箱' },
  open_id: { label: '个人 Open ID', placeholder: '填写该应用下以 ou_ 开头的接收人 ID' },
  chat_id: { label: '群会话 ID', placeholder: '填写以 oc_ 开头的群会话 ID' },
} satisfies Record<FeishuReceiveIdType, { label: string; placeholder: string }>
const currentApp = computed(() => current.value?.mode === 'app' ? current.value : null)
const currentWebhook = computed(() => current.value?.mode === 'app' ? null : current.value)
const keepAppSecret = computed(() => Boolean(currentApp.value && appId.value.trim() === currentApp.value.app_id))

watch(open, visible => {
  const config = visible ? props.config : null
  current.value = config
  storedMode.value = config?.mode ?? (config || (visible && props.legacy) ? 'webhook' : null)
  mode.value = storedMode.value ?? 'app'
  webhook.value = ''
  signingSecret.value = ''
  removeSignature.value = false
  keyword.value = currentWebhook.value?.keyword ?? ''
  appId.value = currentApp.value?.app_id ?? ''
  appSecret.value = ''
  receiveIdType.value = currentApp.value?.receive_id_type ?? 'email'
  receiveId.value = currentApp.value?.receive_id ?? ''
  saveError.value = ''
}, { flush: 'sync' })

const canSave = computed(() => {
  if (mode.value === 'webhook') {
    return Boolean(webhook.value.trim() || (currentWebhook.value && (
      signingSecret.value.trim() || removeSignature.value || keyword.value.trim() !== currentWebhook.value.keyword
    )))
  }
  if (!appId.value.trim() || !receiveId.value.trim()) return false
  if (appSecret.value.trim()) return true
  return keepAppSecret.value && (
    receiveIdType.value !== currentApp.value?.receive_id_type || receiveId.value.trim() !== currentApp.value?.receive_id
  )
})

function changeReceiverType() {
  receiveId.value = currentApp.value?.receive_id_type === receiveIdType.value ? currentApp.value.receive_id : ''
  saveError.value = ''
}

async function save() {
  if (!open.value || !canSave.value || saveRequest) return
  const controller = new AbortController()
  saveRequest = controller
  saving.value = true
  saveError.value = ''
  try {
    const result = await request<Settings>('/settings/feishu', {
      method: 'PUT',
      body: mode.value === 'app' ? {
        mode: 'app',
        app_id: appId.value.trim(),
        app_secret: appSecret.value.trim() || undefined,
        receive_id_type: receiveIdType.value,
        receive_id: receiveId.value.trim(),
      } : {
        mode: 'webhook',
        webhook_url: webhook.value.trim() || undefined,
        signing_secret: removeSignature.value ? null : signingSecret.value.trim() || undefined,
        keyword: keyword.value.trim(),
      },
      signal: controller.signal,
    })
    if (controller.signal.aborted) return
    open.value = false
    emit('saved', result)
  } catch (error) {
    if (!controller.signal.aborted) saveError.value = errorMessage(error)
  } finally {
    saveRequest = undefined
    saving.value = false
  }
}

onScopeDispose(() => saveRequest?.abort())
</script>

<template>
  <ElDialog v-model="open" title="配置飞书机器人" width="min(32rem, calc(100% - 24px))"
    append-to-body destroy-on-close :show-close="!saving" :close-on-press-escape="!saving" :close-on-click-modal="false">
    <ElForm label-position="top" autocomplete="off" :disabled="saving" @submit.prevent="save">
      <ElFormItem label="机器人类型">
        <ElRadioGroup v-model="mode" @change="saveError = ''">
          <ElRadio value="app">应用机器人</ElRadio>
          <ElRadio value="webhook">群自定义机器人</ElRadio>
        </ElRadioGroup>
      </ElFormItem>
      <ElAlert v-if="storedMode && mode !== storedMode" class="feishu-notice"
        title="保存后，飞书通知将使用所选的机器人配置。" type="info" :closable="false" />
      <ElAlert v-else-if="legacy" class="feishu-notice"
        title="当前使用服务器上的 Webhook。若要更换，请填写新的 Webhook 地址。" type="info" :closable="false" />
      <template v-if="mode === 'app'">
        <p class="feishu-instructions">
          在飞书开放平台打开企业自建应用，启用机器人，开通以应用身份发送消息的权限（<code>im:message:send_as_bot</code>）。发布版本并设置成员可用范围，在「凭证与基础信息」复制 App ID 和 App Secret。
        </p>
        <ElFormItem label="App ID" required>
          <ElInput v-model="appId" placeholder="以 cli_ 开头" maxlength="128" required />
        </ElFormItem>
        <ElFormItem label="App Secret" :required="!keepAppSecret">
          <ElInput v-model="appSecret" type="password" show-password autocomplete="new-password" maxlength="256"
            :required="!keepAppSecret" :placeholder="keepAppSecret ? '已配置，留空保留' : '粘贴应用的 App Secret'" />
        </ElFormItem>
        <ElFormItem label="接收方式">
          <ElSelect v-model="receiveIdType" @change="changeReceiverType">
            <ElOption v-for="(option, value) in receiverOptions" :key="value" :label="option.label" :value="value" />
          </ElSelect>
        </ElFormItem>
        <ElFormItem :label="receiverOptions[receiveIdType].label" required>
          <ElInput v-model="receiveId" :type="receiveIdType === 'email' ? 'email' : 'text'"
            :placeholder="receiverOptions[receiveIdType].placeholder" maxlength="254" required />
        </ElFormItem>
        <p v-if="receiveIdType === 'chat_id'" class="feishu-instructions">请先把应用机器人添加到目标群。</p>
      </template>
      <template v-else>
        <p class="feishu-instructions">
          在飞书群的「群机器人」中添加「自定义机器人」，复制 Webhook 地址。如果机器人开启了签名校验或自定义关键词，请填写对应值。
        </p>
        <ElFormItem label="Webhook 地址" :required="!currentWebhook">
          <ElInput v-model="webhook" type="password" show-password autocomplete="new-password" maxlength="2048"
            :required="!currentWebhook" :placeholder="currentWebhook ? '已配置，留空保留' : '粘贴飞书机器人 Webhook 地址'" />
        </ElFormItem>
        <ElFormItem label="签名密钥（可选）" for="feishu-signing-secret">
          <ElInput id="feishu-signing-secret" v-model="signingSecret" type="password" show-password autocomplete="new-password" maxlength="256"
            :disabled="saving || removeSignature"
            :placeholder="currentWebhook?.signing_configured ? '已配置，留空保留' : '仅在启用签名校验时填写'" />
          <ElCheckbox v-if="currentWebhook?.signing_configured" v-model="removeSignature" label="移除已保存的签名密钥" />
        </ElFormItem>
        <ElFormItem label="自定义关键词（可选）">
          <ElInput v-model="keyword" maxlength="100" />
        </ElFormItem>
      </template>
      <ElAlert v-if="saveError" title="保存飞书配置失败" :description="saveError" type="error"
        show-icon :closable="false" />
      <div class="feishu-actions">
        <ElButton :disabled="saving" @click="open = false">取消</ElButton>
        <ElButton type="primary" native-type="submit" :loading="saving" :disabled="!canSave">保存飞书配置</ElButton>
      </div>
    </ElForm>
  </ElDialog>
</template>

<style scoped>
.feishu-instructions { margin: 0 0 16px; }
.feishu-notice { margin-bottom: 16px; }
.feishu-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; margin-top: 16px; }
</style>
