"""Route global Feishu notifications through the existing delivery outbox."""

import json
from sqlite3 import Connection, Row
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError


FeishuEvent = Literal[
    "instance_started", "instance_stopped", "operation_failed", "operation_review", "shutdown_attention",
    "instance_rebooted", "instance_created", "instance_released", "shutdown_registered", "capacity_wait",
]
DEFAULT_EVENTS: tuple[FeishuEvent, ...] = (
    "instance_started", "instance_stopped", "operation_failed", "operation_review", "shutdown_attention",
)
FEISHU_TITLES: dict[FeishuEvent, str] = {
    "instance_started": "实例开机已确认",
    "instance_stopped": "实例关机已确认",
    "operation_failed": "实例操作未完成",
    "operation_review": "实例操作待核对",
    "shutdown_attention": "关机预约需核对",
    "instance_rebooted": "实例重启已确认",
    "instance_created": "实例已创建并有卡运行",
    "instance_released": "实例释放已确认",
    "shutdown_registered": "关机预约已登记",
    "capacity_wait": "开机等待资源",
}


class FeishuEventPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[FeishuEvent] = Field(max_length=10)
    capacity_wait_minutes: int = Field(strict=True, ge=1, le=1440)

    @field_validator("events")
    @classmethod
    def unique_events(cls, events: list[FeishuEvent]) -> list[FeishuEvent]:
        if len(set(events)) != len(events):
            raise ValueError("飞书事件不能重复")
        return events


DEFAULT_POLICY = {"events": list(DEFAULT_EVENTS), "capacity_wait_minutes": 15}

# A later wait for a different reason can retain the last cloud code in the run result.
_CAPACITY_SHORTAGE_REASON = str(CloudError("226604"))
_CAPACITY_WAIT_RUN = """r.state='waiting' AND r.phase IS NULL AND r.stopped=0 AND r.task_id IS NOT NULL
    AND r.reason=:shortage_reason AND json_extract(r.snapshot,'$.kind')='start'
    AND json_type(r.snapshot,'$.config.capacity_retry')='object'
    AND json_extract(r.result,'$.cloud_code')='226604'
    AND json_type(r.result,'$.capacity_wait_since')='integer'
    AND json_extract(r.result,'$.capacity_wait_since') >= 1"""


def _active_policy(conn: Connection, feishu_configured: bool) -> dict | None:
    if not feishu_configured:
        return None
    settings = storage.get_settings(conn)
    return None if settings["restore_gate"] else settings.get("feishu_event_policy", DEFAULT_POLICY)


def emit(
    conn: Connection, event_id: str, payload: dict, channels: list[str], *,
    policy_event: FeishuEvent | None = None, feishu_configured: bool = False,
) -> None:
    """Keep explicit subscriptions and add at most one global Feishu delivery."""
    if policy_event is not None:
        payload = payload | {"policy_event": policy_event, "feishu_title": FEISHU_TITLES[policy_event]}
    if channels:
        storage.emit(conn, event_id, payload, channels)
    if policy_event is None or "feishu" in channels:
        return
    policy = _active_policy(conn, feishu_configured)
    if policy is not None and policy_event in policy["events"]:
        storage.emit(conn, event_id, payload | {"feishu_policy_only": True}, ["feishu"])


def capacity_wait_tick(conn: Connection, now: int, *, feishu_configured: bool) -> None:
    policy = _active_policy(conn, feishu_configured)
    if policy is None or "capacity_wait" not in policy["events"]:
        return
    threshold = now - 60 * policy["capacity_wait_minutes"]
    rows = conn.execute(
        f"""SELECT r.id,r.project_id,r.snapshot,r.result FROM task_runs AS r
           WHERE {_CAPACITY_WAIT_RUN}
             AND json_extract(r.result,'$.capacity_wait_since') <= :threshold
             AND NOT EXISTS (SELECT 1 FROM notifications AS n
                 WHERE n.channel='feishu' AND n.event_id=r.id || ':capacity-wait')""",
        {"shortage_reason": _CAPACITY_SHORTAGE_REASON, "threshold": threshold},
    ).fetchall()
    for row in rows:
        snapshot, result = json.loads(row["snapshot"]), json.loads(row["result"])
        config = snapshot["config"]
        waited = (now - result["capacity_wait_since"]) // 60
        emit(conn, row["id"] + ":capacity-wait", {
            "title": "开机等待资源",
            "message": f"检查时已等待 {waited} 分钟，最近一次开机请求提示资源不足。系统按原安排继续尝试。",
            "task_name": snapshot["name"], "run_id": row["id"], "project_id": row["project_id"],
            "instance_id": config["instance_id"], "zone": config.get("zone"), "gpu_model": result.get("gpu_model"),
            "confirmed_at": now,
        }, [], policy_event="capacity_wait", feishu_configured=feishu_configured)


def delivery_hold(conn: Connection, row: Row, payload: dict, now: int) -> tuple[str, str] | None:
    if row["channel"] != "feishu" or not row["event_id"].endswith(":capacity-wait"):
        return None
    if storage.get_settings(conn)["restore_gate"]:
        return "skipped", "数据库恢复后已取消历史飞书通知"
    pending = conn.execute(
        f"""SELECT 1 FROM task_runs AS r WHERE r.id=:run_id AND r.project_id=:project_id
             AND {_CAPACITY_WAIT_RUN} AND json_extract(r.result,'$.capacity_wait_since') <= :now""",
        {"run_id": payload.get("run_id"), "project_id": payload.get("project_id"),
         "shortage_reason": _CAPACITY_SHORTAGE_REASON, "now": now},
    ).fetchone()
    return None if pending else ("skipped", "开机任务已结束或不再等待资源")


def cancel_restored(conn: Connection) -> None:
    conn.execute(
        """UPDATE notifications SET state='skipped',error='数据库恢复后已取消历史飞书通知'
           WHERE channel='feishu' AND state='pending' AND json_type(payload,'$.feishu_policy_only')='true'""",
    )
