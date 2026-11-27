"""Resolve documented and operator-verified CompShare cardless profiles by zone."""

from decimal import Decimal, InvalidOperation
from sqlite3 import Connection

from gpu_console.application.task_config import CARDLESS_SPECS, integer, text


TIER_PROFILES = {
    "A": {"spec": "A", "cpu": 2, "memory_gb": 4, "standard_hourly_cny": None},
    "B": {"spec": "B", "cpu": 8, "memory_gb": 16, "standard_hourly_cny": "0.60"},
    "C": {"spec": "C", "cpu": 4, "memory_gb": 8, "standard_hourly_cny": "0.30"},
}
ZONE_TIERS = {
    ("cn-wlcb", "cn-wlcb-01"): ("B", "C"),
    ("cn-sh2", "cn-sh2-02"): ("B", "C"),
    ("cn-bj2", "cn-bj2-03"): ("A",),
    ("cn-sh2", "cn-sh2-01"): ("A",),
    ("cn-sh2", "cn-sh2-03"): ("A",),
}


def initialize(conn: Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS cardless_profiles (
        region TEXT NOT NULL, zone TEXT NOT NULL, spec TEXT NOT NULL,
        cpu INTEGER NOT NULL, memory_gb INTEGER NOT NULL, standard_hourly_cny TEXT,
        PRIMARY KEY(region,zone,spec)
    )""")


def _fits_documented_zone(scope: tuple[str, str], spec: str, cpu: int, memory: int) -> bool:
    tiers = ZONE_TIERS.get(scope)
    if tiers is None:
        return True
    return spec in tiers and (TIER_PROFILES[spec]["cpu"], TIER_PROFILES[spec]["memory_gb"]) == (cpu, memory)


def record_profile(conn: Connection, profile: dict) -> None:
    region, zone = (text(profile.get(field), field) for field in ("region", "zone"))
    spec = text(profile.get("spec"), "无卡档位")
    if spec not in CARDLESS_SPECS:
        raise ValueError("无卡档位无效")
    cpu = integer(profile.get("cpu"), "CPU 核数", 1, 4096)
    memory = integer(profile.get("memory_gb"), "内存 GB", 1, 65536)
    if not _fits_documented_zone((region, zone), spec, cpu, memory):
        raise ValueError("无卡配置与该可用区的已知规格不符")
    standard = profile.get("standard_hourly_cny")
    if standard is not None:
        try:
            amount = Decimal(str(standard))
        except InvalidOperation:
            raise ValueError("无卡官方小时价无效") from None
        if not amount.is_finite() or not 0 <= amount <= 10000:
            raise ValueError("无卡官方小时价无效")
        standard = str(amount)
    if (region, zone) not in ZONE_TIERS and conn.execute("""SELECT 1 FROM cardless_profiles WHERE region=? AND zone=?
                       AND cpu=? AND memory_gb=? AND spec<>?""", (region, zone, cpu, memory, spec)).fetchone():
        raise ValueError("同区无卡档位必须可通过 CPU 和内存区分")
    conn.execute("""INSERT INTO cardless_profiles VALUES (?,?,?,?,?,?) ON CONFLICT(region,zone,spec)
        DO UPDATE SET cpu=excluded.cpu,memory_gb=excluded.memory_gb,standard_hourly_cny=excluded.standard_hourly_cny""",
        (region, zone, spec, cpu, memory, standard))


def profiles(conn: Connection) -> dict[tuple[str, str], list[dict]]:
    result = {
        scope: {spec: TIER_PROFILES[spec].copy() for spec in tiers}
        for scope, tiers in ZONE_TIERS.items()
    }
    for row in conn.execute("SELECT * FROM cardless_profiles ORDER BY region,zone,spec"):
        scope, spec = (row["region"], row["zone"]), row["spec"]
        if not _fits_documented_zone(scope, spec, row["cpu"], row["memory_gb"]):
            continue
        options = result.setdefault(scope, {})
        if spec not in options or row["standard_hourly_cny"] is not None:
            options[spec] = {key: row[key] for key in ("spec", "cpu", "memory_gb", "standard_hourly_cny")}
    return {scope: list(options.values()) for scope, options in result.items()}


def eligible(instance: dict) -> bool:
    return (instance.get("charge_type") == "Postpay" and instance.get("support_without_gpu_start") is True
            and "A800" not in str(instance.get("gpu_model") or "").upper())


def options_for(instance: dict, registered: dict[tuple[str, str], list[dict]]) -> list[dict]:
    if not eligible(instance):
        return []
    return registered.get((instance.get("region"), instance.get("zone")), [])


def match_profile(instance: dict, options: list[dict]) -> dict | None:
    if instance.get("state") != "Running" or instance.get("gpu_mode") != "without_gpu":
        return None
    return next((item for item in options if (item["cpu"], item["memory_gb"]) == (
        instance.get("cpu"), instance.get("memory_gb"),
    )), None)


def require_profile(conn: Connection, instance: dict, spec: str) -> dict:
    if not eligible(instance):
        raise ValueError("仅平台明确支持无卡开机的按量付费实例可用")
    options = options_for(instance, profiles(conn))
    profile = next((item for item in options if item["spec"] == spec), None)
    if profile is None:
        raise ValueError("此可用区未开放这项无卡配置")
    return profile


def require_snapshot_target(conn: Connection, project_id: str, config: dict) -> None:
    from gpu_console import storage

    wants_cardless = config.get("boot_mode") == "without_gpu"
    if not (wants_cardless or config.get("shutdown_after_seconds")):
        return
    scope = storage.snapshot(conn, "instances", project_id)
    instance = next((item for item in scope["data"] if item.get("project_id") == project_id
                     and item.get("id") == config["instance_id"]
                     and all(item.get(key) == config[key] for key in ("region", "zone"))), None)
    if wants_cardless and (instance is None or scope["error"]):
        raise ValueError("无法从项目清单核实目标实例，请同步后重试")
    if instance is not None and config.get("shutdown_after_seconds") and instance.get("charge_type") == "Spot":
        raise ValueError("抢占式实例不支持定时关机")
    if wants_cardless:
        require_profile(conn, instance, config["without_gpu_spec"])
