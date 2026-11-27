"""Record operator-verified CompShare cardless profiles by availability zone."""

from decimal import Decimal, InvalidOperation
from sqlite3 import Connection

from gpu_console.application.task_config import CARDLESS_SPECS, integer, text


def initialize(conn: Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS cardless_profiles (
        region TEXT NOT NULL, zone TEXT NOT NULL, spec TEXT NOT NULL,
        cpu INTEGER NOT NULL, memory_gb INTEGER NOT NULL, standard_hourly_cny TEXT,
        PRIMARY KEY(region,zone,spec)
    )""")


def record_profile(conn: Connection, profile: dict) -> None:
    region, zone = (text(profile.get(field), field) for field in ("region", "zone"))
    spec = text(profile.get("spec"), "无卡档位")
    if spec not in CARDLESS_SPECS:
        raise ValueError("无卡档位须为 A 或 B")
    cpu = integer(profile.get("cpu"), "CPU 核数", 1, 4096)
    memory = integer(profile.get("memory_gb"), "内存 GB", 1, 65536)
    standard = profile.get("standard_hourly_cny")
    if standard is not None:
        try:
            amount = Decimal(str(standard))
        except InvalidOperation:
            raise ValueError("无卡官方小时价无效") from None
        if not amount.is_finite() or not 0 <= amount <= 10000:
            raise ValueError("无卡官方小时价无效")
        standard = str(amount)
    if conn.execute("""SELECT 1 FROM cardless_profiles WHERE region=? AND zone=?
                       AND cpu=? AND memory_gb=? AND spec<>?""", (region, zone, cpu, memory, spec)).fetchone():
        raise ValueError("同区无卡档位必须可通过 CPU 和内存区分")
    conn.execute("""INSERT INTO cardless_profiles VALUES (?,?,?,?,?,?) ON CONFLICT(region,zone,spec)
        DO UPDATE SET cpu=excluded.cpu,memory_gb=excluded.memory_gb,standard_hourly_cny=excluded.standard_hourly_cny""",
        (region, zone, spec, cpu, memory, standard))


def profiles(conn: Connection) -> dict[tuple[str, str], list[dict]]:
    result: dict[tuple[str, str], list[dict]] = {}
    for row in conn.execute("SELECT * FROM cardless_profiles ORDER BY region,zone,spec"):
        result.setdefault((row["region"], row["zone"]), []).append({
            key: row[key] for key in ("spec", "cpu", "memory_gb", "standard_hourly_cny")
        })
    return result


def eligible(instance: dict) -> bool:
    return instance.get("charge_type") == "Postpay" and instance.get("support_without_gpu_start") is True


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
        raise ValueError("该可用区的无卡档位未核实，不能开机")
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
