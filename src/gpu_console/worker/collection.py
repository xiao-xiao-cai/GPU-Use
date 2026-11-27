"""Collect complete snapshots and evaluate fresh, per-card idle samples."""

import json
from collections import defaultdict
from collections.abc import Callable
from itertools import batched
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from gpu_console import storage
from gpu_console.adapters.compshare import PAGE_SIZE, CloudError, CompShare, GpuInventory
from gpu_console.application import automation_rules, idle_notices, notice_policies, resource_notices, watches
from gpu_console.application.idle_notices import MAX_SAMPLE_GAP, SAMPLE_FRESHNESS, complete_cards
from gpu_console.storage import (
    availability, availability_rollups, cardless, dashboard_cost, dashboard_usage, instance_assets, instance_history, refresh,
)
from gpu_console.worker.shutdown import settle_early

GPU_UTIL = "cloudwatch_gpu_util"


def initialize(conn: Connection) -> None:
    notice_policies.initialize(conn)
    resource_notices.initialize(conn)
    idle_notices.initialize(conn)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS idle_state (
               project_id TEXT NOT NULL, instance_id TEXT NOT NULL, started_at INTEGER NOT NULL,
               last_sample_at INTEGER NOT NULL, first_idle_at INTEGER, labels TEXT,
               notified INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(project_id,instance_id)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS idle_episodes (
               project_id TEXT NOT NULL, instance_id TEXT NOT NULL, started_at INTEGER NOT NULL,
               episode_at INTEGER, threshold REAL NOT NULL, warmup_minutes INTEGER NOT NULL,
               PRIMARY KEY(project_id,instance_id), FOREIGN KEY(project_id,instance_id)
                   REFERENCES idle_state(project_id,instance_id) ON DELETE CASCADE
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS idle_notification_runs (
               project_id TEXT NOT NULL, instance_id TEXT NOT NULL, start_time INTEGER NOT NULL,
               run_id TEXT NOT NULL REFERENCES task_runs(id), PRIMARY KEY(project_id,instance_id)
           )"""
    )


def _save_snapshot(
    conn: Connection, kind: str, scope: str, data: Any, timestamp: int, error: str | None,
    through_id: int = 0,
) -> None:
    conn.execute(
        """INSERT INTO snapshots(kind,scope,data,success_at,error) VALUES (?,?,?,?,?)
           ON CONFLICT(kind,scope) DO UPDATE SET
               data=CASE WHEN ? THEN excluded.data ELSE snapshots.data END,
               success_at=COALESCE(excluded.success_at,snapshots.success_at),
               error=excluded.error""",
        (
            kind, scope, json.dumps([] if data is None else data, ensure_ascii=False),
            timestamp if error is None else None, error,
            data is not None,
        ),
    )
    conn.execute("DELETE FROM refresh_requests WHERE kind=? AND scope=?", (kind, scope))
    refresh.finish(conn, kind, scope, through_id, timestamp, error)
    storage.record_status(conn, kind, error is None, error)


def _pool_zone_map(locations: list[dict]) -> dict[str, dict[int, str]]:
    regional = {row["region"]: {} for row in locations}
    by_id: dict[int, set[tuple[str, str]]] = defaultdict(set)
    by_location: dict[tuple[str, str], set[int]] = defaultdict(set)
    for row in locations:
        zone_id = row.get("zone_id")
        if type(zone_id) is int and zone_id > 0:
            location = row["region"], row["zone"]
            by_id[zone_id].add(location)
            by_location[location].add(zone_id)
    for location, ids in by_location.items():
        if len(ids) == 1:
            zone_id = next(iter(ids))
            if len(by_id[zone_id]) == 1:
                region, zone = location
                regional[region][zone_id] = zone
    return regional


def _card_readings(
    row: dict, points: list[dict], timestamp: int,
) -> dict[int, dict[str, float]]:
    if row.get("state") != "Running" or not row.get("start_time") or not row.get("gpu") or row.get("gpu_mode") == "without_gpu":
        return {}
    samples: dict[int, dict[str, float]] = defaultdict(dict)
    for point in points:
        sampled_at = point["sampled_at"]
        if (point["metric"] != GPU_UTIL or point["project_id"] != row["project_id"]
            or point["instance_id"] != row["id"] or not row["start_time"] <= sampled_at <= timestamp):
            continue
        label, value = point["label"], point["value"]
        samples[sampled_at][label] = value
    if samples:
        latest = max(samples)
        cards = samples[latest]
        if timestamp - latest <= SAMPLE_FRESHNESS and complete_cards(cards, row["gpu"]):
            row["gpu_mode"] = "with_gpu"
    return samples


def _idle_event(
    conn: Connection, row: dict, first_idle_at: int, sampled_at: int, settings: dict,
) -> None:
    project_id = row["project_id"]
    identity = row["id"]
    if not notice_policies.idle_permitted(conn, project_id, identity, storage.now(), sampled_at, settings):
        return
    channels, context = idle_notices.subscriptions(conn, project_id, identity, row["start_time"], settings)
    message = (
        f"{row['name']}（{identity}）的 {row['gpu_model'] or 'GPU'} × {row['gpu']} "
        f"在 {row['zone']} 的全部卡利用率已连续 {settings['idle_minutes']} 分钟低于 "
        f"{settings['idle_threshold']}%。"
    )
    notice_policies.emit_recurring(
        conn, f"idle:{project_id}:{identity}:{row['start_time']}:{first_idle_at}",
        {
            "title": "GPU 持续空闲", "message": message, "project_id": project_id,
            "instance_id": identity, "gpu_model": row["gpu_model"], "zone": row["zone"],
            "confirmed_at": sampled_at, "idle_threshold": settings["idle_threshold"], "gpu_count": row["gpu"],
            **context,
        },
        channels, settings["notification_repeat_minutes"],
    )


def _record_idle(
    conn: Connection, row: dict, samples: dict[int, dict[str, float]], timestamp: int, settings: dict,
) -> None:
    scope = (row["project_id"], row["id"])
    started_at = row.get("start_time")
    if row.get("state") != "Running" or not started_at or not row.get("gpu"):
        conn.execute("DELETE FROM idle_state WHERE project_id=? AND instance_id=?", scope)
        return
    saved = conn.execute(
        """SELECT s.*,e.episode_at,e.threshold,e.warmup_minutes FROM idle_state s LEFT JOIN idle_episodes e
               ON e.project_id=s.project_id AND e.instance_id=s.instance_id AND e.started_at=s.started_at
           WHERE s.project_id=? AND s.instance_id=? AND s.started_at=?""",
        (*scope, started_at),
    ).fetchone()
    threshold, warmup = settings["idle_threshold"], settings["idle_warmup_minutes"]
    warmup_at = started_at + warmup * 60
    first_idle = saved["first_idle_at"] if saved else None
    last_sample = saved["last_sample_at"] if saved else warmup_at - 1
    labels = saved["labels"] if saved else None
    episode_at = saved["episode_at"] if saved else None
    if saved and saved["threshold"] is None and saved["notified"]:
        episode_at = _legacy_idle_episode(conn, scope, started_at, first_idle)
    elif saved and saved["threshold"] is not None and (saved["threshold"], saved["warmup_minutes"]) != (threshold, warmup):
        first_idle = episode_at = None
    for sampled_at, cards in sorted(samples.items()):
        if sampled_at <= last_sample or sampled_at < warmup_at:
            continue
        continuous = sampled_at - last_sample <= MAX_SAMPLE_GAP
        last_sample = sampled_at
        if not complete_cards(cards, row["gpu"]):
            first_idle = None
            continue
        signature = json.dumps(sorted(cards))
        if not continuous or signature != labels:
            first_idle = None
        labels = signature
        if max(cards.values()) >= threshold:
            first_idle = episode_at = None
            continue
        first_idle = first_idle if first_idle is not None else sampled_at

    if row.get("gpu_mode") != "with_gpu":
        first_idle = None
        last_sample = max(last_sample, timestamp - SAMPLE_FRESHNESS)
    elif first_idle is not None and last_sample - first_idle >= settings["idle_minutes"] * 60:
        episode_at = episode_at if episode_at is not None else first_idle
        _idle_event(conn, row, episode_at, last_sample, settings)

    conn.execute(
        """INSERT INTO idle_state VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(project_id,instance_id) DO UPDATE SET
               started_at=excluded.started_at,last_sample_at=excluded.last_sample_at,
               first_idle_at=excluded.first_idle_at,labels=excluded.labels,notified=excluded.notified""",
        (*scope, started_at, last_sample, first_idle, labels, episode_at is not None),
    )
    conn.execute(
        """INSERT INTO idle_episodes VALUES (?,?,?,?,?,?) ON CONFLICT(project_id,instance_id) DO UPDATE SET
               started_at=excluded.started_at,episode_at=excluded.episode_at,threshold=excluded.threshold,
               warmup_minutes=excluded.warmup_minutes""",
        (*scope, started_at, episode_at, threshold, warmup),
    )


def _legacy_idle_episode(conn: Connection, scope: tuple[str, str], started_at: int, first_idle: int | None) -> int | None:
    prefix = f"idle:{scope[0]}:{scope[1]}:{started_at}:"
    row = conn.execute(
        """SELECT event_id,payload FROM notifications WHERE event_id GLOB 'idle:*'
           AND json_extract(payload,'$.project_id')=? AND substr(event_id,1,?)=? ORDER BY id DESC LIMIT 1""",
        (scope[0], len(prefix), prefix),
    ).fetchone()
    if row:
        context = idle_notices.notice_context(row["event_id"], json.loads(row["payload"]))
        if context:
            return context.first_idle_at
    return first_idle


class Collector:
    def __init__(self, path: Path, cloud: CompShare, *, read_only: bool = False):
        self.path = path
        self.cloud = cloud
        self.read_only = read_only
        self.last_poll: dict[tuple[str, str], int] = {}
        self.last_trim: int | None = None

    def tick(self) -> None:
        timestamp = storage.now()
        with storage.connect(self.path) as conn:
            settings = storage.get_settings(conn)
            projects = storage.snapshot(conn, "projects")["data"]
            pending = {(row["kind"], row["scope"]) for row in conn.execute("SELECT * FROM refresh_requests")}
            requests = refresh.pending(conn)
            pending.update(requests)
            rule_projects = conn.execute("SELECT DISTINCT project_id FROM automation_rules WHERE status='active'").fetchall()
        if self._due("projects", "", settings["catalog_seconds"], timestamp, pending):
            discovered = self._global_snapshot("projects", self.cloud.projects, requests.get(("projects", ""), 0))
            if discovered is not None:
                projects = discovered
        if self._due("catalog", "", settings["catalog_seconds"], timestamp, pending):
            self._catalog(requests.get(("catalog", ""), 0))
        for kind, scope in sorted(pending):
            if kind == "cfs":
                self._cfs(scope)

        pool_at = storage.now()
        pool_key = ("pool", "")
        pool_through_id = requests.get(pool_key, 0)
        previous_pool_at = self.last_poll.get(pool_key)
        if previous_pool_at is None or pool_at - previous_pool_at >= availability.SLOT_SECONDS:
            self._pool(pool_at, pool_through_id)
        elif pool_key in pending:
            self._finish_cached_pool_refresh(pool_at, pool_through_id)

        project_ids = {item["id"] for item in projects}
        project_ids.update(row["project_id"] for row in rule_projects)
        project_ids.update(scope for kind, scope in pending if kind == "instances")
        if settings["project_id"]:
            project_ids.add(settings["project_id"])
        for project_id in sorted(project_ids):
            if self._due("instances", project_id, settings["collection_seconds"], timestamp, pending):
                self._instances(project_id, settings, requests.get(("instances", project_id), 0))

        self._trim(timestamp, settings["retention_days"])

    def _due(
        self, kind: str, scope: str, interval: int, timestamp: int, pending: set[tuple[str, str]],
    ) -> bool:
        key = (kind, scope)
        return key in pending or key not in self.last_poll or timestamp - self.last_poll[key] >= interval

    def _global_snapshot(self, kind: str, query: Callable[[], list[dict]], through_id: int) -> list[dict] | None:
        try:
            rows, error = query(), None
        except CloudError as failure:
            rows, error = None, str(failure)
        timestamp = storage.now()
        with storage.connect(self.path) as conn, storage.transaction(conn):
            _save_snapshot(conn, kind, "", rows, timestamp, error, through_id)
            if kind == "catalog":
                availability.record_catalog(conn, rows, timestamp)
                if rows is not None:
                    availability.reconcile_pool_catalog(conn, availability.catalog_pools(rows))
                    watches.sync_bindings(conn)
        self.last_poll[(kind, "")] = timestamp
        return rows

    def _cfs(self, scope: str) -> None:
        project_id, region, zone = json.loads(scope)
        with storage.connect(self.path) as conn:
            locations = storage.snapshot(conn, "locations")["data"]
        location = next((row for row in locations if row["region"] == region and row["zone"] == zone), {})
        try:
            rows, error = self.cloud.describe_cfs({
                "project_id": project_id, "region": region, "zone": zone, "zone_id": location.get("zone_id"),
            }), None
        except CloudError as failure:
            rows, error = [], str(failure)
        timestamp = storage.now()
        with storage.connect(self.path) as conn, storage.transaction(conn):
            _save_snapshot(conn, "cfs", scope, {"checked_at": timestamp, "items": rows}, timestamp, error)

    def _catalog(self, through_id: int) -> None:
        try:
            zones, error = self.cloud.zones(), None
        except CloudError as failure:
            zones, error = None, str(failure)
        timestamp = storage.now()
        with storage.connect(self.path) as conn, storage.transaction(conn):
            _save_snapshot(conn, "locations", "", zones, timestamp, error)
            if zones is None:
                _save_snapshot(conn, "catalog", "", None, timestamp, error, through_id)
                availability.record_catalog(conn, None, timestamp)
        if zones is None:
            self.last_poll[("catalog", "")] = timestamp
        else:
            self._global_snapshot("catalog", lambda: self.cloud.catalog(zones=zones), through_id)

    def _pool(self, started_at: int, through_id: int) -> None:
        with storage.connect(self.path) as conn:
            locations = storage.snapshot(conn, "locations")["data"]
        mapping = _pool_zone_map(locations)
        outcomes: dict[str, tuple[int, GpuInventory | None]] = {}
        errors = []
        for region, zone_ids in sorted(mapping.items()):
            snapshot = None
            error = None
            if zone_ids:
                try:
                    snapshot = self.cloud.gpu_inventory(region, zone_ids)
                except CloudError as failure:
                    error = str(failure)
            else:
                error = "可用区数字 ID 待同步"
            observed_at = storage.now()
            if snapshot is not None:
                if snapshot.provider_updated_at > observed_at:
                    snapshot, error = None, "平台库存更新时间无效"
                elif observed_at - snapshot.provider_updated_at > availability.POOL_FRESHNESS:
                    error = "平台库存快照已过期"
            if error:
                errors.append(f"{region} {error}")
            outcomes[region] = (observed_at, snapshot)

        finished_at = storage.now()
        error = "、".join(errors) or (None if mapping else "可用区数字 ID 待同步")
        with storage.connect(self.path) as conn, storage.transaction(conn):
            directory = storage.snapshot(conn, "catalog")
            listed: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
            for region, zone, model, pool in availability.catalog_pools(directory["data"]):
                listed[region].add((zone, model, pool))
            if directory["success_at"] is None:
                error = error or "等待首次同步平台规格目录"
            old_regions = {row["region"] for row in availability.pool_inventory(conn, finished_at)}
            for region in old_regions - outcomes.keys():
                availability.record_pool(conn, region, finished_at)
            for region, (observed_at, snapshot) in outcomes.items():
                if snapshot is None:
                    availability.record_pool(conn, region, observed_at)
                else:
                    scopes = listed[region]
                    availability.record_pool(
                        conn, region, observed_at, provider_updated_at=snapshot.provider_updated_at,
                        counts={key: count for key, count in snapshot.counts.items() if key in scopes},
                        unsupported_spot={(zone, model) for zone, model, pool in scopes
                                          if pool == "spot" and model in snapshot.spot_unsupported},
                    )
            totals = {(region, *key): (snapshot.provider_updated_at, total)
                      for region, (_, snapshot) in outcomes.items() if snapshot is not None
                      for key, total in snapshot.total_counts.items()}
            references = availability_rollups.pool_references(conn, finished_at, totals)
            _save_snapshot(conn, "pool", "", references, finished_at, error, through_id)
        self.last_poll[("pool", "")] = started_at

    def _finish_cached_pool_refresh(self, timestamp: int, through_id: int) -> None:
        with storage.connect(self.path) as conn, storage.transaction(conn):
            error = storage.snapshot(conn, "pool")["error"]
            conn.execute("DELETE FROM refresh_requests WHERE kind='pool' AND scope=''")
            refresh.finish(conn, "pool", "", through_id, timestamp, error)

    def _instances(self, project_id: str, settings: dict, through_id: int) -> None:
        try:
            rows = self.cloud.instances(project_id)
        except CloudError as failure:
            timestamp = storage.now()
            error = str(failure)
            with storage.connect(self.path) as conn, storage.transaction(conn):
                _save_snapshot(conn, "instances", project_id, None, timestamp, error, through_id)
                _save_snapshot(conn, "metrics", project_id, None, timestamp, error)
                dashboard_usage.collection_failed(conn, project_id, timestamp)
                dashboard_cost.collection_failed(conn, project_id, timestamp)
                conn.execute("UPDATE idle_state SET first_idle_at=NULL WHERE project_id=?", (project_id,))
                automation_rules.collection_failed(conn, project_id, timestamp)
            self.last_poll[("instances", project_id)] = timestamp
            return

        regions: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            if row["region"] and row["state"] == "Running":
                regions[row["region"]].append(row["id"])
        points_by_id: dict[str, list[dict]] = defaultdict(list)
        monitor_error = None
        for region, ids in regions.items():
            for batch in batched(ids, 10):
                try:
                    for point in self.cloud.monitor(project_id, region, list(batch)):
                        points_by_id[point["instance_id"]].append(point)
                except CloudError as failure:
                    monitor_error = str(failure)

        for row in rows:
            if row.get("gpu_mode") == "without_gpu":
                row["instance_price_per_hour"] = None
        missing_prices = [row for row in rows if row.get("charge_type") in dashboard_cost.HOURLY
                          and (row.get("gpu") or row.get("gpu_mode") == "without_gpu")
                          and row.get("instance_price_per_hour") is None]
        for batch in batched(missing_prices, PAGE_SIZE):
            try:
                prices = self.cloud.instance_prices(project_id, list(batch))
            except CloudError as failure:
                if failure.code == "429":
                    break
                continue
            for row in batch:
                row.update({key: value for key, value in prices.get(row["id"], {}).items()
                            if value is not None and row.get(key) is None})

        timestamp = storage.now()
        cutoff = timestamp - settings["retention_days"] * 86400
        card_readings = {
            row["id"]: _card_readings(row, points_by_id[row["id"]], timestamp) for row in rows
        }
        with storage.connect(self.path) as conn, storage.transaction(conn):
            previous = storage.snapshot(conn, "instances", project_id)["data"]
            previous_by_id = {row["id"]: row for row in previous}
            registered = cardless.profiles(conn)
            for row in rows:
                iid = row["id"]
                previous_row = previous_by_id.get(iid, {})
                if row.get("gpu_mode") == "without_gpu":
                    profile = cardless.match_profile(row, cardless.options_for(row, registered)) or {}
                    account = (dashboard_cost._platform_rate(row.get("instance_price_per_hour"))
                               if profile else None)
                    standard = profile.get("standard_hourly_cny")
                    row.update(without_gpu_spec=profile.get("spec"),
                               cardless_hourly_cny=account if account is not None else standard,
                               cardless_rate_source="account" if account is not None else "official" if standard is not None else None)
                instance_assets.remember_gpu_boot_spec(row, previous_row)
                instance_assets.record_disk_usage(row, previous_row, points_by_id[iid], timestamp)
            conn.executemany(
                """INSERT INTO metrics(project_id,instance_id,metric,label,sampled_at,value)
                   VALUES (:project_id,:instance_id,:metric,:label,:sampled_at,:value)
                   ON CONFLICT(project_id,instance_id,metric,label,sampled_at) DO NOTHING""",
                (
                    point for points in points_by_id.values() for point in points
                    if cutoff <= point["sampled_at"] <= timestamp
                ),
            )
            idle_rows = conn.execute("SELECT instance_id FROM idle_state WHERE project_id=?", (project_id,))
            known_ids = {row["id"] for row in rows}
            conn.executemany(
                "DELETE FROM idle_state WHERE project_id=? AND instance_id=?",
                [(project_id, item["instance_id"]) for item in idle_rows if item["instance_id"] not in known_ids],
            )
            settings = storage.get_settings(conn)
            dashboard_usage.observe(conn, project_id, rows, card_readings, timestamp, settings["collection_seconds"])
            dashboard_cost.observe(conn, project_id, rows, card_readings, points_by_id, timestamp,
                                   settings["collection_seconds"])
            for row in rows:
                _record_idle(conn, row, card_readings[row["id"]], timestamp, settings)
                settle_early(conn, row, timestamp)
            automation_rules.observe(conn, project_id, rows, points_by_id, timestamp,
                                     enabled=not (self.read_only or settings["restore_gate"]))
            idle_notices.observe(
                conn, project_id, rows, card_readings, timestamp,
                lambda instance, context, sampled_at: resource_notices.emit_idle_recovery(
                    conn, instance, context, sampled_at, timestamp, settings,
                ),
            )
            resource_notices.observe_instances(conn, project_id, rows, points_by_id, card_readings, timestamp, settings)
            instance_history.observe(conn, project_id, previous, rows, timestamp)
            _save_snapshot(conn, "instances", project_id, rows, timestamp, None, through_id)
            _save_snapshot(conn, "metrics", project_id, [], timestamp, monitor_error)
        self.last_poll[("instances", project_id)] = timestamp

    def _trim(self, timestamp: int, retention_days: int) -> None:
        if self.last_trim is not None and timestamp - self.last_trim < 3600:
            return
        cutoff = timestamp - retention_days * 86400
        with storage.connect(self.path) as conn, storage.transaction(conn):
            metrics = conn.execute(
                "DELETE FROM metrics WHERE rowid IN (SELECT rowid FROM metrics WHERE sampled_at<? LIMIT 5000)",
                (cutoff,),
            ).rowcount
            stock = conn.execute(
                """DELETE FROM stock_history WHERE rowid IN
                   (SELECT rowid FROM stock_history WHERE sampled_at<? LIMIT 5000)""",
                (cutoff,),
            ).rowcount
            refresh.trim(conn, cutoff)
        if max(metrics, stock) < 5000:
            self.last_trim = timestamp
