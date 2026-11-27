---
name: gpu-git-workflow
description: >
  在本项目拆分独立变更、管理短期分支、审查暂存区、自动本地提交、合并或准备版本发布时使用。
  Use when preparing a focused branch, commit, merge or versioned release and verifying exactly which paths enter history.
  Do NOT use for a read-only question about application state or for authorizing cloud actions.
allowed-tools: Read, Glob, Grep, Bash
---

# Git 分支、提交与版本管理

## 检查起点

在目标目录运行 `git rev-parse --show-toplevel`、`git status --short --branch` 和 `git diff --cached --name-status`。确认仓库边界、当前分支、用户已有改动和暂存内容。在明确授权初始化的新项目使用 `git init -b main`。已有仓库沿用原有历史。

## 本地提交

1. 已有主干的项目为本次改动使用短期分支，例如 `feat/instance-chart`、`fix/sdk-retry` 或 `docs/project-rules`。工作已在适合的分支上时继续使用。初始基线可提交到 `main`。
2. 按独立行为组织提交，相关测试、锁文件和必要文档随对应变更提交。先运行受影响的验证，并审阅 `git diff --check` 与本次差异。
3. 按文件或独立片段暂存。检查 `git diff --cached --check`、`git diff --cached --stat` 及具体差异。真实凭据、数据库、构建缓存和移交 bundle 均不进入提交。
4. 验证通过后自动本地提交。主题使用 `type(scope): 中文动作说明`，范围可省略。例如 `fix(worker): 避免回执超时后重复创建实例`。不要把重构与无关格式变动混入同一提交。
5. 用 `git show --stat --oneline HEAD` 和 `git status --short --branch` 核对结果。报告提交短哈希、分支、验证结果和仍保留的工作区变动。

若已有无关文件被暂存，避免整批提交。独立文件已审阅全部工作区差异时可使用只提交指定路径的方式。同一文件混有他人暂存或未暂存修改时，先隔离本次片段，不能安全隔离则保留现场并说明。

## 合并与发布

合并前核对目标分支和变更范围。公布版本时按兼容修复、新增兼容能力、破坏兼容变更分别考虑 patch、minor、major。只有任务授权发布时才建立 `vMAJOR.MINOR.PATCH` 标签。远端推送、开 PR 和改写共享历史按各自授权执行。

## Gotchas

- 仓库外、只读挂载、未配置提交身份或未解决的冲突，都要报告实际状态。不要把一份暂存补丁称为目标工作树上的提交。
- 不设置全局 Git 身份、不清理他人文件、不以全仓库 reset/checkout 掩盖失败。需要隔离改动时保留原工作树。
- 新建仓库时先配置秘密目录的忽略规则，再审查暂存文件清单。用 `git check-ignore` 验证凭据路径，避免输出文件内容。
