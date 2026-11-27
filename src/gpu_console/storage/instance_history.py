"""Durable, project-scoped records of instances that left the active inventory."""

import json
from sqlite3 import Connection

FIELDS = (
    "id", "project_id", "name", "region", "zone", "instance_type", "gpu_model", "gpu", "cpu", "memory_gb",
    "cpu_platform", "charge_type", "image_name", "image_id", "create_time", "start_time", "stop_time",
    "total_disk_space_gb", "instance_price_per_hour", "disk_price_per_hour",
)
TERMINAL = {"Deleted", "Terminated", "Released"}


def initialize(conn: Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS instance_history (
            project_id TEXT NOT NULL, instance_id TEXT NOT NULL, snapshot TEXT NOT NULL,
            state TEXT NOT NULL, observed_at INTEGER NOT NULL, run_id TEXT,
            PRIMARY KEY(project_id,instance_id)
        )
    """)
    for row in conn.execute("""
        SELECT id,project_id,snapshot,result,updated_at FROM task_runs
        WHERE state='succeeded' AND json_extract(snapshot,'$.kind')='terminate'
          AND json_extract(result,'$.disposition')='released'
          AND NOT EXISTS (
              SELECT 1 FROM instance_history h WHERE h.project_id=task_runs.project_id
                AND h.instance_id=json_extract(task_runs.snapshot,'$.config.instance_id')
                AND h.state='released'
          )
    """).fetchall():
        config = json.loads(row["snapshot"])["config"]
        record = config.get("expected_instance", {}) | {"id": config["instance_id"], "project_id": row["project_id"]}
        result = json.loads(row["result"])
        if result.get("gpu_model"):
            record["gpu_model"] = result["gpu_model"]
        save(conn, record, "released", result.get("instance_review", {}).get("checked_at") or row["updated_at"], row["id"])


def projection(instance: dict) -> dict:
    record = {field: instance[field] for field in FIELDS if field in instance and instance[field] is not None}
    if isinstance(instance.get("disks"), list):
        record["disks"] = [
            {field: disk[field] for field in ("id", "name", "purpose", "type", "size_gb") if field in disk}
            for disk in instance["disks"]
        ]
    return record


def save(conn: Connection, instance: dict, state: str, observed_at: int, run_id: str | None = None) -> None:
    record = projection(instance)
    conn.execute("""
        INSERT INTO instance_history VALUES (?,?,?,?,?,?)
        ON CONFLICT(project_id,instance_id) DO UPDATE SET
            snapshot=json_patch(instance_history.snapshot,excluded.snapshot), state=excluded.state,
            observed_at=excluded.observed_at, run_id=COALESCE(excluded.run_id,instance_history.run_id)
        WHERE instance_history.state='missing' OR
            (excluded.state='released' AND excluded.run_id IS NOT NULL AND instance_history.run_id IS NULL)
    """, (record["project_id"], record["id"], json.dumps(record, ensure_ascii=False), state, observed_at, run_id))


def observe(conn: Connection, project_id: str, previous: list[dict], rows: list[dict], now: int) -> None:
    active = {row["id"]: row for row in rows if row.get("project_id", project_id) == project_id}
    prior_by_id = {row["id"]: row for row in previous if row.get("project_id", project_id) == project_id}
    for item in active.values():
        if item.get("state") in TERMINAL:
            save(conn, prior_by_id.get(item["id"], {}) | projection(item) | {"project_id": project_id}, "released", now)
        else:
            conn.execute("DELETE FROM instance_history WHERE project_id=? AND instance_id=? AND state='missing'",
                         (project_id, item["id"]))
    for item in previous:
        if item["id"] not in active and item.get("project_id", project_id) == project_id:
            save(conn, item | {"project_id": project_id}, "missing", now)


def _view(row) -> dict:
    state = row["state"]
    return json.loads(row["snapshot"]) | {
        "state": "Released" if state == "released" else "Missing",
        "archive_status": state, "archived_at": row["observed_at"], "release_run_id": row["run_id"],
    }


def list_project(conn: Connection, project_id: str) -> list[dict]:
    return [_view(row) for row in conn.execute(
        "SELECT * FROM instance_history WHERE project_id=? ORDER BY observed_at DESC,instance_id", (project_id,),
    )]


def get(conn: Connection, project_id: str, instance_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM instance_history WHERE project_id=? AND instance_id=?",
                       (project_id, instance_id)).fetchone()
    return _view(row) if row else None


def released(conn: Connection, project_id: str, instance_id: str) -> bool:
    return conn.execute("SELECT 1 FROM instance_history WHERE project_id=? AND instance_id=? AND state='released'",
                        (project_id, instance_id)).fetchone() is not None
