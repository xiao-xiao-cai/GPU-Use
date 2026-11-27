---
name: gpu-change-verification
description: >
  根据本次提交的改动范围分级选测，复现回归，验证跨层契约或离线核对 SDK 字段和重试行为。
  Use when selecting targeted checks for a change, reproducing a regression, or verifying a shared contract or SDK upgrade.
  Do NOT use for routine inventory polling, real cloud provisioning or a design discussion with no verification request.
allowed-tools: Read, Glob, Grep, Bash, Write, Edit
---

# 改动验证与回归检查

## 按提交范围分级验证

| 级别 | 适用改动 | 选测方式 |
| --- | --- | --- |
| L0 规则与文档 | 文档、技能、规则或格式配置 | 检查元数据、引用、命令和示例。涉及格式配置时核对实际 Git 属性，涉及可执行脚本时运行对应的模拟测试。 |
| L1 单模块 | 局部算法、格式化、单个页面或组件 | 后端指定 pytest 用例或文件。前端使用已有组件测试和受影响页面的验收操作，涉及类型变化时运行 `npm --prefix frontend run typecheck`。 |
| L2 跨模块与共享边界 | API 契约、数据库迁移、worker 状态、SDK 或共享组件 | 沿调用链合并受影响的定向测试。迁移覆盖旧库升级，任务状态覆盖失败和重试，SDK 核对实际模拟传输。涉及前端打包、依赖、入口或构建配置时运行 `npm --prefix frontend run build`。 |

1. 查看本次提交的差异及实际调用方，从业务入口找能验证改动的现有用例。修复回归时先找能区分修复前后行为的输入。合并多个独立改动各自需要的用例，新增测试聚焦已有用例遗漏的具体风险。
2. pytest 命令带明确的用例或文件路径。失败时先定位原因，再沿发现的影响扩大到相邻用例。全量回归仅在用户明确要求时执行。
3. 复用已就绪的环境。Python 清单或锁文件变动时运行 `uv lock --check`，前端依赖清单或锁文件变动时核对两者一致。需要准备或更新环境时，按项目锁文件执行 `uv sync --locked` 或 `npm --prefix frontend ci`。

- 只改成本汇总的费率更新逻辑时，运行 `uv run --locked pytest tests/test_dashboard_cost.py::test_rate_change_rebuilds_only_affected_days_and_retains_newer_revision`。成本接口字段影响 Web 和 MCP 时，选 `tests/test_dashboard_cost.py::test_cost_and_rental_web_mcp_match_and_require_project_scope`，页面消费该字段时再从对应页面复测。
- 按本次风险选边界用例。前端异步状态关注有效零值、缺失数据、迟到响应和卸载。后端任务或持久化关注重复提交、部分失败、重启和旧数据。SDK 关注请求字段、响应过滤和失败后的发送次数。

## 离线 SDK 校验

从仓库根目录使用 [校验脚本](scripts/check_sdk_contract.py)。它创建带虚拟凭据的官方客户端，注入内存 transport，并阻断测试进程的网络连接。脚本不会打开 `secrets/`，也没有真实账号或端点参数。

Python 项目已有锁文件时，检查仓库实际锁定的 SDK。

```bash
uv run --locked python .agents/skills/gpu-change-verification/scripts/check_sdk_contract.py --json
```

技能先于应用落地时，使用独立环境。下例要求 Python 3.12、SDK 及其依赖已缓存。首次准备依赖是独立的包安装步骤。

```bash
uv run --no-project --offline --python 3.12 --with ucloud-sdk-python3==0.11.161 python .agents/skills/gpu-change-verification/scripts/check_sdk_contract.py --expect-sdk 0.11.161
```

脚本退出码 0 表示字段和发送次数符合预期，1 表示行为检查失败，2 表示依赖或指定的版本不满足。`--help` 使用 Python 标准库即可查看。升级 SDK 时校验新版本，核对改动字段后再更新锁文件与基线。

## 交付格式

列出改变的行为、选测范围及原因、实际执行的命令、观测到的结果和仍待验证的风险。外部真实联调已有明确授权时遵照目标范围执行。分别说明模拟结果、平台真实回执和通知实际送达情况。

## Gotchas

- 只断言 Python 函数入参不足以证明 SDK 最终发出的数据正确。检查 transport 收到的请求与 SDK 返回给调用方的数据。
- 单个 HTTP 成功响应只能证明当前调用被接受。任务是否达成目标要用其终态验收条件判断。
- 文件还没创建、依赖缺失、无浏览器或无可用云账号时，明确列出哪些步骤已经验证。
