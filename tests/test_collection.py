import json

from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError, GpuInventory
from gpu_console.api import create_app
from gpu_console.application import watches
from gpu_console.config import Config
from gpu_console.storage import availability, catalog, refresh
from gpu_console.worker.capacity import CapacityCollector
from gpu_console.worker.collection import Collector, initialize


PROJECT = "org-test"


def instance(iid="uhost-test", *, region="cn-wlcb", gpu=2):
    return {
        "id": iid, "project_id": PROJECT, "region": region, "zone": f"{region}-01",
        "state": "Running", "gpu": gpu, "start_time": 1000, "gpu_mode": None,
        "gpu_model": "4090", "name": "训练节点",
    }


def point(sampled_at, label, value=0):
    return {
        "project_id": PROJECT, "instance_id": "uhost-test",
        "metric": "cloudwatch_gpu_util", "label": label, "sampled_at": sampled_at, "value": value,
    }


def all_cards(sampled_at, value=0):
    return [point(sampled_at, label, value) for label in ("GPU-1", "GPU-2")]


class Cloud:
    def __init__(self):
        self.rows = [instance()]
        self.points = []
        self.batches = []
        self.capacity_calls = []
        self.available = True
        self.partial = False
        self.monitor_error = False

    def projects(self):
        return [{"id": PROJECT, "name": "test"}]

    def zones(self):
        return []

    def catalog(self, *, zones=None):
        return [{"status": "Normal"}]

    def instances(self, _project_id):
        if self.partial:
            raise CloudError("incomplete_inventory")
        return [dict(row) for row in self.rows]

    def instance_prices(self, _project_id, _instances):
        return {}

    def monitor(self, project_id, region, ids):
        self.batches.append((project_id, region, ids))
        if self.monitor_error:
            raise CloudError("503")
        return [point for point in self.points if point["instance_id"] in ids]

    def capacity(self, spec):
        self.capacity_calls.append(spec)
        return self.available


def setup(tmp_path, monkeypatch):
    path = tmp_path / "console.sqlite3"
    storage.initialize(path)
    with storage.connect(path) as conn:
        initialize(conn)
        storage.set_settings(conn, {"idle_minutes": 2, "idle_warmup_minutes": 1})
    time = [1000]
    monkeypatch.setattr(storage, "now", lambda: time[0])
    cloud = Cloud()
    return time, cloud, Collector(path, cloud)


def test_pool_history_uses_provider_buckets_and_current_state_expires(tmp_path):
    db = tmp_path / "pools.sqlite3"
    storage.initialize(db)
    region, zone = "cn-wlcb", "cn-wlcb-01"
    start = 1_800_000_005
    exclusive, spot = (zone, "5090", "exclusive"), (zone, "5090", "spot")

    def by_pool(conn, now):
        return {row["pool"]: row for row in availability.pool_inventory(conn, now) if row["gpu_model"] == "5090"}

    with storage.connect(db) as conn:
        availability.record_pool(conn, region, start + 5, provider_updated_at=start, counts={exclusive: 2, spot: 0})
        rows = by_pool(conn, start + 5)
        assert rows["spot"]["remaining_cards"] == 0 and rows["spot"]["state"] == "known"
        availability.record_pool(conn, region, start + 100, provider_updated_at=start, counts={exclusive: 2, spot: 0})
        assert by_pool(conn, start + 100)["exclusive"] == {
            "region": region, "zone": zone, "gpu_model": "5090", "pool": "exclusive", "remaining_cards": 2,
            "state": "known", "provider_updated_at": start, "observed_at": start + 100,
        }
        assert by_pool(conn, start + 601)["exclusive"]["state"] == "stale"
        assert by_pool(conn, start + 601)["exclusive"]["remaining_cards"] is None

        # A newer timestamp within the same source bucket replaces that bucket without adding a sample.
        availability.record_pool(conn, region, start + 150, provider_updated_at=start + 120, counts={exclusive: 1})
        assert by_pool(conn, start + 150)["spot"]["state"] == "unknown"
        availability.record_pool(conn, region, start + 650, provider_updated_at=start + 630, counts={exclusive: 0})
        assert availability.pool_history(conn, region, *exclusive, start, start + 650) == [
            {"provider_updated_at": start + 120, "remaining_cards": 1},
            {"provider_updated_at": start + 630, "remaining_cards": 0},
        ]
        assert availability.pool_history(conn, region, *spot, start, start + 650) == [
            {"provider_updated_at": start, "remaining_cards": 0},
        ]
        availability.trim(conn, start + availability.RETENTION_SECONDS + 1000)
        assert availability.pool_inventory(conn, start + availability.RETENTION_SECONDS + 1000) == []
        assert availability.pool_history(conn, region, *exclusive, start, start + 650) == []


def test_pool_collector_isolates_catalog_errors_and_reuses_recent_refresh(tmp_path, monkeypatch):
    now, cloud, collector = setup(tmp_path, monkeypatch)
    region, zone, other, other_zone = "cn-wlcb", "cn-wlcb-01", "cn-other", "cn-other-01"
    monkeypatch.setattr(cloud, "zones", lambda: [
        {"region": region, "zone": zone, "zone_id": 10027},
        {"region": region, "zone": "ambiguous", "zone_id": 3},
        {"region": region, "zone": "unmapped", "zone_id": None},
        {"region": other, "zone": other_zone, "zone_id": 10028},
        {"region": other, "zone": "ambiguous", "zone_id": 3},
    ])

    def broken_catalog(*, zones):
        raise CloudError("503")

    monkeypatch.setattr(cloud, "catalog", broken_catalog)
    calls = []
    expected_mapping = {region: {10027: zone}, other: {10028: other_zone}}

    def inventory(target, zone_ids):
        calls.append(target)
        assert zone_ids == expected_mapping[target]
        if target == region and now[0] > 1000:
            raise CloudError("503")
        zone_name = next(iter(zone_ids.values()))
        return GpuInventory(now[0] - 15, {
            (zone_name, "5090", "exclusive"): 2,
            (zone_name, "5090", "spot"): 0,
            (zone_name, "4090", "exclusive"): 3,
        }, frozenset({"4090"}))

    monkeypatch.setattr(cloud, "gpu_inventory", inventory, raising=False)
    with storage.connect(collector.path) as conn:
        specs = [{"gpu": 1, "cpu": 16, "memory_gb": 64}]
        directory = [
            {"region": area, "zone": location, "gpu_model": model, "instance_type": kind, "specs": specs}
            for area, location in ((region, zone), (other, other_zone))
            for model, kind in (("5090", "uhost"), ("5090", "spot"), ("4090", "uhost"))
        ]
        conn.execute("INSERT INTO snapshots VALUES ('catalog','',?,?,NULL)", (json.dumps(directory), 950))
        first = refresh.submit(conn, "pool", None, [""])
        collector.tick()
        assert storage.snapshot(conn, "catalog")["error"]
        assert refresh.get(conn, first)["state"] == "succeeded"
        items = availability.pool_inventory(conn, now[0])
        north = {(row["gpu_model"], row["pool"]): row for row in items if row["zone"] == zone}
        assert north["5090", "exclusive"]["remaining_cards"] == 2
        assert north["5090", "spot"]["remaining_cards"] == 0
        assert ("4090", "spot") not in north
        assert conn.execute("SELECT COUNT(*) FROM availability_pool_items").fetchone()[0] == 6

        now[0] += 30
        cached = refresh.submit(conn, "pool", None, [""])
        collector.tick()
        assert refresh.get(conn, cached)["state"] == "succeeded"
        assert calls == [other, region]
        assert storage.snapshot(conn, "pool")["success_at"] == 1000

        now[0] += 270
        failed = refresh.submit(conn, "pool", None, [""])
        collector.tick()
        assert refresh.get(conn, failed)["state"] == "failed"
        items = availability.pool_inventory(conn, now[0])
        assert all(row["state"] == "unknown" for row in items if row["region"] == region)
        assert any(row["remaining_cards"] == 0 for row in items if row["region"] == other)
        assert availability.pool_history(conn, region, zone, "5090", "spot", 0, now[0]) == [
            {"provider_updated_at": 985, "remaining_cards": 0},
        ]


def test_upgrade_removes_uncatalogued_pool_but_keeps_previously_listed_history(tmp_path, monkeypatch):
    path = tmp_path / "pool-migration.sqlite3"
    now = 1800
    monkeypatch.setattr(storage, "now", lambda: now)
    storage.initialize(path)
    region, zone = "cn-test", "cn-test-01"
    official = {"region": region, "zone": zone, "gpu_model": "5090", "instance_type": "uhost",
                "specs": [{"gpu": 4, "cpu": 16, "memory_gb": 64}]}
    retired = official | {"gpu_model": "4090"}
    with storage.connect(path) as conn:
        availability.record_catalog(conn, [official, retired], now - 300)
        conn.execute("INSERT INTO snapshots VALUES ('catalog','',?,?,NULL)", (json.dumps([official]), now))
        availability.record_pool(conn, region, now, provider_updated_at=now, counts={
            (zone, "5090", "exclusive"): 0, (zone, "5090", "spot"): 14, (zone, "4090", "exclusive"): 0,
        })
        assert conn.execute("SELECT COUNT(*) FROM availability_pool_items").fetchone()[0] == 3
    storage.initialize(path)
    with storage.connect(path) as conn:
        rows = conn.execute("SELECT gpu_model,pool,catalog_listed FROM availability_pool_items ORDER BY gpu_model")
        assert [tuple(row) for row in rows] == [("4090", "exclusive", 0), ("5090", "exclusive", 1)]
        assert availability.pool_inventory(conn, now)[0]["remaining_cards"] == 0
        assert availability.pool_history(conn, region, zone, "4090", "exclusive", 0, now) == [
            {"provider_updated_at": now, "remaining_cards": 0},
        ]
        assert availability.pool_history(conn, region, zone, "5090", "spot", 0, now) == []
        assert not conn.execute("SELECT 1 FROM availability_pool_peaks WHERE pool='spot'").fetchone()
        availability.reconcile_pool_catalog(conn, availability.catalog_pools([official, retired]))
        recovered = next(row for row in availability.pool_inventory(conn, now) if row["gpu_model"] == "4090")
        assert recovered["state"] == "unknown" and recovered["remaining_cards"] is None


WATCH = {"region": "cn-wlcb", "zone": "cn-wlcb-01", "gpu_model": "4090",
         "gpu": 2, "cpu": 16, "memory_gb": 64, "charge_type": "Postpay"}


def configure_watch_pool(monkeypatch, cloud):
    listing = WATCH | {"instance_type": "uhost", "specs": [
        {"gpu": 2, "cpu": 16, "memory_gb": 64}, {"gpu": 2, "cpu": 32, "memory_gb": 128},
    ], "disks": [{"type": "CLOUD_SSD", "purpose": "system", "default_size_gb": 100}]}
    monkeypatch.setattr(cloud, "catalog", lambda *, zones: [listing])
    monkeypatch.setattr(cloud, "zones", lambda: [{"region": "cn-wlcb", "zone": "cn-wlcb-01", "zone_id": 1}])


def test_watch_shares_pool_refresh_and_unknown_keeps_instance_snapshot(tmp_path, monkeypatch):
    time, cloud, collector = setup(tmp_path, monkeypatch)
    configure_watch_pool(monkeypatch, cloud)
    calls = []
    def inventory(region, zone_ids):
        calls.append((region, zone_ids))
        return GpuInventory(time[0], {("cn-wlcb-01", "4090", "exclusive"): 4}, frozenset())
    monkeypatch.setattr(cloud, "gpu_inventory", inventory, raising=False)
    with storage.connect(collector.path) as conn:
        conn.executemany("INSERT INTO watches VALUES (?,?,?,?)", [
            (wid, pid, wid, json.dumps(WATCH)) for wid, pid in
            (("watch1", PROJECT), ("watch2", PROJECT), ("watch3", "org-other"))
        ])
    collector.tick()
    assert len(calls) == 1 and not cloud.capacity_calls
    with storage.connect(collector.path) as conn:
        assert [(row["binding_status"], row["remaining_cards"]) for row in
                watches.list_project(conn, PROJECT, time[0])] == [("bound", 4), ("duplicate", None)]
        assert watches.list_project(conn, "org-other", time[0])[0]["remaining_cards"] == 4

    def unavailable(*args):
        raise CloudError("503")
    monkeypatch.setattr(cloud, "gpu_inventory", unavailable)
    cloud.partial = True
    time[0] += 300
    with storage.connect(collector.path) as conn:
        conn.execute("INSERT INTO refresh_requests VALUES ('instances',?,?)", (PROJECT, time[0]))
    collector.tick()
    with storage.connect(collector.path) as conn:
        row = watches.list_project(conn, PROJECT, time[0], history=True)[0]
        assert row["available"] is None and row["state"] == "unknown"
        assert row["history"] == []
        assert not conn.execute("SELECT 1 FROM stock_history").fetchone()
        instances = storage.snapshot(conn, "instances", PROJECT)
        assert [item["id"] for item in instances["data"]] == ["uhost-test"]
        assert instances["success_at"] == 1000 and instances["error"]


def test_watch_refresh_receipts_share_one_matrix_and_wait_for_post_request_results(tmp_path, monkeypatch):
    clock, cloud, collector = setup(tmp_path, monkeypatch)
    configure_watch_pool(monkeypatch, cloud)
    config = Config(tmp_path, tmp_path / "secrets", tmp_path / "static", False, False)
    body = {"username": "operator", "password": "stub-password-12345"}
    with TestClient(create_app(config)) as client, storage.connect(collector.path) as conn:
        conn.execute("INSERT INTO administrators VALUES (1,?,?)",
                     (body["username"], PasswordHash.recommended().hash(body["password"])))
        conn.executemany("INSERT INTO watches VALUES (?,?,?,?)", [
            ("target", PROJECT, "目标", json.dumps(WATCH)), ("outside", "other", "其他项目", json.dumps(WATCH)),
        ])
        login = client.post("/api/login", json=body)
        headers = {"x-csrf-token": login.json()["csrf_token"]}
        request = {"kind": "stock", "project_id": PROJECT, "watch_id": "target"}
        assert client.post("/api/refresh", json=request | {"watch_id": "outside"}, headers=headers).status_code == 404

        def submit():
            response = client.post("/api/refresh", json=request, headers=headers)
            assert response.status_code == 202
            return "/api/refresh/" + response.json()["id"]

        conn.execute("INSERT INTO watches VALUES (?,?,?,?)", (
            "second-sku", PROJECT, "双卡大内存", json.dumps(WATCH | {"cpu": 32, "memory_gb": 128}),
        ))
        monkeypatch.setattr(cloud, "gpu_inventory", lambda *args: GpuInventory(
            clock[0], {("cn-wlcb-01", "4090", "exclusive"): 12}, frozenset(),
        ), raising=False)
        collector.tick()
        probe = CapacityCollector(collector.path, cloud)
        scope = 'system:' + catalog._image_scope(PROJECT, "cn-wlcb", "cn-wlcb-01")
        conn.execute("INSERT INTO snapshots VALUES ('images',?,?,?,NULL)", (
            scope, json.dumps([{"id": "image-test", "name": "Ubuntu", "runtime": "vm"}]), clock[0],
        ))
        first = submit()
        batch = client.post("/api/refresh", json=request | {"watch_id": "second-sku"}, headers=headers).json()["id"]
        late = []
        calls = []

        def matrix(spec):
            calls.append(spec)
            if not late:
                late.append(submit())
            return {(2, 16, 64): False, (2, 32, 128): True}

        monkeypatch.setattr(cloud, "capacity_matrix", matrix, raising=False)
        probe.tick()
        assert calls == [{
            "project_id": PROJECT, "region": "cn-wlcb", "zone": "cn-wlcb-01", "gpu_model": "4090",
            "charge_type": "Postpay", "image_id": "image-test", "cpu_platform": "Auto",
            "disks": [{"purpose": "system", "type": "CLOUD_SSD", "size_gb": 100}],
        }]
        assert client.get(first).json()["state"] == "succeeded"
        assert client.get("/api/refresh/" + batch).json()["state"] == "succeeded"
        assert client.get(late[0]).json()["state"] == "queued"
        items = {item["id"]: item for item in client.get("/api/stock", params={"project_id": PROJECT}).json()["data"]}
        assert items["target"]["available"] is False
        assert items["second-sku"]["available"] is True
        trends = client.get("/api/dashboard/availability", params={
            "project_id": PROJECT, "card_threshold": 2,
        }).json()
        assert trends["items"][0]["available"] is True
        assert client.get("/api/dashboard/availability", params={"card_threshold": 4}).json()["items"] == []

        for now in (1001, 1010, 1020, 1059):
            clock[0] = now
            probe.tick()
            assert len(calls) == 1
        clock[0] = 1070
        probe.tick()
        assert len(calls) == 2
        assert client.get(late[0]).json()["state"] == "succeeded"


def test_monitor_batches_by_project_and_region(tmp_path, monkeypatch):
    _, cloud, collector = setup(tmp_path, monkeypatch)
    cloud.rows = [instance(f"uhost-{n}") for n in range(12)] + [
        instance("uhost-other", region="cn-sh2"),
    ]
    collector.tick()
    assert [(p, r, len(ids)) for p, r, ids in cloud.batches] == [
        (PROJECT, "cn-wlcb", 10), (PROJECT, "cn-wlcb", 2), (PROJECT, "cn-sh2", 1),
    ]


def test_idle_requires_every_card_after_warmup_and_latch_survives_restart(tmp_path, monkeypatch):
    time, cloud, collector = setup(tmp_path, monkeypatch)

    def poll(values):
        cloud.points = values
        collector.tick()
        with storage.connect(collector.path) as conn:
            inv = storage.snapshot(conn, "instances", PROJECT)["data"][0]
            return inv["gpu_mode"], conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]

    assert poll(all_cards(999)) == (None, 0)
    time[0] = 1060
    assert poll(all_cards(1060)) == ("with_gpu", 0)
    time[0] = 1120
    assert poll([point(1120, "GPU-1")]) == (None, 0)
    time[0] = 1180
    assert poll(all_cards(1180)) == ("with_gpu", 0)
    time[0] = 1240
    assert poll(all_cards(1240)) == ("with_gpu", 0)
    time[0] = 1300
    collector = Collector(collector.path, cloud)
    assert poll(all_cards(1300)) == ("with_gpu", 1)
    time[0] = 1360
    assert poll(all_cards(1360)) == ("with_gpu", 1)
    time[0] = 1540
    assert poll(all_cards(1360)) == (None, 1)
    time[0] = 1600
    assert poll(all_cards(1600, 25)) == ("with_gpu", 1)
    for timestamp in (1660, 1720, 1780):
        time[0] = timestamp
        assert poll(all_cards(timestamp)) == ("with_gpu", 2 if timestamp == 1780 else 1)
    with storage.connect(collector.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 21


def test_monitor_failure_keeps_history_but_clears_current_gpu_proof(tmp_path, monkeypatch):
    time, cloud, collector = setup(tmp_path, monkeypatch)
    time[0] = 1060
    cloud.points = all_cards(1060)
    collector.tick()
    cloud.monitor_error = True
    time[0] = 1120
    collector.tick()
    with storage.connect(collector.path) as conn:
        assert storage.snapshot(conn, "instances", PROJECT)["data"][0]["gpu_mode"] is None
        metrics = storage.snapshot(conn, "metrics", PROJECT)
        assert metrics["error"] and metrics["success_at"] == 1060
        assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 2
        assert conn.execute("SELECT first_idle_at FROM idle_state").fetchone()[0] is None


def test_capacity_worker_keeps_groups_fair_and_obeys_rate_limit(tmp_path, monkeypatch):
    clock, cloud, collector = setup(tmp_path, monkeypatch)
    configure_watch_pool(monkeypatch, cloud)
    base_catalog = cloud.catalog

    def listing(*, zones=None):
        [entry] = base_catalog(zones=zones)
        return [entry, entry | {"gpu_model": "5090"}]

    monkeypatch.setattr(cloud, "catalog", listing)
    monkeypatch.setattr(cloud, "gpu_inventory", lambda *args: GpuInventory(clock[0], {}, frozenset()), raising=False)
    with storage.connect(collector.path) as conn:
        scope = 'system:' + catalog._image_scope(PROJECT, "cn-wlcb", "cn-wlcb-01")
        conn.execute("INSERT INTO snapshots VALUES ('images',?,?,?,NULL)", (
            scope, json.dumps([{"id": "image-test", "name": "Ubuntu", "runtime": "vm"}]), clock[0],
        ))
    collector.tick()
    calls = []

    def matrix(spec):
        calls.append((clock[0], spec["gpu_model"]))
        if len(calls) == 1:
            raise CloudError("429", retryable=True)
        return {(2, 16, 64): True, (2, 32, 128): False}

    monkeypatch.setattr(cloud, "capacity_matrix", matrix, raising=False)
    probe = CapacityCollector(collector.path, cloud)
    for stamp in (1000, 1001, 1010, 1029, 1030, 1039, 1040):
        clock[0] = stamp
        probe.tick()
    assert calls == [(1000, "4090"), (1030, "5090"), (1040, "4090")]
    with storage.connect(collector.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM capacity_profiles WHERE checked_at IS NOT NULL").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM image_sources WHERE kind<>'system'").fetchone()[0] == 0


def test_capacity_worker_uses_official_container_images_in_pod_zone(tmp_path, monkeypatch):
    clock, cloud, collector = setup(tmp_path, monkeypatch)
    configure_watch_pool(monkeypatch, cloud)
    monkeypatch.setattr(cloud, "zones", lambda: [
        {"region": "cn-wlcb", "zone": "cn-wlcb-01", "zone_id": 1, "is_pod": True},
    ])
    monkeypatch.setattr(cloud, "gpu_inventory", lambda *args: GpuInventory(clock[0], {}, frozenset()), raising=False)
    collector.tick()
    probe = CapacityCollector(collector.path, cloud)
    calls = []

    def matrix(spec):
        calls.append(spec)
        return {(2, 16, 64): True}

    monkeypatch.setattr(cloud, "capacity_matrix", matrix, raising=False)
    scope = catalog._image_scope(PROJECT, "cn-wlcb", "cn-wlcb-01")
    with storage.connect(collector.path) as conn:
        conn.executemany("INSERT INTO snapshots VALUES ('images',?,?,?,NULL)", (
            (f"system:{scope}", json.dumps([{"id": "vm", "runtime": "vm"}]), clock[0]),
            (f"shared:{scope}", json.dumps([{"id": "community", "runtime": "container"}]), clock[0]),
        ))
        probe.tick()
        assert not calls
        assert [row["kind"] for row in conn.execute("SELECT kind FROM image_sources")] == ["app"]
        conn.execute("INSERT INTO snapshots VALUES ('images',?,?,?,NULL)", (
            f"app:{scope}", json.dumps([{"id": "official", "runtime": "container"}]), clock[0],
        ))
    clock[0] += 10
    probe.tick()
    assert [spec["image_id"] for spec in calls] == ["official"]
