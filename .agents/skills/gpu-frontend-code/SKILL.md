---
name: gpu-frontend-code
description: >
  实现、调试或审查 Vue 3、TypeScript、组件事件、跨模块数据流、异步结果回读及 ECharts 组件时使用。
  Use when tracing callers or editing Vue components, API consumers, asynchronous state transitions or chart lifecycles in the frontend.
  Do NOT use for a visual proposal without code changes, Python implementation or live GPU operations.
allowed-tools: Read, Glob, Grep, Bash, Write, Edit
---

# Vue 与 TypeScript 实现

## 变更流程

1. 从用户操作定位页面、组件及前端 API 客户端，阅读相邻测试和 `package.json` 的已配置脚本。依赖跟随该目录的 npm 锁文件。
2. 按现有 Vue 3 风格实现。新单文件组件优先 `<script setup lang="ts">`。公开的 props、emits 和服务端响应有明确类型。来自网络的未知值在边界校验后使用。
3. 通过事件通知父级修改由父级持有的状态。可从已有值计算的展示状态用 `computed`。只有复用逻辑或清晰的副作用需要独立生命周期时才提取 composable。
4. 筛选变化和异步刷新使用取消信号或请求序号。组件卸载时注销监听、定时器与观察器。最新请求只可以更新对应项目和实例的视图。
5. ECharts 在元素挂载后初始化，复用实例更新序列。在元素尺寸变化时调整大小，卸载时调用 `dispose()`。使用稳定的序列标识保留图例与缩放选择。
6. 将错误映射成可操作的页面反馈。保留服务端确认状态与本地提交状态的区别。用户可访问的交互元素使用语义化控件和可理解的标签。

## 从改动点核对上下游

- 先查现有的构造函数、请求客户端和消费同一类型的页面，再决定修改哪一层。例如用 `rg -n 'TaskDraft|previewTask|saveTask|/tasks/preview' frontend/src src/gpu_console` 定位草稿的构造、发送和回读。修改字段时核对创建、编辑、预览及已有数据的读取。涉及服务端实现时衔接 `gpu-backend-code`。
- 替换 UI 控件时核实事件值、空值和更新时机，复用原有的状态写入与查询订阅。Element Plus 的 props、emits、表单与焦点行为按 `gpu-element-plus` 核对。
- 预览与保存共享 `draft()` 的序列化规则。提交预览时记录请求序号和草稿内容。草稿、项目或编辑对象变化时使旧预览失效。成功、失败及 finally 分支只更新仍属当前请求的状态。保存入口再次比较当前提交内容与已确认预览，并复用提交中标记防止重复写入。

从同一编辑入口复测三个结果。预算 A 预览成功后可以保存，预览失败后草稿仍可重试，改成预算 B 后迟到的 A 响应无法恢复保存按钮或覆盖 B 的反馈。模板契约变动时运行 `uv run --locked pytest tests/test_task_templates.py`，核对已排队运行的快照、新运行的模板版本和计费方式变化后的预算确认。

## 边界示例

```vue
<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ utilization: number | null }>()
const emit = defineEmits<{ refresh: [] }>()
const utilizationText = computed(() =>
  props.utilization === null ? '暂无采样' : String(props.utilization) + '%'
)
</script>

<template>
  <span>{{ utilizationText }}</span>
  <button type="button" @click="emit('refresh')">刷新</button>
</template>
```

样本值为 `0` 时必须正常显示。专门判断缺失值可以保留有效的零。

## 验证与交付

查看前端 `package.json` 的现有脚本，按 [gpu-change-verification](../gpu-change-verification/SKILL.md) 选择受影响的页面验收、组件测试、类型检查或构建。涉及图表或异步刷新时验证旧请求迟到、页面卸载与有效零值。交付包含改动位置、可见行为、验证证据及未覆盖的环境。

## Gotchas

- 组件名用 PascalCase，组合式函数用 `use` 前缀，事件与 props 的命名保持相邻代码的一致性。
- 页面组件只通过本地服务的前端 API 边界取数据。浏览器包、持久化状态和异常日志中都不应出现平台私钥。
- 不要用全应用状态代替单个弹窗的临时值，不要通过深度监听整张表来驱动每一帧重绘。
