"""Queue read-only catalog price lookups by project, exact specification and billing mode."""

from sqlite3 import Connection
from typing import Literal

from gpu_console import storage
from gpu_console.storage import catalog

ChargeType = Literal["Postpay", "Day", "Month", "Spot"]
UNITS = {"Postpay": "hour", "Day": "day", "Month": "month", "Spot": "hour"}
REQUEST_TIMEOUT = 120
REFRESH_SECONDS = 7 * 86400
INITIAL_RETRY_SECONDS = 1800
RETENTION_SECONDS = 30 * 86400


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS inventory_prices (
            project_id TEXT NOT NULL, spec_id TEXT NOT NULL, charge_type TEXT NOT NULL,
            accessed_at INTEGER NOT NULL, next_at INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('queued','ready','failed')),
            amount TEXT, source TEXT, observed_at INTEGER, error TEXT,
            PRIMARY KEY(project_id,spec_id,charge_type)
        );
        CREATE INDEX IF NOT EXISTS inventory_prices_due ON inventory_prices(next_at);
        CREATE TABLE IF NOT EXISTS inventory_price_targets (
            project_id TEXT NOT NULL, spec_id TEXT NOT NULL, PRIMARY KEY(project_id,spec_id)
        );
    """)


def resolve(conn: Connection, project_id: str, spec_id: str, charge_type: ChargeType) -> dict:
    if not catalog.project_known(conn, project_id):
        raise KeyError("找不到该项目，请先同步项目")
    spec = catalog.compute_specs(storage.snapshot(conn, "catalog")["data"]).get(spec_id)
    if spec is None:
        raise KeyError("平台目录中找不到这项规格")
    if spec["instance_type"] != catalog.instance_type(charge_type):
        raise ValueError("计费方式与所选资源池不匹配")
    return spec | {"project_id": project_id, "charge_type": charge_type}


def result(conn: Connection, project_id: str, spec_id: str, charge_type: ChargeType, now: int) -> dict:
    row = conn.execute(
        "SELECT * FROM inventory_prices WHERE project_id=? AND spec_id=? AND charge_type=?",
        (project_id, spec_id, charge_type),
    ).fetchone()
    if row is None:
        raise KeyError("尚未提交该规格的询价")
    fields = {key: row[key] for key in ("state", "amount", "source", "observed_at", "error")}
    if row["state"] == "queued" and now >= row["next_at"] + REQUEST_TIMEOUT:
        fields.update(state="failed", error="仍在等待后台询价，请稍后查看")
    return fields | {
        "currency": "CNY", "billing_unit": UNITS[charge_type],
        "stale": row["state"] == "ready" and (row["error"] is not None or now >= row["next_at"]),
    }


def enqueue(conn: Connection, project_id: str, spec_id: str, charge_type: ChargeType, now: int) -> dict:
    resolve(conn, project_id, spec_id, charge_type)
    conn.execute(
        """INSERT INTO inventory_prices VALUES (?,?,?,?,?,'queued',NULL,NULL,NULL,NULL)
           ON CONFLICT(project_id,spec_id,charge_type) DO UPDATE SET accessed_at=excluded.accessed_at""",
        (project_id, spec_id, charge_type, now, now),
    )
    return result(conn, project_id, spec_id, charge_type, now)


def sync_targets(conn: Connection, now: int) -> None:
    """Track every ordinary hourly specification from successful catalog snapshots."""
    projects = storage.snapshot(conn, "projects")
    listings = storage.snapshot(conn, "catalog")
    if any(snapshot["success_at"] is None or snapshot["error"] for snapshot in (projects, listings)):
        return
    ordinary = [key for key, spec in catalog.compute_specs(listings["data"]).items() if spec["instance_type"] == "uhost"]
    desired = {(project["id"], key) for project in projects["data"] for key in ordinary}
    tracked = set(map(tuple, conn.execute("SELECT project_id,spec_id FROM inventory_price_targets")))
    conn.executemany("DELETE FROM inventory_price_targets WHERE project_id=? AND spec_id=?", sorted(tracked - desired))
    conn.executemany("INSERT INTO inventory_price_targets VALUES (?,?)", sorted(desired - tracked))
    conn.execute(
        """INSERT INTO inventory_prices
           SELECT project_id,spec_id,'Postpay',0,?,'queued',NULL,NULL,NULL,NULL FROM inventory_price_targets AS t
           WHERE NOT EXISTS (
               SELECT 1 FROM inventory_prices AS p WHERE p.project_id=t.project_id
               AND p.spec_id=t.spec_id AND p.charge_type='Postpay'
           )""", (now,),
    )


def trim(conn: Connection, now: int) -> None:
    conn.execute(
        """DELETE FROM inventory_prices AS p WHERE accessed_at<? AND NOT EXISTS (
               SELECT 1 FROM inventory_price_targets t WHERE t.project_id=p.project_id AND t.spec_id=p.spec_id
               AND p.charge_type='Postpay'
           )""", (now - RETENTION_SECONDS,),
    )
