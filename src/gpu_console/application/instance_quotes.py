"""Store the exact single-instance specification and price shown for confirmation."""

import json
from sqlite3 import Connection, Row
from uuid import uuid4

from gpu_console.application.purchase_options import normalize_purchase_spec
from gpu_console.application.task_config import SPEC_FIELDS, text

QUOTE_SECONDS = 300


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS instance_quotes (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, config TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('queued','ready','failed','expired')),
            created_at INTEGER NOT NULL, quoted_at INTEGER, expires_at INTEGER,
            price TEXT, reason TEXT, run_id TEXT UNIQUE REFERENCES task_runs(id)
        );
        CREATE INDEX IF NOT EXISTS instance_quotes_pending ON instance_quotes(created_at, id) WHERE state='queued';
    """)


def create(conn: Connection, project_id: str, spec: dict, now: int) -> dict:
    project_id = text(project_id, "项目 ID", 100)
    config = normalize_purchase_spec(conn, project_id, spec, paid=False)
    del config["max_price"]
    quote_id = uuid4().hex
    conn.execute(
        "INSERT INTO instance_quotes(id,project_id,config,state,created_at) VALUES (?,?,?,'queued',?)",
        (quote_id, project_id, json.dumps(config, ensure_ascii=False), now),
    )
    return view(get(conn, quote_id), now)


def get(conn: Connection, quote_id: str) -> Row:
    row = conn.execute("SELECT * FROM instance_quotes WHERE id=?", (quote_id,)).fetchone()
    if row is None:
        raise KeyError("报价不存在，请重新询价")
    return row


def view(row: Row, now: int) -> dict:
    state, reason = row["state"], row["reason"]
    if row["run_id"]:
        state = "used"
    elif state == "queued" and now >= row["created_at"] + QUOTE_SECONDS:
        state, reason = "expired", "询价已超时，请重新获取报价"
    elif state == "ready" and now >= row["expires_at"]:
        state, reason = "expired", "报价已过期，请重新获取报价"
    config = json.loads(row["config"])
    location = config["zones"][0]
    spec = {key: config[key] for key in (*SPEC_FIELDS, "quantity", "disks") if key in config}
    spec.update({key: location[key] for key in ("region", "zone", "shared_storage") if key in location})
    return {key: row[key] for key in ("id", "project_id", "price", "quoted_at", "expires_at", "run_id")} | {
        "state": state, "reason": reason, "spec": spec,
    }


def invalidate_unconsumed(conn: Connection) -> None:
    conn.execute(
        """UPDATE instance_quotes SET state='expired',reason='恢复旧备份后请重新询价'
           WHERE run_id IS NULL AND state IN ('queued','ready')""",
    )
