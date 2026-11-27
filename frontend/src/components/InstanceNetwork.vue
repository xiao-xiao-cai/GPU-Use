<script setup lang="ts">
import { computed, ref } from 'vue'
import { isSafeInstanceLink, type Instance } from '../api/client'
import { copyText } from '../clipboard'

const props = defineProps<{ instance: Instance; stale: boolean }>()
const feedback = ref('')
const ipLabels: Record<string, string> = { Private: '私网', Public: '公网', BGP: '公网 BGP' }
const loginLabels: Record<string, string> = { Password: '密码', KeyPair: '密钥对', SSHKey: 'SSH 密钥' }
const apps = computed(() => (props.instance.software_links ?? [])
  .filter(app => app.name.trim().toLowerCase() !== 'jupyterlab' && isSafeInstanceLink(app.url)))
const canOpen = computed(() => props.instance.state === 'Running' && !props.stale)

async function copyIP(address: string) {
  feedback.value = await copyText(address) ? '已复制 IP 地址。' : '无法自动复制，请选中 IP 地址。'
}
</script>

<template>
  <ElCard shadow="never" class="network-card">
    <h2>网络与连接</h2>
    <p v-if="instance.login_mode" class="muted">SSH 登录方式 {{ loginLabels[instance.login_mode] ?? instance.login_mode }}</p>
    <details v-if="instance.ssh?.command">
      <summary>查看 SSH 命令</summary>
      <code>{{ instance.ssh.command }}</code>
    </details>
    <h3>IP 地址</h3>
    <ul v-if="instance.ip_addresses?.length" class="network-list">
      <li v-for="ip in instance.ip_addresses" :key="ip.address + ip.type">
        <div class="network-row">
          <span><code>{{ ip.address }}</code> · {{ ipLabels[ip.type ?? ''] ?? ip.type ?? '网络类型未知' }}<template v-if="ip.is_default"> · 默认</template></span>
          <ElButton type="primary" link @click="copyIP(ip.address)">复制 IP</ElButton>
        </div>
        <p class="muted">{{ ip.bandwidth_mbps == null ? '带宽未知' : ip.bandwidth_mbps + ' Mbps' }}</p>
        <p v-if="ip.mode || ip.ip_id || ip.subnet_id || ip.vpc_id || ip.mac" class="muted mono">
          <span v-if="ip.mode">{{ ip.mode }} </span>
          <span v-if="ip.ip_id">IP ID {{ ip.ip_id }} </span>
          <span v-if="ip.subnet_id">子网 {{ ip.subnet_id }} </span>
          <span v-if="ip.vpc_id">VPC {{ ip.vpc_id }} </span>
          <span v-if="ip.mac">MAC {{ ip.mac }} </span>
        </p>
      </li>
    </ul>
    <p v-else class="muted">平台未返回 IP 地址。</p>
    <p v-if="feedback" role="status">{{ feedback }}</p>
    <h3>其他应用</h3>
    <ul v-if="apps.length" class="network-list">
      <li v-for="app in apps" :key="app.name + app.url">
        <div class="network-row">
          <strong>{{ app.name }}</strong>
          <a v-if="canOpen" :href="app.url" target="_blank" rel="noopener noreferrer">打开 {{ app.name }} ↗</a>
        </div>
        <code>{{ app.url }}</code>
      </li>
    </ul>
    <p v-else class="muted">平台未返回其他应用。</p>
    <p v-if="apps.length && !canOpen" class="muted">实例正常运行且状态已同步时可打开应用。</p>
  </ElCard>
</template>

<style scoped>
.network-card :deep(.el-card__body) { display: grid; gap: 12px; }
.network-card code, .network-list li { overflow-wrap: anywhere; }
.network-card p { margin: 0; }
.network-list { display: grid; gap: 8px; padding: 0; margin: 0; list-style: none; }
.network-list li { padding: 8px 12px; border: 1px solid var(--line); border-radius: 4px; }
.network-row { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 8px; overflow-wrap: anywhere; }
summary { cursor: pointer; color: var(--accent); }
</style>
