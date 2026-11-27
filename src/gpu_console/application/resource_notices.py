"""Evaluate GPU signals from collected samples and known stock transitions."""

import json
from dataclasses import asdict, dataclass, field
from sqlite3 import Connection, Row

from gpu_console import storage
from gpu_console.application.idle_notices import MAX_SAMPLE_GAP, SAMPLE_FRESHNESS, IdleNotice, complete_cards
from gpu_console.application.notice_policies import emit_recurring, idle_permitted

GPU_UTIL = "cloudwatch_gpu_util"
GPU_MEMORY = "cloudwatch_gpu_memory_usage"
MIN_MONITORING_GAP = 300


@dataclass
class MemoryState:
    threshold: float
    duration: int
    last_at: int = 0
    high_since: dict[str, int] = field(default_factory=dict)
    alerted_cards: list[str] = field(default_factory=list)
    episode_at: int | None = None


@dataclass
class MonitoringState:
    last_complete_at: int | None = None
    episode_at: int | None = None


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS gpu_notice_states (
            project_id TEXT NOT NULL, instance_id TEXT NOT NULL, started_at INTEGER NOT NULL,
            memory TEXT, monitoring TEXT, PRIMARY KEY(project_id,instance_id)
        );
        CREATE TABLE IF NOT EXISTS gpu_notice_episodes (
            event_id TEXT PRIMARY KEY, outcome TEXT NOT NULL, observed_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stock_notice_baselines (
            watch_id TEXT PRIMARY KEY REFERENCES watches(id) ON DELETE CASCADE, available INTEGER NOT NULL
        );
    """)


def _instance_payload(instance: dict, title: str, message: str, confirmed_at: int) -> dict:
    return {
        "title": title, "message": message, "project_id": instance["project_id"], "instance_id": instance["id"],
        "gpu_model": instance["gpu_model"], "zone": instance["zone"], "confirmed_at": confirmed_at,
    }


def _channel_payload(payload: dict, rules: dict) -> dict:
    return payload | {"recipient": rules["email_recipient"]}


def _episode_id(kind: str, scope: tuple[str, str], started_at: int, episode_at: int) -> str:
    return f"{kind}:{scope[0]}:{scope[1]}:{started_at}:{episode_at}"


def _save_episode(conn: Connection, event_id: str, outcome: str, observed_at: int) -> None:
    conn.execute(
        """INSERT INTO gpu_notice_episodes VALUES (?,?,?) ON CONFLICT(event_id) DO UPDATE
           SET outcome=excluded.outcome, observed_at=excluded.observed_at""",
        (event_id, outcome, observed_at),
    )


def _memory_samples(instance: dict, points: list[dict], timestamp: int) -> dict[int, dict[str, float]]:
    samples: dict[int, dict[str, float]] = {}
    for point in points:
        sampled_at = point["sampled_at"]
        if (point["metric"] == GPU_MEMORY and point["project_id"] == instance["project_id"]
                and point["instance_id"] == instance["id"] and instance["start_time"] <= sampled_at <= timestamp
                and point["label"] and 0 <= point["value"] <= 100):
            samples.setdefault(sampled_at, {})[point["label"]] = point["value"]
    return samples


def _observe_memory(
    conn: Connection, instance: dict, scope: tuple[str, str], state: MemoryState,
    points: list[dict], timestamp: int, settings: dict,
) -> None:
    rules = settings["notification_rules"]
    started = instance["start_time"]
    samples = _memory_samples(instance, points, timestamp)
    latest = max(samples, default=0)
    for sampled_at, cards in sorted(samples.items()):
        if sampled_at <= state.last_at:
            continue
        if sampled_at - state.last_at > MAX_SAMPLE_GAP:
            state.high_since.clear()
        state.last_at = sampled_at
        state.high_since = {
            card: state.high_since.get(card, sampled_at) for card, value in cards.items() if value >= state.threshold
        }
        qualified = {card for card, first in state.high_since.items() if sampled_at - first >= state.duration}
        if timestamp - sampled_at > SAMPLE_FRESHNESS:
            continue
        if state.episode_at is not None:
            still_high = set(state.alerted_cards) | qualified
            if (complete_cards(cards, instance["gpu"])
                    and all(card in cards and cards[card] < state.threshold - 5 for card in still_high)):
                _save_episode(conn, _episode_id("gpu-memory-high", scope, started, state.episode_at), "recovered", sampled_at)
                state.episode_at = None
                state.alerted_cards = []
            else:
                state.alerted_cards = sorted(still_high)

        if not qualified:
            if (state.episode_at is not None and complete_cards(cards, instance["gpu"])
                    and set(state.alerted_cards) <= cards.keys()):
                _save_episode(conn, _episode_id("gpu-memory-high", scope, started, state.episode_at), "active", sampled_at)
            continue
        if state.episode_at is None:
            state.episode_at = sampled_at
            state.alerted_cards = sorted(qualified)
        event_id = _episode_id("gpu-memory-high", scope, started, state.episode_at)
        _save_episode(conn, event_id, "active", sampled_at)
        # Qualify streaks across samples, and send only if the latest sample still confirms the condition.
        if sampled_at != latest:
            continue
        maxima = "、".join(f"{card} {cards[card]:g}%" for card in sorted(qualified))
        payload = _instance_payload(
            instance, "GPU 显存持续高占用",
            f"{instance['name']} 的显存使用率已连续 {state.duration // 60} 分钟达到 {state.threshold:g}%（{maxima}）。",
            sampled_at,
        ) | {"memory_threshold": state.threshold, "memory_duration_minutes": state.duration // 60}
        emit_recurring(conn, event_id, _channel_payload(payload, rules), rules["gpu_memory_high"]["channels"],
                       settings["notification_repeat_minutes"])


def _observe_monitoring(
    conn: Connection, instance: dict, scope: tuple[str, str], state: MonitoringState,
    utilization: dict[int, dict[str, float]], timestamp: int, settings: dict,
) -> None:
    rules = settings["notification_rules"]
    channels = rules["monitoring"]["channels"]
    complete_at = max((
        sampled_at for sampled_at, cards in utilization.items()
        if 0 <= timestamp - sampled_at <= SAMPLE_FRESHNESS and complete_cards(cards, instance["gpu"])
    ), default=None)
    if complete_at is not None and (state.last_complete_at is None or complete_at > state.last_complete_at):
        state.last_complete_at = complete_at
        if state.episode_at is not None:
            episode_id = _episode_id("monitoring-interrupted", scope, instance["start_time"], state.episode_at)
            _save_episode(conn, episode_id, "recovered", complete_at)
            recovered = _episode_id("monitoring-restored", scope, instance["start_time"], state.episode_at)
            payload = _instance_payload(instance, "GPU 监控采样恢复", f"{instance['name']} 已收到新鲜的全卡利用率样本。", complete_at)
            storage.emit(conn, recovered, _channel_payload(payload, rules), channels)
            state.episode_at = None
    interval = max(MIN_MONITORING_GAP, 3 * settings["collection_seconds"])
    if state.last_complete_at is None or timestamp - state.last_complete_at < interval:
        return
    if state.episode_at is None:
        state.episode_at = timestamp
    event_id = _episode_id("monitoring-interrupted", scope, instance["start_time"], state.episode_at)
    _save_episode(conn, event_id, "active", timestamp)
    payload = _instance_payload(
        instance, "GPU 监控采样中断", f"{instance['name']} 仍在运行，已超过 {interval / 60:g} 分钟未收到全卡利用率样本。",
        timestamp,
    ) | {"last_sample_at": state.last_complete_at}
    emit_recurring(conn, event_id, _channel_payload(payload, rules), channels, settings["notification_repeat_minutes"])


def _retire(
    conn: Connection, kind: str, scope: tuple[str, str], saved: Row,
    state: MemoryState | MonitoringState | None, outcome: str, timestamp: int,
) -> None:
    if state and state.episode_at is not None:
        _save_episode(conn, _episode_id(kind, scope, saved["started_at"], state.episode_at), outcome, timestamp)


def observe_instances(
    conn: Connection, project_id: str, instances: list[dict], all_points: dict[str, list[dict]],
    utilization: dict[str, dict[int, dict[str, float]]], timestamp: int, settings: dict,
) -> None:
    rules = settings["notification_rules"]
    memory_rule, monitoring_rule = rules["gpu_memory_high"], rules["monitoring"]
    existing = {
        row["instance_id"]: row for row in conn.execute("SELECT * FROM gpu_notice_states WHERE project_id=?", (project_id,))
    }
    if not (existing or memory_rule["enabled"] or monitoring_rule["enabled"]):
        return
    instances_by_id = {item["id"]: item for item in instances if item.get("project_id") == project_id}
    for identity in sorted(existing.keys() | instances_by_id.keys()):
        scope = (project_id, identity)
        saved = existing.get(identity)
        instance = instances_by_id.get(identity)
        memory = MemoryState(**json.loads(saved["memory"])) if saved and saved["memory"] else None
        monitoring = MonitoringState(**json.loads(saved["monitoring"])) if saved and saved["monitoring"] else None
        if saved:
            outcome = None
            if instance is None:
                outcome = "unknown"
            elif instance["state"] == "Stopped":
                outcome = "stopped"
            elif instance["state"] == "Running" and saved["started_at"] < (instance["start_time"] or 0) <= timestamp:
                outcome = "new_boot"
            if outcome:
                _retire(conn, "gpu-memory-high", scope, saved, memory, outcome, timestamp)
                _retire(conn, "monitoring-interrupted", scope, saved, monitoring, outcome, timestamp)
                conn.execute("DELETE FROM gpu_notice_states WHERE project_id=? AND instance_id=?", scope)
                memory = monitoring = saved = None

        if not instance or instance["state"] != "Running" or not instance["start_time"] or not instance["gpu"]:
            continue
        if saved and instance["start_time"] != saved["started_at"]:
            continue
        if memory_rule["enabled"]:
            policy = (memory_rule["threshold"], memory_rule["duration_minutes"] * 60)
            if memory and (memory.threshold, memory.duration) != policy:
                _retire(conn, "gpu-memory-high", scope, saved, memory, "policy_changed", timestamp)
                memory = MemoryState(*policy, last_at=timestamp - 1)
            elif memory is None:
                memory = MemoryState(*policy, last_at=timestamp - SAMPLE_FRESHNESS - 1)
            _observe_memory(conn, instance, scope, memory, all_points[identity], timestamp, settings)
        elif memory is not None:
            _retire(conn, "gpu-memory-high", scope, saved, memory, "disabled", timestamp)
            memory = None

        if monitoring_rule["enabled"]:
            monitoring = monitoring or MonitoringState()
            _observe_monitoring(conn, instance, scope, monitoring, utilization[identity], timestamp, settings)
        elif monitoring is not None:
            _retire(conn, "monitoring-interrupted", scope, saved, monitoring, "disabled", timestamp)
            monitoring = None

        if memory or monitoring:
            conn.execute(
                """INSERT INTO gpu_notice_states VALUES (?,?,?,?,?) ON CONFLICT(project_id,instance_id) DO UPDATE
                   SET started_at=excluded.started_at,memory=excluded.memory,monitoring=excluded.monitoring""",
                (*scope, instance["start_time"], json.dumps(asdict(memory)) if memory else None,
                 json.dumps(asdict(monitoring)) if monitoring else None),
            )
        elif saved:
            conn.execute("DELETE FROM gpu_notice_states WHERE project_id=? AND instance_id=?", scope)


def emit_watch_stock(conn: Connection, watch: Row, stock: dict, spec: dict, settings: dict) -> None:
    rules = settings["notification_rules"]
    timestamp = stock["capacity_checked_at"]
    payload = {
        "title": "关注规格当前可租",
        "message": f"平台确认 {watch['name']}（{spec['gpu']} 卡、{spec['cpu']} vCPU、{spec['memory_gb']} GB）当前可调度。",
        "confirmed_at": timestamp, "project_id": watch["project_id"], "watch_id": watch["id"],
        "gpu_model": spec["gpu_model"], "zone": spec["zone"], "catalog_spec_id": spec["catalog_spec_id"],
    }
    storage.emit(conn, f"watch-stock:{watch['id']}:{timestamp}", _channel_payload(payload, rules), rules["watch_stock"]["channels"])


def emit_idle_recovery(
    conn: Connection, instance: dict, context: IdleNotice, sampled_at: int, timestamp: int, settings: dict,
) -> None:
    rules = settings["notification_rules"]
    rule = rules["idle_recovery"]
    scope = (instance["project_id"], instance["id"])
    if not rule["enabled"] or not idle_permitted(conn, *scope, timestamp, sampled_at, settings):
        return
    payload = _instance_payload(
        instance, "GPU 空闲恢复", f"{instance['name']} 至少一张卡的利用率已回到 {context.threshold:g}% 或以上。", sampled_at,
    ) | {"start_time": context.started_at, "idle_threshold": context.threshold, "gpu_count": instance["gpu"]}
    event_id = _episode_id("idle-recovery", scope, context.started_at, context.first_idle_at)
    storage.emit(conn, event_id, _channel_payload(payload, rules), rule["channels"])


RESOURCE_EVENTS = {"idle-recovery", "gpu-memory-high", "monitoring-interrupted", "monitoring-restored", "watch-stock"}
RECURRING_EVENTS = {"gpu-memory-high", "monitoring-interrupted"}


def _instance_snapshot(conn: Connection, project_id: str, instance_id: str) -> tuple[dict, dict | None]:
    snapshot = storage.snapshot(conn, "instances", project_id)
    instance = next((row for row in snapshot["data"]
                     if row.get("id") == instance_id and row.get("project_id", project_id) == project_id), None)
    return snapshot, instance


def _is_fresh(snapshot: dict, timestamp: int, freshness: int) -> bool:
    observed = snapshot["success_at"]
    return not snapshot["error"] and observed is not None and 0 <= timestamp - observed <= freshness


def business(conn: Connection, item: dict, timestamp: int, settings: dict) -> dict:
    project_id, payload, kind = item["project_id"], item["payload"], item["event_type"]
    if not project_id:
        return {}
    if kind == "watch-stock":
        from gpu_console.application.watches import view

        watch_id = payload.get("watch_id")
        watch = conn.execute("SELECT * FROM watches WHERE id=? AND project_id=?", (watch_id, project_id)).fetchone()
        if not watch:
            return {"label": "原关注规格已移除", "observed_at": payload.get("confirmed_at")}
        stock = view(conn, watch, timestamp)
        result = {"destination": {"page": "inventory", "project_id": project_id, "watch_id": watch_id}}
        if stock["available"] is None:
            return result | {"status": "unknown", "label": "规格可租状态待核对"}
        return result | {
            "status": "completed", "label": "规格当前可租" if stock["available"] else "规格当前不可租",
            "observed_at": stock["capacity_checked_at"],
        }
    instance_id = payload.get("instance_id")
    snapshot, instance = _instance_snapshot(conn, project_id, instance_id)
    destination = {"page": "instances", "project_id": project_id, "instance_id": instance_id} if instance else None
    result = {"destination": destination, "observed_at": payload.get("confirmed_at")}
    if kind in {"idle-recovery", "monitoring-restored"}:
        return result | {"status": "resolved", "label": "已记录恢复"}
    episode_id = item["event_id"].rpartition(":repeat:")[0] or item["event_id"]
    episode = conn.execute("SELECT outcome,observed_at FROM gpu_notice_episodes WHERE event_id=?", (episode_id,)).fetchone()
    if episode is None:
        return result
    outcome, observed = episode["outcome"], episode["observed_at"]
    result["observed_at"] = observed
    completed = {
        "recovered": ("显存占用已回落" if kind == "gpu-memory-high" else "监控采样已恢复"),
        "stopped": "原实例已关机", "new_boot": "原实例已重新启动",
    }.get(outcome)
    if completed:
        return result | {"status": "resolved", "label": completed}
    if outcome in {"disabled", "policy_changed"}:
        return result | {"label": "已关闭此通知项" if outcome == "disabled" else "显存判定条件已更改"}
    freshness = SAMPLE_FRESHNESS if kind == "gpu-memory-high" else max(SAMPLE_FRESHNESS, 2 * settings["collection_seconds"])
    if (outcome == "active" and instance and instance["state"] == "Running"
            and 0 <= timestamp - observed <= freshness and _is_fresh(snapshot, timestamp, freshness)):
        return result | {"status": "needs_attention", "attention": True,
                         "label": "显存仍处于提醒状态" if kind == "gpu-memory-high" else "仍未收到完整采样"}
    return result | {"status": "unknown", "label": "待核对实例与监控状态"}


def delivery_hold(conn: Connection, row: Row, payload: dict, timestamp: int) -> tuple[str, str] | None:
    event_id = row["event_id"]
    kind = event_id.split(":", 1)[0]
    if kind not in RECURRING_EVENTS | {"idle-recovery"}:
        return None
    settings = storage.get_settings(conn)
    rules = settings["notification_rules"]
    rule = rules[{"monitoring-interrupted": "monitoring", "gpu-memory-high": "gpu_memory_high",
                  "idle-recovery": "idle_recovery"}[kind]]
    if not rule["enabled"] or row["channel"] not in rule["channels"]:
        return "skipped", "此渠道已取消该类提醒"
    if row["channel"] == "email" and payload.get("recipient") != rules["email_recipient"]:
        return "skipped", "邮件收件地址已更改"
    if kind in RECURRING_EVENTS:
        if kind == "gpu-memory-high" and (payload["memory_threshold"], payload["memory_duration_minutes"]) != (
            rule["threshold"], rule["duration_minutes"],
        ):
            return "skipped", "显存判定条件已更改"
        episode_id = event_id.rpartition(":repeat:")[0] or event_id
        episode = conn.execute("SELECT outcome,observed_at FROM gpu_notice_episodes WHERE event_id=?", (episode_id,)).fetchone()
        if episode is not None and episode["outcome"] != "active":
            return "skipped", "原持续状态已经结束或无法核实"
        freshness = SAMPLE_FRESHNESS if kind == "gpu-memory-high" else max(SAMPLE_FRESHNESS, 2 * settings["collection_seconds"])
        if episode is None or timestamp - episode["observed_at"] > freshness:
            return "pending", "等待新鲜的监控状态"
        snapshot, instance = _instance_snapshot(conn, payload["project_id"], payload["instance_id"])
        if not instance or instance["state"] != "Running" or not _is_fresh(snapshot, timestamp, freshness):
            return "pending", "等待最新的实例状态"
        return None

    project_id, instance_id = payload["project_id"], payload["instance_id"]
    snapshot, instance = _instance_snapshot(conn, project_id, instance_id)
    if not instance or not _is_fresh(snapshot, timestamp, SAMPLE_FRESHNESS):
        return "pending", "等待最新的实例状态"
    if instance["state"] == "Stopped" or instance["start_time"] != payload["start_time"]:
        return "skipped", "恢复消息对应的实例已经关机或重新启动"
    recent = conn.execute(
        """SELECT sampled_at,label,value FROM metrics
           WHERE project_id=? AND instance_id=? AND metric=? AND sampled_at BETWEEN ? AND ?
           ORDER BY sampled_at DESC""",
        (project_id, instance_id, GPU_UTIL, max(timestamp - SAMPLE_FRESHNESS, payload["confirmed_at"]), timestamp),
    ).fetchall()
    if not recent:
        return "pending", "等待新鲜的 GPU 利用率采样"
    newest = recent[0]["sampled_at"]
    cards = {point["label"]: point["value"] for point in recent if point["sampled_at"] == newest}
    if not complete_cards(cards, payload["gpu_count"]):
        return "pending", "等待新鲜的全卡利用率采样"
    if max(cards.values()) < payload["idle_threshold"]:
        return "skipped", "GPU 利用率已再次低于原空闲阈值"
    if not idle_permitted(conn, project_id, instance_id, timestamp, newest, settings):
        return "pending", "等待空闲通知时段或实例恢复提醒"
    return None
