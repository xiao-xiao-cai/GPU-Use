"""Track evidence that closes the GPU idle episode described by a site notice."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from sqlite3 import Connection

from gpu_console import storage
from gpu_console.application.notice_policies import idle_permitted
from gpu_console.application.task_config import includes_event

SAMPLE_FRESHNESS = 120
MAX_SAMPLE_GAP = 90
LEGACY_THRESHOLD = re.compile(r" 的全部卡利用率已连续 \d+ 分钟低于 ([0-9.eE+\-]+)%。$")


@dataclass(frozen=True)
class IdleNotice:
    started_at: int
    first_idle_at: int
    confirmed_at: int
    threshold: float | None
    gpu_count: int | None


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS idle_notice_resolutions (
            notification_id INTEGER PRIMARY KEY REFERENCES notifications(id) ON DELETE CASCADE,
            outcome TEXT NOT NULL, observed_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idle_notices_project
            ON notifications(json_extract(payload,'$.project_id'))
            WHERE channel='site' AND event_id GLOB 'idle:*';
        CREATE INDEX IF NOT EXISTS idle_deliveries_project
            ON notifications(json_extract(payload,'$.project_id'))
            WHERE event_id GLOB 'idle:*';
    """)


def notice_context(event_id: str, payload: dict) -> IdleNotice | None:
    project_id, instance_id = payload.get("project_id"), payload.get("instance_id")
    if not all(isinstance(value, str) and value for value in (project_id, instance_id)):
        return None
    prefix = f"idle:{project_id}:{instance_id}:"
    if not event_id.startswith(prefix):
        return None
    episode, _, _ = event_id[len(prefix):].partition(":repeat:")
    try:
        started_at, first_idle_at = map(int, episode.split(":"))
    except ValueError:
        return None
    confirmed_at = payload.get("confirmed_at")
    if type(confirmed_at) is not int or not 0 < started_at <= first_idle_at <= confirmed_at:
        return None
    threshold = payload.get("idle_threshold")
    if threshold is None and isinstance(payload.get("message"), str):
        match = LEGACY_THRESHOLD.search(payload["message"])
        if match:
            try:
                threshold = float(match[1])
            except ValueError:
                pass
    if type(threshold) not in (int, float) or not 0 <= threshold <= 100:
        threshold = None
    gpu_count = payload.get("gpu_count")
    return IdleNotice(started_at, first_idle_at, confirmed_at, threshold,
                      gpu_count if type(gpu_count) is int and gpu_count > 0 else None)


def complete_cards(cards: dict[str, float], expected: int) -> bool:
    return expected > 0 and len(cards) == expected and "" not in cards and all(0 <= value <= 100 for value in cards.values())


def subscriptions(conn: Connection, project_id: str, instance_id: str, started_at: int, settings: dict) -> tuple[list[str], dict]:
    owner = conn.execute(
        """SELECT r.id,r.snapshot FROM idle_notification_runs AS i JOIN task_runs AS r ON r.id=i.run_id
           WHERE i.project_id=? AND i.instance_id=? AND i.start_time=?""",
        (project_id, instance_id, started_at),
    ).fetchone()
    if owner:
        snapshot = json.loads(owner["snapshot"])
        if not includes_event(snapshot, "idle"):
            return [], {}
        return snapshot["channels"], {
            "recipient": snapshot["config"].get("email"), "task_name": snapshot["name"], "run_id": owner["id"],
        }
    recipient = settings["idle_email_recipient"]
    channels = [channel for channel in settings["idle_channels"] if channel != "email" or recipient]
    return channels, {"recipient": recipient}


def delivery_hold(
    conn: Connection, row: dict, payload: dict, timestamp: int,
) -> tuple[str, str] | None:
    """Check the evidence again immediately before an external send."""
    if not row["event_id"].startswith("idle:"):
        return None
    context = notice_context(row["event_id"], payload)
    if context is None:
        return None
    project_id, instance_id = payload["project_id"], payload["instance_id"]
    ended = conn.execute(
        "SELECT 1 FROM idle_notice_resolutions WHERE notification_id=?", (row["id"],),
    ).fetchone()
    if ended:
        return "skipped", "原空闲状态已结束"
    settings = storage.get_settings(conn)
    channels, details = subscriptions(conn, project_id, instance_id, context.started_at, settings)
    if row["channel"] not in channels:
        return "skipped", "此渠道已取消该空闲提醒"
    if row["channel"] == "email" and payload.get("recipient") != details["recipient"]:
        return "skipped", "邮件收件地址已更改"
    if context.threshold != settings["idle_threshold"]:
        return "skipped", "空闲判定阈值已更改"
    sampled_at = active_sample(conn, project_id, instance_id, context, timestamp)
    if sampled_at is None:
        return "pending", "等待新鲜的空闲采样"
    if not idle_permitted(conn, project_id, instance_id, timestamp, sampled_at, settings):
        return "pending", "等待空闲通知时段或实例恢复提醒"
    return None


def active_sample(
    conn: Connection, project_id: str, instance_id: str, context: IdleNotice, timestamp: int,
) -> int | None:
    row = conn.execute(
        """SELECT s.last_sample_at FROM idle_state s LEFT JOIN idle_episodes e
               ON e.project_id=s.project_id AND e.instance_id=s.instance_id AND e.started_at=s.started_at
           WHERE s.project_id=? AND s.instance_id=? AND s.started_at=?
                 AND COALESCE(e.episode_at,s.first_idle_at)=? AND s.first_idle_at IS NOT NULL
                 AND s.last_sample_at BETWEEN ? AND ?""",
        (project_id, instance_id, context.started_at, context.first_idle_at,
         max(context.confirmed_at, timestamp - SAMPLE_FRESHNESS), timestamp),
    ).fetchone()
    return row["last_sample_at"] if row else None


def observe(
    conn: Connection, project_id: str, instances: list[dict],
    samples: dict[str, dict[int, dict[str, float]]], timestamp: int,
    on_recovery: Callable[[dict, IdleNotice, int], None] | None = None,
) -> None:
    if not instances:
        return
    rows = {row["id"]: row for row in instances if row.get("project_id") == project_id}
    unresolved = conn.execute(
        """SELECT n.id,n.event_id,n.payload,n.state FROM notifications n
           LEFT JOIN idle_notice_resolutions r ON r.notification_id=n.id
           WHERE n.event_id GLOB 'idle:*'
             AND json_extract(n.payload,'$.project_id')=? AND r.notification_id IS NULL""",
        (project_id,),
    ).fetchall()
    for notice in unresolved:
        payload = json.loads(notice["payload"])
        instance_id = payload.get("instance_id")
        row = rows.get(instance_id) if isinstance(instance_id, str) else None
        context = notice_context(notice["event_id"], payload)
        if not row or not context or context.confirmed_at >= timestamp:
            continue
        state, boot = row.get("state"), row.get("start_time")
        outcome, observed_at = None, timestamp
        if state == "Stopped":
            outcome = "stopped"
        elif state == "Running" and type(boot) is int and context.started_at < boot <= timestamp and boot >= context.confirmed_at:
            outcome = "new_boot"
        elif state == "Running" and boot == context.started_at and context.threshold is not None:
            gpu_count = row.get("gpu")
            if type(gpu_count) is not int or context.gpu_count not in (None, gpu_count):
                continue
            recovered_at = min((
                sampled_at for sampled_at, cards in samples[instance_id].items()
                if context.confirmed_at < sampled_at <= timestamp and timestamp - sampled_at <= SAMPLE_FRESHNESS
                and complete_cards(cards, gpu_count) and max(cards.values()) >= context.threshold
            ), default=None)
            if recovered_at is not None:
                outcome, observed_at = "recovered", recovered_at
        if outcome is not None:
            conn.execute(
                "INSERT INTO idle_notice_resolutions VALUES (?,?,?) ON CONFLICT(notification_id) DO NOTHING",
                (notice["id"], outcome, observed_at),
            )
            if outcome == "recovered" and on_recovery and notice["state"] == "delivered":
                on_recovery(row, context, observed_at)
