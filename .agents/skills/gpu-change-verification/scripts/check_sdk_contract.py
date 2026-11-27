#!/usr/bin/env python3
"""Verify the CompShare SDK wire contract using an in-memory transport."""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from unittest.mock import patch


PROJECT = "org-offline"
REGION = "cn-wlcb"
ZONE = "cn-wlcb-01"
INSTANCE = "uhost-offline"
STOP_AT = 2_000_000_000


class ContractFailure(Exception):
    """A required field or transport behavior changed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractFailure(message)


def scope() -> dict[str, str]:
    return {"ProjectId": PROJECT, "Region": REGION, "Zone": ZONE}


def fixtures() -> dict[str, dict[str, Any]]:
    instance = {
        "UHostId": INSTANCE,
        "Region": REGION,
        "Zone": ZONE,
        "State": "Running",
        "Remark": "run-offline",
        "SchedulerStopTime": STOP_AT,
    }
    metric = {
        "MetricKey": "cloudwatch_gpu_util",
        "Tags": {"uuid": "gpu-offline"},
        "Results": [{"Values": [{"Timestamp": 100, "Value": 0.0}]}],
    }
    quote = {"ChargeType": "Day", "Quantity": 3, "Instance": 3.5, "Disks": 0, "SystemDisks": 0}
    spec = {"Gpu": 1, "Cpu": 16, "Mem": 64, "ResourceEnough": True}
    return {
        "CreateCompShareInstance": {"UHostIds": [INSTANCE]},
        "DescribeCompShareInstance": {"TotalCount": 1, "UHostSet": [instance]},
        "GetCompShareInstancePrice": {"PriceDetail": [quote], "PriceDetails": [quote]},
        "GetCompShareInstanceMonitor": {
            "Data": {"List": [{"UHostId": INSTANCE, "Metrics": [metric]}]}
        },
        "CheckCompShareResourceCapacity": {"Specs": [spec]},
        "UpdateCompShareStopScheduler": {"UHostId": INSTANCE},
    }


class RecordingTransport:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.responses = fixtures()
        self.sent: list[dict[str, str]] = []
        self.retry_options: list[int | None] = []
        self.fail_first = fail_first

    def send(self, request: Any, **options: Any) -> Any:
        from ucloud.core.exc import HTTPStatusException
        from ucloud.core.transport.http import Response

        action = request.data["Action"]
        require(action in self.responses, f"unexpected action {action}")
        captured = {
            k: v for k, v in request.data.items() if k not in {"Signature", "PublicKey"}
        }
        self.sent.append(captured)
        self.retry_options.append(options.get("max_retries"))
        if self.fail_first and len(self.sent) == 1:
            raise HTTPStatusException(503)
        payload = {"RetCode": 0, "Action": action + "Response", **self.responses[action]}
        return Response(
            request.url,
            request.method,
            request=request,
            status_code=200,
            content=json.dumps(payload).encode(),
        )

    def check_sent(self, action: str, expected: dict[str, str]) -> None:
        require(bool(self.sent), f"{action} did not reach the transport")
        for field, value in {"Action": action, **scope(), **expected}.items():
            require(self.sent[-1].get(field) == value, f"{action} changed wire field {field}")


def client_for(transport: RecordingTransport) -> Any:
    from ucloud.client import Client
    from ucloud.core.utils.middleware import Middleware

    config = {
        "public_key": "offline-example-public",
        "private_key": "offline-example-private",
        "project_id": PROJECT,
        "region": REGION,
        "base_url": "https://compshare.invalid",
        "max_retries": 0,
    }
    return Client(config, transport=transport, middleware=Middleware()).ucompshare()


def spec_params() -> dict[str, Any]:
    return {
        **scope(),
        "GpuType": "4090",
        "MachineType": "G",
        "MinimalCpuPlatform": "Auto",
        "CompShareImageId": "image-offline",
        "ChargeType": "Day",
        "Disks": [{"Type": "CLOUD_SSD", "Size": 64, "IsBoot": True}],
    }


def create_params() -> dict[str, Any]:
    return {
        **spec_params(),
        "CPU": 16,
        "GPU": 1,
        "Memory": 65536,
        "Quantity": 3,
        "MaxCount": 1,
        "Remark": "run-offline",
    }


def check_allocation(client: Any, wire: RecordingTransport) -> None:
    capacity = "CheckCompShareResourceCapacity"
    result = client.invoke(capacity, spec_params())
    wire.check_sent(capacity, {"Disks.0.IsBoot": "true", "CompShareImageId": "image-offline"})
    require(
        result.get("Specs") == wire.responses[capacity]["Specs"],
        "capacity lost target spec or ResourceEnough",
    )

    price = "GetCompShareInstancePrice"
    args = {
        **scope(),
        "GpuType": "4090",
        "Gpu": 1,
        "Cpu": 16,
        "Memory": 65536,
        "ChargeType": "Day",
        "Quantity": 3,
        "Disks": spec_params()["Disks"],
    }
    result = client.invoke(price, args)
    wire.check_sent(price, {"Quantity": "3", "Memory": "65536", "Disks.0.Size": "64"})
    for field in ("PriceDetail", "PriceDetails"):
        require(result.get(field) == wire.responses[price][field], f"price lost {field} or the quoted term")

    create = "CreateCompShareInstance"
    result = client.invoke(create, create_params())
    wire.check_sent(
        create,
        {"Remark": "run-offline", "MaxCount": "1", "Quantity": "3", "Disks.0.IsBoot": "true"},
    )
    require(result.get("UHostIds") == [INSTANCE], "create lost UHostIds")


def check_monitoring(client: Any, wire: RecordingTransport) -> None:
    action = "GetCompShareInstanceMonitor"
    result = client.invoke(action, {**scope(), "UHostIds": [INSTANCE]})
    wire.check_sent(action, {"UHostIds.0": INSTANCE})
    require(
        result.get("Data") == wire.responses[action]["Data"],
        "monitor lost Data.List, card tag or zero sample",
    )


def check_schedule(client: Any, wire: RecordingTransport) -> None:
    update = "UpdateCompShareStopScheduler"
    result = client.invoke(update, {**scope(), "UHostId": INSTANCE, "SchedulerStopTime": STOP_AT})
    wire.check_sent(update, {"UHostId": INSTANCE, "SchedulerStopTime": str(STOP_AT)})
    require(result.get("UHostId") == INSTANCE, "scheduler lost UHostId")

    query = "DescribeCompShareInstance"
    result = client.invoke(query, {**scope(), "UHostIds": [INSTANCE]})
    wire.check_sent(query, {"UHostIds.0": INSTANCE})
    require(
        result.get("UHostSet") == wire.responses[query]["UHostSet"],
        "describe lost schedule or correlation fields",
    )


def check_single_attempt() -> None:
    from ucloud.core.exc import HTTPStatusException

    wire = RecordingTransport(fail_first=True)
    try:
        client_for(wire).invoke("CreateCompShareInstance", create_params())
    except HTTPStatusException as error:
        require(error.status_code == 503, "unexpected simulated HTTP error")
    else:
        raise ContractFailure("simulated 503 did not reach the caller")
    require(
        len(wire.sent) == 1 and wire.retry_options == [0],
        "create was retried or transport retries were enabled",
    )


def forbid_network(*_args: Any, **_kwargs: Any) -> None:
    raise ContractFailure("unexpected network access during offline check")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expect-sdk", metavar="VERSION", help="require exactly this installed SDK version"
    )
    parser.add_argument(
        "--json", action="store_true", help="print a machine-readable success report"
    )
    args = parser.parse_args()
    try:
        sdk_version = version("ucloud-sdk-python3")
    except PackageNotFoundError:
        print("ucloud-sdk-python3 is missing from this Python environment", file=sys.stderr)
        return 2
    if args.expect_sdk and args.expect_sdk != sdk_version:
        print(f"expected SDK {args.expect_sdk}, found {sdk_version}", file=sys.stderr)
        return 2

    try:
        with (
            patch("socket.getaddrinfo", forbid_network),
            patch("socket.socket.connect", forbid_network),
        ):
            wire = RecordingTransport()
            client = client_for(wire)
            check_allocation(client, wire)
            check_monitoring(client, wire)
            check_schedule(client, wire)
            check_single_attempt()
    except ContractFailure as error:
        print(f"contract check failed: {error}", file=sys.stderr)
        return 1
    except ModuleNotFoundError as error:
        print(f"required module unavailable: {error.name}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"SDK contract could not be verified ({type(error).__name__})", file=sys.stderr)
        return 1

    checks = [
        "capacity", "price", "create", "monitor",
        "schedule-update", "schedule-readback", "single-503-attempt",
    ]
    if args.json:
        report = {"sdk": sdk_version, "transport": "in-memory", "checks": checks, "ok": True}
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"SDK {sdk_version}: {len(checks)} offline contract checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
