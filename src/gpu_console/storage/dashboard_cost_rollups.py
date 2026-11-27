"""Materialize per-instance cost totals in Beijing calendar days."""

import json
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from sqlite3 import Connection, Row

from gpu_console import storage
from gpu_console.storage import dashboard_cost as cost
from gpu_console.storage.dashboard_usage import UsageRange, _local_date, _midnight

REFRESH_SECONDS = 300
BATCH_SIZE = 8
DAY_SECONDS = 86400
# Retained observations use modern Beijing time (UTC+8).
BEIJING_OFFSET = 8 * 3600
MONEY_FIELDS = ("cost", "monitored_cost", "idle_cost", "low_load_cost")

SCHEMA = """
CREATE TABLE IF NOT EXISTS dashboard_cost_daily (
    project_id TEXT NOT NULL, day INTEGER NOT NULL, instance_id TEXT NOT NULL,
    data_through INTEGER NOT NULL, name TEXT NOT NULL, gpu_model TEXT, charge_type TEXT,
    totals TEXT NOT NULL, PRIMARY KEY(project_id,day,instance_id)
);
CREATE INDEX IF NOT EXISTS dashboard_cost_daily_retention ON dashboard_cost_daily(day);
CREATE TABLE IF NOT EXISTS dashboard_cost_dirty_days (
    project_id TEXT NOT NULL, day INTEGER NOT NULL, instance_id TEXT NOT NULL,
    due_at INTEGER NOT NULL, revision INTEGER NOT NULL, backfill INTEGER NOT NULL,
    PRIMARY KEY(project_id,day,instance_id)
);
CREATE INDEX IF NOT EXISTS dashboard_cost_dirty_due ON dashboard_cost_dirty_days(due_at,day DESC);
CREATE TABLE IF NOT EXISTS dashboard_cost_rollup_state (name TEXT PRIMARY KEY);
"""
UPSERT_DIRTY = """
    ON CONFLICT(project_id,day,instance_id) DO UPDATE SET
        due_at=MIN(excluded.due_at,dashboard_cost_dirty_days.due_at),
        revision=dashboard_cost_dirty_days.revision+1,
        backfill=MAX(excluded.backfill,dashboard_cost_dirty_days.backfill)
"""
SEED = "daily-v1"


def _day_sql(timestamp: str) -> str:
    return f"(({timestamp}) + {BEIJING_OFFSET}) / {DAY_SECONDS} * {DAY_SECONDS} - {BEIJING_OFFSET}"


def initialize(conn: Connection) -> None:
    conn.executescript(SCHEMA)
    due = f"(NEW.end + {REFRESH_SECONDS - 1}) / {REFRESH_SECONDS} * {REFRESH_SECONDS}"
    conn.executescript(f"""
        CREATE TRIGGER IF NOT EXISTS dashboard_cost_dirty_on_interval
        AFTER INSERT ON dashboard_cost_intervals BEGIN
            INSERT INTO dashboard_cost_dirty_days
            SELECT NEW.project_id, day, NEW.instance_id, {due}, 1, 0
            FROM (
                SELECT {_day_sql(f'NEW.start - {cost.SUSTAINED_SECONDS}')} AS day
                UNION SELECT {_day_sql('NEW.end - 1')}
            ) WHERE TRUE {UPSERT_DIRTY};
        END;
    """)
    # The rate just saved applies until the next manual rate, and only fills absent platform prices.
    for event, name in (("INSERT", "insert"), ("UPDATE OF hourly_rate", "update")):
        conn.executescript(f"""
            CREATE TRIGGER IF NOT EXISTS dashboard_cost_dirty_on_rate_{name}
            AFTER {event} ON dashboard_cost_manual_rates BEGIN
                INSERT INTO dashboard_cost_dirty_days
                SELECT NEW.project_id, day, NEW.instance_id, 0, 1, 0 FROM (
                    WITH next_rate AS (
                        SELECT COALESCE(MIN(effective_at),9223372036854775807) AS boundary
                        FROM dashboard_cost_manual_rates WHERE project_id=NEW.project_id
                          AND instance_id=NEW.instance_id AND effective_at>NEW.effective_at
                    ), affected AS (
                        SELECT MAX(i.start,NEW.effective_at) AS start, MIN(i.end,n.boundary) AS end
                        FROM dashboard_cost_intervals i CROSS JOIN next_rate n
                        WHERE i.project_id=NEW.project_id AND i.instance_id=NEW.instance_id
                          AND i.platform_rate IS NULL AND i.charge_type IN ('Postpay','Spot')
                          AND i.end>NEW.effective_at AND i.start<n.boundary
                    )
                    SELECT {_day_sql('start')} AS day FROM affected
                    UNION SELECT {_day_sql('end - 1')} FROM affected
                ) WHERE TRUE {UPSERT_DIRTY};
            END;
        """)


def _cutoff(now: int) -> int:
    return _midnight(_local_date(now) - timedelta(days=cost.RETENTION_DAYS - 1))


def _seed(conn: Connection, cutoff: int) -> None:
    if conn.execute("SELECT 1 FROM dashboard_cost_rollup_state WHERE name=?", (SEED,)).fetchone():
        return
    days = conn.execute(
        f"""SELECT project_id,{_day_sql('start')} AS day,instance_id FROM dashboard_cost_intervals WHERE end>?
            UNION SELECT project_id,{_day_sql('end - 1')},instance_id FROM dashboard_cost_intervals WHERE end>?""",
        (cutoff, cutoff),
    ).fetchall()
    with storage.transaction(conn):
        if conn.execute("INSERT OR IGNORE INTO dashboard_cost_rollup_state VALUES (?)", (SEED,)).rowcount:
            conn.executemany(
                "INSERT INTO dashboard_cost_dirty_days VALUES (?,?,?,0,1,1)" + UPSERT_DIRTY,
                (row for row in days if row["day"] >= cutoff),
            )


def _calculate(conn: Connection, job: Row, *, estimate_costs: bool = True) -> tuple | None:
    project, instance, day = job["project_id"], job["instance_id"], job["day"]
    finish = day + DAY_SECONDS
    context = cost.SUSTAINED_SECONDS + cost.SAMPLE_FRESHNESS
    rows = conn.execute(
        """SELECT * FROM dashboard_cost_intervals WHERE project_id=? AND instance_id=?
           AND start>=? AND start<? AND end>? AND end<=? ORDER BY start""",
        (project, instance, day - context, finish + context, day - cost.SUSTAINED_SECONDS, finish + context),
    ).fetchall()
    if not rows:
        return None
    progress = cost._progress(conn, project)
    first_at = progress["first_at"] if progress else None
    rates = cost._rates(conn, project, instance)[instance] if estimate_costs else []
    times = [at for at, _ in rates]
    counts, idle = cost._load_counts(rows)
    total = cost.Totals()
    last = None
    for row, low, whole_idle in zip(rows, counts, idle):
        start, end = max(row["start"], day), min(row["end"], finish)
        if start >= end:
            continue
        last = row
        recovered = first_at is not None and row["start"] < first_at
        pieces = cost._price_segments(row, start, end, rates, times) if estimate_costs else [(end - start, None, None)]
        for seconds, rate, source in pieces:
            total.add(row, seconds, low, whole_idle, rate, source, recovered)
    if last is None:
        return None
    return project, day, instance, min(last["end"], finish), last["name"], last["gpu_model"], last["charge_type"], (
        json.dumps(asdict(total), default=str, separators=(",", ":"))
    )


def refresh(path: Path, now: int, *, limit: int = BATCH_SIZE, estimate_costs: bool = True) -> int:
    """Compute without the write lock, then publish if the inputs have not changed."""
    cutoff = _cutoff(now)
    updated = 0
    with storage.connect(path) as conn:
        _seed(conn, cutoff)
        jobs = conn.execute(
            """SELECT * FROM dashboard_cost_dirty_days WHERE due_at<=? AND day>=?
               ORDER BY due_at,day DESC,project_id,instance_id LIMIT ?""", (now, cutoff, limit),
        ).fetchall()
        for job in jobs:
            key = (job["project_id"], job["day"], job["instance_id"])
            with storage.transaction(conn, immediate=False):
                row = _calculate(conn, job) if estimate_costs else _calculate(conn, job, estimate_costs=False)
            with storage.transaction(conn):
                deleted = conn.execute(
                    "DELETE FROM dashboard_cost_dirty_days WHERE project_id=? AND day=? AND instance_id=? AND revision=?",
                    (*key, job["revision"]),
                )
                if not deleted.rowcount:
                    continue
                if row is None:
                    conn.execute("DELETE FROM dashboard_cost_daily WHERE project_id=? AND day=? AND instance_id=?", key)
                else:
                    conn.execute(
                        """INSERT INTO dashboard_cost_daily VALUES (?,?,?,?,?,?,?,?)
                           ON CONFLICT(project_id,day,instance_id) DO UPDATE SET data_through=excluded.data_through,
                           name=excluded.name,gpu_model=excluded.gpu_model,charge_type=excluded.charge_type,
                           totals=excluded.totals""", row,
                    )
                updated += 1
    return updated


def trim(conn: Connection, now: int) -> None:
    cutoff = _cutoff(now)
    conn.execute("DELETE FROM dashboard_cost_daily WHERE day<?", (cutoff,))
    conn.execute("DELETE FROM dashboard_cost_dirty_days WHERE day<?", (cutoff,))


def reset(conn: Connection) -> None:
    conn.execute("DELETE FROM dashboard_cost_daily")
    conn.execute("DELETE FROM dashboard_cost_dirty_days")
    conn.execute("DELETE FROM dashboard_cost_rollup_state")


def read(
    conn: Connection, project: str, period: UsageRange, now: int, instance: str | None, *, estimate_costs: bool = True,
) -> dict | None:
    if not conn.execute("SELECT 1 FROM dashboard_cost_rollup_state WHERE name=?", (SEED,)).fetchone():
        return None
    since, today = _midnight(cost._first_day(period, now)), _midnight(_local_date(now))
    scope = " WHERE project_id=? AND day BETWEEN ? AND ?"
    params = (project, since, today)
    if instance is not None:
        scope += " AND instance_id=?"
        params = (*params, instance)

    pending = conn.execute("SELECT MAX(backfill) FROM dashboard_cost_dirty_days" + scope, params).fetchone()[0]
    if pending == 1:
        return None

    totals, days = cost.Totals(), cost._day_totals(period, now)
    per_instance, metadata = {}, {}
    data_through = None
    for row in conn.execute("SELECT * FROM dashboard_cost_daily" + scope + " ORDER BY day,instance_id", params):
        if row["data_through"] > now:
            return None
        amount = json.loads(row["totals"])
        for field in MONEY_FIELDS:
            amount[field] = Decimal(amount[field])
        values = cost.Totals(**amount)
        key = row["instance_id"]
        totals.merge(values)
        days[_local_date(row["day"]).isoformat()].merge(values)
        per_instance.setdefault(key, cost.Totals()).merge(values)
        metadata[key] = {k: row[k] for k in ("instance_id", "name", "gpu_model", "charge_type")}
        data_through = max(data_through or 0, row["data_through"])

    rates = cost._rates(conn, project, instance) if estimate_costs else {}
    report = cost._format_summary(conn, project, period, now, instance, cost._progress(conn, project),
                                  rates, totals, days, metadata, per_instance, estimate_costs=estimate_costs)
    return report | cost._detail_view([], []) | {"data_through": data_through, "aggregation_pending": pending is not None}
