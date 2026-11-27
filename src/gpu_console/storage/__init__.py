"""SQLite connections and shared persistence used by the web and worker."""

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_shared_connection: ContextVar[tuple[Path, sqlite3.Connection] | None] = ContextVar(
    "shared_sqlite_connection", default=None,
)

DEFAULTS = {
    "collection_seconds": 60,
    "catalog_seconds": 300,
    "stock_seconds": 60,
    "task_stock_seconds": 15,
    "retention_days": 30,
    "idle_minutes": 30,
    "idle_threshold": 10,
    "idle_warmup_minutes": 10,
    "idle_channels": ["site"],
    "idle_email_recipient": None,
    "idle_schedule": {"mode": "all_day", "windows": []},
    "notification_repeat_minutes": {"site": 0, "feishu": 0, "email": 0},
    "notification_rules": {
        "idle_recovery": {"enabled": False, "channels": ["site"]},
        "gpu_memory_high": {"enabled": False, "channels": ["site"], "threshold": 90, "duration_minutes": 10},
        "monitoring": {"enabled": False, "channels": ["site"]},
        "watch_stock": {"enabled": False, "channels": ["site"]},
        "email_recipient": None,
    },
    "project_id": None,
    "restore_gate": False,
}

SCHEMA = """
    CREATE TABLE IF NOT EXISTS administrators (
        id INTEGER PRIMARY KEY CHECK (id=1), username TEXT NOT NULL, password_hash TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY, csrf_token TEXT NOT NULL, expires_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS login_attempts (
        client TEXT PRIMARY KEY, window_started INTEGER NOT NULL, attempts INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings (
        name TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS system_status (
        component TEXT PRIMARY KEY, success_at INTEGER, error TEXT
    );
    CREATE TABLE IF NOT EXISTS snapshots (
        kind TEXT NOT NULL, scope TEXT NOT NULL, data TEXT NOT NULL DEFAULT '[]',
        success_at INTEGER, error TEXT, PRIMARY KEY(kind, scope)
    );
    CREATE TABLE IF NOT EXISTS instance_jupyter_overrides (
        project_id TEXT NOT NULL, instance_id TEXT NOT NULL, url TEXT NOT NULL,
        PRIMARY KEY(project_id, instance_id)
    );
    CREATE TABLE IF NOT EXISTS metrics (
        project_id TEXT NOT NULL, instance_id TEXT NOT NULL, metric TEXT NOT NULL,
        label TEXT NOT NULL, sampled_at INTEGER NOT NULL, value REAL NOT NULL,
        PRIMARY KEY(project_id, instance_id, metric, label, sampled_at)
    );
    CREATE INDEX IF NOT EXISTS metrics_retention ON metrics(sampled_at);
    CREATE TABLE IF NOT EXISTS watches (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, spec TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS stock_history (
        watch_id TEXT NOT NULL, sampled_at INTEGER NOT NULL, available INTEGER,
        error TEXT, PRIMARY KEY(watch_id, sampled_at)
    );
    CREATE INDEX IF NOT EXISTS stock_retention ON stock_history(sampled_at);
    CREATE TABLE IF NOT EXISTS refresh_requests (
        kind TEXT NOT NULL, scope TEXT NOT NULL, requested_at INTEGER NOT NULL, PRIMARY KEY(kind, scope)
    );
    CREATE TABLE IF NOT EXISTS templates (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
        config TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, channel TEXT NOT NULL, payload TEXT NOT NULL,
        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at INTEGER NOT NULL,
        next_at INTEGER NOT NULL, delivered_at INTEGER, read_at INTEGER, UNIQUE(event_id, channel)
    );
    CREATE INDEX IF NOT EXISTS notifications_pending ON notifications(state, next_at);
    CREATE TABLE IF NOT EXISTS recovery_purchases (
        task_id TEXT PRIMARY KEY, adjustment INTEGER NOT NULL CHECK(adjustment>=0)
    );
    CREATE TABLE IF NOT EXISTS recovery_log (
        id INTEGER PRIMARY KEY, backup_at INTEGER NOT NULL, restored_at INTEGER NOT NULL,
        reviewed_at INTEGER, note TEXT
    );
"""


def now() -> int:
    return int(time.time())


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    shared = _shared_connection.get()
    if shared is not None and path.resolve() == shared[0]:
        yield shared[1]
        return
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = True) -> Iterator[None]:
    shared = _shared_connection.get()
    if shared is not None and conn is shared[1]:
        conn.execute("SAVEPOINT console_nested_write")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK TO console_nested_write")
            raise
        finally:
            conn.execute("RELEASE console_nested_write")
        return
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN DEFERRED")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def shared_write(path: Path) -> Iterator[sqlite3.Connection]:
    """Share one transaction with application calls made on behalf of an MCP write."""
    with connect(path) as conn, transaction(conn):
        context = _shared_connection.set((path.resolve(), conn))
        try:
            yield conn
        finally:
            _shared_connection.reset(context)


def initialize(path: Path) -> None:
    from gpu_console.application.automation_rules import initialize as initialize_automation
    from gpu_console.application.instance_quotes import initialize as initialize_instance_quotes
    from gpu_console.application.instance_resize import initialize as initialize_resize
    from gpu_console.application.inventory_prices import initialize as initialize_inventory_prices
    from gpu_console.application.purchase_options import initialize as initialize_purchase_options
    from gpu_console.application.tasks import initialize as initialize_tasks
    from gpu_console.application.watches import sync_bindings
    from gpu_console.mcp.auth import initialize as initialize_mcp_auth
    from gpu_console.mcp.common import initialize as initialize_mcp_tools
    from gpu_console.storage.availability import initialize as initialize_availability
    from gpu_console.storage.availability_rollups import initialize as initialize_availability_rollups
    from gpu_console.storage.billing import initialize as initialize_billing
    from gpu_console.storage.cardless import initialize as initialize_cardless
    from gpu_console.storage.capacity import initialize as initialize_capacity
    from gpu_console.storage.catalog import initialize as initialize_catalog
    from gpu_console.storage.dashboard_billed_cost_rollups import initialize as initialize_billed_cost_rollups
    from gpu_console.storage.dashboard_cost import initialize as initialize_dashboard_cost
    from gpu_console.storage.dashboard_cost_rollups import initialize as initialize_dashboard_cost_rollups
    from gpu_console.storage.dashboard_usage import initialize as initialize_dashboard_usage
    from gpu_console.storage.instance_assets import initialize as initialize_assets
    from gpu_console.storage.instance_history import initialize as initialize_history
    from gpu_console.storage.refresh import initialize as initialize_refresh
    from gpu_console.storage.storage_cost import initialize as initialize_storage_cost
    from gpu_console.worker.collection import initialize as initialize_collection

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with connect(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        initialize_tasks(conn)
        initialize_history(conn)
        initialize_assets(conn)
        initialize_resize(conn)
        initialize_automation(conn)
        initialize_instance_quotes(conn)
        initialize_inventory_prices(conn)
        initialize_purchase_options(conn)
        initialize_mcp_auth(conn)
        initialize_mcp_tools(conn)
        initialize_refresh(conn)
        initialize_catalog(conn)
        initialize_collection(conn)
        initialize_availability(conn)
        initialize_capacity(conn)
        initialize_availability_rollups(conn)
        with transaction(conn):
            sync_bindings(conn)
        initialize_dashboard_usage(conn)
        initialize_cardless(conn)
        initialize_dashboard_cost(conn)
        initialize_dashboard_cost_rollups(conn)
        initialize_storage_cost(conn)
        initialize_billing(conn)
        initialize_billed_cost_rollups(conn)
        conn.execute("INSERT OR IGNORE INTO settings(name,value) VALUES ('feishu_policy_since',?)", (str(now()),))


def get_settings(conn: sqlite3.Connection) -> dict:
    from gpu_console.application.feishu_notices import DEFAULT_POLICY

    return DEFAULTS | {"feishu_event_policy": DEFAULT_POLICY} | {
        row["name"]: json.loads(row["value"])
        for row in conn.execute("SELECT name,value FROM settings")
    }


def set_settings(conn: sqlite3.Connection, values: dict) -> None:
    conn.executemany(
        "INSERT INTO settings(name,value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
        [(name, json.dumps(value)) for name, value in values.items()],
    )


def record_status(
    conn: sqlite3.Connection, component: str, success: bool, error: str | None = None
) -> None:
    conn.execute(
        """INSERT INTO system_status(component,success_at,error) VALUES (?,?,?)
           ON CONFLICT(component) DO UPDATE SET
               success_at=COALESCE(excluded.success_at,system_status.success_at), error=excluded.error""",
        (component, now() if success else None, error),
    )


def emit(conn: sqlite3.Connection, event_id: str, payload: dict, channels: list[str]) -> None:
    timestamp = now()
    conn.executemany(
        """INSERT INTO notifications(event_id,channel,payload,state,created_at,next_at,delivered_at)
           VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_id,channel) DO NOTHING""",
        [
            (event_id, channel, json.dumps(payload, ensure_ascii=False),
             "delivered" if channel == "site" else "pending", timestamp, timestamp,
             timestamp if channel == "site" else None)
            for channel in dict.fromkeys(channels)
        ],
    )


def snapshot(conn: sqlite3.Connection, kind: str, scope: str = "") -> dict:
    row = conn.execute(
        "SELECT data,success_at,error FROM snapshots WHERE kind=? AND scope=?", (kind, scope)
    ).fetchone()
    if row is None:
        return {"data": [], "success_at": None, "error": None}
    return {"data": json.loads(row["data"]), "success_at": row["success_at"], "error": row["error"]}
