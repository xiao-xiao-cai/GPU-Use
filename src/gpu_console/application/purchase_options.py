"""Authoritative disk and shared-storage configuration for new instances."""

import json
from sqlite3 import Connection
from urllib.parse import urlsplit

from gpu_console import storage
from gpu_console.application.task_config import integer, price_limit, text, validate_spec
from gpu_console.domain.locations import SYSTEM_DISK_INCLUDED_GB
from gpu_console.storage import catalog

# Regional storage and instance eligibility documented by CompShare.
# https://www.compshare.cn/docs/operation/introduce/region
BLOCK_ZONES = {"cn-wlcb-01", "cn-sh2-02"}
BLOCK_TYPES = {"CLOUD_SSD", "CLOUD_RSSD"}
CFS_REFRESH_SECONDS = 60
CFS_CHARGES = {"Day", "Month", "Year", "Dynamic"}


def initialize(conn: Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS verified_boot_disks (
        scope TEXT PRIMARY KEY, disk TEXT NOT NULL, source TEXT NOT NULL, verified_at INTEGER NOT NULL
    )""")


def _boot_scope(project_id: str, region: str, zone: str, gpu_model: str, instance_type: str, image_id: str) -> str:
    return json.dumps([project_id, region, zone, gpu_model, instance_type, image_id], separators=(",", ":"))


def cfs_scope(project_id: str, region: str, zone: str) -> str:
    return json.dumps([project_id, region, zone], separators=(",", ":"))


def _catalog_disks(conn: Connection, region: str, zone: str, gpu_model: str, instance_type: str) -> list[dict]:
    snapshot = storage.snapshot(conn, "catalog")
    interval = storage.get_settings(conn)["catalog_seconds"]
    if snapshot["error"] or snapshot["success_at"] is None or storage.now() - snapshot["success_at"] > interval * 2:
        return []
    rows = [
        row for row in snapshot["data"]
        if (row.get("region"), row.get("zone"), row.get("gpu_model"), row.get("instance_type"))
        == (region, zone, gpu_model, instance_type)
    ]
    return rows[0].get("disks", []) if len(rows) == 1 else []


def _within(disk: dict, size: int) -> bool:
    low, high = disk.get("min_size_gb"), disk.get("max_size_gb")
    return (low is None or low <= size) and (high is None or size <= high)


def _system_disk(conn: Connection, scope: str, zone: str, disks: list[dict]) -> dict | None:
    candidates = {}
    boot_disks = [disk for disk in disks if disk.get("purpose") == "system"]
    for disk in boot_disks:
        size = disk.get("default_size_gb")
        if size is None and len(boot_disks) == 1 and disk.get("min_size_gb") == disk.get("max_size_gb"):
            size = disk.get("min_size_gb")
        if type(size) is int and size > 0 and _within(disk, size):
            candidates[(disk["type"], size)] = {"purpose": "system", "type": disk["type"], "size_gb": size}
    if len(candidates) == 1:
        return next(iter(candidates.values()))
    row = conn.execute("SELECT disk FROM verified_boot_disks WHERE scope=?", (scope,)).fetchone()
    if row is not None:
        verified = json.loads(row["disk"])
        if candidates and (verified["type"], verified["size_gb"]) not in candidates:
            return None
        if any(disk["type"] == verified["type"] and _within(disk, verified["size_gb"]) for disk in boot_disks):
            return verified
        return None
    if not candidates and not any("default_size_gb" in disk for disk in boot_disks):
        # Verified regional defaults match the included capacity. Both backings use CLOUD_SSD in the catalog.
        size = SYSTEM_DISK_INCLUDED_GB.get(zone)
        if size is None:
            return None
        if any(disk["type"] == "CLOUD_SSD" and _within(disk, size) for disk in boot_disks):
            return {"purpose": "system", "type": "CLOUD_SSD", "size_gb": size}
    return None


def record_boot_disk(conn: Connection, profile: dict) -> None:
    """Record the default observed by an administrator in the official creation form."""
    project_id, region, zone, model, pool, image_id = (
        text(profile.get(key), key, 100)
        for key in ("project_id", "region", "zone", "gpu_model", "instance_type", "image_id")
    )
    if not catalog.project_known(conn, project_id) or pool not in {"uhost", "spot"}:
        raise ValueError("项目或资源池无效，请同步平台目录")
    source = text(profile.get("source"), "官方创建页链接", 2000)
    url = urlsplit(source)
    host = url.hostname or ""
    if (url.scheme != "https" or not (host == "compshare.cn" or host.endswith(".compshare.cn"))
            or url.username or url.password or url.query or url.fragment):
        raise ValueError("请提供优云智算官方 HTTPS 创建页链接")
    disk = {
        "purpose": "system", "type": text(profile.get("type"), "系统盘类型"),
        "size_gb": integer(profile.get("size_gb"), "系统盘容量"),
    }
    if not any(item["purpose"] == "system" and item["type"] == disk["type"] and _within(item, disk["size_gb"])
               for item in _catalog_disks(conn, region, zone, model, pool)):
        raise ValueError("系统盘类型或容量与当前平台目录不符")
    conn.execute("""INSERT INTO verified_boot_disks(scope,disk,source,verified_at) VALUES(?,?,?,?)
                    ON CONFLICT(scope) DO UPDATE SET disk=excluded.disk,source=excluded.source,verified_at=excluded.verified_at""",
                 (_boot_scope(project_id, region, zone, model, pool, image_id), json.dumps(disk), source, storage.now()))


def create_options(
    conn: Connection, project_id: str, region: str, zone: str, gpu_model: str, instance_type: str, image_id: str,
    *, queue_cfs: bool = False,
) -> dict:
    if not catalog.project_known(conn, project_id):
        raise ValueError("找不到该项目，请同步项目列表")
    disks = _catalog_disks(conn, region, zone, gpu_model, instance_type)
    system_disk = _system_disk(conn, _boot_scope(project_id, region, zone, gpu_model, instance_type, image_id), zone, disks)
    data_disks = [
        disk for disk in disks if zone in BLOCK_ZONES and disk["purpose"] == "data" and disk["type"] in BLOCK_TYPES
        and type(disk.get("min_size_gb")) is int and type(disk.get("max_size_gb")) is int
        and 0 < disk["min_size_gb"] <= disk["max_size_gb"]
    ]
    locations = storage.snapshot(conn, "locations")["data"]
    pod = any(row.get("region") == region and row.get("zone") == zone and row.get("is_pod") is True
              for row in locations)
    snapshots = catalog.image_snapshots(conn, project_id, region, zone)
    images = [
        (kind, image)
        for kind in catalog.IMAGE_KINDS
        if not snapshots[kind]["error"] and snapshots[kind]["success_at"] is not None
        and storage.now() - snapshots[kind]["success_at"] <= catalog.IMAGE_INTERVAL * 2
        for image in snapshots[kind]["data"] if image.get("id") == image_id
    ]
    container = {image.get("runtime") for _, image in images} == {"container"}
    reason = None
    if pod and instance_type == "spot":
        reason = "所选容器可用区暂不支持抢占式计费"
    elif pod and not container:
        reason = "所选可用区仅支持容器镜像，请选择已同步的容器镜像"
    elif zone == "cn-sh2-02" and images and all(kind == "shared" for kind, _ in images):
        reason = "上海二B暂不支持社区镜像"
    elif system_disk is None:
        reason = "平台默认系统盘待核实，请同步机型目录或核实官方创建页"
    mode = "cfs" if pod else "us3" if zone in BLOCK_ZONES else "none"
    can_mount = container and mode != "none" and not (pod and instance_type == "spot")
    cfs = None
    cfs_known = False
    if mode == "cfs" and can_mount:
        scope = cfs_scope(project_id, region, zone)
        snapshot = storage.snapshot(conn, "cfs", scope)
        cached = snapshot["data"] if isinstance(snapshot["data"], dict) else {}
        fresh = storage.now() - cached.get("checked_at", 0) < CFS_REFRESH_SECONDS
        rows = cached.get("items", [])
        cfs_known = fresh and snapshot["error"] is None and len(rows) <= 1
        if cfs_known and rows:
            cfs = rows[0]
        if queue_cfs and not fresh:
            conn.execute(
                "INSERT OR IGNORE INTO refresh_requests(kind,scope,requested_at) VALUES ('cfs',?,?)",
                (scope, storage.now()),
            )
    return {
        "system_disk": system_disk if reason is None else None, "data_disks": data_disks,
        "system_disk_backing": "cloud" if zone in BLOCK_ZONES else "local" if pod else None,
        "storage": mode, "can_mount": can_mount, "cfs": cfs, "cfs_known": cfs_known, "reason": reason,
    }


def _shared_storage(value: object, options: dict, *, paid: bool, require_snapshot: bool) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not options["can_mount"]:
        raise ValueError("所选可用区或镜像暂不支持自动挂载共享存储")
    mode = value.get("mode")
    if not isinstance(mode, str):
        raise ValueError("共享存储方式无效")
    if mode == "us3" and options["storage"] == "us3":
        return {"mode": "us3"}
    if options["storage"] != "cfs" or mode not in {"cfs_attach", "cfs_create", "cfs_resize"}:
        raise ValueError("共享存储方式不适用于所选配置")
    if mode != "cfs_attach" and not paid:
        raise ValueError("云存储 Pro 的开通或扩容需要一次性确认")
    if require_snapshot and not options["cfs_known"]:
        raise ValueError("正在核对同区云存储 Pro，请稍后重试")
    cfs = options.get("cfs")
    if mode == "cfs_create":
        if require_snapshot and cfs is not None:
            raise ValueError("同区已有云存储 Pro，请选择挂载或扩容")
        charge = value.get("charge_type")
        if not isinstance(charge, str) or charge not in CFS_CHARGES:
            raise ValueError("云存储 Pro 计费周期无效")
        return {"mode": mode, "name": text(value.get("name"), "云存储名称", 63),
                "size_gib": integer(value.get("size_gib"), "云存储容量 GiB", 50, 2048),
                "charge_type": charge, "quantity": integer(value.get("quantity"), "云存储购买周期", 1, 3650),
                "max_price": price_limit(value.get("max_price"))}
    cfs_id = text(value.get("cfs_id"), "云存储 ID", 100)
    if require_snapshot and (cfs is None or cfs["id"] != cfs_id):
        raise ValueError("目标云存储与已核实的同区资源不符")
    result = {"mode": mode, "cfs_id": cfs_id}
    if mode == "cfs_resize":
        size = integer(value.get("size_gib"), "目标总容量 GiB", 50, 2048)
        if require_snapshot and size <= cfs["size_gib"]:
            raise ValueError("目标总容量必须大于现有容量")
        result.update(size_gib=size, max_price=price_limit(value.get("max_price")))
    return result


def _resolve_zone(
    conn: Connection, project_id: str, spec: dict, location: dict, disks: list[dict], shared: object,
    *, paid: bool, verify: bool = False,
) -> dict:
    pool = "spot" if spec["charge_type"] == "Spot" else "uhost"
    options = create_options(conn, project_id, location["region"], location["zone"], spec["gpu_model"], pool, spec["image_id"])
    system = options["system_disk"]
    if system is None:
        raise ValueError(options["reason"])
    supplied = [disk for disk in disks if disk["purpose"] == "system"]
    if (supplied or verify) and supplied != [system]:
        raise ValueError("系统盘与已核实的平台默认值不符，请重新预览")
    data = [disk for disk in disks if disk["purpose"] == "data"]
    if len(data) > 9 or any(not any(option["type"] == disk["type"] and _within(option, disk["size_gb"])
                                   for option in options["data_disks"]) for disk in data):
        raise ValueError("数据盘类型、数量或大小不适用于所选可用区和机型")
    shared_storage = _shared_storage(shared, options, paid=paid, require_snapshot=not verify)
    return {"region": location["region"], "zone": location["zone"], "resolved_disks": [system, *data],
            **({"shared_storage": shared_storage} if shared_storage else {})}


def normalize_purchase_config(conn: Connection, project_id: str, config: dict, *, paid: bool = False) -> dict:
    requested = config.get("zone_storage", [])
    if not isinstance(requested, list) or any(not isinstance(row, dict) for row in requested):
        raise ValueError("按区磁盘配置无效")
    locations = {(item["region"], item["zone"]) for item in config["zones"]}
    choices = {}
    for row in requested:
        key = (text(row.get("region"), "地域"), text(row.get("zone"), "可用区"))
        if key not in locations or key in choices:
            raise ValueError("磁盘配置的可用区重复或不在所选范围内")
        choices[key] = row
    zones = []
    for location in config["zones"]:
        if config.get("cpu_auto"):
            catalog.resolve_compute(conn, config | location)
        choice = choices.get((location["region"], location["zone"]), {})
        disks = validate_spec(config | {"disks": choice.get("disks", config["disks"])}, purchasing=False)["disks"]
        zones.append(_resolve_zone(conn, project_id, config, location, disks,
                                   choice.get("shared_storage", config.get("shared_storage")), paid=paid))
    zone_storage = [
        {key: zone[key] for key in ("region", "zone", "shared_storage") if key in zone}
        | {"disks": zone["resolved_disks"]}
        for zone in zones
    ]
    return {key: value for key, value in config.items() if key != "shared_storage"} | {
        "zones": zones, "disks": zones[0]["resolved_disks"], "zone_storage": zone_storage,
    }


def normalize_purchase_spec(conn: Connection, project_id: str, spec: dict, *, paid: bool = True) -> dict:
    base = spec | {"zones": [{key: spec.get(key) for key in ("region", "zone")}]}
    config = validate_spec(base, purchasing=False)
    config = normalize_purchase_config(conn, project_id, config | {"shared_storage": spec.get("shared_storage")}, paid=paid)
    validated = validate_spec(config | {"max_price": "0", "skip_if_match": False, "purchase_limit": 1}, purchasing=True)
    return validated | config


def verify_purchase_spec(conn: Connection, project_id: str, spec: dict) -> None:
    _resolve_zone(conn, project_id, spec, spec, spec["disks"], spec.get("shared_storage"), paid=True, verify=True)


def platform_candidates(conn: Connection, spec: dict) -> list[dict]:
    if not spec.get("cpu_auto"):
        return [spec]
    combo = catalog.resolve_compute(conn, spec)
    if spec.get("cpu_locked"):
        if spec["cpu_platform"] not in combo["cpu_platforms"]:
            raise ValueError("已确认报价的 CPU 平台已不在目录，请重新询价")
        return [spec]
    return [spec | {"cpu_platform": value} for value in catalog.automatic_platforms(combo)]
