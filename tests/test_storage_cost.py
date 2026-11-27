from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.storage import dashboard_cost, dashboard_cost_rollups, instance_assets, instance_history, maintenance, storage_cost
from gpu_console.worker.instance_assets import AssetCollector

from test_dashboard_cost import _cost_intervals
from test_instance_assets import INSTANCE, NOW, PROJECT, ZONE, console, snapshot

CFS_ZONE = {"region": "cn-sh2", "zone": "cn-sh2-03", "is_pod": True, "zone_id": 5001}
DISK = ZONE | {"id": "disk-test", "name": "Training data", "project_id": PROJECT, "kind": "udisk",
               "type": "SSDDataDisk", "size": 200, "charge_type": "Postpay",
               "status": "InUse", "mounted_instance_ids": [INSTANCE["id"]]}
CFS = CFS_ZONE | {"id": "cfs-test", "name": "Shared files", "project_id": PROJECT, "kind": "cfs",
                  "type": "CFS", "size": 100, "size_gib": 100, "charge_type": "Dynamic",
                  "mounted_instance_ids": [INSTANCE["id"], "another"]}


def quote(conn, row, amount, at, source="account"):
    conn.execute("INSERT OR REPLACE INTO storage_prices VALUES (?,?,?,?,?)",
                 (instance_assets.key(instance_assets.price_scope(row)), source, amount, at, at))


def observe(conn, at, *scopes, started_at=None, project_id=PROJECT):
    with storage.transaction(conn):
        storage_cost.observe(conn, project_id, [
            (scope["region"], scope["zone"], kind, at if started_at is None else started_at, at, rows)
            for scope, kind, rows in scopes
        ])


def disk_scope(*rows):
    return ZONE, "udisk", list(rows)


def test_legacy_rollups_include_storage_once_and_show_deleted_resources(console):
    _, db, now = console
    owner = INSTANCE["id"]
    other = INSTANCE | {"id": "another", "name": "Second GPU"}
    with storage.connect(db) as conn:
        quote(conn, DISK, "0.12", NOW)
        quote(conn, CFS, "0.24", NOW)
        _cost_intervals(conn, owner, NOW - 300, NOW, price="6", load=1.0)

    class Cloud:
        mounts = [owner]
        cfs = [CFS]

        def data_disks(self, scope):
            if scope["zone"] != ZONE["zone"]:
                return []
            return [DISK | {"mounted_instance_ids": self.mounts, "status": "InUse" if self.mounts else "Available"}]

        def describe_cfs(self, scope, *, inventory):
            assert inventory
            return self.cfs

    cloud = Cloud()
    collector = AssetCollector(db, cloud, estimate_costs=True)
    for i, mounts in enumerate(([owner], [owner], [], [], [other["id"]], [other["id"]])):
        now[0] = NOW + i * 300
        cloud.mounts = mounts
        if i == 5:
            cloud.cfs = []
        with storage.connect(db) as conn:
            disk_spec = {"id": DISK["id"], "purpose": "data", "type": "CLOUD_SSD", "size_gb": DISK["size"]}
            snapshot(conn, "instances", PROJECT, [
                INSTANCE | {"disks": [*INSTANCE["disks"], disk_spec], "disk_price_per_hour": "0.09"}, other,
            ], now[0])
        collector.poll_storage(PROJECT, [ZONE, CFS_ZONE])
    collector.poll_storage(PROJECT, [ZONE, CFS_ZONE])
    with storage.connect(db) as conn:
        instance_history.save(conn, other, "released", now[0])
        snapshot(conn, "instances", PROJECT, [INSTANCE], now[0])

        report = dashboard_cost.summary(conn, PROJECT, "today", now[0], include_details=False)
        scoped = dashboard_cost.summary(conn, PROJECT, "today", now[0], owner, include_details=False)
        detail = dashboard_cost.storage_details(conn, PROJECT, "today", now[0])
    assert report["estimated_cost_cny"] == "0.5000"
    assert report["storage_cost_cny"] == "0.1275"
    assert report["estimated_total_cost_cny"] == "0.6275"
    assert report["storage_cost_status"] == "partial"
    assert report["storage_data_through"] == now[0]
    assert report["days"][0]["estimated_total_cost_cny"] == report["estimated_total_cost_cny"]
    instances = {row["instance_id"]: row for row in report["instances"]}
    assert instances[owner]["storage_cost_cny"] == "0.0075"
    assert instances[owner]["estimated_total_cost_cny"] == "0.5075"
    assert instances["another"]["storage_cost_cny"] == "0.0100"
    assert instances["another"]["name"] == "Second GPU"
    assert not instances["another"]["current"]
    assert scoped["estimated_total_cost_cny"] == "0.5075"
    assert scoped["low_load_cost_ratio"] == report["low_load_cost_ratio"]
    assert detail["project_only_storage_cost_cny"] == "0.1100"
    rows = {row["resource_id"]: row for row in detail["data"]}
    assert rows["disk-test"]["price_sources"] == ["account", "actual"]
    assert rows["cfs-test"]["storage_cost_cny"] == "0.0800"
    assert rows["cfs-test"]["unpriced_storage_hours"] == pytest.approx(300 / 3600)
    assert dashboard_cost_rollups.refresh(db, now[0]) == 1
    with storage.connect(db) as conn:
        assert dashboard_cost.summary(conn, PROJECT, "today", now[0], include_details=False) == report


def test_scope_failure_resize_and_long_gap_do_not_extend_prices_or_duplicate_cost(console):
    _, db, now = console
    with storage.connect(db) as conn:
        quote(conn, DISK, "0.12", NOW)
        quote(conn, DISK | {"size": 400}, "0.24", NOW + 600)
        quote(conn, CFS, "0.24", NOW)
    class Cloud:
        size = 200
        failed = False

        def data_disks(self, scope):
            return [DISK | {"size": self.size}] if scope["zone"] == ZONE["zone"] else []

        def describe_cfs(self, scope, *, inventory):
            if self.failed:
                raise CloudError("transport_error")
            return [CFS]

    cloud = Cloud()
    collector = AssetCollector(db, cloud, estimate_costs=True)
    for elapsed, size, failed in ((0, 200, False), (300, 200, True), (600, 400, False),
                                  (900, 400, False), (1800, 400, False), (2100, 400, False)):
        now[0] = NOW + elapsed
        cloud.size, cloud.failed = size, failed
        collector.poll_storage(PROJECT, [ZONE, CFS_ZONE])
    with storage.connect(db) as conn:
        observe(conn, NOW + 2150, disk_scope(DISK), started_at=NOW + 1800)
        observe(conn, NOW + 2100, disk_scope(DISK))
        report = dashboard_cost.summary(conn, PROJECT, "today", now[0])
        scoped = dashboard_cost.summary(conn, PROJECT, "today", now[0], INSTANCE["id"])
    assert report["storage_cost_cny"] == "0.0900"
    assert report["unpriced_storage_hours"] == pytest.approx(300 / 3600)
    assert report["storage_scope_gap_hours"] == pytest.approx((3 * 900 + 600) / 3600)
    assert report["storage_data_through"] == NOW + 2100
    assert scoped["storage_scope_gap_hours"] == pytest.approx(900 / 3600)


def test_monthly_quote_splits_at_midnight_expiry_and_renewal(console):
    web, db, now = console
    midnight = int(datetime(2028, 3, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    starting_at = midnight - 300
    row = CFS | {"charge_type": "Month", "expires_at": midnight + 150}
    with storage.connect(db) as conn:
        quote(conn, row, "29", starting_at)
        observe(conn, starting_at, (CFS_ZONE, "cfs", [row]))
        renewed = row | {"expires_at": midnight + 31 * 86400}
        observe(conn, midnight + 300, (CFS_ZONE, "cfs", [renewed]))
        observe(conn, midnight + 600, (CFS_ZONE, "cfs", [renewed]))
        report = dashboard_cost.summary(conn, PROJECT, "7d", midnight + 600)
        today = Decimal(29) * 450 / (31 * 86400)
        previous_day = Decimal(29) * 300 / (29 * 86400)
        assert report["days"][-2]["storage_cost_cny"] == dashboard_cost.money(previous_day)
        assert report["days"][-1]["storage_cost_cny"] == dashboard_cost.money(today)
        assert report["storage_cost_cny"] == dashboard_cost.money(today + previous_day)
        assert report["unpriced_storage_hours"] == pytest.approx(150 / 3600)


@pytest.mark.parametrize(("charge", "amount", "expected"), [
    ("Dynamic", "3.6", "0.3000"), ("Day", "8.64", "0.0300"), ("Year", "366", "0.0035"),
])
def test_prepaid_single_period_rates_use_correct_units(tmp_path, charge, amount, expected):
    db = tmp_path / "rates.sqlite3"
    storage.initialize(db)
    at = int(datetime(2028, 2, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    resource = DISK | {"charge_type": charge}
    with storage.connect(db) as conn:
        quote(conn, resource, amount, at)
        observe(conn, at, disk_scope(resource))
        observe(conn, at + 300, disk_scope(resource))
        assert dashboard_cost.summary(conn, PROJECT, "today", at + 300)["storage_cost_cny"] == expected


def test_zero_rate_and_quote_history_are_distinct_from_missing_data(console):
    _, db, _ = console
    with storage.connect(db) as conn:
        observe(conn, NOW, disk_scope(DISK))
        quote(conn, DISK, "0", NOW + 300)
        observe(conn, NOW + 300, disk_scope(DISK))
        assert dashboard_cost.summary(conn, PROJECT, "today", NOW + 300)["storage_cost_cny"] is None
        observe(conn, NOW + 600, disk_scope(DISK))
        quote(conn, DISK, "0.12", NOW + 590)
        observe(conn, NOW + 900, disk_scope(DISK))
        assert dashboard_cost.summary(conn, PROJECT, "today", NOW + 900)["storage_cost_cny"] == "0.0000"
        observe(conn, NOW + 1200, disk_scope(DISK))
        report = dashboard_cost.summary(conn, PROJECT, "today", NOW + 1200)
        assert report["storage_cost_cny"] == "0.0100"
        assert report["unpriced_storage_hours"] == pytest.approx(300 / 3600)


def test_legacy_database_preserves_compute_and_combines_unrounded_costs(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    storage.initialize(db)
    at = NOW + 300
    with storage.connect(db) as conn:
        conn.execute("DROP TABLE storage_cost_samples")
        conn.execute("DROP TABLE storage_cost_daily")
        _cost_intervals(conn, INSTANCE["id"], NOW - 60, NOW, price="0.0024")
        quote(conn, DISK, "0.00048", NOW)
        snapshot(conn, "storage", PROJECT, [DISK], NOW)
    storage.initialize(db)
    with storage.connect(db) as conn:
        before = dashboard_cost.summary(conn, PROJECT, "today", NOW)
        assert before["estimated_cost_cny"] == "0.0000"
        assert before["storage_cost_status"] == "unobserved"
        assert before["storage_cost_cny"] is None
        observe(conn, NOW, disk_scope(DISK))
        observe(conn, at, disk_scope(DISK))
        report = dashboard_cost.summary(conn, PROJECT, "today", at)
        assert report["storage_cost_cny"] == report["estimated_cost_cny"] == "0.0000"
        assert report["estimated_total_cost_cny"] == "0.0001"
        assert dashboard_cost.summary(conn, "another-project", "today", at)["storage_cost_cny"] is None


def test_confirmed_empty_storage_has_zero_cost(tmp_path):
    db = tmp_path / "empty.sqlite3"
    storage.initialize(db)
    midnight = int(datetime(2028, 3, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    with storage.connect(db) as conn:
        observe(conn, midnight, disk_scope())
        observe(conn, midnight + 300, disk_scope())
        report = dashboard_cost.summary(conn, PROJECT, "today", midnight + 300)
        assert report["storage_cost_status"] == "complete"
        assert report["storage_cost_cny"] == "0.0000"
        assert report["estimated_total_cost_cny"] == "0.0000"


def test_restore_breaks_cost_intervals_and_keeps_saved_totals(tmp_path, monkeypatch):
    db, backup = tmp_path / "live" / "console.sqlite3", tmp_path / "before.sqlite3"
    storage.initialize(db)
    now = [NOW + 300]
    monkeypatch.setattr(storage, "now", lambda: now[0])
    with storage.connect(db) as conn:
        quote(conn, DISK, "0.12", NOW)
        observe(conn, NOW, disk_scope(DISK))
        observe(conn, NOW + 300, disk_scope(DISK))
    maintenance.backup(db, backup)
    now[0] = NOW + 350
    maintenance.restore(db, backup)
    with storage.connect(db) as conn:
        observe(conn, NOW + 450, disk_scope(DISK), started_at=NOW + 340)
        observe(conn, NOW + 600, disk_scope(DISK))
        observe(conn, NOW + 900, disk_scope(DISK))
        report = dashboard_cost.summary(conn, PROJECT, "today", NOW + 900)
        assert report["storage_cost_cny"] == "0.0200"
        assert report["storage_scope_gap_hours"] == pytest.approx(300 / 3600)
