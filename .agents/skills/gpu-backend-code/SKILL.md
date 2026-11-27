---
name: gpu-backend-code
description: >
  实现、重构、调试或审查 Python 3.12、FastAPI、后台执行器、SQLite 和官方 SDK 适配代码时使用。
  Use when changing Python implementation, API input validation, database operations, error mapping or a CompShare SDK adapter.
  Do NOT use for architecture-only proposals, Vue-only edits or live cloud account operations.
allowed-tools: Read, Glob, Grep, Bash, Write, Edit
---

# Python 与 FastAPI 实现

## 改动顺序

1. 从路由、后台任务或 CLI 的实际入口找到调用方、边界模型与相邻测试。保持可观察的兼容行为，先定位导致回归的输入或状态。
2. 用 `uv` 管理 Python 3.12 项目。增加运行依赖使用 `uv add`，测试依赖使用 `uv add --dev`，同时审查清单和锁文件差异。`web` 与 `worker` 使用同一 Python 包和依赖集合。
3. 对 HTTP 和 SDK 等外部边界定义输入类型、校验单位与可选字段。Python 变量与函数用 snake_case，类名用 PascalCase。平台字段的大小写仅留在适配层。为公共接口和非显然的返回值补类型注解。
4. 把阻塞 SDK 调用放到 worker 或既有的受控执行上下文。SQLite 更新使用短事务和参数化查询。事务结束后再等待网络，确保失败或取消时资源正确释放。
5. 在最接近外部边界处区分业务拒绝、网络失败和结果待确认。记录操作名、运行编号、错误码和云端请求标识。将应用允许的错误反馈返回上层，避免记录整份请求、平台响应或凭据。
6. 对会产生费用或更改云端状态的调用，在客户端配置层设置 `max_retries=0`。使用官方 SDK 的 `client.ucompshare().invoke(action, params)` 让适配层显式控制请求字段和响应投影。

## 代码检查示例

```python
def memory_gb_to_mb(memory_gb: int) -> int:
    if memory_gb <= 0:
        raise ValueError("内存必须大于零")
    return memory_gb * 1024
```

目录规格使用 GB，创建和询价接口需要 MB 时在适配边界转换一次。测试正常值、零值和重复转换风险。

## 验证与交付

按 [gpu-change-verification](../gpu-change-verification/SKILL.md) 从受影响的入口选择 pytest 用例或文件。涉及供应商字段或 SDK 升级时，补做模拟传输检查。报告受影响的入口、错误及重试行为、执行过的验证与仍需真实环境确认的部分。

## Gotchas

- 对收费请求不使用裸 `except` 后重试。超时不能证明购买失败。
- `Quantity`、`Remark` 和 `MaxCount` 等字段要在实际发送的数据中核对。单测若只观察传给应用包装函数的参数，会遗漏 SDK 序列化产生的差异。
- 不在模块导入时读取生产密钥、连接云端、启动 worker 或执行数据库迁移。让测试能注入虚拟 transport。
