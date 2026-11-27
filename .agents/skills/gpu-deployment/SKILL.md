---
name: gpu-deployment
description: >
  在 GPU 实验管理台的功能更新通过验收后同轮部署，或在隔离测试 Docker 镜像、清理测试资源和回退版本时使用。
  优先在本地开发机或 CI 构建，生产宿主机的例外构建须通过资源检查并持续监控。
  Docker 出网须经过本机代理，正式切换后验证 web、mcp 和 worker 的镜像及健康状态。
  Use when shipping a validated GPU console change, testing or retiring a local Docker image, or rolling back a release.
  Prefer off-host builds, enforce production build limits, route Docker egress through the host proxy, and verify each Compose service after switching versions.
  Do NOT use for ordinary source tests, registry distribution, database disaster recovery or cloud GPU instance operations.
allowed-tools: Read, Glob, Grep, Bash
---

# 管理台本机部署

## 何时部署

- 完成影响管理台运行的功能更新并通过验收后，默认在同一轮从已验证的 Git 提交出镜像，完成隔离测试、备份、正式切换和上线验收。优先在本地开发机或 CI 构建，生产宿主机导入已构建镜像并以 `--no-build` 启动服务。用户指定了交付范围或上线时间时，按其要求执行。

## 生产宿主机的构建约束

优先在 CI 或开发机上构建。生产服务可用性优先于构建进度；宿主机规格、本地代理、磁盘设备及事故记录留在忽略的 `private-ops/`，不要提交。

### 开工检查

1. 只在必须使用该宿主机时安排例外构建。这些约束覆盖该机上的依赖安装和构建，包括宿主机、容器、其他终端及工作树中的任务。一次只允许一个安装或构建任务。使用同一个宿主机锁文件，持锁覆盖检查、执行、监控及确认退出，勿重建锁文件。已有任务时排队。

   ```bash
   exec 9>/run/lock/gpu-console-build.lock
   flock -n 9 || exit 1
   ```

   在本轮子 shell 中执行锁命令并保持文件描述符打开。锁用于协调遵循流程的任务，仍须检查锁外启动的进程和实际 builder 活动。

2. 每次开始或重试前，记录可用内存、load1、构建进程、磁盘空间与 BuildKit 缓存。以下命令在持锁的同一子 shell 中运行。`pgrep` 返回 1 表示无匹配，其他错误须排查。核对实际进程，略过仅含匹配文字的命令行。`buildx du` 指向实际使用的已运行 BuildKit builder，不为盘点启动新 builder。

   ```bash
   free -m | awk 'NR==2{print "可用内存:",$7,"MiB"}'
   cat /proc/loadavg
   pgrep -af '([n]pm|[n]px|[n]ext[[:space:]]+build|[u]v[[:space:]]+(sync|pip[[:space:]]+install)|[d]ocker([[:space:]]+[^[:space:]]+)*[[:space:]]+(build|buildx))([[:space:]]|$)' || [[ $? -eq 1 ]]
   df -h /
   docker buildx du --builder "$builder"
   ```

   先看宿主机指标和进程。可用内存低于 2,000 MiB 禁止开始，load1 高于 2.0 等 5 分钟后重测，有其他任务时等其退出并重测。确认负责监控的会话已就绪，再核对缓存、Docker 数据目录所在文件系统及其剩余空间。使用经典 builder 且无运行中的 BuildKit builder 时，记录这一情况并用 `docker system df` 检查 Docker 空间。确认监控使用根盘和 Docker 数据盘对应的块设备。指标无法采集或构建无法及时中断时，转到开发机或 CI。

3. 构建前核对实际生效的 `.dockerignore`，确保构建上下文排除 `node_modules`、`.venv`、`.git`、`dist` 和 `*.log`，包括实际存在的子目录。按模块做小步验证，每步使用覆盖改动的最小检查。禁止在生产机上积攒多模块或大批文件改动后直接全量重建。

### 构建资源与并发

- 所有生产机安装和构建命令使用 `nice -n 19 ionice -c3`。直接执行前端命令时设置 `NODE_OPTIONS=--max-old-space-size=1536` 和 `UV_THREADPOOL_SIZE=2`，依次执行 `npm ci --no-audit --no-fund --prefer-offline` 与 `npm run build`。生产 Python 安装使用 `uv sync --locked --no-dev`，沿用仓库所需的 `--no-editable` 等打包参数，禁止加 extras、`--all-extras` 或 `--all-groups`。`UV_THREADPOOL_SIZE` 控制 Node 的 libuv，Astral uv 的并发用 `UV_CONCURRENT_DOWNLOADS`、`UV_CONCURRENT_BUILDS`、`UV_CONCURRENT_INSTALLS` 控制。
- Dockerfile 中使用同样的安装参数及 Node 线程限制，Node 堆上限保留已有的更低值。宿主机环境变量和 CLI 的调度优先级不会自动传给 Docker daemon 或 `RUN` 步骤。在构建阶段设置变量，并为执行安装和构建的 `RUN` 命令设置同样的低优先级。核对构建依赖顺序，保证 `npm ci`、`npm run build` 与 `uv sync` 串行。Docker 构建期间禁止另启这些任务或其他镜像构建。Dockerfile 或 `.dockerignore` 未达标时，先修正并验证，或转到开发机或 CI 构建。
- Docker 构建必须确认 2 GiB 内存上限、内存与交换空间合计 2 GiB（无额外交换空间）和 1.5 核 CPU 配额实际生效。默认 `docker buildx build` 接口不支持经典 builder 的资源参数。使用 BuildKit 时，必须已有专用受限 builder，核对 worker 的实际 cgroup 限额和 `max-parallelism = 1`，并保留 Dockerfile 的步骤顺序。使用经典 builder 时，先确认 daemon 支持该模式和资源参数，再使用下面的命令。限制和串行执行无法确认时，转到开发机或 CI 构建。

  ```bash
  DOCKER_BUILDKIT=0 nice -n 19 ionice -c3 docker build \
    --memory=2g --memory-swap=2g --cpu-period=100000 --cpu-quota=150000 \
    --network=host --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY \
    --build-arg http_proxy --build-arg https_proxy --build-arg no_proxy \
    --build-arg "GPU_REVISION=$revision" --label "io.gpu-console.test-run=$run_id" \
    -t "$test_image" .
  ```

  使用下文镜像准备流程的唯一测试标签、修订、运行编号及代理变量。经典 builder 的资源参数约束构建容器，仍须监控宿主机上 daemon 的拉取、解包和导出负载。

### 监控、熔断与恢复

1. 保留能向本轮构建发送中断的会话，从开工到进程退出至少每 2 分钟采样一次，短任务也记录开工和结束值。将 `disk_device` 设置为实际块设备，需同时观察根盘与 Docker 数据盘时把两者都传给 `iostat`。`-y` 让 `%util` 使用当前 5 秒采样。

   ```bash
   cat /proc/loadavg
   free -m | sed -n '2p'
   iostat -x -y "${disk_device:?请设置实际块设备名}" 5 1
   ```

2. load1 高于 6、可用内存低于 300 MiB、任一受监控磁盘的 `%util` 高于 95%，或出现明显卡顿、SSH 延迟升高时，立即向本轮命令发送 Ctrl-C 或 SIGINT，并确认安装进程及后台构建已退出。仍有本轮 BuildKit 任务时，仅可用 `docker buildx stop "$builder"` 停止本轮独占且支持停止的 builder。禁止停止共享 Docker daemon 或无关任务。停止后执行 `sync`，报告 `cat /proc/loadavg` 与 `free -m` 的当前值。宿主机卡顿或 SSH 延迟异常时结束本轮，待恢复后再评估。
3. 失败后先读日志，只对已定位且有明确修正的原因重试。每轮最多重试两次，累计两次失败须暂停并完成定位，禁止盲重试。熔断后禁止自动重试，待资源回落、原因查明且重新通过开工检查后再决定是否继续。
4. 每次构建结束都报告耗时、采样峰值 load1、最低和最高可用内存、构建前后的 `df -h /`、是否熔断及触发指标、失败原因和重试次数。

### 禁止操作与缓存回收

- 禁止在其他终端并行构建、运行 `find /`、`grep -r /`、`du -sh /` 等全盘扫描、执行 `docker system prune -a`、删除 `/var/lib/docker`、修改 swap、内核参数或 `fstab`，以及常驻 `npm run dev`、`next dev` 或 watcher。
- 禁止在构建、安装或熔断处置中运行 `docker buildx prune`。确需回收缓存时，须持同一宿主机锁，确认 load1 低于 1 且没有构建或安装进程，只对已确认的目标 builder 执行 `docker buildx prune --builder "$builder" -f --filter 'until=168h'`。项目测试镜像仍按本轮标签和完整 ID 定向清理。

## 本轮输入

- 在仓库根目录核对 Dockerfile 的最终阶段、OCI 版本和修订标签、Compose 的 `web`、`mcp` 和 `worker` 及各自健康检查。测试只使用本轮的 Docker 对象、临时数据和虚拟凭据。
- 区分单次镜像测试与实际发布。发布前记录现用的 `GPU_IMAGE`、各容器实际运行的完整 ID、可重建旧版的 Git 修订、锁文件和非私密构建参数。确认新版本的构建输入可从所记录的 Git 修订还原。发布涉及数据库兼容性时，用隔离库核对旧程序能否读取升级后的数据。
- 每次构建独占 `gpu-console:test-<Git 修订>-<运行编号>`。同一标签只分配一次，转正式标签、切换服务和回收历史版本按项目串行执行。

## Docker 出网代理

- Docker 的对外请求必须经过本机代理，包括镜像仓库鉴权、元数据、镜像层及 Dockerfile 构建步骤的依赖下载。构建前核对 Docker daemon 和 BuildKit 的代理，为 Docker CLI 和构建步骤显式传入代理变量。用 `docker info --format '{{.NoProxy}}'` 核对 daemon 的代理例外，`NO_PROXY` 和 `no_proxy` 仅允许本机或内网地址。
- 生产宿主机、开发机和 CI 各自使用可达的代理。按镜像准备示例验证代理能访问 Docker Hub 鉴权端点。若鉴权超时或代理不可用，保持现用正式版本，检查客户端、daemon 或 builder 的代理配置，按构建重试规则通过代理重试。仍受阻时报告失败环节。

## 准备部署镜像

以下构建示例在开发机或 CI 的已验证 Git 检出目录运行。设置构建机代理 `GPU_BUILD_PROXY` 和生产机的目标平台 `GPU_BUILD_PLATFORM`，核对 Docker CLI 和 builder 指向构建机。生产宿主机确需例外构建时，先完成上面的开工检查和监控安排，只使用已核实资源限制的构建命令。

```bash
(
  set -euo pipefail
  docker_proxy=${GPU_BUILD_PROXY:?请设置构建机的代理地址}
  build_platform=${GPU_BUILD_PLATFORM:?请设置生产机的目标平台}
  proxy_exceptions=localhost,127.0.0.1,::1
  if [[ $(docker info --format '{{.HTTPProxy}} {{.HTTPSProxy}}') != "$docker_proxy $docker_proxy" ]]; then
    echo '请核对构建机 Docker daemon 的 HTTP 和 HTTPS 代理' >&2
    exit 1
  fi
  curl -fsS --max-time 10 --noproxy '' --proxy "$docker_proxy" --output /dev/null \
    'https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull'
  export HTTP_PROXY="$docker_proxy" HTTPS_PROXY="$docker_proxy" NO_PROXY="$proxy_exceptions"
  export http_proxy="$docker_proxy" https_proxy="$docker_proxy" no_proxy="$proxy_exceptions"
  revision=$(git rev-parse HEAD)
  run_id=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
  test_image="gpu-console:test-${revision:0:12}-$run_id"
  printf '测试镜像 %s\n' "$test_image"
  # 让构建步骤通过宿主机网络访问回环地址上的代理。
  docker buildx build --builder default --load --network=host --platform "$build_platform" \
    --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY \
    --build-arg http_proxy --build-arg https_proxy --build-arg no_proxy \
    --build-arg "GPU_REVISION=$revision" --label "io.gpu-console.test-run=$run_id" -t "$test_image" .
  docker image inspect --format '{{.Id}}' "$test_image"
)
```

记录构建机输出的测试标签和完整镜像 ID。通过受信渠道将这个唯一测试标签导入生产机。例如，构建机用 `docker image save` 只导出本轮标签，生产机核对传输文件的校验和及标签未被占用后，用 `docker image load --input "$image_archive"` 导入。安排在无构建或安装任务时导入，留意解包期间的宿主机负载。使用同一个镜像运行隔离测试并提升为正式标签，按标签和完整 ID 收尾各机器上的测试镜像。

## 隔离测试与自动收尾

使用 Bash、Python 3、Docker CLI 和支持 `!override` 的 Compose。复用产品的服务与健康检查，把[测试覆盖文件](assets/test.compose.yaml)作为第二个 Compose 文件。它替换各服务的数据和凭据挂载，`web` 和 `mcp` 的测试端口仅发布到回环地址，`worker` 使用内部网络，三个服务均启用只读模式。MCP 测试端口默认 `18089`，已占用时可用 `GPU_TEST_MCP_PORT` 指定空闲端口。临时数据目录须允许容器 UID 10001 写入，凭据目录使用空目录或专用虚拟值。

以下例子使用本机 Docker socket 和已导入的镜像。先把构建时记录的标签和完整 ID 分别导出为 `GPU_TEST_IMAGE` 和 `GPU_TEST_IMAGE_ID`，在对应的已验证 Git 检出目录运行。默认仅测试。实际发布时把 `release_tag` 填为正式版本标签，使通过测试的同一 ID 在收尾前获得正式标签。使用其他本机 socket 时替换为部署所用的地址。

```bash
(
  set -euo pipefail
  unset DOCKER_CONTEXT
  export DOCKER_HOST=unix:///var/run/docker.sock
  skill_dir=.agents/skills/gpu-deployment
  release_tag=''
  revision=$(git rev-parse HEAD)
  : "${GPU_TEST_IMAGE:?请提供本轮测试镜像标签}" "${GPU_TEST_IMAGE_ID:?请提供构建机记录的完整镜像 ID}"
  run_id=${GPU_TEST_IMAGE##*-}
  [[ $run_id =~ ^[0-9a-f]{32}$ && $GPU_TEST_IMAGE == "gpu-console:test-${revision:0:12}-$run_id" ]]
  [[ $GPU_TEST_IMAGE_ID =~ ^sha256:[0-9a-f]{64}$ ]]
  image_record=$(docker image inspect --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "io.gpu-console.test-run"}}' "$GPU_TEST_IMAGE")
  if [[ $image_record != "$GPU_TEST_IMAGE_ID $revision $run_id" ]]; then
    echo '导入镜像的 ID、Git 修订或运行编号与本轮记录不符' >&2
    exit 1
  fi
  built_id=$GPU_TEST_IMAGE_ID
  export GPU_TEST_IMAGE
  export GPU_TEST_DIR
  GPU_TEST_DIR=$(mktemp -d)
  test_project="gpu-console-test-$run_id"
  compose=(docker compose --env-file /dev/null -p "$test_project"
           -f compose.yaml -f "$skill_dir/assets/test.compose.yaml")
  started=0

  finish() {
    local test_status=$? left
    trap - EXIT
    trap '' HUP INT TERM
    if (( started )); then "${compose[@]}" down || exit 1; fi
    python3 "$skill_dir/scripts/cleanup_test_image.py" --tag "$GPU_TEST_IMAGE" --image-id "$built_id" || exit 1
    left=$(docker image ls --quiet "$GPU_TEST_IMAGE") || exit 1
    if [[ -n $left ]]; then echo "仍有待核对的测试标签 $GPU_TEST_IMAGE" >&2; exit 1; fi
    rm -rf -- "$GPU_TEST_DIR" || exit 1
    exit "$test_status"
  }
  trap finish EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  printf '测试项目 %s，目录 %s，标签 %s\n' "$test_project" "$GPU_TEST_DIR" "$GPU_TEST_IMAGE"
  install -d -m 0700 -o 10001 -g 10001 "$GPU_TEST_DIR/data" "$GPU_TEST_DIR/secrets" "$GPU_TEST_DIR/secrets/feishu-settings"
  "${compose[@]}" config --quiet
  printf '%s\n' "$built_id" > "$GPU_TEST_DIR/image-id"
  printf '测试镜像 ID %s\n' "$built_id"
  started=1
  "${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 120
  for service in web mcp; do
    "${compose[@]}" exec -T "$service" python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3).read().decode())"
  done
  "${compose[@]}" exec -T worker gpu-admin worker-health
  # 在此运行本次改动的针对性检查，任一失败进入 finish。
  if [[ -n $release_tag ]]; then
    version=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$built_id")
    [[ $version =~ ^[0-9][[:alnum:]_.-]{0,127}$ && $release_tag == "gpu-console:$version" ]]
    existing=$(docker image ls --no-trunc --quiet "$release_tag")
    [[ -z $existing || $existing == "$built_id" ]]
    docker image tag "$built_id" "$release_tag"
  fi
)
```

涉及 MCP 或入口变更时，从测试回环地址用虚拟 Key 完成 Streamable HTTP 握手、工具发现和一次只读调用。核对独立入口拒绝无 Key 的请求，同时确认网页端口的 `/mcp` 返回 `404`。涉及 Key 迁移时，用旧版结构的隔离数据验证迁移前后访问与撤销行为。

[定向清理脚本](scripts/cleanup_test_image.py)只接受完整测试标签和完整镜像 ID。脚本检查包含已停止容器在内的引用，再按标签删除。测试与正式标签共享同一 ID 时保留正式标签。查询失败、标签已换指向或仍被容器占用时，保留诊断和本轮身份信息。进程被强制终止后，按原测试项目收尾，使用记录的 ID 重试脚本。避免主机级 prune、强制删除和按模糊前缀批量删除。

## 正式升级的数据库备份

1. 切换已有部署前，用当前正在运行的旧 `web` 对生产 SQLite 做一次在线备份。从已确定的目标版本 `target_version` 生成未占用的文件名，记录备份路径和当时的生产版本。`before-` 后的版本是升级目标，库内数据来自切换前运行的版本。

   ```bash
   backup_name="before-${target_version}-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
   docker compose exec -T web gpu-admin backup "/app/data/$backup_name"
   ```

2. 切换前确认备份权限为 `0600`，并确认备份没有尚未合入本体的 WAL。对备份本体用 `mode=ro&immutable=1` 验证 `PRAGMA quick_check` 为 `ok`，且 `gpu_backup_metadata.backup_at` 与本轮命令回执相符。校验通过的本轮备份和原有备份一起保留至正式切换验收结束。
3. 三个服务均确认健康且实际运行预期镜像 ID 后，只保留本轮升级前的这一份。盘点 `data/` 和 `data/backups/` 中带管理台备份元数据的部署备份，逐项按已核对的路径删除其余副本和无占用的伴随文件。保留生产库 `data/console.sqlite3`、生产库的伴随文件和锁文件。再次核对备份清单只余一份，并记录保留路径。
4. 切换失败时先保护原有备份并完成回退核对。回退完成后，保留当前正式版本原有的上一版备份。缺少原备份时保留本轮通过校验的一份。

## 正式切换与回退

1. 正式切换和回退使用与测试相同的本机 Docker socket。在部署 shell 中执行 `unset DOCKER_CONTEXT`，将 `DOCKER_HOST` 导出为测试时的 socket 地址。把已验证的正式标签写入宿主机部署使用的 `.env` 的 `GPU_IMAGE` 项，保留其他字段及文件权限，清除 shell 中覆盖它的 `GPU_IMAGE`。用 `docker compose config --images` 核实三个服务选择该标签，再用 `docker image inspect` 核对本机 ID 与测试记录相同。保留旧版到新服务健康。
2. 已部署的宿主机执行 `docker compose up -d --no-build --pull never --wait --wait-timeout 120`。首次部署按仓库 `README.md` 准备数据目录、凭据权限和访问控制。改变绑定地址或端口前检查实际部署的防火墙及反代配置。上线后检查 `web` 和 `mcp` 的 `/healthz` 及 `gpu-admin worker-health`，核对下列输出，并从正式服务的实际入口验证本轮功能。每个服务必须有容器，实际镜像 ID 必须等于本轮记录的 ID，健康状态必须为 `healthy`。

   ```bash
   for service in web mcp worker; do
     container=$(docker compose ps -q "$service")
     docker container inspect --format '{{.Config.Image}} {{.Image}} {{.State.Health.Status}}' "$container"
   done
   ```

3. 切换失败时恢复原 `GPU_IMAGE`，用旧版 Compose 和镜像重新验收该版本的服务，再移除已确认闲置的失败版本。回退到网页内置 MCP 的版本时，先停止并移除独立的 `mcp` 容器。镜像回退与数据库还原分别核对，避免覆盖切换期间写入的数据。成功清理旧镜像后的回退，优先导入已验证的旧版本镜像，或在开发机或 CI 从所记录的 Git 修订、锁文件和构建参数重建，重新隔离测试并发布。
4. 所有服务验收通过后，用 `docker image ls --no-trunc gpu-console` 盘点本项目标签。对每个退役版本核对记录的 ID 及当前容器引用，按标签执行 `docker image rm --no-prune "$old_tag"`。同时复核发布记录中的旧 ID。若已失去所有标签、能确认归属且没有容器引用，可按该完整旧 ID 定向回收。完成后重新盘点，报告仍在使用、归属不明或 ID 已变化的条目。

## 交付与 Gotchas

- 报告版本标签、Git 修订、预期 ID、各服务的实际 ID 和健康结果、已保留的上一版数据库备份路径、已清理项及仍待处理项。最终本项目只保留一个正式版本标签和一个正式镜像 ID，三个服务均使用它。基础镜像和构建缓存由各自的生命周期管理。
- `docker compose -p` 只改变项目名。隔离测试同时检查展开后的挂载和端口。查看部署状态只取所需字段，避免输出整个容器环境或私密配置。
- Docker 的标签核对和按标签删除是两条命令。从镜像准备到测试收尾，本轮独占测试项目名和标签。
