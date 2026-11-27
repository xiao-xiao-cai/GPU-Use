---
name: gpu-element-plus
description: >
  为 GPU 控制台选择、接入或调试 Element Plus 的组件、事件、表单、主题和中文日期控件时使用。
  Use when integrating Element Plus into this Vue 3 console or mapping a control to its model, events and feedback.
  Do NOT use for general Vue data flow, ECharts rendering, backend contracts or local Docker deployment.
allowed-tools: Read, Glob, Grep, Bash, Write, Edit
---

# Element Plus 组件使用

## 先核对版本和接入方式

1. 读取前端清单、npm 锁文件、应用入口和邻近组件。在已有安装的工作树可运行 `npm --prefix frontend ls element-plus @element-plus/icons-vue --depth=0` 核对解析版本。遇到陌生的 props、emits、slots 或已弃用属性，查[官方组件页](https://element-plus.org/en-US/component/overview.html)并对照安装版本的类型声明。官网示例依赖较新版本时，选用当前版本支持的 API。
2. 首次接入且任务包含依赖变更时，执行 `npm --prefix frontend install element-plus @element-plus/icons-vue`，一并审阅清单和锁文件。已有接入沿用项目的按需加载方式。手动导入时同时导入使用的组件及其 CSS，图标按名称导入。页签中的 ECharts 保持页面级异步加载。
3. 在应用根部一次配置 `ElConfigProvider` 和 `element-plus/es/locale/lang/zh-cn`。主色、悬停、禁用、边框和焦点使用 `--el-*` 主题变量，品牌色的派生色一起核对。弹层可被 Teleport 到 body，主题变量应覆盖弹层作用域。收窄旧全局控件选择器，实测输入框、下拉面板及键盘焦点。[主题配置](https://element-plus.org/en-US/guide/theming.html)

## 按场景选组件

| 场景 | 组件与接线规则 |
| --- | --- |
| 导航、筛选与表格 | `ElMenu`、`ElTabs` 管理入口与视图，深链接保留真实 href。用 `ElInput`、`ElSelect` 绑定页面筛选值，`ElTable` 使用对象稳定 ID 作为 row-key。先筛选再分页，保留已有的服务端或客户端分页边界 |
| 分步创建与编辑 | `ElSteps` 显示当前步骤，草稿由编辑页持有。`ElFormItem` 的 prop 对应 model 字段，通过本地校验后请求服务端预览。预算精度、空值、模板版本与现有 API 契约一致 |
| 查看详情与确认操作 | `ElDrawer` 用来查看行详情，`ElDialog` 承载需确认的对象、影响和提交结果。在 v-model 关闭或组件卸载时撤销请求、清空敏感值，关闭动画结束后核对焦点回到触发控件 |
| 异步反馈 | 首载用 `ElSkeleton`，成功且数据为空用 `ElEmpty`，持续错误或旧快照用 `ElAlert` 配时间和重试入口。复制成功可用 `ElMessage`，后台运行结果要能按运行编号回查 |

## 项目选择器

将选择值交还应用层，由应用层更新项目、hash 和已有查询订阅。使用组件时把原先读取 `event.target.value` 的原生 select 处理器改为接收项目 ID。一次选择只触发一处状态写入。

```vue
<script setup lang="ts">
import { ElOption, ElSelect } from 'element-plus'
import 'element-plus/es/components/option/style/css'
import 'element-plus/es/components/select/style/css'

const props = defineProps<{ projectId: string; projects: { id: string; name?: string }[] }>()
const emit = defineEmits<{ selectProject: [id: string] }>()

function selectProject(value: unknown) {
  if (typeof value === 'string' && props.projects.some(project => project.id === value)) {
    emit('selectProject', value)
  }
}
</script>

<template>
  <ElSelect aria-label="当前项目" :model-value="projectId" @change="selectProject">
    <ElOption v-for="project in projects" :key="project.id"
      :label="project.name || project.id" :value="project.id" />
  </ElSelect>
</template>
```

## 北京时间输入与表单提交

下面的预约输入组件在 `src/components/` 使用。控件保存北京时间的年月日时分，提交边界复用 `unixTime` 转 Unix 秒，编辑既有值用 `localInput` 反向填充。中文语言包负责显示文案。验收时把浏览器设为其他时区，核对输入、请求秒值与回执显示的是同一预约时间。

```vue
<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElButton, ElDatePicker, ElForm, ElFormItem, type FormInstance } from 'element-plus'
import 'element-plus/es/components/button/style/css'
import 'element-plus/es/components/date-picker/style/css'
import 'element-plus/es/components/form/style/css'
import 'element-plus/es/components/form-item/style/css'
import { unixTime } from '../display'

const props = defineProps<{ pending: boolean }>()
const emit = defineEmits<{ preview: [scheduledAt: number] }>()
const form = ref<FormInstance>()
const fields = reactive({ at: '' })
const validating = ref(false)

async function requestPreview() {
  if (!form.value || validating.value || props.pending) return
  validating.value = true
  try {
    if (await form.value.validate().catch(() => false)) emit('preview', unixTime(fields.at))
  } finally {
    validating.value = false
  }
}
</script>

<template>
  <ElForm ref="form" :model="fields" @submit.prevent="requestPreview">
    <ElFormItem label="预约时间（北京时间）" prop="at"
      :rules="[{ required: true, message: '请选择预约时间', trigger: 'change' }]">
      <ElDatePicker v-model="fields.at" type="datetime" format="YYYY-MM-DD HH:mm"
        value-format="YYYY-MM-DD[T]HH:mm" />
    </ElFormItem>
    <ElButton native-type="submit" type="primary" :loading="pending || validating">预览预约</ElButton>
  </ElForm>
</template>
```

## 验证与 Gotchas

- 从实际表单或列表验证鼠标和键盘行为。表单只保留一个提交入口，按钮的 `type` 控制外观，`native-type` 控制原生提交行为。服务端校验错误要保留草稿并定位相应字段。
- 切换项目或修改草稿后，只接纳仍属于当前项目、草稿和请求的回执。抽屉打开时测量图表可见尺寸，关闭按钮、Esc、程序切换路由都应释放临时状态。
- 交付所选组件、绑定字段、发出事件、消费函数、成功与失败表现及版本验证结果。组件选型交给本技能，业务状态和 API 回读衔接 `gpu-frontend-code`。
