"""Materialize actual-bill GPU metrics for completed Beijing days."""

import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from sqlite3 import Connection

from gpu_console import storage
from gpu_console.storage import billing, dashboard_billed_cost as billed, dashboard_cost
from gpu_console.storage.dashboard_cost_rollups import BATCH_SIZE, DAY_SECONDS, _cutoff
from gpu_console.storage.dashboard_usage import _local_date, _midnight


def initialize(conn: Connection) -> None:
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS dashboard_billed_cost_days (
            project_id TEXT NOT NULL, day INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
            computed_revision INTEGER, computed_at INTEGER, PRIMARY KEY(project_id,day)
        );
        CREATE TABLE IF NOT EXISTS dashboard_billed_cost_daily (
            project_id TEXT NOT NULL, day INTEGER NOT NULL, instance_id TEXT NOT NULL, totals TEXT NOT NULL,
            PRIMARY KEY(project_id,day,instance_id),
            FOREIGN KEY(project_id,day) REFERENCES dashboard_billed_cost_days(project_id,day) ON DELETE CASCADE
        );
        CREATE TRIGGER IF NOT EXISTS dashboard_billed_cost_dirty_on_interval
        AFTER INSERT ON dashboard_cost_intervals BEGIN
            UPDATE dashboard_billed_cost_days SET revision=revision+1
            WHERE project_id=NEW.project_id
              AND day>NEW.start-{dashboard_cost.SUSTAINED_SECONDS}-{DAY_SECONDS}
              AND day<NEW.end+{dashboard_cost.SUSTAINED_SECONDS};
        END;
    """)
    # Database triggers keep the cache safe when an older release replaces an entire billing cycle.
    for event, row in (("INSERT", "NEW"), ("DELETE", "OLD")):
        conn.executescript(f"""
            CREATE TRIGGER IF NOT EXISTS dashboard_billed_cost_dirty_on_order_{event.lower()}
            AFTER {event} ON billing_orders WHEN {billing.compute_cash_filter(row + '.')}
            BEGIN
                UPDATE dashboard_billed_cost_days SET revision=revision+1
                WHERE project_id={row}.project_id AND day>{row}.start_at-{DAY_SECONDS} AND day<{row}.end_at;
            END;
        """)


def _seed(conn: Connection, since: int, today: int) -> None:
    projects = storage.snapshot(conn, "projects")["data"]
    counts = dict(conn.execute(
        "SELECT project_id,COUNT(*) FROM dashboard_billed_cost_days WHERE day>=? AND day<? GROUP BY project_id",
        (since, today),
    ))
    missing = [row["id"] for row in projects if counts.get(row["id"], 0) != (today - since) // DAY_SECONDS]
    if missing:
        with storage.transaction(conn):
            conn.executemany(
                "INSERT OR IGNORE INTO dashboard_billed_cost_days(project_id,day) VALUES (?,?)",
                ((project, day) for project in missing for day in range(since, today, DAY_SECONDS)),
            )


def refresh(path: Path, now: int, *, limit: int = BATCH_SIZE) -> int:
    """Compute on a read snapshot and publish only if the input revision still matches."""
    since, today = _cutoff(now), _midnight(_local_date(now))
    updated = 0
    with storage.connect(path) as conn:
        _seed(conn, since, today)
        jobs = conn.execute("""
            SELECT project_id,day,revision FROM dashboard_billed_cost_days
            WHERE day>=? AND day<? AND computed_revision IS NOT revision
            ORDER BY day DESC,project_id LIMIT ?
        """, (since, today, limit)).fetchall()
        for project, day, revision in jobs:
            with storage.transaction(conn, immediate=False):
                daily = billed.calculate(conn, project, day, day + DAY_SECONDS, now)
            rows = [(project, day, instance, json.dumps(asdict(values), default=str, separators=(",", ":")))
                    for (_, instance), values in daily.items()]
            with storage.transaction(conn):
                published = conn.execute("""
                    UPDATE dashboard_billed_cost_days SET computed_revision=revision,computed_at=?
                    WHERE project_id=? AND day=? AND revision=? AND computed_revision IS NOT revision
                """, (now, project, day, revision))
                if not published.rowcount:
                    continue
                conn.execute("DELETE FROM dashboard_billed_cost_daily WHERE project_id=? AND day=?", (project, day))
                conn.executemany("INSERT INTO dashboard_billed_cost_daily VALUES (?,?,?,?)", rows)
                updated += 1
    return updated


def read(
    conn: Connection, project: str, since: int, today: int, now: int, instance: str | None,
) -> dict[tuple[date, str], billed.Totals]:
    if since == today:
        return billed.calculate(conn, project, since, now, now, instance)

    instance_filter = " AND d.instance_id=?" if instance is not None else ""
    args = (instance, project, since, today, now) if instance is not None else (project, since, today, now)
    ready, daily = set(), {}
    for row in conn.execute(f"""
        SELECT state.day,d.instance_id,d.totals FROM dashboard_billed_cost_days AS state
        LEFT JOIN dashboard_billed_cost_daily AS d ON d.project_id=state.project_id AND d.day=state.day{instance_filter}
        WHERE state.project_id=? AND state.day>=? AND state.day<?
          AND state.computed_revision=state.revision AND state.computed_at<=?
    """, args):
        ready.add(row["day"])
        if row["instance_id"] is not None:
            values = json.loads(row["totals"])
            for field in ("weighted_seconds", "matched_cash", "monitored_cash", "low_load_cash", "idle_cash"):
                values[field] = Decimal(values[field])
            daily[_local_date(row["day"]), row["instance_id"]] = billed.Totals(**values)

    # Adjacent missing days share one interval and order scan. Today's data is always live.
    start = None
    for day in range(since, today, DAY_SECONDS):
        if day not in ready:
            if start is None:
                start = day
        elif start is not None:
            daily.update(billed.calculate(conn, project, start, day, now, instance))
            start = None
    daily.update(billed.calculate(conn, project, today if start is None else start, now, now, instance))
    return daily


def trim(conn: Connection, now: int) -> None:
    conn.execute("DELETE FROM dashboard_billed_cost_days WHERE day<?", (_cutoff(now),))


def reset(conn: Connection) -> None:
    conn.execute("DELETE FROM dashboard_billed_cost_days")
