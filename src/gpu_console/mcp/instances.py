"""Publish instance operation tools backed by the existing task queue."""

import json
from sqlite3 import Connection, Row
from typing import Annotated, Literal, TypedDict
from uuid import uuid4

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from gpu_console import storage
from gpu_console.api.resources import configured_feishu
from gpu_console.application import instance_quotes, instance_release
from gpu_console.application.purchase_options import create_options
from gpu_console.application.task_config import BootMode, CardlessSpec
from gpu_console.application.tasks import TaskStore
from gpu_console.config import Config
from gpu_console.mcp.common import issue_preview, mutate, read_preview, require_manager
from gpu_console.mcp.server import ConsoleMCPServer

Id = Annotated[str, Field(min_length=1, max_length=100)]
Timestamp = Annotated[int, Field(strict=True, gt=0, le=2_147_483_647, description="北京时间对应的 Unix 秒")]
ReleaseOperation = "gpu_release_instance"
PurchaseRetryOperation = "gpu_submit_instance_purchase_request"
QuoteState = Literal["queued", "ready", "failed", "expired", "used"]


class QuoteDisk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: Literal["system", "data"]
    type: Id
    size_gb: int = Field(strict=True, ge=1, le=1_000_000)


class InstanceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region: Id
    zone: Id
    gpu_model: Id
    gpu: int = Field(strict=True, ge=1, le=64)
    cpu: int = Field(strict=True, ge=1, le=4096)
    memory_gb: int = Field(strict=True, ge=1, le=65536)
    cpu_platform: SkipJsonSchema[Id | None] = None
    image_id: Id
    charge_type: Literal["Postpay", "Spot", "Day", "Month"]
    quantity: int | None = Field(default=None, strict=True, ge=1, le=3650, description="按天或包月时必填的计费周期数")
    disks: list[QuoteDisk] = Field(default_factory=list, max_length=10, description="可选数据盘，系统盘由服务器解析")
    shared_storage: dict | None = None


class RunReceipt(TypedDict):
    run_id: str
    project_id: str
    instance_id: str | None
    state: str
    phase: str | None
    reason: str | None


class QuoteReceipt(TypedDict):
    quote_id: str
    project_id: str
    state: QuoteState
    spec: dict
    price: str | None
    price_unit: Literal["CNY/order", "CNY/hour"]
    quoted_at: int | None
    expires_at: int | None
    run_id: str | None
    reason: str | None


class CreateOptions(TypedDict):
    system_disk: dict | None
    system_disk_backing: Literal["cloud", "local"] | None
    data_disks: list[dict]
    reason: str | None
    storage: Literal["us3", "cfs", "none"]
    can_mount: bool
    cfs: dict | None
    cfs_known: bool


class PurchaseRetryQuote(TypedDict):
    preview_id: str
    project_id: str
    configuration: dict
    expires_in_seconds: int


class ReleasePreview(TypedDict):
    instance: dict
    blockers: dict[str, list[str]]
    consequences: str
    preview_id: str | None
    expires_at: int | None


def _run_receipt(run: dict) -> RunReceipt:
    return {
        "run_id": run["id"], "project_id": run["project_id"],
        "instance_id": run.get("result", {}).get("instance_id") or run["snapshot"]["config"].get("instance_id"),
        "state": run["state"],
        "phase": run["phase"], "reason": run["reason"],
    }


def _get_quote(conn: Connection, project_id: str, quote_id: str) -> Row:
    quote = instance_quotes.get(conn, quote_id)
    if quote["project_id"] != project_id:
        raise KeyError("指定项目中找不到该报价")
    return quote


def _quote_receipt(row: Row, now: int) -> QuoteReceipt:
    quote = instance_quotes.view(row, now)
    config = json.loads(row["config"])
    return {
        "quote_id": quote["id"], "project_id": quote["project_id"], "state": quote["state"],
        "spec": quote["spec"], "price": quote["price"],
        "price_unit": "CNY/order" if config["charge_type"] in {"Day", "Month"} else "CNY/hour",
        "quoted_at": quote["quoted_at"], "expires_at": quote["expires_at"],
        "run_id": quote["run_id"], "reason": quote["reason"],
    }


def _action(
    config: Config, project_id: str, instance_id: str, kind: str, request_id: str, **fields: object,
) -> RunReceipt:
    action = {"project_id": project_id, "kind": kind, "config": {"instance_id": instance_id, **fields}}

    def submit(_conn: Connection) -> dict:
        return _run_receipt(TaskStore(config.db).submit_action(
            action, f"mcp:{uuid4().hex}", read_only=config.read_only, use_snapshot_scope=True,
        ))

    return mutate(config, f"instance_{kind}", request_id, action, submit)


def register(server: ConsoleMCPServer, config: Config) -> None:
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
    destructive = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

    @server.scoped_tool("manage", name="gpu_get_instance_create_options", annotations=write)
    def get_instance_create_options(
        project_id: Id, region: Id, zone: Id, gpu_model: Id, instance_type: Literal["uhost", "spot"], image_id: Id,
    ) -> CreateOptions:
        """查看当前配置的已核实系统盘、可选数据盘与同区共享存储能力。"""
        with storage.connect(config.db) as conn, storage.transaction(conn):
            require_manager(conn)
            return create_options(conn, project_id, region, zone, gpu_model, instance_type, image_id, queue_cfs=True)

    @server.scoped_tool("manage", name="gpu_preview_instance_purchase_request", annotations=read)
    def preview_instance_purchase_request(
        project_id: Id, spec: InstanceSpec, max_price: Id,
        window_seconds: Annotated[int, Field(strict=True, ge=1, le=604800)] = 86400,
        channels: list[Literal["site", "feishu", "email"]] | None = None,
        email: str | None = None, retry: bool = True,
    ) -> PurchaseRetryQuote:
        """预览一台实例的待确认购买申请。告诉用户已解析的磁盘、报价上限、期限和存储费用，再请求明确确认。"""
        from gpu_console.application.tasks import prepare_purchase_retry

        body = {
            "project_id": project_id, "spec": spec.model_dump(exclude_none=True), "max_price": max_price,
            "window_seconds": window_seconds, "channels": channels or ["site"], "email": email, "retry": retry,
        }
        with storage.connect(config.db) as conn:
            snapshot = prepare_purchase_retry(conn, body)
        preview_id = issue_preview(config, PurchaseRetryOperation, {"snapshot": snapshot})
        return {"preview_id": preview_id, "project_id": project_id, "configuration": snapshot,
                "expires_in_seconds": 300}

    @server.scoped_tool("manage", name=PurchaseRetryOperation, annotations=destructive)
    def submit_instance_purchase_request(project_id: Id, preview_id: Id, request_id: Id) -> RunReceipt:
        """经用户确认后提交实例购买申请。它可在预览上限内购买一台实例及已授权的存储变更。"""
        def submit(conn: Connection) -> RunReceipt:
            snapshot = read_preview(conn, PurchaseRetryOperation, preview_id)["snapshot"]
            if snapshot["project_id"] != project_id:
                raise ValueError("预览与指定项目不符")
            store = TaskStore(config.db, feishu_configured=lambda db: configured_feishu(config, db)[1])
            return _run_receipt(store.submit_purchase_retry_snapshot(
                snapshot, f"mcp:{uuid4().hex}", read_only=config.read_only,
            ))

        return mutate(config, PurchaseRetryOperation, request_id, {"project_id": project_id, "preview_id": preview_id}, submit)

    @server.scoped_tool("manage", name="gpu_request_instance_quote", annotations=write)
    def request_instance_quote(project_id: Id, spec: InstanceSpec, request_id: Id) -> QuoteReceipt:
        """排队获取单台 GPU 实例的库存和实时报价。以返回的 quote_id 查询报价，再经用户确认提交购买。"""
        options = spec.model_dump(exclude_none=True)

        def create_quote(conn: Connection) -> dict:
            now = storage.now()
            quote = instance_quotes.create(conn, project_id, options, now)
            return _quote_receipt(instance_quotes.get(conn, quote["id"]), now)

        return mutate(config, "request_instance_quote", request_id,
                      {"project_id": project_id, "spec": options}, create_quote, allow_read_only=True)

    @server.scoped_tool("read", name="gpu_get_instance_quote", annotations=read)
    def get_instance_quote(project_id: Id, quote_id: Id) -> QuoteReceipt:
        """查询指定项目的一次报价，返回规格、价格、计费单位、期限和是否已被使用。"""
        with storage.connect(config.db) as conn:
            return _quote_receipt(_get_quote(conn, project_id, quote_id), storage.now())

    @server.scoped_tool("manage", name="gpu_create_instance", annotations=destructive)
    def create_instance(
        project_id: Id, quote_id: Id, confirmed_price: Id, request_id: Id,
        channels: list[Literal["site", "feishu", "email"]] | None = None, email: str | None = None,
    ) -> RunReceipt:
        """按用户确认的单台实例报价提交付费创建。confirmed_price 必须与报价中的金额字符串一致。返回运行 ID。"""
        def submit(conn: Connection) -> dict:
            quote = _get_quote(conn, project_id, quote_id)
            if quote["price"] != confirmed_price:
                raise ValueError("确认金额与报价不一致，请重新核对报价")
            store = TaskStore(config.db, feishu_configured=lambda db: configured_feishu(config, db)[1])
            return _run_receipt(store.submit_purchase(
                quote_id, f"mcp:{uuid4().hex}", read_only=config.read_only, channels=channels, email=email,
            ))

        payload = {
            "project_id": project_id, "quote_id": quote_id, "confirmed_price": confirmed_price,
        } | {key: value for key, value in {"channels": channels, "email": email}.items() if value is not None}
        return mutate(config, "create_instance", request_id, payload, submit)

    @server.scoped_tool("manage", name="gpu_start_instance", annotations=write)
    def start_instance(project_id: Id, instance_id: Id, request_id: Id,
                       boot_mode: BootMode | None = None,
                       without_gpu_spec: CardlessSpec | None = None) -> RunReceipt:
        """请求启动现有实例。默认有卡；无卡需选择实例可用的配置。实际运行模式和结果以回读为准。"""
        fields = {key: value for key, value in {"boot_mode": boot_mode, "without_gpu_spec": without_gpu_spec}.items()
                  if value is not None and (key != "boot_mode" or value != "with_gpu")}
        return _action(config, project_id, instance_id, "start", request_id, **fields)

    @server.scoped_tool("manage", name="gpu_stop_instance", annotations=destructive)
    def stop_instance(project_id: Id, instance_id: Id, request_id: Id) -> RunReceipt:
        """请求关机并停止本实例当前尚未提交的预约开机尝试。返回运行 ID。"""
        return _action(config, project_id, instance_id, "stop", request_id)

    @server.scoped_tool("manage", name="gpu_reboot_instance", annotations=destructive)
    def reboot_instance(project_id: Id, instance_id: Id, request_id: Id) -> RunReceipt:
        """请求重启一台运行中的实例。重启可能中断训练和连接。返回运行 ID。"""
        return _action(config, project_id, instance_id, "reboot", request_id)

    @server.scoped_tool("manage", name="gpu_rename_instance", annotations=write)
    def rename_instance(project_id: Id, instance_id: Id, expected_name: Id, name: Id, request_id: Id) -> RunReceipt:
        """请求把指定实例改为新名称，并在后台核对原名称。名称长度限制为 63 字符。"""
        return _action(config, project_id, instance_id, "rename", request_id, name=name, expected_name=expected_name)

    @server.scoped_tool("manage", name="gpu_schedule_instance_shutdown", annotations=destructive)
    def schedule_shutdown(
        project_id: Id, instance_id: Id, stop_at: Timestamp, request_id: Id, expected_stop_at: Timestamp | None = None,
    ) -> RunReceipt:
        """设置云平台关机预约，时间为 Unix 秒。替换现有预约时给出实际确认过的原预约时间。"""
        return _action(config, project_id, instance_id, "shutdown", request_id,
                       shutdown_at=stop_at, replace=expected_stop_at is not None, expected_stop_at=expected_stop_at)

    @server.scoped_tool("manage", name="gpu_cancel_instance_shutdown", annotations=write)
    def cancel_shutdown(project_id: Id, instance_id: Id, expected_stop_at: Timestamp, request_id: Id) -> RunReceipt:
        """取消与用户确认时间相符的云平台关机预约，保留其他预约。返回运行 ID。"""
        return _action(config, project_id, instance_id, "cancel_shutdown", request_id, expected_stop_at=expected_stop_at)

    @server.scoped_tool("manage", name="gpu_prepare_instance_release", annotations=read)
    def prepare_release(project_id: Id, instance_id: Id) -> ReleasePreview:
        """预览释放一台已关机的普通虚机，核对计费范围并列出关联任务、规则和未决运行。"""
        now = storage.now()
        with storage.connect(config.db) as conn:
            target = instance_release.from_snapshot(conn, project_id, instance_id, now)
            instance_release.check_target(target, config.mcp_release_charge_types)
            blockers = instance_release.blockers(conn, project_id, instance_id)
        instance = instance_release.identity(target)
        result: ReleasePreview = {
            "instance": instance, "blockers": blockers, "preview_id": None, "expires_at": None,
            "consequences": "释放后实例将从活动清单移除，实例数据可能丢失。磁盘、回收站和费用结果以云平台为准。",
        }
        if not any(blockers.values()):
            expires_at = now + instance_release.PREVIEW_SECONDS
            result.update(expires_at=expires_at, preview_id=issue_preview(config, ReleaseOperation, {
                "instance": instance, "expires_at": expires_at,
            }, ttl=instance_release.PREVIEW_SECONDS))
        return result

    @server.scoped_tool("manage", name=ReleaseOperation, annotations=destructive)
    def release_instance(project_id: Id, instance_id: Id, preview_id: Id, request_id: Id) -> RunReceipt:
        """按用户确认的预览提交一台指定实例的释放。返回运行 ID，通过回读确认已受理与实际结果。"""
        def submit(conn: Connection) -> dict:
            preview = read_preview(conn, ReleaseOperation, preview_id)
            instance = preview["instance"]
            if (instance["project_id"], instance["id"]) != (project_id, instance_id):
                raise ValueError("释放预览与指定项目或实例不符")
            return _run_receipt(instance_release.enqueue(
                conn, instance, preview["expires_at"], f"action:mcp:{uuid4().hex}",
                storage.now(), config.mcp_release_charge_types,
            ))

        return mutate(config, ReleaseOperation, request_id, {
            "project_id": project_id, "instance_id": instance_id, "preview_id": preview_id,
        }, submit)
