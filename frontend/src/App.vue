<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, onUnmounted, provide, ref, watch } from 'vue'
import { Menu } from '@element-plus/icons-vue'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import ConsoleNav from './components/ConsoleNav.vue'
import InstancePage from './pages/InstancePage.vue'
import { query, request, updateSession, type Instance, type Project, type Session, type Settings } from './api/client'
import { queueCollection } from './api/refresh'
import { locationKey, type Location } from './catalog'
import { errorMessage, timeLabel, useSnapshot } from './display'

const InventoryPage = defineAsyncComponent(() => import('./pages/InventoryPage.vue'))
const TaskPage = defineAsyncComponent(() => import('./pages/TaskPage.vue'))
const SettingsPage = defineAsyncComponent(() => import('./pages/SettingsPage.vue'))
const DashboardPage = defineAsyncComponent(() => import('./pages/DashboardPage.vue'))
const DashboardSignals = defineAsyncComponent(() => import('./components/DashboardSignals.vue'))
const InstanceDetailPage = defineAsyncComponent(() => import('./pages/InstanceDetailPage.vue'))
const pages = ['instances', 'dashboard', 'inventory', 'tasks', 'settings'] as const
const locationHash = ref(location.hash)
const page = computed(() => {
  const id = locationHash.value.slice(1).split('?')[0]
  return pages.find(item => item === id) ?? 'instances'
})
const routeQuery = computed(() => new URLSearchParams(locationHash.value.split('?')[1]))
const runId = computed(() => routeQuery.value.get('run') ?? '')
const taskId = computed(() => runId.value ? '' : routeQuery.value.get('task') ?? '')
const automationInstanceId = computed(() => routeQuery.value.get('automation') ?? undefined)
const routeProject = computed(() => page.value !== 'settings' ? routeQuery.value.get('project') ?? '' : '')
const routeInstance = computed(() => page.value === 'instances' ? routeQuery.value.get('instance') ?? '' : '')
const instanceTab = computed(() => {
  const view = routeQuery.value.get('view')
  return view === 'history' || view === 'storage' ? view : 'current'
})
function selectInstanceTab(tab: string) {
  location.hash = 'instances' + query({ project: projectId.value, view: tab === 'current' ? undefined : tab })
}
const watchId = computed(() => routeQuery.value.get('watch') ?? '')
const watchSpec = computed(() => routeQuery.value.get('spec') ?? '')
const catalogFilter = computed(() => routeQuery.value.get('tab') === 'catalog' ? {
  gpuModel: routeQuery.value.get('gpu_model') ?? '', region: routeQuery.value.get('region') ?? '',
  zone: routeQuery.value.get('zone') ?? '', instanceType: routeQuery.value.get('instance_type') ?? '',
} : undefined)
const returnFocus = ref('')
const menuOpen = ref(false)
function locationChanged() {
  const previousInstance = routeInstance.value
  const previousProject = routeProject.value
  locationHash.value = location.hash
  menuOpen.value = false
  returnFocus.value = page.value === 'instances' && !routeInstance.value
    && (previousProject || projectId.value) === (routeProject.value || projectId.value) ? previousInstance : ''
}
function showRun(id: string) {
  location.hash = 'tasks' + query({
    run: id || undefined,
    project: id ? projectId.value : routeProject.value || undefined,
    automation: page.value === 'tasks' ? automationInstanceId.value : undefined,
  })
}
function showTask(id: string) {
  location.hash = 'tasks' + query({ task: id || undefined, project: projectId.value })
}

const session = ref<Session | null>(null)
const settings = ref<Settings>()
const operationBlock = computed(() => {
  if (!settings.value) return '正在确认管理台操作状态。'
  if (settings.value.restore_gate) return '恢复核对期间暂停云端操作。'
  return settings.value.read_only ? '只读模式下暂停云端操作。' : ''
})
let settingsRequest: AbortController | undefined
const projectId = ref(localStorage.getItem('gpu-console-project') ?? '')
const username = ref('')
const password = ref('')
const checkingSession = ref(true)
const loginPending = ref(false)
const loginError = ref('')
const settingsError = ref('')
const logoutPending = ref(false)
const logoutError = ref('')
const syncingProjects = ref(false)
const projectFeedback = ref('')
const projectSyncFailure = ref<{ message: string; clearAfter: number | null }>()
let projectSyncRequest: AbortController | undefined
const { state: projects, loading: projectsLoading, fault: projectsError, refresh: refreshProjects } =
  useSnapshot<Project>(computed(() => session.value?.authenticated ? '/projects' : null))
const { state: instances, loading: instancesLoading, fault: instancesError, refresh: refreshInstances } =
  useSnapshot<Instance>(computed(() => session.value?.authenticated && projectId.value
    ? '/instances' + query({ project_id: projectId.value }) : null))
const { state: locations } = useSnapshot<Location>(computed(() => session.value?.authenticated ? '/locations' : null), 60_000)
provide(locationKey, computed(() => locations.value.data))

const projectSyncError = computed(() => {
  const failure = projectSyncFailure.value
  if (!failure || failure.message === projectsError.value) return ''
  const successAt = projects.value.success_at
  if (successAt !== null && (failure.clearAfter === null || successAt > failure.clearAfter)) return ''
  return failure.message
})

const projectRouteState = computed(() => {
  if (!routeProject.value) return 'ready'
  if (projects.value.data.some(project => project.id === routeProject.value)) {
    return projectId.value === routeProject.value ? 'ready' : 'loading'
  }
  if (projects.value.success_at && !projectsError.value) return 'missing'
  return projectsError.value ? 'error' : 'loading'
})
const overviewFilters = new Map<string, { search: string; stateFilter: string }>()
const search = ref('')
const stateFilter = ref('')
watch(projectId, (id, previous) => {
  overviewFilters.set(previous, { search: search.value, stateFilter: stateFilter.value })
  const filters = overviewFilters.get(id)
  search.value = filters?.search ?? ''
  stateFilter.value = filters?.stateFilter ?? ''
})

function switchProject(selected: unknown) {
  if (typeof selected !== 'string' || !projects.value.data.some(project => project.id === selected)) return
  projectId.value = selected
  if (page.value === 'dashboard') {
    const params = new URLSearchParams(routeQuery.value)
    params.set('project', selected)
    if (params.get('source') === 'watch') params.delete('key')
    location.hash = 'dashboard?' + params.toString()
  } else if (page.value !== 'settings') location.hash = page.value + query({ project: selected })
}

watch([projects, settings, routeProject], ([{ data }, config, requested]) => {
  if (requested) {
    if (data.some(project => project.id === requested)) projectId.value = requested
    return
  }
  if (data.some(project => project.id === projectId.value)) return
  if (!data.length && !projects.value.success_at) return
  const preferred = data.find(project => project.id === config?.project_id)
  projectId.value = preferred?.id ?? data[0]?.id ?? ''
})
watch(projectId, id => localStorage.setItem('gpu-console-project', id))

async function loadSettings() {
  if (!session.value?.authenticated || settingsRequest) return
  settingsError.value = ''
  const controller = new AbortController()
  settingsRequest = controller
  try {
    const result = await request<Settings>('/settings', { signal: controller.signal })
    if (!controller.signal.aborted) settings.value = result
  } catch (error) {
    if (!controller.signal.aborted) settingsError.value = errorMessage(error)
  } finally {
    if (settingsRequest === controller) settingsRequest = undefined
  }
}

function settingsSaved(config: Settings) {
  settingsRequest?.abort()
  settingsRequest = undefined
  settings.value = config
  settingsError.value = ''
}

async function checkSession() {
  checkingSession.value = true
  loginError.value = ''
  try {
    const result = await request<Session>('/session')
    session.value = result
    updateSession(result)
    if (result.authenticated) void loadSettings()
  } catch (error) {
    loginError.value = errorMessage(error)
  } finally {
    checkingSession.value = false
  }
}

async function login() {
  if (loginPending.value || !username.value.trim() || !password.value) return
  loginError.value = ''
  loginPending.value = true
  try {
    const result = await request<Session>('/login', { method: 'POST', body: { username: username.value, password: password.value } })
    session.value = result
    updateSession(result)
    if (result.authenticated) void loadSettings()
  } catch (error) {
    loginError.value = errorMessage(error)
  } finally {
    loginPending.value = false
    password.value = ''
  }
}

async function logout() {
  if (logoutPending.value) return
  logoutPending.value = true
  logoutError.value = ''
  try {
    await request('/logout', { method: 'POST' })
    clearSession()
  } catch (error) {
    logoutError.value = errorMessage(error)
  } finally {
    logoutPending.value = false
  }
}

function clearSession() {
  const result = { initialized: true, authenticated: false }
  session.value = result
  updateSession(result)
  settingsRequest?.abort()
  settingsRequest = undefined
  settings.value = undefined
  menuOpen.value = false
  loginError.value = ''
  projectSyncRequest?.abort()
  projectSyncRequest = undefined
  syncingProjects.value = false
  projectFeedback.value = ''
  projectSyncFailure.value = undefined
}

function expireSession() {
  clearSession()
  loginError.value = '会话已过期，请重新登录。'
}

async function requestProjectSync() {
  if (syncingProjects.value) return
  const controller = new AbortController()
  projectSyncRequest = controller
  syncingProjects.value = true
  projectFeedback.value = ''
  projectSyncFailure.value = undefined
  try {
    const receipt = await queueCollection({ kind: 'projects' }, controller.signal, () => {
      projectFeedback.value = '项目同步已排队，等待后台读取。'
    })
    if (controller.signal.aborted) return
    const outcome = receipt.outcomes[0]
    if (receipt.state === 'failed') {
      projectFeedback.value = ''
      projectSyncFailure.value = {
        message: outcome?.error ?? '项目同步失败',
        clearAfter: outcome?.finished_at ?? projects.value.success_at,
      }
    } else {
      projectFeedback.value = '项目同步成功 · ' + timeLabel(outcome?.finished_at)
    }
    await refreshProjects()
  } catch (error) {
    if (!controller.signal.aborted) {
      projectFeedback.value = ''
      projectSyncFailure.value = { message: errorMessage(error), clearAfter: projects.value.success_at }
    }
  } finally {
    if (projectSyncRequest === controller) {
      projectSyncRequest = undefined
      syncingProjects.value = false
    }
  }
}

onMounted(() => {
  window.addEventListener('gpu-session-expired', expireSession)
  window.addEventListener('hashchange', locationChanged)
  void checkSession()
})
onUnmounted(() => {
  window.removeEventListener('gpu-session-expired', expireSession)
  window.removeEventListener('hashchange', locationChanged)
  settingsRequest?.abort()
  projectSyncRequest?.abort()
})
</script>

<template>
  <ElConfigProvider :locale="zhCn">
    <main v-if="!session?.authenticated" class="login-shell">
      <section class="login-panel">
        <div class="brand-mark" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i></div>
        <h1>GPU 实验管理台</h1>
        <ElAlert v-if="loginError" :title="loginError" type="error" show-icon :closable="false" />
        <ElSkeleton v-if="checkingSession" :rows="2" animated aria-label="正在检查会话" />
        <ElButton v-else-if="!session" @click="checkSession">重新连接</ElButton>
        <div v-else-if="!session.initialized" class="initialization">
          <p>管理员尚未初始化。请在宿主机终端运行 <code>docker compose exec web gpu-admin init</code>。</p>
          <ElButton @click="checkSession">检查初始化状态</ElButton>
        </div>
        <ElForm v-else label-position="top" @submit.prevent="login">
          <ElFormItem label="管理员账号" for="login-username">
            <ElInput id="login-username" v-model="username" autocomplete="username" required />
          </ElFormItem>
          <ElFormItem label="密码" for="login-password">
            <ElInput id="login-password" v-model="password" type="password" autocomplete="current-password" required />
          </ElFormItem>
          <ElButton type="primary" native-type="submit" :loading="loginPending" class="login-submit">登录</ElButton>
        </ElForm>
      </section>
    </main>
    <div v-else class="app-shell">
      <aside class="sidebar">
        <div class="brand">
          <div class="brand-mark" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i></div>
          <span>GPU 实验管理台</span>
        </div>
        <ConsoleNav :page="page" :pending="logoutPending" @logout="logout" />
      </aside>
      <div class="workspace">
        <header class="topbar">
          <ElButton class="mobile-menu" :icon="Menu" aria-label="打开管理台菜单"
            aria-haspopup="dialog" :aria-expanded="menuOpen" @click="menuOpen = true">菜单</ElButton>
          <div class="project-picker">
            <span>项目</span>
            <ElSelect aria-label="当前项目" :model-value="projectId" :loading="projectsLoading"
              :placeholder="projectsLoading ? '载入中…' : '选择项目'" :disabled="!projects.data.length"
              @change="switchProject">
              <ElOption v-for="project in projects.data" :key="project.id"
                :label="project.name || project.id" :value="project.id" />
            </ElSelect>
          </div>
          <span class="timezone">北京时间 · UTC+8</span>
        </header>
        <ElDrawer v-model="menuOpen" title="GPU 实验管理台" direction="ltr"
          size="min(320px, 100vw)" append-to-body destroy-on-close class="mobile-navigation">
          <ConsoleNav :page="page" :pending="logoutPending" @navigate="menuOpen = false" @logout="logout" />
        </ElDrawer>
        <main class="page">
          <ElAlert v-if="logoutError" :title="logoutError" type="error" show-icon :closable="false" />
          <ElAlert v-if="settingsError" :title="'无法读取管理台设置 · ' + settingsError" type="error" show-icon :closable="false">
            <ElButton size="small" @click="loadSettings">重试</ElButton>
          </ElAlert>
          <ElAlert v-if="projectsError" :title="'项目同步 · ' + projectsError" type="warning" show-icon :closable="false">
            <ElButton size="small" :loading="syncingProjects" @click="requestProjectSync">重新同步项目</ElButton>
          </ElAlert>
          <ElAlert v-if="projectSyncError" :title="'项目同步 · ' + projectSyncError" type="error" show-icon :closable="false">
            <ElButton v-if="!projectsError" size="small" :loading="syncingProjects" @click="requestProjectSync">重新同步项目</ElButton>
          </ElAlert>
          <p v-else-if="projectFeedback" role="status">{{ projectFeedback }}</p>
          <ElAlert v-if="settings?.read_only || settings?.restore_gate" type="warning" show-icon :closable="false"
            :title="settings.restore_gate ? '备份已恢复，云端操作正在等待核对。' : '服务处于只读同步模式。'">
            <a :href="settings.restore_gate ? '#settings?tab=status&section=recovery' : '#settings?tab=status'">
              {{ settings.restore_gate ? '查看后台状态和恢复核对流程' : '查看后台状态' }}
            </a>
          </ElAlert>
          <SettingsPage v-if="page === 'settings'" :settings="settings" :projects="projects.data" :route-query="routeQuery"
            @saved="settingsSaved" @reload="loadSettings" />
          <section v-else-if="projectRouteState !== 'ready'" class="panel">
            <h1>{{ page === 'tasks' ? '任务中心' : page === 'inventory' ? '库存' : page === 'dashboard' ? '数据看板' : routeInstance ? '实例详情' : '实例总览' }}</h1>
            <ElSkeleton v-if="projectRouteState === 'loading'" animated :rows="2" aria-label="正在核对链接中的项目" />
            <ElAlert v-else :closable="false" :type="projectRouteState === 'error' ? 'error' : 'warning'"
              :title="projectRouteState === 'error' ? '暂时无法核对链接中的项目。' : '此链接中的项目不在可用项目清单中。'" />
            <a :href="'#' + page">{{ page === 'tasks' ? '返回任务列表' : page === 'inventory' ? '返回库存' : page === 'dashboard' ? '返回数据看板' : '返回实例总览' }}</a>
          </section>
          <DashboardPage v-else-if="page === 'dashboard'" :project-id="projectId" :route-query="routeQuery" :settings="settings" />
          <InventoryPage v-else-if="page === 'inventory'" :project-id="projectId"
            :detail-key="routeQuery.get('item') ?? undefined" :spot-link="routeQuery.get('spot') === '1'"
            :watch-id="watchId" :watch-spec="watchSpec" :catalog-filter="catalogFilter" :show-watches="routeQuery.get('tab') === 'watch'" />
          <section v-else-if="!projectId" class="panel">
            <h1>{{ page === 'tasks' ? '任务中心' : '实例总览' }}</h1>
            <ElSkeleton v-if="projectsLoading" animated :rows="2" aria-label="正在读取项目" />
            <ElEmpty v-else description="暂无可用项目。平台凭据就绪后可请求同步。">
              <ElButton :loading="syncingProjects" @click="requestProjectSync">同步项目</ElButton>
            </ElEmpty>
          </section>
          <TaskPage v-else-if="page === 'tasks'" :key="projectId" :project-id="projectId" :selected-run-id="runId"
            :selected-task-id="taskId" :automation-instance-id="automationInstanceId" :instances="instances.data"
            :channels="settings?.channels" @select-run="showRun" @select-task="showTask" />
          <InstanceDetailPage v-else-if="routeInstance" :key="projectId + ':' + routeInstance" :project-id="projectId" :instance-id="routeInstance"
            :snapshot="instances" :loading="instancesLoading" :sample-interval="settings?.collection_seconds"
            :error="instancesError" :operation-block="operationBlock" @refresh="refreshInstances" @show-run="showRun" />
          <InstancePage v-else :key="projectId" v-model:search="search" v-model:state-filter="stateFilter"
            :project-id="projectId" :snapshot="instances" :loading="instancesLoading" :sample-interval="settings?.collection_seconds"
            :project-name="projects.data.find(item => item.id === projectId)?.name" :operation-block="operationBlock" :channels="settings?.channels"
            :error="instancesError" :focus-instance-id="returnFocus" :tab="instanceTab"
            @refresh="refreshInstances" @show-run="showRun" @select-tab="selectInstanceTab">
            <DashboardSignals :project-id="projectId" @select-run="showRun" />
          </InstancePage>
        </main>
      </div>
    </div>
  </ElConfigProvider>
</template>
