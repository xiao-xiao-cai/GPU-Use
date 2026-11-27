"""Durable task definitions, immutable execution snapshots and recovery state."""

import json
import sqlite3
import uuid
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
from time import time
from typing import Callable, Literal

from gpu_console import storage
from gpu_console.application import feishu_notices, instance_quotes
from gpu_console.storage import cardless, catalog, instance_history
from gpu_console.application.purchase_options import (
    normalize_purchase_config, normalize_purchase_spec, platform_candidates, verify_purchase_spec,
)
from gpu_console.application.task_config import (
    SHUTDOWN_LEAD,
    SHUTDOWN_MINIMUM,
    SPEC_FIELDS,
    channel_list,
    includes_event,
    integer,
    match_spec,
    next_time,
    run_deadline,
    run_window_expired,
    shanghai_time,
    text,
    validate_config,
    validate_task,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL,
    version INTEGER NOT NULL, definition TEXT NOT NULL, next_at INTEGER,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER, status_before_delete TEXT
);
CREATE INDEX IF NOT EXISTS tasks_due ON tasks(status, next_at);
CREATE TABLE IF NOT EXISTS task_previews (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    task_version INTEGER NOT NULL, definition_hash TEXT NOT NULL, token TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instance_purchase_previews (
    id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), project_id TEXT NOT NULL,
    scheduled_at INTEGER NOT NULL, deadline_at INTEGER NOT NULL, request_id TEXT UNIQUE,
    state TEXT NOT NULL, phase TEXT, reason TEXT, snapshot TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT '{}', next_check INTEGER NOT NULL,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, stopped INTEGER NOT NULL DEFAULT 0,
    source_task_id TEXT, cancel_shutdown_on_confirmation INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS task_runs_schedule ON task_runs(task_id, scheduled_at)
    WHERE request_id IS NULL;
CREATE INDEX IF NOT EXISTS task_runs_due ON task_runs(state, next_check);
CREATE INDEX IF NOT EXISTS task_runs_shutdown_review_due ON task_runs(phase, next_check) WHERE phase='review_shutdown';
CREATE INDEX IF NOT EXISTS task_runs_project ON task_runs(project_id, created_at);
CREATE TABLE IF NOT EXISTS task_run_warning_dismissals (
    run_id TEXT NOT NULL PRIMARY KEY REFERENCES task_runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_operations (
    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES task_runs(id),
    project_id TEXT NOT NULL, action TEXT NOT NULL, state TEXT NOT NULL, fingerprint TEXT,
    data TEXT NOT NULL, request_id TEXT, cloud_code TEXT, created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS task_operations_run ON task_operations(run_id, id);
CREATE INDEX IF NOT EXISTS task_operations_project ON task_operations(project_id, action, state);
CREATE INDEX IF NOT EXISTS task_operations_start_attempts ON task_operations(run_id)
    WHERE action='start' AND state IN ('reserved','accepted','rejected');
CREATE INDEX IF NOT EXISTS task_operations_create_binding
    ON task_operations(project_id,json_extract(data,'$.instance_id'))
    WHERE action='create' AND state='accepted';
CREATE TABLE IF NOT EXISTS task_audit (
    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES task_runs(id),
    action TEXT NOT NULL, data TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS task_audit_run ON task_audit(run_id, id);
CREATE TABLE IF NOT EXISTS shutdown_owners (
    project_id TEXT NOT NULL, instance_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES task_runs(id),
    task_id TEXT REFERENCES tasks(id), region TEXT NOT NULL, zone TEXT NOT NULL, stop_at INTEGER NOT NULL,
    PRIMARY KEY(project_id, instance_id)
);
CREATE TABLE IF NOT EXISTS external_shutdown_observations (
    run_id TEXT PRIMARY KEY REFERENCES task_runs(id) ON DELETE CASCADE,
    instance_id TEXT NOT NULL, region TEXT NOT NULL, zone TEXT NOT NULL, stop_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS external_shutdown_due ON external_shutdown_observations(stop_at);
"""

ACTIVE_RUNS = ("queued", "waiting", "reconciling")
TERMINAL_RUNS = {"succeeded", "failed", "expired", "canceled", "missed"}
TRASH_RETENTION_SECONDS = 7 * 86400
TaskListView = Literal["current", "history", "trash"]
TaskTrashAction = Literal["trash", "restore", "purge"]
RUN_SOURCE_TASK = "COALESCE(r.source_task_id,r.task_id)"
SHUTDOWN_OWNER_RUNS = "FROM shutdown_owners o JOIN task_runs r ON r.id=o.run_id AND r.project_id=o.project_id"
SHUTDOWN_OWNER_TASK = f"COALESCE(o.task_id,{RUN_SOURCE_TASK})"
TASK_PENDING_REASON = f"""CASE
    WHEN EXISTS (SELECT 1 FROM task_runs r WHERE r.project_id=t.project_id AND {RUN_SOURCE_TASK}=t.id
                 AND (r.state='manual_review' OR r.phase='review_shutdown'))
        THEN '仍有运行待人工核对'
    WHEN EXISTS (SELECT 1 FROM task_runs r WHERE r.project_id=t.project_id AND {RUN_SOURCE_TASK}=t.id
                 AND r.state IN ('queued','waiting','reconciling'))
        THEN '仍有运行或云端操作待完成'
    WHEN EXISTS (SELECT 1 {SHUTDOWN_OWNER_RUNS} WHERE o.project_id=t.project_id AND {SHUTDOWN_OWNER_TASK}=t.id)
        THEN '仍有关机预约待处理'
    WHEN t.deleted_at IS NULL AND EXISTS (
        SELECT 1 FROM external_shutdown_observations o JOIN task_runs r ON r.id=o.run_id
        WHERE r.project_id=t.project_id AND {RUN_SOURCE_TASK}=t.id)
        THEN '等待回读关机结果'
    END"""
TASK_LIST_COLUMNS = f"""t.*, {TASK_PENDING_REASON} AS pending_reason,
    (SELECT json_object('id',r.id,'state',r.state,'scheduled_at',r.scheduled_at,'updated_at',r.updated_at,'reason',r.reason)
     FROM task_runs r WHERE r.task_id=t.id AND r.project_id=t.project_id
       AND json_extract(r.snapshot,'$.kind')!='cancel_shutdown'
     ORDER BY r.created_at DESC,r.rowid DESC LIMIT 1) AS last_run"""
PURCHASE_PREVIEW_SECONDS = 300
PURCHASE_RETRY_MIN_SECONDS = 60
PURCHASE_RETRY_MAX_SECONDS = 604800
IMMEDIATE_ACTION_NAMES = {
    "start": "启动实例",
    "stop": "关闭实例",
    "shutdown": "设置关机预约",
    "cancel_shutdown": "取消关机预约",
    "reboot": "重启实例",
    "rename": "重命名实例",
}
INSTANCE_RUN_MATCH = """(
    json_extract(r.snapshot,'$.config.instance_id')=:instance_id OR
    json_extract(r.result,'$.instance_id')=:instance_id OR
    (json_type(r.result,'$.submitted_ids')='array' AND EXISTS (
        SELECT 1 FROM json_each(r.result,'$.submitted_ids') AS submitted
        WHERE submitted.type='text' AND submitted.value=:instance_id
    ))
)"""
INSTANCE_CONFIG_TARGET = """r.project_id=:project_id
    AND json_extract(r.snapshot,'$.config.instance_id')=:instance_id
    AND json_extract(r.snapshot,'$.config.region')=:region
    AND json_extract(r.snapshot,'$.config.zone')=:zone"""
SCHEDULED_START_TARGET = f"""{INSTANCE_CONFIG_TARGET} AND r.task_id IS NOT NULL
    AND json_extract(r.snapshot,'$.kind')='start'"""
INSTANCE_RECHECK_PHASES = {"verify_start", "verify_stop", "verify_reboot", "verify_rename", "verify_terminate", "verify_resize"}
PURCHASE_RECHECK_PHASES = {"verify_create", "manual_bind", "verify_cfs"}
INSTANCE_RECHECK_SQL = "('verify_start','verify_stop','verify_reboot','verify_rename','verify_terminate','verify_resize')"
RUN_CONFIG_ISSUE = """(
    r.state='manual_review' AND r.phase IS NULL AND r.task_id IS NOT NULL AND (
        json_extract(r.result,'$.task_config_issue')=1 OR (
            json_type(r.snapshot,'$.template_id')='text'
            AND json_extract(r.snapshot,'$.kind') IN ('stock','acquire')
            AND NOT EXISTS (SELECT 1 FROM task_operations WHERE run_id=r.id)
        )
    )
)"""
RUN_LINKED_SHUTDOWN_JOIN = """LEFT JOIN task_runs linked_shutdown
    ON linked_shutdown.id=json_extract(r.result,'$.shutdown_run_id')
    AND linked_shutdown.project_id=r.project_id
    AND json_extract(linked_shutdown.snapshot,'$.kind')='shutdown'
    AND json_extract(linked_shutdown.snapshot,'$.config.parent_run_id')=r.id"""
SHUTDOWN_REVIEW_RESOLVED = """(
    json_type(r.result,'$.shutdown_review.resolved')='true'
    AND NOT COALESCE(json_extract(r.result,'$.shutdown_outcome')='missed'
        AND json_extract(r.result,'$.shutdown_observation.checked_at') > json_extract(r.result,'$.shutdown_review.checked_at'),0)
)"""
# Use the same predicate to filter the attention queue and to describe each notice.
RUN_NEEDS_ATTENTION = f"""(
    r.phase='review_shutdown'
    OR json_type(r.result,'$.shutdown_cleanup_pending')='true'
    OR json_type(r.result,'$.purchase_binding_conflict')='true'
    OR (r.state='reconciling' AND (
        r.phase='review_cancel_shutdown' OR (
            r.phase IN ('verify_create','manual_bind') AND json_type(r.result,'$.reviewable')='true'
        )
        OR json_type(r.result,'$.instance_recheck_pending')='true'
    ))
    OR (NOT COALESCE({SHUTDOWN_REVIEW_RESOLVED},0) AND (
        r.state='manual_review'
        OR json_extract(r.result,'$.shutdown_outcome')='missed'
        OR (json_extract(r.result,'$.shutdown_confirmation')='unconfirmed' AND linked_shutdown.id IS NULL)
        OR (json_extract(r.snapshot,'$.kind')='shutdown' AND r.state IN ('failed','expired','missed'))
    ))
)"""
RUN_BUSINESS_COLUMNS = f"""
    COALESCE({RUN_NEEDS_ATTENTION},0) AS business_attention,
    linked_shutdown.id AS shutdown_followup_id,
    json_object('state',linked_shutdown.state,'phase',linked_shutdown.phase,'reason',linked_shutdown.reason,
        'result',json(linked_shutdown.result),'updated_at',linked_shutdown.updated_at) AS business_linked_shutdown,
    CASE WHEN {RUN_CONFIG_ISSUE} AND t.status<>'canceled' AND t.deleted_at IS NULL THEN t.id END AS business_task_id,
    CASE WHEN r.state='failed' THEN (
        SELECT created_at FROM task_audit WHERE run_id=r.id AND action='release' ORDER BY id DESC LIMIT 1
    ) END AS business_released_at
"""


class InstanceActionUnavailable(ValueError):
    """An instance action cannot be enqueued against the current service state."""


class TaskTrashConflict(ValueError):
    """The task changed or cleanup is blocked by pending work."""


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    with storage.transaction(conn):
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "deleted_at" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN deleted_at INTEGER")
        if "status_before_delete" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN status_before_delete TEXT")
        run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}
        if "source_task_id" not in run_columns:
            conn.execute("ALTER TABLE task_runs ADD COLUMN source_task_id TEXT")
            conn.execute("UPDATE task_runs SET source_task_id=task_id WHERE task_id IS NOT NULL")
            conn.execute(
                """UPDATE task_runs AS child SET source_task_id=(
                       SELECT parent.task_id FROM task_runs parent
                       WHERE parent.id=json_extract(child.snapshot,'$.config.parent_run_id')
                         AND parent.project_id=child.project_id)
                   WHERE child.source_task_id IS NULL AND json_extract(child.snapshot,'$.kind')='shutdown'""",
            )
        if "cancel_shutdown_on_confirmation" not in run_columns:
            conn.execute("ALTER TABLE task_runs ADD COLUMN cancel_shutdown_on_confirmation INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS tasks_trash_due ON tasks(deleted_at) WHERE deleted_at IS NOT NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS task_runs_source ON task_runs(project_id,COALESCE(source_task_id,task_id))")
        conn.execute(
            """WITH duplicates AS (
                   SELECT project_id,json_extract(data,'$.instance_id') AS instance_id
                   FROM task_operations WHERE action='create' AND state='accepted'
                     AND json_type(data,'$.instance_id')='text'
                   GROUP BY project_id,instance_id HAVING count(DISTINCT run_id)>1
               )
               UPDATE task_runs SET result=json_set(result,'$.purchase_binding_conflict',json('true'))
               WHERE json_type(result,'$.purchase_binding_conflict') IS NOT 'true' AND id IN (
                   SELECT o.run_id FROM duplicates d JOIN task_operations o
                   ON o.project_id=d.project_id AND json_extract(o.data,'$.instance_id')=d.instance_id
                   WHERE o.action='create' AND o.state='accepted'
               )"""
        )
        # Adjustment evidence survives the task definition just like purchase receipts.
        if conn.execute("PRAGMA foreign_key_list(recovery_purchases)").fetchone() is not None:
            conn.execute("ALTER TABLE recovery_purchases RENAME TO recovery_purchases_previous")
            conn.execute("""CREATE TABLE recovery_purchases (
                task_id TEXT PRIMARY KEY, adjustment INTEGER NOT NULL CHECK(adjustment>=0))""")
            conn.execute("INSERT INTO recovery_purchases SELECT task_id,adjustment FROM recovery_purchases_previous")
            conn.execute("DROP TABLE recovery_purchases_previous")
    conn.execute(
        """INSERT OR IGNORE INTO external_shutdown_observations (run_id,instance_id,region,zone,stop_at)
           SELECT id,json_extract(result,'$.instance_id'),json_extract(result,'$.region'),
                  json_extract(result,'$.zone'),json_extract(result,'$.shutdown_at')
           FROM task_runs WHERE state='succeeded' AND json_extract(snapshot,'$.kind')='shutdown'
            AND json_type(result,'$.shutdown_at')='integer' AND json_extract(result,'$.shutdown_at')>?
            AND json_extract(result,'$.previous_shutdown_at')=json_extract(result,'$.shutdown_at')
            AND json_extract(result,'$.shutdown_outcome') IS NULL
            AND json_type(result,'$.instance_id')='text' AND json_type(result,'$.region')='text'
            AND json_type(result,'$.zone')='text'""", (int(time()),),
    )


def encode(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def task_definition_hash(definition: dict) -> str:
    return sha256(encode(definition).encode("utf-8")).hexdigest()


def require_row(row: sqlite3.Row | None, message: str) -> sqlite3.Row:
    if row is None:
        raise KeyError(message)
    return row


def task_view(row: sqlite3.Row) -> dict:
    task = dict(row)
    task.pop("status_before_delete", None)
    task.update(json.loads(task.pop("definition")))
    task["next_at_shanghai"] = shanghai_time(task["next_at"])
    if "pending_reason" in task:
        task["last_run"] = json.loads(task["last_run"]) if task["last_run"] else None
        task["purge_at"] = None
        if task["deleted_at"] is not None:
            task["view"] = "trash"
            task["purge_at"] = task["deleted_at"] + TRASH_RETENTION_SECONDS
        elif task["status"] in {"completed", "canceled"} and not task["pending_reason"]:
            task["view"] = "history"
        else:
            task["view"] = "current"
    return task


def run_view(row: sqlite3.Row) -> dict:
    run = dict(row)
    run["snapshot"] = json.loads(run["snapshot"])
    run["result"] = json.loads(run["result"])
    run["name"] = run["snapshot"]["name"]
    run["kind"] = run["snapshot"]["kind"]
    if "warning_dismissed" in run:
        run["warning_dismissed"] = bool(run["warning_dismissed"])
    if run["kind"] != "cancel_shutdown":
        followup = run.get("shutdown_followup_id", run["result"].get("shutdown_run_id"))
        run["shutdown_reviewable"] = not followup and run["phase"] != "review_shutdown" and shutdown_review_target(run) is not None
    if not run.get("shutdown_followup_id"):
        run.pop("shutdown_followup_id", None)
    return run


def current_shutdown_review(result: dict) -> dict:
    review = result.get("shutdown_review") or {}
    observation = result.get("shutdown_observation") or {}
    if result.get("shutdown_outcome") == "missed" and observation.get("checked_at", 0) > review.get("checked_at", 0):
        return {}
    return review


def shutdown_review_target(run: dict) -> tuple[dict, int] | None:
    result, config = run["result"], run["snapshot"].get("config", {})
    if current_shutdown_review(result).get("resolved") is True:
        return None
    if run["kind"] == "shutdown" and (
        run["state"] in {"manual_review", "failed", "expired", "missed"}
        or result.get("shutdown_outcome") == "missed"
    ):
        scope = config
        target = result.get("shutdown_at") or config.get("shutdown_at") or run["scheduled_at"]
    elif run["kind"] in {"start", "acquire"} and (
        run["state"] == "succeeded" and result.get("shutdown_confirmation") == "unconfirmed"
    ):
        scope = result
        target = result.get("shutdown_at")
    else:
        return None
    if type(target) is not int or target <= 0:
        return None
    keys = ("instance_id", "region", "zone")
    if not all(isinstance(scope.get(key), str) and scope[key] for key in keys):
        return None
    return {key: scope[key] for key in keys}, target


def run_with_business(row: sqlite3.Row) -> dict:
    run = run_view(row)
    attention = bool(run.pop("business_attention"))
    task_id = run.pop("business_task_id")
    released_at = run.pop("business_released_at")
    linked = json.loads(run.pop("business_linked_shutdown"))
    subject, kind = run, run["kind"]
    destination = {"page": "tasks", "project_id": run["project_id"]}
    destination.update({"task_id": task_id} if task_id else {"run_id": run["id"]})
    if run.get("shutdown_followup_id") and run["result"].get("shutdown_confirmation") == "unconfirmed":
        subject, kind = linked, "shutdown"
        destination["run_id"] = run["shutdown_followup_id"]
        attention = linked["phase"] == "review_shutdown" or (
            current_shutdown_review(linked["result"]).get("resolved") is not True
            and (linked["state"] in {"manual_review", "failed", "expired", "missed"}
                 or linked["result"].get("shutdown_outcome") == "missed")
        )
    state, result = subject["state"], subject["result"]
    review = current_shutdown_review(result)
    detail = subject["reason"]
    if subject["phase"] == "review_shutdown" or (state == "reconciling" and subject["phase"] == "review_cancel_shutdown"):
        status, label, detail = "in_progress", "正在核对预约", "正在读取原实例的当前关机状态和云端预约"
    elif state == "reconciling" and result.get("instance_recheck_pending"):
        status, label = "in_progress", "正在核对实例操作"
    elif result.get("shutdown_cleanup_pending"):
        status, label, detail = "needs_attention", "旧预约清理待确认", "已提前关机，等待核对云端旧预约"
    elif result.get("purchase_binding_conflict"):
        status, label, detail = "needs_attention", "采购归属待核对", "同一实例关联多笔采购记录"
    elif result.get("shutdown_outcome") in {"early", "canceled", "superseded", "released"}:
        status, label = "completed", {
            "early": "已提前关机", "canceled": "已撤销", "superseded": "已改期", "released": "实例已释放",
        }[result["shutdown_outcome"]]
        detail = label
    elif result.get("shutdown_cutoff_reached"):
        status, label = "history", "超过最晚开机时间"
    elif result.get("shutdown_outcome") == "confirmed" and review.get("outcome") not in {"kept", "stopped"}:
        status, label, detail = "completed", "已确认关机", "已回读到实例关机的证据"
    elif state == "reviewed" or released_at is not None or review.get("resolved") is True:
        status, label = "resolved", {
            "matching": "已确认目标预约", "kept": "已保留云端预约", "stopped": "已确认关机",
        }.get(review.get("outcome"), "已核对")
        detail = review.get("detail") or detail
    elif result.get("shutdown_outcome") == "missed":
        status, label, detail = "needs_attention", "关机结果待处理", review.get("detail") or "已过预约时间，未回读确认实例关机"
    elif result.get("shutdown_confirmation") == "unconfirmed":
        status, label = "needs_attention", "关机预约待处理"
        detail = review.get("detail") or "该次运行关联的云端关机预约尚未确认"
    elif attention:
        status, label = "needs_attention", "待处理"
        detail = review.get("detail") or detail
    elif result.get("automation_reminded"):
        status, label = "history", "仅提醒"
    elif state == "succeeded" and (kind == "shutdown" or result.get("shutdown_confirmation") == "confirmed"):
        status, label = "completed", "关机预约已登记"
    else:
        status, label = {
            "queued": ("in_progress", "已排队"), "waiting": ("in_progress", "等待中"),
            "reconciling": ("in_progress", "核对中"), "succeeded": ("completed", "已完成"),
            "failed": ("failed", "执行失败"), "expired": ("history", "窗口已结束"),
            "missed": ("history", "已错过触发"), "canceled": ("history", "已取消"),
        }.get(state, ("unknown", "状态待核实"))
    run["business"] = {
        "status": status, "label": label, "attention": attention,
        "detail": detail, "observed_at": released_at or max(
            review.get("checked_at", 0), (result.get("shutdown_observation") or {}).get("checked_at", 0),
        ) or subject["updated_at"],
        "destination": destination,
    }
    return run


def shutdown_cleanup_pending(conn: sqlite3.Connection, project_id: str, instance_id: str) -> bool:
    return conn.execute(
        """SELECT 1 FROM shutdown_owners o JOIN task_runs r ON r.id=o.run_id
           WHERE o.project_id=? AND o.instance_id=? AND json_type(r.result,'$.shutdown_cleanup_pending')='true'""",
        (project_id, instance_id),
    ).fetchone() is not None


def write_audit(conn: sqlite3.Connection, run_id: str, action: str, data: dict, now: int) -> None:
    conn.execute(
        "INSERT INTO task_audit(run_id,action,data,created_at) VALUES (?,?,?,?)",
        (run_id, action, encode(data), now),
    )


def conflicts_with_create_receipt(data: dict, instance_id: str) -> bool:
    submitted_ids = data.get("submitted_ids") or []
    bound_id = data.get("instance_id")
    return bool((submitted_ids and instance_id not in submitted_ids) or (bound_id and instance_id != bound_id))


def insert_run(
    conn: sqlite3.Connection,
    snapshot: dict,
    scheduled_at: int,
    now: int,
    *,
    request_id: str | None = None,
    source_task_id: str | None = None,
    state: str = "queued",
    reason: str | None = None,
) -> dict:
    run_id = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO task_runs(id,task_id,project_id,scheduled_at,deadline_at,request_id,state,
             reason,snapshot,next_check,created_at,updated_at,source_task_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id, snapshot.get("id"), snapshot["project_id"], scheduled_at, run_deadline(snapshot, scheduled_at),
            request_id, state, reason, encode(snapshot), now, now, now, source_task_id or snapshot.get("id"),
        ),
    )
    return run_view(conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone())


def task_snapshot(task: dict) -> dict:
    return {name: task[name] for name in (
        "id", "project_id", "name", "kind", "config", "channels", "schedule", "window_seconds", "version",
        "template_id", "template_version", "notification_events",
    ) if name in task}


def prepare_purchase_retry(conn: sqlite3.Connection, body: dict) -> dict:
    project_id = text(body.get("project_id"), "项目 ID", 100)
    config = normalize_purchase_spec(conn, project_id, body["spec"])
    retry = body.get("retry", True)
    if type(retry) is not bool:
        raise ValueError("持续尝试开关须为布尔值")
    window = integer(body.get("window_seconds"), "等待秒数", PURCHASE_RETRY_MIN_SECONDS,
                     PURCHASE_RETRY_MAX_SECONDS) if retry else PURCHASE_PREVIEW_SECONDS
    validated = validate_config("acquire", config | {
        "max_price": body.get("max_price"), "skip_if_match": False, "purchase_limit": 1, "email": body.get("email"),
    })
    config.update(max_price=validated["max_price"], skip_if_match=False, purchase_limit=1)
    if "email" in validated:
        config["email"] = validated["email"]
    channels = channel_list(body.get("channels", ["site"]), config)
    return {
        "kind": "acquire", "name": "持续尝试创建实例" if retry else "立即创建实例", "project_id": project_id,
        "channels": channels, "config": config, "purchase_retry": retry, "window_seconds": window,
    }


def purchase_retry_preview(snapshot: dict) -> dict:
    config = snapshot["config"]
    zone = config["zones"][0]
    spec = {key: config[key] for key in SPEC_FIELDS + ("quantity",) if key in config}
    spec.update(region=zone["region"], zone=zone["zone"], disks=zone.get("resolved_disks", config["disks"]))
    if shared_storage := zone.get("shared_storage", config.get("shared_storage")):
        spec["shared_storage"] = shared_storage
    return {
        "project_id": snapshot["project_id"], "spec": spec, "max_price": config["max_price"],
        "price_unit": "CNY/order" if config["charge_type"] in {"Day", "Month"} else "CNY/hour",
        "window_seconds": snapshot["window_seconds"], "retry": snapshot["purchase_retry"],
        "channels": snapshot["channels"], "email": config.get("email"),
    }


def _purchase_retry_identity(snapshot: dict) -> dict:
    spec = purchase_retry_preview(snapshot)["spec"]
    if shared := spec.get("shared_storage"):
        spec["shared_storage"] = {key: value for key, value in shared.items() if key != "max_price"}
    return spec


def _validate_task_purchase(conn: sqlite3.Connection, body: dict) -> dict:
    definition = validate_task(body)
    config = definition["config"]
    if definition["kind"] == "start":
        cardless.require_snapshot_target(conn, definition["project_id"], config)
    if config.get("stock_pool"):
        for zone in config["zones"]:
            catalog.resolve_compute(conn, config | zone)
    if definition["kind"] == "acquire":
        definition["config"] = normalize_purchase_config(conn, definition["project_id"], definition["config"])
    return definition


def resolve_template(conn: sqlite3.Connection, body: dict, *, check_version: bool = True) -> dict:
    if not isinstance(body, dict):
        raise ValueError("任务请求无效")
    template_id = body.get("template_id")
    if template_id is None:
        return _validate_task_purchase(conn, body)
    template_id = text(template_id, "模板 ID")
    if body.get("kind") not in {"stock", "acquire"}:
        raise ValueError("只有库存通知或按模板获取实例任务可关联模板")
    project_id = text(body.get("project_id"), "项目")
    row = conn.execute(
        "SELECT version,config FROM templates WHERE id=? AND project_id=?", (template_id, project_id)
    ).fetchone()
    if row is None:
        raise ValueError("当前项目中找不到所选模板")
    submitted_version = body.get("template_version")
    if check_version and submitted_version is not None and integer(submitted_version, "模板版本") != row["version"]:
        raise ValueError("模板已更新，请刷新并用最新版本重新预览")
    source = body.get("config")
    if not isinstance(source, dict):
        raise ValueError("任务配置无效")
    template_fields = {*SPEC_FIELDS, "zones", "disks", "quantity", "skip_if_match", "zone_storage", "shared_storage",
                       "cpu_auto", "stock_pool"}
    config = {key: value for key, value in source.items() if key not in template_fields}
    template_config = json.loads(row["config"])
    config.update({key: value for key, value in template_config.items() if key in template_fields})
    if source.get("cpu_platform") is None or source.get("cpu_auto") or source.get("stock_pool"):
        config.pop("cpu_platform", None)
    elif not check_version:
        config.pop("cpu_auto", None)
        config.pop("stock_pool", None)
        config["cpu_platform"] = source["cpu_platform"]
    definition = _validate_task_purchase(conn, body | {"config": config})
    if definition["kind"] == "stock" and not definition["config"].get("stock_pool"):
        resolved = {
            (zone["region"], zone["zone"]): zone["resolved_disks"]
            for zone in template_config["zones"] if "resolved_disks" in zone
        }
        for zone in definition["config"]["zones"]:
            if (zone["region"], zone["zone"]) in resolved:
                zone["resolved_disks"] = resolved[zone["region"], zone["zone"]]
    return definition | {"template_id": template_id, "template_version": row["version"]}


def start_trigger_conflict(
    conn: sqlite3.Connection, task: dict, scheduled_at: int, now: int, *, automatic: bool,
) -> str | None:
    scope = {"project_id": task["project_id"], **task["config"]}
    if automatic and conn.execute(
        f"""SELECT 1 FROM task_runs r WHERE {INSTANCE_CONFIG_TARGET}
            AND r.request_id GLOB 'action:*' AND json_extract(r.snapshot,'$.kind')='stop'
            AND r.created_at>=:since
            AND r.state IN ('queued','waiting','reconciling','manual_review','succeeded') LIMIT 1""",
        scope | {"since": max(scheduled_at, task["created_at"])},
    ).fetchone():
        return "已提交手动关机，跳过本次定时开机"
    matching = conn.execute(
        f"""SELECT r.* FROM task_runs r WHERE {SCHEDULED_START_TARGET}
            AND ((r.phase IS NULL AND r.stopped=0 AND r.state IN ('queued','waiting'))
                 OR (r.phase='verify_start' AND r.state IN ('reconciling','manual_review')))""",
        scope,
    )
    if any(run["phase"] or not run_window_expired(run, now) for run in map(run_view, matching)):
        return "同一实例已有开机运行正在等待或核对"
    return None


def enqueue_trigger(
    conn: sqlite3.Connection, task: dict, scheduled_at: int, now: int, request_id: str | None = None,
    *, feishu_configured: Callable[[sqlite3.Connection], bool] | None = None,
) -> dict:
    snapshot = task_snapshot(task)
    if task["kind"] in {"start", "stop", "shutdown"} and termination_pending(
        conn, task["project_id"], task["config"]["instance_id"],
    ):
        reason = "实例存在待核对的释放操作"
        if request_id is not None:
            raise InstanceActionUnavailable(reason)
        return insert_run(conn, snapshot, scheduled_at, now, state="missed", reason=reason)
    if task["kind"] == "start":
        conflict = start_trigger_conflict(conn, task, scheduled_at, now, automatic=request_id is None)
        if conflict:
            return insert_run(
                conn, snapshot, scheduled_at, now, request_id=request_id, state="missed", reason=conflict,
            )
    if not task.get("template_id"):
        return insert_run(conn, snapshot, scheduled_at, now, request_id=request_id)
    reason = None
    try:
        current = resolve_template(conn, snapshot, check_version=False)
        old, new = snapshot["config"], current["config"]
        if task["kind"] == "acquire" and any(old.get(key) != new.get(key) for key in ("charge_type", "quantity")):
            reason = "模板的计费方式或购买时长已改变，请重新预览并保存预算"
        snapshot.update(current)
    except ValueError as error:
        reason = str(error)
    if reason:
        run = insert_run(
            conn, snapshot, scheduled_at, now, request_id=request_id, state="manual_review", reason=reason,
        )
        conn.execute("UPDATE task_runs SET result=? WHERE id=?", (encode({"task_config_issue": True}), run["id"]))
        run["result"] = {"task_config_issue": True}
        policy_event = "operation_review" if task["kind"] == "acquire" else None
        channels = snapshot["channels"] if includes_event(snapshot, "task_result") else []
        feishu_notices.emit(conn, run["id"] + ":review", {
            "title": snapshot["name"], "message": reason, "run_id": run["id"], "project_id": run["project_id"],
            "recipient": snapshot["config"].get("email"), "confirmed_at": now,
        }, channels, policy_event=policy_event,
            feishu_configured=bool(policy_event and feishu_configured and feishu_configured(conn)))
        return run
    return insert_run(conn, snapshot, scheduled_at, now, request_id=request_id)


def termination_pending(conn: sqlite3.Connection, project_id: str, instance_id: str) -> bool:
    from gpu_console.application.instance_release import has_pending_termination

    return has_pending_termination(conn, project_id, instance_id)


def upsert_history(
    conn: sqlite3.Connection, task: dict, scheduled_at: int, now: int, reason: str
) -> None:
    row = conn.execute(
        "SELECT id FROM task_runs WHERE task_id=? AND scheduled_at=? AND request_id IS NULL",
        (task["id"], scheduled_at),
    ).fetchone()
    if row is None:
        insert_run(conn, task_snapshot(task), scheduled_at, now, state="missed", reason=reason)


def mark_restored(conn: sqlite3.Connection, restored_at: int) -> None:
    """Advance trigger cursors across the restore boundary without replaying operations."""
    instance_quotes.invalidate_unconsumed(conn)
    conn.execute("DELETE FROM instance_purchase_previews")
    conn.execute(
        """UPDATE task_runs SET state='missed', reason='恢复旧备份后跳过历史触发', stopped=1, updated_at=?
           WHERE state IN ('queued','waiting') AND phase IS NULL AND scheduled_at<=?""",
        (restored_at, restored_at),
    )
    conn.execute(
        """UPDATE task_runs SET stopped=1,reason='从旧备份恢复后请核对云端回执',updated_at=?
           WHERE state IN ('reconciling','manual_review')""",
        (restored_at,),
    )
    for row in conn.execute("SELECT * FROM tasks WHERE next_at<=?", (restored_at,)).fetchall():
        task = task_view(row)
        upsert_history(conn, task, task["next_at"], restored_at, "恢复旧备份后跳过历史触发")
        next_at = next_time(task["schedule"], restored_at, inclusive=False)
        status = "completed" if task["status"] == "active" and next_at is None else task["status"]
        conn.execute(
            "UPDATE tasks SET next_at=?,status=?,updated_at=? WHERE id=?",
            (next_at, status, restored_at, task["id"]),
        )


class TaskStore:
    def __init__(
        self, path: Path, clock: Callable[[], float] = time,
        *, feishu_configured: Callable[[sqlite3.Connection], bool] | None = None,
    ):
        self.path = path
        self.clock = clock
        self._feishu_configured = feishu_configured

    def now(self) -> int:
        return int(self.clock())

    def feishu_configured(self, conn: sqlite3.Connection) -> bool:
        return self._feishu_configured is not None and self._feishu_configured(conn)

    def list_tasks(self, project_id: str | None = None, *, view: TaskListView | None = None) -> list[dict]:
        with storage.connect(self.path) as conn:
            rows = conn.execute(
                f"""SELECT {TASK_LIST_COLUMNS} FROM tasks t
                    WHERE (? IS NULL OR t.project_id=?) AND (t.deleted_at IS NOT NULL)=?
                    ORDER BY t.deleted_at DESC,t.created_at DESC,t.id""",
                (project_id, project_id, view == "trash"),
            )
            tasks = [task_view(row) for row in rows]
            return tasks if view is None else [task for task in tasks if task["view"] == view]

    def get_task(self, task_id: str, project_id: str | None = None) -> dict:
        with storage.connect(self.path) as conn:
            return self._task(conn, task_id, project_id)

    def _task(self, conn: sqlite3.Connection, task_id: str, project_id: str | None = None) -> dict:
        row = conn.execute(
            f"SELECT {TASK_LIST_COLUMNS} FROM tasks t WHERE t.id=? AND (? IS NULL OR t.project_id=?)",
            (task_id, project_id, project_id),
        ).fetchone()
        message = "任务不存在" if project_id is None else "当前项目中找不到该任务"
        return task_view(require_row(row, message))

    def change_trash(
        self, project_id: str, task_id: str, expected_version: int, action: TaskTrashAction,
    ) -> dict:
        with storage.connect(self.path) as conn, storage.transaction(conn):
            now = self.now()
            task = self._task(conn, task_id, project_id)
            if task["version"] != expected_version:
                raise TaskTrashConflict("任务版本已变化，请刷新后重试")
            if action == "trash":
                if task["deleted_at"] is not None:
                    return task
                conn.execute(
                    """UPDATE tasks SET deleted_at=?,status_before_delete=status,status='canceled',
                           next_at=NULL,version=version+1,updated_at=? WHERE id=?""",
                    (now, now, task_id),
                )
                conn.execute(
                    f"""UPDATE task_runs AS r SET stopped=1,updated_at=?,cancel_shutdown_on_confirmation=1,
                             state=CASE WHEN phase IS NULL THEN 'canceled' ELSE state END,
                             reason=CASE WHEN phase IS NULL THEN '任务已移入回收站，停止后续尝试' ELSE reason END
                       WHERE project_id=? AND {RUN_SOURCE_TASK}=?
                         AND state IN ('queued','waiting','reconciling','manual_review')
                         AND json_extract(snapshot,'$.kind')!='cancel_shutdown'""",
                    (now, project_id, task_id),
                )
                conn.execute(
                    f"""UPDATE task_runs AS r SET result=json_set(r.result,'$.shutdown_confirmation','canceled')
                       WHERE r.project_id=? AND {RUN_SOURCE_TASK}=?
                         AND EXISTS (SELECT 1 FROM task_runs child WHERE child.project_id=r.project_id
                             AND child.id=json_extract(r.result,'$.shutdown_run_id') AND child.state='canceled')""",
                    (project_id, task_id),
                )
                owners = conn.execute(
                    f"""SELECT o.* {SHUTDOWN_OWNER_RUNS}
                        WHERE o.project_id=? AND {SHUTDOWN_OWNER_TASK}=? AND o.stop_at>?""",
                    (project_id, task_id, now),
                ).fetchall()
                for owner in owners:
                    enqueue_owner_cancel(conn, task_snapshot(task), dict(owner), now)
                self._clear_task_previews(conn, project_id, task_id)
            elif task["deleted_at"] is None:
                raise TaskTrashConflict("任务不在回收站")
            elif action == "restore":
                if task["purge_at"] <= now:
                    raise TaskTrashConflict("回收期限已过，任务无法恢复")
                status = conn.execute("SELECT status_before_delete FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
                next_at = None
                if status in {"active", "paused"}:
                    next_at = self._next_restorable_at(conn, task, now)
                    status = "paused" if next_at is not None else "completed"
                conn.execute(
                    """UPDATE tasks SET deleted_at=NULL,status_before_delete=NULL,next_at=?,status=?,
                           version=version+1,updated_at=? WHERE id=?""",
                    (next_at, status, now, task_id),
                )
            elif action == "purge":
                if task["pending_reason"]:
                    raise TaskTrashConflict(task["pending_reason"])
                self._purge_task(conn, project_id, task_id)
                return {"id": task_id, "project_id": project_id, "purged": True}
            else:
                raise ValueError("回收站操作无效")
            return self._task(conn, task_id)

    @staticmethod
    def _next_restorable_at(conn: sqlite3.Connection, task: dict, now: int) -> int | None:
        earliest = now + (SHUTDOWN_MINIMUM if task["kind"] == "shutdown" else 1)
        next_at = next_time(task["schedule"], earliest)
        # A shutdown may have been enqueued ahead of its scheduled time.
        while next_at is not None and conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id=? AND scheduled_at=? AND request_id IS NULL",
            (task["id"], next_at),
        ).fetchone():
            next_at = next_time(task["schedule"], next_at, inclusive=False)
        return next_at

    @staticmethod
    def _clear_task_previews(conn: sqlite3.Connection, project_id: str, task_id: str) -> None:
        conn.execute("DELETE FROM task_previews WHERE task_id=?", (task_id,))
        conn.execute(
            """DELETE FROM mcp_previews WHERE operation LIKE 'task:%'
               AND json_extract(data,'$.project_id')=? AND json_extract(data,'$.task_id')=?""",
            (project_id, task_id),
        )

    @staticmethod
    def _purge_task(conn: sqlite3.Connection, project_id: str, task_id: str) -> None:
        TaskStore._clear_task_previews(conn, project_id, task_id)
        conn.execute(
            "UPDATE task_runs SET source_task_id=COALESCE(source_task_id,task_id),task_id=NULL WHERE task_id=?",
            (task_id,),
        )
        # Keep idempotency keys while retiring copies of the editable task configuration.
        conn.execute(
            """UPDATE mcp_receipts SET result=json_object('id',?,'project_id',?,'purged',json('true'))
               WHERE project_id=? AND (operation LIKE 'task:%' OR operation IN ('gpu_create_task','gpu_update_task'))
                 AND json_extract(result,'$.id')=?""",
            (task_id, project_id, project_id, task_id),
        )
        conn.execute("DELETE FROM tasks WHERE project_id=? AND id=?", (project_id, task_id))

    def purge_expired_tasks(self, limit: int = 50) -> int:
        with storage.connect(self.path) as conn, storage.transaction(conn):
            rows = conn.execute(
                f"""SELECT t.project_id,t.id FROM tasks t WHERE t.deleted_at<=?
                     AND ({TASK_PENDING_REASON}) IS NULL ORDER BY t.deleted_at,t.id LIMIT ?""",
                (self.now() - TRASH_RETENTION_SECONDS, limit),
            ).fetchall()
            for row in rows:
                self._purge_task(conn, row["project_id"], row["id"])
            return len(rows)

    def create_task(self, body: dict) -> dict:
        now = self.now()
        task_id = uuid.uuid4().hex
        with storage.connect(self.path) as conn, storage.transaction(conn):
            definition = resolve_template(conn, body)
            next_at = self._valid_initial_time(definition, now)
            conn.execute(
                """INSERT INTO tasks(id,project_id,status,version,definition,next_at,created_at,updated_at)
                   VALUES (?,?,'active',1,?,?,?,?)""",
                (task_id, definition["project_id"], encode(definition), next_at, now, now),
            )
            return self._task(conn, task_id)

    def update_task(self, task_id: str, body: dict) -> dict:
        now = self.now()
        with storage.connect(self.path) as conn, storage.transaction(conn):
            definition = resolve_template(conn, body)
            existing = self._task(conn, task_id)
            self._validate_edit(existing, definition)
            preview_token = body.get("preview_token")
            if preview_token is None and any(definition["config"].get(key) and not existing["config"].get(key)
                                             for key in ("cpu_auto", "stock_pool")):
                raise ValueError("切换规格规则前请重新预览并确认任务")
            if preview_token is not None:
                receipt = conn.execute(
                    """SELECT 1 FROM task_previews
                       WHERE task_id=? AND task_version=? AND definition_hash=? AND token=?""",
                    (task_id, existing["version"], task_definition_hash(definition), text(preview_token, "预览凭证")),
                ).fetchone()
                if receipt is None:
                    raise ValueError("任务配置或版本已变化，请重新预览后保存")
            next_at = existing["next_at"]
            status = existing["status"]
            if existing["schedule"] != definition["schedule"]:
                next_at = self._valid_initial_time(definition, now)
                status = "active" if status == "completed" else status
            conn.execute(
                """UPDATE tasks SET version=version+1,definition=?,next_at=?,status=?,updated_at=? WHERE id=?""",
                (encode(definition), next_at, status, now, task_id),
            )
            conn.execute("DELETE FROM task_previews WHERE task_id=?", (task_id,))
            if preview_token is not None:
                self._resolve_task_config_issues(conn, existing, now)
            return self._task(conn, task_id)

    @staticmethod
    def _validate_edit(existing: dict, definition: dict) -> None:
        if existing["deleted_at"] is not None:
            raise TaskTrashConflict("请先从回收站恢复任务")
        if existing["status"] == "canceled":
            raise ValueError("已取消的任务无法编辑")
        if existing["project_id"] != definition["project_id"] or existing["kind"] != definition["kind"]:
            raise ValueError("修改项目或任务类型时请新建任务")

    @staticmethod
    def _resolve_task_config_issues(conn: sqlite3.Connection, task: dict, now: int) -> None:
        evidence = {"task_version": task["version"] + 1, "checked_at": now}
        runs = conn.execute(
            f"""SELECT r.id,r.reason FROM task_runs r
                WHERE r.task_id=? AND r.project_id=? AND {RUN_CONFIG_ISSUE}""",
            (task["id"], task["project_id"]),
        ).fetchall()
        for run in runs:
            conn.execute(
                """UPDATE task_runs SET state='reviewed',reason='已保存经预览的新任务配置',updated_at=?,
                     result=json_set(result,'$.task_config_resolution',json(?)) WHERE id=?""",
                (now, encode(evidence), run["id"]),
            )
            write_audit(conn, run["id"], "task_config_updated", evidence | {"original_reason": run["reason"]}, now)

    @staticmethod
    def _valid_initial_time(definition: dict, now: int) -> int:
        earliest = now + 300 if definition["kind"] == "shutdown" else now - 60
        scheduled_at = next_time(definition["schedule"], earliest)
        if scheduled_at is None or run_deadline(definition, scheduled_at) < now:
            raise ValueError("计划时间已过或距离关机不足 5 分钟")
        return scheduled_at

    def preview(self, body: dict, instances: list[dict] | tuple[dict, ...] = ()) -> dict:
        task_id = body.get("task_id") if isinstance(body, dict) else None
        preview_token = None
        now = self.now()
        with storage.connect(self.path) as conn, (
            storage.transaction(conn) if task_id is not None else nullcontext()
        ):
            snapshot = resolve_template(conn, body)
            if task_id is None:
                scheduled_at = self._valid_initial_time(snapshot, now)
            else:
                existing = self._task(conn, text(task_id, "任务 ID"))
                self._validate_edit(existing, snapshot)
                if existing["version"] != integer(body.get("task_version"), "任务版本"):
                    raise ValueError("任务版本已更新，请重新打开任务编辑")
                scheduled_at = (
                    existing["next_at"] if existing["schedule"] == snapshot["schedule"]
                    else self._valid_initial_time(snapshot, now)
                )
                preview_token = uuid.uuid4().hex
                conn.execute(
                    """INSERT INTO task_previews(task_id,task_version,definition_hash,token) VALUES (?,?,?,?)
                       ON CONFLICT(task_id) DO UPDATE SET task_version=excluded.task_version,
                          definition_hash=excluded.definition_hash,token=excluded.token""",
                    (existing["id"], existing["version"], task_definition_hash(snapshot), preview_token),
                )
        config = snapshot["config"]
        if snapshot["kind"] == "acquire":
            allowed = {
                (zone["region"], zone["zone"]): config | {"disks": zone.get("resolved_disks", config["disks"])}
                for zone in config["zones"]
            }
            matches = [item for item in instances if (item.get("region"), item.get("zone")) in allowed
                       and match_spec(item, allowed[item["region"], item["zone"]]) is not False]
        else:
            matches = [item for item in instances if item.get("id") == config.get("instance_id")]
        deadline_at = run_deadline(snapshot, scheduled_at) if scheduled_at is not None else None
        response = {
            "scheduled_at": scheduled_at, "scheduled_at_shanghai": shanghai_time(scheduled_at),
            "deadline_at": deadline_at, "deadline_at_shanghai": shanghai_time(deadline_at),
            "matching_instances": matches, "max_price": config.get("max_price"),
            "purchase_limit": config.get("purchase_limit"),
            "template_id": snapshot.get("template_id"), "template_version": snapshot.get("template_version"),
        }
        if snapshot["kind"] == "start":
            response.update(boot_mode=config.get("boot_mode", "with_gpu"), without_gpu_spec=config.get("without_gpu_spec"),
                            shutdown_after_seconds=config.get("shutdown_after_seconds"))
        if preview_token is not None:
            response["preview_token"] = preview_token
        return response

    def control_task(self, task_id: str, action: str) -> dict:
        now = self.now()
        with storage.connect(self.path) as conn, storage.transaction(conn):
            task = self._task(conn, task_id)
            if task["deleted_at"] is not None:
                raise TaskTrashConflict("请先从回收站恢复任务")
            if task["status"] == "canceled" and action != "cancel":
                raise ValueError("已取消的任务无法恢复")
            if action == "resume":
                next_at = task["next_at"]
                if next_at is not None and run_deadline(task, next_at) < now:
                    upsert_history(conn, task, next_at, now, "暂停期间错过计划时间")
                    next_at = next_time(task["schedule"], now)
                status = "active" if next_at is not None else "completed"
            elif action in {"pause", "cancel"}:
                status = "paused" if action == "pause" else "canceled"
                next_at = task["next_at"] if action == "pause" else None
                conn.execute(
                    f"""UPDATE task_runs AS r SET stopped=1,updated_at=?,
                         cancel_shutdown_on_confirmation=CASE WHEN ? THEN 1 ELSE cancel_shutdown_on_confirmation END,
                         state=CASE
                         WHEN phase IS NULL THEN 'canceled' ELSE state END,
                         reason=CASE WHEN phase IS NULL THEN '任务已停止后续尝试' ELSE reason END
                       WHERE project_id=? AND {RUN_SOURCE_TASK}=? AND state IN ('queued','waiting','reconciling','manual_review')
                         AND json_extract(snapshot,'$.kind')!='cancel_shutdown'
                         AND (? OR json_extract(snapshot,'$.config.parent_run_id') IS NULL)""",
                    (now, action == "cancel", task["project_id"], task_id, action == "cancel"),
                )
                if action == "cancel":
                    for owner in conn.execute(
                        f"""SELECT o.* {SHUTDOWN_OWNER_RUNS}
                            WHERE o.project_id=? AND {SHUTDOWN_OWNER_TASK}=? AND o.stop_at>?""",
                        (task["project_id"], task_id, now),
                    ).fetchall():
                        enqueue_owner_cancel(conn, task_snapshot(task), dict(owner), now)
            else:
                raise ValueError("操作类型无效")
            conn.execute(
                "UPDATE tasks SET version=version+(status<>?),status=?,next_at=?,updated_at=? WHERE id=?",
                (status, status, next_at, now, task_id),
            )
            return self._task(conn, task_id)

    def trigger_task(self, task_id: str, request_id: str) -> dict:
        now = self.now()
        request_id = "task:" + task_id + ":" + text(request_id, "请求标识")
        with storage.connect(self.path) as conn, storage.transaction(conn):
            existing = conn.execute("SELECT * FROM task_runs WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                return run_view(existing)
            task = self._task(conn, task_id)
            if task["deleted_at"] is not None:
                raise TaskTrashConflict("请先从回收站恢复任务")
            if task["status"] in {"paused", "canceled"}:
                raise ValueError("恢复任务后才能立即执行")
            snapshot = task_snapshot(task)
            scheduled_at = max(now, task["next_at"] or task["schedule"]["at"]) if task["kind"] == "shutdown" else now
            if run_deadline(snapshot, scheduled_at) < now:
                raise ValueError("距离关机不足 5 分钟")
            return enqueue_trigger(conn, task, scheduled_at, now, request_id=request_id,
                                   feishu_configured=self.feishu_configured)

    def preview_purchase_retry(self, body: dict) -> dict:
        now = self.now()
        preview_id = uuid.uuid4().hex
        expires_at = now + PURCHASE_PREVIEW_SECONDS
        with storage.connect(self.path) as conn, storage.transaction(conn):
            snapshot = prepare_purchase_retry(conn, body)
            self._validate_purchase_channels(conn, snapshot)
            conn.execute("DELETE FROM instance_purchase_previews WHERE expires_at<=?", (now,))
            conn.execute(
                "INSERT INTO instance_purchase_previews VALUES (?,?,?)", (preview_id, encode(snapshot), expires_at),
            )
        return purchase_retry_preview(snapshot) | {"preview_id": preview_id, "expires_at": expires_at}

    def _validate_purchase_channels(self, conn: sqlite3.Connection, snapshot: dict) -> None:
        if "feishu" in snapshot["channels"] and not self.feishu_configured(conn):
            raise ValueError("飞书通知尚未配置")
        if "email" in snapshot["channels"] and not conn.execute(
            "SELECT 1 FROM system_status WHERE component='channel_email' AND error='configured'",
        ).fetchone():
            raise ValueError("邮件通知尚未配置")

    def _insert_purchase_retry(
        self, conn: sqlite3.Connection, snapshot: dict, request_id: str, *, read_only: bool,
    ) -> dict:
        if read_only or storage.get_settings(conn)["restore_gate"]:
            raise InstanceActionUnavailable("服务处于只读或恢复核对状态，暂不能创建实例")
        self._validate_purchase_channels(conn, snapshot)
        project_id = snapshot["project_id"]
        spec = purchase_retry_preview(snapshot)["spec"]
        verify_purchase_spec(conn, project_id, spec)
        identity = _purchase_retry_identity(snapshot)
        for row in conn.execute(
            """SELECT snapshot FROM task_runs WHERE project_id=?
               AND state IN ('queued','waiting','reconciling','manual_review')
               AND json_type(snapshot,'$.purchase_retry') IN ('true','false')""",
            (project_id,),
        ):
            if _purchase_retry_identity(json.loads(row["snapshot"])) == identity:
                raise InstanceActionUnavailable("同项目同规格已有创建申请，请在任务中心检查")
        now = self.now()
        return insert_run(conn, snapshot, now, now, request_id=request_id)

    def submit_purchase_retry(self, preview_id: str, request_id: str, *, read_only: bool = False) -> dict:
        preview_id = text(preview_id, "预览 ID", 100)
        request_id = "action:" + text(request_id, "请求标识", 100)
        with storage.connect(self.path) as conn, storage.transaction(conn):
            existing = conn.execute("SELECT * FROM task_runs WHERE request_id=?", (request_id,)).fetchone()
            if existing is not None:
                run = run_view(existing)
                if run["snapshot"].get("purchase_preview_id") != preview_id:
                    raise InstanceActionUnavailable("请求标识已用于另一项操作")
                return run
            preview = conn.execute(
                "DELETE FROM instance_purchase_previews WHERE id=? AND expires_at>? RETURNING snapshot",
                (preview_id, self.now()),
            ).fetchone()
            if preview is None:
                raise InstanceActionUnavailable("创建预览无效、已使用或已过期，请重新预览")
            snapshot = json.loads(preview["snapshot"]) | {"purchase_preview_id": preview_id}
            return self._insert_purchase_retry(conn, snapshot, request_id, read_only=read_only)

    def submit_purchase_retry_snapshot(self, snapshot: dict, request_id: str, *, read_only: bool = False) -> dict:
        """Submit a snapshot confirmed by the authenticated caller's own preview mechanism."""
        request_id = "action:" + text(request_id, "请求标识", 100)
        with storage.connect(self.path) as conn, storage.transaction(conn):
            existing = conn.execute("SELECT * FROM task_runs WHERE request_id=?", (request_id,)).fetchone()
            if existing is not None:
                run = run_view(existing)
                if run["snapshot"] != snapshot:
                    raise InstanceActionUnavailable("请求标识已用于另一项操作")
                return run
            return self._insert_purchase_retry(conn, snapshot, request_id, read_only=read_only)

    def submit_purchase(
        self, quote_id: str, request_id: str, *, read_only: bool = False,
        channels: list[str] | None = None, email: str | None = None,
    ) -> dict:
        quote_id = text(quote_id, "报价 ID", 100)
        request_id = "action:" + text(request_id, "请求标识", 100)
        email = text(email, "收件地址", 254) if email is not None else None
        selected_channels = channel_list(["site"] if channels is None else channels, {"email": email})
        with storage.connect(self.path) as conn, storage.transaction(conn):
            existing = conn.execute("SELECT * FROM task_runs WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                run = run_view(existing)
                saved = run["snapshot"]
                if (saved.get("instance_quote_id") != quote_id or set(saved["channels"]) != set(selected_channels)
                        or saved["config"].get("email") != email):
                    raise InstanceActionUnavailable("请求标识已用于另一项操作")
                return run
            if read_only or storage.get_settings(conn)["restore_gate"]:
                raise InstanceActionUnavailable("服务处于只读或恢复核对状态，暂不能创建实例")
            now = self.now()
            quote = instance_quotes.get(conn, quote_id)
            current = instance_quotes.view(quote, now)
            if current["state"] != "ready":
                raise InstanceActionUnavailable(current["reason"] or (
                    "此报价已用于另一项创建申请" if current["state"] == "used" else "请等待有效报价"
                ))
            config = json.loads(quote["config"]) | {"max_price": quote["price"]}
            try:
                verify_purchase_spec(conn, quote["project_id"], current["spec"])
                if config.get("cpu_locked"):
                    platform_candidates(conn, current["spec"] | {"cpu_auto": True, "cpu_locked": True})
            except ValueError as error:
                raise InstanceActionUnavailable(str(error)) from error
            if email is not None:
                config["email"] = validate_config("acquire", config | {"email": email})["email"]
            snapshot = {
                "kind": "acquire", "name": "立即创建实例", "project_id": quote["project_id"],
                "channels": selected_channels, "config": config, "instance_quote_id": quote_id,
                "confirmed_quoted_at": quote["quoted_at"], "window_seconds": quote["expires_at"] - now - 1,
            }
            self._validate_purchase_channels(conn, snapshot)
            run = insert_run(conn, snapshot, now, now, request_id=request_id)
            conn.execute("UPDATE instance_quotes SET run_id=? WHERE id=?", (run["id"], quote_id))
            return run

    def submit_action(
        self, body: dict, request_id: str, *, read_only: bool = False, use_snapshot_scope: bool = False,
    ) -> dict:
        kind = text(body.get("kind"), "实例操作")
        if kind not in IMMEDIATE_ACTION_NAMES:
            raise ValueError("实例操作无效")
        now = self.now()
        project_id = text(body.get("project_id"), "项目")
        requested = body.get("config", {})
        request_id = "action:" + text(request_id, "请求标识")
        with storage.connect(self.path) as conn, storage.transaction(conn):
            existing = conn.execute("SELECT * FROM task_runs WHERE request_id=?", (request_id,)).fetchone()
            if existing is None and (read_only or storage.get_settings(conn)["restore_gate"]):
                raise InstanceActionUnavailable("服务处于只读或恢复核对状态，暂不能提交云端操作")
            if use_snapshot_scope:
                if not isinstance(requested, dict):
                    raise ValueError("任务配置无效")
                if existing:
                    scope = json.loads(existing["snapshot"])["config"]
                else:
                    scope = next((
                        row for row in storage.snapshot(conn, "instances", project_id)["data"]
                        if row["id"] == requested.get("instance_id")
                    ), None)
                    if scope is None:
                        raise InstanceActionUnavailable("项目快照中找不到该实例，请刷新实例清单")
                    if kind == "shutdown" and scope.get("charge_type") == "Spot":
                        raise ValueError("抢占式实例不支持定时关机")
                requested = requested | {key: scope.get(key) for key in ("region", "zone")}
            config = validate_config(kind, requested, immediate=True)
            snapshot = {
                "kind": kind, "name": "无卡开机" if config.get("boot_mode") == "without_gpu" else IMMEDIATE_ACTION_NAMES[kind],
                "config": config, "project_id": project_id,
                "channels": channel_list(body.get("channels", ["site"]), config), "window_seconds": 1800,
            }
            if existing:
                run = run_view(existing)
                if run["snapshot"] != snapshot:
                    raise ValueError("请求标识已用于另一项操作")
                return run
            if run_deadline(snapshot, now) < now:
                raise ValueError("距离关机不足 5 分钟")
            cardless.require_snapshot_target(conn, project_id, config)
            if instance_history.released(conn, project_id, config["instance_id"]):
                raise InstanceActionUnavailable("该实例已释放")
            if termination_pending(conn, project_id, config["instance_id"]):
                raise InstanceActionUnavailable("实例存在待核对的释放操作")
            from gpu_console.application import instance_resize

            if instance_resize.pending(conn, project_id, config["instance_id"]):
                raise InstanceActionUnavailable("实例有待处理的改配操作")
            if conn.execute(
                f"""SELECT 1 FROM task_runs r WHERE r.project_id=:project_id AND (
                    (r.state IN ('queued','waiting','reconciling')
                     AND (:kind IN ('reboot','rename') OR json_extract(r.snapshot,'$.kind') IN ('reboot','rename')))
                    OR (r.state IN ('reconciling','manual_review') AND r.phase IN {INSTANCE_RECHECK_SQL})
                    )
                    AND {INSTANCE_RUN_MATCH} LIMIT 1""",
                {"project_id": project_id, "kind": kind, "instance_id": config["instance_id"]},
            ).fetchone():
                raise InstanceActionUnavailable("此实例仍有操作在途，请回读后再提交")
            if kind == "stop":
                conn.execute(
                    f"""UPDATE task_runs AS r SET state='canceled',stopped=1,updated_at=:now,
                           reason='已提交手动关机，停止本次定时开机'
                       WHERE {SCHEDULED_START_TARGET} AND r.phase IS NULL AND r.state IN ('queued','waiting')""",
                    {"project_id": project_id, "now": now, **config},
                )
            return insert_run(conn, snapshot, now, now, request_id=request_id)

    def enqueue_due(self, limit: int = 100) -> int:
        now = self.now()
        queued = 0
        with storage.connect(self.path) as conn, storage.transaction(conn):
            rows = conn.execute(
                """SELECT * FROM tasks WHERE status='active' AND deleted_at IS NULL
                     AND next_at<=?+CASE WHEN json_extract(definition,'$.kind')='shutdown' THEN ? ELSE 0 END
                   ORDER BY next_at,id LIMIT ?""",
                (now, SHUTDOWN_LEAD, limit),
            ).fetchall()
            for row in rows:
                task = task_view(row)
                snapshot = task_snapshot(task)
                due = task["next_at"]
                expired = run_deadline(snapshot, due) < now
                if expired:
                    upsert_history(conn, task, due, now, "后台恢复时已错过执行窗口")
                    next_at = next_time(task["schedule"], due, inclusive=False)
                else:
                    if task["kind"] == "shutdown" and conn.execute(
                        """SELECT 1 FROM task_runs WHERE task_id=? AND scheduled_at<?
                           AND deadline_at>=? AND stopped=0
                           AND state IN ('queued','waiting','reconciling')
                           AND json_extract(snapshot,'$.kind')='shutdown'""",
                        (task["id"], due, now),
                    ).fetchone():
                        continue
                    exists = conn.execute(
                        "SELECT id FROM task_runs WHERE task_id=? AND scheduled_at=? AND request_id IS NULL",
                        (task["id"], due),
                    ).fetchone()
                    if exists is None:
                        run = enqueue_trigger(conn, task, due, now, feishu_configured=self.feishu_configured)
                        if run["state"] != "missed":
                            queued += 1
                    next_at = next_time(task["schedule"], due, inclusive=False)
                conn.execute(
                    "UPDATE tasks SET next_at=?,status=?,updated_at=? WHERE id=?",
                    (next_at, "active" if next_at is not None else "completed", now, task["id"]),
                )
        return queued

    def list_runs(
        self, project_id: str | None = None, task_id: str | None = None, instance_id: str | None = None,
        *, attention: bool = False, automation_rule_id: str | None = None, limit: int = 200, offset: int = 0,
    ) -> list[dict]:
        columns = f", {RUN_BUSINESS_COLUMNS}" if attention else ""
        task_join = "LEFT JOIN tasks t ON t.id=r.task_id AND t.project_id=r.project_id" if attention else ""
        attention_filter = f"AND {RUN_NEEDS_ATTENTION}" if attention else ""
        view = run_with_business if attention else run_view
        with storage.connect(self.path) as conn:
            return [view(row) for row in conn.execute(
                f"""SELECT r.*, w.run_id IS NOT NULL AS warning_dismissed {columns} FROM task_runs r
                    LEFT JOIN task_run_warning_dismissals w ON w.run_id=r.id
                    {task_join}
                    {RUN_LINKED_SHUTDOWN_JOIN if attention else ''}
                    WHERE (:project_id IS NULL OR r.project_id=:project_id)
                    AND (:task_id IS NULL OR {RUN_SOURCE_TASK}=:task_id)
                    AND (:instance_id IS NULL OR {INSTANCE_RUN_MATCH})
                    AND (:automation_rule_id IS NULL OR json_extract(r.snapshot,'$.trigger.automation_rule_id')=:automation_rule_id)
                    {attention_filter}
                    ORDER BY r.created_at DESC,r.id LIMIT :limit OFFSET :offset""",
                {"project_id": project_id, "task_id": task_id, "instance_id": instance_id,
                 "automation_rule_id": automation_rule_id, "limit": limit, "offset": offset},
            )]

    def get_run(self, run_id: str) -> dict:
        with storage.connect(self.path) as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                f"""SELECT r.*, w.run_id IS NOT NULL AS warning_dismissed, linked_shutdown.id AS shutdown_followup_id
                    FROM task_runs r LEFT JOIN task_run_warning_dismissals w ON w.run_id=r.id
                    {RUN_LINKED_SHUTDOWN_JOIN} WHERE r.id=?""",
                (run_id,),
            ).fetchone()
            run = run_view(require_row(row, "运行不存在"))
            capacity_retry = (
                (run["source_task_id"] or run["task_id"]) is not None
                and run["kind"] == "start" and "capacity_retry" in run["snapshot"]["config"]
            )
            purchase_retry = run["snapshot"].get("purchase_retry") is True
            order = "DESC LIMIT 100" if capacity_retry or purchase_retry else "ASC"
            operations = conn.execute(
                f"SELECT * FROM task_operations WHERE run_id=? ORDER BY id {order}", (run_id,),
            ).fetchall()
            if capacity_retry or purchase_retry:
                operations.reverse()
                action, count_key = ("start", "start_attempts") if capacity_retry else ("create", "purchase_attempts")
                run[count_key] = conn.execute(
                    """SELECT count(*) FROM task_operations WHERE run_id=? AND action=?
                       AND state IN ('reserved','accepted','rejected')""", (run_id, action),
                ).fetchone()[0]
            run["operations"] = [dict(row) | {"data": json.loads(row["data"])} for row in operations]
            run["audit"] = [dict(row) | {"data": json.loads(row["data"])} for row in conn.execute(
                "SELECT * FROM task_audit WHERE run_id=? ORDER BY id", (run_id,),
            )]
            return run

    def set_run_warning_dismissed(self, run_id: str, dismissed: bool) -> None:
        """Store the operator's reminder preference separately from the result."""
        with storage.connect(self.path) as conn, storage.transaction(conn):
            row = require_row(conn.execute(
                "SELECT state,json_extract(snapshot,'$.kind') AS kind FROM task_runs WHERE id=?",
                (run_id,),
            ).fetchone(), "运行不存在")
            if row["kind"] != "cancel_shutdown" or row["state"] != "manual_review":
                raise ValueError("只有待人工核对的取消关机预约可以关闭或恢复提醒")
            if dismissed:
                conn.execute(
                    "INSERT INTO task_run_warning_dismissals(run_id) VALUES (?) ON CONFLICT(run_id) DO NOTHING",
                    (run_id,),
                )
            else:
                conn.execute("DELETE FROM task_run_warning_dismissals WHERE run_id=?", (run_id,))

    def queue_shutdown_review(self, run_id: str, keep_stop_at: int | None = None) -> dict:
        """Queue an audited readback of the current schedule in the original scope."""
        with storage.connect(self.path) as conn, storage.transaction(conn):
            row = conn.execute(
                f"""SELECT r.*, linked_shutdown.id AS shutdown_followup_id FROM task_runs r
                    {RUN_LINKED_SHUTDOWN_JOIN} WHERE r.id=?""", (run_id,),
            ).fetchone()
            run = run_view(require_row(row, "运行不存在"))
            if run["kind"] == "cancel_shutdown":
                if keep_stop_at is not None:
                    raise ValueError("取消预约核对不需要确认保留时间")
                if run["state"] == "reviewed" or (
                    run["state"] == "reconciling" and run["phase"] == "review_cancel_shutdown"
                ):
                    return run
                if run["state"] == "manual_review":
                    now = self.now()
                    updated = conn.execute(
                        """UPDATE task_runs SET state='reconciling',phase='review_cancel_shutdown',
                           reason='正在核对云端关机预约',next_check=?,updated_at=? WHERE id=? RETURNING *""",
                        (now, now, run_id),
                    ).fetchone()
                    write_audit(conn, run_id, "review_cancel_shutdown", {
                        "original_reason": run["reason"], "original_phase": run["phase"],
                    }, now)
                    return run_view(updated)
                raise ValueError("只有待人工核对的取消关机预约可以发起核对")

            if run["phase"] == "review_shutdown":
                pending = conn.execute(
                    """SELECT json_extract(data,'$.keep_stop_at') FROM task_audit
                       WHERE run_id=? AND action='review_shutdown_requested' ORDER BY id DESC LIMIT 1""",
                    (run_id,),
                ).fetchone()
                if pending and pending[0] == keep_stop_at:
                    return run
                raise ValueError("此运行已有另一项关机核对在途")
            if run.get("shutdown_followup_id"):
                raise ValueError("请核对关联的关机运行")
            review = current_shutdown_review(run["result"])
            if review.get("resolved") is True and (keep_stop_at is None or (
                review.get("outcome") == "kept" and review.get("cloud_shutdown_at") == keep_stop_at
            )):
                return run
            if shutdown_review_target(run) is None:
                raise ValueError("此运行没有可核对的关机事项")
            now = self.now()
            if keep_stop_at is not None and not (
                review.get("outcome") == "different" and review.get("cloud_shutdown_at") == keep_stop_at > now
            ):
                raise ValueError("请先核对当前云端预约，再确认保留对应时间")
            updated = conn.execute(
                "UPDATE task_runs SET phase='review_shutdown',next_check=?,updated_at=? WHERE id=? RETURNING *",
                (now, now, run_id),
            ).fetchone()
            write_audit(conn, run_id, "review_shutdown_requested", {
                "original_phase": run["phase"], "keep_stop_at": keep_stop_at,
            }, now)
            return run_view(updated)

    def queue_instance_recheck(self, run_id: str) -> dict:
        with storage.connect(self.path) as conn, storage.transaction(conn):
            run = run_view(require_row(conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone(), "运行不存在"))
            phase = run["phase"]
            if phase not in INSTANCE_RECHECK_PHASES | PURCHASE_RECHECK_PHASES:
                raise ValueError("此运行没有待核对的实例操作")
            if run["state"] == "reconciling" and run["result"].get("instance_recheck_pending"):
                return run
            operation = conn.execute(
                "SELECT action,state FROM task_operations WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,),
            ).fetchone()
            actions = {"cfs_create", "cfs_resize"} if phase == "verify_cfs" else (
                {"create"} if phase == "manual_bind" else {phase.removeprefix("verify_")}
            )
            if (run["state"] != "manual_review" or operation is None or operation["action"] not in actions
                    or operation["state"] not in {"accepted", "reserved"}):
                raise ValueError("此运行没有待核对的实例操作")
            now = self.now()
            updated = conn.execute(
                """UPDATE task_runs SET state='reconciling',reason='正在核对实例操作结果',
                   result=json_set(result,'$.instance_recheck_pending',json('true')),next_check=?,updated_at=?
                   WHERE id=? RETURNING *""",
                (now, now, run_id),
            ).fetchone()
            write_audit(conn, run_id, "instance_recheck_requested", {"previous_reason": run["reason"]}, now)
            return run_view(updated)

    def cancel_action(self, run_id: str) -> dict:
        """Stop new attempts for a direct action or a capacity retry and preserve cloud readback."""
        with storage.connect(self.path) as conn, storage.transaction(conn):
            row = require_row(conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone(), "运行不存在")
            run = run_view(row)
            capacity_retry = run["kind"] == "start" and "capacity_retry" in run["snapshot"]["config"]
            if not capacity_retry and not (row["request_id"] or "").startswith("action:"):
                raise ValueError("请从所属任务暂停或取消该运行")
            if row["state"] in ACTIVE_RUNS:
                conn.execute(
                    """UPDATE task_runs SET stopped=1,updated_at=?,
                           state=CASE WHEN phase IS NULL THEN 'canceled' ELSE state END,
                           reason=CASE WHEN phase IS NULL THEN '已停止后续尝试' ELSE reason END
                       WHERE id=?""",
                    (self.now(), run_id),
                )
                row = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            return run_view(row)

    def reconcile_run(
        self, run_id: str, action: str, instance_id: str | None = None, note: str | None = None,
        *, confirmed_no_order: bool = False,
    ) -> dict:
        now = self.now()
        with storage.connect(self.path) as conn, storage.transaction(conn):
            run = run_view(require_row(conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone(), "运行不存在"))
            if run["phase"] not in {"verify_create", "manual_bind"} or run["state"] not in {"manual_review", "reconciling"}:
                raise ValueError("只有结果未知的采购可以人工核对")
            if run["state"] != "manual_review" and not run["result"].get("reviewable"):
                raise ValueError("等待运行完成一次云端回读后再核对")
            if action == "release" and confirmed_no_order is not True:
                raise ValueError("请确认已核对平台订单且未成交")
            if note is not None or action == "release":
                note = text(note, "核对说明", 500)
            if action == "bind":
                instance_id = text(instance_id, "实例 ID")
                operation = conn.execute(
                    """SELECT data FROM task_operations WHERE run_id=? AND action='create'
                       ORDER BY id DESC LIMIT 1""",
                    (run_id,),
                ).fetchone()
                if operation and conflicts_with_create_receipt(json.loads(operation["data"]), instance_id):
                    raise ValueError("实例 ID 与已受理的采购回执或已核实实例不一致")
                result = encode(run["result"] | {"manual_instance_id": instance_id})
                conn.execute(
                    """UPDATE task_runs SET phase='manual_bind',state='reconciling',next_check=?,updated_at=?,
                         reason='正在回读手工指定的实例',result=? WHERE id=?""",
                    (now, now, result, run_id),
                )
            elif action == "release":
                confirmed = conn.execute(
                    "SELECT 1 FROM task_operations WHERE run_id=? AND action='create' AND state='accepted'",
                    (run_id,),
                ).fetchone()
                if confirmed:
                    raise ValueError("已确认成交的申请无法释放预留")
                conn.execute(
                    "UPDATE task_operations SET state='released',updated_at=? WHERE run_id=? AND state='reserved'",
                    (now, run_id),
                )
                conn.execute(
                    """UPDATE task_runs SET phase=NULL,state='failed',reason='已按订单记录确认未成交并释放预留',
                         updated_at=?,result=json_set(result,'$.purchase_review',json(?)) WHERE id=?""",
                    (now, encode({"outcome": "not_purchased", "checked_at": now}), run_id),
                )
            else:
                raise ValueError("人工核对操作无效")
            audit = {"instance_id": instance_id, "note": note}
            if action == "release":
                audit["confirmed_no_order"] = confirmed_no_order
            write_audit(conn, run_id, action, audit, now)
            return run_view(conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone())


def enqueue_owner_cancel(conn: sqlite3.Connection, source: dict, owner: dict, now: int) -> None:
    snapshot = dict(source)
    snapshot.update(
        kind="cancel_shutdown", name="取消关机预约", window_seconds=1800,
        config={
            "instance_id": owner["instance_id"], "region": owner["region"], "zone": owner["zone"],
            "owner_run_id": owner["run_id"], "expected_stop_at": owner["stop_at"],
            **({"email": source["config"]["email"]} if source["config"].get("email") else {}),
        },
    )
    request_id = "cancel-owner:" + owner["run_id"]
    if not conn.execute("SELECT id FROM task_runs WHERE request_id=?", (request_id,)).fetchone():
        insert_run(conn, snapshot, now, now, request_id=request_id)
