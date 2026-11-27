"""Collect project storage and cached capacity and price checks independently of the web."""

from decimal import Decimal
from pathlib import Path

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.application.purchase_options import BLOCK_ZONES
from gpu_console.storage import storage_cost
from gpu_console.storage import instance_assets as assets
from gpu_console.worker.collection import _save_snapshot

STANDARD_SSD = {"Postpay": "0.0005", "Dynamic": "0.0005", "Day": "0.011", "Month": "0.3"}
DISK_TYPES = {"CLOUD_SSD": "SSDDataDisk", "SSDDataDisk": "SSDDataDisk",
              "CLOUD_RSSD": "RSSDDataDisk", "RSSDDataDisk": "RSSDDataDisk"}


def _disks(instance: dict) -> list[dict]:
    return [item for item in (instance.get("disks") or []) if item.get("purpose") == "data" and item.get("id")
            and item.get("type") in DISK_TYPES]


def _attached(instances: list[dict], project_id: str) -> dict[str, tuple[dict, dict]]:
    return {disk["id"]: (row | {"project_id": project_id}, disk) for row in instances
            if row.get("project_id", project_id) == project_id and row.get("state") not in {"Deleted", "Terminated", "Released"}
            for disk in _disks(row)}


def _actual(instance: dict, disk: dict, resource: dict) -> object:
    system = [item for item in instance["disks"] if item["purpose"] == "system"]
    if (sum(item["purpose"] == "data" for item in instance["disks"]) == 1 and len(system) == 1
            and system[0]["type"] == "CLOUD_SSD" and system[0]["size_gb"] <= 100
            and instance.get("charge_type") == resource["charge_type"] == "Postpay"
            and disk["size_gb"] == resource["size"]
            and DISK_TYPES[disk["type"]] == resource["type"]
            and all(instance.get(key) == resource.get(key) for key in ("region", "zone"))
            and resource["mounted_instance_ids"] == [instance["id"]]):
        return instance.get("disk_price_per_hour")
    return None


class AssetCollector:
    def __init__(self, path: Path, cloud, *, estimate_costs: bool = False) -> None:
        self.path = path
        self.cloud = cloud
        self.estimate_costs = estimate_costs
        self.attempts: dict[str, int] = {}
        self.last_trim = 0

    def tick(self) -> None:
        now = storage.now()
        with storage.connect(self.path) as conn:
            projects = sorted({item["id"] for item in storage.snapshot(conn, "projects")["data"]})
            locations = storage.snapshot(conn, "locations")["data"]
            pending = {row["scope"] for row in conn.execute("SELECT scope FROM refresh_requests WHERE kind='storage'")}
        project_id = min(projects, key=lambda pid: self.attempts.get(pid, 0), default=None)
        if pending:
            project_id = min(pending, key=lambda pid: self.attempts.get(pid, 0))
        if project_id and (project_id in pending or now - self.attempts.get(project_id, 0) >= assets.SYNC_SECONDS):
            self.poll_storage(project_id, locations)
            self.attempts[project_id] = storage.now()
        self.probe_capacity(projects, storage.now())
        self.quote_storage(projects, storage.now())
        if now - self.last_trim >= 3600:
            with storage.connect(self.path) as conn, storage.transaction(conn):
                conn.execute("DELETE FROM instance_capacity_checks WHERE checked_at<?", (now - 86400,))
                conn.execute("DELETE FROM storage_prices WHERE checked_at<?", (now - 60 * 86400,))
            self.last_trim = now

    def poll_storage(self, project_id: str, locations: list[dict]) -> None:
        with storage.connect(self.path) as conn:
            previous = storage.snapshot(conn, "storage", project_id)["data"]
            instances = storage.snapshot(conn, "instances", project_id)
        attached = _attached(instances["data"], project_id) if not instances["error"] else {}
        locations_by_zone = {row["zone"]: row for row in locations}
        for zone in BLOCK_ZONES:
            locations_by_zone.setdefault(zone, {"region": zone.rsplit("-", 1)[0], "zone": zone})
        rows, errors, observations = [], [], []
        for zone, location in sorted(locations_by_zone.items()):
            if zone not in BLOCK_ZONES and location.get("is_pod") is not True:
                continue
            kind = "udisk" if zone in BLOCK_ZONES else "cfs"
            scope = {field: location[field] for field in ("region", "zone")} | {"project_id": project_id}
            started_at = storage.now()
            try:
                if kind == "udisk":
                    observed = self.cloud.data_disks(scope)
                else:
                    observed = [
                        row | {"size": row["size_gib"], "type": "CFS"}
                        for row in self.cloud.describe_cfs(scope | location, inventory=True)
                    ]
                sampled_at = storage.now()
                fresh = []
                for resource in observed:
                    identified = attached.get(resource["id"]) if kind == "udisk" else None
                    resource = resource | scope
                    fresh.append(resource | {
                        "kind": kind, "observed_at": sampled_at,
                        "actual_hourly": _actual(*identified, resource) if identified else None,
                        "actual_observed_at": instances["success_at"] if identified else None,
                        "stale": False,
                    })
                rows.extend(fresh)
                observations.append((scope["region"], zone, kind, started_at, sampled_at, fresh))
            except CloudError as error:
                observations.append((scope["region"], zone, kind, started_at, storage.now(), None))
                errors.append(f"{zone} · {error}")
                fallback = {row["id"]: row | {"stale": True} for row in previous
                            if row["kind"] == kind and row["zone"] == zone}
                if kind == "udisk":
                    for instance, disk in attached.values():
                        if instance.get("zone") != zone:
                            continue
                        # Keep resources already seen independently and use current attachment evidence where available.
                        old = fallback.get(disk["id"], {})
                        fallback[disk["id"]] = old | scope | {
                            "id": disk["id"], "kind": kind, "type": DISK_TYPES[disk["type"]], "size": disk["size_gb"],
                            "name": disk.get("name") or old.get("name") or disk["id"],
                            "charge_type": old.get("charge_type") or ("Postpay" if instance.get("charge_type") == "Postpay" else None),
                            "mounted_instance_ids": [instance["id"]], "observed_at": instances["success_at"],
                            "stale": True,
                        }
                        resource = fallback[disk["id"]]
                        resource["actual_hourly"] = _actual(instance, disk, resource)
                        resource["actual_observed_at"] = instances["success_at"]
                    active_ids = {row["id"] for row in instances["data"]}
                    for resource in fallback.values():
                        if any(iid not in active_ids for iid in resource.get("mounted_instance_ids") or ()):
                            resource["mounted_instance_ids"] = None
                rows.extend(fallback.values())
        with storage.connect(self.path) as conn, storage.transaction(conn):
            _save_snapshot(conn, "storage", project_id, rows, storage.now(), "；".join(errors) if errors else None)
            storage_cost.observe(conn, project_id, observations, estimate_costs=self.estimate_costs)

    def probe_capacity(self, projects: list[str], now: int) -> None:
        with storage.connect(self.path) as conn:
            checks = dict(conn.execute("SELECT scope,checked_at FROM instance_capacity_checks"))
            targets = {}
            for project in projects:
                snapshot = storage.snapshot(conn, "instances", project)
                if snapshot["error"]:
                    continue
                for row in snapshot["data"]:
                    if row.get("state") != "Stopped" or row.get("project_id", project) != project:
                        continue
                    if spec := assets.probe_spec(assets.gpu_boot_target(row) | {"project_id": project}):
                        targets[assets.key(spec)] = spec
        identity = min(targets, key=lambda item: checks.get(item, 0), default=None)
        if identity is None or now - checks.get(identity, 0) < assets.SYNC_SECONDS:
            return
        try:
            available = self.cloud.capacity(targets[identity])
        except CloudError:
            available = None
        with storage.connect(self.path) as conn:
            conn.execute("""INSERT INTO instance_capacity_checks VALUES (?,?,?)
                            ON CONFLICT(scope) DO UPDATE SET checked_at=excluded.checked_at,available=excluded.available""",
                         (identity, storage.now(), available))

    def quote_storage(self, projects: list[str], now: int) -> None:
        with storage.connect(self.path) as conn:
            targets, mounted = {}, {}
            for project in projects:
                for row in storage.snapshot(conn, "storage", project)["data"]:
                    if row.get("charge_type") and row.get("actual_hourly") is None:
                        targets[assets.key(assets.price_scope(row))] = row
                mounted[project] = _attached(storage.snapshot(conn, "instances", project)["data"], project)
            checked = {(row["scope"], row["source"]): row["checked_at"]
                       for row in conn.execute("SELECT scope,source,checked_at FROM storage_prices")}
        identity = min(targets, key=lambda item: checked.get((item, "account"), 0), default=None)
        if identity is None or now - checked.get((identity, "account"), 0) < assets.PRICE_SECONDS:
            return
        resource = targets[identity]
        try:
            account, standard = self.cloud.storage_rates(resource)
        except CloudError:
            account, standard = None, None
        if resource["kind"] == "udisk" and (account is None or standard is None):
            linked = mounted[resource["project_id"]].get(resource["id"])
            if linked and assets.probe_spec(linked[0]) and linked[0]["charge_type"] == resource["charge_type"]:
                account_instance, standard_instance = self.cloud.attached_disk_rates(*linked)
                account = account if account is not None else account_instance
                standard = standard if standard is not None else standard_instance
            if standard is None and resource["type"] == "SSDDataDisk" and resource["zone"] in BLOCK_ZONES:
                unit = STANDARD_SSD.get(resource["charge_type"])
                if unit:
                    standard = Decimal(unit) * resource["size"]
        checked_at = storage.now()
        with storage.connect(self.path) as conn, storage.transaction(conn):
            for source, value in (("account", account), ("standard", standard)):
                if source == "standard" and now - checked.get((identity, source), 0) < assets.STANDARD_SECONDS:
                    continue
                conn.execute("""
                    INSERT INTO storage_prices VALUES (?,?,?,?,?)
                    ON CONFLICT(scope,source) DO UPDATE SET checked_at=excluded.checked_at,
                        amount=COALESCE(excluded.amount,storage_prices.amount),
                        observed_at=COALESCE(excluded.observed_at,storage_prices.observed_at)
                """, (identity, source, str(value) if value is not None else None,
                      checked_at, checked_at if value is not None else None))
