import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.api import create_app
from gpu_console.api.auth import COOKIE, digest
from gpu_console.application.tasks import TaskStore
from gpu_console.config import Config
from gpu_console.mcp import auth
from gpu_console.mcp.server import create_mcp_app
from gpu_console.storage import availability, cardless, instance_history
from gpu_console.worker.collection import Collector
from gpu_console.worker.instance_assets import AssetCollector
from gpu_console.worker.instance_resize import quote_tick
from gpu_console.worker.tasks import TaskRunner
from test_compshare import PROJECT, ZONE, Wire, client
from test_dashboard_api import call_tool

NOW = 1_800_000_000
INSTANCE = {
    "id": "uhost-test", "project_id": PROJECT, **ZONE, "name": "GPU test", "state": "Stopped",
    "charge_type": "Postpay", "instance_type": "UHost", "gpu_model": "4090", "image_id": "image-test",
    "gpu": 1, "cpu": 16, "memory_gb": 64,
    "disks": [{"id": "boot", "purpose": "system", "type": "CLOUD_SSD", "size_gb": 100}],
}
NEXT = {"gpu": 2, "cpu": 32, "memory_gb": 128}


def snapshot(conn, kind, scope, data, timestamp=NOW):
    conn.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,NULL)", (kind, scope, json.dumps(data), timestamp))


@pytest.fixture
def console(tmp_path, monkeypatch):
    now = [NOW]
    monkeypatch.setattr(storage, "now", lambda: now[0])
    config = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    with TestClient(create_app(config)) as web:
        with storage.connect(config.db) as conn:
            conn.execute("INSERT INTO sessions VALUES (?,?,?)", (digest("session-assets"), "csrf", NOW + 7200))
            snapshot(conn, "instances", PROJECT, [INSTANCE])
            snapshot(conn, "catalog", "", [ZONE | {"gpu_model": "4090", "instance_type": "uhost",
                                                    "specs": [NEXT, {key: INSTANCE[key] for key in NEXT}]}])
            snapshot(conn, "projects", "", [{"id": PROJECT}])
        web.cookies.set(COOKIE, "session-assets")
        yield web, config.db, now


@pytest.mark.parametrize("tier", ["B", "C"])
def test_cardless_web_and_mcp_share_profile_gate_and_start_snapshot(console, tier):
    web, db, _ = console
    instance_id = INSTANCE["id"]
    with storage.connect(db) as conn, storage.transaction(conn):
        token = auth.issue(conn, name="Cardless test", manage=True, ssh=False, days=None)["token"]
    body = {"project_id": PROJECT, "instance_id": instance_id, "kind": "start", "boot_mode": "without_gpu",
            "without_gpu_spec": tier, "request_id": "cardless-web"}
    headers = {"X-CSRF-Token": "csrf"}
    assert web.post("/api/actions", json=body, headers=headers).status_code == 422
    with storage.connect(db) as conn, storage.transaction(conn):
        snapshot(conn, "instances", PROJECT, [INSTANCE | {"support_without_gpu_start": True}])
    assert web.post("/api/actions", json=body | {"without_gpu_spec": "A"}, headers=headers).status_code == 422
    detail = web.get(f"/api/instances/{instance_id}", params={"project_id": PROJECT}).json()["data"]
    assert detail["cardless_profiles"] == [
        {"spec": "B", "cpu": 8, "memory_gb": 16, "standard_hourly_cny": "0.60"},
        {"spec": "C", "cpu": 4, "memory_gb": 8, "standard_hourly_cny": "0.30"},
    ]
    submitted = web.post("/api/actions", json=body, headers=headers)
    assert submitted.status_code == 202, submitted.text
    preview = web.post("/api/tasks/preview", headers=headers, json={
        "project_id": PROJECT, "name": "循环", "kind": "start", "schedule": {"type": "daily", "at": NOW + 60},
        "config": submitted.json()["snapshot"]["config"] | {"shutdown_after_seconds": 900},
    })
    assert preview.status_code == 200 and preview.json()["shutdown_after_seconds"] == 900
    config = Config(db.parent, db.parent / "secrets", db.parent / "static", False, False)
    with TestClient(create_mcp_app(config), base_url=f"http://{config.mcp_bind_ip}:{config.mcp_bind_port}") as mcp:
        args = {key: value for key, value in body.items() if key not in {"kind", "request_id"}} | {"request_id": "cardless-mcp"}
        rejected = mcp.post("/mcp", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "gpu_start_instance", "arguments": args | {"without_gpu_spec": "A"},
        }})
        assert rejected.status_code == 200 and rejected.json()["result"]["isError"]
        assert "未开放" in str(rejected.json()["result"]["content"])
        receipt = call_tool(mcp, token, "gpu_start_instance", args)
        assert call_tool(mcp, token, "gpu_start_instance", args) == receipt
        queued = call_tool(mcp, token, "gpu_get_run", {"project_id": PROJECT, "run_id": receipt["run_id"]})
        assert queued["target"]["without_gpu_spec"] == tier
        instance = call_tool(mcp, token, "gpu_get_instance", {"project_id": PROJECT, "instance_id": instance_id})
        assert instance["instance"]["cardless_profiles"] == detail["cardless_profiles"]


@pytest.mark.parametrize(("region", "zone", "expected"), [
    ("cn-wlcb", "cn-wlcb-01", [("B", 8, 16), ("C", 4, 8)]),
    ("cn-sh2", "cn-sh2-02", [("B", 8, 16), ("C", 4, 8)]),
    ("cn-bj2", "cn-bj2-03", [("A", 2, 4)]),
    ("cn-sh2", "cn-sh2-01", [("A", 2, 4)]),
    ("cn-sh2", "cn-sh2-03", [("A", 2, 4)]),
    ("unknown", "unknown-01", []),
])
def test_cardless_options_follow_the_zone_for_any_supported_gpu_configuration(console, region, zone, expected):
    web, db, _ = console
    target = INSTANCE | {"region": region, "zone": zone, "support_without_gpu_start": True}
    variants = [target, target | {"id": "uhost-5090", "gpu_model": "5090", "gpu": 2, "cpu": 32, "memory_gb": 96}]
    with storage.connect(db) as conn, storage.transaction(conn):
        snapshot(conn, "instances", PROJECT, variants)
    items = web.get("/api/instances", params={"project_id": PROJECT}).json()["data"]
    assert len(items) == len(variants)
    for item in items:
        assert [(profile["spec"], profile["cpu"], profile["memory_gb"])
                for profile in item["cardless_profiles"]] == expected


@pytest.mark.parametrize("override", [
    {"charge_type": "Spot"}, {"charge_type": "Month"}, {"gpu_model": "NVIDIA A800"},
    {"support_without_gpu_start": False}, {"support_without_gpu_start": None},
])
def test_cardless_eligibility_applies_even_in_a_supported_zone(console, override):
    web, db, _ = console
    target = INSTANCE | {"support_without_gpu_start": True} | override
    with storage.connect(db) as conn, storage.transaction(conn):
        snapshot(conn, "instances", PROJECT, [target])
    [item] = web.get("/api/instances", params={"project_id": PROJECT}).json()["data"]
    assert item["cardless_profiles"] == []
    denied = web.post("/api/actions", headers={"X-CSRF-Token": "csrf"}, json={
        "project_id": PROJECT, "instance_id": target["id"], "kind": "start", "boot_mode": "without_gpu",
        "without_gpu_spec": "B", "request_id": "cardless-ineligible",
    })
    assert denied.status_code == 422


def test_cardless_defaults_preserve_compatible_profile_records(console):
    _, db, _ = console
    profile = {"spec": "B", "cpu": 8, "memory_gb": 16, "standard_hourly_cny": "0.55"}
    wrong_tier = {"spec": "A", "cpu": 2, "memory_gb": 4}
    wrong_size = {"spec": "C", "cpu": 2, "memory_gb": 4}
    unknown = ("other-region", "other-zone")
    scope = ZONE["region"], ZONE["zone"]
    with storage.connect(db) as conn, storage.transaction(conn):
        cardless.record_profile(conn, ZONE | profile)
        cardless.record_profile(conn, {"region": unknown[0], "zone": unknown[1]} | profile)
        for invalid in (wrong_tier, wrong_size):
            with pytest.raises(ValueError, match="已知规格不符"):
                cardless.record_profile(conn, ZONE | invalid)
            conn.execute("INSERT INTO cardless_profiles VALUES (?,?,?,?,?,NULL)",
                         (*scope, invalid["spec"], invalid["cpu"], invalid["memory_gb"]))
        resolved = cardless.profiles(conn)
    assert resolved[scope] == [profile, {"spec": "C", "cpu": 4, "memory_gb": 8, "standard_hourly_cny": "0.30"}]
    assert resolved[unknown] == [profile]


class ResizeCloud:
    last_request_id = "request-example"

    def __init__(self, uncertain=False):
        self.instance = INSTANCE.copy()
        self.amount = Decimal("-1.25")
        self.calls = 0
        self.uncertain = uncertain

    def instances(self, project_id, zones):
        assert project_id == PROJECT and zones == [ZONE]
        return [self.instance.copy()]

    def capacity(self, spec):
        assert all(spec[key] == value for key, value in NEXT.items()) and spec["disks"]
        return True

    def resize_quote(self, instance, target):
        assert instance["id"] == INSTANCE["id"] and target == NEXT
        return self.amount

    def resize_instance(self, instance, target):
        self.calls += 1
        self.instance |= target
        if self.uncertain:
            raise CloudError("transport_error", uncertain=True)
        return instance["id"]


@pytest.mark.parametrize("uncertain", [False, True])
def test_resize_api_queues_one_write_and_reads_back_original_instance(console, uncertain):
    web, db, now = console
    path = "/api/instances/" + INSTANCE["id"]
    params = {"project_id": PROJECT}
    csrf = {"X-CSRF-Token": "csrf"}
    cloud = ResizeCloud(uncertain)
    runner = TaskRunner(TaskStore(db, clock=lambda: now[0]), cloud)
    [target] = web.get(path + "/resize-options", params=params).json()["options"]
    response = web.post(path + "/resize-quotes", params=params, headers=csrf, json={"spec_id": target["id"]})
    assert response.status_code == 202
    pending = response.json()
    assert pending["state"] == "queued"
    quote_tick(db, cloud)
    quote = web.get(path + "/resize-quotes/" + pending["id"], params=params).json()
    assert quote["state"] == "ready" and quote["amount"] == "-1.25" and quote["available"] is True
    body = {"quote_id": quote["id"], "request_id": "test-resize"}
    submitted = web.post(path + "/resize", params=params, headers=csrf, json=body)
    assert submitted.status_code == 202
    run = submitted.json()
    assert web.post(path + "/resize", params=params, headers=csrf, json=body).json()["id"] == run["id"]
    blocked = web.post("/api/actions", headers=csrf, json={
        "project_id": PROJECT, "instance_id": INSTANCE["id"], "kind": "start", "request_id": "test-start",
    })
    assert blocked.status_code == 409
    runner.tick()
    assert runner.store.get_run(run["id"])["phase"] == "verify_resize"
    now[0] += 60
    runner.tick()
    result = runner.store.get_run(run["id"])
    assert result["state"] == "succeeded"
    assert result["result"]["disposition"] == ("resize_observed" if uncertain else "resized")
    assert cloud.calls == 1 and cloud.instance["state"] == "Stopped"


def test_stock_projection_and_archived_detail_survive_active_snapshot_replacement(console):
    web, db, now = console
    params = {"project_id": PROJECT}
    with storage.connect(db) as conn, storage.transaction(conn):
        availability.record_pool(conn, ZONE["region"], NOW, provider_updated_at=NOW,
                                 counts={(ZONE["zone"], INSTANCE["gpu_model"], "exclusive"): 0})
    [item] = web.get("/api/instances", params=params).json()["data"]
    assert item["stock"]["remaining_cards"] == 0 and not item["stock"]["available"]
    with storage.connect(db) as conn, storage.transaction(conn):
        instance_history.observe(conn, PROJECT, [INSTANCE | {"ssh": {"command": "secret"}}], [], NOW + 2)
        snapshot(conn, "instances", PROJECT, [], NOW + 2)
    now[0] += 2
    [item] = web.get("/api/instance-history", params=params).json()["data"]
    assert item["archive_status"] == "missing" and "ssh" not in item
    detail = web.get("/api/instances/" + INSTANCE["id"], params=params).json()
    assert detail["data"] == item and detail["idle_snooze"] is None
    with storage.connect(db) as conn, storage.transaction(conn):
        instance_history.save(conn, INSTANCE, "released", NOW + 3, "test-run")
        snapshot(conn, "instances", PROJECT, [INSTANCE], NOW + 3)
    assert web.get("/api/instances", params=params).json()["data"] == []
    [item] = web.get("/api/instance-history", params=params).json()["data"]
    assert item["archive_status"] == "released" and item["release_run_id"] == "test-run"
    assert web.get("/api/instance-history", params={"project_id": "other"}).json()["data"] == []


def test_stopped_instance_probes_its_saved_configuration_without_boot_history(console):
    web, db, now = console
    params = {"project_id": PROJECT}
    instance = INSTANCE | {"cpu_platform": "Intel/EmeraldRapids"}
    observed = []

    class Cloud:
        def capacity(self, spec):
            observed.append(spec)
            return False

    with storage.connect(db) as conn, storage.transaction(conn):
        snapshot(conn, "instances", PROJECT, [
            instance,
            (INSTANCE | NEXT) | {"id": "uhost-running", "state": "Running"},
            (INSTANCE | NEXT) | {"id": "uhost-incomplete", "image_id": None},
        ])
        availability.record_pool(conn, ZONE["region"], NOW, provider_updated_at=NOW,
                                 counts={(ZONE["zone"], instance["gpu_model"], "exclusive"): 0})
    collector = AssetCollector(db, Cloud())
    collector.probe_capacity([PROJECT], NOW)
    collector.probe_capacity([PROJECT], NOW + 1)
    [spec] = observed
    assert {field: spec[field] for field in ("gpu", "cpu", "memory_gb", "cpu_platform", "image_id")} == {
        field: instance[field] for field in ("gpu", "cpu", "memory_gb", "cpu_platform", "image_id")
    }
    assert spec["disks"] == [{"purpose": "system", "type": "CLOUD_SSD", "size_gb": 100}]
    item = next(item for item in web.get("/api/instances", params=params).json()["data"] if item["id"] == instance["id"])
    assert item["stock"]["remaining_cards"] == 0
    assert item["stock"]["spec_available"] is False
    assert item["stock"]["spec_checked_at"] == NOW


def test_stopped_instance_probes_last_gpu_configuration_after_cardless_boot_and_restart(console):
    web, db, now = console
    original = INSTANCE | {"support_without_gpu_start": True, "cpu_platform": "Intel/EmeraldRapids"}
    cardless_run = original | {"state": "Running", "gpu_mode": "without_gpu", "gpu": 0, "cpu": 4,
                              "memory_gb": 8, "cpu_platform": "Amd/Epyc2", "start_time": NOW}
    stopped = cardless_run | {"state": "Stopped", "gpu_mode": None, "image_id": "image-new"}
    observed = []

    class Cloud:
        current = original

        def instances(self, project_id):
            return [self.current.copy()]

        def monitor(self, project_id, region, instance_ids):
            return []

        def instance_prices(self, project_id, instances):
            return {}

        def capacity(self, spec):
            observed.append(spec)
            return spec["gpu"] == original["gpu"]

    cloud = Cloud()
    assets = AssetCollector(db, cloud)

    def sync(instance):
        now[0] += 1
        cloud.current = instance
        Collector(db, cloud)._instances(PROJECT, {"retention_days": 30}, 0)

    def stock():
        [item] = web.get("/api/instances", params={"project_id": PROJECT}).json()["data"]
        assert (item["gpu"], item["cpu"], item["memory_gb"]) == (0, 4, 8)
        return item["stock"]

    sync(original)
    sync(cardless_run)
    sync(stopped)
    with storage.connect(db) as conn, storage.transaction(conn):
        availability.record_pool(conn, ZONE["region"], now[0], provider_updated_at=now[0],
                                 counts={(ZONE["zone"], original["gpu_model"], "exclusive"): 25})
    assert stock()["spec_available"] is None
    assert stock()["required_cards"] == 1
    assets.probe_capacity([PROJECT], now[0])
    [spec] = observed
    assert {field: spec[field] for field in ("gpu", "cpu", "memory_gb", "cpu_platform")} == {
        field: original[field] for field in ("gpu", "cpu", "memory_gb", "cpu_platform")
    }
    assert spec["image_id"] == stopped["image_id"]
    assert stock()["spec_available"] is True and stock()["uses_saved_spec"] is True

    sync(original | NEXT)
    sync(stopped)
    assert stock()["required_cards"] == 2 and stock()["spec_available"] is None
    assets.probe_capacity([PROJECT], now[0])
    assert stock()["spec_available"] is False
    sync(stopped | {"zone": "other-zone"})
    assert stock()["required_cards"] is None and stock()["uses_saved_spec"] is False


@pytest.mark.parametrize("gpu", [0, None])
def test_stopped_instance_with_unknown_boot_configuration_still_shows_its_pool(console, gpu):
    web, db, now = console
    cardless = INSTANCE | {"gpu": gpu, "cpu": 8, "memory_gb": 16}
    spot = cardless | {"id": "uhost-spot", "charge_type": "Spot"}
    cpu_only = cardless | {"id": "uhost-cpu", "gpu_model": None}
    running = cardless | {"id": "uhost-running", "state": "Running"}
    with storage.connect(db) as conn, storage.transaction(conn):
        snapshot(conn, "instances", PROJECT, [cardless, spot, cpu_only, running])
        availability.record_pool(conn, ZONE["region"], NOW, provider_updated_at=NOW,
                                 counts={(ZONE["zone"], "4090", "exclusive"): 25, (ZONE["zone"], "4090", "spot"): 0})

    def items():
        return {item["id"]: item for item in web.get("/api/instances", params={"project_id": PROJECT}).json()["data"]}

    current = items()
    stock = current[INSTANCE["id"]]["stock"]
    assert (stock["pool"], stock["state"], stock["remaining_cards"]) == ("exclusive", "known", 25)
    assert stock["required_cards"] is stock["spec_available"] is stock["spec_checked_at"] is None
    assert current[INSTANCE["id"]]["gpu"] == gpu
    assert current["uhost-spot"]["stock"]["remaining_cards"] == 0
    assert current["uhost-cpu"]["stock"] is current["uhost-running"]["stock"] is None
    now[0] += availability.POOL_FRESHNESS + 1
    expired = items()[INSTANCE["id"]]["stock"]
    assert expired["state"] == "stale" and expired["remaining_cards"] is None


def test_platform_wire_contract_for_resize_and_udisk_price():
    wire = Wire({"Price": -1.25}, {"UHostId": INSTANCE["id"]}, {
        "TotalCount": 2, "DataSet": [
            {"UDiskId": "boot", "IsBoot": "True"},
            {"UDiskId": "data", "IsBoot": "False", "Size": 100, "DiskType": "CLOUD_SSD",
             "Status": "Available", "ChargeType": "Postpay"},
        ],
    }, {"DataSet": [{"ChargeType": "Postpay", "ChargeName": "UDisk", "Price": 5, "ListPrice": 6}]})
    cloud = client(wire)
    assert cloud.resize_quote(INSTANCE, NEXT) == Decimal("-1.25")
    assert cloud.resize_instance(INSTANCE, NEXT) == INSTANCE["id"]
    [disk] = cloud.data_disks(INSTANCE)
    assert disk["mounted_instance_ids"] == [] and disk["type"] == "SSDDataDisk"
    assert cloud.storage_rates(INSTANCE | disk | {"kind": "udisk"}) == (Decimal("0.05"), Decimal("0.06"))
    (preview, _), (execute, retries) = wire.sent[:2]
    assert (preview["CPU"], preview["GPU"], preview["Memory"]) == ("32", "2", "131072")
    assert (execute["Cpu"], execute["Gpu"], execute["Memory"]) == ("32", "2", "131072")
    assert retries == 0


def test_storage_snapshot_keeps_mounts_distinct_and_falls_back_to_standard_price(console):
    from gpu_console.storage import instance_assets

    web, db, now = console
    disk = ZONE | {"project_id": PROJECT, "kind": "udisk", "id": "volume", "name": "Detached disk",
                   "type": "SSDDataDisk", "size": 100, "charge_type": "Postpay",
                   "observed_at": NOW, "mounted_instance_ids": [], "stale": False}
    cfs = disk | {"kind": "cfs", "type": "CFS", "id": "cfs", "charge_type": "Day",
                  "mounted_instance_ids": [INSTANCE["id"], "other"]}
    with storage.connect(db) as conn:
        snapshot(conn, "storage", PROJECT, [disk, cfs])
        key = instance_assets.key(instance_assets.price_scope(disk))
        conn.executemany("INSERT INTO storage_prices VALUES (?,?,?,?,?)", [
            (key, "account", "0.04", NOW, NOW - 86400),
            (key, "standard", "0.05", NOW, NOW),
        ])
    rows = web.get("/api/instance-storage", params={"project_id": PROJECT}).json()["data"]
    assert rows[0]["mounted_instance_ids"] == [] and rows[0]["instances"] == []
    assert rows[0]["price"]["source"] == "standard" and rows[0]["price"]["amount"] == "0.05"
    assert rows[1]["instances"] == [
        {"id": INSTANCE["id"], "name": INSTANCE["name"], "active": True},
        {"id": "other", "name": "other", "active": False},
    ]
    now[0] += 601
    assert all(row["stale"] for row in web.get("/api/instance-storage", params={"project_id": PROJECT}).json()["data"])


def test_storage_usage_follows_the_whole_device_and_waits_for_a_new_sample_after_resize(console):
    web, db, now = console
    disk = {"id": "volume", "purpose": "data", "type": "SSDDataDisk", "size_gb": 200, "drive": "vdb"}
    resource = ZONE | {"project_id": PROJECT, "id": disk["id"], "kind": "udisk", "name": "Data",
                       "size": 200, "mounted_instance_ids": [INSTANCE["id"]], "observed_at": NOW}
    instance = INSTANCE | {"state": "Running", "start_time": NOW - 600, "instance_price_per_hour": "1.00"}

    def current_instance():
        return instance | {"disks": [*INSTANCE["disks"], disk.copy()]}

    class Cloud:
        points = []

        def instances(self, project_id):
            return [current_instance()]

        def monitor(self, project_id, region, instance_ids):
            return self.points

    cloud = Cloud()
    collector = Collector(db, cloud)

    def point(device, mount, percent, sampled_at=None):
        return {"project_id": PROJECT, "instance_id": INSTANCE["id"], "metric": "cloudwatch_data_disk_used_per",
                "disk_device": device, "label": f"{device} · {mount}", "value": percent,
                "sampled_at": now[0] - 10 if sampled_at is None else sampled_at}

    def update(*points):
        cloud.points = points
        collector._instances(PROJECT, {"retention_days": 30}, 0)
        return read()

    def read():
        return web.get("/api/instance-storage", params={"project_id": PROJECT}).json()["data"][0]

    with storage.connect(db) as conn:
        snapshot(conn, "storage", PROJECT, [resource])
        snapshot(conn, "instances", PROJECT, [current_instance()])

    row = update(point("/dev/vdb", "/vol", 75), point("/dev/vda2", "/data", 99), point("/dev/vdb1", "/part", 85))
    assert row["usage"] == {"used_gb": 150, "percent": 75, "sampled_at": NOW - 10}
    assert row["usage_state"] == "estimated"
    assert update(point("/dev/vdb", "/vol", 75), point("/dev/vdb", "/vol2", 68))["usage_state"] == "no_sample"
    assert update(point("/dev/vdb", "/vol", 0))["usage"]["used_gb"] == 0

    now[0] += 111
    assert read()["usage_state"] == "stale"
    disk["size_gb"] = resource["size"] = 300
    with storage.connect(db) as conn:
        snapshot(conn, "storage", PROJECT, [resource])
    assert update(point("/dev/vdb", "/vol", 50))["usage"] is None
    now[0] += 30
    assert update(point("/dev/vdb", "/vol", 50))["usage"]["used_gb"] == 150
    instance["state"] = "Stopped"
    assert update()["usage_state"] == "stopped"
