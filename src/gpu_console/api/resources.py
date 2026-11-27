"""Read snapshots and queue refresh, settings, and notification commands."""

import json
from collections.abc import Iterator
from sqlite3 import Connection
from typing import Annotated, Literal, Self
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gpu_console import storage
from gpu_console.api.auth import require_session
from gpu_console.application import inventory_overview, inventory_prices, notice_policies, watches as watch_service
from gpu_console.application.tasks import shutdown_cleanup_pending
from gpu_console.application.availability import pool_snapshot
from gpu_console.application.feishu_notices import FeishuEventPolicy
from gpu_console.application.notifications import list_notifications
from gpu_console.application.ssh_password import PasswordServiceBusy, PasswordServiceUnavailable, fetch_ssh_password
from gpu_console.application.task_config import optional_bool, validate_spec
from gpu_console.config import Config
from gpu_console.domain.instance_access import safe_instance_url
from gpu_console.storage import availability, cardless, instance_assets, instance_history
from gpu_console.storage import refresh as refresh_store
from gpu_console.storage.feishu import (
    FeishuAppSettings,
    FeishuReceiveIdType,
    FeishuSettingsUnavailable,
    read_feishu_settings,
    save_feishu_settings,
)

router = APIRouter(prefix="/api", dependencies=[Depends(require_session)])
Name = Annotated[str, Field(min_length=1, max_length=100)]
Id = Annotated[str, Field(min_length=1, max_length=100)]
CatalogSpecId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Channel = Literal["site", "feishu", "email"]
NoticeEvent = Literal[
    "result", "review", "shutdown-pending", "shutdown-outcome", "idle", "stock", "test", "legacy",
    "idle-recovery", "gpu-memory-high", "monitoring-interrupted", "monitoring-restored", "watch-stock", "rental-window",
    "capacity-wait",
]


def database(request: Request) -> Iterator[Connection]:
    with storage.connect(request.app.state.config.db) as conn:
        yield conn


# FastAPI enters sync dependencies and their handlers in a thread pool.
Db = Annotated[Connection, Depends(database)]


class Refresh(BaseModel):
    kind: Literal["projects", "catalog", "instances", "stock", "pool"]
    project_id: Id | None = None
    watch_id: Id | None = None


class InventoryPriceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: Id
    spec_id: CatalogSpecId
    charge_type: inventory_prices.ChargeType


class JupyterOverride(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class Watch(BaseModel):
    name: Name
    project_id: Id
    catalog_spec_id: Id | None = None
    spec: dict | None = None
    expected_version: Id | None = None


class Template(BaseModel):
    name: Name
    project_id: Id
    config: dict


class IdleWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekday: int = Field(strict=True, ge=1, le=7)
    start: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    end: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")

    @model_validator(mode="after")
    def distinct_times(self) -> Self:
        if self.start == self.end:
            raise ValueError("开始与结束时间必须不同")
        return self


class IdleSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["all_day", "scheduled", "paused"]
    windows: list[IdleWindow] = Field(max_length=70)

    @model_validator(mode="after")
    def require_windows(self) -> Self:
        if self.mode == "scheduled" and not self.windows:
            raise ValueError("请设置至少一个通知时段")
        return self


RepeatMinutes = Annotated[int, Field(strict=True, ge=0, le=10080)]


class NotificationRepeatMinutes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site: RepeatMinutes
    feishu: RepeatMinutes
    email: RepeatMinutes


class IdleSnooze(BaseModel):
    model_config = ConfigDict(extra="forbid")

    until: int | None = Field(strict=True, ge=1, le=2_147_483_647)


class NoticeRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(strict=True)
    channels: list[Channel] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_channels(self) -> Self:
        if self.enabled and not self.channels:
            raise ValueError("启用的通知项至少需要一个渠道")
        if len(self.channels) != len(set(self.channels)):
            raise ValueError("通知渠道不能重复")
        return self


class MemoryNoticeRule(NoticeRule):
    threshold: float = Field(ge=6, le=100, allow_inf_nan=False, strict=True)
    duration_minutes: int = Field(ge=1, le=1440, strict=True)


class NotificationRules(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idle_recovery: NoticeRule
    gpu_memory_high: MemoryNoticeRule
    monitoring: NoticeRule
    watch_stock: NoticeRule
    email_recipient: str | None = Field(max_length=254)

    @model_validator(mode="after")
    def validate_email(self) -> Self:
        rules = (self.idle_recovery, self.gpu_memory_high, self.monitoring, self.watch_stock)
        if any(rule.enabled and "email" in rule.channels for rule in rules) and not self.email_recipient:
            raise ValueError("邮件渠道需要填写收件地址")
        if self.email_recipient:
            local, _, domain = self.email_recipient.partition("@")
            if not local or "." not in domain or "@" in domain or any(
                char.isspace() or char in ",;" for char in self.email_recipient
            ):
                raise ValueError("请输入有效的邮件收件地址")
        return self


class Settings(BaseModel):
    collection_seconds: int | None = Field(default=None, ge=15, le=3600)
    catalog_seconds: int | None = Field(default=None, ge=60, le=86400)
    stock_seconds: int | None = Field(default=None, ge=15, le=3600)
    task_stock_seconds: int | None = Field(default=None, ge=5, le=300)
    retention_days: int | None = Field(default=None, ge=1, le=365)
    idle_minutes: int | None = Field(default=None, ge=1, le=1440)
    idle_threshold: float | None = Field(default=None, ge=0, le=100)
    idle_warmup_minutes: int | None = Field(default=None, ge=0, le=120)
    idle_channels: list[Channel] | None = Field(default=None, max_length=3)
    idle_email_recipient: str | None = Field(default=None, max_length=256)
    idle_schedule: IdleSchedule | None = None
    notification_repeat_minutes: NotificationRepeatMinutes | None = None
    notification_rules: NotificationRules | None = None
    feishu_event_policy: FeishuEventPolicy | None = None
    project_id: Id | None = None


class TestNotification(BaseModel):
    channel: Channel
    recipient: str | None = Field(default=None, max_length=256)


class FeishuWebhookDraft(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    mode: Literal["webhook"] = "webhook"
    webhook_url: str | None = Field(default=None, max_length=2048, repr=False)
    signing_secret: str | None = Field(default=None, min_length=1, max_length=256, repr=False)
    keyword: str = Field(max_length=100)


class FeishuAppDraft(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    mode: Literal["app"]
    app_id: str = Field(min_length=1, max_length=128)
    app_secret: str | None = Field(default=None, min_length=1, max_length=256, repr=False)
    receive_id_type: FeishuReceiveIdType
    receive_id: str = Field(min_length=1, max_length=254)


def request_refresh(conn: Connection, kind: str, scope: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO refresh_requests(kind,scope,requested_at) VALUES (?,?,?)",
        (kind, scope, storage.now()),
    )


def clear_watch_stock(conn: Connection, watch_id: str) -> None:
    conn.execute("DELETE FROM snapshots WHERE kind='stock' AND scope=?", (watch_id,))
    conn.execute("DELETE FROM refresh_requests WHERE kind='stock' AND scope=?", (watch_id,))
    refresh_store.abandon_watch(conn, watch_id)
    conn.execute("DELETE FROM stock_history WHERE watch_id=?", (watch_id,))
    conn.execute("DELETE FROM stock_notice_baselines WHERE watch_id=?", (watch_id,))
    availability.remove_watch(conn, watch_id)


@router.get("/projects")
def projects(conn: Db) -> dict:
    return storage.snapshot(conn, "projects")


@router.get("/catalog")
def catalog(conn: Db, project_id: Id | None = None) -> dict:
    with storage.transaction(conn, immediate=False):
        return watch_service.catalog_view(conn, storage.now(), project_id)


@router.get("/inventory/pools")
def inventory_pools(conn: Db) -> dict:
    return pool_snapshot(conn, storage.now())


@router.get("/inventory/overview")
def read_inventory_overview(conn: Db, project_id: Id | None = None, include_spot: bool = False) -> dict:
    with storage.transaction(conn, immediate=False):
        return inventory_overview.overview(conn, project_id, storage.now(), include_spot=include_spot)


@router.post("/inventory/prices")
def request_inventory_price(body: InventoryPriceRequest, conn: Db) -> dict:
    with storage.transaction(conn):
        return inventory_prices.enqueue(conn, body.project_id, body.spec_id, body.charge_type, storage.now())


@router.get("/inventory/prices/{spec_id}")
def read_inventory_price(spec_id: CatalogSpecId, project_id: Id, charge_type: inventory_prices.ChargeType, conn: Db) -> dict:
    return inventory_prices.result(conn, project_id, spec_id, charge_type, storage.now())


@router.get("/instances")
def instances(project_id: Id, conn: Db) -> dict:
    snapshot = storage.snapshot(conn, "instances", project_id)
    overrides = dict(conn.execute(
        "SELECT instance_id,url FROM instance_jupyter_overrides WHERE project_id=?",
        (project_id,),
    ))
    archived = {row[0] for row in conn.execute(
        "SELECT instance_id FROM instance_history WHERE project_id=? AND state='released'", (project_id,),
    )}
    rows = [row | {"project_id": project_id} for row in snapshot["data"]
            if row.get("project_id", project_id) == project_id and row["id"] not in archived
            and row.get("state") not in instance_history.TERMINAL]
    now = storage.now()
    stock = instance_assets.project_stock(conn, rows, now)
    active = instance_assets.active_operations(conn, project_id, now)
    registered = cardless.profiles(conn)
    snapshot["data"] = [
        row | {"jupyter": jupyter_link(row, overrides.get(row["id"])),
               "stock": stock.get(row["id"]), "active_operation": active.get(row["id"]),
               "cardless_profiles": cardless.options_for(row, registered)}
        for row in rows
    ]
    return snapshot


def instance_from_snapshot(conn: Connection, project_id: str, instance_id: str) -> tuple[dict, dict]:
    if instance_history.released(conn, project_id, instance_id):
        raise HTTPException(404, "该实例已释放")
    snapshot = storage.snapshot(conn, "instances", project_id)
    for item in snapshot["data"]:
        if item.get("id") == instance_id and item.get("project_id", project_id) == project_id:
            return item, snapshot
    if snapshot["success_at"] is None or snapshot["error"]:
        raise HTTPException(503, "实例清单待同步，请刷新实例")
    raise HTTPException(404, "项目快照中找不到该实例")


def jupyter_link(instance: dict, override: str | None = None) -> dict:
    if url := safe_instance_url(override):
        return {"url": url, "source": "custom"}
    for link in instance.get("software_links") or ():
        if link["name"].strip().casefold() == "jupyterlab" and (url := safe_instance_url(link["url"])):
            return {"url": url, "source": "platform"}
    return {"url": None, "source": None}


@router.get("/instances/{instance_id}")
def instance_detail(instance_id: Id, project_id: Id, conn: Db) -> dict:
    archived = instance_history.get(conn, project_id, instance_id)
    if archived:
        instance, snapshot = archived, {"success_at": archived["archived_at"], "error": None}
    else:
        instance, snapshot = instance_from_snapshot(conn, project_id, instance_id)
    last_sample = conn.execute(
        "SELECT MAX(sampled_at) FROM metrics WHERE project_id=? AND instance_id=?",
        (project_id, instance_id),
    ).fetchone()
    override = conn.execute(
        "SELECT url FROM instance_jupyter_overrides WHERE project_id=? AND instance_id=?",
        (project_id, instance_id),
    ).fetchone()
    return {
        "data": instance if archived else instance | {"cardless_profiles": cardless.options_for(instance, cardless.profiles(conn))},
        "success_at": snapshot["success_at"],
        "error": snapshot["error"],
        "last_sample_at": last_sample[0],
        **({"shutdown_cleanup_pending": True} if shutdown_cleanup_pending(conn, project_id, instance_id) else {}),
        "jupyter": {"url": None, "source": None} if archived else jupyter_link(instance, override["url"] if override else None),
        "idle_snooze": None if archived else notice_policies.snooze(conn, project_id, instance_id, storage.now()),
    }


@router.put("/instances/{instance_id}/idle-snooze")
def pause_idle_notices(instance_id: Id, project_id: Id, body: IdleSnooze, conn: Db) -> dict:
    now = storage.now()
    if body.until is not None and body.until <= now:
        raise HTTPException(422, "请选择将来的恢复时间")
    with storage.transaction(conn):
        instance_from_snapshot(conn, project_id, instance_id)
        notice_policies.set_snooze(conn, project_id, instance_id, body.until)
    return {"paused": True, "until": body.until}


@router.delete("/instances/{instance_id}/idle-snooze")
def resume_idle_notices(instance_id: Id, project_id: Id, conn: Db) -> dict:
    with storage.transaction(conn):
        instance_from_snapshot(conn, project_id, instance_id)
        now = storage.now()
        conn.execute(
            """UPDATE idle_snoozes SET until=? WHERE project_id=? AND instance_id=?
               AND (until IS NULL OR until>?)""",
            (now, project_id, instance_id, now),
        )
    return {"paused": False, "until": None}


@router.post("/instances/{instance_id}/ssh-password")
def reveal_ssh_password(instance_id: Id, project_id: Id, request: Request, conn: Db) -> dict[str, str]:
    instance, _ = instance_from_snapshot(conn, project_id, instance_id)
    if instance.get("login_mode") in ("KeyPair", "SSHKey"):
        raise HTTPException(404, "此实例使用 SSH 密钥登录")
    region, zone = instance.get("region"), instance.get("zone")
    if not region or not zone:
        raise HTTPException(503, "实例地域或可用区待同步，请刷新实例")
    try:
        password = fetch_ssh_password(request.app.state.config.data_dir, project_id, region, zone, instance_id)
    except PasswordServiceBusy:
        raise HTTPException(429, "正在查询 SSH 密码，请稍后再试") from None
    except PasswordServiceUnavailable:
        raise HTTPException(503, "暂时无法查询 SSH 密码，请稍后再试") from None
    if password is None:
        raise HTTPException(404, "平台未返回可显示的 SSH 密码，请在优云智算控制台核对连接信息")
    return {"password": password}


@router.put("/instances/{instance_id}/jupyter")
def save_jupyter_link(instance_id: Id, project_id: Id, body: JupyterOverride, conn: Db) -> dict:
    url = safe_instance_url(body.url)
    if url is None:
        raise HTTPException(422, "请输入有效的 HTTP 或 HTTPS 地址，地址中不能包含登录信息、查询参数或片段")
    with storage.transaction(conn):
        instance_from_snapshot(conn, project_id, instance_id)
        conn.execute(
            """INSERT INTO instance_jupyter_overrides(project_id,instance_id,url) VALUES (?,?,?)
               ON CONFLICT(project_id,instance_id) DO UPDATE SET url=excluded.url""",
            (project_id, instance_id, url),
        )
    return {"url": url, "source": "custom"}


@router.delete("/instances/{instance_id}/jupyter")
def clear_jupyter_link(instance_id: Id, project_id: Id, conn: Db) -> dict:
    with storage.transaction(conn):
        instance, _ = instance_from_snapshot(conn, project_id, instance_id)
        conn.execute(
            "DELETE FROM instance_jupyter_overrides WHERE project_id=? AND instance_id=?",
            (project_id, instance_id),
        )
        return jupyter_link(instance)


@router.get("/instances/{instance_id}/metrics")
def metrics(
    instance_id: Id, project_id: Id, conn: Db, since: int = 0, until: int | None = None
) -> dict:
    until = until or storage.now()
    settings = storage.get_settings(conn)
    since = max(since or (until - 3600), until - settings["retention_days"] * 86400)
    interval = max(1, (until - since) // 1500)
    rows = conn.execute(
        """SELECT metric,label,MAX(sampled_at) AS sampled_at,AVG(value) AS value
           FROM metrics WHERE project_id=? AND instance_id=? AND sampled_at>=? AND sampled_at<=?
           GROUP BY metric,label,sampled_at / ? ORDER BY MAX(sampled_at)""",
        (project_id, instance_id, since, until, interval),
    )
    meta = storage.snapshot(conn, "metrics", project_id)
    return meta | {"data": [dict(row) for row in rows]}


@router.get("/stock")
def stock(project_id: Id, conn: Db) -> dict:
    with storage.transaction(conn, immediate=False):
        data = watch_service.list_project(conn, project_id, storage.now(), history=True)
        checked_at = max((row["success_at"] for row in data if row["success_at"] is not None), default=None)
        return {"success_at": checked_at, "error": None, "data": data}


@router.post("/refresh", status_code=202)
def refresh(body: Refresh, conn: Db) -> dict:
    if body.watch_id and body.kind != "stock":
        raise HTTPException(422, "关注项只适用于规格检测")
    if body.kind in {"instances", "stock"} and not body.project_id:
        raise HTTPException(422, "需要明确的项目 ID")
    if body.kind == "pool" and body.project_id is not None:
        raise HTTPException(422, "平台余量同步无需指定项目")
    with storage.transaction(conn):
        if body.kind == "stock":
            scopes = [row["id"] for row in conn.execute(
                "SELECT id FROM watches WHERE project_id=? AND (? IS NULL OR id=?) ORDER BY id",
                (body.project_id, body.watch_id, body.watch_id),
            )]
            if body.watch_id and not scopes:
                raise HTTPException(404, "此项目下没有该关注项")
        elif body.kind == "instances":
            scopes = [body.project_id]
        else:
            scopes = [""]
        for scope in scopes:
            request_refresh(conn, body.kind, scope)
        batch_id = refresh_store.submit(conn, body.kind, body.project_id, scopes)
    return {"queued": True, "id": batch_id}


@router.get("/refresh/{batch_id}")
def refresh_receipt(batch_id: Id, conn: Db) -> dict:
    receipt = refresh_store.get(conn, batch_id)
    if receipt is None:
        raise HTTPException(404, "同步请求不存在或已经过期")
    return receipt


@router.get("/watches")
def watches(project_id: Id, conn: Db) -> list:
    with storage.transaction(conn, immediate=False):
        return watch_service.list_project(conn, project_id, storage.now())


@router.post("/watches", status_code=201)
def create_watch(body: Watch, conn: Db, response: Response) -> dict:
    with storage.transaction(conn):
        item, created = watch_service.save(conn, body.project_id, body.name, body.catalog_spec_id, body.spec)
        if not created:
            response.status_code = 200
        return item | {"created": created}


@router.put("/watches/{watch_id}")
def rebind_watch(watch_id: Id, body: Watch, conn: Db) -> dict:
    with storage.transaction(conn):
        item, _ = watch_service.save(conn, body.project_id, body.name, body.catalog_spec_id, body.spec,
                                     watch_id=watch_id, expected_version=body.expected_version)
        return item


@router.delete("/watches/{watch_id}")
def delete_watch(watch_id: Id, conn: Db) -> dict:
    with storage.transaction(conn):
        if not conn.execute("DELETE FROM watches WHERE id=?", (watch_id,)).rowcount:
            raise HTTPException(404, "关注项不存在")
        clear_watch_stock(conn, watch_id)
    return {"deleted": True}


@router.get("/templates")
def templates(project_id: Id, conn: Db) -> list:
    return [
        dict(row) | {"config": json.loads(row["config"])}
        for row in conn.execute("SELECT * FROM templates WHERE project_id=? ORDER BY name,id", (project_id,))
    ]


def template_definition(conn: Connection, project_id: str, config: dict) -> dict:
    from gpu_console.application.purchase_options import normalize_purchase_config

    validated = validate_spec(config, purchasing=False) | {
        "skip_if_match": optional_bool(config, "skip_if_match", True),
        **{key: config[key] for key in ("zone_storage", "shared_storage") if key in config},
    }
    return normalize_purchase_config(conn, project_id, validated)


@router.post("/templates", status_code=201)
def create_template(body: Template, conn: Db) -> dict:
    item = body.model_dump() | {"id": uuid4().hex, "version": 1,
                               "config": template_definition(conn, body.project_id, body.config)}
    conn.execute(
        "INSERT INTO templates VALUES (?,?,?,?,?)",
        (item["id"], body.project_id, body.name, json.dumps(item["config"]), 1),
    )
    return item


@router.put("/templates/{template_id}")
def update_template(template_id: Id, body: Template, conn: Db) -> dict:
    definition = template_definition(conn, body.project_id, body.config)
    row = conn.execute(
        """UPDATE templates SET name=?,config=?,version=version+1 WHERE id=? AND project_id=?
           RETURNING version""",
        (body.name, json.dumps(definition), template_id, body.project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "模板不存在")
    return body.model_dump() | {"id": template_id, "version": row["version"], "config": definition}


@router.get("/settings")
def settings(request: Request, conn: Db) -> dict:
    status = {row["component"]: dict(row) for row in conn.execute("SELECT * FROM system_status")}
    config = request.app.state.config
    feishu, feishu_configured = feishu_status(request, conn)
    values = storage.get_settings(conn)
    values.pop("feishu_policy_since", None)
    return values | {
        "status": status,
        "read_only": config.read_only,
        "feishu": feishu,
        "recovery": [
            dict(row) for row in conn.execute("SELECT * FROM recovery_log ORDER BY id DESC LIMIT 10")
        ],
        "channels": {
            "site": True,
            "feishu": feishu_configured,
            "email": status.get("channel_email", {}).get("error") == "configured",
        },
    }


def feishu_status(request: Request, conn: Db) -> tuple[dict | None, bool]:
    return configured_feishu(request.app.state.config, conn)


def configured_feishu(config: Config, conn: Connection) -> tuple[dict | None, bool]:
    try:
        feishu = read_feishu_settings(config.secrets_dir)
    except FeishuSettingsUnavailable:
        return None, False
    if isinstance(feishu, FeishuAppSettings):
        return {
            "mode": "app",
            "app_id": feishu.app_id,
            "receive_id_type": feishu.receive_id_type,
            "receive_id": feishu.receive_id,
        }, True
    if feishu is not None:
        return {"signing_configured": bool(feishu.signing_secret), "keyword": feishu.keyword}, True
    row = conn.execute("SELECT error FROM system_status WHERE component='channel_feishu'").fetchone()
    return None, row is not None and row["error"] == "configured"


@router.put("/settings/feishu")
def update_feishu_settings(body: FeishuWebhookDraft | FeishuAppDraft, request: Request, conn: Db) -> dict:
    try:
        save_feishu_settings(request.app.state.config.secrets_dir, body.model_dump(exclude_unset=True))
    except OSError:
        raise HTTPException(503, "飞书配置目录不可写，请检查部署配置") from None
    return settings(request, conn)


@router.patch("/settings")
def update_settings(body: Settings, request: Request, conn: Db) -> dict:
    changes = body.model_dump(exclude_unset=True)
    if any(value is None and key not in ("project_id", "idle_email_recipient") for key, value in changes.items()):
        raise HTTPException(422, "此设置需要有效值")
    with storage.transaction(conn):
        storage.set_settings(conn, changes)
        if changes.get("project_id"):
            request_refresh(conn, "instances", changes["project_id"])
    return settings(request, conn)


@router.get("/notifications")
def notifications(
    conn: Db, limit: int = Query(100, ge=1, le=500), *, paged: bool = False,
    channel: Literal["site", "feishu", "email", "external"] | None = None,
    project_id: Id | None = None, event_type: NoticeEvent | None = None,
    event_id: Annotated[str, Query(min_length=1, max_length=500)] | None = None,
    cursor: Annotated[int, Query(gt=0, le=2**63 - 1)] | None = None,
) -> list | dict:
    page = list_notifications(
        conn, limit=limit, channel=channel, project_id=project_id,
        event_type=event_type, event_id=event_id, cursor=cursor,
    )
    if paged or any(value is not None for value in (channel, project_id, event_type, event_id, cursor)):
        return page
    return page["items"]


@router.post("/notifications/test", status_code=202)
def test_notification(body: TestNotification, request: Request, conn: Db) -> dict:
    if body.channel == "email" and not body.recipient:
        raise HTTPException(422, "请填写收件地址")
    if body.channel == "feishu" and not feishu_status(request, conn)[1]:
        raise HTTPException(409, "请先在设置中保存飞书机器人配置")
    event_id = f"test-{uuid4().hex}"
    storage.emit(conn, event_id, {"title": "GPU 实验管理台测试通知", "recipient": body.recipient}, [body.channel])
    return {"event_id": event_id}


@router.post("/notifications/{notification_id}/retry", status_code=202)
def retry_notification(notification_id: int, conn: Db) -> dict:
    if not conn.execute(
        """UPDATE notifications SET state='pending',next_at=?,error=NULL,attempts=0
           WHERE id=? AND channel<>'site' AND state='failed'""",
        (storage.now(), notification_id),
    ).rowcount:
        raise HTTPException(409, "只有失败的外部通知可以重试")
    return {"queued": True}


@router.post("/notifications/{notification_id}/read")
def read_notification(notification_id: int, conn: Db) -> dict:
    if not conn.execute(
        "UPDATE notifications SET read_at=COALESCE(read_at,?) WHERE id=? AND channel='site'",
        (storage.now(), notification_id),
    ).rowcount:
        raise HTTPException(404, "站内通知不存在")
    return {"read": True}


@router.get("/instance-history")
def archived_instances(project_id: Id, conn: Db) -> dict:
    return {"data": instance_history.list_project(conn, project_id), "success_at": storage.now(), "error": None}


@router.get("/instance-storage")
def instance_storage(project_id: Id, conn: Db) -> dict:
    with storage.transaction(conn, immediate=False):
        return instance_assets.read_storage(conn, project_id, storage.now())
