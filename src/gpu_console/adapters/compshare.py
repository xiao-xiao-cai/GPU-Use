"""Official CompShare SDK boundary. All inputs and outputs use application field names."""

from __future__ import annotations

import base64
import json
import logging
import math
import re
import time
from collections.abc import Iterable
from concurrent.futures import Future
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException
from ipaddress import ip_address
from pathlib import Path
from threading import Lock
from typing import Any

from requests.exceptions import RequestException
from ucloud.client import Client
from ucloud.core import exc
from ucloud.core.transport import Request, RequestsTransport, Transport
from ucloud.core.utils.middleware import Middleware

from gpu_console.domain.instance_access import safe_instance_url, valid_host
from gpu_console.domain.billing import Order
from gpu_console.domain.locations import SYSTEM_DISK_INCLUDED_GB

logger = logging.getLogger(__name__)

PROJECTS = "GetProjectList"
CREATE = "CreateCompShareInstance"
LIST = "DescribeCompShareInstance"
MONITOR = "GetCompShareInstanceMonitor"
CAPACITY = "CheckCompShareResourceCapacity"
GPU_INVENTORY = "DescribeCompShareGpuInventory"
QUOTE = "GetCompShareInstancePrice"
USER_QUOTE = "GetCompShareInstanceUserPrice"
CFS_CHARGE_TYPES = frozenset({"Month", "Year", "Day", "Dynamic"})
PAGE_SIZE = 100
IMAGE_ACTIONS = {
    "system": ("DescribeCompShareImages", "System"),
    "app": ("DescribeCompShareImages", "App"),
    "custom": ("DescribeCompShareCustomImages", None),
    "shared": ("DescribeCompShareSharingImages", None),
}
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
METRICS = frozenset({
    "uhost_cpu_used",
    "cloudwatch_memory_usage",
    "cloudwatch_sys_disk_used_per",
    "cloudwatch_data_disk_used_per",
    "cloudwatch_gpu_util",
    "cloudwatch_gpu_memory_usage",
})
GPU_METRICS = frozenset({"cloudwatch_gpu_util", "cloudwatch_gpu_memory_usage"})
DISK_METRICS = frozenset({"cloudwatch_data_disk_used_per", "cloudwatch_sys_disk_used_per"})
DISK_SKUS = frozenset({
    "LOCAL_NORMAL", "LOCAL_SSD", "CLOUD_NORMAL", "CLOUD_SSD",
    "CLOUD_ESSD", "CLOUD_RSSD", "EXCLUSIVE_LOCAL_DISK",
})

ERRORS = {
    "110": "平台响应超时",
    "130": "平台网关或认证服务异常",
    "150": "平台服务暂不可用",
    "8433": "平台服务异常，结果待核对",
    "226601": "实例已欠费",
    "226602": "实例已过期",
    "226603": "所选镜像与卡型不兼容",
    "226604": "当前资源不足",
    "226606": "账户尚未实名认证",
    "InstanceOperationInProgress": "实例操作正在执行",
    "credential_invalid": "平台凭据无效或无访问权限",
    "429": "平台请求受限",
    "incomplete_inventory": "实例清单不完整，请重新采集",
    "incomplete_images": "镜像目录分页不完整，请重新同步",
    "invalid_response": "平台响应格式无法核实",
    "quote_unverified": "无法核实所选计费方式或购买时长对应的报价",
    "bill_incomplete": "账单分页不完整或返回的数据已变化",
    "cfs_scope_unverified": "无法核实云存储 Pro 的项目、可用区或配置",
    "transport_error": "平台连接失败",
}
TRANSIENT_RETCODES = frozenset({"110", "130", "150", "8433"})


class CloudError(RuntimeError):
    """Only expose allowlisted diagnostics and distinguish uncertain writes."""

    def __init__(
        self, code: str, *, uncertain: bool = False, retryable: bool = False,
        request_id: str | None = None,
    ) -> None:
        self.code = str(code)
        self.uncertain = uncertain
        self.retryable = retryable
        self.request_id = request_id
        super().__init__(ERRORS.get(self.code, f"平台请求失败（{self.code}）"))


@dataclass(frozen=True)
class ImagePage:
    images: list[dict[str, Any]]
    ids: set[str]
    total: int | None


@dataclass(frozen=True)
class GpuInventory:
    provider_updated_at: int
    counts: dict[tuple[str, str, str], int]
    spot_unsupported: frozenset[str]
    # The current CompShare wire contract supplies remaining counts only.
    total_counts: dict[tuple[str, str, str], int] = field(default_factory=dict)


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _zone_id(value: Any) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        value = int(value)
    number = _integer(value)
    return number if number and number <= 2**63 - 1 else None


def _flag(value: Any) -> bool | None:
    if type(value) is bool:
        return value
    if isinstance(value, str):
        return {"yes": True, "true": True, "no": False, "false": False}.get(value.casefold())
    return None


def _objects(value: Any, *, code: str = "invalid_response") -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise CloudError(code, retryable=True)
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _instance_hourly_prices(row: dict, charge_type: str | None) -> dict[str, float | None]:
    prices = {}
    for name, field in (
        ("instance_price_per_hour", "InstancePrice"),
        ("disk_price_per_hour", "DiskPrice"),
        ("image_price_per_hour", "CompShareImagePrice"),
    ):
        value = _number(row.get(field)) if charge_type in {"Postpay", "Spot"} else None
        prices[name] = value if value is not None and value >= 0 else None
    return prices


def _scope(spec: dict[str, Any]) -> dict[str, str]:
    return {"ProjectId": spec["project_id"], "Region": spec["region"], "Zone": spec["zone"]}


def _cfs_size(value: Any) -> int | None:
    size = _integer(value)
    return size if size is not None and 50 <= size <= 2048 else None


def _price_amount(value: Any) -> Decimal | None:
    if type(value) not in {str, int, float}:
        return None
    try:
        price = Decimal(str(value))
    except DecimalException:
        return None
    return price if price.is_finite() and price >= 0 else None


def _bill_timestamp(value: object) -> int | None:
    return value if type(value) is int and 0 < value < 253402214400 else None


def _bill_cents(value: object) -> int | None:
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?\d{1,15}(?:\.\d{1,12})?", value):
        return None
    cents = Decimal(value) * 100
    return int(cents) if cents == cents.to_integral_value() else None


def _bill_order(row: dict, project_name: str) -> Order:
    billing_project_id, order_no = _identifier(row.get("ProjectId")), _identifier(row.get("OrderNo"))
    order_type = _text(row.get("OrderType")) or "unknown"
    resource_type = _text(row.get("ResourceType")) or "unknown"
    if not billing_project_id or not order_no or row.get("ProjectName") != project_name:
        raise CloudError("bill_incomplete", retryable=True)
    cash, order, coupon, credit = (_bill_cents(row.get(key)) for key in ("AmountReal", "Amount", "AmountCoupon", "AmountFree"))
    amounts = None
    if row.get("ShowHover") == 1 and None not in (cash, order, coupon, credit):
        if order_type != "OT_REFUND" or max(cash, order, coupon, credit) <= 0:
            amounts = (cash, order, coupon, credit)
    names = {
        entry["Value"] for entry in row.get("ResourceExtendInfo") or []
        if isinstance(entry, dict) and entry.get("KeyId") == "name" and _text(entry.get("Value"))
    } if isinstance(row.get("ResourceExtendInfo"), list) else set()
    name = next(iter(names))[:128] if len(names) == 1 else None
    return Order(billing_project_id, project_name, order_no, order_type, _identifier(row.get("ResourceId")),
                 resource_type, name, _text(row.get("ChargeType")), _bill_timestamp(row.get("CreateTime")),
                 _bill_timestamp(row.get("StartTime")), _bill_timestamp(row.get("EndTime")), amounts)


def _cfs_echo_matches(response: dict[str, Any], params: dict[str, Any]) -> bool:
    return all(key not in response or type(response[key]) is type(value) and response[key] == value
               for key, value in params.items())


def _disks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = sorted(items, key=lambda disk: (disk["purpose"] != "system", disk["type"], disk["size_gb"]))
    return [
        {"IsBoot": disk["purpose"] == "system", "Type": disk["type"], "Size": disk["size_gb"]}
        for disk in items
    ]


def _purchase_spec(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        **_scope(spec),
        "GpuType": spec["gpu_model"],
        "CompShareImageId": spec["image_id"],
        "ChargeType": spec["charge_type"],
        "Disks": _disks(spec["disks"]),
    }


def create_instance_name(run_id: str) -> str:
    return f"gpu-console-{run_id}"


def _inventory_disks(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        if not isinstance(item, dict):
            return None
        is_boot = item.get("IsBoot")
        disk_type = _identifier(item.get("DiskType"))
        legacy_type = _identifier(item.get("Type"))
        if not disk_type and legacy_type in DISK_SKUS:
            disk_type = legacy_type
        size = _integer(item.get("Size"))
        boot = _flag(is_boot)
        if boot is None or not disk_type or not size:
            return None
        details = {
            "id": _identifier(item.get("DiskId")),
            "name": _text(item.get("Name")),
            "drive": _text(item.get("Drive")),
            "encrypted": _flag(item.get("Encrypted")),
        }
        result.append({
            "purpose": "system" if boot else "data", "type": disk_type, "size_gb": size,
            **{key: value for key, value in details.items() if value is not None},
        })
    # A valid purchasable spec always has one system disk. A server may return a partial row.
    return result if sum(disk["purpose"] == "system" for disk in result) == 1 else None


def _scheduler(row: dict[str, Any], *, omits_empty: bool) -> tuple[int | None, str]:
    if omits_empty and "SchedulerStopTime" not in row and "StopSchedulerTime" not in row:
        return None, "known"
    stop_at = _integer(row.get("SchedulerStopTime"))
    if stop_at is None:
        return None, "unknown"
    if "StopSchedulerTime" in row and _integer(row["StopSchedulerTime"]) != stop_at:
        return None, "unknown"
    return stop_at or None, "known"


def _utilization(row: dict[str, Any]) -> dict[str, Any]:
    values = row.get("MonitorMessages")
    if not isinstance(values, dict):
        return {"cpu": None, "memory": None, "gpus": []}

    def percent(value: Any) -> float | None:
        return _number(value.rstrip("%") if isinstance(value, str) else value)

    cards = values.get("GpuInfo")
    return {
        "cpu": percent(values.get("CpuUsageRate")),
        "memory": percent(values.get("MemUsageRate")),
        "gpus": [
            {"utilization": percent(item.get("GpuUsageRate")),
             "memory": percent(item.get("MemoryUsageRate"))}
            for item in cards if isinstance(item, dict)
        ] if isinstance(cards, list) else [],
    }


def _ip_addresses(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    addresses = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("IP"), str):
            continue
        try:
            address = str(ip_address(item["IP"]))
        except ValueError:
            continue
        addresses.append({
            "address": address,
            "type": _text(item.get("Type")),
            "mode": _text(item.get("IPMode")),
            "bandwidth_mbps": _integer(item.get("Bandwidth")),
            "ip_id": _identifier(item.get("IPId")),
            "subnet_id": _identifier(item.get("SubnetId")),
            "vpc_id": _identifier(item.get("VPCId")),
            "mac": _identifier(item.get("Mac")),
            "is_default": _flag(item.get("Default")),
        })
    return addresses


def _ssh(value: Any) -> dict[str, str | int] | None:
    if not isinstance(value, str):
        return None
    parts = value.split()
    if not parts or parts[0] != "ssh":
        return None
    if len(parts) == 2:
        target, port_value = parts[1], "22"
    elif len(parts) == 4 and parts[1] == "-p":
        port_value, target = parts[2:]
    elif len(parts) == 4 and parts[2] == "-p":
        target, port_value = parts[1], parts[3]
    else:
        return None
    user, _, host = target.partition("@")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if (
        not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}", user)
        or not valid_host(host) or not host.isascii()
        or not re.fullmatch(r"[0-9]{1,5}", port_value)
    ):
        return None
    port = int(port_value)
    if not 1 <= port <= 65535:
        return None
    return {"user": user, "host": host, "port": port, "command": f"ssh -p {port} {user}@{host}"}


def _software_links(value: Any) -> list[dict[str, str]] | None:
    if not isinstance(value, list):
        return None
    links = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("Name"))
        url = safe_instance_url(item.get("URL"), platform=True)
        if name and url:
            links.append({"name": name, "url": url})
    return links


def _instance(
    project_id: str, row: dict[str, Any], scope: dict[str, str], *, scheduler_omits_empty: bool,
) -> dict[str, Any]:
    iid = _identifier(row.get("UHostId"))
    if not iid:
        raise CloudError("incomplete_inventory", retryable=True)
    region = _identifier(row.get("Region")) or scope.get("Region")
    zone = _identifier(row.get("Zone")) or scope.get("Zone")
    if (
        ("Region" in scope and region != scope["Region"])
        or ("Zone" in scope and zone != scope["Zone"])
        or ("ProjectId" in row and row["ProjectId"] != project_id)
    ):
        raise CloudError("incomplete_inventory", retryable=True)
    memory_mb = _integer(row.get("Memory"))
    graphics_memory = row.get("GraphicsMemory")
    charge_type = "Spot" if row.get("IsSpot") is True else _text(row.get("ChargeType"))
    stop_at, scheduler_status = _scheduler(row, omits_empty=scheduler_omits_empty)
    return {
        "id": iid, "project_id": project_id, "region": region, "zone": zone,
        "name": _text(row.get("Name")) or iid,
        "state": _text(row.get("State")),
        "machine_type": _text(row.get("MachineType")),
        "instance_type": _text(row.get("InstanceType")),
        "gpu_model": _text(row.get("GpuType")),
        "gpu": _integer(row.get("GPU")),
        "graphics_memory_gb": _integer(graphics_memory.get("Value")) if isinstance(graphics_memory, dict) else None,
        "cpu": _integer(row.get("CPU")),
        "memory_gb": memory_mb // 1024 if memory_mb and memory_mb % 1024 == 0 else None,
        "cpu_platform": _text(row.get("CpuPlatform")),
        "cpu_arch": _text(row.get("CpuArch")),
        "os_name": _text(row.get("OsName")),
        "os_type": _text(row.get("OsType")),
        "image_id": _identifier(row.get("CompShareImageId")),
        "image_name": _text(row.get("CompShareImageName")),
        "image_type": _text(row.get("CompShareImageType")),
        "image_status": _text(row.get("CompShareImageStatus")),
        "image_source": _text(row.get("CompShareImageOwnerAlias")),
        "image_author": _text(row.get("CompShareImageAuthor")),
        "basic_image_id": _identifier(row.get("BasicImageId")),
        "basic_image_name": _text(row.get("BasicImageName")),
        "disks": _inventory_disks(row.get("DiskSet")),
        "total_disk_space_gb": _integer(row.get("TotalDiskSpace")),
        "total_volume_space_gb": _integer(row.get("TotalVolumeSpace")),
        "us3_attached_at": _integer(row.get("AttachUs3Time")),
        "charge_type": charge_type,
        "auto_renew": _flag(row.get("AutoRenew")),
        "is_expired": _flag(row.get("IsExpire")),
        "usage_seconds": _integer(row.get("UsageSeconds")),
        **_instance_hourly_prices(row, charge_type),
        "discount_type": _integer(row.get("DiscountType")),
        "business_group": _text(row.get("Tag")),
        "create_time": _integer(row.get("CreateTime")),
        "start_time": _integer(row.get("StartTime")),
        "stop_time": _integer(row.get("StopTime")),
        "updated_at": _integer(row.get("UpdateTime")),
        "expire_time": _integer(row.get("ExpireTime")),
        "release_time": _integer(row.get("ReleaseTime")),
        "login_mode": _text(row.get("LoginMode")),
        "ip_addresses": _ip_addresses(row.get("IPSet")),
        "ssh": _ssh(row.get("SshLoginCommand")),
        "software_links": _software_links(row.get("Softwares")),
        "scheduler_stop_time": stop_at,
        "scheduler_status": scheduler_status,
        "support_without_gpu_start": row.get("SupportWithoutGpuStart")
        if type(row.get("SupportWithoutGpuStart")) is bool else None,
        "gpu_mode": "without_gpu" if (row.get("State") == "Running" and charge_type == "Postpay"
                                          and row.get("SupportWithoutGpuStart") is True and _integer(row.get("GPU")) == 0)
        else None,
        "remark": _text(row.get("Remark")),
        "utilization": _utilization(row),
    }


def _catalog_disks(row: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for family in _objects(row.get("Disks", [])):
        for purpose, key in (("system", "BootDisk"), ("data", "DataDisk")):
            for item in _objects(family.get(key, [])):
                disk_type = _text(item.get("Name"))
                if disk_type:
                    default = _integer(item.get("DefaultSize"))
                    result.append({
                        "purpose": purpose, "type": disk_type,
                        "min_size_gb": _integer(item.get("MinimalSize")),
                        "max_size_gb": _integer(item.get("MaximalSize")),
                        **({"default_size_gb": default} if default else {}),
                    })
    return result


def _catalog_item(row: dict[str, Any], zone: dict[str, Any], instance_type: str) -> dict[str, Any]:
    model = _text(row.get("Name"))
    if not model or row.get("Zone", zone["zone"]) != zone["zone"]:
        raise CloudError("invalid_response", retryable=True)
    platforms = row.get("CpuPlatforms")
    if not isinstance(platforms, dict):
        platforms = {}
    graphics = row.get("GraphicsMemory")
    status = _text(row.get("Status"))
    specs = []
    for size in _objects(row.get("MachineSizes", [])):
        gpu = _integer(size.get("Gpu"))
        if not gpu:
            continue
        for combo in _objects(size.get("Collection", [])):
            cpu = _integer(combo.get("Cpu"))
            if not cpu or not isinstance(combo.get("Memory"), list):
                continue
            allowed = combo.get("MinimalCpuPlatform") or []
            for memory in combo["Memory"]:
                if _integer(memory):
                    specs.append({
                        "gpu": gpu, "cpu": cpu, "memory_gb": memory,
                        "cpu_platforms": [v for v in allowed if _text(v)] if isinstance(allowed, list) else [],
                    })
    return {
        "region": zone["region"], "zone": zone["zone"], "zone_name": zone["name"],
        "gpu_model": model, "instance_type": instance_type,
        "status": "SoldOut" if status and status.casefold() == "soldout" else status,
        "specs": specs, "disks": _catalog_disks(row),
        "cpu_platforms": [
            value for key in ("Intel", "Amd")
            for value in (platforms[key] if isinstance(platforms.get(key), list) else [])
            if _text(value)
        ],
        "graphics_memory_gb": _integer(graphics.get("Value")) if isinstance(graphics, dict) else None,
    }


def _samples(
    row: dict[str, Any], project_id: str, instance_id: str,
) -> Iterable[dict[str, Any]]:
    metric = _text(row.get("MetricKey"))
    if metric not in METRICS:
        return
    outer_tags = row.get("Tags")
    outer_tags = outer_tags if isinstance(outer_tags, dict) else {}
    for result in _objects(row.get("Results", [])):
        result_tags = result.get("TagMap")
        tags = outer_tags | result_tags if isinstance(result_tags, dict) else outer_tags
        label = ""
        dimensions = {}
        if metric in GPU_METRICS:
            label = _identifier(tags.get("gpu_bus_id")) or _identifier(tags.get("uuid")) or ""
            if not label:
                continue
        elif metric in DISK_METRICS:
            mount = _text(tags.get("mount")) or _text(tags.get("mountpoint"))
            disk = _text(tags.get("disk")) or _text(tags.get("device"))
            label = " · ".join(part for part in (disk, mount) if part)
            if metric == "cloudwatch_data_disk_used_per" and disk:
                dimensions["disk_device"] = disk
        for point in _objects(result.get("Values", [])):
            sampled_at = _integer(point.get("Timestamp"))
            value = _number(point.get("Value"))
            if sampled_at and value is not None:
                yield {
                    "project_id": project_id, "instance_id": instance_id, "metric": metric,
                    "label": label, "sampled_at": sampled_at, "value": value,
                    **dimensions,
                }


class _MonitorTransport(Transport):
    """Send the monitor array as JSON after the SDK signs its concatenated values."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.instance_ids: ContextVar[tuple[str, ...]] = ContextVar("monitor_instance_ids", default=())

    def send(self, request: Request, **options: Any) -> Any:
        if request.data.get("Action") == MONITOR:
            # Keep the original SDK request intact for RetCodeException construction.
            request = Request(
                request.url, request.method,
                json={**request.data, "UHostIds": list(self.instance_ids.get())},
                headers={**request.headers, "Content-Type": "application/json"},
            )
        return self.transport.send(request, **options)


class CompShare:
    def __init__(
        self, public_key: str, private_key: str, *, transport: Transport | None = None,
        scheduler_omits_empty: bool = False,
    ) -> None:
        self._scheduler_omits_empty = scheduler_omits_empty
        self._transport = _MonitorTransport(transport or RequestsTransport(max_retries=0))
        self._client = Client(
            {
                "public_key": public_key, "private_key": private_key,
                "base_url": "https://api.compshare.cn", "max_retries": 0, "timeout": 8,
            },
            transport=self._transport,
            middleware=Middleware(),
        )
        self._service = self._client.ucompshare()
        self._request_id: ContextVar[str | None] = ContextVar("compshare_request_id", default=None)
        self._client.middleware.response(self._read_request_id)
        self._read_lock = Lock()
        self._reads: dict[tuple[str, str], Future[tuple[dict[str, Any], str | None]]] = {}
        self._rate_limited_until = 0.0

    def _sdk_invoke(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == PROJECTS:
            return self._client.invoke(action, params.copy())
        if action != MONITOR:
            return self._service.invoke(action, params.copy())
        ids = tuple(params["UHostIds"])
        token = self._transport.instance_ids.set(ids)
        try:
            # The Go SDK's UCloud signing rule concatenates native array elements.
            return self._service.invoke(action, params | {"UHostIds": "".join(ids)})
        finally:
            self._transport.instance_ids.reset(token)

    def _read_request_id(self, response: Any, http_response: Any) -> dict[str, Any]:
        if not isinstance(response, dict) or _integer(response.get("RetCode")) is None:
            raise exc.InvalidResponseException(b"", "invalid result")
        self._request_id.set(
            _identifier(http_response.request_uuid) or _identifier(response.get("request_uuid"))
        )
        return response

    @property
    def last_request_id(self) -> str | None:
        return self._request_id.get()

    @classmethod
    def from_directory(cls, directory: Path | str, *, scheduler_omits_empty: bool = False) -> CompShare:
        root = Path(directory)
        public_key = (root / "compshare_public_key").read_text().strip()
        private_key = (root / "compshare_private_key").read_text().strip()
        if not public_key or not private_key:
            raise ValueError("CompShare 凭据文件为空")
        return cls(public_key, private_key, scheduler_omits_empty=scheduler_omits_empty)

    def _invoke(
        self, action: str, params: dict[str, Any], *, write: bool = False, run_id: str | None = None,
    ) -> dict[str, Any]:
        self._request_id.set(None)
        if write:
            return self._send(action, params, write=True, run_id=run_id)
        key = (action, json.dumps(params, sort_keys=True, separators=(",", ":")))
        with self._read_lock:
            pending = self._reads.get(key)
            if pending is None:
                pending = self._reads[key] = Future()
                sender = True
            else:
                sender = False
        if not sender:
            response, request_id = pending.result()
            self._request_id.set(request_id)
            return response
        try:
            try:
                response = self._send(action, params)
            except CloudError as failure:
                if action != PROJECTS or failure.code != "transport_error":
                    raise
                response = self._send(action, params)
            pending.set_result((response, self.last_request_id))
            return response
        except BaseException as error:
            pending.set_exception(error)
            raise
        finally:
            with self._read_lock:
                del self._reads[key]

    def _send(
        self, action: str, params: dict[str, Any], *, write: bool = False, run_id: str | None = None,
    ) -> dict[str, Any]:
        with self._read_lock:
            if time.monotonic() < self._rate_limited_until:
                raise CloudError("429", retryable=True)
        failure: CloudError
        try:
            response = self._sdk_invoke(action, params)
        except exc.RetCodeException as error:
            code = str(error.code)
            message = error.message if isinstance(error.message, str) else ""
            if "InstanceOperationInProgress" in message:
                code = "InstanceOperationInProgress"
            elif code == "153":
                code = "429"
            elif code in {"170", "171", "172", "174", "293", "294", "295", "299"}:
                code = "credential_invalid"
            failure = CloudError(
                code, uncertain=write and code in TRANSIENT_RETCODES,
                retryable=code in TRANSIENT_RETCODES or code in {"226604", "InstanceOperationInProgress", "429"},
                request_id=_identifier(error.request_uuid) or self.last_request_id,
            )
        except exc.HTTPStatusException as error:
            code = "credential_invalid" if error.status_code in {401, 403} else str(error.status_code)
            # Proxies can return an error after the provider has accepted the write.
            failure = CloudError(
                code, uncertain=write and error.status_code != 429 and error.status_code not in {401, 403},
                retryable=error.status_code in {408, 429} or error.status_code >= 500,
                request_id=_identifier(error.request_uuid),
            )
        except exc.InvalidResponseException as error:
            failure = CloudError(
                "invalid_response", uncertain=write, retryable=True,
                request_id=_identifier(error.request_uuid),
            )
        except (exc.UCloudException, RequestException, OSError):
            failure = CloudError("transport_error", uncertain=write, retryable=True)
        else:
            logger.info("CompShare action=%s run=%s request=%s code=0", action, run_id, self.last_request_id)
            return response

        logger.warning(
            "CompShare action=%s run=%s request=%s code=%s",
            action, run_id, failure.request_id, failure.code,
        )
        if failure.code == "429":
            with self._read_lock:
                self._rate_limited_until = time.monotonic() + 30
        raise failure from None

    def projects(self) -> list[dict[str, str]]:
        data = self._invoke(PROJECTS, {})
        result = []
        for row in _objects(data.get("ProjectSet")):
            pid = _identifier(row.get("ProjectId"))
            if not pid:
                raise CloudError("invalid_response")
            result.append({"id": pid, "name": _text(row.get("ProjectName")) or pid})
        return result

    def billing_orders(self, project_name: str, cycle: str) -> list[Order]:
        params = {"BillingCycle": cycle, "ProjectName": project_name, "PaidState": 2, "ShowZero": 1, "Limit": PAGE_SIZE}

        def page(offset: int, expected: int | None = None) -> tuple[int, list[Order]]:
            data = self._invoke("ListUBillDetail", params | {"Offset": offset})
            total = _integer(data.get("TotalCount"))
            rows = _objects(data.get("Items"), code="bill_incomplete")
            if total is None or expected not in (None, total) or len(rows) != min(PAGE_SIZE, total - offset):
                raise CloudError("bill_incomplete", retryable=True, request_id=self.last_request_id)
            return total, [_bill_order(row, project_name) for row in rows]

        total, first = page(0)
        orders = first.copy()
        for offset in range(PAGE_SIZE, total, PAGE_SIZE):
            orders.extend(page(offset, total)[1])
        identities = {(row.billing_project_id, row.order_no) for row in orders}
        if len(identities) != total:
            raise CloudError("bill_incomplete", retryable=True, request_id=self.last_request_id)
        if total > PAGE_SIZE and set(page(0, total)[1]) != set(first):
            raise CloudError("bill_incomplete", retryable=True, request_id=self.last_request_id)
        return orders

    def instances(
        self, project_id: str, zones: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        scopes = (
            [{"Region": zone["region"], "Zone": zone["zone"]} for zone in zones]
            if zones is not None else [{}]
        )
        if not scopes:
            raise ValueError("至少指定一个允许查询的可用区")
        instances: dict[str, dict[str, Any]] = {}
        for scope in scopes:
            offset = 0
            total = None
            while True:
                data = self._invoke(
                    LIST, {"ProjectId": project_id, **scope, "Limit": PAGE_SIZE, "Offset": offset},
                )
                count = _integer(data.get("TotalCount"))
                if count is None or (total is not None and count != total):
                    raise CloudError("incomplete_inventory", retryable=True, request_id=self.last_request_id)
                total = count
                rows = _objects(data.get("UHostSet"), code="incomplete_inventory")
                if len(rows) != min(PAGE_SIZE, total - offset):
                    raise CloudError("incomplete_inventory", retryable=True, request_id=self.last_request_id)
                for row in rows:
                    instance = _instance(
                        project_id, row, scope, scheduler_omits_empty=self._scheduler_omits_empty,
                    )
                    iid = instance["id"]
                    if iid in instances:
                        raise CloudError("incomplete_inventory", retryable=True, request_id=self.last_request_id)
                    instances[iid] = instance
                offset += len(rows)
                if offset == total:
                    break
        return list(instances.values())

    def instance_prices(self, project_id: str, instances: list[dict]) -> dict[str, dict[str, float | None]]:
        """Describe explicit instance IDs, which populates price fields omitted by list queries."""
        expected = {row["id"]: row for row in instances}
        data = self._invoke(LIST, {
            "ProjectId": project_id, "UHostIds": sorted(expected), "Limit": len(expected), "Offset": 0,
        })
        prices, seen = {}, set()
        rows = _objects(data.get("UHostSet"))
        if _integer(data.get("TotalCount")) != len(rows):
            raise CloudError("invalid_response", request_id=self.last_request_id)
        for row in rows:
            identity = _identifier(row.get("UHostId"))
            if identity not in expected or identity in seen or row.get("ProjectId", project_id) != project_id:
                raise CloudError("invalid_response", request_id=self.last_request_id)
            seen.add(identity)
            target = expected[identity]
            charge = "Spot" if row.get("IsSpot") is True else _text(row.get("ChargeType"))
            if target.get("gpu_mode") == "without_gpu" and not (
                row.get("State") == "Running" and _integer(row.get("GPU")) == 0
                and row.get("Region") == target.get("region") and row.get("Zone") == target.get("zone")
                and type(target.get("start_time")) is int and target["start_time"] > 0
                and _integer(row.get("StartTime")) == target.get("start_time")
                and _integer(row.get("CPU")) == target.get("cpu")
                and _integer(row.get("Memory")) == (target.get("memory_gb") or 0) * 1024
            ):
                continue
            spec_matches = (
                ("Region", _text, target.get("region")), ("Zone", _text, target.get("zone")),
                ("GpuType", _text, target.get("gpu_model")), ("GPU", _integer, target.get("gpu")),
                ("CPU", _integer, target.get("cpu")),
                ("Memory", _integer, (target.get("memory_gb") or 0) * 1024),
            )
            if charge != target.get("charge_type") or any(
                field in row and normalize(row[field]) != value for field, normalize, value in spec_matches
            ):
                continue
            prices[identity] = _instance_hourly_prices(row, charge)
        return prices

    def ssh_password(self, project_id: str, region: str, zone: str, instance_id: str) -> str | None:
        scope = {"ProjectId": project_id, "Region": region, "Zone": zone}
        if not all(_identifier(value) for value in (*scope.values(), instance_id)):
            raise CloudError("invalid_response")
        data = self._invoke(LIST, scope | {"UHostIds": [instance_id], "Limit": 1, "Offset": 0})
        rows = _objects(data.get("UHostSet"))
        if not rows:
            return None
        row = rows[0]
        if (
            len(rows) != 1 or row.get("UHostId") != instance_id
            or any(key in row and row[key] != value for key, value in scope.items())
        ):
            raise CloudError("invalid_response")
        if row.get("LoginMode") in ("KeyPair", "SSHKey"):
            return None
        value = row.get("Password")
        if not isinstance(value, str) or not value or len(value) > 2048:
            return None
        try:
            password = base64.b64decode(value, validate=True).decode()
        except ValueError:
            return None
        return password if password and password.isprintable() else None

    def zones(self) -> list[dict[str, Any]]:
        data = self._invoke("DescribeCompShareSupportZone", {})
        result = []
        for row in _objects(data.get("ZoneInfo")):
            region, zone = _identifier(row.get("Region")), _identifier(row.get("Zone"))
            if not region or not zone:
                raise CloudError("invalid_response", retryable=True)
            result.append({
                "region": region, "zone": zone, "name": _text(row.get("Describe")) or zone,
                "is_pod": row.get("IsPod") is True, "zone_id": _zone_id(row.get("ZoneId")),
            })
        return result

    def gpu_inventory(self, region: str, zone_ids: dict[int, str]) -> GpuInventory:
        response = self._invoke(GPU_INVENTORY, {"Region": region})
        inventory = response.get("GpuInventory", response.get("GpuInventoryByZone"))
        if isinstance(inventory, str):
            try:
                inventory = json.loads(inventory)
            except (ValueError, RecursionError):
                inventory = None
        updated_at = _integer(response.get("UpdateTime"))
        if not isinstance(inventory, dict) or not updated_at or updated_at > 2**63 - 1:
            raise CloudError("invalid_response", retryable=True, request_id=self.last_request_id)

        counts = {}
        for wire_pool, pool in (("Exclusive", "exclusive"), ("Spot", "spot")):
            by_zone = inventory.get(wire_pool)
            if not isinstance(by_zone, dict):
                continue
            for zone_id, zone in zone_ids.items():
                models = by_zone.get(str(zone_id))
                if not isinstance(models, dict):
                    continue
                for model, raw_count in models.items():
                    count = _integer(raw_count)
                    if _identifier(model) and count is not None and count <= 2**63 - 1:
                        counts[(zone, model, pool)] = count
        unsupported = response.get("SpotUnsupportedGpuTypes")
        spot_unsupported = frozenset(
            model for model in unsupported if _identifier(model)
        ) if isinstance(unsupported, list) else frozenset()
        return GpuInventory(updated_at, counts, spot_unsupported)

    def catalog(self, *, zones: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        result = []
        for zone in self.zones() if zones is None else zones:
            for instance_type in ("uhost", "spot"):
                response = self._invoke(
                    "DescribeAvailableCompShareInstanceTypes",
                    {"Region": zone["region"], "Zone": zone["zone"], "InstanceType": instance_type},
                )
                result.extend(
                    _catalog_item(item, zone, instance_type)
                    for item in _objects(response.get("AvailableInstanceTypes"))
                )
        return result

    def image_page(
        self, project_id: str, region: str, zone: str, kind: str, offset: int,
    ) -> ImagePage:
        action, image_type = IMAGE_ACTIONS[kind]
        params = {"ProjectId": project_id, "Region": region, "Zone": zone, "Offset": offset, "Limit": PAGE_SIZE}
        if image_type is not None:
            params["ImageType"] = image_type
        response = self._invoke(action, params)
        rows = _objects(response.get("ImageSet"), code="incomplete_images")
        total = _integer(response.get("TotalCount"))
        if len(rows) > PAGE_SIZE or (total is None and (kind == "shared" or "TotalCount" in response)):
            raise CloudError("incomplete_images", retryable=True, request_id=self.last_request_id)
        images, ids = [], set()
        for row in rows:
            identity = _identifier(row.get("CompShareImageId"))
            if not identity or identity in ids:
                raise CloudError("incomplete_images", retryable=True, request_id=self.last_request_id)
            ids.add(identity)
            status = _text(row.get("Status"))
            if status and status.casefold() != "available":
                continue
            size_mb = _integer(row.get("Size"))
            runtime = {True: "container", False: "vm"}.get(_flag(row.get("Container")))
            software = row.get("Softwares")
            software = software if isinstance(software, dict) else {}
            versions = {
                key: (_text(software.get(field)) or "").strip() or None
                for key, field in (
                    ("framework", "Framework"), ("framework_version", "FrameworkVersion"),
                    ("cuda_version", "CUDAVersion"), ("python_version", "PythonVersion"),
                    ("os_version", "OsVersion"),
                )
            }
            images.append({
                "id": identity, "name": _text(row.get("Name")) or identity, "kind": kind, "status": status,
                "size_gb": size_mb / 1024 if size_mb is not None else None,
                "source_gpu_type": _text(row.get("SourceGpuType")), "paid": _flag(row.get("ImageCharge")),
                "runtime": runtime,
            } | versions)
        return ImagePage(images, ids, total)

    def monitor(
        self, project_id: str, region: str, instance_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not region or not 1 <= len(instance_ids) <= 10:
            raise ValueError("监控单批需指定地域及 1 至 10 个实例")
        data = self._invoke(
            MONITOR, {"ProjectId": project_id, "Region": region, "UHostIds": instance_ids},
        ).get("Data")
        if not isinstance(data, dict):
            raise CloudError("invalid_response", retryable=True, request_id=self.last_request_id)
        points = {}
        for row in _objects(data.get("List")):
            iid = _identifier(row.get("UHostId"))
            if iid not in instance_ids:
                continue
            for metric in _objects(row.get("Metrics", [])):
                for point in _samples(metric, project_id, iid):
                    key = (iid, point["metric"], point["label"], point["sampled_at"])
                    points[key] = point
        return list(points.values())

    def capacity_matrix(self, spec: dict[str, Any]) -> dict[tuple[int, int, int], bool | None]:
        params = _purchase_spec(spec)
        disks = params["Disks"]
        if not any(disk["IsBoot"] for disk in disks):
            # Use the documented capacity probe disk for watches and stock checks that omit a boot disk.
            disks.insert(0, {"IsBoot": True, "Type": "CLOUD_SSD", "Size": 60})
        data = self._invoke(
            CAPACITY, {
                **params, "MachineType": "G",
                "MinimalCpuPlatform": spec.get("cpu_platform") or "Auto",
            },
        )
        items = data.get("Specs")
        if not isinstance(items, list):
            raise CloudError("invalid_response", retryable=True, request_id=self.last_request_id)
        results = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            key = tuple(item.get(field) for field in ("Gpu", "Cpu", "Mem"))
            if not all(type(value) is int and value > 0 for value in key):
                continue
            enough = item.get("ResourceEnough")
            results[key] = enough if key not in results and type(enough) is bool else None
        return results

    def capacity(self, spec: dict[str, Any]) -> bool | None:
        return self.capacity_matrix(spec).get((spec["gpu"], spec["cpu"], spec["memory_gb"]))

    def quote(self, spec: dict[str, Any]) -> Decimal:
        charge_type = spec["charge_type"]
        quantity = spec.get("quantity") or 1
        data = self._invoke(
            QUOTE, {
                **_purchase_spec(spec), "Gpu": spec["gpu"], "Cpu": spec["cpu"],
                "Memory": spec["memory_gb"] * 1024, "Quantity": quantity,
            },
        )
        quote = self._price_detail(data, "PriceDetail", charge_type, quantity)
        required = {"Instance"}
        included_size = SYSTEM_DISK_INCLUDED_GB.get(spec["zone"], 0)
        for disk in spec["disks"]:
            if disk["purpose"] == "data":
                required.add("Disks")
            elif disk["type"] != "CLOUD_SSD" or disk["size_gb"] > included_size:
                required.add("SystemDisks")
        if not required <= quote.keys():
            raise CloudError("quote_unverified", request_id=self.last_request_id)
        total = Decimal(0)
        for component in ("Instance", "SystemDisks", "Disks", "CompShareImage"):
            # The documented response may omit optional components of a free disk or image.
            value = quote.get(component, 0)
            try:
                amount = Decimal(str(value))
                if not amount.is_finite() or amount < 0:
                    raise CloudError("quote_unverified", request_id=self.last_request_id)
                if (
                    component == "CompShareImage" and amount > 0
                    and (charge_type in {"Day", "Month"} or spec["gpu"] != 1)
                ):
                    # UCloud documents a per-GPU hourly rate. CompShare does not specify
                    # if this field includes all cards and hours in the selected order.
                    raise CloudError("quote_unverified", request_id=self.last_request_id)
                total += amount
            except DecimalException:
                raise CloudError("quote_unverified", request_id=self.last_request_id) from None
        return total

    def _price_detail(self, data: dict, key: str, charge_type: str, quantity: int) -> dict:
        plural = key + "s"
        if key in data and plural in data and data[key] != data[plural]:
            raise CloudError("quote_unverified", request_id=self.last_request_id)
        prices = data.get(key, data.get(plural))
        entries = [row for row in _objects(prices, code="quote_unverified") if row.get("ChargeType") == charge_type]
        if len(entries) == 1:
            # Multi-period requests require a receipt for the actual purchase term.
            term = entries[0].get("Quantity", 1)
            if type(term) is int and term == quantity:
                return entries[0]
        raise CloudError("quote_unverified", request_id=self.last_request_id)

    def inventory_price(self, spec: dict) -> tuple[Decimal, str]:
        """Fetch the per-spec compute amount, preferring the current account price."""
        charge_type = spec["charge_type"]
        params = {
            **_scope(spec), "GpuType": spec["gpu_model"], "Gpu": spec["gpu"], "Cpu": spec["cpu"],
            "Memory": spec["memory_gb"] * 1024, "ChargeType": charge_type, "Quantity": 1,
        }
        try:
            user = self._invoke(USER_QUOTE, params)
        except CloudError:
            user = {}
        for key, source in (("PriceDetail", "account"), ("ListPriceDetail", "catalog")):
            try:
                rate = _price_amount(self._price_detail(user, key, charge_type, 1).get("Instance"))
            except CloudError:
                continue
            if rate is not None:
                return rate, source

        standard = self._invoke(QUOTE, params)
        rate = _price_amount(self._price_detail(standard, "ListPriceDetail", charge_type, 1).get("Instance"))
        if rate is None:
            raise CloudError("quote_unverified", request_id=self.last_request_id)
        return rate, "catalog"

    def _cfs_scope(self, spec: dict[str, Any]) -> tuple[dict[str, str], int]:
        scope = _scope(spec)
        if not all(_identifier(value) for value in scope.values()) or spec.get("is_pod") is False:
            raise CloudError("cfs_scope_unverified")
        zone_id = _zone_id(spec.get("zone_id"))
        if zone_id is None:
            matches = [
                zone["zone_id"] for zone in self.zones()
                if zone["region"] == spec["region"] and zone["zone"] == spec["zone"] and zone["is_pod"]
            ]
            if len(matches) != 1 or not matches[0]:
                raise CloudError("cfs_scope_unverified")
            zone_id = matches[0]
        return scope, zone_id

    def describe_cfs(
        self, spec: dict[str, Any], cfs_id: str | None = None, *, inventory: bool = False,
    ) -> list[dict[str, Any]]:
        scope, zone_id = self._cfs_scope(spec)
        if cfs_id is not None and not _identifier(cfs_id):
            raise CloudError("cfs_scope_unverified")
        data = self._invoke("DescribeCFS", scope | ({"CfsId": cfs_id} if cfs_id else {}))
        if not _cfs_echo_matches(data, scope):
            raise CloudError("cfs_scope_unverified", request_id=self.last_request_id)
        records = _objects(data.get("CFSSet", []), code="cfs_scope_unverified")
        if cfs_id and not records and data.get("Found") is True:
            records = [data]
        if (len(records) > 1 and (not inventory or cfs_id)
                or "TotalCount" in data and _integer(data["TotalCount"]) != len(records)):
            raise CloudError("cfs_scope_unverified", request_id=self.last_request_id)
        if cfs_id and ((not records and data.get("Found") is not False) or
                       (records and data.get("Found") is False)):
            raise CloudError("cfs_scope_unverified", request_id=self.last_request_id)
        result = []
        seen = set()
        for row in records:
            identity, size = _identifier(row.get("CfsId")), _integer(row.get("Size"))
            name, charge = _text(row.get("Name")), _text(row.get("ChargeType"))
            mounted = row.get("MountedUHostIds")
            if (
                not identity or identity in seen or not size or not name or charge not in CFS_CHARGE_TYPES | {"Postpay"}
                or _zone_id(row.get("ZoneId")) != zone_id or (cfs_id and identity != cfs_id)
                or row.get("Found") is False
                or not _cfs_echo_matches(row, scope)
                or (mounted is not None and (not isinstance(mounted, list) or not all(map(_identifier, mounted))))
                or (mounted and row.get("MountStatus") == "Unmounted")
            ):
                raise CloudError("cfs_scope_unverified", request_id=self.last_request_id)
            seen.add(identity)
            if cfs_id and row is not data and any(
                key in data and key in row and data[key] != row[key]
                for key in ("CfsId", "Name", "ZoneId", "Size", "ChargeType", "MountedUHostIds")
            ):
                raise CloudError("cfs_scope_unverified", request_id=self.last_request_id)
            item = {
                "id": identity, "name": name, "size_gib": size, "charge_type": charge,
                "zone_id": zone_id, "created_at": _integer(row.get("CreateTime")),
                "mounted_instance_ids": mounted,
            }
            if inventory:
                item |= {
                    "expires_at": _integer(row.get("PurchaseValue")),
                    "mounted_instance_ids": [] if row.get("MountStatus") == "Unmounted" else mounted,
                }
            result.append(item)
        return result

    def _cfs_order(self, spec: dict[str, Any], intent: dict[str, Any]) -> tuple[dict[str, Any], int]:
        scope, zone_id = self._cfs_scope(spec)
        size, charge, quantity = _cfs_size(intent.get("size_gib")), _text(intent.get("charge_type")), _integer(intent.get("quantity"))
        if size is None or charge not in CFS_CHARGE_TYPES or not quantity:
            raise CloudError("cfs_scope_unverified")
        return scope | {"Size": size, "ChargeType": charge, "Quantity": quantity}, zone_id

    def quote_cfs(self, spec: dict[str, Any], intent: dict[str, Any]) -> Decimal:
        params, _ = self._cfs_order(spec, intent)
        data = self._invoke("GetCompShareCFSPrice", params)
        if not _cfs_echo_matches(data, params):
            raise CloudError("quote_unverified", request_id=self.last_request_id)
        matching = [row for row in _objects(data.get("PriceDetails"), code="quote_unverified")
                    if row.get("ChargeType") == params["ChargeType"]]
        if len(matching) == 1 and _cfs_echo_matches(matching[0], params):
            if (price := _price_amount(matching[0].get("Disks"))) is not None:
                return price
        raise CloudError("quote_unverified", request_id=self.last_request_id)

    def data_disks(self, spec: dict) -> list[dict]:
        scope = _scope(spec)
        offset, total = 0, None
        items, seen = [], set()
        while True:
            data = self._invoke("DescribeUDisk", scope | {
                "ProtocolVersion": 1, "IgnoreBackupMode": "Yes", "IgnoreUBillInfo": "No",
                "Limit": PAGE_SIZE, "Offset": offset,
            })
            count = _integer(data.get("TotalCount"))
            rows = _objects(data.get("DataSet", []), code="incomplete_inventory")
            if count is None or total not in (None, count) or len(rows) != min(PAGE_SIZE, count - offset):
                raise CloudError("incomplete_inventory", request_id=self.last_request_id)
            for row in rows:
                identity, boot = _identifier(row.get("UDiskId")), _flag(row.get("IsBoot"))
                if (not identity or identity in seen or boot is None
                        or any(row.get(name, value) != value for name, value in scope.items())):
                    raise CloudError("incomplete_inventory", request_id=self.last_request_id)
                seen.add(identity)
                if boot:
                    continue
                size, kind = _integer(row.get("Size")), _identifier(row.get("DiskType"))
                if not size or not kind:
                    raise CloudError("incomplete_inventory", request_id=self.last_request_id)
                kind = {"CLOUD_SSD": "SSDDataDisk", "CLOUD_RSSD": "RSSDDataDisk"}.get(kind, kind)
                host = _identifier(row.get("HostId") or row.get("UHostId"))
                status = _text(row.get("Status"))
                items.append({
                    "id": identity, "name": _text(row.get("Name")) or identity, "size": size,
                    "type": kind, "charge_type": _text(row.get("ChargeType")), "status": status,
                    "expires_at": _integer(row.get("ExpiredTime")),
                    "mounted_instance_ids": [host] if host else [] if status == "Available" else None,
                })
            total = count
            offset += len(rows)
            if offset == total:
                return items

    def storage_rates(self, resource: dict) -> tuple[Decimal | None, Decimal | None]:
        charge = resource["charge_type"]
        size = resource["size"]
        if resource["kind"] == "cfs":
            params, _ = self._cfs_order(resource, {"size_gib": size, "charge_type": charge, "quantity": 1})
            data = self._invoke("GetCompShareCFSPrice", params)
            if not _cfs_echo_matches(data, params):
                raise CloudError("quote_unverified", request_id=self.last_request_id)
            return self._storage_instance_rates(data, charge)
        data = self._invoke("DescribeUDiskPrice", _scope(resource) | {
            "Size": size, "DiskType": resource["type"], "ChargeType": charge, "Quantity": 1,
        })
        rows = [row for row in _objects(data.get("DataSet"), code="quote_unverified")
                if row.get("ChargeName") == "UDisk" and row.get("ChargeType") == charge]
        if len(rows) != 1:
            raise CloudError("quote_unverified", request_id=self.last_request_id)
        account, standard = [_price_amount(rows[0].get(field)) for field in ("Price", "ListPrice")]
        return (account / 100 if account is not None else None, standard / 100 if standard is not None else None)

    def _storage_instance_rates(self, data: dict, charge: str) -> tuple[Decimal | None, Decimal | None]:
        rates = []
        for key in ("PriceDetail", "ListPriceDetail"):
            try:
                rate = _price_amount(self._price_detail(data, key, charge, 1).get("Disks"))
            except CloudError:
                rate = None
            rates.append(rate)
        return rates[0], rates[1]

    def attached_disk_rates(self, instance: dict, disk: dict) -> tuple[Decimal | None, Decimal | None]:
        spec = instance | {"disks": [item for item in instance["disks"] if item["purpose"] == "system"] + [disk]}
        charge = spec["charge_type"]
        params = _purchase_spec(spec) | {"Gpu": spec["gpu"], "Cpu": spec["cpu"],
                                        "Memory": spec["memory_gb"] * 1024, "Quantity": 1}
        try:
            account, standard = self._storage_instance_rates(self._invoke(USER_QUOTE, params), charge)
        except CloudError:
            account, standard = None, None
        if standard is None:
            try:
                _, standard = self._storage_instance_rates(self._invoke(QUOTE, params), charge)
            except CloudError:
                pass
        return account, standard

    def create_cfs(self, spec: dict[str, Any], intent: dict[str, Any], run_id: str) -> str:
        params, zone_id = self._cfs_order(spec, intent)
        name = _text(intent.get("name"))
        if not name:
            raise CloudError("cfs_scope_unverified")
        data = self._invoke("CreateCFS", params | {"Name": name}, write=True, run_id=run_id)
        identity = _identifier(data.get("CfsId"))
        if (not identity or _zone_id(data.get("ZoneId")) != zone_id
                or _integer(data.get("Size")) != params["Size"] or data.get("Name") != name
                or not _cfs_echo_matches(data, params)):
            raise CloudError("invalid_response", uncertain=True, request_id=self.last_request_id)
        return identity

    def _cfs_resize_order(self, spec: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        scope, _ = self._cfs_scope(spec)
        cfs_id, size = _identifier(intent.get("cfs_id")), _cfs_size(intent.get("size_gib"))
        if cfs_id is None or size is None:
            raise CloudError("cfs_scope_unverified")
        return scope | {"CfsId": cfs_id, "Size": size}

    def quote_cfs_resize(self, spec: dict[str, Any], intent: dict[str, Any]) -> Decimal:
        params = self._cfs_resize_order(spec, intent)
        data = self._invoke("GetCompShareCFSUpgradePrice", params)
        price = _price_amount(data.get("Price"))
        if price is None or not _cfs_echo_matches(data, params):
            raise CloudError("quote_unverified", request_id=self.last_request_id)
        return price

    def resize_cfs(self, spec: dict[str, Any], intent: dict[str, Any], run_id: str) -> str:
        params = self._cfs_resize_order(spec, intent)
        data = self._invoke("ResizeCFS", params, write=True, run_id=run_id)
        if (data.get("CfsId") != params["CfsId"] or _integer(data.get("NewSize")) != params["Size"]
                or not _cfs_echo_matches(data, params)):
            raise CloudError("invalid_response", uncertain=True, request_id=self.last_request_id)
        return params["CfsId"]

    def create(self, spec: dict[str, Any], remark: str) -> list[str]:
        mode = (spec.get("shared_storage") or {}).get("mode")
        attachment = {"EnableUS3": True} if mode == "us3" else {}
        if mode in {"cfs_attach", "cfs_create", "cfs_resize"}:
            attachment = {"EnableCfs": True}
        data = self._invoke(
            CREATE, {
                **_purchase_spec(spec), **attachment, "MachineType": "G",
                "MinimalCpuPlatform": spec.get("cpu_platform") or "Auto",
                "GPU": spec["gpu"], "CPU": spec["cpu"], "Memory": spec["memory_gb"] * 1024,
                "Quantity": spec.get("quantity") or 1, "MaxCount": 1, "Remark": remark,
                "Name": create_instance_name(remark),
            },
            write=True, run_id=remark,
        )
        ids = data.get("UHostIds")
        if not isinstance(ids, list) or len(ids) != 1 or not _identifier(ids[0]):
            raise CloudError("invalid_response", uncertain=True, request_id=self.last_request_id)
        return ids

    def _instance_action(self, action: str, instance: dict[str, Any], **params: Any) -> None:
        self._invoke(action, {**_scope(instance), "UHostId": instance["id"], **params}, write=True)

    def start(self, instance: dict[str, Any]) -> None:
        self._instance_action("StartCompShareInstance", instance)

    def start_without_gpu(self, instance: dict[str, Any], spec: str) -> None:
        if spec not in ("A", "B", "C"):
            raise ValueError("无卡档位无效")
        data = self._invoke("StartCompShareInstance", _scope(instance) | {
            "UHostId": instance["id"], "WithoutGpuSpec": spec,
        }, write=True)
        if data.get("UHostId") != instance["id"]:
            raise CloudError("invalid_response", uncertain=True, request_id=self.last_request_id)

    def stop(self, instance: dict[str, Any]) -> None:
        self._instance_action("StopCompShareInstance", instance)

    def terminate(self, instance: dict[str, Any]) -> str:
        target = instance["id"]
        receipt = self._invoke(
            "TerminateCompShareInstance", {**_scope(instance), "UHostId": target}, write=True,
        )
        if receipt.get("UHostId") != target:
            raise CloudError("invalid_response", uncertain=True, request_id=self.last_request_id)
        return target

    def resize_quote(self, instance: dict, target: dict) -> Decimal:
        response = self._invoke("GetCompShareInstanceUpgradePrice", _scope(instance) | {
            "UHostId": instance["id"], "CPU": target["cpu"], "GPU": target["gpu"], "Memory": target["memory_gb"] * 1024,
        })
        value = response.get("Price")
        try:
            amount = Decimal(str(value)) if type(value) in {int, float, str} else None
        except DecimalException:
            amount = None
        if amount is None or not amount.is_finite():
            raise CloudError("quote_unverified", request_id=self.last_request_id)
        return amount

    def resize_instance(self, instance: dict, target: dict) -> str:
        receipt = self._invoke("ResizeCompShareInstance", _scope(instance) | {
            "UHostId": instance["id"], "Cpu": target["cpu"], "Gpu": target["gpu"], "Memory": target["memory_gb"] * 1024,
        }, write=True)
        if receipt.get("UHostId") != instance["id"]:
            raise CloudError("invalid_response", uncertain=True, request_id=self.last_request_id)
        return instance["id"]

    def set_stop(self, instance: dict[str, Any], stop_at: int) -> None:
        self._instance_action("UpdateCompShareStopScheduler", instance, SchedulerStopTime=stop_at)

    def cancel_stop(self, instance: dict[str, Any]) -> None:
        self._instance_action("DeleteCompShareStopScheduler", instance)

    def reboot(self, instance: dict[str, Any]) -> None:
        self._instance_action("RebootCompShareInstance", instance)

    def rename(self, instance: dict[str, Any], name: str) -> None:
        self._instance_action("ModifyCompShareInstanceName", instance, Name=name)
