"""Cached storage inventory, storage prices and full instance capacity probes."""

import json
from collections import Counter
from hashlib import sha256
from sqlite3 import Connection

from gpu_console import storage
from gpu_console.application.idle_notices import SAMPLE_FRESHNESS
from gpu_console.application import watches
from gpu_console.storage import catalog

SYNC_SECONDS = 300
PRICE_SECONDS = 86400
STANDARD_SECONDS = 7 * 86400
BILLING_UNITS = {"Postpay": "hour", "Dynamic": "hour", "Day": "day", "Month": "month", "Year": "year"}


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS instance_capacity_checks (
            scope TEXT PRIMARY KEY, checked_at INTEGER NOT NULL, available INTEGER
        );
        CREATE INDEX IF NOT EXISTS instance_capacity_checks_time ON instance_capacity_checks(checked_at);
        CREATE TABLE IF NOT EXISTS storage_prices (
            scope TEXT NOT NULL, source TEXT NOT NULL, amount TEXT, checked_at INTEGER NOT NULL,
            observed_at INTEGER, PRIMARY KEY(scope,source)
        );
    """)


def key(spec: dict) -> str:
    return sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def probe_spec(instance: dict) -> dict | None:
    fields = ("project_id", "region", "zone", "gpu_model", "charge_type", "image_id", "gpu", "cpu", "memory_gb")
    spec = {name: instance.get(name) for name in fields}
    if not all(spec.values()) or not instance.get("disks"):
        return None
    spec["cpu_platform"] = instance.get("cpu_platform") or "Auto"
    spec["disks"] = sorted(
        ({name: disk[name] for name in ("purpose", "type", "size_gb")} for disk in instance["disks"]),
        key=lambda disk: (disk["purpose"], disk["type"], disk["size_gb"]),
    )
    return spec


def stock_status(instance: dict, pools: dict, checks: dict) -> dict | None:
    if instance["state"] != "Stopped":
        return None
    needed = instance.get("gpu")
    if type(needed) is not int or needed <= 0:
        return None
    scope = {field: instance.get(field) for field in ("region", "zone", "gpu_model")}
    scope["instance_type"] = catalog.instance_type(instance["charge_type"]) if instance.get("charge_type") else None
    pool = watches.pool_status(scope | {"gpu": needed}, pools) if all(scope.values()) else {
        "state": "unknown", "required_cards": needed, "remaining_cards": None, "provider_updated_at": None,
        "pool": None, "available": None,
    }
    spec = probe_spec(instance)
    check = checks.get(key(spec)) if spec else None
    available = bool(check["available"]) if check and check["available"] is not None else None
    return pool | {"available": available, "spec_available": available,
                   "spec_checked_at": check["checked_at"] if check else None}


def project_stock(conn: Connection, rows: list[dict], now: int) -> dict:
    pools = watches.pool_rows(conn, now)
    checks = {row["scope"]: row for row in conn.execute(
        "SELECT * FROM instance_capacity_checks WHERE checked_at>=?", (now - 2 * SYNC_SECONDS,),
    )}
    return {row["id"]: stock_status(row, pools, checks) for row in rows if row.get("state") == "Stopped"}


def price_scope(resource: dict) -> dict:
    return {name: resource.get(name) for name in (
        "project_id", "region", "zone", "kind", "type", "size", "charge_type",
    )}


def storage_quotes(conn: Connection, resource: dict, cache: dict[str, list[dict]]) -> list[dict]:
    identity = key(price_scope(resource))
    if identity not in cache:
        cache[identity] = [dict(row) for row in conn.execute(
            """SELECT source,amount,checked_at,observed_at FROM storage_prices
               WHERE scope=? AND amount IS NOT NULL
               ORDER BY CASE source WHEN 'account' THEN 0 ELSE 1 END""", (identity,),
        )]
    return cache[identity]


def price_valid_until(price: dict) -> int:
    source, observed_at = price["source"], price["observed_at"]
    if not observed_at or price.get("checked_at", observed_at) > observed_at:
        return 0
    lifetime = {"actual": 2 * SYNC_SECONDS, "account": PRICE_SECONDS, "standard": STANDARD_SECONDS}[source]
    return observed_at + lifetime


def actual_storage_price(resource: dict) -> dict | None:
    if resource.get("actual_hourly") is None:
        return None
    return {"source": "actual", "amount": resource["actual_hourly"], "observed_at": resource.get("actual_observed_at")}


def record_disk_usage(instance: dict, previous: dict, points: list[dict], now: int) -> None:
    """Attach the latest unambiguous whole-device sample to the instance snapshot."""
    boot = instance.get("start_time")
    if instance.get("state") != "Running" or not boot or not instance.get("disks"):
        return
    old_disks = {disk.get("id"): disk for disk in previous.get("disks") or []}
    drives = Counter(disk["drive"].removeprefix("/dev/") for disk in instance["disks"] if disk.get("drive"))
    devices: dict[str, list[dict]] = {}
    for point in points:
        device = point.get("disk_device")
        if (point["metric"] == "cloudwatch_data_disk_used_per" and device
                and point["project_id"] == instance["project_id"] and point["instance_id"] == instance["id"]
                and max(boot, now - SAMPLE_FRESHNESS) <= point["sampled_at"] <= now):
            devices.setdefault(device.removeprefix("/dev/"), []).append(point)
    for disk in instance["disks"]:
        identity, drive, size = disk.get("id"), disk.get("drive"), disk.get("size_gb")
        if disk["purpose"] != "data" or not identity or not drive or not size:
            continue
        old = old_disks.get(identity, {})
        same_disk = old.get("drive") == drive and old.get("size_gb") == size
        since = old.get("usage_since", boot) if same_disk else now
        disk["usage_since"] = max(since, boot)
        device = drive.removeprefix("/dev/")
        if drives[device] != 1:
            continue
        samples = [point for point in devices.get(device, [])
                   if point["sampled_at"] >= disk["usage_since"]]
        if len({point["label"] for point in samples}) != 1:
            continue
        point = max(samples, key=lambda point: point["sampled_at"])
        if 0 <= point["value"] <= 100:
            disk["usage"] = {"percent": point["value"], "used_gb": size * point["value"] / 100,
                             "sampled_at": point["sampled_at"]}


def _storage_usage(
    resource: dict, mounted: list[str] | None, stale: bool, instances: dict, instance_error: str | None, now: int,
) -> tuple[dict | None, str]:
    if stale or mounted is None or resource.get("status") in {"Attaching", "Detaching", "Failed"}:
        return None, "mount_unverified"
    if not mounted:
        return None, "unmounted"
    if resource["kind"] != "udisk":
        return None, "no_sample"
    instance = instances.get(mounted[0], {})
    if (len(mounted) != 1 or instance_error or instance.get("region") != resource["region"]
            or instance.get("zone") != resource["zone"]):
        return None, "mount_unverified"
    if instance.get("state") == "Stopped":
        return None, "stopped"
    disks = [disk for disk in instance.get("disks") or [] if disk.get("id") == resource["id"]
             and disk["purpose"] == "data" and disk.get("size_gb") == resource["size"]]
    usage = disks[0].get("usage") if len(disks) == 1 and instance.get("state") == "Running" else None
    if usage is None:
        return None, "no_sample"
    if not 0 <= now - usage["sampled_at"] <= SAMPLE_FRESHNESS:
        return None, "stale"
    return usage, "estimated"


def read_storage(conn: Connection, project_id: str, now: int) -> dict:
    snapshot = storage.snapshot(conn, "storage", project_id)
    instance_snapshot = storage.snapshot(conn, "instances", project_id)
    instances = {row["id"]: row for row in instance_snapshot["data"]}
    by_key = {}
    items = []
    for resource in snapshot["data"]:
        candidates = storage_quotes(conn, resource, by_key)
        chosen = actual_storage_price(resource)
        if chosen is None and candidates:
            chosen = next((candidate for candidate in candidates if now < price_valid_until(candidate)), candidates[0])
        price = None
        if chosen is not None:
            limit = price_valid_until(chosen)
            price = chosen | {"stale": now > limit if chosen["source"] == "actual" else now >= limit}
        mounted = resource.get("mounted_instance_ids")
        observed_at = resource.get("observed_at")
        stale = bool(resource.get("stale") or not observed_at or now - observed_at > 2 * SYNC_SECONDS)
        usage, usage_state = _storage_usage(resource, mounted, stale, instances, instance_snapshot["error"], now)
        resource_view = resource | {"price": price,
                                    "stale": stale, "usage": usage, "usage_state": usage_state,
                                    "instances": [
            {"id": iid, "name": instances.get(iid, {}).get("name") or iid, "active": iid in instances}
            for iid in (mounted or ())
        ]}
        items.append(resource_view)
    return {"success_at": snapshot["success_at"], "error": snapshot["error"], "data": items}


def active_operations(conn: Connection, project_id: str, now: int) -> dict:
    return {
        row["instance_id"]: {
            "kind": row["kind"], "state": row["state"], "capacity_wait": bool(row["capacity_wait"]),
        } for row in conn.execute("""
            SELECT json_extract(snapshot,'$.config.instance_id') AS instance_id,
                   json_extract(snapshot,'$.kind') AS kind,state,
                   json_extract(result,'$.capacity_wait_since') AS capacity_wait
            FROM task_runs WHERE project_id=? AND scheduled_at<=?
              AND json_extract(snapshot,'$.config.instance_id') IS NOT NULL
              AND (state IN ('queued','waiting','reconciling') OR (state='manual_review' AND phase IS NOT NULL))
            ORDER BY (state='manual_review'),updated_at,id
        """, (project_id, now))
    }
