<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue'
import { isSafeInstanceLink, query, request, type Instance, type InstanceJupyter } from '../api/client'
import { copyText } from '../clipboard'
import { errorMessage } from '../display'

const props = defineProps<{
  instance: Instance
  jupyter: InstanceJupyter
  stale: boolean
}>()
const emit = defineEmits<{ jupyterChange: [link: InstanceJupyter] }>()

const editing = ref(false)
const linkPending = ref(false)
const draft = ref('')
const linkStatus = ref('')
const linkError = ref('')
const sshCopied = ref<boolean | null>(null)
let linkRequest: AbortController | undefined

const passwordOpen = ref(false)
const revealedPassword = ref('')
const passwordError = ref('')
const passwordPending = ref(false)
const passwordCopyStatus = ref('')
let passwordRequest: AbortController | undefined

const jupyterUrl = computed(() => isSafeInstanceLink(props.jupyter.url) ? props.jupyter.url : null)
const jupyterUnavailable = computed(() => {
  if (props.instance.state === 'Stopped') return '实例开机后可访问 JupyterLab。'
  if (props.stale) return '同步实例状态后可打开 JupyterLab。'
  if (props.instance.state !== 'Running') return '等待实例进入运行状态。'
  return jupyterUrl.value ? '' : '尚无 JupyterLab 地址，可在连接设置中填写。'
})
const sshCommand = computed(() => props.instance.ssh?.command || '')
const usesKeyOnly = computed(() => ['keypair', 'sshkey'].includes(props.instance.login_mode?.toLowerCase() ?? ''))
const instancePath = computed(() => '/instances/' + encodeURIComponent(props.instance.id))
const instanceScope = computed(() => query({ project_id: props.instance.project_id }))

function editLink() {
  draft.value = jupyterUrl.value ?? ''
  linkStatus.value = ''
  linkError.value = ''
  editing.value = true
}

async function updateLink(method: 'PUT' | 'DELETE') {
  if (linkPending.value) return
  const url = draft.value.trim()
  linkError.value = ''
  linkStatus.value = ''
  if (method === 'PUT' && !isSafeInstanceLink(url)) {
    linkError.value = '请输入完整的 HTTP 或 HTTPS 地址，去掉登录参数和片段。可以保留工作区路径。'
    return
  }
  const controller = new AbortController()
  linkRequest = controller
  linkPending.value = true
  try {
    const body = method === 'PUT' ? { url } : undefined
    const link = await request<InstanceJupyter>(instancePath.value + '/jupyter' + instanceScope.value, {
      method, body, signal: controller.signal,
    })
    if (controller.signal.aborted) return
    emit('jupyterChange', link)
    editing.value = false
    linkStatus.value = method === 'PUT' ? '已保存自定义地址。' : '已恢复使用平台地址。'
  } catch (error) {
    if (!controller.signal.aborted) linkError.value = errorMessage(error)
  } finally {
    if (linkRequest === controller) {
      linkPending.value = false
      linkRequest = undefined
    }
  }
}

async function copyAddress() {
  if (!jupyterUrl.value) return
  linkStatus.value = await copyText(jupyterUrl.value) ? '已复制 JupyterLab 地址。' : '无法自动复制，请选中地址。'
}

async function copyCommand() {
  const command = sshCommand.value
  const copied = await copyText(command)
  if (sshCommand.value === command) sshCopied.value = copied
}

function clearPassword() {
  passwordRequest?.abort()
  passwordRequest = undefined
  revealedPassword.value = ''
  passwordPending.value = false
  passwordError.value = ''
  passwordCopyStatus.value = ''
}

function closePassword(done: () => void) {
  clearPassword()
  done()
}

async function revealPassword() {
  if (passwordOpen.value || usesKeyOnly.value) return
  clearPassword()
  passwordOpen.value = true
  const controller = new AbortController()
  passwordRequest = controller
  passwordPending.value = true
  try {
    const result = await request<{ password: string }>(instancePath.value + '/ssh-password' + instanceScope.value, {
      method: 'POST', signal: controller.signal,
    })
    if (!controller.signal.aborted && passwordOpen.value) {
      if (typeof result.password !== 'string' || !result.password) throw new Error('平台未返回 SSH 密码。')
      revealedPassword.value = result.password
    }
  } catch (error) {
    if (!controller.signal.aborted) passwordError.value = errorMessage(error)
  } finally {
    if (passwordRequest === controller) {
      passwordRequest = undefined
      passwordPending.value = false
    }
  }
}

async function copyPassword() {
  const password = revealedPassword.value
  if (!password) return
  const copied = await copyText(password)
  if (passwordOpen.value && revealedPassword.value === password) {
    passwordCopyStatus.value = copied ? '已复制 SSH 密码。' : '无法自动复制，请选中密码复制。'
  }
}

watch([instancePath, instanceScope], () => {
  linkRequest?.abort()
  editing.value = false
  linkStatus.value = ''
  sshCopied.value = null
  passwordOpen.value = false
  clearPassword()
}, { flush: 'sync' })
watch(sshCommand, () => { sshCopied.value = null })
watch(passwordOpen, open => { if (!open) clearPassword() }, { flush: 'sync' })
onScopeDispose(() => {
  linkRequest?.abort()
  clearPassword()
})
</script>

<template>
  <div class="connection-area" aria-label="快速连接">
    <div class="connection-actions">
      <ElButton v-if="!jupyterUnavailable && jupyterUrl" tag="a" type="primary" class="launch-button"
        :href="jupyterUrl" target="_blank" rel="noopener noreferrer">打开 JupyterLab <span aria-hidden="true">↗</span></ElButton>
      <ElButton v-if="sshCommand" @click="copyCommand">复制 SSH 命令</ElButton>
      <ElButton v-if="!usesKeyOnly" link type="primary" @click="revealPassword">查看 SSH 密码</ElButton>
      <ElButton link type="primary" @click="editLink">连接设置</ElButton>
    </div>
    <p v-if="jupyterUnavailable" class="muted">{{ jupyterUnavailable }}</p>
    <p v-if="!sshCommand" class="muted">平台未返回 SSH 连接命令。</p>
    <p v-if="usesKeyOnly" class="muted">此实例使用 SSH 密钥登录。</p>
    <template v-if="sshCopied !== null">
      <p role="status" :class="{ 'copy-error': !sshCopied }">{{ sshCopied ? '已复制 SSH 命令。' : '无法自动复制，请选中 SSH 命令。' }}</p>
      <code v-if="!sshCopied" class="selectable-value">{{ sshCommand }}</code>
    </template>
    <p v-if="linkStatus && !editing" role="status">{{ linkStatus }}</p>

    <ElDialog v-model="editing" title="JupyterLab 连接设置" width="min(32rem, calc(100% - 24px))" append-to-body destroy-on-close
      :show-close="!linkPending" :close-on-press-escape="!linkPending" :close-on-click-modal="false">
      <ElForm label-position="top" :disabled="linkPending" @submit.prevent="updateLink('PUT')">
        <ElFormItem label="JupyterLab 地址" :error="linkError">
          <ElInput v-model="draft" type="url" maxlength="2048" autocomplete="off" />
        </ElFormItem>
        <p v-if="jupyterUrl" class="muted">当前地址（{{ jupyter.source === 'custom' ? '自定义' : '平台提供' }}）</p>
        <code v-if="jupyterUrl" class="selectable-value">{{ jupyterUrl }}</code>
        <p v-if="linkStatus" role="status">{{ linkStatus }}</p>
        <div class="connection-actions dialog-actions">
          <ElButton v-if="jupyterUrl" link type="primary" @click="copyAddress">复制当前地址</ElButton>
          <ElButton v-if="jupyter.source === 'custom'" link type="primary" :disabled="linkPending"
            @click="updateLink('DELETE')">恢复平台地址</ElButton>
          <ElButton :disabled="linkPending" @click="editing = false">取消</ElButton>
          <ElButton type="primary" native-type="submit" :loading="linkPending">保存地址</ElButton>
        </div>
      </ElForm>
    </ElDialog>

    <ElDialog v-model="passwordOpen" title="SSH 密码" width="min(32rem, calc(100% - 24px))" append-to-body destroy-on-close
      :before-close="closePassword" @close="clearPassword">
      <p class="muted">{{ instance.name || instance.id }}</p>
      <p v-if="passwordPending" role="status">正在查询平台记录的密码…</p>
      <ElAlert v-if="passwordError" :title="passwordError" type="warning" :closable="false" show-icon />
      <template v-if="revealedPassword">
        <code class="selectable-value">{{ revealedPassword }}</code>
        <p class="muted">如果你在实例系统内改过密码，请使用修改后的密码。</p>
      </template>
      <p v-if="passwordCopyStatus" role="status">{{ passwordCopyStatus }}</p>
      <template #footer>
        <ElButton v-if="revealedPassword" @click="copyPassword">复制密码</ElButton>
        <ElButton @click="passwordOpen = false">关闭</ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.connection-area, .connection-actions { min-width: 0; }
.connection-area { display: grid; gap: 8px; }
.connection-area p { margin: 0; }
.connection-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
.connection-actions > .el-button + .el-button { margin-left: 0; }
.dialog-actions { justify-content: flex-end; margin-top: 16px; }
.launch-button { text-decoration: none; }
.launch-button span { margin-left: 4px; }
.copy-error { color: var(--warning); }
.selectable-value { display: block; padding: 8px; background: var(--accent-bg); white-space: pre-wrap; user-select: all; }
@media (max-width: 767px) {
  .connection-actions > .el-button { min-height: 44px; }
}
</style>
