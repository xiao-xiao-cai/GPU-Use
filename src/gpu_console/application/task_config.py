"""Validation and stable comparison for task snapshots."""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
PRESETS = {"stock", "start", "acquire", "stop", "shutdown", "inspection"}
CHARGES = {"Postpay", "Spot", "Day", "Month"}
NOTIFICATION_CHANNELS = {"site", "feishu", "email"}
TASK_EVENTS = ("acquisition_succeeded", "acquisition_expired", "shutdown_result", "task_result", "idle")
INSTANCE_FIELDS = ("region", "zone", "instance_id")
SPEC_FIELDS = ("gpu_model", "gpu", "cpu", "memory_gb", "cpu_platform", "image_id", "charge_type")
SHUTDOWN_LEAD = 3600
SHUTDOWN_MINIMUM = 300
BootMode = Literal["with_gpu", "without_gpu"]
CardlessSpec = Literal["A", "B"]
CARDLESS_SPECS = ("A", "B")
INSTANCE_NAME = re.compile(r"[a-zA-Z0-9_.,: \u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff-]+")


def text(value: object, label: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} 无效")
    return value.strip()


def integer(value: object, label: str, minimum: int = 1, maximum: int = 2_147_483_647) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} 必须是 {minimum} 至 {maximum} 之间的整数")
    return value


def optional_bool(config: dict, name: str, default: bool = False) -> bool:
    value = config.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} 必须为布尔值")
    return value


def price_limit(value: object) -> str:
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("报价上限必须是有效金额") from None
    if not amount.is_finite() or not Decimal("0") <= amount <= Decimal("10000000"):
        raise ValueError("报价上限必须为 0 至 10000000 之间的金额")
    if amount.normalize().as_tuple().exponent < -6:
        raise ValueError("报价上限最多保留六位小数")
    return format(amount.normalize(), "f")


def validate_spec(config: dict, purchasing: bool, *, resolve_system_disk: bool = False, pool_only: bool = False) -> dict:
    zones = config.get("zones")
    if not isinstance(zones, list) or not 1 <= len(zones) <= 50:
        raise ValueError("选择 1 至 50 个可用区")
    locations = []
    seen = set()
    for item in zones:
        if not isinstance(item, dict):
            raise ValueError("可用区必须包含地域和区标识")
        region, zone = text(item.get("region"), "地域"), text(item.get("zone"), "可用区")
        if (region, zone) in seen:
            raise ValueError("可用区重复")
        seen.add((region, zone))
        locations.append({"region": region, "zone": zone})

    disks = config.get("disks", [])
    if not isinstance(disks, list) or len(disks) > 10:
        raise ValueError("磁盘配置无效")
    normalized_disks = []
    for item in disks:
        if not isinstance(item, dict):
            raise ValueError("磁盘必须包含用途、类型和容量")
        purpose = text(item.get("purpose"), "磁盘用途").lower()
        if purpose not in {"system", "data"}:
            raise ValueError("磁盘用途须为系统盘或数据盘")
        normalized_disks.append({
            "purpose": purpose,
            "type": text(item.get("type"), "磁盘类型"),
            "size_gb": integer(item.get("size_gb"), "磁盘容量", 1, 1_000_000),
        })

    charge_type = text(config.get("charge_type"), "计费方式")
    if charge_type not in CHARGES:
        raise ValueError("请选择支持的计费方式")
    spec = {
        "zones": locations,
        "gpu_model": text(config.get("gpu_model"), "GPU 型号"),
        "gpu": integer(config.get("gpu"), "GPU 张数", 1, 64),
        "cpu": integer(config.get("cpu"), "CPU 核数", 1, 4096),
        "memory_gb": integer(config.get("memory_gb"), "内存 GB", 1, 65536),
        "charge_type": charge_type,
    }
    if pool_only:
        return spec | {"stock_pool": True}
    auto_cpu = config.get("cpu_auto") is True or config.get("cpu_platform") is None
    spec.update(
        cpu_platform="Auto" if auto_cpu else text(config["cpu_platform"], "CPU 平台"),
        image_id=text(config.get("image_id"), "镜像 ID"),
        disks=sorted(normalized_disks, key=lambda disk: (disk["purpose"], disk["type"], disk["size_gb"])),
    )
    if auto_cpu:
        spec["cpu_auto"] = True
    if charge_type in {"Day", "Month"} and (purchasing or config.get("quantity") is not None):
        spec["quantity"] = integer(config.get("quantity"), "购买时长", 1, 3650)
    if purchasing:
        systems = sum(disk["purpose"] == "system" for disk in spec["disks"])
        if systems > 1 or systems == 0 and not resolve_system_disk:
            raise ValueError("新建模板需要且仅可配置一个系统盘")
        spec["max_price"] = price_limit(config.get("max_price"))
        spec["skip_if_match"] = optional_bool(config, "skip_if_match", True)
        spec["purchase_limit"] = integer(config.get("purchase_limit", 1), "总购买次数", 1, 1000)
    return spec


def validate_config(kind: str, source: object, *, immediate: bool = False) -> dict:
    if not isinstance(source, dict):
        raise ValueError("任务配置无效")
    if kind in {"stock", "acquire"}:
        pool_only = kind == "stock" and (source.get("stock_pool") is True or source.get("cpu_platform") is None)
        config = validate_spec(source, kind == "acquire", resolve_system_disk=kind == "acquire", pool_only=pool_only)
        if kind == "acquire":
            config.update({key: source[key] for key in ("zone_storage", "shared_storage") if key in source})
    elif kind == "inspection" and not source.get("instance_id"):
        config = {}
    else:
        config = {field: text(source.get(field), field) for field in INSTANCE_FIELDS}
    boot_mode, cardless_spec = source.get("boot_mode"), source.get("without_gpu_spec")
    if boot_mode is not None or cardless_spec is not None:
        if kind != "start" or boot_mode not in (None, "with_gpu", "without_gpu"):
            raise ValueError("仅开机操作可配置有效的启动模式")
        if boot_mode == "without_gpu":
            if cardless_spec not in CARDLESS_SPECS:
                raise ValueError("无卡开机须指定已核实的 A 或 B 档位")
            config.update(boot_mode=boot_mode, without_gpu_spec=cardless_spec)
        elif cardless_spec is not None:
            raise ValueError("只有无卡开机可指定无卡档位")
    if "capacity_retry" in source:
        retry = source["capacity_retry"]
        if kind != "start" or immediate or not isinstance(retry, dict):
            raise ValueError("只有定时开机支持缺资源时持续重试")
        if config.get("boot_mode") == "without_gpu":
            raise ValueError("无卡开机不支持缺卡持续重试")
        config["capacity_retry"] = {
            "interval_seconds": integer(retry.get("interval_seconds"), "缺资源重试间隔（秒）", 30, 3600),
        }
    if kind in {"start", "acquire", "shutdown"} and source.get("shutdown_at") is not None:
        if config.get("charge_type") == "Spot":
            raise ValueError("抢占式实例不支持定时关机")
        config["shutdown_at"] = integer(source["shutdown_at"], "关机时间")
    if source.get("shutdown_after_seconds") is not None:
        if kind != "start" or immediate or "shutdown_at" in config:
            raise ValueError("开机任务的运行时长关机不能与固定关机时间同时设置")
        config["shutdown_after_seconds"] = integer(source["shutdown_after_seconds"], "运行时长（秒）", 900, 604800)
    if kind == "shutdown":
        config["replace"] = optional_bool(source, "replace")
        if immediate and "shutdown_at" not in config:
            raise ValueError("填写关机时间")
    if kind in {"shutdown", "cancel_shutdown"} and source.get("expected_stop_at") is not None:
        config["expected_stop_at"] = integer(source["expected_stop_at"], "原关机时间")
    if kind == "rename":
        name = source.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 63 or not INSTANCE_NAME.fullmatch(name):
            raise ValueError("实例名称须为 1 至 63 个字符，可包含中英文、数字、空格及 _ . , : -")
        expected = source.get("expected_name")
        if not isinstance(expected, str) or not expected.strip() or len(expected) > 200:
            raise ValueError("缺少实例原名称，请刷新实例详情")
        config.update(name=name, expected_name=expected)
    if source.get("email") is not None:
        address = text(source["email"], "收件地址", 254)
        local, sep, domain = address.partition("@")
        if not local or not sep or "." not in domain or "@" in domain or any(char.isspace() for char in address):
            raise ValueError("收件地址无效")
        config["email"] = address
    return config


def channel_list(source: object, config: dict) -> list[str]:
    if not isinstance(source, list) or any(
        not isinstance(channel, str) or channel not in NOTIFICATION_CHANNELS for channel in source
    ):
        raise ValueError("通知渠道无效")
    if "email" in source and not config.get("email"):
        raise ValueError("填写邮件收件地址")
    return list(dict.fromkeys(["site", *source]))


def includes_event(snapshot: dict, event: str) -> bool:
    return event in snapshot.get("notification_events", TASK_EVENTS)


def validate_task(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("任务请求无效")
    kind = text(body.get("kind"), "任务类型")
    if kind not in PRESETS:
        raise ValueError("任务类型无效")
    if not isinstance(body.get("schedule"), dict):
        raise ValueError("填入任务时间")
    source = body["schedule"]
    schedule_type = text(source.get("type"), "计划类型")
    if schedule_type not in {"once", "daily", "weekly", "interval"}:
        raise ValueError("计划类型无效")
    schedule = {"type": schedule_type, "at": integer(source.get("at"), "计划时间")}
    if schedule_type == "interval":
        minimum = SHUTDOWN_MINIMUM + 60 if kind == "shutdown" else 60
        schedule["interval_seconds"] = integer(source.get("interval_seconds"), "间隔秒数", minimum, 31_536_000)
    config = validate_config(kind, body.get("config", {}))
    if kind == "shutdown":
        config.pop("shutdown_at", None)
    events = body.get("notification_events", list(TASK_EVENTS))
    if not isinstance(events, list) or len(events) > len(TASK_EVENTS) or any(
        event not in TASK_EVENTS for event in events
    ):
        raise ValueError("通知事件无效")
    return {
        "name": text(body.get("name"), "任务名称"),
        "project_id": text(body.get("project_id"), "项目"),
        "kind": kind,
        "schedule": schedule,
        "window_seconds": integer(body.get("window_seconds", 7200), "窗口秒数", 1, 604800),
        "config": config,
        "channels": channel_list(body.get("channels", ["site"]), config),
        "notification_events": list(dict.fromkeys(events)),
    }


def period(schedule: dict) -> int | None:
    return {"daily": 86400, "weekly": 604800, "interval": schedule.get("interval_seconds")}.get(schedule["type"])


def next_time(schedule: dict, after: int, *, inclusive: bool = True) -> int | None:
    at = schedule["at"]
    if at >= after + (not inclusive):
        return at
    step = period(schedule)
    if step is None:
        return None
    return at + ((after + (not inclusive) - at + step - 1) // step) * step


def run_deadline(snapshot: dict, scheduled_at: int) -> int:
    if snapshot["kind"] == "shutdown":
        target = snapshot["config"].get("shutdown_at", scheduled_at)
        return target - SHUTDOWN_MINIMUM
    return scheduled_at + snapshot["window_seconds"]


def run_window_expired(run: dict, now: int) -> bool:
    """Retain the first-attempt deadline until an opted-in start is refused for capacity."""
    capacity_waiting = (
        run["kind"] == "start" and "capacity_retry" in run["snapshot"]["config"]
        and run["result"].get("capacity_wait_since") is not None
    )
    return shutdown_registration_closed(run, now) or (now > run["deadline_at"] and not capacity_waiting)


def shutdown_registration_closed(run: dict, now: int) -> bool:
    target = run["snapshot"]["config"].get("shutdown_at")
    return run["kind"] in {"start", "acquire"} and target is not None and now > target - SHUTDOWN_MINIMUM


def shanghai_time(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, SHANGHAI).isoformat()


def disk_key(disks: object) -> tuple[tuple[str, str, int], ...] | None:
    if not isinstance(disks, list):
        return None
    try:
        return tuple(sorted((disk["purpose"].casefold(), disk["type"].casefold(), disk["size_gb"]) for disk in disks))
    except (KeyError, TypeError, AttributeError):
        return None


def cpu_platform_matches(actual: object, desired: str) -> bool | None:
    required = desired.casefold()
    if required == "auto":
        return True
    if not isinstance(actual, str) or not actual:
        return None
    platform = actual.casefold()
    if required in {"amd/auto", "intel/auto"}:
        vendor = platform.split("/", 1)[0]
        return required == vendor + "/auto" if vendor in {"amd", "intel"} else None
    return platform == required


def match_spec(instance: dict, spec: dict) -> bool | None:
    """Unknown fields matter only when no known field rules out a match."""
    unknown = False
    for name in SPEC_FIELDS:
        actual, desired = instance.get(name), spec[name]
        if name == "cpu_platform":
            match = cpu_platform_matches(actual, desired)
            if match is False:
                return False
            unknown = unknown or match is None
            continue
        if actual is None or actual == "" or (name == "gpu" and actual == 0 and instance.get("gpu_model")):
            unknown = True
        elif (actual.casefold() if isinstance(actual, str) else actual) != (
            desired.casefold() if isinstance(desired, str) else desired
        ):
            return False
    actual_disks, desired_disks = disk_key(instance.get("disks")), disk_key(spec["disks"])
    if actual_disks is None:
        unknown = True
    elif actual_disks != desired_disks:
        return False
    return None if unknown else True
