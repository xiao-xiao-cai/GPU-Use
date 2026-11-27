---
name: gpu-deployment
description: >
  用于 GPU 实验管理台的 GitHub Actions 镜像发布、GHCR 隔离验收、生产部署、本机清理和回退。
  发布镜像统一在 GitHub 构建，生产通过本机代理按摘要拉取，并验证 web、mcp 和 worker。
  Use when publishing a GPU console image via GitHub Actions, validating or deploying a GHCR digest, or rolling back a release.
  Keep release history in GHCR; after acceptance, keep only the running project image and one pre-upgrade database backup locally.
  Do NOT use for ordinary source tests, database disaster recovery or cloud GPU instance operations.
allowed-tools: Read, Glob, Grep, Bash
---

# 管理台镜像发布与部署

## 何时部署

- 影响管理台运行且已验收的功能更新，在发布和部署获授权后，同轮完成镜像发布、隔离验收、备份和正式切换。仅更新规则或技能文档时不发布镜像、不部署。用户指定了交付范围或上线时间时，按其要求执行。

## GitHub 构建与发布

- 以[发布工作流](../../../.github/workflows/ci.yml)为准。推送到 `main` 或发往 `main` 的 PR 运行验证；授权发布时推送与 `pyproject.toml` 版本匹配、且指向 `main` 历史的 `vMAJOR.MINOR.PATCH` 标签。标签未触发发布时，可用 `gh workflow run ci.yml --ref main -f release_tag="$release_tag"` 触发，`release_tag` 为已推送且尚未成功发布的版本标签。已发布的版本不移标签、不覆盖镜像；有变更则发布新版本。
- GitHub 托管 runner 构建 `linux/amd64` 镜像，`web`、`mcp`、`worker` 通过隔离健康检查后推送到 `ghcr.io/xiao-xiao-cai/gpu-use:<版本>`。`gpu-console:test-…` 只是隔离测试标签，不是发布包名。记录 Actions 摘要中的 `Version`、`Revision` 和 `Deployment image`（`@sha256:`）；生产和回退都使用这个摘要引用，不使用 `latest` 或单独的版本标签。
- GHCR 保留历史发布镜像，本机清理不删除远端包、版本或标签。若发布报 `permission_denied: write_package`，核对 GHCR 包是否已关联当前仓库，以及 Actions 的写权限；同名仓库重建后不能假定沿用旧授权。不通过改包名、删除旧包或给 CI 写入长期令牌绕过权限问题。GitHub 不连接生产主机，也不保存生产凭据。

## Docker 出网代理

- GitHub 构建使用工作流配置的代理。生产机的 Docker 仓库鉴权、元数据和层下载都走宿主机代理。使用部署实际连接的 Docker daemon 核对 HTTP/HTTPS 代理和 `NO_PROXY`；代理例外仅允许本机或内网，不能把 GHCR 及其下载端点排除。核对 CLI 鉴权也走代理。仅在包不公开时使用具 `read:packages` 权限的凭据，避免输出令牌、代理凭据或完整环境。
- 代理不可用、鉴权失败或宿主机负载异常时，保持当前正式版本；先定位并修复，再重试。禁止绕过代理、停止共享 daemon 或清理其他项目。实际代理、主机规格和运维细节留在忽略的 `.env` 或 `private-ops/`。

## 准备部署镜像

- 拉取、隔离验收、切换和清理按项目串行，在同一本机 Docker socket 上操作。拉取前检查宿主机负载和 Docker 数据所在文件系统的可用空间，避开其他高负载操作；负载或磁盘异常时暂停本轮。
- 升级前记录当前 `GPU_IMAGE` 摘要、Git 修订、Compose 版本和三个容器的实际镜像 ID，确认旧版摘要仍可从 GHCR 拉取。若当前配置为旧式本地标签，先核实其对应的已发布摘要；无法核实回退来源时暂停，不猜摘要。
- 在发布记录对应的检出目录核对 Compose 服务和健康检查。在下例中预先设置 `deployment_image`、`target_version`、`revision`，值来自 Actions 摘要。生产只拉取，不在生产机或开发机构建、导入用于正式部署的镜像。

```bash
(
  set -euo pipefail
  : "${deployment_image:?请提供发布摘要引用}" "${target_version:?请提供版本}" "${revision:?请提供 Git 修订}"
  [[ $deployment_image =~ ^ghcr\.io/xiao-xiao-cai/gpu-use@sha256:[0-9a-f]{64}$ ]]
  [[ $(git rev-parse HEAD) == "$revision" ]]
  docker pull --platform linux/amd64 "$deployment_image"
  image_record=$(docker image inspect --format '{{.Os}}/{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "org.opencontainers.image.revision"}}' "$deployment_image")
  [[ $image_record == "linux/amd64 $target_version $revision" ]]
  docker image inspect --format '{{.Id}}' "$deployment_image"
)
```

记录本机解析出的完整镜像 ID，并确认平台适配宿主机。GHCR 摘要标识仓库中的镜像，Docker 镜像 ID 用于核对实际容器，两者不能混用。拉取后 OCI 校验失败时，不进入隔离验收；按本轮摘要和 ID 核对并清理闲置候选。

## 隔离测试与自动收尾

使用 Bash、Python 3、Docker CLI 和支持 `!override` 的 Compose。复用产品的服务与健康检查，把[测试覆盖文件](assets/test.compose.yaml)作为第二个 Compose 文件。它替换各服务的数据和凭据挂载，`web` 和 `mcp` 的测试端口仅发布到回环地址，`worker` 使用内部网络，三个服务均启用只读模式。MCP 测试端口默认 `18089`，已占用时可用 `GPU_TEST_MCP_PORT` 指定空闲端口。临时数据目录须允许容器 UID 10001 写入，凭据目录使用空目录或专用虚拟值。

在已验证的 Git 检出目录、与拉取相同的 Docker socket 上运行。将已拉取的摘要引用和完整 ID 分别设置为 `GPU_TEST_IMAGE` 和 `GPU_TEST_IMAGE_ID`。测试直接使用该摘要，不再创建本地镜像标签。使用能设置临时目录属主和清理测试数据的账号；启动前检查展开后的挂载、端口和镜像，确保不引用生产数据、凭据或网络。

```bash
(
  set -euo pipefail
  skill_dir=.agents/skills/gpu-deployment
  : "${GPU_TEST_IMAGE:?请提供本轮摘要引用}" "${GPU_TEST_IMAGE_ID:?请提供拉取时记录的完整镜像 ID}"
  [[ $GPU_TEST_IMAGE =~ ^ghcr\.io/xiao-xiao-cai/gpu-use@sha256:[0-9a-f]{64}$ ]]
  [[ $GPU_TEST_IMAGE_ID =~ ^sha256:[0-9a-f]{64}$ ]]
  [[ $(docker image inspect --format '{{.Id}}' "$GPU_TEST_IMAGE") == "$GPU_TEST_IMAGE_ID" ]]
  run_id=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
  export GPU_TEST_IMAGE
  export GPU_TEST_DIR
  GPU_TEST_DIR=$(mktemp -d)
  test_project="gpu-console-test-$run_id"
  compose=(docker compose --env-file /dev/null -p "$test_project"
           -f compose.yaml -f "$skill_dir/assets/test.compose.yaml")
  started=0

  finish() {
    local test_status=$?
    trap - EXIT
    trap '' HUP INT TERM
    if (( started )); then "${compose[@]}" down || exit 1; fi
    rm -rf -- "$GPU_TEST_DIR" || exit 1
    exit "$test_status"
  }
  trap finish EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  printf '测试项目 %s，目录 %s，镜像 %s（%s）\n' "$test_project" "$GPU_TEST_DIR" "$GPU_TEST_IMAGE" "$GPU_TEST_IMAGE_ID"
  install -d -m 0700 -o 10001 -g 10001 "$GPU_TEST_DIR/data" "$GPU_TEST_DIR/secrets" "$GPU_TEST_DIR/secrets/feishu-settings"
  "${compose[@]}" config --quiet
  started=1
  "${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 120
  for service in web mcp worker; do
    container=$("${compose[@]}" ps -q "$service")
    [[ $(docker container inspect --format '{{.Image}} {{.State.Health.Status}}' "$container") == "$GPU_TEST_IMAGE_ID healthy" ]]
  done
  for service in web mcp; do
    "${compose[@]}" exec -T "$service" python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3).read().decode())"
  done
  "${compose[@]}" exec -T worker gpu-admin worker-health
  # 在此运行本次改动的针对性检查，任一失败进入 finish。
)
```

涉及 MCP 或入口变更时，从测试回环地址用虚拟 Key 完成 Streamable HTTP 握手、工具发现和一次只读调用。核对独立入口拒绝无 Key 的请求，同时确认网页端口的 `/mcp` 返回 `404`。涉及数据库时，用旧版结构的隔离数据核对升级和读回，并验证旧程序能否读取升级后的库。涉及 Key 迁移时，同时验证迁移前后访问与撤销行为。回环测试不能替代正式入口联调。

- 收尾时移除本轮测试容器、网络和数据目录；强制终止后使用记录的测试项目名和数据目录补做收尾。将部署的候选通过验收后保留至正式切换；仅做测试、验收失败或取消部署时，按本轮记录的摘要和 ID 清理不再使用的候选，不能删除正式服务仍引用的镜像。
- 若仍有本轮创建的测试标签，使用[定向清理脚本](scripts/cleanup_test_image.py)，传入完整测试标签和完整镜像 ID。该脚本只清理测试标签，不用于删除 GHCR 摘要引用。

## 正式升级的数据库备份

1. 切换已有部署前，用当前正在运行的旧 `web` 对生产 SQLite 做一次在线备份。从已确定的目标版本 `target_version` 生成未占用的文件名，记录备份路径和当时的生产版本。`before-` 后的版本是升级目标，库内数据来自切换前运行的版本。

   ```bash
   backup_name="before-${target_version}-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
   docker compose exec -T web gpu-admin backup "/app/data/$backup_name"
   ```

2. 切换前确认备份权限为 `0600`，并确认备份没有尚未合入本体的 WAL。对备份本体用 `mode=ro&immutable=1` 验证 `PRAGMA quick_check` 为 `ok`，且 `gpu_backup_metadata.backup_at` 与本轮命令回执相符。校验通过的本轮备份和原有备份一起保留至正式切换验收结束。
3. 三个服务均确认健康、实际运行预期镜像 ID 且正式入口验收通过后，本机只保留本轮升级前的这一份数据库备份。盘点 `data/` 和 `data/backups/` 中带管理台备份元数据的部署备份，逐项按已核对的路径删除其余副本和无占用的伴随文件，不用通配符删除，不额外留副本。保留生产库 `data/console.sqlite3`、生产库的伴随文件、锁文件及无关数据。再次核对备份清单只余一份，并记录保留路径。首次部署没有旧库时不创建备份。
4. 切换失败时先保护原有备份并完成回退核对。回退验收通过后，只保留当前正式版本原有的上一版备份；若没有原有效备份，则只保留本轮通过校验的一份。

## 正式切换与回退

1. 正式切换和回退使用与测试相同的本机 Docker socket。旧库备份校验通过后，用旧版配置执行 `docker compose stop worker`，防止旧 worker 在数据库迁移期间继续执行任务。只把已验收的摘要引用写入宿主机部署 `.env` 的 `GPU_IMAGE` 项，保留其他字段及文件权限，并清除 shell 中覆盖它的 `GPU_IMAGE`。用 `docker compose config --images` 核实三个服务选择该摘要，再用 `docker image inspect` 核对本机 ID 与测试记录相同。旧镜像只临时留到正式验收结束。
2. 已部署的宿主机执行 `docker compose up -d --no-build --pull never --wait --wait-timeout 120`。首次部署按仓库 `README.md` 准备数据目录、凭据权限和访问控制。改变绑定地址或端口前检查实际部署的防火墙及反代配置。上线后检查 `web` 和 `mcp` 的 `/healthz` 及 `gpu-admin worker-health`，核对下列输出，并从正式服务的实际入口验证本轮功能。每个服务必须有容器，实际镜像 ID 必须等于本轮记录的 ID，健康状态必须为 `healthy`。

   ```bash
   for service in web mcp worker; do
     container=$(docker compose ps -q "$service")
     docker container inspect --format '{{.Config.Image}} {{.Image}} {{.State.Health.Status}}' "$container"
   done
   ```

3. 切换失败时恢复原 `GPU_IMAGE` 和对应版本的 Compose；确认旧版可读取当前库后，用尚未回收的旧镜像启动并重新验收，再清理失败版本。已清理旧镜像后主动回退时，从 GHCR 按该版本已记录的摘要重新拉取，先隔离验收，再按正式切换流程操作。镜像回退不自动恢复 SQLite；数据不兼容时先停止切换并确认恢复方案，避免覆盖切换期间写入的数据。
4. 正式验收通过后，使用 `docker image ls --digests --no-trunc` 盘点本项目 GHCR 引用、本项目遗留标签和已知 ID。逐一核对待删摘要或完整标签当前指向的 ID，并检查包含已停止容器在内的引用。只对确认不再使用的项目引用执行 `docker image rm --no-prune "$old_ref"`。已无标签和摘要引用但能确认归属且无容器引用的旧镜像，可按完整 ID 定向删除。清理后再次盘点；不做全局 prune、强制删除或远端删除。存在未确认的归属或引用时，保留并报告阻塞，不声称已清理完。

## 交付与 Gotchas

- 报告版本、Git 修订、GHCR 摘要、三个服务的实际镜像 ID 和健康状态、正式入口验收结果、唯一的上一版数据库备份以及已清理和待处理项。正常结束后，本机只留三个服务共同使用的项目镜像，历史发布镜像留在 GHCR。基础镜像和其他项目镜像不在本轮清理范围内。
- `docker compose -p` 只改变项目名。隔离测试同时检查展开后的挂载和端口。查看部署状态只取所需字段，避免输出整个容器环境或私密配置。
- Docker 的引用核对和删除是两条命令。部署和清理须串行，删除前再次确认引用未被改指向。
