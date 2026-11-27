"""Persist observed GPU usage and project its daily dashboard totals."""

import json
from datetime import date, datetime, time, timedelta
from sqlite3 import Connection
from typing import Literal

from gpu_console import storage
from gpu_console.application.idle_notices import SAMPLE_FRESHNESS, complete_cards
from gpu_console.application.task_config import SHANGHAI
from gpu_console.application.tasks import ACTIVE_RUNS, RUN_LINKED_SHUTDOWN_JOIN, RUN_NEEDS_ATTENTION, RUN_SOURCE_TASK

UsageRange = Literal["today", "7d", "30d"]
PERIOD_DAYS = {"today": 1, "7d": 7, "30d": 30}
RETENTION_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS dashboard_usage_progress (
    project_id TEXT PRIMARY KEY, first_at INTEGER, sampled_at INTEGER NOT NULL,
    gap_limit INTEGER NOT NULL, instances TEXT
);
CREATE TABLE IF NOT EXISTS dashboard_usage_days (
    project_id TEXT NOT NULL, day INTEGER NOT NULL, observed_seconds INTEGER NOT NULL,
    PRIMARY KEY(project_id,day)
);
CREATE INDEX IF NOT EXISTS dashboard_usage_days_retention ON dashboard_usage_days(day);
CREATE TABLE IF NOT EXISTS dashboard_usage_instance_days (
    project_id TEXT NOT NULL, instance_id TEXT NOT NULL, day INTEGER NOT NULL,
    name TEXT NOT NULL, gpu_model TEXT, gpu_seconds INTEGER NOT NULL,
    utilization_gpu_seconds REAL NOT NULL, PRIMARY KEY(project_id,day,instance_id)
);
CREATE INDEX IF NOT EXISTS dashboard_usage_instance_days_retention ON dashboard_usage_instance_days(day);
"""


def initialize(conn: Connection) -> None:
    conn.executescript(SCHEMA)


def _local_date(timestamp: int) -> date:
    return datetime.fromtimestamp(timestamp, SHANGHAI).date()


def _midnight(day: date) -> int:
    return int(datetime.combine(day, time.min, SHANGHAI).timestamp())


def _observation(row: dict, readings: dict[int, dict[str, float]], timestamp: int) -> dict:
    observation = {
        "state": row.get("state"), "boot": row.get("start_time"), "stop": row.get("stop_time"),
        "name": row.get("name") or row["id"], "gpu_model": row.get("gpu_model"),
        "gpu": row.get("gpu"), "utilization": None, "without_gpu": row.get("gpu_mode") == "without_gpu",
    }
    if row.get("state") == "Running" and row.get("gpu_mode") == "with_gpu" and readings:
        sampled_at = max(readings)
        cards = readings[sampled_at]
        gpu, boot = observation["gpu"], observation["boot"]
        if type(gpu) is int and type(boot) is int and 0 < boot <= sampled_at <= timestamp and (
            timestamp - sampled_at <= SAMPLE_FRESHNESS and complete_cards(cards, gpu)
        ):
            observation["utilization"] = sum(cards.values()) / gpu
    return observation


def _classify(before: dict, after: dict) -> tuple[int, float] | None:
    if before["state"] != after["state"] or (before["boot"], before["stop"]) != (after["boot"], after["stop"]):
        return None
    if after["state"] == "Stopped":
        return 0, 0.0
    if after["state"] != "Running":
        return None
    if before["without_gpu"] and after["without_gpu"] and type(after["boot"]) is int and after["boot"] > 0:
        return 0, 0.0
    if before["utilization"] is None or after["utilization"] is None:
        return None
    if (before["gpu"], before["gpu_model"]) != (after["gpu"], after["gpu_model"]):
        return None
    return after["gpu"], (before["utilization"] + after["utilization"]) / 2


def _record_interval(conn: Connection, project_id: str, initial: dict, current: dict, start: int, end: int) -> None:
    classified = []
    for instance_id in initial.keys() & current.keys():
        proof = _classify(initial[instance_id], current[instance_id])
        if proof is not None:
            classified.append((instance_id, *proof))
    complete = len(classified) == len(initial) == len(current)
    while start < end:
        local_day = _local_date(start)
        day, boundary = _midnight(local_day), _midnight(local_day + timedelta(days=1))
        interval_end = min(end, boundary)
        seconds = interval_end - start
        if complete:
            conn.execute(
                """INSERT INTO dashboard_usage_days VALUES (?,?,?)
                   ON CONFLICT(project_id,day) DO UPDATE SET
                       observed_seconds=observed_seconds+excluded.observed_seconds""",
                (project_id, day, seconds),
            )
        conn.executemany(
            """INSERT INTO dashboard_usage_instance_days VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(project_id,day,instance_id) DO UPDATE SET name=excluded.name,gpu_model=excluded.gpu_model,
                   gpu_seconds=gpu_seconds+excluded.gpu_seconds,
                   utilization_gpu_seconds=utilization_gpu_seconds+excluded.utilization_gpu_seconds""",
            ((project_id, instance_id, day, current[instance_id]["name"], current[instance_id]["gpu_model"],
              gpu * seconds, utilization * gpu * seconds)
             for instance_id, gpu, utilization in classified if gpu),
        )
        start = interval_end


def observe(
    conn: Connection, project_id: str, rows: list[dict], card_readings: dict[str, dict[int, dict[str, float]]],
    timestamp: int, collection_seconds: int,
) -> None:
    """Pair neighboring successful inventory scans inside the caller's write transaction."""
    previous = conn.execute("SELECT * FROM dashboard_usage_progress WHERE project_id=?", (project_id,)).fetchone()
    if previous is not None and timestamp <= previous["sampled_at"]:
        return
    gap_limit = min(2 * collection_seconds, SAMPLE_FRESHNESS)
    current = {
        row["id"]: _observation(row, card_readings.get(row["id"], {}), timestamp)
        for row in rows if row.get("project_id") == project_id
        and not (row.get("gpu") == 0 and not row.get("gpu_model") and row.get("gpu_mode") != "without_gpu")
    }
    if previous is not None and previous["instances"] is not None and (
        timestamp - previous["sampled_at"] <= min(gap_limit, previous["gap_limit"])
    ):
        _record_interval(conn, project_id, json.loads(previous["instances"]), current, previous["sampled_at"], timestamp)
    conn.execute(
        """INSERT INTO dashboard_usage_progress VALUES (?,?,?,?,?)
           ON CONFLICT(project_id) DO UPDATE SET first_at=COALESCE(first_at,excluded.first_at),
               sampled_at=excluded.sampled_at,gap_limit=excluded.gap_limit,instances=excluded.instances""",
        (project_id, timestamp, timestamp, gap_limit, json.dumps(current, ensure_ascii=False, allow_nan=False)),
    )


def collection_failed(conn: Connection, project_id: str, timestamp: int) -> None:
    conn.execute(
        """INSERT INTO dashboard_usage_progress VALUES (?,NULL,?,0,NULL)
           ON CONFLICT(project_id) DO UPDATE SET sampled_at=excluded.sampled_at,instances=NULL
           WHERE sampled_at<excluded.sampled_at""",
        (project_id, timestamp),
    )


def restore_boundary(conn: Connection, timestamp: int) -> None:
    conn.execute("UPDATE dashboard_usage_progress SET sampled_at=MAX(sampled_at,?),instances=NULL", (timestamp,))


def trim(conn: Connection, timestamp: int) -> None:
    cutoff = _midnight(_local_date(timestamp) - timedelta(days=RETENTION_DAYS - 1))
    conn.execute("DELETE FROM dashboard_usage_days WHERE day<?", (cutoff,))
    conn.execute("DELETE FROM dashboard_usage_instance_days WHERE day<?", (cutoff,))


def _automation(conn: Connection, project_id: str, since: int, now: int) -> dict:
    rows = conn.execute(
        f"""SELECT r.id,r.state,r.stopped,r.updated_at,
                   json_extract(r.snapshot,'$.name') AS name,
                   json_extract(r.snapshot,'$.trigger.automation_rule_id') AS rule_id,
                   json_extract(r.result,'$.automation_reminded') AS reminded,
                   json_extract(r.result,'$.disposition') AS disposition,
                   json_extract(r.result,'$.instance_review.checked_at') AS checked_at,
                   COALESCE({RUN_NEEDS_ATTENTION},0) AS needs_review,
                   EXISTS (SELECT 1 FROM task_operations AS o WHERE o.run_id=r.id AND o.project_id=r.project_id
                           AND o.action='stop' AND o.state='accepted') AS accepted_stop,
                   CASE WHEN {RUN_SOURCE_TASK} IS NOT NULL AND json_extract(r.snapshot,'$.kind')='start'
                        AND json_type(r.result,'$.capacity_wait_since')='integer'
                        THEN json_extract(r.result,'$.capacity_wait_since') END AS wait_since
            FROM task_runs AS r {RUN_LINKED_SHUTDOWN_JOIN}
            WHERE r.project_id=:project AND r.updated_at<=:now
              AND (r.updated_at>=:since OR (r.state IN ('queued','waiting','reconciling') AND r.stopped=0
                  AND json_type(r.result,'$.capacity_wait_since')='integer'))
              AND (json_type(r.snapshot,'$.trigger.automation_rule_id')='text'
                  OR ({RUN_SOURCE_TASK} IS NOT NULL AND json_extract(r.snapshot,'$.kind')='start'))""",
        {"project": project_id, "now": now, "since": since},
    )
    totals = {"confirmed_shutdowns": 0, "reminded": 0, "needs_review": 0, "capacity_waits": 0, "capacity_completed": 0}
    recent = []
    completed_wait_seconds = 0
    for row in rows:
        outcome = None
        observed_at = row["updated_at"]
        wait_seconds = None
        checked_at = row["checked_at"]
        if row["rule_id"] and row["state"] == "succeeded" and row["disposition"] == "stopped" and (
            row["accepted_stop"] and type(checked_at) is int and since <= checked_at <= now
        ):
            totals["confirmed_shutdowns"] += 1
            outcome, observed_at = "confirmed_shutdown", checked_at
        elif row["rule_id"] and row["reminded"] and observed_at >= since:
            totals["reminded"] += 1
            outcome = "reminded"
        elif row["needs_review"] and observed_at >= since:
            totals["needs_review"] += 1
            outcome = "needs_review"

        wait_since = row["wait_since"]
        if type(wait_since) is int and 0 < wait_since <= row["updated_at"]:
            completed = row["state"] == "succeeded" and row["disposition"] == "started"
            active = row["state"] in ACTIVE_RUNS and not row["stopped"]
            ended = now if active else row["updated_at"]
            if completed and type(checked_at) is int and wait_since <= checked_at <= ended:
                ended = checked_at
            if ended >= since:
                totals["capacity_waits"] += 1
                wait_seconds = ended - wait_since
            if wait_seconds is not None and completed:
                totals["capacity_completed"] += 1
                completed_wait_seconds += wait_seconds
                outcome, observed_at = outcome or "capacity_completed", ended
            elif active:
                outcome = outcome or "capacity_waiting"
            elif wait_seconds is not None:
                outcome = outcome or "capacity_ended"
        if outcome is not None:
            recent.append({"run_id": row["id"], "name": row["name"] or row["id"], "observed_at": observed_at,
                           "outcome": outcome, "wait_seconds": wait_seconds})

    recent.sort(key=lambda item: (-item["observed_at"], item["run_id"]))
    completed = totals["capacity_completed"]
    return totals | {
        "average_wait_seconds": round(completed_wait_seconds / completed) if completed else None, "recent": recent[:12],
    }


def _usage(gpu_seconds: int, utilization_gpu_seconds: float, observed: bool) -> dict:
    return {
        "gpu_hours": gpu_seconds / 3600 if observed else None,
        "average_utilization": utilization_gpu_seconds / gpu_seconds if gpu_seconds else None,
    }


def summary(conn: Connection, project_id: str, period: UsageRange, now: int) -> dict:
    if period not in PERIOD_DAYS:
        raise ValueError("用量统计范围须为今天、近 7 天或近 30 天")
    if conn.in_transaction:
        return _summary(conn, project_id, period, now)
    # Read the coverage, instance totals and latest snapshot from the same WAL view.
    conn.execute("BEGIN")
    try:
        return _summary(conn, project_id, period, now)
    finally:
        conn.rollback()


def _summary(conn: Connection, project_id: str, period: UsageRange, now: int) -> dict:
    first_date = _local_date(now) - timedelta(days=PERIOD_DAYS[period] - 1)
    since, last_day = _midnight(first_date), _midnight(_local_date(now))
    from_range = (project_id, since, last_day)
    coverage = dict(conn.execute(
        "SELECT day,observed_seconds FROM dashboard_usage_days WHERE project_id=? AND day BETWEEN ? AND ?",
        from_range,
    ).fetchall())
    rows = conn.execute(
        """SELECT instance_id,day,name,gpu_model,gpu_seconds,utilization_gpu_seconds
           FROM dashboard_usage_instance_days WHERE project_id=? AND day BETWEEN ? AND ? ORDER BY day,instance_id""",
        from_range,
    )
    current_ids = {row["id"] for row in storage.snapshot(conn, "instances", project_id)["data"]}
    instance_totals, day_totals = {}, {}
    gpu_seconds, utilization_gpu_seconds = 0, 0.0
    for row in rows:
        instance_id, day = row["instance_id"], row["day"]
        seconds, util = row["gpu_seconds"], row["utilization_gpu_seconds"]
        day_seconds, day_util = day_totals.get(day, (0, 0.0))
        day_totals[day] = day_seconds + seconds, day_util + util
        instance = instance_totals.setdefault(instance_id, {"gpu_seconds": 0, "utilization_gpu_seconds": 0.0})
        instance.update(name=row["name"], gpu_model=row["gpu_model"])
        instance["gpu_seconds"] += seconds
        instance["utilization_gpu_seconds"] += util
        gpu_seconds += seconds
        utilization_gpu_seconds += util

    days = []
    for offset in range(PERIOD_DAYS[period]):
        day = first_date + timedelta(days=offset)
        start, end = _midnight(day), min(now, _midnight(day + timedelta(days=1)))
        seconds, util = day_totals.get(start, (0, 0.0))
        elapsed = end - start
        known = min(coverage.get(start, 0), elapsed)
        days.append({
            "date": day.isoformat(), **_usage(seconds, util, bool(seconds or known)),
            "coverage": known / elapsed if elapsed else 0.0, "covered_seconds": known, "elapsed_seconds": elapsed,
        })
    instances = [
        {"instance_id": instance_id, "name": row["name"], "gpu_model": row["gpu_model"],
         "current": instance_id in current_ids, **_usage(row["gpu_seconds"], row["utilization_gpu_seconds"], True)}
        for instance_id, row in sorted(instance_totals.items(), key=lambda item: (-item[1]["gpu_seconds"], item[0]))
    ]
    progress = conn.execute("SELECT first_at FROM dashboard_usage_progress WHERE project_id=?", (project_id,)).fetchone()
    return {
        "as_of": now, "since": since, "range": period, "recording_since": progress["first_at"] if progress else None,
        **_usage(gpu_seconds, utilization_gpu_seconds, bool(gpu_seconds or coverage)), "instances_used": len(instances),
        "days": days, "instances": instances, "automation": _automation(conn, project_id, since, now),
    }
