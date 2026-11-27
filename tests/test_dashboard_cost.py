import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.api import create_app
from gpu_console.api.auth import COOKIE, digest
from gpu_console.application import rental_plan
from gpu_console.config import Config
from gpu_console.mcp import auth
from gpu_console.mcp.server import create_mcp_app
from gpu_console.storage import availability, cardless, dashboard_cost, dashboard_cost_rollups
from gpu_console.worker.collection import Collector

from test_collection import PROJECT, Cloud, instance, point
from test_dashboard_api import call_tool, seed_watch_catalog


MIDNIGHT = int(datetime(2026, 11, 23, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


def _cost_intervals(conn, instance_id, start, end, *, price="8.2", load=1.0, low_cards=("1", "2", "3")):
    conn.executemany(dashboard_cost.INSERT_INTERVAL, [
        (PROJECT, instance_id, at, at + 60, MIDNIGHT - 600, instance_id, "4090", 4, "Postpay",
         price, load, json.dumps(low_cards) if load is not None else None, 1, 1, 0, 0)
        for at in range(start, end, 60)
    ])


def test_cost_daily_backfill_reuses_totals_and_reclassifies_previous_day(tmp_path):
    db = tmp_path / "rollups.sqlite3"
    storage.initialize(db)
    early = MIDNIGHT + 120
    settled = MIDNIGHT + 1800
    with storage.connect(db) as conn, storage.transaction(conn):
        _cost_intervals(conn, "mixed", MIDNIGHT - 600, early)
        # Simulate history from before the queue existed.
        dashboard_cost_rollups.reset(conn)
    assert dashboard_cost_rollups.refresh(db, early, limit=1) == 1
    with storage.connect(db) as conn:
        assert dashboard_cost.summary(conn, PROJECT, "7d", early)["low_load_cost_cny"] == "0.0000"
    assert dashboard_cost_rollups.refresh(db, early) == 1
    with storage.connect(db) as conn:
        with storage.transaction(conn):
            _cost_intervals(conn, "mixed", early, MIDNIGHT + 900)
            _cost_intervals(conn, "mixed", MIDNIGHT + 900, MIDNIGHT + 1800, load=None)
            _cost_intervals(conn, "free", MIDNIGHT + 900, MIDNIGHT + 1800, price="0")
        pending = dashboard_cost.summary(conn, PROJECT, "7d", early, include_details=False)
        assert pending["aggregation_pending"]
        assert pending["data_through"] == early
        assert pending["low_load_cost_cny"] == "0.0000"
    assert dashboard_cost_rollups.refresh(db, settled) == 3
    with storage.connect(db) as conn:
        for period in ("today", "7d", "30d"):
            for instance_id in (None, "mixed", "free"):
                raw = dashboard_cost._summary(conn, PROJECT, period, settled, instance_id)
                report = dashboard_cost.summary(conn, PROJECT, period, settled, instance_id)
                assert report["estimated_cost_cny"] == raw["estimated_cost_cny"]
                assert report["low_load_cost_cny"] == raw["low_load_cost_cny"]
                if raw["low_load_cost_ratio"] is None:
                    assert report["low_load_cost_ratio"] is None
                else:
                    assert report["low_load_cost_ratio"] == pytest.approx(raw["low_load_cost_ratio"])
                assert report["idle_cost_cny"] == raw["idle_cost_cny"]
                assert report["low_load_windows"] == raw["low_load_windows"]
                assert report["boot_waits"] == raw["boot_waits"]
                assert [day["low_load_cost_cny"] for day in report["days"]] == [
                    day["low_load_cost_cny"] for day in raw["days"]
                ]
        queries = []
        conn.set_trace_callback(queries.append)
        lean = dashboard_cost.summary(conn, PROJECT, "7d", settled, include_details=False)
    assert not any("FROM dashboard_cost_intervals" in query for query in queries)
    assert lean["low_load_cost_ratio"] == pytest.approx(.75 * 1500 / 2400)
    assert lean["days"][-2]["low_load_cost_cny"] == "1.0250"
    assert lean["days"][-1]["low_load_cost_ratio"] == pytest.approx(.75 * 900 / 1800)
    assert not lean["low_load_windows"] and not lean["aggregation_pending"]


def test_rate_change_rebuilds_only_affected_days_and_retains_newer_revision(tmp_path, monkeypatch):
    db = tmp_path / "rollups.sqlite3"
    storage.initialize(db)
    now = MIDNIGHT + 1800
    with storage.connect(db) as conn, storage.transaction(conn):
        for name in ("changed", "other"):
            _cost_intervals(conn, name, MIDNIGHT - 1200, MIDNIGHT, price=None)
            _cost_intervals(conn, name, MIDNIGHT, now, price=None)
        dashboard_cost.save_rate(
            conn, PROJECT, "changed", dashboard_cost.ManualRate(effective_at=MIDNIGHT, hourly_rate=12), now,
        )
    dashboard_cost_rollups.refresh(db, now)
    with storage.connect(db) as conn, storage.transaction(conn):
        dashboard_cost.save_rate(
            conn, PROJECT, "changed", dashboard_cost.ManualRate(effective_at=MIDNIGHT - 1200, hourly_rate=6), now,
        )
        assert [tuple(row) for row in conn.execute(
            "SELECT day,instance_id,due_at FROM dashboard_cost_dirty_days"
        )] == [(MIDNIGHT - 86400, "changed", 0)]

    calculate = dashboard_cost_rollups._calculate

    def rate_changes_during_read(conn, job):
        row = calculate(conn, job)
        with storage.connect(db) as writer, storage.transaction(writer):
            dashboard_cost.save_rate(
                writer, PROJECT, "changed", dashboard_cost.ManualRate(effective_at=MIDNIGHT - 1200, hourly_rate=9), now,
            )
        return row

    with monkeypatch.context() as patch:
        patch.setattr(dashboard_cost_rollups, "_calculate", rate_changes_during_read)
        assert dashboard_cost_rollups.refresh(db, now) == 0
    with storage.connect(db) as conn:
        assert dashboard_cost.summary(conn, PROJECT, "7d", now, "changed", include_details=False)["aggregation_pending"]
    assert dashboard_cost_rollups.refresh(db, now) == 1
    with storage.connect(db) as conn:
        report = dashboard_cost.summary(conn, PROJECT, "7d", now, "changed", include_details=False)
        assert report["estimated_cost_cny"] == "9.0000"
        assert report["days"][-2]["estimated_cost_cny"] == "3.0000"
        assert report["days"][-1]["estimated_cost_cny"] == "6.0000"
        assert not report["aggregation_pending"]


def test_worker_records_holding_independently_of_monitoring_and_reprices_across_midnight(tmp_path, monkeypatch):
    db = tmp_path / "console.sqlite3"
    storage.initialize(db)
    clock = [MIDNIGHT - 12 * 60]
    monkeypatch.setattr(storage, "now", lambda: clock[0])
    cloud = Cloud()
    cloud.rows = [instance(gpu=4) | {"start_time": clock[0], "charge_type": "Postpay"}]
    collector = Collector(db, cloud)

    def collect(minute, levels, hints=False):
        clock[0] = MIDNIGHT + minute * 60
        cloud.points = [point(clock[0], str(i), value) for i, value in enumerate(levels)]
        if hints:
            cloud.points.append(point(clock[0], "", 30) | {"metric": "uhost_cpu_used"})
            cloud.points.append(point(clock[0], "0", 70) | {"metric": "cloudwatch_gpu_memory_usage"})
        collector._instances(PROJECT, {"retention_days": 30}, 0)

    for minute in range(-12, 9):
        collect(minute, [0, 0, 0, 0], hints=True)
    for minute in range(9, 25):
        collect(minute, [100, 50, 0, 0])
    collect(25, [])
    collect(26, [])
    with storage.connect(db) as conn:
        dashboard_cost.save_rate(conn, PROJECT, "uhost-test",
                                 dashboard_cost.ManualRate(effective_at=MIDNIGHT - 12 * 60, hourly_rate=6), clock[0])
        dashboard_cost.save_rate(conn, PROJECT, "uhost-test",
                                 dashboard_cost.ManualRate(effective_at=MIDNIGHT + 30, hourly_rate=12), clock[0])
        report = dashboard_cost.summary(conn, PROJECT, "7d", clock[0])
        assert report["gpu_hours"] == pytest.approx(38 / 15)
        assert report["weighted_gpu_hours"] == pytest.approx(15.5 * 1.5 / 60)
        assert report["unmonitored_gpu_hours"] == pytest.approx(2 / 15)
        assert report["low_gpu_hours"] == pytest.approx((20 * 4 + 16 * 2) / 60)
        assert report["estimated_cost_cny"] == "6.3500"
        assert report["low_load_cost_cny"] == "4.3500"
        assert report["evaluable_compute_cost_cny"] == "5.9500"
        assert report["low_load_cost_ratio"] == pytest.approx(4.35 / 6.35)
        assert report["idle_cost_cny"] == "2.7500"
        assert report["cost_per_weighted_gpu_hour_cny"] == "15.3548"
        assert report["days"][-2]["estimated_cost_cny"] == "1.2000"
        assert report["days"][-2]["low_load_cost_cny"] == "1.2000"
        assert report["days"][-2]["low_load_cost_ratio"] == 1
        assert report["days"][-1]["estimated_cost_cny"] == "5.1500"
        assert report["days"][-1]["low_load_cost_cny"] == "3.1500"
        assert report["instances"][0]["low_load_cost_ratio"] == report["low_load_cost_ratio"]
        assert report["boot_waits"][0]["wait_seconds"] == 21 * 60
        assert report["boot_waits"][0]["complete_before_load"]
        assert any(row["whole_idle"] and row["cpu_load"] and row["gpu_memory_load"] for row in report["low_load_windows"])
        assert report["low_load_windows"][0]["busy_cards"] == 2

        today = dashboard_cost.summary(conn, PROJECT, "today", clock[0])
        assert today["idle_instance_hours"] == pytest.approx(8 / 60)
        assert today["idle_cost_cny"] == "1.5500"
        dashboard_cost.restore_boundary(conn, clock[0])
    collect(27, [0, 0, 0, 0])
    cloud.partial = True
    collect(28, [])
    cloud.partial = False
    collect(29, [0, 0, 0, 0])
    with storage.connect(db) as conn:
        assert dashboard_cost.summary(conn, PROJECT, "7d", clock[0])["gpu_hours"] == pytest.approx(38 / 15)


def test_prices_prefer_observed_platform_rate_and_retain_unknown_intervals(tmp_path):
    db = tmp_path / "console.sqlite3"
    storage.initialize(db)
    row = instance(gpu=1) | {"charge_type": "Postpay", "gpu_mode": "with_gpu", "start_time": MIDNIGHT}
    with storage.connect(db) as conn, storage.transaction(conn):
        for minute, price in enumerate([None, None, 0, 0, 6, 6]):
            at = MIDNIGHT + minute * 60
            dashboard_cost.observe(conn, PROJECT, [row | {"instance_price_per_hour": price}],
                                   {"uhost-test": {at: {"0": 0}}}, {}, at, 60)
        dashboard_cost.save_rate(conn, PROJECT, "uhost-test",
                                 dashboard_cost.ManualRate(effective_at=MIDNIGHT + 150, hourly_rate="12"), at)
    with storage.connect(db) as conn:
        report = dashboard_cost.summary(conn, PROJECT, "today", at)
        assert report["estimated_cost_cny"] == "0.3000"
        assert report["unpriced_instance_hours"] == pytest.approx(2 / 60)
        assert report["manual_priced_instance_hours"] == pytest.approx(1 / 60)
        assert report["platform_priced_instance_hours"] == pytest.approx(2 / 60)
        assert report["cost_per_weighted_gpu_hour_cny"] is None
        assert report["idle_cost_cny"] == "0.0000"
        assert report["low_load_cost_cny"] == "0.0000"
        assert report["low_load_cost_ratio"] == 0

        free = dashboard_cost.summary(conn, PROJECT, "today", MIDNIGHT + 180)
        assert free["low_load_cost_cny"] == "0.0000"
        assert free["low_load_cost_ratio"] is None

        _cost_intervals(conn, "unmonitored", MIDNIGHT, MIDNIGHT + 60, load=None)
        unmonitored = dashboard_cost.summary(conn, PROJECT, "today", at, "unmonitored")
        assert unmonitored["estimated_cost_cny"] == "0.1367"
        assert unmonitored["low_load_cost_cny"] is None
        assert unmonitored["low_load_cost_ratio"] is None


def seed_pool(conn, now, *, project_id="p", days=5):
    from capacity_fixtures import record_capacity

    zones = ("z-early", "z-late", "z-empty", "z-one", "z-two")
    spec = (4, 32, 96)
    rows = [
        {"region": "region", "zone": zone, "gpu_model": "4090", "instance_type": "uhost",
         "specs": [dict(zip(("gpu", "cpu", "memory_gb"), spec))]} for zone in zones
    ]
    conn.execute("INSERT OR REPLACE INTO snapshots VALUES ('catalog','',?,?,NULL)", (json.dumps(rows), now))

    for day in reversed(range(1, days + 1)):
        base = now - day * 86400
        for step in range(10):
            ts = base + step * 300
            counts = {
                ("z-early", "4090", "exclusive"): 4 if step in (1, 2) else 0,
                ("z-late", "4090", "exclusive"): 4 if step in (5, 6) else 0,
                ("z-empty", "4090", "exclusive"): 0,
                ("z-one", "4090", "exclusive"): 2,
                ("z-two", "4090", "exclusive"): 2,
            }
            if day == 2 and step == 6:
                del counts[("z-late", "4090", "exclusive")]
            availability.record_pool(conn, "region", ts, provider_updated_at=ts, counts=counts)
            for zone in zones:
                count = counts.get((zone, "4090", "exclusive"))
                result = None if count is None else count >= 4
                record_capacity(conn, project_id, ("region", zone, "4090", "uhost"), {spec: result}, ts)
    availability.record_pool(conn, "region", now, provider_updated_at=now, counts={
        ("z-early", "4090", "exclusive"): 4, ("z-late", "4090", "exclusive"): 0,
        ("z-empty", "4090", "exclusive"): 12, ("z-one", "4090", "exclusive"): 2,
        ("z-two", "4090", "exclusive"): 2,
    })
    for zone in zones:
        record_capacity(conn, project_id, ("region", zone, "4090", "uhost"), {spec: zone == "z-early"}, now)


def candidate(zone, price=None):
    return {"region": "region", "zone": zone, "pool": "exclusive", "hourly_rate": price}


def draft(now, candidates):
    return {"gpu_model": "4090", "cards": 4, "target_start": now + 7 * 300, "latest_start": now + 9 * 300,
            "training_minutes": 60, "preparation_minutes": 10, "candidates": candidates}


def test_rental_replays_by_deadline_and_never_pools_cards_across_zones(tmp_path):
    db = tmp_path / "console.sqlite3"
    storage.initialize(db)
    now = MIDNIGHT + 18 * 3600
    with storage.connect(db) as conn:
        seed_pool(conn, now)
        body = draft(now, [candidate("z-early", "10"), candidate("z-late", "10"), candidate("z-empty", "0"),
                           candidate("z-one", "1"), candidate("z-two", "1")])
        result = rental_plan.plan(conn, "p", rental_plan.RentalRequest(**body), now)
        assert result["status"] == "ready"
        assert result["latest_search_at"] == now + 7 * 300
        assert result["recommendation"]["candidate_index"] == 1
        assert result["recommendation"]["start_search_at"] == now + 5 * 300
        assert result["recommendation"]["history"]["hit_rate"] == 1
        assert result["recommendation"]["advance_hold_seconds"] == 0
        assert result["recommendation"]["estimated_cost_cny"] == "11.6667"
        assert result["recommendation"]["savings_vs_now_cny"] == "4.1667"
        assert result["candidates"][0]["can_try_now"]
        assert not result["candidates"][1]["can_try_now"]
        assert result["candidates"][2]["now"]["history"]["miss_days"] == 5
        assert result["candidates"][2]["remaining_cards"] == 12
        assert result["candidates"][2]["can_try_now"] is False
        assert all(item["later"] is None for item in result["candidates"][2:])
        assert rental_plan.plan(conn, "p", rental_plan.RentalRequest(**(
            body | {"candidates": [candidate("z-one"), candidate("z-two")]}
        )), now)["status"] == "no_window"
        assert rental_plan.plan(conn, "p", rental_plan.RentalRequest(**(
            body | {"preparation_minutes": 46}
        )), now)["status"] == "deadline_too_close"
        assert rental_plan.plan(conn, "p", rental_plan.RentalRequest(**(
            body | {"candidates": [candidate("z-late")]}
        )), now)["status"] == "availability_only"


def test_rental_observation_after_the_cutoff_is_not_a_hit():
    now = MIDNIGHT + 18 * 3600
    counts = [now - day * 86400 + 55 for day in range(7, 0, -1)]
    exact = rental_plan._replay(counts, counts, now, now + 30, now)
    assert exact["hit_days"] == 0 and exact["unknown_days"] == 6
    available = rental_plan._replay(counts, counts, now, now + 60, now)
    assert available["hit_days"] == 7 and available["coverage"] == 1


def test_cost_and_rental_web_mcp_match_and_require_project_scope(tmp_path, monkeypatch):
    now = MIDNIGHT + 18 * 3600
    monkeypatch.setattr(storage, "now", lambda: now)
    config = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    origin = f"http://{config.mcp_bind_ip}:{config.mcp_bind_port}"
    with (
        TestClient(create_app(config), base_url=origin) as client,
        TestClient(create_mcp_app(config), base_url=origin) as mcp_client,
    ):
        with storage.connect(config.db) as conn:
            conn.execute("INSERT INTO sessions VALUES (?,?,?)", (digest("browser"), "csrf", now + 60))
            conn.execute("INSERT INTO snapshots VALUES ('projects','',?,?,NULL)", (
                json.dumps([{"id": PROJECT}, {"id": "other"}]), now,
            ))
            conn.execute("INSERT INTO snapshots VALUES ('instances',?,?,?,NULL)", (
                PROJECT, json.dumps([instance() | {"charge_type": "Postpay"}]), now,
            ))
            seed_watch_catalog(conn, {"region": "region", "zone": "z-early", "gpu_model": "4090", "gpu": 4,
                                      "cpu": 32, "memory_gb": 96}, now)
            conn.execute("UPDATE watches SET project_id=? WHERE id='watch-a'", (PROJECT,))
            conn.execute("UPDATE catalog_watch_bindings SET project_id=? WHERE watch_id='watch-a'", (PROJECT,))
            seed_pool(conn, now, project_id=PROJECT, days=2)
            token = auth.issue(conn, name="Test client", manage=True, ssh=True, days=None)["token"]
        headers = {"Origin": origin, "X-CSRF-Token": "csrf"}
        params = {"project_id": PROJECT}
        assert client.get("/api/dashboard/costs", params=params).status_code == 401
        client.cookies.set(COOKIE, "browser")
        rate_path = "/api/dashboard/costs/rates/uhost-test"
        rate = {"effective_at": now, "hourly_rate": "4.80"}
        assert client.put(rate_path, params=params, json=rate).status_code == 403
        assert client.put(rate_path, params={"project_id": "unrelated"}, json=rate, headers=headers).status_code == 404
        assert client.put(rate_path, params=params, json=rate, headers=headers).status_code == 410
        web = client.get("/api/dashboard/costs", params=params).json()
        assert web["instances"][0]["manual_rates"] == []
        assert web["billing"]["coverage"]["status"] == "pending"
        assert web["estimated_cost_cny"] is None
        assert web["low_load_cost_cny"] is None
        assert web["low_load_cost_ratio"] is None
        assert call_tool(mcp_client, token, "gpu_get_cost_summary", params) == web
        lean = client.get("/api/dashboard/costs", params=params | {"include_details": False}).json()
        assert call_tool(mcp_client, token, "gpu_get_cost_summary", params | {"include_details": False}) == lean
        assert client.get("/api/dashboard/costs/details", params=params).json() == {
            "low_load_windows": [], "boot_waits": [],
        }
        body = draft(now, [{"watch_id": "watch-a"}, candidate("z-late")])
        assert client.post("/api/dashboard/rental-plan", params={"project_id": "other"}, json=body,
                           headers=headers).status_code == 404
        web = client.post("/api/dashboard/rental-plan", params=params, json=body, headers=headers).json()
        assert web["status"] == "insufficient" and web["recommendation"] is None
        assert web["candidates"][0]["can_try_now"]
        assert call_tool(mcp_client, token, "gpu_plan_gpu_rental", params | {"plan": body}) == web


def test_existing_run_history_is_imported_once_and_kept_after_reboot(tmp_path, monkeypatch):
    db = tmp_path / "history.sqlite3"
    storage.initialize(db)
    boot, cutoff = MIDNIGHT, MIDNIGHT + 30 * 60
    now = cutoff + 60
    monkeypatch.setattr(storage, "now", lambda: now)
    running = instance(gpu=2) | {"start_time": boot, "charge_type": "Postpay", "instance_price_per_hour": 12}
    stopped = instance("stopped", gpu=1) | {
        "start_time": boot, "stop_time": boot + 20 * 60, "state": "Stopped", "charge_type": "Postpay",
    }
    cloud = Cloud()
    cloud.rows = [running, stopped]
    collector = Collector(db, cloud)
    with storage.connect(db) as conn, storage.transaction(conn):
        conn.execute("INSERT INTO dashboard_cost_progress VALUES (?,?,?,?,NULL)", (PROJECT, cutoff, cutoff, 120))
        conn.executemany("INSERT INTO metrics VALUES (?,?,?,?,?,?)", [
            (PROJECT, key, "cloudwatch_gpu_util", label, boot + minute * 60, 0)
            for key, labels in (("uhost-test", ("a", "b")), ("stopped", ("s",)))
            for minute in range(-1, 32)
            for label in labels
            if key == "stopped" or minute != 24 or label != "b"
        ])
    collector._instances(PROJECT, {"retention_days": 30}, 0)

    with storage.connect(db) as conn:
        active = dashboard_cost.summary(conn, PROJECT, "today", now, "uhost-test")
        assert active["recovered_gpu_hours"] == pytest.approx(28 * 2 / 60)
        assert active["gpu_hours"] == active["recovered_gpu_hours"]
        assert active["estimated_cost_cny"] is None
        assert active["low_load_cost_cny"] is None
        assert active["low_load_cost_ratio"] is None
        assert active["idle_instance_hours"] == pytest.approx(23 / 60)
        report = dashboard_cost.summary(conn, PROJECT, "today", now, "stopped")
        assert report["recovered_gpu_hours"] == pytest.approx(20 / 60)
        assert report["instances"][0]["manual_rates"] == []
        dashboard_cost.save_rate(conn, PROJECT, "stopped",
                                 dashboard_cost.ManualRate(effective_at=boot, hourly_rate="6"), now)
        assert dashboard_cost.summary(conn, PROJECT, "today", now, "stopped")["estimated_cost_cny"] == "2.0000"

    cloud.rows[0] = running | {"start_time": now}
    now += 60
    cloud.points = [point(now, label, 60) for label in ("new-a", "new-b")]
    collector._instances(PROJECT, {"retention_days": 30}, 0)
    now += 60
    cloud.points = [point(now, label, 60) for label in ("new-a", "new-b")]
    collector._instances(PROJECT, {"retention_days": 30}, 0)
    with storage.connect(db) as conn:
        active = dashboard_cost.summary(conn, PROJECT, "today", now, "uhost-test")
        assert active["recovered_gpu_hours"] == pytest.approx(28 * 2 / 60)
        assert active["gpu_hours"] == pytest.approx(29 * 2 / 60)
        assert active["estimated_cost_cny"] == "0.2000"
        project = dashboard_cost.summary(conn, PROJECT, "today", now)
        assert project["recovered_gpu_hours"] == pytest.approx((28 * 2 + 20) / 60)
        assert project["estimated_cost_cny"] == "2.2000"
        assert project["low_load_cost_cny"] == "2.0000"
        assert project["low_load_cost_ratio"] == pytest.approx(2 / 2.2)


def test_cardless_collection_prefers_verified_account_price_then_official_rate(tmp_path, monkeypatch):
    db = tmp_path / "cardless-prices.sqlite3"
    storage.initialize(db)
    now = [MIDNIGHT + 60]
    monkeypatch.setattr(storage, "now", lambda: now[0])
    cloud = Cloud()
    vm = cloud.rows[0]
    vm.update(gpu=0, gpu_mode="without_gpu", charge_type="Postpay", support_without_gpu_start=True,
              cpu=8, memory_gb=16, start_time=MIDNIGHT, instance_price_per_hour=12)
    with storage.connect(db) as conn:
        cardless.record_profile(conn, {"region": vm["region"], "zone": vm["zone"], "spec": "B", "cpu": 8,
                                       "memory_gb": 16, "standard_hourly_cny": "0.60"})
    collector = Collector(db, cloud)
    for account, expected, source in ((None, "0.60", "official"), ("0.45", "0.45", "account"), (0, "0", "account")):
        def lookup(project, items):
            assert project == PROJECT and len(items) == 1 and items[0]["id"] == vm["id"]
            return {} if account is None else {vm["id"]: {"instance_price_per_hour": account}}

        monkeypatch.setattr(cloud, "instance_prices", lookup)
        collector._instances(PROJECT, {"retention_days": 30}, 0)
        with storage.connect(db) as conn:
            saved, = storage.snapshot(conn, "instances", PROJECT)["data"]
            assert (saved["without_gpu_spec"], saved["cardless_hourly_cny"], saved["cardless_rate_source"]) == (
                "B", expected, source,
            )
        now[0] += 60


def test_targeted_instance_price_feeds_snapshots_and_only_subsequent_cost(tmp_path, monkeypatch):
    db = tmp_path / "price.sqlite3"
    storage.initialize(db)
    now = MIDNIGHT + 60
    monkeypatch.setattr(storage, "now", lambda: now)
    cloud = Cloud()
    running = instance(gpu=1) | {"start_time": MIDNIGHT, "charge_type": "Postpay"}
    stopped = instance("stopped", gpu=1) | {
        "start_time": MIDNIGHT - 3600, "stop_time": MIDNIGHT, "state": "Stopped", "charge_type": "Postpay",
    }
    free = instance("free", gpu=1) | {"charge_type": "Postpay", "instance_price_per_hour": 0}
    cloud.rows = [running, stopped, free]
    collector = Collector(db, cloud)
    lookups = []

    def lookup(project, batch):
        lookups.append((project, [row["id"] for row in batch]))
        if len(lookups) == 1:
            raise CloudError("503")
        return {"uhost-test": {"instance_price_per_hour": 8.2, "disk_price_per_hour": 0},
                "stopped": {"instance_price_per_hour": 3.79}}

    monkeypatch.setattr(cloud, "instance_prices", lookup)
    for _ in range(3):
        cloud.points = [point(now, "0", 60)]
        collector._instances(PROJECT, {"retention_days": 30}, 0)
        now += 60
    assert lookups == [(PROJECT, ["uhost-test", "stopped"])] * 3
    with storage.connect(db) as conn:
        snapshot = storage.snapshot(conn, "instances", PROJECT)
        assert snapshot["error"] is None
        assert snapshot["data"][0]["instance_price_per_hour"] == 8.2
        assert snapshot["data"][0]["disk_price_per_hour"] == 0
        assert snapshot["data"][2]["instance_price_per_hour"] == 0
        active = dashboard_cost.summary(conn, PROJECT, "today", now, "uhost-test")
        assert active["instances"][0]["current_hourly_rate_cny"] == "8.2"
        assert active["estimated_cost_cny"] == "0.1367"
        assert active["unpriced_instance_hours"] == pytest.approx(1 / 60)
        inactive = dashboard_cost.summary(conn, PROJECT, "today", now, "stopped")
        assert inactive["instances"][0]["current_hourly_rate_cny"] == "3.79"
        assert inactive["estimated_cost_cny"] is None
