<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import { request } from '../api/client'
import { copyText } from '../clipboard'
import { errorMessage, timeLabel } from '../display'

type McpInfo = { url: string; local_url: string; healthy: boolean }
type McpKey = {
  id: string
  name: string
  manage: boolean
  ssh: boolean
  status: 'active' | 'expired' | 'revoked'
  created_at: number
  expires_at: number | null
  last_used_at: number | null
}
type IssuedKey = { key: McpKey; token: string }
type KeyDraft = { name: string; manage: boolean; ssh: boolean; days: 7 | 30 | 90 | 'unlimited' }

const labels = { active: '有效', expired: '已过期', revoked: '已撤销' }
const tags = { active: 'success', expired: 'warning', revoked: 'info' } as const
const info = ref<McpInfo>()
const keys = ref<McpKey[]>([])
const loading = ref(true)
const loadError = ref('')
const endpoint = ref<'url' | 'local_url'>('url')
const url = computed(() => info.value?.[endpoint.value] ?? '')
const codexConfig = computed(() => '[mcp_servers.gpu-console]\nurl = ' + JSON.stringify(url.value)
  + '\nbearer_token_env_var = "GPU_CONSOLE_MCP_TOKEN"')
const httpConfig = computed(() => JSON.stringify({
  type: 'streamable-http', url: url.value, headers: { Authorization: 'Bearer <MCP_KEY>' },
}, null, 2))

const dialog = ref(false)
const operation = ref<'create' | 'rotate' | 'revoke'>('create')
const target = ref<McpKey>()
const issued = ref<IssuedKey>()
const draft = ref<KeyDraft>({ name: '', manage: false, ssh: false, days: 90 })
const saving = ref(false)
const saveError = ref('')
const title = computed(() => issued.value ? '复制 MCP Key'
  : { create: '签发 MCP Key', rotate: '轮换 MCP Key', revoke: '撤销 MCP Key' }[operation.value])
let loadRequest: AbortController | undefined
let saveRequest: AbortController | undefined

async function load() {
  loadRequest?.abort()
  const controller = new AbortController()
  loadRequest = controller
  loading.value = true
  loadError.value = ''
  try {
    const [connection, rows] = await Promise.all([
      request<McpInfo>('/mcp', { signal: controller.signal }),
      request<McpKey[]>('/mcp/keys', { signal: controller.signal }),
    ])
    if (controller.signal.aborted) return
    info.value = connection
    keys.value = rows
  } catch (error) {
    if (!controller.signal.aborted) loadError.value = errorMessage(error)
  } finally {
    if (loadRequest === controller) {
      loadRequest = undefined
      loading.value = false
    }
  }
}

function open(action: typeof operation.value, key?: McpKey) {
  operation.value = action
  target.value = key
  draft.value = {
    name: key?.name ?? '', manage: key?.manage ?? false, ssh: key?.ssh ?? false,
    days: key && key.expires_at === null ? 'unlimited' : 90,
  }
  saveError.value = ''
  dialog.value = true
}

async function submit() {
  if (saveRequest || issued.value || !dialog.value) return
  const action = operation.value
  const key = target.value
  const name = draft.value.name.trim()
  if (action !== 'revoke' && !name) return
  const controller = new AbortController()
  saveRequest = controller
  saving.value = true
  saveError.value = ''
  const base = '/mcp/keys'
  try {
    if (action === 'revoke' && key) {
      await request<void>(base + '/' + key.id, { method: 'DELETE', signal: controller.signal })
      if (controller.signal.aborted) return
      keys.value = keys.value.map(row => row.id === key.id ? { ...row, status: 'revoked' } : row)
      dialog.value = false
      ElMessage.success('已撤销 Key')
      return
    }
    const result = await request<IssuedKey>(key ? base + '/' + key.id + '/rotate' : base, {
      method: 'POST',
      body: { ...draft.value, name, days: draft.value.days === 'unlimited' ? null : draft.value.days },
      signal: controller.signal,
    })
    if (controller.signal.aborted) return
    keys.value = [result.key, ...keys.value.filter(row => row.id !== result.key.id)]
    issued.value = result
  } catch (error) {
    if (!controller.signal.aborted) saveError.value = errorMessage(error)
  } finally {
    if (saveRequest === controller) {
      saveRequest = undefined
      saving.value = false
    }
  }
}

async function copy(value: string) {
  if (await copyText(value)) ElMessage.success('已复制')
  else ElMessage.error('复制失败，请手动复制')
}

watch(dialog, visible => {
  if (!visible) issued.value = undefined
}, { flush: 'sync' })
onMounted(load)
onScopeDispose(() => {
  loadRequest?.abort()
  saveRequest?.abort()
  issued.value = undefined
})
</script>

<template>
  <div class="mcp-settings">
    <div class="mcp-toolbar">
      <ElTag v-if="info" :type="info.healthy ? 'success' : 'warning'">
        {{ info.healthy ? 'MCP 服务可用' : 'MCP 服务不可达' }}
      </ElTag>
      <div class="mcp-actions">
        <ElButton :loading="loading" :disabled="dialog" @click="load">刷新</ElButton>
        <ElButton type="primary" :disabled="loading || !!loadError" @click="open('create')">签发 Key</ElButton>
      </div>
    </div>

    <ElAlert v-if="loadError" :title="loadError" type="error" :closable="false" />
    <ElSkeleton v-if="loading && !info" animated :rows="4" aria-label="正在读取 MCP 配置" />
    <section v-if="info" class="mcp-section" aria-labelledby="mcp-connection-heading">
      <h3 id="mcp-connection-heading">连接配置</h3>
      <ElRadioGroup v-model="endpoint" aria-label="客户端所在位置">
        <ElRadioButton value="url">远程</ElRadioButton>
        <ElRadioButton value="local_url">部署主机</ElRadioButton>
      </ElRadioGroup>
      <div class="mcp-address">
        <code>{{ url }}</code>
        <ElButton @click="copy(url)">复制地址</ElButton>
      </div>
      <div class="mcp-configs">
        <div>
          <h4>Codex</h4>
          <p class="muted">启动客户端前，在其环境中设置 GPU_CONSOLE_MCP_TOKEN。</p>
          <pre>{{ codexConfig }}</pre>
          <ElButton @click="copy(codexConfig)">复制 Codex 配置</ElButton>
        </div>
        <div>
          <h4>Streamable HTTP</h4>
          <p class="muted">在客户端的凭据设置中填写签发的 Key。</p>
          <pre>{{ httpConfig }}</pre>
          <ElButton @click="copy(httpConfig)">复制 HTTP 配置</ElButton>
        </div>
      </div>
    </section>

    <section v-if="info" class="mcp-section" aria-labelledby="mcp-keys-heading">
      <h3 id="mcp-keys-heading">客户端 Key</h3>
      <ElEmpty v-if="!keys.length" description="还没有签发 MCP Key" />
      <div v-for="key in keys" :key="key.id" class="mcp-key">
        <div>
          <strong>{{ key.name }}</strong>
          <div class="mcp-key-tags">
            <ElTag size="small" :type="tags[key.status]">{{ labels[key.status] }}</ElTag>
            <ElTag size="small" type="info">只读</ElTag>
            <ElTag v-if="key.manage" size="small">管理</ElTag>
            <ElTag v-if="key.ssh" size="small" type="warning">SSH</ElTag>
          </div>
        </div>
        <dl>
          <div><dt>签发</dt><dd>{{ timeLabel(key.created_at) }}</dd></div>
          <div><dt>到期</dt><dd>{{ key.expires_at === null ? '长期有效' : timeLabel(key.expires_at) }}</dd></div>
          <div><dt>最近认证</dt><dd>{{ key.last_used_at === null ? '暂无记录' : timeLabel(key.last_used_at) }}</dd></div>
        </dl>
        <div v-if="key.status !== 'revoked'" class="mcp-actions">
          <ElButton :disabled="loading" @click="open('rotate', key)">轮换</ElButton>
          <ElButton type="danger" plain :disabled="loading" @click="open('revoke', key)">撤销</ElButton>
        </div>
      </div>
    </section>

    <ElDialog v-model="dialog" :title="title" width="min(500px, 92vw)" destroy-on-close
      :show-close="!saving" :close-on-click-modal="false" :close-on-press-escape="!saving">
      <div v-if="issued" class="mcp-issued">
        <p>Key 只显示这一次，请保存到客户端的凭据设置。</p>
        <ElInput :model-value="issued.token" readonly aria-label="新签发的 MCP Key" />
      </div>
      <p v-else-if="operation === 'revoke'">
        撤销「{{ target?.name }}」后，使用该 Key 的客户端和 SSH 连接将失效。
      </p>
      <ElForm v-else :model="draft" :disabled="saving" label-position="top" @submit.prevent="submit">
        <p v-if="operation === 'rotate'">签发新 Key 后，当前 Key 将立即失效。</p>
        <ElFormItem label="名称" required>
          <ElInput v-model="draft.name" maxlength="64" placeholder="例如 Codex 工作站" />
        </ElFormItem>
        <ElFormItem label="权限（覆盖所有项目）">
          <div class="mcp-permissions">
            <ElCheckbox :model-value="true" disabled>只读</ElCheckbox>
            <ElCheckbox v-model="draft.manage">管理实例、任务和通知</ElCheckbox>
            <ElCheckbox v-model="draft.ssh">使用 SSH 访问已登记实例</ElCheckbox>
          </div>
        </ElFormItem>
        <ElFormItem label="有效期">
          <ElSelect v-model="draft.days">
            <ElOption label="7 天" :value="7" />
            <ElOption label="30 天" :value="30" />
            <ElOption label="90 天" :value="90" />
            <ElOption label="长期有效" value="unlimited" />
          </ElSelect>
        </ElFormItem>
        <button type="submit" hidden></button>
      </ElForm>
      <ElAlert v-if="saveError" :title="saveError" type="error" :closable="false" />
      <template #footer>
        <template v-if="issued">
          <ElButton @click="dialog = false">完成</ElButton>
          <ElButton type="primary" @click="copy(issued.token)">复制 Key</ElButton>
        </template>
        <template v-else>
          <ElButton :disabled="saving" @click="dialog = false">取消</ElButton>
          <ElButton :type="operation === 'revoke' ? 'danger' : 'primary'" :loading="saving"
            :disabled="operation !== 'revoke' && !draft.name.trim()" @click="submit">
            {{ operation === 'revoke' ? '撤销 Key' : '签发 Key' }}
          </ElButton>
        </template>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.mcp-settings,
.mcp-section {
  display: grid;
  gap: 14px;
  min-width: 0;
}

.mcp-toolbar,
.mcp-address,
.mcp-key,
.mcp-actions,
.mcp-key-tags {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.mcp-toolbar,
.mcp-address,
.mcp-key {
  justify-content: space-between;
}

.mcp-toolbar .mcp-actions {
  margin-inline-start: auto;
}

.mcp-actions :deep(.el-button + .el-button) {
  margin-inline-start: 0;
}

.mcp-section {
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  padding: 16px;
}

h3,
h4 {
  margin: 0;
}

.mcp-configs {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr));
  gap: 16px;
}

.mcp-configs > div {
  min-width: 0;
}

.mcp-address code,
.mcp-key strong {
  overflow-wrap: anywhere;
}

pre {
  overflow: auto;
  padding: 10px;
  background: var(--el-fill-color-light);
  border-radius: 6px;
}

.mcp-key {
  gap: 12px;
  padding-block: 12px;
  border-top: 1px solid var(--el-border-color-light);
}

.mcp-key-tags {
  margin-top: 6px;
}

dl {
  display: flex;
  gap: 8px 16px;
  flex-wrap: wrap;
  margin: 0;
}

dt {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

dd {
  margin: 4px 0 0;
}

.mcp-permissions {
  display: grid;
}

.mcp-issued p {
  margin-top: 0;
}
</style>
