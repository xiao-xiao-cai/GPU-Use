"""Read inventory history and enqueue actionable rental-window reminders."""

import json
from collections import Counter, defaultdict
from datetime import datetime
from fractions import Fraction
from sqlite3 import Connection, Row
from typing import Literal

from gpu_console import storage
from gpu_console.application import watches
from gpu_console.application.task_config import SHANGHAI
from gpu_console.storage import availability, availability_rollups, capacity, catalog

HOUR_SECONDS = 3600
LEAD_SECONDS = 1800
CHANNELS = {"site", "feishu", "email"}
MODES = {"off", "advance", "when_available"}
POOL_BY_INSTANCE_TYPE = catalog.POOL_BY_INSTANCE_TYPE
INSTANCE_TYPE_BY_POOL = {pool: kind for kind, pool in POOL_BY_INSTANCE_TYPE.items()}
CardThreshold = Literal[1, 2, 4, 8]


def _hour_start(timestamp: int) -> int:
    return timestamp // HOUR_SECONDS * HOUR_SECONDS


def _expected_hours(since: int, now: int) -> dict[int, int]:
    slot = availability.SLOT_SECONDS
    first, last = availability.slot_start(since), availability.slot_start(now)
    return {
        hour: (min(hour + HOUR_SECONDS - slot, last) - max(hour, first)) // slot + 1
        for hour in range(_hour_start(first), _hour_start(last) + HOUR_SECONDS, HOUR_SECONDS)
    }


def _hourly_counts(conn: Connection, source: str, key: str | None, since: int, now: int) -> list[dict]:
    rows = availability.sample_counts(conn, source, key, since, now, hourly=True) if key else []
    counts = {row["start"]: row for row in rows}
    return [
        {
            "start": start,
            "available_count": counts[start]["available_count"] if start in counts else 0,
            "known_count": counts[start]["known_count"] if start in counts else 0,
            "expected_count": slots,
        }
        for start, slots in _expected_hours(since, now).items()
    ]


def _rank(hours: list[dict]) -> tuple[str, list[dict], list[dict]]:
    by_hour: dict[int, list[Fraction]] = defaultdict(list)
    for row in hours:
        if row["known_count"] >= 8:
            hour = datetime.fromtimestamp(row["start"], SHANGHAI).hour
            by_hour[hour].append(Fraction(row["available_count"], row["known_count"]))
    scores = [(hour, sum(rates) / len(rates), len(rates)) for hour, rates in by_hour.items() if len(rates) >= 3]
    if len(scores) < 2:
        return "insufficient", [], []
    if len({score for _, score, _ in scores}) == 1:
        return "stable", [], []

    recommended = sorted(
        (row for row in scores if row[1] >= Fraction(7, 10)), key=lambda row: (-row[1], -row[2], row[0]),
    )
    tight = sorted(
        (row for row in scores if row[1] <= Fraction(3, 10)), key=lambda row: (row[1], -row[2], row[0]),
    )
    return "ready", _public_rank(recommended), _public_rank(tight)


def _public_rank(rows: list[tuple[int, Fraction, int]]) -> list[dict]:
    return [{"hour": hour, "rate": float(rate), "days": days} for hour, rate, days in rows[:3]]


def _watch_item(row: Row) -> dict:
    spec = json.loads(row["spec"])
    return {
        "key": row["id"], "name": row["name"], "region": spec.get("region", ""), "zone": spec.get("zone", ""),
        "gpu_model": spec.get("gpu_model", ""), "instance_type": "spot" if spec.get("charge_type") == "Spot" else "uhost",
        "watch_id": row["id"], "spec": spec,
    }


def _catalog_item(row: Row) -> dict:
    return {
        "key": row["key"], "name": row["gpu_model"], "region": row["region"], "zone": row["zone"],
        "gpu_model": row["gpu_model"], "instance_type": row["instance_type"], "watch_id": None, "spec": None,
    }


def _fresh(available: bool | None, observed_at: int | None, now: int, interval: int) -> bool | None:
    return available if observed_at is not None and 0 <= now - observed_at <= 2 * interval else None


def _stock_references(rows: list[dict]) -> dict[tuple, dict]:
    return {tuple(row[field] for field in ("region", "zone", "gpu_model", "pool")): row for row in rows}


def _stock_reference(references: dict, key: tuple, stock: dict) -> dict | None:
    row = references.get(key)
    if row and stock["state"] == "known" and row["provider_updated_at"] == stock["provider_updated_at"]:
        return row["reference"]
    return None


def _pool_rows(conn: Connection, now: int, references: dict) -> list[dict]:
    fields = availability.POOL_FIELDS
    rows = {tuple(row[field] for field in fields): row for row in availability.pool_inventory(conn, now)}
    for identity in availability.catalog_pools(storage.snapshot(conn, "catalog")["data"]):
        rows.setdefault(identity, dict(zip(fields, identity)) | {
            "remaining_cards": None, "state": "unknown", "provider_updated_at": None, "observed_at": None,
        })
    return [rows[key] | {"reference": _stock_reference(references, key, rows[key])}
            for key in sorted(rows, key=lambda item: (item[2], item[0], item[1], item[3]))]


def pool_snapshot(conn: Connection, now: int) -> dict:
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN DEFERRED")
    try:
        snapshot = storage.snapshot(conn, "pool")
        return snapshot | {"data": _pool_rows(conn, now, _stock_references(snapshot["data"]))}
    finally:
        if owns_snapshot:
            conn.rollback()


def _median_cards(counts: Counter[int], size: int) -> int | float | None:
    cumulative = 0
    lower = None
    for cards, count in sorted(counts.items()):
        cumulative += count
        if lower is None and cumulative > (size - 1) // 2:
            lower = cards
        if cumulative > size // 2:
            return cards if size % 2 or lower is None else (lower + cards) / 2
    return None


def _capacity_counts(
    conn: Connection, key: str | None, since: int, now: int, *, hourly: bool,
) -> tuple[dict, list[dict]]:
    expected = _expected_hours(since, now)
    if hourly:
        hours = _hourly_counts(conn, "capacity", key, since, now)
        return ({name: sum(row[name] for row in hours)
                 for name in ("available_count", "known_count", "expected_count")}, hours)
    totals = (availability.sample_counts(conn, "capacity", key, since, now, hourly=False)[0]
              if key else {"available_count": 0, "known_count": 0})
    return totals | {"expected_count": sum(expected.values())}, []


def _median_rollup(rows: list[Row], hours: list[dict]) -> int | float | None:
    total: Counter[int] = Counter()
    by_hour: dict[int, Counter[int]] = defaultdict(Counter)
    for row in rows:
        total[row["value"]] += row["count"]
        if hours:
            by_hour[row["start"]][row["value"]] += row["count"]
    for hour in hours:
        counts = by_hour.get(hour["start"], Counter())
        hour["median_cards"] = _median_cards(counts, sum(counts.values()))
    return _median_cards(total, sum(total.values()))


def _pool_query(
    conn: Connection, now: int, since: int, key: str | None, filters: dict,
    limit: int, offset: int, card_threshold: CardThreshold, project_id: str | None,
) -> dict:
    combos = watches.current_catalog(conn)
    matching_scopes = {capacity.scope(spec) for spec in combos.values() if spec["gpu"] == card_threshold}
    matching = []
    for row in pool_snapshot(conn, now)["data"]:
        identity = (row["region"], row["zone"], row["gpu_model"], row["pool"])
        instance_type = INSTANCE_TYPE_BY_POOL[row["pool"]]
        if (*identity[:3], instance_type) not in matching_scopes:
            continue
        item = row | {
            "key": "pool-" + json.dumps(identity, ensure_ascii=False, separators=(",", ":")),
            "instance_type": instance_type,
        }
        if (key is None or key == item["key"]) and all(
            value is None or item[field] == value for field, value in filters.items()
        ):
            matching.append(item)
    if key is not None and not matching:
        raise KeyError(f"未找到符合 {card_threshold} 卡条件的目录规格")
    project_id = capacity.read_project(conn, project_id)
    profiles = capacity.profiles(conn, project_id, combos)
    items = []
    detail = None
    for row in matching[:1] if key is not None else matching[offset:offset + limit]:
        pool_key = row["region"], row["zone"], row["gpu_model"], row["pool"]
        profile = profiles.get((*pool_key[:3], row["instance_type"]))
        evidence_key = capacity.card_key(card_threshold)
        counts, hours = _capacity_counts(conn, capacity.series(profile, evidence_key), since, now, hourly=key is not None)
        distribution = availability_rollups.pool_distribution(conn, *pool_key, since, now, hourly=key is not None)
        available, checked_at = capacity.current(profile, evidence_key, now)
        item = row | counts | {
            "name": row["gpu_model"], "watch_id": None, "spec": None,
            "available": available, "capacity_checked_at": checked_at, "median_cards": _median_rollup(distribution, hours),
            "capacity_error": profile["error"] if profile else None,
        }
        items.append(item)
        if key is not None:
            status, recommended, tight = _rank(hours)
            detail = {"item": item, "hours": hours, "recommended": recommended, "tight": tight,
                      "recommendation_status": status, "reminder": None}
    return {"as_of": now, "since": since, "source": "pool", "card_threshold": card_threshold,
            "items": items, "total": len(matching), "detail": detail, "project_id": project_id}


def _watch_series(stock: dict, profiles: dict) -> str | None:
    if stock["binding_status"] != "bound":
        return None
    return capacity.series(profiles.get(capacity.scope(stock["spec"])), stock["catalog_spec_id"])


def _watch_query(conn: Connection, project_id: str, now: int, key: str | None, filters: dict,
                 limit: int, offset: int) -> dict:
    since = now - availability.WINDOW_SECONDS
    combos, pools = watches.current_catalog(conn), watches.pool_rows(conn, now)
    profiles = capacity.profiles(conn, project_id, combos)
    references = _stock_references(storage.snapshot(conn, "pool")["data"])
    rows, total = availability.list_watches(conn, filters, project_id=project_id, key=key,
                                            limit=1 if key else limit, offset=0 if key else offset)
    if key is not None and not rows:
        raise KeyError("未找到对应的关注项")
    items, detail = [], None
    for row in rows:
        stock = watches.view(conn, row, now, combos=combos, pools=pools, profiles=profiles)
        counts, hours = _capacity_counts(conn, _watch_series(stock, profiles), since, now, hourly=key is not None)
        reference = (_stock_reference(references, watches.pool_key(stock["spec"]), stock)
                     if stock["binding_status"] == "bound" else None)
        item = _watch_item(row) | stock | {"observed_at": stock["provider_updated_at"], "reference": reference} | counts
        items.append(item)
        if key is not None:
            status, recommended, tight = _rank(hours)
            detail = {"item": item, "hours": hours, "recommended": recommended, "tight": tight,
                      "recommendation_status": status, "reminder": availability.reminder(conn, row["id"])}
    return {"as_of": now, "since": since, "source": "watch", "items": items, "total": total, "detail": detail}


def query(
    conn: Connection, *, source: str, now: int, project_id: str | None = None, key: str | None = None,
    gpu_model: str | None = None, region: str | None = None, zone: str | None = None,
    instance_type: str | None = None, limit: int = 50, offset: int = 0, card_threshold: CardThreshold = 1,
) -> dict:
    if source not in {"catalog", "watch", "pool"}:
        raise ValueError("库存数据来源无效")
    if source == "watch" and not project_id:
        raise ValueError("请为关注规格选择项目")
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("库存分页范围无效")
    if card_threshold not in (1, 2, 4, 8):
        raise ValueError("库存卡数须为 1、2、4 或 8 张")

    filters = {"region": region, "zone": zone, "gpu_model": gpu_model, "instance_type": instance_type}
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN DEFERRED")
    try:
        if source == "pool":
            return _pool_query(conn, now, now - availability.WINDOW_SECONDS, key, filters, limit, offset, card_threshold, project_id)
        if source == "watch":
            return _watch_query(conn, project_id, now, key, filters, limit, offset)
        return _query_catalog(conn, now, key, filters, limit, offset)
    finally:
        if owns_snapshot:
            conn.rollback()


def _query_catalog(conn: Connection, now: int, key: str | None, filters: dict, limit: int, offset: int) -> dict:
    since = now - availability.WINDOW_SECONDS
    expected_count = sum(_expected_hours(since, now).values())
    rows, total = availability.list_catalog(conn, filters, since=since, now=now, key=key,
                                            limit=1 if key is not None else limit, offset=0 if key is not None else offset)
    latest = availability.latest_catalog_round(conn, now)
    interval = storage.get_settings(conn)["catalog_seconds"]
    items = []
    for row in rows:
        state, observed_at = availability.latest_catalog(conn, row["key"], latest)
        counts = availability.sample_counts(conn, "catalog", row["key"], since, now, hourly=False)[0]
        items.append(_catalog_item(row) | dict(counts) | {
            "available": _fresh(state, observed_at, now, interval), "observed_at": observed_at,
            "expected_count": expected_count,
        })
    detail = None
    if key is not None:
        if not items:
            raise KeyError("未找到对应的目录状态记录")
        detail = {"item": items[0], "hours": _hourly_counts(conn, "catalog", key, since, now),
                  "recommended": [], "tight": [], "recommendation_status": "insufficient", "reminder": None}
    return {"as_of": now, "since": since, "source": "catalog", "items": items, "total": total, "detail": detail}


def set_reminder(
    conn: Connection, *, project_id: str, watch_id: str, mode: str, channels: list[str],
) -> dict:
    if mode not in MODES or not CHANNELS.issuperset(channels) or (mode != "off" and not channels):
        raise ValueError("请检查提醒模式或通知渠道")
    watch = conn.execute("SELECT * FROM watches WHERE project_id=? AND id=?", (project_id, watch_id)).fetchone()
    if not watch:
        raise KeyError("该项目下没有此关注规格")
    if mode != "off" and watches.view(conn, watch, storage.now())["binding_status"] != "bound":
        raise ValueError("请先绑定平台目录规格")
    channels = list(dict.fromkeys(channels)) if mode != "off" else ["site"]
    availability.save_reminder(conn, watch_id, mode, channels)
    return {"mode": mode, "channels": channels}


def reminder_tick(conn: Connection, now: int) -> None:
    settings = storage.get_settings(conn)
    if settings["restore_gate"]:
        return
    rows = conn.execute(
        """SELECT w.id,w.project_id,w.name,w.spec,r.mode,r.channels
           FROM availability_reminders r JOIN watches w ON w.id=r.watch_id ORDER BY w.id""",
    )
    combos, pools = watches.current_catalog(conn), watches.pool_rows(conn, now)
    by_project = {}
    for row in rows:
        window = _hour_start(now)
        if row["mode"] == "advance":
            window += HOUR_SECONDS
            if now < window - LEAD_SECONDS:
                continue
        watch_id = row["id"]
        local = datetime.fromtimestamp(window, SHANGHAI)
        event_id = f"rental-window:{row['project_id']}:{watch_id}:{local.date()}"
        if conn.execute("SELECT 1 FROM notifications WHERE event_id=? LIMIT 1", (event_id,)).fetchone():
            continue
        if row["project_id"] not in by_project:
            by_project[row["project_id"]] = capacity.profiles(conn, row["project_id"], combos)
        profiles = by_project[row["project_id"]]
        stock = watches.view(conn, row, now, combos=combos, pools=pools, profiles=profiles)
        if stock["binding_status"] != "bound":
            continue
        available, observed_at = stock["available"], stock["capacity_checked_at"]
        if row["mode"] == "when_available" and (available is not True or observed_at < window):
            continue
        since = now - availability.WINDOW_SECONDS
        _, hours = _capacity_counts(conn, _watch_series(stock, profiles), since, now, hourly=True)
        _, recommended, _ = _rank(hours)
        choice = next((item for item in recommended if item["hour"] == local.hour), None)
        if choice is not None:
            _emit_reminder(conn, row, event_id, window, choice, available, observed_at, settings)


def _emit_reminder(
    conn: Connection, row: Row, event_id: str, start: int, choice: dict, available: bool | None,
    observed_at: int | None, settings: dict,
) -> None:
    end = start + HOUR_SECONDS
    begins = datetime.fromtimestamp(start, SHANGHAI).strftime("%m-%d %H:%M")
    ends = datetime.fromtimestamp(end, SHANGHAI).strftime("%H:%M")
    state = {True: "当前规格可租", False: "当前规格不可租", None: "当前规格待核对"}[available]
    seen = "" if observed_at is None else (
        f"（规格探测 {datetime.fromtimestamp(observed_at, SHANGHAI):%m-%d %H:%M}）"
    )
    spec = json.loads(row["spec"])
    channels = json.loads(row["channels"])
    recipient = settings["notification_rules"]["email_recipient"]
    if not recipient:
        channels = [channel for channel in channels if channel != "email"]
    if not channels:
        return
    payload = {
        "title": "租卡时段提醒",
        "message": (
            f"「{row['name']}」{begins} 至 {ends}（北京时间）历史规格可租率 "
            f"{choice['rate']:.0%}（{choice['days']} 个有效日期）。{state}{seen}。"
        ),
        "project_id": row["project_id"], "watch_id": row["id"], "gpu_model": spec.get("gpu_model"),
        "zone": spec.get("zone"), "mode": row["mode"], "window_start": start, "window_end": end,
        "confirmed_at": observed_at, "catalog_spec_id": watches.catalog_spec_id(spec),
    }
    if "email" in channels:
        payload["recipient"] = recipient
    storage.emit(conn, event_id, payload, channels)


def cancel_restored(conn: Connection) -> None:
    conn.execute("UPDATE catalog_watch_bindings SET baseline_at=NULL,baseline_available=NULL")
    conn.execute(
        """UPDATE notifications SET state='skipped',error='数据库恢复后已取消历史提醒'
           WHERE (event_id GLOB 'rental-window:*' OR event_id GLOB 'watch-stock:*') AND state='pending'""",
    )


def delivery_hold(conn: Connection, row: Row, payload: dict, now: int) -> tuple[str, str] | None:
    rental = row["event_id"].startswith("rental-window:")
    if not rental and not row["event_id"].startswith("watch-stock:"):
        return None
    watch = conn.execute("SELECT * FROM watches WHERE id=? AND project_id=?",
                         (payload.get("watch_id"), payload.get("project_id"))).fetchone()
    if watch is None:
        return "skipped", "关注项已删除"
    stock = watches.view(conn, watch, now)
    if stock["binding_status"] != "bound" or payload.get("catalog_spec_id") != stock["catalog_spec_id"]:
        return "skipped", "关注规格已变更"
    settings = storage.get_settings(conn)
    if settings["restore_gate"]:
        return "skipped", "数据库恢复后已取消历史提醒"
    if not rental:
        rules = settings["notification_rules"]
        rule = rules["watch_stock"]
        if not rule["enabled"] or row["channel"] not in rule["channels"]:
            return "skipped", "此渠道已取消该类提醒"
        if row["channel"] == "email" and payload.get("recipient") != rules["email_recipient"]:
            return "skipped", "邮件收件地址已更改"
        return None if stock["available"] is True else ("skipped", "发送时没有探测到规格可租")
    if now >= (payload["window_start"] if payload["mode"] == "advance" else payload["window_end"]):
        return "skipped", "推荐时段提醒已过期"
    watch_id = payload["watch_id"]
    current = availability.reminder(conn, watch_id)
    if current["mode"] != payload["mode"] or row["channel"] not in current["channels"]:
        return "skipped", "关注规格已删除或提醒已取消"
    if row["channel"] == "email" and (
        not payload.get("recipient") or payload["recipient"] != settings["notification_rules"]["email_recipient"]
    ):
        return "skipped", "邮件收件地址已更改"
    if payload["mode"] == "when_available":
        available, observed_at = stock["available"], stock["capacity_checked_at"]
        if available is not True or observed_at < payload["window_start"]:
            return "pending", "等待推荐时段内的最新有货结果"
    return None
