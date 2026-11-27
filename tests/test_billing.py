from dataclasses import replace
from datetime import date
import json
from sqlite3 import IntegrityError

import pytest
from fastapi.testclient import TestClient

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError, PAGE_SIZE
from gpu_console.config import Config
from gpu_console.domain.billing import Order, apportioned, cycles
from gpu_console.mcp import auth
from gpu_console.mcp.server import create_mcp_app
from gpu_console.storage import billing, dashboard_billed_cost, dashboard_billed_cost_rollups, dashboard_cost, dashboard_cost_rollups
from gpu_console.storage import maintenance, storage_cost
from gpu_console.storage.dashboard_usage import _local_date, _midnight
from gpu_console.worker.billing import BillingCollector
from gpu_console.worker.instance_assets import AssetCollector

from test_compshare import Wire, client
from test_dashboard_api import call_tool
from test_dashboard_cost import _cost_intervals
from test_instance_assets import INSTANCE, NOW, PROJECT, ZONE, console, snapshot
from test_storage_cost import CFS, CFS_ZONE, DISK, quote

PROJECT_ROW = {"id": PROJECT, "name": "Default"}
MIDNIGHT = _midnight(_local_date(NOW))
CYCLE = _local_date(NOW).isoformat()[:7]
SYNC_AT = MIDNIGHT + 5 * 3600


def paid(order_no, resource_id, resource_type, cash, order=None, **extra):
    return {
        "ProjectId": "1234", "ProjectName": "Default", "OrderNo": order_no, "OrderType": "OT_POSTPAID_PAYMENT",
        "ResourceId": resource_id, "ResourceType": resource_type, "ChargeType": "Post", "ShowHover": 1,
        "StartTime": MIDNIGHT - 300, "EndTime": MIDNIGHT + 300, "CreateTime": MIDNIGHT + 600,
        "AmountReal": cash, "Amount": order if order is not None else cash, "AmountCoupon": "0.00", "AmountFree": "0.00",
    } | extra


def test_sdk_requests_paid_zero_amounts_and_checks_page_snapshot():
    rows = [paid(str(i), INSTANCE["id"], "uhost", "0.00", "1.07", AmountFree="1.07")
            for i in range(PAGE_SIZE + 1)]
    rows[0] |= {"UserEmail": "fixture@example.invalid", "ResourceExtendInfo": [
        {"KeyId": "name", "Value": "GPU example"}, {"KeyId": "private_ip", "Value": "192.0.2.99"},
    ]}
    first, last = rows[:PAGE_SIZE], rows[PAGE_SIZE:]
    wire = Wire(*({"Items": part, "TotalCount": len(rows)} for part in (first, last, first)))
    fetched = client(wire).billing_orders("Default", CYCLE)
    assert len(fetched) == len(rows)
    assert fetched[0].amounts == (0, 107, 0, 107) and fetched[0].resource_name == "GPU example"
    assert "fixture@example.invalid" not in repr(fetched) and "192.0.2.99" not in repr(fetched)
    for (request, retries), offset in zip(wire.sent, (0, PAGE_SIZE, 0), strict=True):
        assert (request["Action"], request["ProjectName"], request["BillingCycle"]) == ("ListUBillDetail", "Default", CYCLE)
        assert (request["PaidState"], request["ShowZero"], request["Limit"], request["Offset"], retries) == (
            "2", "1", str(PAGE_SIZE), str(offset), 0,
        )

    changed = [{**first[0], "AmountReal": "0.09"}, *first[1:]]
    for parts in ((first, changed[-1:], first), (first, last, changed), (first, [], first)):
        wire = Wire(*({"Items": part, "TotalCount": len(rows)} for part in parts))
        with pytest.raises(CloudError, match="分页"):
            client(wire).billing_orders("Default", CYCLE)


def test_paid_costs_cover_categories_and_use_same_order_ledger_in_web_and_mcp(console):
    web, db, now = console
    now[0] = SYNC_AT + 901
    instance_id = INSTANCE["id"]
    with storage.connect(db) as conn, storage.transaction(conn):
        snapshot(conn, "projects", "", [PROJECT_ROW])
        _cost_intervals(conn, instance_id, MIDNIGHT - 60, MIDNIGHT, price="99")
        storage_cost.observe(conn, PROJECT, [
            (ZONE["region"], ZONE["zone"], "udisk", at, at, [DISK])
            for at in (MIDNIGHT - 300, MIDNIGHT, MIDNIGHT + 300)
        ], estimate_costs=False)
        token = auth.issue(conn, name="Read-only fixture", manage=False, ssh=False, days=None)["token"]
    items = [
        paid("compute", instance_id, "uhost", "4.00", "5.00", AmountFree="1.00"),
        paid("known-disk", DISK["id"], "udisk_ssd", "1.00", "2.00",
             AmountCoupon="1.00", OrderType="OT_RENEW", ChargeType="Day"),
        paid("shared-disk", "unmatched-storage", "ufs", "2.00", StartTime=MIDNIGHT - 1800, EndTime=MIDNIGHT - 1200),
        paid("system-disk", "boot", "udisk_system_disk", "0.00", "4.00", AmountFree="4.00"),
        paid("image", "test-image", "compshareImage", "0.50", StartTime=None, EndTime=None),
        paid("other", "test-bandwidth", "eip", "0.25", StartTime=None, EndTime=None),
        paid("refund", instance_id, "uhost", "-0.25", OrderType="OT_REFUND"),
        paid("review", "not-registered", "udisk_ssd", "4.00", OrderType="OT_REFUND"),
        paid("archived", "released-instance", "compsharecpod", "0.00", "0.20", AmountFree="0.20",
             ResourceExtendInfo=[{"KeyId": "name", "Value": "Old GPU"}]),
    ]
    wire = Wire({"Items": items, "TotalCount": len(items)})
    BillingCollector(db, client(wire)).tick()
    args = {"project_id": PROJECT, "range": "7d", "include_details": False}
    response = web.get("/api/dashboard/costs", params=args).json()
    assert response["estimated_cost_cny"] is None and response["estimated_total_cost_cny"] is None
    assert response["gpu_hours"] == pytest.approx(4 / 60)
    report = response["billing"]
    assert report["basis"] == "order_date"
    assert report["totals"]["cash_cny"] == "7.50"
    assert report["totals"]["order_amount_cny"] == "13.70"
    assert report["totals"]["free_credit_cny"] == "5.20"
    assert report["totals"]["coupon_cny"] == "1.00"
    assert report["totals"]["review_order_count"] == 1
    assert report["coverage"]["status"] == "partial"
    assert [report["totals"]["by_category"][key]["cash_cny"] for key in ("compute", "storage", "image", "other")] == [
        "3.75", "3.00", "0.50", "0.25",
    ]
    assert report["instances"][instance_id]["cash_cny"] == "4.75"
    assert report["instances"]["released-instance"]["cash_cny"] == "0.00"
    assert any(row["name"] == "Old GPU" and not row["current"] for row in response["instances"])
    service_args = args | {"basis": "service_period"}
    service_response = web.get("/api/dashboard/costs", params=service_args).json()
    service = service_response["billing"]
    assert service["totals"] == report["totals"]
    assert service["days"][_local_date(MIDNIGHT - 100).isoformat()]["cash_cny"] == "4.50"
    assert service["days"][_local_date(MIDNIGHT).isoformat()]["cash_cny"] == "3.00"
    today = web.get("/api/dashboard/costs/orders", params=service_args | {"range": "today"}).json()
    by_id = {row["order_no"]: row for row in today["data"]}
    assert by_id["known-disk"]["selected_amounts"]["cash_cny"] == "0.50"
    assert by_id["refund"]["allocation"] == "order_date"
    assert by_id["review"]["amounts"]["cash_cny"] is None
    assert "shared-disk" not in by_id
    disk_report = web.get("/api/dashboard/costs/storage", params=args).json()
    assert disk_report["project_only"]["cash_cny"] == "2.00"
    assert web.get("/api/dashboard/costs/orders", params=args | {"project_id": "unrelated"}).status_code == 404
    assert web.get("/api/dashboard/costs/orders", params=args | {"category": "storage", "limit": 1}).json()["total"] == 4
    assert dashboard_cost_rollups.refresh(db, now[0], estimate_costs=False) == 1
    assert web.get("/api/dashboard/costs", params=args).json() == response
    with storage.connect(db) as conn:
        totals = json.loads(conn.execute("SELECT totals FROM dashboard_cost_daily").fetchone()[0])
        assert totals["cost"] == "0" and totals["platform_seconds"] == 0
    config = Config(db.parent, db.parent / "secrets", db.parent / "static", False, False)
    with TestClient(create_mcp_app(config), base_url=f"http://{config.mcp_bind_ip}:{config.mcp_bind_port}") as mcp:
        assert call_tool(mcp, token, "gpu_get_cost_summary", service_args) == service_response


def test_sync_is_idempotent_and_failed_pages_keep_the_published_month(console):
    web, db, now = console
    with storage.connect(db) as conn:
        snapshot(conn, "projects", "", [PROJECT_ROW])
    day = MIDNIGHT + 900
    item = paid("one", INSTANCE["id"], "uhost", "14.50")
    good = {"Items": [item], "TotalCount": 1}
    for i, payload in enumerate((good, good, {"Items": [], "TotalCount": 4},
                                 {"Items": [item | {"ProjectId": "other-id"}], "TotalCount": 1})):
        now[0] = day + i
        BillingCollector(db, client(Wire(payload))).sync(PROJECT_ROW, CYCLE, [PROJECT_ROW], now[0])
    result = web.get("/api/dashboard/costs", params={"project_id": PROJECT}).json()["billing"]
    assert result["totals"]["order_count"] == 1 and result["totals"]["cash_cny"] == "14.50"
    assert result["coverage"]["status"] == "partial" and result["coverage"]["synced_at"] == day + 1
    assert "项目 ID" in result["coverage"]["failed_cycles"][0]["error"]
    assert web.get("/api/dashboard/costs", params={"project_id": PROJECT, "instance_id": "unrelated"}
                   ).json()["billing"]["totals"]["cash_cny"] == "0.00"
    with storage.connect(db) as conn:
        assert billing.binding(conn, PROJECT) == ("1234", ["Default"])
        assert conn.execute("SELECT COUNT(*) FROM billing_orders").fetchone()[0] == 1


def test_unknown_project_binding_cannot_publish_a_paid_order(console):
    web, db, now = console
    item = paid("one", "unmatched-instance", "uhost", "14.50")
    collector = BillingCollector(db, client(Wire({"Items": [item], "TotalCount": 1})))
    now[0] = MIDNIGHT + 900
    collector.sync(PROJECT_ROW, CYCLE, [PROJECT_ROW], now[0])
    report = web.get("/api/dashboard/costs", params={"project_id": PROJECT}).json()["billing"]
    assert report["totals"]["cash_cny"] is None
    assert report["coverage"]["status"] == "pending"
    assert "归属待核对" in report["coverage"]["failed_cycles"][0]["error"]


def test_normal_storage_collection_retains_rates_and_mounts_for_today(console):
    _, db, now = console

    class Cloud:
        def data_disks(self, scope):
            return [DISK] if scope["zone"] == ZONE["zone"] else []

    with storage.connect(db) as conn:
        quote(conn, DISK, "0.12", MIDNIGHT)
    assets = AssetCollector(db, Cloud())
    for at in (MIDNIGHT, MIDNIGHT + 300):
        now[0] = at
        assets.poll_storage(PROJECT, [ZONE])
    with storage.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM storage_cost_daily").fetchone()[0] == 0
        sample = conn.execute("SELECT resources FROM storage_cost_samples WHERE zone=? ORDER BY sampled_at DESC LIMIT 1",
                              (ZONE["zone"],)).fetchone()[0]
        observed = json.loads(sample)[DISK["id"]]
        assert observed["mounted_instance_ids"] == [INSTANCE["id"]]
        assert [(price["source"], price["amount"]) for price in observed["prices"]] == [("account", "0.12")]


def test_collector_refreshes_only_in_dawn_window_and_keeps_failed_attempts(console):
    _, db, now = console
    with storage.connect(db) as conn:
        snapshot(conn, "projects", "", [PROJECT_ROW])
        prior = SYNC_AT - 86400
        for cycle in cycles(_local_date(SYNC_AT)):
            billing.publish(conn, PROJECT, cycle, prior, prior, None, [], [])
    wire = Wire({"Items": [], "TotalCount": 0}, {"Items": [], "TotalCount": 1}, {"Items": [], "TotalCount": 0})
    for timestamp in (SYNC_AT - 1, SYNC_AT, SYNC_AT + 10, SYNC_AT + 1800, SYNC_AT + 3600, SYNC_AT + 86399):
        now[0] = timestamp
        BillingCollector(db, client(wire)).tick()
    previous_cycle = cycles(_local_date(SYNC_AT), 2)[1]
    assert [request["BillingCycle"] for request, _ in wire.sent] == [CYCLE, previous_cycle]
    with storage.connect(db) as conn:
        assert conn.execute("SELECT synced_at,error FROM billing_sync WHERE cycle=?", (CYCLE,)).fetchone()[0] == SYNC_AT
        row = conn.execute("SELECT synced_at,error FROM billing_sync WHERE cycle=?", (previous_cycle,)).fetchone()
        assert row["synced_at"] == prior and "分页" in row["error"]
    now[0] = SYNC_AT + 86400
    BillingCollector(db, client(wire)).tick()
    assert [request["BillingCycle"] for request, _ in wire.sent] == [CYCLE, previous_cycle, CYCLE]


def test_interrupted_claim_delays_retries_until_next_window(console):
    _, db, now = console
    with storage.connect(db) as conn:
        assert billing.start_sync(conn, PROJECT, CYCLE, SYNC_AT)
        assert not billing.start_sync(conn, PROJECT, CYCLE, SYNC_AT + 1)
        assert billing.next_sync(conn, [PROJECT_ROW], SYNC_AT + 1)[1] != CYCLE
        assert billing.next_sync(conn, [PROJECT_ROW], SYNC_AT + 86400) == (PROJECT_ROW, CYCLE)
    now[0] = SYNC_AT + 86400
    BillingCollector(db, client(Wire({"Items": [], "TotalCount": 0}))).tick()
    with storage.connect(db) as conn:
        state = conn.execute("SELECT synced_at,error FROM billing_sync WHERE cycle=?", (CYCLE,)).fetchone()
        assert tuple(state) == (now[0], None)


def test_history_schedule_and_restore_revalidate_paid_cycles(tmp_path, monkeypatch):
    db = tmp_path / "data" / "console.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    storage.initialize(db)
    now = [SYNC_AT + 900]
    monkeypatch.setattr(storage, "now", lambda: now[0])
    row = Order("1234", "Default", "order", "OT_BUY", INSTANCE["id"], "uhost", None, "Year",
                MIDNIGHT, MIDNIGHT, MIDNIGHT + 86400, (100, 100, 0, 0))
    with storage.connect(db) as conn, storage.transaction(conn):
        snapshot(conn, "projects", "", [PROJECT_ROW])
        for i, month in enumerate(cycles(_local_date(now[0]))):
            assert billing.next_sync(conn, [PROJECT_ROW], now[0]) == (PROJECT_ROW, month)
            billing.publish(conn, PROJECT, month, now[0], now[0], "1234", ["Default"],
                            [(row, "compute", INSTANCE["id"])] if i == 0 else [])
        assert billing.next_sync(conn, [PROJECT_ROW], now[0]) is None
        assert billing.summary(conn, PROJECT, "today", "service_period", now[0], None)["coverage"]["status"] == "ready"
    assert dashboard_billed_cost_rollups.refresh(db, now[0], limit=30) == 29
    maintenance.backup(db, backup)
    now[0] += 60
    maintenance.restore(db, backup)
    with storage.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dashboard_billed_cost_days").fetchone()[0] == 0
        report = billing.summary(conn, PROJECT, "today", "service_period", now[0], None)
        assert report["coverage"]["status"] == "partial" and len(report["coverage"]["pending_cycles"]) == 13
        assert report["totals"]["cash_cny"] == "1.00"
        assert billing.next_sync(conn, [PROJECT_ROW], now[0]) is None
        now[0] += 86400
        assert billing.next_sync(conn, [PROJECT_ROW], now[0]) == (PROJECT_ROW, CYCLE)
        # The same supplier order in another month must roll back that month's replacement.
        other = cycles(_local_date(now[0]), 2)[1]
        with pytest.raises(IntegrityError, match="UNIQUE constraint"), storage.transaction(conn):
            billing.publish(conn, PROJECT, other, now[0], now[0], "1234", ["Default"],
                            [(replace(row, created_at=MIDNIGHT - 86400), "compute", INSTANCE["id"])])
        assert conn.execute("SELECT COUNT(*) FROM billing_orders").fetchone()[0] == 1


def test_cents_are_conserved_when_splitting_orders_across_days():
    points = (0, 1, 2, 3)
    for value in (0, 1, 100, -100, 900719925474099301):
        assert sum(apportioned(value, a, b, 3) for a, b in zip(points, points[1:])) == value
    assert cycles(date(2026, 1, 1), 3) == ["2026-01", "2025-12", "2025-11"]


def test_today_estimates_use_unbilled_service_windows_and_roll_over(console):
    web, db, now = console
    instance_id = INSTANCE["id"]
    day = MIDNIGHT + 1800
    now[0] = day
    prepaid_disk = DISK | {"id": "prepaid", "charge_type": "Month"}
    with storage.connect(db) as conn, storage.transaction(conn):
        conn.execute("UPDATE sessions SET expires_at=?", (MIDNIGHT + 2 * 86400,))
        snapshot(conn, "projects", "", [PROJECT_ROW])
        _cost_intervals(conn, instance_id, MIDNIGHT - 600, day, price="12")
        quote(conn, DISK, "3.6", MIDNIGHT - 300)
        quote(conn, CFS, "7.2", MIDNIGHT - 300)
        quote(conn, prepaid_disk, "9000", MIDNIGHT - 300)
        storage_cost.observe(conn, PROJECT, [
            (scope["region"], scope["zone"], kind, at, at, resources)
            for at in range(MIDNIGHT - 300, day + 1, 300)
            for scope, kind, resources in ((ZONE, "udisk", [DISK, prepaid_disk]), (CFS_ZONE, "cfs", [CFS]))
        ], estimate_costs=False)
        token = auth.issue(conn, name="Read-only estimate fixture", manage=False, ssh=False, days=None)["token"]
    params = {"project_id": PROJECT, "range": "7d", "include_details": False}
    before = web.get("/api/dashboard/costs", params=params).json()
    estimate = before["today_estimate"]
    assert estimate["compute_cny"] == "6.0000" and estimate["storage_cny"] == "5.4000"
    assert estimate["total_cny"] == "11.4000" and before["billing"]["totals"]["cash_cny"] is None
    assert estimate["low_load_cost_cny"] == "4.5000"
    assert estimate["idle_cost_cny"] == "0.0000" and estimate["cost_per_weighted_gpu_hour_cny"] == "12.0000"
    assert estimate["compute_data_through"] == estimate["storage_data_through"] == day

    compute = Order("1234", "Default", "paid-compute", "OT_POSTPAID_PAYMENT", instance_id, "uhost", None, "Post",
                    MIDNIGHT + 900, MIDNIGHT - 360, MIDNIGHT + 300, (275, 275, 0, 0))
    zero = replace(compute, order_no="zero", start_at=MIDNIGHT + 240, end_at=MIDNIGHT + 900, amounts=(0, 50, 0, 50))
    disk = replace(compute, order_no="disk", resource_id=DISK["id"], resource_type="udisk_ssd",
                   start_at=MIDNIGHT + 300, end_at=MIDNIGHT + 1200, amounts=(50, 50, 0, 0))
    refund = replace(compute, order_no="refund", order_type="OT_REFUND", start_at=MIDNIGHT + 900,
                     end_at=day, amounts=(-10, -10, 0, 0))
    rows = [(item, "compute", instance_id) for item in (compute, zero, refund)] + [(disk, "storage", instance_id)]
    with storage.connect(db) as conn, storage.transaction(conn):
        billing.publish(conn, PROJECT, CYCLE, day, day, "1234", ["Default"], rows)
    result = web.get("/api/dashboard/costs", params=params).json()
    estimate = result["today_estimate"]
    assert estimate["compute_cny"] == "3.0000" and estimate["storage_cny"] == "4.5000"
    assert estimate["total_cny"] == "7.5000" and estimate["project_only_storage_cny"] == "3.6000"
    assert estimate["instances"][instance_id]["total_cny"] == "3.9000"
    assert estimate["low_load_cost_cny"] == "2.2500"
    assert result["billing"]["totals"]["cash_cny"] == "3.15"
    assert result["billing"]["project_latest_order_at"] == MIDNIGHT + 900
    service = web.get("/api/dashboard/costs", params=params | {"basis": "service_period", "range": "today"}).json()
    assert service["today_estimate"] == estimate
    scoped = web.get("/api/dashboard/costs", params=params | {"instance_id": instance_id}).json()
    assert scoped["today_estimate"]["total_cny"] == "3.9000"
    config = Config(db.parent, db.parent / "secrets", db.parent / "static", False, False)
    with TestClient(create_mcp_app(config), base_url=f"http://{config.mcp_bind_ip}:{config.mcp_bind_port}") as mcp:
        assert call_tool(mcp, token, "gpu_get_cost_summary", params) == result

    now[0] = MIDNIGHT + 86400
    rolled = web.get("/api/dashboard/costs", params=params).json()
    assert rolled["today_estimate"]["date"] == _local_date(now[0]).isoformat()
    assert rolled["today_estimate"]["total_cny"] is None
    assert _local_date(day).isoformat() in rolled["billing"]["provisional_days"]
    assert rolled["billing"]["totals"]["cash_cny"] == "3.15"
    # A late order adds actual cash after midnight without reviving the previous estimate.
    final = replace(compute, order_no="late", start_at=MIDNIGHT + 900, end_at=day,
                    created_at=now[0], amounts=(300, 300, 0, 0))
    with storage.connect(db) as conn, storage.transaction(conn):
        billing.publish(conn, PROJECT, CYCLE, now[0], now[0], "1234", ["Default"], rows + [(final, "compute", instance_id)])
    settled = web.get("/api/dashboard/costs", params=params | {"basis": "service_period"}).json()
    assert settled["today_estimate"]["total_cny"] is None
    assert settled["billing"]["totals"]["cash_cny"] == "6.15"


def test_cardless_estimate_uses_its_own_rate_and_clears_paid_seconds_at_midnight(console):
    web, db, now = console
    instance_id = INSTANCE["id"]
    day = MIDNIGHT + 900
    now[0] = day
    row = INSTANCE | {"state": "Running", "start_time": MIDNIGHT - 300, "gpu": 0, "gpu_mode": "without_gpu",
                      "cpu": 8, "memory_gb": 16, "charge_type": "Postpay", "instance_price_per_hour": 12,
                      "cardless_hourly_cny": "0.60", "cardless_rate_source": "official"}
    with storage.connect(db) as conn, storage.transaction(conn):
        conn.execute("UPDATE sessions SET expires_at=?", (MIDNIGHT + 2 * 86400,))
        snapshot(conn, "projects", "", [PROJECT_ROW])
        for at in range(MIDNIGHT, day + 1, 60):
            observed = row if at <= MIDNIGHT + 600 else row | {"cardless_hourly_cny": None}
            dashboard_cost.observe(conn, PROJECT, [observed], {}, {}, at, 60)
        order = Order("1234", "Default", "cardless-zero", "OT_POSTPAID_PAYMENT", instance_id, "uhost", None, "Post",
                      MIDNIGHT + 600, MIDNIGHT, MIDNIGHT + 300, (0, 5, 0, 5))
        billing.publish(conn, PROJECT, CYCLE, day, day, "1234", ["Default"], [(order, "compute", instance_id)])
        assert conn.execute("SELECT count(*) FROM dashboard_cost_intervals").fetchone()[0] == 0
        token = auth.issue(conn, name="Read-only cardless fixture", manage=False, ssh=False, days=None)["token"]
    params = {"project_id": PROJECT, "range": "7d", "include_details": False}
    result = web.get("/api/dashboard/costs", params=params).json()
    estimate = result["today_estimate"]
    assert estimate["compute_cny"] == estimate["cardless_compute_cny"] == "0.0500"
    assert estimate["cardless_unpriced_hours"] == pytest.approx(300 / 3600)
    assert estimate["cardless_rate_sources"] == ["official"] and estimate["idle_cost_cny"] is None
    assert estimate["instances"][instance_id]["cardless_compute_cny"] == "0.0500"
    config = Config(db.parent, db.parent / "secrets", db.parent / "static", False, False)
    with TestClient(create_mcp_app(config), base_url=f"http://{config.mcp_bind_ip}:{config.mcp_bind_port}") as mcp:
        assert call_tool(mcp, token, "gpu_get_cost_summary", params) == result
    now[0] = MIDNIGHT + 86400
    rolled = web.get("/api/dashboard/costs", params=params).json()
    assert rolled["today_estimate"]["cardless_compute_cny"] is None
    assert rolled["billing"]["totals"]["cash_cny"] == "0.00"


def test_unverifiable_metered_orders_and_missing_rates_do_not_turn_into_zero_estimates(console):
    web, db, now = console
    day = MIDNIGHT + 900
    now[0] = day
    priced, manual, unknown = INSTANCE["id"], "manual", "unknown"
    paid_order = Order("1234", "Default", "no-service-window", "OT_POSTPAID_PAYMENT", priced, "uhost", None, "Post",
                       day, None, None, (50, 50, 0, 0))
    with storage.connect(db) as conn, storage.transaction(conn):
        for key, price in ((priced, "12"), (manual, None), (unknown, None)):
            _cost_intervals(conn, key, MIDNIGHT, day, price=price)
        conn.execute("INSERT INTO dashboard_cost_manual_rates VALUES (?,?,?,?)", (PROJECT, manual, MIDNIGHT, "0"))
        billing.publish(conn, PROJECT, CYCLE, day, day, "1234", ["Default"], [(paid_order, "compute", priced)])
    estimate = web.get("/api/dashboard/costs", params={"project_id": PROJECT, "range": "today"}).json()["today_estimate"]
    assert estimate["status"] == "review" and estimate["review_resource_count"] == 1
    assert estimate["unpriced_instance_hours"] == 0.25 and estimate["storage_cny"] is None
    assert estimate["instances"][manual]["compute_cny"] == "0.0000"
    assert estimate["instances"][unknown]["compute_cny"] is None
    assert estimate["instances"][priced]["compute_cny"] is None


def test_prior_month_orders_cover_today_and_expired_storage_rate_stops_at_expiry(console):
    web, db, now = console
    first_day = _midnight(date(2027, 2, 1))
    now[0] = first_day + 600
    instance_id = INSTANCE["id"]
    prior = Order("1234", "Default", "prior-cycle", "OT_POSTPAID_PAYMENT", instance_id, "uhost", None, "Post",
                  first_day - 60, first_day - 60, first_day + 300, (0, 25, 0, 25))
    with storage.connect(db) as conn, storage.transaction(conn):
        conn.execute("UPDATE sessions SET expires_at=?", (now[0] + 3600,))
        _cost_intervals(conn, instance_id, first_day - 60, now[0], price="12")
        quote(conn, DISK, "3.6", first_day - 86400 + 150)
        storage_cost.observe(conn, PROJECT, [
            (ZONE["region"], ZONE["zone"], "udisk", at, at, [DISK])
            for at in range(first_day, now[0] + 1, 300)
        ], estimate_costs=False)
        billing.publish(conn, PROJECT, "2027-01", first_day - 60, first_day - 60, "1234", ["Default"],
                        [(prior, "compute", instance_id)])
    estimate = web.get("/api/dashboard/costs", params={"project_id": PROJECT}).json()["today_estimate"]
    assert estimate["compute_cny"] == "1.0000" and estimate["storage_cny"] == "0.1500"
    assert estimate["status"] == "partial" and estimate["unpriced_storage_hours"] == 0.125


def test_gpu_cost_metrics_match_cash_windows_and_monitoring_without_duplicate_gpu_hours(console):
    web, db, now = console
    now[0] = MIDNIGHT + 1800
    iid = INSTANCE["id"]
    with storage.connect(db) as conn, storage.transaction(conn):
        token = auth.issue(conn, name="Billed rollup verification", manage=False, ssh=False, days=None)["token"]
        _cost_intervals(conn, iid, MIDNIGHT - 1200, MIDNIGHT, price="99", load=2.0, low_cards=("1",))
        _cost_intervals(conn, iid, MIDNIGHT, MIDNIGHT + 1200, price="99", load=0.25, low_cards=("1", "2", "3", "4"))
        _cost_intervals(conn, iid, MIDNIGHT + 1200, MIDNIGHT + 1500, price="99", load=None)
        base = Order("1234", "Default", "base", "OT_POSTPAID_PAYMENT", iid, "uhost", None, "Post",
                     MIDNIGHT + 1201, MIDNIGHT - 1200, MIDNIGHT + 1200, (2400, 3000, 0, 600))
        overlapping = replace(base, order_no="overlap", start_at=MIDNIGHT, end_at=MIDNIGHT + 600, amounts=(400, 400, 0, 0))
        no_monitor = replace(base, order_no="unmonitored", start_at=MIDNIGHT + 1200, end_at=MIDNIGHT + 1500,
                             amounts=(200, 200, 0, 0))
        refund = replace(base, order_no="refund", order_type="OT_REFUND", amounts=(-300, -300, 0, 0))
        storage_order = replace(base, order_no="disk", resource_type="udisk_ssd", amounts=(5000, 5000, 0, 0))
        billing.publish(conn, PROJECT, CYCLE, now[0], now[0], "1234", ["Default"],
                        [(order, "compute", iid) for order in (base, overlapping, no_monitor, refund)]
                        + [(storage_order, "storage", iid)])
    assert dashboard_cost_rollups.refresh(db, now[0], estimate_costs=False) == 2
    params = {"project_id": PROJECT, "range": "7d", "include_details": False}
    order_date = web.get("/api/dashboard/costs", params=params).json()
    service = web.get("/api/dashboard/costs", params=params | {"basis": "service_period", "instance_id": iid}).json()
    for report in (order_date, service):
        assert report["billing"]["totals"]["cash_cny"] == "77.00"
        assert report["estimated_cost_cny"] is None
        assert report["cost_metrics_basis"] == "paid_compute_service_period"
        assert report["matched_compute_cost_cny"] == "30.0000"
        assert report["evaluable_compute_cost_cny"] == "28.0000"
        assert report["low_load_cost_cny"] == "19.0000"
        assert report["idle_cost_cny"] == "16.0000"
        assert report["low_load_cost_ratio"] == pytest.approx(19 / 30)
        assert report["matched_gpu_hours"] == 3
        assert report["matched_monitored_gpu_hours"] == pytest.approx(8 / 3)
        assert report["priced_weighted_gpu_hours"] == pytest.approx(0.75)
        assert report["cost_per_weighted_gpu_hour_cny"] == "37.3333"
        assert report["days"][-2]["low_load_cost_cny"] == "3.0000"
        assert report["days"][-1]["idle_cost_cny"] == "16.0000"
        assert report["instances"][0]["cost_per_weighted_gpu_hour_cny"] == "37.3333"
    assert dashboard_billed_cost_rollups.refresh(db, now[0], limit=30) == 29
    assert web.get("/api/dashboard/costs", params=params).json() == order_date
    service_args = params | {"basis": "service_period", "instance_id": iid}
    assert web.get("/api/dashboard/costs", params=service_args).json() == service
    config = Config(db.parent, db.parent / "secrets", db.parent / "static", False, False)
    with TestClient(create_mcp_app(config), base_url=f"http://{config.mcp_bind_ip}:{config.mcp_bind_port}") as mcp:
        assert call_tool(mcp, token, "gpu_get_cost_summary", service_args) == service


def test_billed_cost_rollups_classify_late_samples_across_month_boundary(console, monkeypatch):
    web, db, now = console
    boundary = _midnight(date(2027, 2, 1))
    now[0] = boundary + 240
    iid = INSTANCE["id"]
    all_low = {"load": 0.0, "low_cards": ("1", "2", "3", "4")}
    order = Order("1234", "Default", "monthly", "OT_BUY", iid, "uhost", None, "Month",
                  boundary - 3600, boundary - 450, boundary + 450, (900, 900, 0, 0))
    with storage.connect(db) as conn, storage.transaction(conn):
        conn.execute("UPDATE sessions SET expires_at=?", (boundary + 7200,))
        # One interval straddles midnight. The series has only reached 11 minutes.
        _cost_intervals(conn, iid, boundary - 450, boundary + 210, **all_low)
        billing.publish(conn, PROJECT, "2027-01", now[0], now[0], "1234", ["Default"], [(order, "compute", iid)])
    args = {"project_id": PROJECT, "range": "7d", "basis": "service_period", "include_details": False}
    early = web.get("/api/dashboard/costs", params=args).json()
    assert early["days"][-2]["idle_cost_cny"] == "0.0000"
    assert dashboard_billed_cost_rollups.refresh(db, now[0], limit=30) == 29
    assert web.get("/api/dashboard/costs", params=args).json() == early

    now[0] = boundary + 500
    with storage.connect(db) as conn, storage.transaction(conn):
        _cost_intervals(conn, iid, boundary + 210, boundary + 450, **all_low)

    raw = dashboard_billed_cost.calculate
    scans = []

    def record(conn, project, start, end, as_of, instance=None):
        scans.append((start, end))
        return raw(conn, project, start, end, as_of, instance)

    monkeypatch.setattr(dashboard_billed_cost, "calculate", record)
    late = web.get("/api/dashboard/costs", params=args).json()
    assert scans == [(boundary - 86400, now[0])]
    assert late["days"][-2]["idle_cost_cny"] == late["days"][-1]["idle_cost_cny"] == "4.5000"
    assert late["idle_cost_cny"] == "9.0000"
    assert late["cost_per_weighted_gpu_hour_cny"] is None
    assert dashboard_billed_cost_rollups.refresh(db, now[0]) == 1
    scans.clear()
    assert web.get("/api/dashboard/costs", params=args).json() == late
    assert scans == [(boundary, now[0])]


def test_billed_cost_rollups_refresh_only_changed_bills_and_reject_stale_work(console, monkeypatch):
    web, db, now = console
    now[0] = MIDNIGHT + 1800
    iid, other = INSTANCE["id"], "uhost-other"
    start, finish = MIDNIGHT - 1800, MIDNIGHT - 900
    base = Order("1234", "Default", "order", "OT_POSTPAID_PAYMENT", iid, "uhost", None, "Post",
                 finish, start, finish, (900, 900, 0, 0))

    def publish(orders):
        now[0] += 1
        with storage.connect(db) as conn, storage.transaction(conn):
            billing.publish(conn, PROJECT, CYCLE, now[0], now[0], "1234", ["Default"], orders)

    with storage.connect(db) as conn, storage.transaction(conn):
        for instance in (iid, other):
            _cost_intervals(conn, instance, start, finish, load=0.5)
    publish([(base, "compute", iid)])
    assert dashboard_billed_cost_rollups.refresh(db, now[0], limit=30) == 29
    publish([(base, "compute", iid)])
    assert dashboard_billed_cost_rollups.refresh(db, now[0]) == 0

    reassigned = replace(base, resource_id=other, amounts=(1800, 1800, 0, 0))
    publish([(reassigned, "compute", other)])
    raw = dashboard_billed_cost.calculate

    def remove_order_during_calculation(*args, **kwargs):
        result = raw(*args, **kwargs)
        publish([])
        return result

    with monkeypatch.context() as patch:
        patch.setattr(dashboard_billed_cost, "calculate", remove_order_during_calculation)
        assert dashboard_billed_cost_rollups.refresh(db, now[0]) == 0

    args = {"project_id": PROJECT, "range": "7d", "include_details": False}
    removed = web.get("/api/dashboard/costs", params=args).json()
    assert removed["matched_compute_cost_cny"] is None
    assert dashboard_billed_cost_rollups.refresh(db, now[0]) == 1
    publish([(reassigned, "compute", other)])
    assert dashboard_billed_cost_rollups.refresh(db, now[0]) == 1
    report = web.get("/api/dashboard/costs", params=args).json()
    by_id = {row["instance_id"]: row for row in report["instances"]}
    assert by_id[iid]["matched_compute_cost_cny"] is None
    assert by_id[other]["matched_compute_cost_cny"] == report["matched_compute_cost_cny"] == "18.0000"


def test_gpu_cost_metrics_keep_zero_paid_cash_and_unknown_missing_bills_distinct(console):
    web, db, now = console
    now[0] = MIDNIGHT + 1500
    iid = INSTANCE["id"]
    with storage.connect(db) as conn, storage.transaction(conn):
        _cost_intervals(conn, iid, MIDNIGHT, MIDNIGHT + 900, price="99", load=0.3, low_cards=("1", "2", "3", "4"))
    params = {"project_id": PROJECT, "range": "today"}
    pending = web.get("/api/dashboard/costs", params=params).json()
    assert pending["idle_cost_cny"] is None and pending["matched_gpu_hours"] == 0
    with storage.connect(db) as conn, storage.transaction(conn):
        free = Order("1234", "Default", "credited", "OT_POSTPAID_PAYMENT", iid, "uhost", None, "Post",
                     MIDNIGHT + 960, MIDNIGHT, MIDNIGHT + 900, (0, 500, 0, 500))
        billing.publish(conn, PROJECT, CYCLE, now[0], now[0], "1234", ["Default"], [(free, "compute", iid)])
    cleared = web.get("/api/dashboard/costs", params=params).json()
    assert cleared["idle_cost_cny"] == "0.0000"
    assert cleared["low_load_cost_ratio"] is None
    assert cleared["cost_per_weighted_gpu_hour_cny"] == "0.0000"
