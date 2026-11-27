<script setup lang="ts">
import { Box, Calendar, DataAnalysis, Grid, Setting, SwitchButton } from '@element-plus/icons-vue'

defineProps<{ page: string; pending: boolean }>()
const emit = defineEmits<{ navigate: []; logout: [] }>()
const links = [
  { id: 'instances', label: '实例总览', icon: Grid },
  { id: 'dashboard', label: '数据看板', icon: DataAnalysis },
  { id: 'inventory', label: 'GPU 库存', icon: Box },
  { id: 'tasks', label: '任务中心', icon: Calendar },
  { id: 'settings', label: '设置与通知', icon: Setting },
] as const
</script>

<template>
  <div class="console-nav">
    <nav aria-label="管理台导航">
      <a v-for="link in links" :key="link.id" :href="'#' + link.id"
        :aria-current="page === link.id ? 'page' : undefined" :aria-label="link.label"
        :title="link.label" @click="emit('navigate')">
        <ElIcon aria-hidden="true"><component :is="link.icon" /></ElIcon>
        <span>{{ link.label }}</span>
      </a>
    </nav>
    <ElButton class="logout" :icon="SwitchButton" aria-label="退出登录" title="退出登录"
      :loading="pending" @click="emit('logout')"><span class="logout-label">退出登录</span></ElButton>
  </div>
</template>

<style scoped>
.console-nav { display: flex; flex: 1; flex-direction: column; justify-content: space-between; gap: 24px; min-height: 0; }
nav { display: grid; gap: 6px; }
nav a {
  display: flex;
  min-height: 44px;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border-radius: 6px;
  color: #dbe9ed;
  text-decoration: none;
  white-space: nowrap;
}
nav a :deep(.el-icon) { font-size: 20px; color: #9bd1c5; }
nav a[aria-current] { box-shadow: inset 3px 0 #99dbc8; }
nav a[aria-current], nav a:hover { color: #fff; background: #254650; }
.logout {
  justify-content: flex-start;
  min-height: 44px;
  --el-button-bg-color: transparent;
  --el-button-text-color: #d7e9ec;
  --el-button-border-color: #51737b;
  --el-button-hover-bg-color: #254650;
  --el-button-hover-text-color: #fff;
  --el-button-hover-border-color: #9bd1c5;
}
.logout :deep(.el-icon) { margin: 0; font-size: 18px; }
@media (min-width: 768px) and (max-width: 1199px) {
  nav a, .logout { justify-content: center; padding-inline: 0; }
  nav a span, .logout-label { display: none; }
  .logout :deep(.el-icon + span) { margin: 0; }
}
</style>
