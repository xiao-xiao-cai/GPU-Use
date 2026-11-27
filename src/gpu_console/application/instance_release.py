"""Validate and queue a single confirmed CompShare instance release."""

from collections.abc import Collection
from sqlite3 import Connection

from gpu_console import storage
from gpu_console.application import automation_rules
from gpu_console.application.tasks import INSTANCE_RUN_MATCH, insert_run

PREVIEW_SECONDS = 300
DEFAULT_CHARGE_TYPES = frozenset({"Postpay"})
INSTANCE_FIELDS = (
    "id", "project_id", "name", "region", "zone", "state", "instance_type",
    "charge_type", "create_time", "start_time", "stop_time",
)
PENDING = "(r.state IN ('queued','waiting','reconciling') OR (r.state='manual_review' AND r.phase IS NOT NULL))"


def identity(instance: dict) -> dict:
    return {field: instance.get(field) for field in INSTANCE_FIELDS}


def check_target(instance: dict, charge_types: Collection[str], expected: dict | None = None) -> None:
    if instance.get("state") != "Stopped" or instance.get("instance_type") != "UHost":
        raise ValueError("仅可释放已关机的普通虚机，请先核对实例类型和状态")
    if instance.get("charge_type") not in charge_types:
        raise ValueError("该实例计费类型未开启释放权限")
    if expected is not None and identity(instance) != expected:
        raise ValueError("实例信息已变化，请重新预览释放")


def from_snapshot(conn: Connection, project_id: str, instance_id: str, now: int) -> dict:
    snapshot = storage.snapshot(conn, "instances", project_id)
    if not automation_rules.fresh_instance_snapshot(conn, snapshot, now):
        raise ValueError("实例清单待同步，请刷新后再预览释放")
    for instance in snapshot["data"]:
        if instance.get("id") != instance_id or instance.get("project_id") != project_id:
            continue
        if not instance.get("region") or not instance.get("zone"):
            raise ValueError("实例地域和可用区不明确，请刷新实例清单")
        return instance
    raise KeyError("项目快照中找不到该实例")


def blockers(conn: Connection, project_id: str, instance_id: str, *, exclude_run_id: str | None = None) -> dict:
    scope = {"project_id": project_id, "instance_id": instance_id, "exclude_run_id": exclude_run_id}
    tasks = conn.execute(
        """SELECT id FROM tasks WHERE project_id=:project_id AND status='active' AND deleted_at IS NULL
           AND json_extract(definition,'$.config.instance_id')=:instance_id ORDER BY id LIMIT 20""", scope,
    )
    rules = conn.execute(
        """SELECT id FROM automation_rules WHERE project_id=:project_id AND status='active'
           AND instance_id=:instance_id ORDER BY id LIMIT 20""", scope,
    )
    runs = conn.execute(
        f"""SELECT r.id FROM task_runs AS r WHERE r.project_id=:project_id AND {PENDING}
            AND (:exclude_run_id IS NULL OR r.id != :exclude_run_id) AND {INSTANCE_RUN_MATCH}
            ORDER BY r.id LIMIT 20""", scope,
    )
    return {key: [row["id"] for row in rows] for key, rows in (
        ("task_ids", tasks), ("rule_ids", rules), ("run_ids", runs),
    )}


def has_pending_termination(
    conn: Connection, project_id: str, instance_id: str, *, exclude_run_id: str | None = None,
) -> bool:
    return conn.execute(
        f"""SELECT 1 FROM task_runs AS r WHERE r.project_id=:project_id AND {PENDING}
            AND (:exclude_run_id IS NULL OR r.id != :exclude_run_id)
            AND json_extract(r.snapshot,'$.kind')='terminate'
            AND json_extract(r.snapshot,'$.config.instance_id')=:instance_id LIMIT 1""",
        {"project_id": project_id, "instance_id": instance_id, "exclude_run_id": exclude_run_id},
    ).fetchone() is not None


def enqueue(
    conn: Connection, expected: dict, expires_at: int, request_id: str,
    now: int, charge_types: Collection[str],
) -> dict:
    if now >= expires_at:
        raise ValueError("释放预览已过期，请重新预览")
    project_id, instance_id = expected["project_id"], expected["id"]
    check_target(from_snapshot(conn, project_id, instance_id, now), charge_types, expected)
    if any(blockers(conn, project_id, instance_id).values()):
        raise ValueError("存在关联的启用任务、规则或待核对操作，请处理后重新预览")
    snapshot = {
        "kind": "terminate", "name": "释放实例", "project_id": project_id,
        "config": {"instance_id": instance_id, "region": expected["region"], "zone": expected["zone"],
                   "expected_instance": expected},
        "channels": ["site"], "window_seconds": expires_at - now - 1,
    }
    return insert_run(conn, snapshot, now, now, request_id=request_id)
