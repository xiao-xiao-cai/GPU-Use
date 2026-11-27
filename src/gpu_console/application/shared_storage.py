"""Persist CFS purchase intents with the run that may order the GPU."""

import json
from sqlite3 import Connection

from gpu_console.application.tasks import encode


PAID_MODES = frozenset({"cfs_create", "cfs_resize"})
PENDING_STATES = frozenset({"reserved", "accepted"})
VERIFY_PHASE = "verify_cfs"


def fingerprint(scope: dict) -> str:
    return encode({key: scope[key] for key in ("region", "zone")})


def operation(conn: Connection, run_id: str) -> dict | None:
    row = conn.execute(
        """SELECT * FROM task_operations WHERE run_id=? AND action IN ('cfs_create','cfs_resize')
           ORDER BY id DESC LIMIT 1""",
        (run_id,),
    ).fetchone()
    return dict(row) | {"data": json.loads(row["data"])} if row else None


def blocked(conn: Connection, run_id: str, scope: dict) -> bool:
    return conn.execute(
        """SELECT 1 FROM task_operations WHERE run_id!=? AND fingerprint=?
           AND action IN ('cfs_create','cfs_resize') AND state IN ('reserved','accepted') LIMIT 1""",
        (run_id, fingerprint(scope)),
    ).fetchone() is not None


def reserve(conn: Connection, run: dict, data: dict, now: int) -> int:
    cursor = conn.execute(
        """INSERT INTO task_operations(run_id,project_id,action,state,fingerprint,data,created_at,updated_at)
           VALUES (?,?,?,'reserved',?,?,?,?)""",
        (run["id"], run["project_id"], data["intent"]["mode"], fingerprint(data["scope"]), encode(data), now, now),
    )
    return cursor.lastrowid


def confirm(conn: Connection, operation: dict, identity: str, now: int) -> None:
    conn.execute(
        """UPDATE task_operations SET state='verified',data=?,updated_at=?
           WHERE id=? AND state IN ('reserved','accepted')""",
        (encode(operation["data"] | {"cfs_id": identity}), now, operation["id"]),
    )
