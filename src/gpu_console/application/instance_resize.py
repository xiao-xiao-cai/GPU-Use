"""Issue short-lived resize quotes and submit one verified change to the task runner."""

import json
from sqlite3 import Connection
from uuid import uuid4

from gpu_console import storage
from gpu_console.application import automation_rules
from gpu_console.application.tasks import INSTANCE_RUN_MATCH, insert_run, run_view
from gpu_console.storage import catalog, instance_assets, instance_history

TTL = 300
COMPUTE = ("gpu", "cpu", "memory_gb")
PENDING = "(r.state IN ('queued','waiting','reconciling') OR (r.state='manual_review' AND r.phase IS NOT NULL))"


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS instance_resize_quotes (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, instance_id TEXT NOT NULL, spec_id TEXT NOT NULL,
            expected TEXT NOT NULL, target TEXT NOT NULL, created_at INTEGER NOT NULL,
            state TEXT NOT NULL, amount TEXT, available INTEGER, checked_at INTEGER, error TEXT, run_id TEXT
        );
        CREATE INDEX IF NOT EXISTS instance_resize_quotes_pending ON instance_resize_quotes(state,created_at);
    """)


def baseline(instance: dict) -> dict:
    spec = instance_assets.probe_spec(instance)
    if not spec:
        raise ValueError("实例规格或磁盘配置待同步，请先刷新")
    return spec | {"id": instance["id"], "create_time": instance.get("create_time"),
                   "instance_type": instance.get("instance_type"),
                   "disk_ids": sorted(str(disk.get("id") or "") for disk in instance["disks"])}


def pending(conn: Connection, project_id: str, instance_id: str) -> bool:
    return conn.execute(
        f"""SELECT 1 FROM task_runs r WHERE r.project_id=:project_id AND {PENDING}
              AND json_extract(r.snapshot,'$.kind')='resize' AND {INSTANCE_RUN_MATCH} LIMIT 1""",
        {"project_id": project_id, "instance_id": instance_id},
    ).fetchone() is not None


def _target(conn: Connection, project_id: str, instance_id: str, now: int) -> dict:
    if instance_history.released(conn, project_id, instance_id):
        raise ValueError("该实例已释放")
    snapshot = storage.snapshot(conn, "instances", project_id)
    if not automation_rules.fresh_instance_snapshot(conn, snapshot, now):
        raise ValueError("实例清单待同步，请先刷新")
    instance = next((row for row in snapshot["data"] if row["id"] == instance_id
                     and row.get("project_id", project_id) == project_id), None)
    if instance is None:
        raise KeyError("项目快照中找不到该实例")
    if instance["state"] != "Stopped":
        raise ValueError("关机后才可更改规格")
    return instance | {"project_id": project_id}


def options(conn: Connection, project_id: str, instance_id: str, now: int) -> dict:
    instance = _target(conn, project_id, instance_id, now)
    expected = baseline(instance)
    current = {key: expected[key] for key in COMPUTE}
    kind = catalog.instance_type(expected["charge_type"])
    result = []
    for spec in catalog.compute_specs(storage.snapshot(conn, "catalog")["data"]).values():
        if (spec["region"], spec["zone"], spec["gpu_model"], spec["instance_type"]) != (
            expected["region"], expected["zone"], expected["gpu_model"], kind,
        ):
            continue
        compute = {key: spec[key] for key in COMPUTE}
        if compute != current:
            result.append(compute | {"id": spec["catalog_spec_id"]})
    return {"current": current, "options": sorted(result, key=lambda spec: tuple(spec[key] for key in COMPUTE))}


def quote_view(row, now: int) -> dict:
    expires = (row["checked_at"] or row["created_at"]) + TTL
    return {"id": row["id"], "target": json.loads(row["target"]), "amount": row["amount"],
            "state": "expired" if now >= expires and row["run_id"] is None else row["state"],
            "available": bool(row["available"]) if row["available"] is not None else None,
            "checked_at": row["checked_at"], "expires_at": expires, "error": row["error"], "run_id": row["run_id"]}


def get(conn: Connection, project_id: str, instance_id: str, quote_id: str):
    row = conn.execute("""SELECT * FROM instance_resize_quotes WHERE id=? AND project_id=? AND instance_id=?""",
                       (quote_id, project_id, instance_id)).fetchone()
    if row is None:
        raise KeyError("改配报价不存在")
    return row


def create_quote(conn: Connection, project_id: str, instance_id: str, spec_id: str, now: int) -> dict:
    scope = {"project_id": project_id, "instance_id": instance_id}
    expected = baseline(_target(conn, **scope, now=now))
    match = next((spec for spec in options(conn, **scope, now=now)["options"] if spec["id"] == spec_id), None)
    if match is None:
        raise ValueError("该规格不在可选目录中，请刷新后重新选择")
    target = {key: match[key] for key in COMPUTE}
    for row in conn.execute("""
        SELECT * FROM instance_resize_quotes WHERE project_id=? AND instance_id=? AND spec_id=?
          AND run_id IS NULL AND state='queued' ORDER BY created_at DESC
    """, (project_id, instance_id, spec_id)):
        view = quote_view(row, now)
        if view["state"] != "expired" and json.loads(row["expected"]) == expected:
            return view
    quote_id = uuid4().hex
    conn.execute("""INSERT INTO instance_resize_quotes(id,project_id,instance_id,spec_id,expected,target,created_at,state)
                    VALUES (?,?,?,?,?,?,?,'queued')""",
                 (quote_id, project_id, instance_id, spec_id, json.dumps(expected), json.dumps(target), now))
    return quote_view(get(conn, **scope, quote_id=quote_id), now)


def submit(conn: Connection, project_id: str, instance_id: str, quote_id: str, request_id: str,
           now: int, *, read_only: bool) -> dict:
    request_id = "resize:" + request_id
    previous = conn.execute("SELECT * FROM task_runs WHERE request_id=?", (request_id,)).fetchone()
    if previous:
        run = run_view(previous)
        config = run["snapshot"]["config"]
        if run["project_id"] != project_id or config.get("instance_id") != instance_id or config.get("quote_id") != quote_id:
            raise ValueError("请求标识已用于另一项改配")
        return run
    if read_only or storage.get_settings(conn)["restore_gate"]:
        raise ValueError("服务处于只读或恢复核对状态，暂不能改配")
    row = get(conn, project_id, instance_id, quote_id)
    if row["run_id"]:
        return run_view(conn.execute("SELECT * FROM task_runs WHERE id=?", (row["run_id"],)).fetchone())
    quote = quote_view(row, now)
    if quote["state"] != "ready" or quote["available"] is not True:
        raise ValueError("报价已过期或目标规格库存未确认，请重新询价")
    expected = json.loads(row["expected"])
    if baseline(_target(conn, project_id, instance_id, now)) != expected:
        raise ValueError("实例配置已变化，请重新询价")
    if conn.execute(
        f"SELECT 1 FROM task_runs r WHERE r.project_id=:project_id AND {PENDING} AND {INSTANCE_RUN_MATCH} LIMIT 1",
        {"project_id": project_id, "instance_id": instance_id},
    ).fetchone():
        raise ValueError("此实例有操作在途，请先查看运行记录")
    snapshot = {
        "kind": "resize", "name": "更改实例规格", "project_id": project_id, "channels": ["site"],
        "window_seconds": quote["expires_at"] - now - 1,
        "config": {"instance_id": instance_id, "quote_id": quote_id, "expected": expected,
                   "region": expected["region"], "zone": expected["zone"],
                   "target": quote["target"], "amount": quote["amount"]},
    }
    run = insert_run(conn, snapshot, now, now, request_id=request_id)
    conn.execute("UPDATE instance_resize_quotes SET run_id=? WHERE id=?", (run["id"], quote_id))
    return run
