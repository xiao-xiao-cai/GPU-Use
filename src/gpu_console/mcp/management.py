"""Manage console task definitions, rules, inventory watches and notifications."""

import json
from hashlib import sha256
from sqlite3 import Connection
from typing import Annotated, Any, Literal
from uuid import uuid4

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt
from pydantic.json_schema import SkipJsonSchema

from gpu_console import storage
from gpu_console.api.automation_rules import Rule, RuleConfig
from gpu_console.api.resources import (
    Channel, Id, IdleSchedule, Name, NoticeEvent, NotificationRepeatMinutes,
    NotificationRules, clear_watch_stock, configured_feishu, template_definition,
)
from gpu_console.application import automation_rules, watches
from gpu_console.application.feishu_notices import FeishuEventPolicy
from gpu_console.application.notifications import PROJECT_ID, list_notifications
from gpu_console.application.task_config import BootMode, CardlessSpec, SHANGHAI, next_time, run_deadline, shanghai_time, validate_config
from gpu_console.application.tasks import (
    TaskListView, TaskStore, TaskTrashAction, encode, require_row, resolve_template, task_snapshot, task_view, termination_pending,
)
from gpu_console.config import Config
from gpu_console.mcp.auth import require_principal
from gpu_console.mcp.common import issue_preview, mutate, page, read_preview
from gpu_console.mcp.server import ConsoleMCPServer

Limit = Annotated[int, Field(ge=1, le=100)]
Offset = Annotated[int, Field(ge=0)]
Version = Annotated[int, Field(strict=True, ge=1)]
Seconds = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
NotificationId = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
EventId = Annotated[str, Field(min_length=1, max_length=500)]
TaskOperation = Literal["create", "update", "resume", "trigger"]
TaskKind = Literal["stock", "start", "acquire", "stop", "shutdown", "inspection"]
TaskEvent = Literal["acquisition_succeeded", "acquisition_expired", "shutdown_result", "task_result", "idle"]
ChargeType = Literal["Postpay", "Spot", "Day", "Month"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Zone(Input):
    region: Id
    zone: Id


class Disk(Input):
    purpose: Literal["system", "data"]
    type: Id
    size_gb: StrictInt


class MachineSpec(Input):
    gpu_model: Id
    gpu: StrictInt
    cpu: StrictInt
    memory_gb: StrictInt
    cpu_platform: SkipJsonSchema[Id | None] = None
    image_id: Id
    disks: list[Disk] = Field(default_factory=list)
    charge_type: ChargeType
    quantity: StrictInt | None = None


class ZoneStorage(Zone):
    disks: list[Disk] = Field(default_factory=list, max_length=10)
    shared_storage: dict | None = None


class TemplateSpec(MachineSpec):
    zones: list[Zone]
    zone_storage: list[ZoneStorage] | None = None
    shared_storage: dict | None = None
    skip_if_match: bool = True


class WatchSpec(MachineSpec, Zone):
    pass


class TaskSchedule(Input):
    type: Literal["once", "daily", "weekly", "interval"]
    at: Seconds = Field(description="计划首次执行的 Unix 秒时间戳")
    interval_seconds: StrictInt | None = None


class CapacityRetry(Input):
    interval_seconds: StrictInt


class TaskConfig(Input):
    instance_id: Id | None = None
    region: Id | None = None
    zone: Id | None = None
    zones: list[Zone] | None = None
    gpu_model: Id | None = None
    gpu: StrictInt | None = None
    cpu: StrictInt | None = None
    memory_gb: StrictInt | None = None
    cpu_platform: SkipJsonSchema[Id | None] = None
    image_id: Id | None = None
    disks: list[Disk] | None = None
    zone_storage: list[ZoneStorage] | None = None
    shared_storage: dict | None = None
    charge_type: ChargeType | None = None
    quantity: StrictInt | None = None
    max_price: str | StrictInt | StrictFloat | None = None
    purchase_limit: StrictInt | None = None
    skip_if_match: bool | None = None
    capacity_retry: CapacityRetry | None = None
    boot_mode: BootMode | None = None
    without_gpu_spec: CardlessSpec | None = None
    shutdown_after_seconds: Seconds | None = None
    shutdown_at: Seconds | None = None
    replace: bool | None = None
    expected_stop_at: Seconds | None = None
    email: str | None = None


class TaskDefinition(Input):
    name: Name
    kind: TaskKind
    schedule: TaskSchedule
    config: TaskConfig = Field(default_factory=TaskConfig)
    channels: list[Channel] = Field(default_factory=lambda: ["site"], max_length=3)
    notification_events: list[TaskEvent] | None = None
    window_seconds: Annotated[int, Field(strict=True, ge=1, le=604800)] = 7200
    template_id: Id | None = None
    template_version: Version | None = None


class RuleDefinition(Input):
    name: Name
    instance_id: Id
    kind: Literal["idle_stop", "runtime_stop", "cardless_idle_stop"]
    config: RuleConfig
    channels: list[Channel] = Field(default_factory=lambda: ["site"], max_length=3)


class TemplateDefinition(Input):
    name: Name
    config: TemplateSpec


class WatchDefinition(Input):
    name: Name
    catalog_spec_id: Id | None = None
    spec: SkipJsonSchema[WatchSpec | None] = None


class NotificationSettings(Input):
    idle_minutes: Annotated[int, Field(strict=True, ge=1, le=1440)] | None = None
    idle_threshold: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] | None = None
    idle_warmup_minutes: Annotated[int, Field(strict=True, ge=0, le=120)] | None = None
    idle_channels: list[Channel] | None = Field(default=None, max_length=3)
    idle_email_recipient: str | None = Field(default=None, max_length=254)
    idle_schedule: IdleSchedule | None = None
    notification_repeat_minutes: NotificationRepeatMinutes | None = None
    notification_rules: NotificationRules | None = None
    feishu_event_policy: FeishuEventPolicy | None = None


def _values(model: Input) -> dict:
    return model.model_dump(exclude_none=True)


def _lookup_task(conn: Connection, project_id: str, task_id: str) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE id=? AND project_id=?", (task_id, project_id)).fetchone()
    return task_view(require_row(row, "当前项目中找不到该任务"))


def _require_project(conn: Connection, project_id: str) -> None:
    if not any(item["id"] == project_id for item in storage.snapshot(conn, "projects")["data"]):
        raise KeyError("找不到该项目，请先同步项目列表")


def _bind_task_instance(conn: Connection, body: dict) -> dict:
    config = body["config"]
    if body["kind"] not in {"start", "stop", "shutdown", "inspection"} or not config.get("instance_id"):
        return body
    project_id, instance_id = body["project_id"], config["instance_id"]
    snapshot = storage.snapshot(conn, "instances", project_id)
    instance = next((item for item in snapshot["data"] if item["id"] == instance_id and item.get("project_id") == project_id), None)
    if instance is None:
        raise KeyError("当前项目中找不到该实例，请刷新实例清单")
    scope = {key: instance.get(key) for key in ("region", "zone")}
    if any(config.get(key) not in (None, value) for key, value in scope.items()):
        raise ValueError("实例的地域或可用区与当前项目清单不一致")
    return body | {"config": config | scope}


def _expect_version(resource: dict, expected_version: int | str) -> None:
    if resource["version"] != expected_version:
        raise ValueError("版本已变化，请重新读取后再提交")


def _task_context(
    conn: Connection, project_id: str, operation: TaskOperation, definition: TaskDefinition | None,
    task_id: str | None, expected_version: int | None,
) -> dict:
    if (operation in {"create", "update"}) != (definition is not None) or (operation == "create") != (task_id is None):
        raise ValueError("创建需要任务定义，修改需要任务 ID 和定义，恢复或触发需要任务 ID")
    if (operation == "update") != (expected_version is not None):
        raise ValueError("只有修改操作需要 expected_version")
    current = _lookup_task(conn, project_id, task_id) if task_id is not None else None
    if current is not None and current["deleted_at"] is not None:
        raise ValueError("请先从回收站恢复任务")
    if current is not None and current["status"] == "canceled":
        raise ValueError("已取消的任务无法修改或执行")
    if operation == "trigger" and current["status"] == "paused":
        raise ValueError("请先恢复任务再立即执行")
    if expected_version is not None:
        _expect_version(current, expected_version)
    if definition is not None:
        if current is None:
            _require_project(conn, project_id)
        raw = _bind_task_instance(conn, _values(definition) | {"project_id": project_id})
        effective = resolve_template(conn, raw)
        if current is not None:
            TaskStore._validate_edit(current, effective)
    else:
        effective = resolve_template(conn, task_snapshot(current), check_version=False)
        if current["kind"] == "acquire" and any(
            current["config"].get(key) != effective["config"].get(key) for key in ("charge_type", "quantity")
        ):
            raise ValueError("模板的计费方式或购买时长已变，请先修改任务并重新确认预算")
    instance_id = effective["config"].get("instance_id")
    if effective["kind"] in {"start", "stop", "shutdown"} and instance_id and termination_pending(conn, project_id, instance_id):
        raise ValueError("实例存在待核对的释放操作")
    now = storage.now()
    planned_at = current["next_at"] if current else None
    if current is None or (operation == "update" and current["schedule"] != effective["schedule"]):
        planned_at = TaskStore._valid_initial_time(effective, now)
    if operation == "resume" and planned_at is not None and run_deadline(effective, planned_at) < now:
        planned_at = next_time(effective["schedule"], now)
    return {
        "project_id": project_id, "task_id": task_id, "expected_version": expected_version,
        "current_status": current["status"] if current else None,
        "current_version": current["version"] if current else None,
        "next_at": current["next_at"] if current else None,
        "planned_at": planned_at,
        "definition": effective,
    }


def _confirm(conn: Connection, operation: str, preview_id: str | None, context: dict, *, required: bool) -> None:
    if preview_id is None:
        if required:
            raise ValueError("此操作涉及采购，请先预览并确认后提交 preview_id")
        return
    if read_preview(conn, operation, preview_id) != context:
        raise ValueError("请求内容或当前配置已变化，请重新预览")


def _trigger_id(request_id: str) -> str:
    key_id = require_principal().key_id
    return "mcp:" + sha256(f"{key_id}:{request_id}".encode()).hexdigest()


def _rule_model(project_id: str, definition: RuleDefinition) -> dict:
    return Rule.model_validate(_values(definition) | {"project_id": project_id}).model_dump(exclude_none=True)


def _template(conn: Connection, project_id: str, template_id: str) -> dict:
    row = conn.execute("SELECT * FROM templates WHERE id=? AND project_id=?", (template_id, project_id)).fetchone()
    row = require_row(row, "当前项目中找不到该模板")
    return dict(row) | {"config": json.loads(row["config"])}


def _template_references(
    conn: Connection, project_id: str, template_id: str, *, active_acquire: bool = False,
) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM tasks WHERE project_id=? AND (status<>'canceled' OR deleted_at IS NOT NULL)
           AND json_extract(definition,'$.template_id')=? ORDER BY id""",
        (project_id, template_id),
    )
    tasks = map(task_view, rows)
    return [
        {key: task[key] for key in ("id", "name", "version", "kind", "status", "next_at")}
        | {"max_price": task["config"].get("max_price"), "purchase_limit": task["config"].get("purchase_limit")}
        for task in tasks if not active_acquire or (
            task["kind"] == "acquire" and task["status"] == "active" and task["deleted_at"] is None
        )
    ]


def _template_context(
    conn: Connection, project_id: str, template_id: str, expected_version: int, definition: TemplateDefinition,
) -> dict:
    original = _template(conn, project_id, template_id)
    _expect_version(original, expected_version)
    new_spec = template_definition(conn, project_id, _values(definition.config))
    return {
        "project_id": project_id, "template_id": template_id, "expected_version": expected_version,
        "name": definition.name, "config": new_spec,
        "affected_tasks": _template_references(conn, project_id, template_id, active_acquire=True)
        if new_spec != original["config"] else [],
    }


def _watch(conn: Connection, project_id: str, watch_id: str) -> dict:
    row = conn.execute("SELECT * FROM watches WHERE id=? AND project_id=?", (watch_id, project_id)).fetchone()
    return watches.view(conn, require_row(row, "当前项目中找不到该关注项"), storage.now())


def _save_watch(
    conn: Connection, project_id: str, definition: WatchDefinition, *, watch_id: str | None = None,
    expected_version: str | None = None,
) -> dict:
    spec = _values(definition.spec) if definition.spec else None
    item, created = watches.save(conn, project_id, definition.name, definition.catalog_spec_id, spec,
                                 watch_id=watch_id, expected_version=expected_version)
    return item | {"created": created}


def _notice_settings(conn: Connection, config: Config) -> dict:
    settings = storage.get_settings(conn)
    channels = {"site": True, "feishu": configured_feishu(config, conn)[1], "email": False}
    row = conn.execute("SELECT error FROM system_status WHERE component='channel_email'").fetchone()
    channels["email"] = row is not None and row["error"] == "configured"
    return {key: settings[key] for key in NotificationSettings.model_fields} | {"channels": channels}


def _notice_public(item: dict) -> dict:
    fields = ("id", "project_id", "channel", "state", "attempts", "error", "created_at", "next_at", "read_at",
              "delivered_at", "event_id", "event_type")
    payload = item["payload"]
    return {key: item[key] for key in fields} | {
        key: payload[key] for key in ("title", "message", "instance_id", "run_id", "watch_id", "policy_event") if key in payload
    } | ({"business": item["business"]} if "business" in item else {})


def _notice_email(address: str | None) -> str | None:
    if address is None:
        return None
    if any(char in address for char in ",;"):
        raise ValueError("需要指定单个有效的邮件收件地址")
    return validate_config("stop", {"instance_id": "_", "region": "_", "zone": "_", "email": address})["email"]


def register(server: ConsoleMCPServer, config: Config) -> None:
    store = TaskStore(config.db, feishu_configured=lambda conn: configured_feishu(config, conn)[1])

    def reader(name: str, summary: str):
        return server.scoped_tool("read", name=name, description=summary, annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True,
        ))

    def writer(name: str, summary: str, *, destructive: bool = False):
        return server.scoped_tool("manage", name=name, description=summary, annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=destructive, idempotentHint=True, openWorldHint=True,
        ))

    @reader("gpu_list_tasks", "按项目分页查看定时任务。可用 view 筛选当前任务、历史任务或回收站。")
    def list_tasks(
        project_id: Id, include_archived: bool = False, limit: Limit = 50, offset: Offset = 0,
        view: TaskListView | None = None,
    ) -> dict[str, Any]:
        rows = store.list_tasks(project_id, view=view)
        return page([row for row in rows if view is not None or include_archived or row["status"] != "canceled"], limit, offset)

    @reader("gpu_get_task", "读取指定项目的单个定时任务、版本和下一次执行时间。")
    def get_task(project_id: Id, task_id: Id) -> dict[str, Any]:
        return store.get_task(task_id, project_id)

    @writer("gpu_preview_task", "预览定时任务的创建、修改、恢复或立即触发。采购类操作提交前须预览并确认。")
    def preview_task(
        project_id: Id, operation: TaskOperation, definition: TaskDefinition | None = None,
        task_id: Id | None = None, expected_version: Version | None = None,
    ) -> dict[str, Any]:
        with storage.connect(config.db) as conn:
            context = _task_context(conn, project_id, operation, definition, task_id, expected_version)
        effective = context["definition"]
        planned_at = context["planned_at"]
        if operation == "trigger":
            planned_at = storage.now()
            if effective["kind"] == "shutdown":
                planned_at = max(planned_at, context["next_at"] or effective["schedule"]["at"])
        receipt = issue_preview(config, "task:" + operation, context)
        return {
            "preview_id": receipt, "expires_in_seconds": 300, "confirmation_required": effective["kind"] == "acquire",
            "project_id": project_id, "task_id": task_id, "operation": operation,
            "definition": effective, "planned_at": planned_at, "planned_at_shanghai": shanghai_time(planned_at),
            "time_zone": str(SHANGHAI),
        }

    @writer("gpu_create_task", "创建定时任务。采购任务需要同一配置的有效 preview_id。")
    def create_task(project_id: Id, definition: TaskDefinition, request_id: Id, preview_id: Id | None = None) -> dict[str, Any]:
        data = {"project_id": project_id, "definition": _values(definition), "preview_id": preview_id}

        def execute(conn: Connection) -> dict[str, Any]:
            context = _task_context(conn, project_id, "create", definition, None, None)
            _confirm(conn, "task:create", preview_id, context, required=definition.kind == "acquire")
            return store.create_task(context["definition"])

        return mutate(config, "gpu_create_task", request_id, data, execute)

    @writer("gpu_update_task", "用 expected_version 修改定时任务。采购任务需要同一修改的有效 preview_id。")
    def update_task(
        project_id: Id, task_id: Id, definition: TaskDefinition, expected_version: Version,
        request_id: Id, preview_id: Id | None = None,
    ) -> dict[str, Any]:
        data = {"project_id": project_id, "task_id": task_id, "definition": _values(definition),
                "expected_version": expected_version, "preview_id": preview_id}

        def execute(conn: Connection) -> dict[str, Any]:
            context = _task_context(conn, project_id, "update", definition, task_id, expected_version)
            _confirm(conn, "task:update", preview_id, context, required=definition.kind == "acquire")
            body = context["definition"]
            receipt = store.preview(body | {"task_id": task_id, "task_version": expected_version})
            return store.update_task(task_id, body | {"preview_token": receipt["preview_token"]})

        return mutate(config, "gpu_update_task", request_id, data, execute)

    def control_task(
        project_id: str, task_id: str, request_id: str, action: Literal["pause", "resume", "cancel"],
        preview_id: str | None = None,
    ) -> dict[str, Any]:
        data = {"project_id": project_id, "task_id": task_id, "preview_id": preview_id}

        def execute(conn: Connection) -> dict[str, Any]:
            _lookup_task(conn, project_id, task_id)
            if action == "resume":
                context = _task_context(conn, project_id, action, None, task_id, None)
                _confirm(conn, "task:resume", preview_id, context, required=context["definition"]["kind"] == "acquire")
            return store.control_task(task_id, action)

        return mutate(config, "task:" + action, request_id, data, execute)

    @writer("gpu_pause_task", "暂停定时任务，并停止尚未提交的运行尝试。")
    def pause_task(project_id: Id, task_id: Id, request_id: Id) -> dict[str, Any]:
        return control_task(project_id, task_id, request_id, "pause")

    @writer("gpu_resume_task", "恢复定时任务。恢复采购任务需要预览确认。")
    def resume_task(project_id: Id, task_id: Id, request_id: Id, preview_id: Id | None = None) -> dict[str, Any]:
        return control_task(project_id, task_id, request_id, "resume", preview_id)

    @writer("gpu_delete_task", "取消定时任务，保留其历史运行及云端核对记录。可恢复删除请使用 gpu_trash_task。", destructive=True)
    def delete_task(project_id: Id, task_id: Id, request_id: Id) -> dict[str, Any]:
        return control_task(project_id, task_id, request_id, "cancel")

    def change_trash(
        action: TaskTrashAction, project_id: str, task_id: str, expected_version: int, request_id: str,
    ) -> dict[str, Any]:
        data = {"project_id": project_id, "task_id": task_id, "expected_version": expected_version}

        def execute(_conn: Connection) -> dict[str, Any]:
            result = store.change_trash(project_id, task_id, expected_version, action)
            return {key: result[key] for key in (
                "id", "project_id", "version", "status", "view", "deleted_at", "purge_at", "pending_reason", "purged",
            ) if key in result}

        return mutate(config, "task:" + action, request_id, data, execute)

    @writer("gpu_trash_task", "停止任务的后续调度并移入回收站，可在 7 天内恢复。", destructive=True)
    def trash_task(project_id: Id, task_id: Id, expected_version: Version, request_id: Id) -> dict[str, Any]:
        return change_trash("trash", project_id, task_id, expected_version, request_id)

    @writer("gpu_restore_task", "恢复回收站里的任务。仍有计划的任务会恢复为暂停。")
    def restore_task(project_id: Id, task_id: Id, expected_version: Version, request_id: Id) -> dict[str, Any]:
        return change_trash("restore", project_id, task_id, expected_version, request_id)

    @writer("gpu_purge_task", "彻底删除回收站中已收尾的任务配置，保留历史运行和核对记录。", destructive=True)
    def purge_task(project_id: Id, task_id: Id, expected_version: Version, request_id: Id) -> dict[str, Any]:
        return change_trash("purge", project_id, task_id, expected_version, request_id)

    @writer("gpu_trigger_task", "立即触发一次任务并返回 run_id。采购任务需要预览确认。")
    def trigger_task(project_id: Id, task_id: Id, request_id: Id, preview_id: Id | None = None) -> dict[str, Any]:
        data = {"project_id": project_id, "task_id": task_id, "preview_id": preview_id}

        def execute(conn: Connection) -> dict[str, Any]:
            context = _task_context(conn, project_id, "trigger", None, task_id, None)
            _confirm(conn, "task:trigger", preview_id, context, required=context["definition"]["kind"] == "acquire")
            run = store.trigger_task(task_id, _trigger_id(request_id))
            return {"run_id": run["id"], "project_id": project_id, "task_id": task_id,
                    "state": run["state"], "reason": run["reason"], "next_check": run["next_check"]}

        return mutate(config, "task:trigger", request_id, data, execute)

    @reader("gpu_list_rules", "按项目分页查看自动化规则，可按实例筛选。")
    def list_rules(
        project_id: Id, instance_id: Id | None = None, include_archived: bool = False, limit: Limit = 50, offset: Offset = 0,
    ) -> dict[str, Any]:
        with storage.connect(config.db) as conn:
            return page(automation_rules.list_rules(
                conn, project_id, instance_id, storage.now(), include_archived=include_archived,
            ), limit, offset)

    @reader("gpu_get_rule", "查看指定项目的规则、绑定实例、版本和当前进度。")
    def get_rule(project_id: Id, rule_id: Id) -> dict[str, Any]:
        with storage.connect(config.db) as conn:
            return automation_rules.get_rule(conn, project_id, rule_id, storage.now())

    @writer("gpu_create_rule", "创建和启用空闲关机或开机满时规则，时段按北京时间解释。")
    def create_rule(project_id: Id, definition: RuleDefinition, request_id: Id) -> dict[str, Any]:
        data = _rule_model(project_id, definition)
        return mutate(config, "rule:create", request_id, data, lambda conn: automation_rules.save_bound_rule(
            conn, data, storage.now(),
        ))

    @writer("gpu_update_rule", "按当前 expected_version 修改规则，项目、绑定实例和规则类型保持原值。")
    def update_rule(
        project_id: Id, rule_id: Id, definition: RuleDefinition, expected_version: Version, request_id: Id,
    ) -> dict[str, Any]:
        rule = _rule_model(project_id, definition)
        data = {"project_id": project_id, "rule_id": rule_id, "definition": rule, "expected_version": expected_version}

        def execute(conn: Connection) -> dict[str, Any]:
            timestamp = storage.now()
            _expect_version(automation_rules.get_rule(conn, project_id, rule_id, timestamp), expected_version)
            return automation_rules.save_bound_rule(conn, rule, timestamp, rule_id)

        return mutate(config, "rule:update", request_id, data, execute)

    def control_rule(project_id: str, rule_id: str, request_id: str, action: str) -> dict[str, Any]:
        data = {"project_id": project_id, "rule_id": rule_id}

        def execute(conn: Connection) -> dict[str, Any]:
            timestamp = storage.now()
            automation_rules.get_rule(conn, project_id, rule_id, timestamp)
            return automation_rules.control_rule(conn, rule_id, action, timestamp)

        return mutate(config, "rule:" + action, request_id, data, execute)

    @writer("gpu_pause_rule", "暂停规则并取消未发送的自动化运行。")
    def pause_rule(project_id: Id, rule_id: Id, request_id: Id) -> dict[str, Any]:
        return control_rule(project_id, rule_id, request_id, "pause")

    @writer("gpu_resume_rule", "恢复规则并从新的采样开始评估。")
    def resume_rule(project_id: Id, rule_id: Id, request_id: Id) -> dict[str, Any]:
        return control_rule(project_id, rule_id, request_id, "resume")

    @writer("gpu_delete_rule", "删除规则，保留历史和已发送操作的核对记录。", destructive=True)
    def delete_rule(project_id: Id, rule_id: Id, request_id: Id) -> dict[str, Any]:
        return control_rule(project_id, rule_id, request_id, "archive")

    @reader("gpu_list_templates", "按项目分页查看规格模板。")
    def list_templates(project_id: Id, limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        with storage.connect(config.db) as conn:
            rows = conn.execute("SELECT * FROM templates WHERE project_id=? ORDER BY name,id", (project_id,))
            return page([dict(row) | {"config": json.loads(row["config"])} for row in rows], limit, offset)

    @reader("gpu_get_template", "查看指定项目的规格模板和版本。")
    def get_template(project_id: Id, template_id: Id) -> dict[str, Any]:
        with storage.connect(config.db) as conn:
            return _template(conn, project_id, template_id)

    @writer("gpu_create_template", "创建项目的实例规格模板。")
    def create_template(project_id: Id, definition: TemplateDefinition, request_id: Id) -> dict[str, Any]:
        data = {"project_id": project_id, "definition": _values(definition)}

        def execute(conn: Connection) -> dict[str, Any]:
            _require_project(conn, project_id)
            template_id = uuid4().hex
            spec = template_definition(conn, project_id, _values(definition.config))
            conn.execute(
                "INSERT INTO templates(id,project_id,name,config,version) VALUES (?,?,?,?,1)",
                (template_id, project_id, definition.name, encode(spec)),
            )
            return _template(conn, project_id, template_id)

        return mutate(config, "template:create", request_id, data, execute)

    @writer("gpu_preview_template_update", "预览模板修改及它将影响的启用中采购任务。")
    def preview_template_update(
        project_id: Id, template_id: Id, expected_version: Version, definition: TemplateDefinition,
    ) -> dict[str, Any]:
        with storage.connect(config.db) as conn:
            context = _template_context(conn, project_id, template_id, expected_version, definition)
        return context | {
            "preview_id": issue_preview(config, "template:update", context),
            "expires_in_seconds": 300, "confirmation_required": bool(context["affected_tasks"]),
        }

    @writer("gpu_update_template", "修改规格模板。影响已启用的采购任务时需要有效 preview_id。")
    def update_template(
        project_id: Id, template_id: Id, expected_version: Version, definition: TemplateDefinition,
        request_id: Id, preview_id: Id | None = None,
    ) -> dict[str, Any]:
        data = {"project_id": project_id, "template_id": template_id, "expected_version": expected_version,
                "definition": _values(definition), "preview_id": preview_id}

        def execute(conn: Connection) -> dict[str, Any]:
            context = _template_context(conn, project_id, template_id, expected_version, definition)
            _confirm(conn, "template:update", preview_id, context, required=bool(context["affected_tasks"]))
            conn.execute(
                "UPDATE templates SET name=?,config=?,version=version+1 WHERE id=? AND project_id=?",
                (definition.name, encode(context["config"]), template_id, project_id),
            )
            return _template(conn, project_id, template_id)

        return mutate(config, "template:update", request_id, data, execute)

    @writer("gpu_delete_template", "删除项目模板。需先解除仍可运行的任务对模板的引用。", destructive=True)
    def delete_template(project_id: Id, template_id: Id, request_id: Id) -> dict[str, Any]:
        data = {"project_id": project_id, "template_id": template_id}

        def execute(conn: Connection) -> dict[str, Any]:
            _template(conn, project_id, template_id)
            references = _template_references(conn, project_id, template_id)
            if references:
                ids = ", ".join(row["id"] for row in references[:10])
                raise ValueError(f"{len(references)} 个任务仍引用模板：{ids}。请先更新或删除相应任务")
            conn.execute("DELETE FROM templates WHERE id=? AND project_id=?", (template_id, project_id))
            return data | {"deleted": True}

        return mutate(config, "template:delete", request_id, data, execute)

    @reader("gpu_list_watches", "按项目分页查看库存关注、近期库存状态和版本。")
    def list_watches(project_id: Id, limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        with storage.connect(config.db) as conn:
            with storage.transaction(conn, immediate=False):
                return page(watches.list_project(conn, project_id, storage.now()), limit, offset)

    @reader("gpu_get_watch", "查看指定项目的库存关注、查到的可用性及其时间。")
    def get_watch(project_id: Id, watch_id: Id) -> dict[str, Any]:
        with storage.connect(config.db) as conn:
            return _watch(conn, project_id, watch_id)

    @writer("gpu_create_watch", "关注平台目录中的规格，使用其对应资源池的余量。")
    def create_watch(project_id: Id, definition: WatchDefinition, request_id: Id) -> dict[str, Any]:
        data = {"project_id": project_id, "definition": _values(definition)}
        return mutate(config, "watch:create", request_id, data, lambda conn: _save_watch(conn, project_id, definition))

    @writer("gpu_update_watch", "按 expected_version 修改关注名称或绑定的平台规格。")
    def update_watch(
        project_id: Id, watch_id: Id, definition: WatchDefinition, expected_version: Id, request_id: Id,
    ) -> dict[str, Any]:
        data = {"project_id": project_id, "watch_id": watch_id, "definition": _values(definition),
                "expected_version": expected_version}
        return mutate(config, "watch:update", request_id, data, lambda conn: _save_watch(
            conn, project_id, definition, watch_id=watch_id, expected_version=expected_version,
        ))

    @writer("gpu_delete_watch", "取消指定项目的库存关注并清理其检测记录。", destructive=True)
    def delete_watch(project_id: Id, watch_id: Id, request_id: Id) -> dict[str, Any]:
        data = {"project_id": project_id, "watch_id": watch_id}

        def execute(conn: Connection) -> dict[str, Any]:
            if not conn.execute("DELETE FROM watches WHERE id=? AND project_id=?", (watch_id, project_id)).rowcount:
                raise KeyError("当前项目中找不到该关注项")
            clear_watch_stock(conn, watch_id)
            return data | {"deleted": True}

        return mutate(config, "watch:delete", request_id, data, execute)

    @reader("gpu_list_notifications", "按项目、渠道和事件分页查询通知及送达状态。")
    def notifications(
        project_id: Id, limit: Limit = 50, channel: Literal["site", "feishu", "email", "external"] | None = None,
        event_type: NoticeEvent | None = None, event_id: EventId | None = None,
        cursor: Annotated[int, Field(gt=0, le=2**63 - 1)] | None = None,
    ) -> dict[str, Any]:
        with storage.connect(config.db) as conn:
            result = list_notifications(conn, limit=limit, channel=channel, project_id=project_id,
                                        event_type=event_type, event_id=event_id, cursor=cursor)
        return {"items": [_notice_public(item) for item in result["items"]], "next_cursor": result["next_cursor"]}

    @writer("gpu_read_notification", "标记指定项目的站内通知为已读。")
    def read_notification(project_id: Id, notification_id: NotificationId, request_id: Id) -> dict[str, Any]:
        data = {"project_id": project_id, "notification_id": notification_id}

        def execute(conn: Connection) -> dict[str, Any]:
            if not conn.execute(
                f"UPDATE notifications SET read_at=COALESCE(read_at,?) WHERE id=? AND channel='site' AND {PROJECT_ID}=?",
                (storage.now(), notification_id, project_id),
            ).rowcount:
                raise KeyError("当前项目中找不到该站内通知")
            return data | {"read": True}

        return mutate(config, "notification:read", request_id, data, execute)

    @writer("gpu_retry_notification", "重新排队指定项目中投递失败的飞书或邮件通知。")
    def retry_notification(project_id: Id, notification_id: NotificationId, request_id: Id) -> dict[str, Any]:
        data = {"project_id": project_id, "notification_id": notification_id}

        def execute(conn: Connection) -> dict[str, Any]:
            if not conn.execute(
                f"""UPDATE notifications SET state='pending',next_at=?,error=NULL,attempts=0
                    WHERE id=? AND channel<>'site' AND state='failed' AND {PROJECT_ID}=?""",
                (storage.now(), notification_id, project_id),
            ).rowcount:
                raise ValueError("只有当前项目投递失败的外部通知可以重试")
            return data | {"queued": True}

        return mutate(config, "notification:retry", request_id, data, execute)

    @reader("gpu_get_notification_settings", "读取全局通知策略、收件地址及渠道是否已配置。")
    def notification_settings() -> dict[str, Any]:
        with storage.connect(config.db) as conn:
            return _notice_settings(conn, config)

    @writer("gpu_update_notification_settings", "修改全局通知策略、使用渠道及收件地址。")
    def update_notification_settings(changes: NotificationSettings, request_id: Id) -> dict[str, Any]:
        data = changes.model_dump(exclude_unset=True)

        def execute(conn: Connection) -> dict[str, Any]:
            if any(value is None for key, value in data.items() if key != "idle_email_recipient"):
                raise ValueError("此通知设置需要有效值")
            updated = data.copy()
            if "idle_email_recipient" in updated:
                updated["idle_email_recipient"] = _notice_email(updated["idle_email_recipient"])
            if {"idle_email_recipient", "idle_channels"} & updated.keys():
                preferred = storage.get_settings(conn) | updated
                if "email" in preferred["idle_channels"] and not preferred["idle_email_recipient"]:
                    raise ValueError("邮件渠道需要填写收件地址")
            storage.set_settings(conn, updated)
            return _notice_settings(conn, config)

        return mutate(config, "notification:settings", request_id, data, execute)

    @writer("gpu_test_notification", "向已配置的指定渠道排队发送一条测试通知，返回通知事件 ID。")
    def test_notification(project_id: Id, channel: Channel, request_id: Id, recipient: str | None = None) -> dict[str, Any]:
        data = {"project_id": project_id, "channel": channel, "recipient": recipient}

        def execute(conn: Connection) -> dict[str, Any]:
            _require_project(conn, project_id)
            settings = _notice_settings(conn, config)
            if not settings["channels"][channel]:
                raise ValueError("请先在管理台配置该通知渠道")
            to_address = recipient if channel == "email" else None
            if channel == "email":
                to_address = to_address or settings["notification_rules"]["email_recipient"] or settings["idle_email_recipient"]
                if not to_address:
                    raise ValueError("需要指定测试通知的邮件收件地址")
                to_address = _notice_email(to_address)
            event_id = f"test-{uuid4().hex}"
            storage.emit(conn, event_id, {
                "title": "GPU 实验管理台测试通知", "project_id": project_id, "recipient": to_address,
            }, [channel])
            return {"event_id": event_id, "project_id": project_id, "state": "queued", "channel": channel}

        return mutate(config, "notification:test", request_id, data, execute)
