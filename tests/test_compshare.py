import hashlib
import json
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from decimal import Decimal
from threading import Event

import pytest
from requests.exceptions import ConnectTimeout
from ucloud.core.exc import HTTPStatusException
from ucloud.core.transport.http import Response

from gpu_console.adapters.compshare import CloudError, CompShare


PROJECT = "org-test"
ZONE = {"region": "cn-wlcb", "zone": "cn-wlcb-01"}
SPEC = {
    "project_id": PROJECT, **ZONE,
    "gpu_model": "4090", "gpu": 1, "cpu": 16, "memory_gb": 64,
    "cpu_platform": "Amd/Auto", "image_id": "compshare-image-test",
    "disks": [{"purpose": "system", "type": "CLOUD_SSD", "size_gb": 100}],
    "charge_type": "Day", "quantity": 3,
}


class Wire:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.sent = []
        self.requests = []

    def send(self, request, **options):
        self.requests.append(request)
        self.sent.append((dict(request.json or request.data), options["max_retries"]))
        body = next(self.responses)
        if isinstance(body, Exception):
            raise body
        return Response(
            request.url, request.method, request=request,
            status_code=200,
            headers={"X-UCLOUD-REQUEST-UUID": "request-test"},
            content=json.dumps({"RetCode": 0, **body}).encode(),
        )


def client(wire):
    return CompShare("public-example", "private-example", transport=wire)


@pytest.mark.parametrize("field, encoded", [("GpuInventory", False), ("GpuInventoryByZone", True)])
def test_gpu_pool_inventory_wire_zero_and_regional_ownership(field, encoded):
    counts = {"Exclusive": {"10027": {"5090": 2, "bool": True, "negative": -1, "text": "3"},
                            "10028": {"5090": 99}}, "Spot": {"10027": {"5090": 0}}}
    wire = Wire(
        {"ZoneInfo": [{"Region": ZONE["region"], "Zone": ZONE["zone"], "ZoneId": "10027"},
                      {"Region": "other", "Zone": "other-01", "ZoneId": True}]},
        {field: json.dumps(counts) if encoded else counts, "UpdateTime": 1_800_000_000,
         "SpotUnsupportedGpuTypes": ["4090"]},
    )
    cloud = client(wire)
    zone, other = cloud.zones()
    assert zone["zone_id"] == 10027 and other["zone_id"] is None
    result = cloud.gpu_inventory(zone["region"], {zone["zone_id"]: zone["zone"]})
    assert result.provider_updated_at == 1_800_000_000
    assert result.counts == {(ZONE["zone"], "5090", "exclusive"): 2, (ZONE["zone"], "5090", "spot"): 0}
    assert result.spot_unsupported == {"4090"}
    request, retries = wire.sent[-1]
    assert request["Action"] == "DescribeCompShareGpuInventory"
    assert request["Region"] == ZONE["region"] and "Zone" not in request and retries == 0


@pytest.mark.parametrize("response", [
    {"UpdateTime": 123}, {"GpuInventory": []}, {"GpuInventoryByZone": "unparseable", "UpdateTime": 123},
    {"GpuInventory": {}, "UpdateTime": None}, {"GpuInventory": {}, "UpdateTime": True},
])
def test_gpu_pool_inventory_rejects_unverified_response(response):
    with pytest.raises(CloudError) as error:
        client(Wire(response)).gpu_inventory(ZONE["region"], {10027: ZONE["zone"]})
    assert error.value.code == "invalid_response" and error.value.request_id == "request-test"


def test_catalog_normalizes_sold_out_spelling():
    wire = Wire({"AvailableInstanceTypes": [{"Name": "5090", "Status": "Soldout"}]},
                {"AvailableInstanceTypes": [{"Name": "5090", "Status": "Normal"}]})
    rows = client(wire).catalog(zones=[ZONE | {"name": "华北二 A"}])
    assert [row["status"] for row in rows] == ["SoldOut", "Normal"]


@pytest.mark.parametrize(("kind", "action", "image_type"), [
    ("system", "DescribeCompShareImages", "System"),
    ("app", "DescribeCompShareImages", "App"),
    ("custom", "DescribeCompShareCustomImages", None),
    ("shared", "DescribeCompShareSharingImages", None),
])
def test_project_scoped_image_page_sends_platform_parameters_and_filters(caplog, kind, action, image_type):
    wire = Wire({"TotalCount": 103, "ImageSet": [
        {"CompShareImageId": "image-1", "Name": "Ubuntu", "Container": "False", "Status": "Available",
         "Size": 10240, "ImageCharge": False, "Owner": {"PrivateValue": "private-image-owner-example"}},
        {"CompShareImageId": "image-2", "Name": "PyTorch", "Container": "True", "Status": "Available",
         "Softwares": {"Framework": "PyTorch", "FrameworkVersion": "2.13.0", "CUDAVersion": "13.2",
                       "PythonVersion": "3.12", "OsVersion": "Ubuntu 22.04"}},
        {"CompShareImageId": "image-3", "Container": "True", "Status": "Making"},
    ]})
    page = client(wire).image_page(PROJECT, ZONE["region"], ZONE["zone"], kind, 100)
    assert page.total == 103
    assert page.ids == {"image-1", "image-2", "image-3"}
    assert page.images == [
        {"id": "image-1", "name": "Ubuntu", "kind": kind, "status": "Available", "size_gb": 10.0,
         "source_gpu_type": None, "paid": False, "runtime": "vm", "framework": None, "framework_version": None,
         "cuda_version": None, "python_version": None, "os_version": None},
        {"id": "image-2", "name": "PyTorch", "kind": kind, "status": "Available", "size_gb": None,
         "source_gpu_type": None, "paid": None, "runtime": "container", "framework": "PyTorch",
         "framework_version": "2.13.0", "cuda_version": "13.2", "python_version": "3.12", "os_version": "Ubuntu 22.04"},
    ]
    sent, retries = wire.sent[0]
    assert sent["Action"] == action
    assert sent.get("ImageType") == image_type
    assert sent["ProjectId"] == PROJECT and sent["Region"] == ZONE["region"] and sent["Zone"] == ZONE["zone"]
    assert sent["Offset"] == sent["Limit"] == "100"
    assert retries == 0 and "private-image-owner-example" not in caplog.text


def instance(n=0, **overrides):
    return {
        "UHostId": f"uhost-test-{n}", "Region": ZONE["region"], "Zone": ZONE["zone"],
        "State": "Running", "CPU": 16, "GPU": 1, "Memory": 65536, "GpuType": "4090",
        "ChargeType": "Day", "CompShareImageId": SPEC["image_id"],
        "DiskSet": [{"IsBoot": "True", "Type": "CLOUD_SSD", "Size": 100, "Secret": "disk-secret"}],
        "CpuPlatform": "Amd/Epyc2", "SchedulerStopTime": 2_000_000_000,
        "Password": "example-cloud-password", "FileBrowserPassword": "browser-secret",
        "SupportWithoutGpuStart": True,
        **overrides,
    }


def test_instance_pagination_and_projection(caplog):
    caplog.set_level(logging.INFO)
    wire = Wire(
        {"TotalCount": 101, "UHostSet": [instance(n) for n in range(100)]},
        {"TotalCount": 101, "UHostSet": [instance(100)]},
    )
    rows = client(wire).instances(PROJECT)
    assert len(rows) == 101
    assert [r["Offset"] for r, _ in wire.sent] == ["0", "100"]
    assert all("Region" not in r and r["ProjectId"] == PROJECT for r, _ in wire.sent)
    assert rows[0]["memory_gb"] == 64
    assert rows[0]["scheduler_stop_time"] == 2_000_000_000
    assert rows[0]["gpu_mode"] is None
    assert rows[0]["disks"] == SPEC["disks"]
    exposed = json.dumps(rows) + caplog.text
    assert all(secret not in exposed for secret in ("cloud-password", "disk-secret", "browser-secret"))
    assert "request-test" in caplog.text


@pytest.mark.parametrize("receipt", [{"UHostId": "uhost-test-0"}, {}, {"UHostId": "another-instance"}])
def test_instance_release_wire_requires_exact_receipt_without_sdk_retry(receipt):
    wire = Wire(receipt)
    cloud = client(wire)
    target = {"id": "uhost-test-0", "project_id": PROJECT, **ZONE}
    if receipt.get("UHostId") == target["id"]:
        assert cloud.terminate(target) == target["id"]
    else:
        with pytest.raises(CloudError) as error:
            cloud.terminate(target)
        assert error.value.uncertain and error.value.request_id == "request-test"
    [(sent, retries)] = wire.sent
    assert (sent["Action"], sent["ProjectId"], sent["Region"], sent["Zone"], sent["UHostId"], retries) == (
        "TerminateCompShareInstance", PROJECT, ZONE["region"], ZONE["zone"], target["id"], 0,
    )
    assert "ReleaseUDisk" not in sent


@pytest.mark.parametrize("count, row", [(102, instance(100)), (101, instance(0))])
def test_incomplete_instance_scan_is_rejected(count, row):
    wire = Wire(
        {"TotalCount": 101, "UHostSet": [instance(n) for n in range(100)]},
        {"TotalCount": count, "UHostSet": [row]},
    )
    with pytest.raises(CloudError, match="实例清单不完整"):
        client(wire).instances(PROJECT, [ZONE])


@pytest.mark.parametrize("extra", [{"SchedulerStopTime": None}, {"StopSchedulerTime": 1}])
def test_unknown_or_conflicting_appointment_is_not_reported_as_absent(extra):
    wire = Wire({"TotalCount": 1, "UHostSet": [instance(**extra)]})
    assert client(wire).instances(PROJECT)[0]["scheduler_status"] == "unknown"


@pytest.mark.parametrize("enabled, absent_status", [(False, "unknown"), (True, "known")])
def test_gateway_empty_appointment_field_omission_is_opt_in(enabled, absent_status, tmp_path, monkeypatch):
    absent = instance(0, StopTime=1_800_000_000)
    del absent["SchedulerStopTime"]
    alternate_only = instance(1, StopSchedulerTime=0)
    del alternate_only["SchedulerStopTime"]
    rows = [
        absent, alternate_only, instance(2, SchedulerStopTime=None),
        instance(3, SchedulerStopTime=0, StopSchedulerTime=1), instance(4, SchedulerStopTime=0), instance(5),
    ]
    wire = Wire({"TotalCount": len(rows), "UHostSet": rows})
    monkeypatch.setattr("gpu_console.adapters.compshare.RequestsTransport", lambda **_kwargs: wire)
    (tmp_path / "compshare_public_key").write_text("public-example")
    (tmp_path / "compshare_private_key").write_text("private-example")
    gateway = CompShare.from_directory(tmp_path, scheduler_omits_empty=enabled)
    result = gateway.instances(PROJECT, [ZONE])
    assert [(row["scheduler_status"], row["scheduler_stop_time"]) for row in result] == [
        (absent_status, None), ("unknown", None), ("unknown", None),
        ("unknown", None), ("known", None), ("known", 2_000_000_000),
    ]
    assert result[0]["stop_time"] == 1_800_000_000


def test_disk_projection_prefers_sku_and_leaves_legacy_category_unknown():
    instance_with_skus = instance(DiskSet=[
        {"IsBoot": "True", "Type": "Boot", "DiskType": "CLOUD_SSD", "Size": 100},
        {"IsBoot": "False", "Type": "Udisk", "DiskType": "CLOUD_RSSD", "Size": 200},
    ])
    instance_without_sku = instance(1, DiskSet=[
        {"IsBoot": "True", "Type": "Boot", "Size": 100},
    ])
    wire = Wire({"TotalCount": 2, "UHostSet": [instance_with_skus, instance_without_sku]})
    with_skus, unknown = client(wire).instances(PROJECT)
    assert with_skus["disks"] == [
        {"purpose": "system", "type": "CLOUD_SSD", "size_gb": 100},
        {"purpose": "data", "type": "CLOUD_RSSD", "size_gb": 200},
    ]
    assert unknown["disks"] is None


def test_allocation_and_appointment_fields_are_actually_sent():
    wire = Wire(
        {"Specs": [{"Gpu": 1, "Cpu": 16, "Mem": 64, "ResourceEnough": False}]},
        {"PriceDetails": [{"ChargeType": "Day", "Quantity": 3, "Instance": 120.5,
                           "SystemDisks": 4, "Disks": 3.1, "CompShareImage": 0}]},
        {"UHostIds": ["uhost-test-0"]},
        {},
        {},
        {},
    )
    cloud = client(wire)
    assert cloud.capacity(SPEC) is False
    assert cloud.quote(SPEC) == Decimal("127.60")
    assert cloud.create(SPEC, "run-17") == ["uhost-test-0"]
    row = {"id": "uhost-test-0", "project_id": PROJECT, **ZONE}
    cloud.start(row)
    cloud.set_stop(row, 2_000_000_000)
    cloud.cancel_stop(row)
    (capacity, quote, create, start, schedule, cancel) = [r for r, _ in wire.sent]
    assert capacity["Disks.0.IsBoot"] == "true"
    assert capacity["Disks.0.Size"] == "100"
    assert "Disks.1.IsBoot" not in capacity
    assert capacity["MinimalCpuPlatform"] == "Amd/Auto"
    assert capacity["CompShareImageId"] == SPEC["image_id"]
    assert quote["Quantity"] == create["Quantity"] == "3"
    assert quote["Memory"] == create["Memory"] == "65536"
    assert quote["Gpu"] == create["GPU"] == "1"
    assert create["MaxCount"] == "1"
    assert create["Remark"] == "run-17"
    assert create["Name"] == "gpu-console-run-17"
    assert "WithoutGpuSpec" not in start
    assert schedule["SchedulerStopTime"] == "2000000000"
    assert cancel["Action"] == "DeleteCompShareStopScheduler"
    assert all(request["ProjectId"] == PROJECT and retry == 0 for request, retry in wire.sent)
    assert all(request.json is None for request in wire.requests)


@pytest.mark.parametrize("disks", [[], [{"purpose": "data", "type": "CLOUD_SSD", "size_gb": 200}]])
def test_capacity_adds_a_boot_disk_to_specs_missing_one(disks):
    wire = Wire({"Specs": [{"Gpu": 1, "Cpu": 16, "Mem": 64, "ResourceEnough": False}]})
    spec = SPEC | {"disks": disks.copy()}

    assert client(wire).capacity(spec) is False

    (request, _), = wire.sent
    assert request["Disks.0.IsBoot"] == "true"
    assert request["Disks.0.Type"] == "CLOUD_SSD"
    assert request["Disks.0.Size"] == "60"
    if disks:
        assert request["Disks.1.IsBoot"] == "false"
        assert request["Disks.1.Size"] == "200"
    assert spec["disks"] == disks


@pytest.mark.parametrize(("code", "retryable", "uncertain"), [
    (226604, True, False),
    (226601, False, False),
    (110, True, True),
])
def test_start_distinguishes_capacity_rejection_from_uncertain_submission(code, retryable, uncertain):
    wire = Wire({"RetCode": code})
    row = {"id": "uhost-test-0", "project_id": PROJECT, **ZONE}

    with pytest.raises(CloudError) as caught:
        client(wire).start(row)

    assert caught.value.code == str(code)
    assert (caught.value.retryable, caught.value.uncertain) == (retryable, uncertain)
    assert caught.value.request_id == "request-test"
    (request, retries), = wire.sent
    assert request["Action"] == "StartCompShareInstance" and retries == 0
    assert request["ProjectId"] == PROJECT and request["UHostId"] == row["id"]
    assert request["Region"] == ZONE["region"] and request["Zone"] == ZONE["zone"]


@pytest.mark.parametrize(("tier", "cpu", "memory_mb"), [("A", 2, 4096), ("B", 8, 16384), ("C", 4, 8192)])
def test_cardless_wire_and_rate_require_explicit_zero_gpu_and_current_boot(tier, cpu, memory_mb):
    live = instance(ChargeType="Postpay", GPU=0, CPU=cpu, Memory=memory_mb, StartTime=1_800_000_000)
    wire = Wire({"UHostId": live["UHostId"]},
                {"TotalCount": 3, "UHostSet": [live, instance(1, ChargeType="Postpay", GPU=None),
                                             instance(2, ChargeType="Postpay", GPU=0, State="Stopped")]},
                {"TotalCount": 1, "UHostSet": [live | {"InstancePrice": 0.45}]},
                {"TotalCount": 1, "UHostSet": [live | {"InstancePrice": 5, "StartTime": 1_799_999_990}]},
                {"TotalCount": 1, "UHostSet": [live | {"InstancePrice": 5, "GPU": 1}]})
    cloud = client(wire)
    scope = {"region": "cn-sh2", "zone": "cn-sh2-01"} if tier == "A" else ZONE
    cloud.start_without_gpu({"id": live["UHostId"], "project_id": PROJECT, **scope}, tier)
    request, retries = wire.sent[0]
    assert request["WithoutGpuSpec"] == tier and retries == 0
    assert request["Region"] == scope["region"] and request["Zone"] == scope["zone"]
    rows = cloud.instances(PROJECT)
    assert [row["gpu_mode"] for row in rows] == ["without_gpu", None, None]
    assert cloud.instance_prices(PROJECT, rows[:1])[live["UHostId"]]["instance_price_per_hour"] == 0.45
    assert cloud.instance_prices(PROJECT, rows[:1]) == {}
    assert cloud.instance_prices(PROJECT, rows[:1]) == {}


def test_missing_capacity_combo_and_quote_mismatch_are_not_purchase_authorization():
    wire = Wire(
        {"Specs": [{"Gpu": 2, "Cpu": 16, "Mem": 64, "ResourceEnough": True}]},
        {"PriceDetails": [{"ChargeType": "Day", "Quantity": 1, "Instance": 4.2}]},
    )
    cloud = client(wire)
    assert cloud.capacity(SPEC) is None
    with pytest.raises(CloudError) as caught:
        cloud.quote(SPEC)
    assert caught.value.code == "quote_unverified"


@pytest.mark.parametrize("field", ["PriceDetail", "PriceDetails"])
def test_quote_accepts_both_response_field_names(field):
    wire = Wire({field: [{"ChargeType": "Day", "Quantity": 3, "Instance": 12.5}]})
    assert client(wire).quote(SPEC) == Decimal("12.5")
    assert wire.sent[0][0]["Quantity"] == "3"


def test_price_fields_must_agree_if_the_platform_sends_both():
    quote = [{"ChargeType": "Day", "Quantity": 3, "Instance": 12.5}]
    wire = Wire(
        {"PriceDetail": quote, "PriceDetails": quote},
        {"PriceDetail": quote, "PriceDetails": [quote[0] | {"Instance": 13.4}]},
    )
    cloud = client(wire)
    assert cloud.quote(SPEC) == Decimal("12.5")
    with pytest.raises(CloudError, match="无法核实"):
        cloud.quote(SPEC)


@pytest.mark.parametrize("charge_type, quantity", [("Day", 3), ("Month", 6)])
def test_multi_period_quote_requires_confirmed_total_term(charge_type, quantity):
    wire = Wire({"PriceDetail": [{"ChargeType": charge_type, "Instance": 12.5}]})
    with pytest.raises(CloudError, match="购买时长"):
        client(wire).quote(SPEC | {"charge_type": charge_type, "quantity": quantity})


@pytest.mark.parametrize("charge_type", ["Postpay", "Spot", "Day", "Month"])
def test_single_period_and_hourly_quote_can_omit_quantity(charge_type):
    wire = Wire({"PriceDetail": [{"ChargeType": charge_type, "Instance": 12.5}]})
    assert client(wire).quote(SPEC | {"charge_type": charge_type, "quantity": 1}) == Decimal("12.5")


def test_quote_requires_a_price_for_selected_paid_disks():
    spec = SPEC | {"disks": [*SPEC["disks"], {"purpose": "data", "type": "CLOUD_SSD", "size_gb": 100}]}
    wire = Wire({"PriceDetails": [{"ChargeType": "Day", "Quantity": 3, "Instance": 4.2}]})
    with pytest.raises(CloudError) as caught:
        client(wire).quote(spec)
    assert caught.value.code == "quote_unverified"


def test_container_quote_only_omits_the_cost_of_the_included_system_disk():
    spec = SPEC | {"region": "cn-sh2", "zone": "cn-sh2-01"}
    boot = SPEC["disks"][0]
    quote = {"ChargeType": "Day", "Quantity": 3, "Instance": 4.2}
    cloud = client(Wire(
        {"PriceDetails": [quote]},
        {"PriceDetails": [quote]},
        {"PriceDetails": [quote | {"SystemDisks": 0.5}]},
    ))
    assert cloud.quote(spec | {"disks": [boot | {"size_gb": 50}]}) == Decimal("4.2")
    extended = spec | {"disks": [boot | {"size_gb": 60}]}
    with pytest.raises(CloudError, match="无法核实"):
        cloud.quote(extended)
    assert cloud.quote(extended) == Decimal("4.7")


@pytest.mark.parametrize("charge_type, cards", [("Day", 1), ("Month", 1), ("Postpay", 2), ("Spot", 2)])
def test_paid_image_quote_requires_unambiguous_card_and_period_units(charge_type, cards):
    wire = Wire({"PriceDetails": [{"ChargeType": charge_type, "Instance": 3, "CompShareImage": 1.1}]})
    with pytest.raises(CloudError) as caught:
        client(wire).quote(SPEC | {"charge_type": charge_type, "gpu": cards, "quantity": 1})
    assert caught.value.code == "quote_unverified"


def test_hourly_single_card_budget_includes_paid_image_price():
    wire = Wire({"PriceDetails": [{"ChargeType": "Postpay", "Instance": 3, "CompShareImage": 1.1}]})
    assert client(wire).quote(SPEC | {"charge_type": "Postpay", "quantity": 1}) == Decimal("4.1")


@pytest.mark.parametrize("charge_type, quantity", [("Postpay", 1), ("Spot", 1), ("Day", 3), ("Month", 6)])
def test_create_and_quote_use_the_same_charge_type_and_period(charge_type, quantity):
    spec = SPEC | {"charge_type": charge_type, "quantity": quantity}
    wire = Wire(
        {"PriceDetails": [{"ChargeType": charge_type, "Quantity": quantity, "Instance": 123}]},
        {"UHostIds": ["uhost-test-0"]},
    )
    cloud = client(wire)
    assert cloud.quote(spec) == Decimal("123")
    cloud.create(spec, "run-test")
    assert [(request["ChargeType"], request["Quantity"]) for request, _ in wire.sent] == [
        (charge_type, str(quantity)), (charge_type, str(quantity)),
    ]


@pytest.mark.parametrize("response", [ConnectTimeout(), HTTPStatusException(503), {"UHostIds": []}])
def test_create_response_uncertain_and_sent_once(response):
    wire = Wire(response)
    with pytest.raises(CloudError) as caught:
        client(wire).create(SPEC, "run-test")
    assert caught.value.uncertain
    assert len(wire.sent) == 1
    assert wire.sent[0][1] == 0


def test_clear_capacity_rejection_is_sanitized(caplog):
    wire = Wire({"RetCode": 226604, "Message": "message from cloud with password-secret"})
    with pytest.raises(CloudError) as caught:
        client(wire).create(SPEC, "run-test")
    assert caught.value.code == "226604"
    assert not caught.value.uncertain
    assert caught.value.retryable
    assert "password-secret" not in str(caught.value) + caplog.text


def test_monitor_retains_duplicate_points_as_one_and_uses_only_requested_instances():
    def monitor_row(iid, uuid, value):
        return {
            "UHostId": iid,
            "Metrics": [{
                "MetricKey": "cloudwatch_gpu_util", "Tags": {"uuid": uuid, "private": "ignored"},
                "Results": [{"Values": [{"Timestamp": 100, "Value": value}] * 2}],
            }],
        }

    wire = Wire({"Data": {"List": [
        monitor_row("uhost-test-0", "GPU-test", 0),
        monitor_row("uhost-test-0", "", 70),
        monitor_row("uhost-other-project", "GPU-other", 20),
        {"UHostId": "uhost-test-no-samples"},
    ]}})
    assert client(wire).monitor(PROJECT, ZONE["region"], ["uhost-test-0", "uhost-test-no-samples"]) == [{
        "project_id": PROJECT, "instance_id": "uhost-test-0", "metric": "cloudwatch_gpu_util",
        "label": "GPU-test", "sampled_at": 100, "value": 0.0,
    }]
    request = wire.requests[0]
    assert request.headers["Content-Type"] == "application/json"
    assert request.data is None
    payload, retries = wire.sent[0]
    assert payload["UHostIds"] == ["uhost-test-0", "uhost-test-no-samples"]
    assert payload["ProjectId"] == PROJECT and retries == 0
    signature = payload.pop("Signature")
    payload["UHostIds"] = "".join(payload["UHostIds"])
    signed = "".join(key + value for key, value in sorted(payload.items()))
    assert signature == hashlib.sha1((signed + "private-example").encode()).hexdigest()


def test_projects_and_catalog_projection():
    kind = {
        "Name": "4090", "Zone": ZONE["zone"], "Status": "SoldOut",
        "CpuPlatforms": {"Amd": ["Amd/Epyc2"]},
        "GraphicsMemory": {"Value": 24},
        "MachineSizes": [{"Gpu": 1, "Collection": [
            {"Cpu": 16, "Memory": [64, 96], "MinimalCpuPlatform": ["Amd/Auto"]},
        ]}],
    }
    wire = Wire(
        {"ProjectSet": [{"ProjectId": PROJECT, "ProjectName": "Test"}]},
        {"ZoneInfo": [{"Region": ZONE["region"], "Zone": ZONE["zone"], "Describe": "Test Zone"}]},
        {"AvailableInstanceTypes": [kind]},
        {"AvailableInstanceTypes": []},
    )
    cloud = client(wire)
    assert cloud.projects() == [{"id": PROJECT, "name": "Test"}]
    assert cloud.catalog() == [{
        **ZONE, "zone_name": "Test Zone", "gpu_model": "4090", "status": "SoldOut",
        "instance_type": "uhost", "cpu_platforms": ["Amd/Epyc2"], "graphics_memory_gb": 24,
        "specs": [
            {"gpu": 1, "cpu": 16, "memory_gb": 64, "cpu_platforms": ["Amd/Auto"]},
            {"gpu": 1, "cpu": 16, "memory_gb": 96, "cpu_platforms": ["Amd/Auto"]},
        ],
        "disks": [],
    }]


def test_projects_retry_transport_failure_once_and_return_recovered_snapshot():
    wire = Wire(ConnectTimeout(), {"ProjectSet": [{"ProjectId": PROJECT, "ProjectName": "Test"}]})
    cloud = client(wire)

    assert cloud.projects() == [{"id": PROJECT, "name": "Test"}]
    assert cloud.last_request_id == "request-test"
    assert [(request["Action"], retries) for request, retries in wire.sent] == [("GetProjectList", 0)] * 2


@pytest.mark.parametrize(("responses", "code"), [
    ([ConnectTimeout(), ConnectTimeout()], "transport_error"),
    ([HTTPStatusException(403)], "credential_invalid"),
    ([HTTPStatusException(429)], "429"),
])
def test_projects_stop_after_second_transport_failure_or_first_platform_rejection(responses, code):
    wire = Wire(*responses)

    with pytest.raises(CloudError) as caught:
        client(wire).projects()

    assert caught.value.code == code
    assert [(request["Action"], retries) for request, retries in wire.sent] == [("GetProjectList", 0)] * len(responses)


def test_monitor_preserves_per_result_gpu_and_disk_dimensions():
    def result(tags, value):
        return {"TagMap": tags, "Values": [{"Timestamp": 100, "Value": value}]}

    wire = Wire({"Data": {"List": [{"UHostId": "uhost-test-0", "Metrics": [
        {"MetricKey": "cloudwatch_gpu_util", "Tags": {"uuid": "instance-uuid"}, "Results": [
            result({"gpu_bus_id": "0000:01:00.0"}, 0),
            result({"gpu_bus_id": "0000:02:00.0"}, 40),
        ]},
        {"MetricKey": "cloudwatch_data_disk_used_per", "Tags": {"disk": "/dev/vdb"}, "Results": [
            result({"mount": "/mnt/a"}, 10),
            result({"mount": "/mnt/b"}, 20),
        ]},
    ]}]}})
    points = client(wire).monitor(PROJECT, ZONE["region"], ["uhost-test-0"])
    assert [(point["label"], point["value"]) for point in points] == [
        ("0000:01:00.0", 0), ("0000:02:00.0", 40), ("/dev/vdb · /mnt/a", 10), ("/dev/vdb · /mnt/b", 20),
    ]
    assert [point.get("disk_device") for point in points] == [None, None, "/dev/vdb", "/dev/vdb"]


def test_parallel_capacity_shares_inflight_request_but_rechecks_after_completion(monkeypatch):
    sent = Event()
    joined = Event()

    class ObservedFuture(Future):
        def result(self, timeout=None):
            joined.set()
            return super().result(timeout)

    class BlockingWire(Wire):
        def send(self, request, **options):
            if not sent.is_set():
                sent.set()
                assert joined.wait(3)
            return super().send(request, **options)

    monkeypatch.setattr("gpu_console.adapters.compshare.Future", ObservedFuture)
    wire = BlockingWire(
        {"Specs": [{"Gpu": 1, "Cpu": 16, "Mem": 64, "ResourceEnough": True}]},
        {"Specs": [{"Gpu": 1, "Cpu": 16, "Mem": 64, "ResourceEnough": False}]},
    )
    cloud = client(wire)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(cloud.capacity, SPEC)
        assert sent.wait(3)
        second = pool.submit(cloud.capacity, SPEC)
        assert first.result() is second.result() is True
    assert cloud.capacity(SPEC) is False
    assert len(wire.sent) == 2


@pytest.mark.parametrize("rejection", [HTTPStatusException(429), {"RetCode": 153}])
def test_rate_limit_does_not_send_followup_requests_immediately(rejection):
    wire = Wire(rejection)
    cloud = client(wire)
    for call in (lambda: cloud.capacity(SPEC), lambda: cloud.create(SPEC, "run-test")):
        with pytest.raises(CloudError) as caught:
            call()
        assert caught.value.code == "429"
        assert caught.value.retryable and not caught.value.uncertain
    assert len(wire.sent) == 1


@pytest.mark.parametrize("code", [110, 130, 150, 8433])
def test_gateway_timeout_and_outage_keep_purchase_outcome_unknown(code):
    wire = Wire({"RetCode": code})
    with pytest.raises(CloudError) as caught:
        client(wire).create(SPEC, "run-test")
    assert caught.value.code == str(code)
    assert caught.value.retryable and caught.value.uncertain
    assert len(wire.sent) == 1


def test_reboot_and_rename_send_scoped_sdk_writes_without_retries():
    wire = Wire({}, HTTPStatusException(503))
    cloud = client(wire)
    row = {"id": "uhost-test-0", "project_id": PROJECT, **ZONE}
    cloud.reboot(row)
    with pytest.raises(CloudError) as caught:
        cloud.rename(row, "训练_gpu,01:OK-2.3")
    assert caught.value.uncertain

    assert len(wire.sent) == 2
    (reboot, _), (rename, _) = wire.sent
    assert reboot["Action"] == "RebootCompShareInstance"
    assert rename["Action"] == "ModifyCompShareInstanceName"
    assert rename["Name"] == "训练_gpu,01:OK-2.3"
    for request, retry in wire.sent:
        assert request["UHostId"] == row["id"] and request["ProjectId"] == PROJECT
        assert request["Region"] == ZONE["region"] and request["Zone"] == ZONE["zone"]
        assert retry == 0


def test_capacity_matrix_distinguishes_duplicate_invalid_and_explicit_values():
    wire = Wire({"Specs": [
        {"Gpu": 2, "Cpu": 16, "Mem": 64, "ResourceEnough": False},
        {"Gpu": 2, "Cpu": 32, "Mem": 96, "ResourceEnough": True},
        {"Gpu": 4, "Cpu": 32, "Mem": 192, "ResourceEnough": True},
        {"Gpu": 4, "Cpu": 32, "Mem": 192, "ResourceEnough": False},
        {"Gpu": 8, "Cpu": 64, "Mem": 384, "ResourceEnough": 0},
        {"Gpu": True, "Cpu": 16, "Mem": 64, "ResourceEnough": True},
        None,
    ]})
    matrix = client(wire).capacity_matrix(SPEC)
    assert matrix == {(2, 16, 64): False, (2, 32, 96): True, (4, 32, 192): None, (8, 64, 384): None}
    assert len(wire.sent) == 1
