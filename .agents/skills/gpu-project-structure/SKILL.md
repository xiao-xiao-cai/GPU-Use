---
name: gpu-project-structure
description: >
  规划项目目录、决定新文件归属、重组跨层依赖或迁移已有模块时使用。
  Use when placing files, reviewing module boundaries or moving code across the Vue frontend, Python package, tests and deployment configuration.
  Do NOT use for a local algorithm edit, a visual-only change or a request to operate a GPU instance.
allowed-tools: Read, Glob, Grep, Bash, Write, Edit
---

# 项目结构与依赖方向

## 先确认现状

用 `rg --files --hidden -g '!.git' -g '!secrets' -g '!.venv' -g '!node_modules'` 找到清单、入口、调用方与测试。列出本次要交付的文件和它们各自的所有者。已有模块能承担该职责时，直接在该模块内修改。

## 新文件归属

| 新增内容 | 首次落地的位置与职责 |
| --- | --- |
| Python 项目、依赖与工具版本 | 根目录的 `pyproject.toml`、`uv.lock`、`.python-version`。`web` 与 `worker` 共用一个可安装的 Python 包和同一把锁 |
| HTTP 入口与外部输入模型 | `src/gpu_console/api/`，校验 HTTP 输入、调用应用层、返回面向客户端的结果 |
| 后台入口与调度 | `src/gpu_console/worker/`，获取持久任务、驱动执行、记录进度 |
| 跨入口的用例 | 当 API 与 worker 都需要同一段业务编排时，归入 `src/gpu_console/application/`。两个入口分别调用它 |
| 业务规则与持久化 | 可复用的纯规则放 `src/gpu_console/domain/`，SQLite 读写和事务放 `src/gpu_console/storage/`。首次有相应实现时建立 |
| 外部系统 | `src/gpu_console/adapters/`，隔离云 SDK 和通知协议。公开应用所需的最小接口 |
| Vue 应用 | `frontend/` 独立维护 npm 清单和锁。`src/pages/` 组织页面，`src/components/` 放展示组件，`src/composables/` 组织可复用交互逻辑，`src/api/` 管理 HTTP 边界 |
| 测试 | Python 测试归根目录 `tests/`，Vue 组件测试与组件相邻 |
| 部署、配置与运行数据 | `Dockerfile`、Compose 文件、`.editorconfig`、`.gitattributes` 放根目录。SQLite 运行数据归 `data/`，本机凭据归 `secrets/` |
| 开发规则与方案 | 根目录 `AGENTS.md` 指向 `.agents/skills/<技能名>/SKILL.md`。技能自己的脚本和参考资料随技能放置。平台架构和交付里程碑见根目录的《GPU管理平台实施方案.md》 |

表中的新建位置用于没有既有入口的情况。按实际功能建立目录，每个新目录都应有实际使用者。根据代码所有者判断归属，不用 `utils/` 或 `common/` 收集无关代码。

例如新增定时关机时，Vue 页面提交实例和预约时间，HTTP 入口校验后保存任务并返回运行编号。worker 通过云端适配器登记预约并回读时间，确认后由平台到点关机。UCloud SDK 的字段映射归适配器。验证时分别走表单提交和后台登记入口。

## 依赖与迁移

1. 页面通过前端 API 边界消费后端公开契约。浏览器代码不依赖 Python 模块、SDK 凭据或 SQLite 文件。
2. HTTP 与 worker 可共用业务规则。纯规则的导入不应启动调度器、打开数据库或连接网络。持久化与外部 IO 的边界要能用测试替身替换。
3. 移动文件前用 `rg -n` 找出导入、启动命令、包入口、测试发现规则、构建路径和资源引用。保持已有调用兼容，或在同一次独立变更里同步迁移调用方。
4. 对照根目录 `.editorconfig` 与 `.gitattributes` 放置文本和二进制资源。Python 用 4 空格，Web 和配置文本用 2 空格，Makefile 配方保留制表符。修改已有文件时只格式化本次相关内容。

## 交付与验收

列出“原位置 → 新位置 → 负责的行为”，说明受影响的入口和验证命令。用实际 Python 导入、测试发现或前端构建验证移动后的入口。只有目录规划时，交付目标目录表和依赖方向。

变更文件格式规则时，用 `git check-attr text eol -- AGENTS.md` 查看文本属性，用 `git ls-files --eol` 核对实际索引与工作树行尾。选一份 Python、一份 Vue 或配置文件及一份二进制资源核对规则匹配。保留行为变更以外文件的原有差异。

## Gotchas

- 根目录 Python 清单与 `frontend/` 的 npm 清单各有自己的锁文件。不要为了统一目录外观重复创建 Python 项目。
- Docker 中的工作目录、静态资源路径和 CLI 入口也是调用方，重命名时一起检查。
- 目录创建与功能交付同步。不得为了补齐示意结构创建空目录、占位包或没有调用方的抽象。
