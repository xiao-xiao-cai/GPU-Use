# GPU 实验管理台

自托管管理台，用于管理优云智算实例、库存和定时任务。网页下线后，后台仍会运行已保存的任务。一次性关机预约以云端回读为准，周期预约依赖后台逐次登记。

## 在宿主机部署

在项目根目录执行以下命令。需要 Docker Compose，镜像内进程以 UID 10001 运行。默认只监听回环地址；需要远程访问时，请先配置受信任的私有网络、访问控制及相应的 `.env`，不要直接暴露管理端口到公网。

```bash
install -d -m 0700 -o 10001 -g 10001 data
install -d -m 0700 secrets
install -d -m 0700 -o 10001 -g 10001 secrets/feishu-settings
cp -n .env.example .env
```

把优云智算公私钥分别放入 `secrets/compshare_public_key` 和 `secrets/compshare_private_key`，文件只含密钥值。为 worker 授予目录遍历和指定文件的读权限。

按[发布和升级说明](#从-ghcr-发布和升级)，先将 `.env` 中的 `GPU_IMAGE` 设为本次发布的 GHCR 摘要引用。此处只拉取镜像，不在生产机上构建。

```bash
chmod 600 secrets/compshare_public_key secrets/compshare_private_key
setfacl -m u:10001:--x secrets
setfacl -m u:10001:r-- secrets/compshare_public_key secrets/compshare_private_key
docker compose config --quiet
docker compose pull
docker compose up -d --no-build --pull never --wait
docker compose exec web gpu-admin init
docker compose ps
```

在宿主机打开 [GPU 实验管理台](http://127.0.0.1:18080/)，用刚初始化的管理员账号登录。对照平台控制台核对项目与实例，检查设置页的后台心跳和各次采集时间。确认只读同步后，可将 `.env` 的 `GPU_READ_ONLY` 改为 `0` 并重建服务容器。若配置 HTTPS 入口，将 `GPU_COOKIE_SECURE` 设为 `1`。

```bash
docker compose up -d --no-build --wait
docker compose exec web gpu-admin passwd
```

`passwd` 用于本地修改管理员密码，同时使已有会话失效。自定义服务单元、防火墙脚本或运维文档放在本机的 `private-ops/`，不要提交。

如果网络需要不同的网桥 MTU，在 `.env` 中显式设置 `GPU_DOCKER_MTU`，用 `docker compose down` 和 `docker compose up -d --no-build --pull never --wait` 重建本项目网桥。

若已在同一账户的测试实例上确认平台登记后返回确切的 `SchedulerStopTime`，并在撤销后省略两个预约字段，可在 `.env` 设置 `GPU_SCHEDULER_OMITS_EMPTY=1`，再重建 worker。默认将缺失视为待核对。格式无效或有冲突的预约时间始终需要核对。

## 自动关机和预约开机

在「任务中心 → 自动化规则」选择项目和实例。空闲关机只累计允许时段内的低负载时间。默认要求 CPU、每张 GPU 的利用率和显存使用率都在 10% 以下，连续满足 60 分钟。开机满时关机从平台记录的开机时间起算，到时检查最近连续 5 分钟的低负载。有负载或监控数据不足时，当次只提醒。规则时段使用北京时间，执行结果可在任务中心查看。

创建「只启动已有实例」任务时，可设置无资源时的重试间隔。平台明确返回资源不足后，后台持续尝试，直到指定实例有卡运行、用户停止本次重试或遇到明确的终止错误。首次执行仍受预约补跑窗口约束，默认 2 小时。云端已受理或提交结果不明时，后台通过回读确认。停止本次重试后，未来的周期预约仍可按计划运行。

## 数据看板

登录后从侧栏打开「GPU 库存」，查看各可用区的独占池和抢占池剩余卡数，以及平台缓存更新时间。机型目录提供可选配置。后台只为目录中有规格的资源池记录余量，完整规格的容量由项目下的库存探测确认。

库存列表默认展示普通池的余量、规格数和完整小时价区间。点击条目查看各配置、报价来源、库存趋势和关注入口。后台为普通池的全部目录规格保存每个项目的报价，优先使用账户报价，缺失时显示平台目录价。列表和详情读取 SQLite，后台每 7 天更新一次已采集的价格。按天、包月和抢占式报价可在详情按需查询。

「数据看板」按项目展示观测到的 GPU·小时、利用率和自动化回执。库存趋势分别展示平台池余量与规格可租率。后台按项目和相同调度参数合并探测，目标为每组 5 分钟一次。按卡数查看时，只显示目录里有该卡数规格的资源池，恰好该卡数的目录组合有任一项可调度即记为可租，全部明确不可调度才记为不可租。规格可租率为近七天可租次数除以有明确结果的探测次数，规格探测完整率为有明确结果的探测次数除以计划探测次数。

在「数据看板 → 成本与占卡」和「实例详情 → 成本与用量」查看平台已支付账单。合计以现金实付为主，按算力、存储、镜像和其他拆分，并列出订单金额和抵扣。订单日按平台创建订单的北京时间归集；服务期按已知服务区间分摊使用、新购和续费订单的实际金额，退款及其他调整按订单日列入。存储订单按能核实的实例归属汇总，其余归项目。「独立存储」也可以打开逐项账单。

后台在北京时间 05:00–06:00 同步账单。每个项目的当月和上月账期每天各尝试一次，较早账期按需回填，失败后等下一个同步窗口。页面保留最后一次成功同步的金额，并显示账单同步时间、最新订单时间、账期覆盖和同步错误。成本 API 和 MCP 接受 `basis=order_date` 或 `basis=service_period`，实付金额在 `billing` 中返回。

「今日待入账估算」按北京时间当天的有效采样估算算力和按时或按量收取的独立存储费用。已支付订单覆盖的服务时段从估算中扣除。估算金额在 `today_estimate` 中单独返回。零点后前一天显示已同步的款项和入账状态，后续以平台账单补齐。

空转成本、整机低负载成本和每加权 GPU·小时费用，按实例和服务时间将实际算力账单与 GPU 占用时段匹配，再根据监控分摊现金实付。当天尚未入账且具备费率与逐卡监控的时段提供单独的估算。占卡、低负载和利用率加权卡时来自监控，保留近 30 天。

实例清单缺少平台算力单价时，后台按实例 ID 补查。详情展示最新采集的单价和采集时间，历史成本使用对应区间内采集的费率或手填费率。

实例卡片分别展示「已关机」和最近的库存。池余量每 5 分钟采集一次。无卡关机后，后台用上次读到的有卡配置继续检查库存。有余卡但从未读到原有卡规格时显示「原有卡规格未读到」，余量为零时显示「暂时无卡 · 池余 0 张」。详情中的报价与库存按所选配置核对。已关机的实例可从「更多操作 → 更改规格」选择同 GPU 型号的新配置，核对差价后提交后台。改配完成后保持关机，可在任务中心查看回读结果。

### 无卡开机与循环关机

已关机的按量实例，只要平台确认支持无卡开机，就会自动显示所在可用区开放的配置。A800 实例暂不支持。任务中心的一次、每天、每周和固定间隔开机任务使用同一套可用区规则。配置「每轮实际开机后按运行时长关机」时，从平台回读的实际开机时间起算（15 分钟至 7 天）。仅为本轮确认由本任务启动的实例登记预约，不覆盖其他预约。实例已按目标模式运行时保留现状；另一模式或档位已运行时跳过并提醒。不会自动停机切换，也不会在缺卡时自动转用无卡。

| 可用区 | 无卡开机配置 | 官方标准价或限制 |
| --- | --- | --- |
| 华北二 A、上海二 B | 4 核 / 8 GB（`C`），8 核 / 16 GB（`B`） | 分别为 ¥0.30/时、¥0.60/时 |
| 华北一 C、上海二 A、上海二 C | 2 核 / 4 GB（`A`） | 平台限制每个可用区同时最多 1 台无卡实例 |

华北二 A 已在真实实例上验证 4 核 / 8 GB 和 8 核 / 16 GB 开机、实际 CPU 和内存回读，以及关机预约。4 核 / 8 GB 的接口参数为 `WithoutGpuSpec=C`。 [可用区说明](https://www.compshare.cn/docs/operation/introduce/region) · [无卡模式指南](https://www.compshare.cn/docs/operation/gpu/cardlessmode) · [启动接口](https://www.compshare.cn/docs/gpus/instance/startcompshareinstance)

「无卡实例仅 CPU 低负载关机」规则仅对明确无卡运行且能匹配无卡配置的实例生效；CPU 采样缺失、中断或负载回升不关机。普通 GPU 低负载和最长运行时间规则不会把缺失的 GPU 采样当作空闲。今日无卡算力估算使用可核实的账号小时价或同区同档的官方标准价，未核价时单独标注，不计入 GPU·小时。已支付服务时段不再估算，次日历史只用平台账单。

管理员可补充同区同档的官方小时价，或登记其他可用区实测通过的配置。登记前确认实例列表能回读 `Running`、`GPU=0`、此次 `StartTime` 和实际 CPU / 内存，且云端关机预约能登记并回读。在部署主机运行 `GPU_DATA_DIR=./data uv run --locked gpu-admin verify-cardless-profile /path/to/profile.json`。JSON 包含 `region`、`zone`、`spec`（`A`、`B` 或 `C`）、`cpu`、`memory_gb`，以及可选的 `standard_hourly_cny`（该区该档官方小时价）。

MCP `gpu_start_instance` 与开机任务的 `config` 接受 `boot_mode=without_gpu` 和 `without_gpu_spec`；省略则保持有卡。仅开机任务可设置 `shutdown_after_seconds`，与原 `shutdown_at` 互斥。升级时先停止旧 Worker，避免其忽略新增字段；开启档位后如需回退，先停写并核对、停用包含新字段的开机任务与 CPU 规则，以及对应在途回执和预约，不能让旧 Worker 直接继续执行这些任务。

「实例总览 → 已释放」保留最后能确认的规格和释放回执，可回查保留期内的监控、成本和用量。「独立存储」列出数据盘和云存储 Pro，包括未挂载的资源。后台每 5 分钟回读资源和挂载关系，每天刷新账号报价，每周刷新标准价。容量或计费规格变化时提前询价，当前价格旁标明来源。独立数据盘在未挂载时仍按平台规则计费。

占卡试算按最迟开训时间和准备缓冲回放近七天库存，从达到历史库存门槛的候选中比较提前持有和训练费用。输入费率须采用同一算力费用口径。MCP 对应工具为 `gpu_get_cost_summary` 和 `gpu_plan_gpu_rental`，两者只读。

可从机型目录关注现有组合，并为关注规格订阅租卡时段提醒。关注、租卡试算和「仅通知有货」任务使用平台调度探测。关注按保存的 GPU 卡数、CPU 核数和内存组合检查，后台使用默认系统盘和自动 CPU 平台。普通可用区使用官方系统镜像，容器可用区使用官方容器镜像。探测失败或结果超过 10 分钟时显示待核对。升级前的余量记录保留在余量图中，规格可租率从新探测开始累计。外部提醒沿用现有飞书或邮件配置。

## 创建实例与缺货等待

在「实例总览 → 创建实例」选择可用区、机型、计费方式和镜像。系统盘采用已核实的官方默认配置。支持数据盘的规格可选择盘型和容量。普通云存储的开通在平台控制台完成。容器可用区的云存储 Pro 支持选择已有资源，或确认独立的开通、扩容价格上限。

创建实例、模板和采购任务时，选择平台提供的 GPU 张数、CPU 核数和内存组合。CPU 平台由系统按区选配。下单前后台核验完整配置、容量和报价。已保存的旧任务编辑并重新确认后启用自动选配。

华北二A、上海二B提供 100 GB 默认云硬盘系统盘、可选数据盘和对象存储。华北一C、上海二A、上海二C使用 50 GB 默认本地系统盘及云存储 Pro，仅支持容器及普通计费池。区级默认值用于当前机型目录支持该盘型和容量的规格。 [官方 GPU 可用区说明](https://www.compshare.cn/docs/operation/introduce/region)

「现在尝试，失败后继续」可在缺货时提交。确认前填写实例报价上限、期限和通知渠道。任务中心显示等待原因、下次检查时间和云端回读结果。若还需开通或扩容云存储 Pro，后台等实例可用且两项报价均符合授权后先处理存储，再复核实例。停止后续尝试也会保留已受理订单的回读记录。

如果机型目录和区级规则都无法确认系统盘默认值，管理员可以从优云智算官方创建页核实指定配置的盘型和容量，并在部署主机登记。通过同一配置实际使用的项目、可用区、机型、资源池和镜像 ID 确定登记范围。下面是 JSON 字段示例。

```json
{
  "project_id": "org-example",
  "region": "cn-wlcb",
  "zone": "cn-wlcb-01",
  "gpu_model": "5090",
  "instance_type": "uhost",
  "image_id": "image-example",
  "type": "CLOUD_SSD",
  "size_gb": 100,
  "source": "https://www.compshare.cn/"
}
```

将实际已核实的配置填入 JSON。容量、盘型和来源取官方页面显示的值，来源 URL 去掉查询串和片段。在宿主机运行 `GPU_DATA_DIR=./data uv run --locked gpu-admin verify-boot-default /path/to/verified-disk.json`。登记的系统盘必须符合当前机型目录。系统盘待核实的组合会提示后暂停提交。

## 通过 MCP 管理 GPU

独立的 `mcp` 服务提供 Streamable HTTP，默认地址为 `http://127.0.0.1:18081/mcp`。发布端口可用 `GPU_MCP_BIND_PORT` 调整。使用 HTTPS 反代时，将 `GPU_MCP_PUBLIC_URL` 设为客户端可达的完整地址，包含 `/mcp`，并保留请求的 Host。其他允许的 Host 和 Origin 可分别通过 `GPU_MCP_TRUSTED_HOSTS` 和 `GPU_MCP_TRUSTED_ORIGINS` 指定。

登录「设置与通知 → MCP 配置」，为每个客户端签发独立 Key，按需添加管理或 SSH 权限。复制 Key 和所用客户端的配置。Key 只在签发和轮换后显示一次。旧版 Key 会迁入列表并保留其权限，切换客户端地址后可继续使用。用完旧 Key 后，可在界面撤销。作为本机管理入口，`docker compose exec -T web gpu-admin mcp-token status` 列出 Key 的 ID 和状态，`rotate --key-id <ID>` 或 `revoke --key-id <ID>` 对指定 Key 生效。

以下命令为运行在部署主机的 Codex 添加默认连接。远程使用时，把 URL 换成管理台显示的客户端可达地址。提供 Key 后，在同一环境中启动 Codex。图形客户端可在其凭据设置里使用对应 Key 和 `Authorization: Bearer <MCP_KEY>`。

```bash
read -rsp 'GPU MCP Key: ' GPU_CONSOLE_MCP_TOKEN
export GPU_CONSOLE_MCP_TOKEN
printf '\n'
codex mcp add gpu-console --url 'http://127.0.0.1:18081/mcp' --bearer-token-env-var GPU_CONSOLE_MCP_TOKEN
```

启用项目里的 `gpu-management` Skill 后，可在聊天中查询实例和监控，管理任务、自动化规则、模板、库存关注与通知。购买、释放和影响自动采购的改动先展示预览，再根据你的聊天确认提交。MCP 校验预览和请求标识。 [GPU 管理 Skill](.agents/skills/gpu-management/SKILL.md)

虚机释放要求目标已关机，关联任务和规则已逐项处理。`GPU_MCP_RELEASE_CHARGE_TYPES` 默认允许按量后付费。包日、包月和抢占式的部署能力需逐类完成平台核对后加入该配置。镜像回退前应暂停云端写入，核对仍在途的释放记录，随后恢复与旧版兼容的运行状态。

回退至 0.25.0 之前的镜像前，等待已提交的改配运行回读结束，并取消未发送的改配运行。

### SSH 到 GPU 实例

第一版支持优云智算平台密码登录。先用已有可信的 SSH 会话或实例控制台读取服务器公钥（如 `/etc/ssh/ssh_host_ed25519_key.pub`），再在部署主机的 `secrets/gpu-ssh-targets.json` 登记目标。文件为 JSON 数组，每项含 `project_id`、`instance_id` 和 `host_key`，其中 `host_key` 填完整公钥行（算法和 Base64 内容，可带注释）。文件使用 `0600`，按现有私密文件的配置方式授权容器 UID `10001` 只读。登记后，持有 SSH 权限 Key 的客户端可以使用实例配置的 SSH 用户执行命令。更新主机公钥时先从可信渠道重新核实。

在部署主机的项目目录运行以下命令。这个入口使用已有后台密码服务，需要本机账号可以访问 `data/.ssh.sock`。将示例 ID 换成 MCP 查到的项目和实例 ID。

```bash
uv run --locked gpu-admin ssh open project-example instance-example
uv run --locked gpu-admin ssh exec project-example instance-example 'nvidia-smi'
uv run --locked gpu-admin ssh attach project-example instance-example
uv run --locked gpu-admin ssh close project-example instance-example
```

其他 MCP 客户端可调用 `gpu_get_ssh_connection` 查询地址，接着用 `gpu_ssh_open` 建立或复用连接。用 `gpu_ssh_exec` 执行短命令，或通过 `gpu_ssh_terminal_send` 与 `gpu_ssh_terminal_read` 操作实例上的 `tmux`。同一次命令或终端输入在当前 SSH 会话中使用同一个 `request_id`。每个会话保存 256 条操作回执，用满后关闭并重新连接即可继续操作。命令结果为 `unknown` 时先检查进程或日志。用 `gpu_ssh_close` 主动断开。后台建立 SSH 会话前必须通过 `GPU_SSH_PROXY` 配置从 worker 可达的 HTTP CONNECT 代理（`host:port`），未设置时不会直连。

终端和 MCP 使用实例上的同一个 `gpu-console` tmux 会话。把切换目录、激活环境和训练命令放在这个会话中。SSH 连接空闲 15 分钟后释放，新聊天可以重新连接同一终端。实例重启后可建立新的终端会话。

## 配置通知

在「设置与通知 → 飞书规则」选择全局事件。已配置飞书时，默认推送管理台、MCP、定时任务和自动化产生的开关机确认、操作异常和关机预约待核对。重启、新建、释放、登记预约和等卡过久可单独开启。全局规则与任务各自的飞书订阅合并，同一事件只安排一条飞书投递。

在任务表单中选择该任务的通知事件和渠道。任务成功启动或新建实例后，该次开机的持续空闲提醒使用任务的通知配置。其他实例的空闲提醒使用设置页的默认配置。

在管理台打开「设置与通知 → 投递记录 → 通知渠道 → 配置飞书」，按你已有的机器人选择配置方式。

- **应用机器人（企业自建应用）**。在飞书开发者后台进入应用，从「凭证与基础信息 → 应用凭证」复制 App ID 和 App Secret。启用机器人能力，在「权限管理」开通 `im:message:send_as_bot`，发布新版本并把接收用户加入可用范围。回管理台填入应用凭证。发给个人时填写飞书邮箱或该应用下的 Open ID。已有机器人单聊时，可以从会话设置复制完整的 `oc_` 会话 ID，填到管理台的「群会话 ID」选项。群聊的会话 ID 也填在这里，并先把应用机器人加入群聊。 [飞书发送消息文档](https://open.feishu.cn/document/server-docs/im-v1/message/create)
- **群自定义机器人（Webhook）**。在飞书群打开「设置 → 群机器人 → 添加机器人 → 自定义机器人」，复制 Webhook 地址。如果机器人开启了签名校验或自定义关键词，在管理台填写对应值。

保存后选择飞书并点击「发送测试消息」，在投递记录查看回执，再在目标会话确认实际送达。按需在采集与提醒、任务或库存关注中启用对应订阅。

页面保存的配置存放在 `secrets/feishu-settings/`，下次投递自动生效。保存前，后台继续使用原有的 `secrets/feishu_webhook_url`、`secrets/feishu_sign_secret` 和可选的 `secrets/feishu_keyword`。

阿里云邮件推送使用 `secrets/aliyun_access_key_id`、`secrets/aliyun_access_key_secret` 和 `secrets/aliyun_mail_sender`，当前使用华东 1 邮件推送端点。每个文件只保存对应值。设置权限为 `0600` 后，用 `setfacl -m u:10001:r-- <文件>` 授予 worker 读取权限，然后运行 `docker compose restart worker`。

## 升级、备份和恢复

### 从 GHCR 发布和升级

[GitHub Actions](https://github.com/xiao-xiao-cai/GPU-Use/actions/workflows/ci.yml) 验证发往 `main` 的推送和 PR。授权发布时，从 `main` 推送与项目版本一致的 `vMAJOR.MINOR.PATCH` 标签。Actions 在 GitHub 托管的 runner 上构建 `linux/amd64` 镜像，三个服务通过隔离健康检查后，才发布 `ghcr.io/xiao-xiao-cai/gpu-use:<版本>`。首次发布的包如果仍为私有，需在包设置中启用公开读取，或在部署机使用仅具 `read:packages` 权限的凭据登录 GHCR。GitHub 不连接生产主机，也不保存生产凭据。

若标签没有触发发布，在 `main` 上运行 `gh workflow run ci.yml --ref main -f release_tag=v0.30.1`。将示例值换成已推送的版本标签，无需移动标签。

在 Actions 任务摘要中复制 `Deployment image`（含 `@sha256:`），通过宿主机 Docker 代理拉取。核对镜像的 OCI 版本、Git 修订与发布记录一致，并用该镜像完成[隔离验收](.agents/skills/gpu-deployment/SKILL.md#隔离测试与自动收尾)。已有部署先按下文备份，再停止旧 worker；仅将 `.env` 的 `GPU_IMAGE` 替换为上述摘要引用。启动并检查三个服务：

```bash
docker compose config --images
docker compose up -d --no-build --pull never --wait --wait-timeout 120
docker compose ps
```

确认 `web`、`mcp` 和 `worker` 都使用已验收的同一镜像 ID。切换失败时恢复原 `GPU_IMAGE` 和旧服务，不自动还原数据库。

### 数据库备份和恢复

升级前进行在线备份。备份包含任务、会话及管理员密码哈希，应存放在权限受限的目录。请定期将备份复制到另一台受信任的主机。

```bash
docker compose exec web gpu-admin backup /app/data/before-upgrade.sqlite3
```

如使用自定义访问控制，在调整绑定地址和端口前同步检查防火墙或反代配置。

首次升级到支持网页配置飞书的版本时，在宿主机执行 `install -d -m 0700 -o 10001 -g 10001 secrets/feishu-settings` 后再重建服务。飞书配置由独立的私密目录持久化，随 `secrets/` 单独备份。

保留当前备份后，在 web、mcp 和 worker 均停止的窗口恢复旧数据。恢复会注销原网页登录会话、撤销 MCP Key、启用只读核对并跳过还原时间之前的历史触发。

```bash
docker compose stop
docker compose run --rm --no-deps web gpu-admin restore /app/data/before-upgrade.sqlite3
docker compose up -d --no-build --pull never --wait
```

按恢复命令输出的 UTC 时间范围，核对云端实例、运行备注、平台订单和预约。盘点备份之后的采购时也包括已释放的实例。在写入恢复前，暂停仍有疑问的任务。对于已核实且未在恢复库计数的成功订单，可记入任务的补充采购次数。此数字是累计值，包含之前恢复中为同一任务录入且仍未体现在库内成功回执的次数。

后台创建实例时会提交运行备注和 `gpu-console-<运行编号>` 格式的实例名称。平台省略备注时，可用名称辅助核对该笔采购。后台会核对名称、可用区、规格和创建时间，结果无法确认时会继续保留采购预留。

```bash
docker compose exec web gpu-admin restore-purchases <任务ID> <补充次数>
docker compose exec web gpu-admin confirm-restore --note '记录已核对的订单、实例和预约'
```

恢复开关与 `GPU_READ_ONLY` 独立。后台在两个开关均允许时发起新的云端写操作。周期任务从对账后的下一次触发继续运行。

## 开发验证

按本次改动选择下表中的检查。跨层改动组合受影响的用例和入口，具体选测规则见 [gpu-change-verification](.agents/skills/gpu-change-verification/SKILL.md)。

| 改动 | 命令或验证方式 |
| --- | --- |
| 文档、规则、技能说明 | 检查引用和示例，运行 `git diff --check` |
| 成本汇总逻辑 | `uv run --locked pytest tests/test_dashboard_cost.py`，局部回归可指定单个用例 |
| Python 依赖清单或锁文件 | `uv lock --check` |
| SDK 字段、版本或重试行为 | `uv run --locked python .agents/skills/gpu-change-verification/scripts/check_sdk_contract.py --json` |
| 前端交互或类型 | 从改动的页面入口验收，涉及类型变化时运行 `npm --prefix frontend run typecheck` |
| 前端打包、依赖、入口或构建配置 | `npm --prefix frontend run build` |
| Compose 配置 | `docker compose config --quiet` |

依赖环境需要准备或更新时，运行 `uv sync --locked` 或 `npm --prefix frontend ci`。
