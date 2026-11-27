"""Determine when recurring notices may enter the delivery queue."""

from datetime import datetime
from sqlite3 import Connection

from gpu_console import storage
from gpu_console.application.task_config import SHANGHAI


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS idle_snoozes (
            project_id TEXT NOT NULL, instance_id TEXT NOT NULL, until INTEGER,
            PRIMARY KEY(project_id,instance_id)
        );
        CREATE TABLE IF NOT EXISTS notice_cadence (
            episode_id TEXT NOT NULL, channel TEXT NOT NULL, event_id TEXT NOT NULL,
            enqueued_at INTEGER NOT NULL, PRIMARY KEY(episode_id,channel)
        );
    """)


def snooze(conn: Connection, project_id: str, instance_id: str, timestamp: int) -> dict:
    row = conn.execute(
        "SELECT until FROM idle_snoozes WHERE project_id=? AND instance_id=?",
        (project_id, instance_id),
    ).fetchone()
    paused = row is not None and (row["until"] is None or row["until"] > timestamp)
    return {"paused": paused, "until": row["until"] if paused else None}


def set_snooze(conn: Connection, project_id: str, instance_id: str, until: int | None) -> None:
    conn.execute(
        """INSERT INTO idle_snoozes VALUES (?,?,?) ON CONFLICT(project_id,instance_id)
           DO UPDATE SET until=excluded.until""",
        (project_id, instance_id, until),
    )


def in_idle_window(schedule: dict, timestamp: int) -> bool:
    mode = schedule["mode"]
    if mode != "scheduled":
        return mode == "all_day"
    local = datetime.fromtimestamp(timestamp, SHANGHAI)
    weekday = local.isoweekday()
    previous_day = (weekday - 2) % 7 + 1
    current = local.strftime("%H:%M")
    for entry in schedule["windows"]:
        day, start, end = entry["weekday"], entry["start"], entry["end"]
        if start < end and day == weekday and start <= current < end:
            return True
        if start > end and ((day == weekday and current >= start) or (day == previous_day and current < end)):
            return True
    return False


def idle_permitted(
    conn: Connection, project_id: str, instance_id: str, timestamp: int, sampled_at: int, settings: dict,
) -> bool:
    schedule = settings["idle_schedule"]
    if not in_idle_window(schedule, timestamp) or not in_idle_window(schedule, sampled_at):
        return False
    row = conn.execute(
        "SELECT until FROM idle_snoozes WHERE project_id=? AND instance_id=?",
        (project_id, instance_id),
    ).fetchone()
    return row is None or (row["until"] is not None and row["until"] <= min(timestamp, sampled_at))


def emit_recurring(
    conn: Connection, episode_id: str, payload: dict, channels: list[str], intervals: dict[str, int],
) -> None:
    timestamp = storage.now()
    for channel in dict.fromkeys(channels):
        last = conn.execute(
            """SELECT c.event_id,c.enqueued_at,n.state FROM notice_cadence c
               JOIN notifications n ON n.event_id=c.event_id AND n.channel=c.channel
               WHERE c.episode_id=? AND c.channel=?""",
            (episode_id, channel),
        ).fetchone()
        if last is None:
            # A notice queued before the cadence table was introduced is still the first notice.
            last = conn.execute(
                "SELECT state,created_at AS enqueued_at FROM notifications WHERE event_id=? AND channel=?",
                (episode_id, channel),
            ).fetchone()
        interval = intervals[channel]
        if last is not None and (last["state"] == "pending" or not interval
                                 or timestamp - last["enqueued_at"] < interval * 60):
            continue
        event_id = episode_id if last is None else f"{episode_id}:repeat:{timestamp}"
        storage.emit(conn, event_id, payload, [channel])
        conn.execute(
            """INSERT INTO notice_cadence VALUES (?,?,?,?) ON CONFLICT(episode_id,channel)
               DO UPDATE SET event_id=excluded.event_id,enqueued_at=excluded.enqueued_at""",
            (episode_id, channel, event_id, timestamp),
        )
