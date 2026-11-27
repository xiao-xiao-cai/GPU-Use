"""Paginate notification records and resolve their current site destinations."""

import json
from sqlite3 import Connection

from gpu_console import storage
from gpu_console.storage import capacity
from gpu_console.application import watches
from gpu_console.application import resource_notices
from gpu_console.application.idle_notices import SAMPLE_FRESHNESS, active_sample, notice_context
from gpu_console.application.notice_policies import idle_permitted
from gpu_console.application.task_config import validate_spec
from gpu_console.application.tasks import RUN_BUSINESS_COLUMNS, RUN_LINKED_SHUTDOWN_JOIN, run_with_business

EVENT_TYPE = """CASE
    WHEN event_id GLOB 'idle:*' THEN 'idle'
    WHEN event_id GLOB 'idle-recovery:*' THEN 'idle-recovery'
    WHEN event_id GLOB 'gpu-memory-high:*' THEN 'gpu-memory-high'
    WHEN event_id GLOB 'monitoring-interrupted:*' THEN 'monitoring-interrupted'
    WHEN event_id GLOB 'monitoring-restored:*' THEN 'monitoring-restored'
    WHEN event_id GLOB 'watch-stock:*' THEN 'watch-stock'
    WHEN event_id GLOB 'rental-window:*' THEN 'rental-window'
    WHEN event_id GLOB 'test-*' THEN 'test'
    WHEN event_id GLOB '*:capacity-wait' THEN 'capacity-wait'
    WHEN event_id GLOB '*:result' AND EXISTS (
        SELECT 1 FROM task_runs r WHERE r.id=json_extract(notifications.payload,'$.run_id')
          AND r.project_id=json_extract(notifications.payload,'$.project_id') AND r.state='succeeded'
          AND notifications.event_id=r.id || ':result' AND json_extract(r.snapshot,'$.kind')='stock'
    ) THEN 'stock'
    WHEN event_id GLOB '*:result' THEN 'result'
    WHEN event_id GLOB '*:review' THEN 'review'
    WHEN event_id GLOB '*:shutdown-pending' THEN 'shutdown-pending'
    WHEN event_id GLOB '*:shutdown-outcome' OR event_id GLOB '*:shutdown-confirmed' THEN 'shutdown-outcome'
    ELSE 'legacy'
END"""
PROJECT_ID = """CASE WHEN json_type(payload,'$.project_id')='text'
    THEN NULLIF(json_extract(payload,'$.project_id'),'') END"""


def list_notifications(
    conn: Connection, *, limit: int, channel: str | None, project_id: str | None,
    event_type: str | None, event_id: str | None, cursor: int | None,
) -> dict:
    conditions = ["1=1"]
    if channel == "external":
        conditions.append("channel IN ('feishu','email')")
    elif channel is not None:
        conditions.append("channel=:channel")
    if cursor is not None:
        conditions.append("id<:cursor")
    if event_id is not None:
        conditions.append("event_id=:event_id")
    rows = conn.execute(
        f"""WITH records AS (
                SELECT *, {EVENT_TYPE} AS event_type, {PROJECT_ID} AS project_id
                FROM notifications WHERE {' AND '.join(conditions)}
            ) SELECT records.*, i.outcome AS idle_outcome, i.observed_at AS idle_observed_at
            FROM records LEFT JOIN idle_notice_resolutions i ON i.notification_id=records.id
            WHERE (:project_id IS NULL OR project_id=:project_id)
              AND (:event_type IS NULL OR event_type=:event_type)
            ORDER BY records.id DESC LIMIT :take""",
        {
            "channel": channel, "cursor": cursor, "event_id": event_id,
            "project_id": project_id, "event_type": event_type, "take": limit + 1,
        },
    ).fetchall()
    items = [dict(row) | {"payload": json.loads(row["payload"])} for row in rows[:limit]]
    site = [item for item in items if item["channel"] == "site"]
    linked_runs = _linked_runs(conn, site)
    current_events = {"idle", "stock"} | resource_notices.RESOURCE_EVENTS
    settings = storage.get_settings(conn) if any(item["event_type"] in current_events for item in site) else {}
    instances_by_project: dict[str, dict] = {}
    watches_by_project: dict[str, dict[str, str]] = {}
    now = storage.now()
    for item in site:
        item["business"] = _site_business(conn, item, linked_runs, instances_by_project, watches_by_project, settings, now)
    for item in items:
        item.pop("idle_outcome")
        item.pop("idle_observed_at")
    return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}


def _linked_runs(conn: Connection, items: list[dict]) -> dict[str, dict]:
    run_ids = {
        run_id for item in items if item["event_type"] not in {"idle", "test"}
        and isinstance(run_id := item["payload"].get("run_id"), str) and run_id
    }
    if not run_ids:
        return {}
    return {
        row["id"]: run_with_business(row) for row in conn.execute(
            f"""SELECT r.*, {RUN_BUSINESS_COLUMNS} FROM task_runs r
                LEFT JOIN tasks t ON t.id=r.task_id AND t.project_id=r.project_id
                {RUN_LINKED_SHUTDOWN_JOIN}
                WHERE r.id IN ({','.join('?' for _ in run_ids)})""",
            tuple(run_ids),
        )
    }


def _site_business(
    conn: Connection, item: dict, runs: dict[str, dict], instances_by_project: dict[str, dict],
    watches_by_project: dict[str, dict[str, str]], settings: dict, now: int,
) -> dict:
    payload, project_id = item["payload"], item["project_id"]
    history = {
        "status": "history", "label": "历史记录", "attention": False,
        "detail": None, "observed_at": None, "destination": None,
    }
    if item["event_type"] == "test":
        return history | {"label": "测试通知", "observed_at": item["delivered_at"]}
    if item["event_type"] == "rental-window":
        watch_id = payload.get("watch_id")
        watch = conn.execute("SELECT 1 FROM watches WHERE id=? AND project_id=?", (watch_id, project_id)).fetchone()
        destination = {"page": "inventory", "project_id": project_id, "watch_id": watch_id} if watch else None
        return history | {"label": "租卡时段提醒", "observed_at": item["created_at"], "destination": destination}
    if item["event_type"] == "idle":
        return history | _idle_business(conn, item, instances_by_project, settings, now)
    if item["event_type"] in resource_notices.RESOURCE_EVENTS:
        return history | resource_notices.business(conn, item, now, settings)
    run_id = payload.get("run_id")
    run = runs.get(run_id) if isinstance(run_id, str) else None
    if run is not None and run["project_id"] == project_id:
        if item["event_type"] == "stock":
            return run["business"] | _stock_business(conn, run, watches_by_project, now)
        return run["business"]
    return history


def _idle_business(
    conn: Connection, item: dict, instances_by_project: dict[str, dict], settings: dict, now: int,
) -> dict:
    project_id, payload = item["project_id"], item["payload"]
    instance_id = payload.get("instance_id")
    if not project_id or not isinstance(instance_id, str) or not instance_id:
        return {}
    if project_id not in instances_by_project:
        snapshot = storage.snapshot(conn, "instances", project_id)
        instances_by_project[project_id] = snapshot | {"instances": {
            instance["id"]: instance for instance in snapshot["data"]
            if instance.get("project_id", project_id) == project_id and isinstance(instance.get("id"), str)
        }}
    snapshot = instances_by_project[project_id]
    instance = snapshot["instances"].get(instance_id)
    destination = {"page": "instances", "project_id": project_id, "instance_id": instance_id} if instance else None
    evidence = {
        "recovered": ("利用率已回升", "已读到同一次开机内达到原提醒阈值的全卡完整样本"),
        "stopped": ("实例已关机", "已从原项目的实例清单确认关机"),
        "new_boot": ("实例已重新启动", "已确认当前是另一轮开机，原空闲记录已结束"),
    }.get(item["idle_outcome"])
    if evidence:
        return {
            "status": "resolved", "label": evidence[0], "detail": evidence[1],
            "observed_at": item["idle_observed_at"], "destination": destination,
        }
    context = notice_context(item["event_id"], payload)
    if not instance:
        return {} if context is None else {
            "status": "unknown", "label": "待核对实例状态",
            "detail": "当前实例清单无法确认原实例的状态。",
        }
    latest = snapshot["success_at"]
    if (
        context and context.threshold == settings["idle_threshold"] and instance.get("state") == "Running"
        and instance.get("start_time") == context.started_at and not snapshot["error"]
        and context.gpu_count in (None, instance.get("gpu"))
        and latest is not None and 0 <= now - latest <= SAMPLE_FRESHNESS
    ):
        sampled_at = active_sample(conn, project_id, instance_id, context, now)
        if sampled_at is not None:
            permitted = idle_permitted(conn, project_id, instance_id, now, sampled_at, settings)
            return {
                "status": "needs_attention" if permitted else "active",
                "label": "仍处于原空闲时段" if permitted else "仍空闲 · 提醒暂停", "attention": permitted,
                "detail": "同一次开机的最新完整采样仍低于原提醒阈值",
                "observed_at": sampled_at, "destination": destination,
            }
    return {
        "status": "unknown", "label": "待核对当前利用率", "destination": destination,
        "detail": "当前缺少可核对的连续完整采样，请查看实例监控。",
    }


def _watch_spec(spec: object) -> dict | None:
    if not isinstance(spec, dict):
        return None
    location = {"region": spec.get("region"), "zone": spec.get("zone")}
    try:
        validated = validate_spec(spec | {"zones": [location]}, purchasing=False, pool_only=True)
        validated.pop("stock_pool")
    except ValueError:
        return None
    return validated.pop("zones")[0] | validated


def _winning_spec(run: dict) -> dict | None:
    result, config = run["result"], run["snapshot"].get("config", {})
    zones = [zone for zone in config.get("zones", []) if isinstance(zone, dict) and "zone" in zone and "region" in zone]
    reported = result.get("spec")
    if isinstance(reported, dict):
        if reported.get("project_id", run["project_id"]) != run["project_id"]:
            return None
        region, zone = reported.get("region"), reported.get("zone")
        if (region != result.get("region", region) or zone != result.get("zone", zone)
            or {"region": region, "zone": zone} not in zones):
            return None
        return _watch_spec(reported)
    winning_zones = [zone for zone in zones if zone["zone"] == result.get("zone")
                     and zone["region"] == result.get("region", zone["region"])]
    return _watch_spec(config | winning_zones[0]) if len(winning_zones) == 1 else None


def _stock_business(
    conn: Connection, run: dict, watches_by_project: dict[str, dict[str, str]], now: int,
) -> dict:
    spec = _winning_spec(run)
    if spec is None:
        return {}
    project_id = run["project_id"]
    destination = {"page": "inventory", "project_id": project_id}
    key = watches.catalog_spec_id(spec)
    if project_id not in watches_by_project:
        watches_by_project[project_id] = {
            row["catalog_spec_id"]: row["watch_id"] for row in conn.execute(
                "SELECT catalog_spec_id,watch_id FROM catalog_watch_bindings WHERE project_id=?", (project_id,)
            )
        }
    watch_id = watches_by_project[project_id].get(key)
    destination.update({"watch_id": watch_id} if watch_id else {"spec": spec})
    combos = watches.current_catalog(conn)
    if key not in combos:
        return {"status": "unknown", "label": "待核对平台目录规格", "destination": destination}
    stock = watches.spec_stock(spec, watches.pool_rows(conn, now), capacity.profiles(conn, project_id, combos), now)
    observed_at = stock["capacity_checked_at"]
    return {
        "status": "unknown" if stock["available"] is None else "completed",
        "label": {None: "规格可租状态待核对", True: "规格当前可租", False: "规格当前不可租"}[stock["available"]],
        "observed_at": observed_at, "destination": destination,
    }
