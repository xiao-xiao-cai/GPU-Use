import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.api import create_app
from gpu_console.api.auth import COOKIE, digest
from gpu_console.application import inventory_prices
from gpu_console.config import Config
from gpu_console.storage.catalog import compute_spec_id
from gpu_console.worker.instance_quotes import InventoryPriceWorker

from test_compshare import PROJECT, Wire, client

NOW = 1_800_000_000
COMBO = {"gpu": 2, "cpu": 32, "memory_gb": 128}
LISTING = {"region": "region-a", "zone": "zone-b", "gpu_model": "4090",
           "instance_type": "uhost", "status": "SoldOut", "specs": [COMBO]}
SPEC = LISTING | COMBO | {"project_id": PROJECT, "charge_type": "Postpay"}
KEY = compute_spec_id(SPEC)
ROW = {"ChargeType": "Postpay", "Instance": 3.79}
STANDARD = {"ChargeType": "Postpay", "Instance": 3.99}


@pytest.mark.parametrize(("responses", "expected", "actions"), [
    ([{"PriceDetails": [ROW], "ListPriceDetails": [STANDARD]}], ("3.79", "account"), ["UserPrice"]),
    ([{"PriceDetail": [ROW | {"Instance": 0}], "ListPriceDetails": [STANDARD]}], ("0", "account"), ["UserPrice"]),
    ([{"PriceDetails": [ROW | {"Quantity": 2}], "ListPriceDetails": [STANDARD]}], ("3.99", "catalog"), ["UserPrice"]),
    ([{"RetCode": 172}, {"PriceDetails": [ROW], "ListPriceDetails": [STANDARD]}], ("3.99", "catalog"), ["UserPrice", "Price"]),
])
def test_account_priority_and_catalog_fallback_on_signed_transport(responses, expected, actions):
    wire = Wire(*responses)
    value, source = client(wire).inventory_price(SPEC)
    assert (value, source) == (Decimal(expected[0]), expected[1])
    assert [item["Action"] for item, _ in wire.sent] == ["GetCompShareInstance" + action for action in actions]
    for params, retries in wire.sent:
        assert {key: params[key] for key in
                ("ProjectId", "Region", "Zone", "GpuType", "Gpu", "Cpu", "Memory", "ChargeType", "Quantity")} == {
            "ProjectId": PROJECT, "Region": "region-a", "Zone": "zone-b", "GpuType": "4090",
            "Gpu": "2", "Cpu": "32", "Memory": "131072", "ChargeType": "Postpay", "Quantity": "1",
        }
        assert retries == 0 and "CompShareImageId" not in params


def test_standard_fallback_rejects_discounted_and_wrong_period_amounts():
    wire = Wire({"PriceDetails": [{"ChargeType": "Spot", "Instance": 1}]},
                {"PriceDetails": [ROW], "ListPriceDetails": [STANDARD | {"ChargeType": "Day"}]})
    with pytest.raises(CloudError) as failure:
        client(wire).inventory_price(SPEC)
    assert failure.value.code == "quote_unverified"


def test_inventory_price_web_reuses_db_and_worker_refreshes_even_when_sold_out(tmp_path, monkeypatch):
    now = [NOW]
    monkeypatch.setattr(storage, "now", lambda: now[0])
    cfg = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    wire = Wire(
        {"PriceDetails": [ROW], "ListPriceDetails": [STANDARD]},
        {"ListPriceDetails": [STANDARD]},
        {"RetCode": 150}, {"RetCode": 150},
        {"PriceDetails": [ROW | {"Instance": 3.50}]},
        {"PriceDetails": [{"ChargeType": "Day", "Instance": 39.50}]},
    )
    cloud = client(wire)
    worker = InventoryPriceWorker(cfg.db, cloud, clock=lambda: now[0])
    body = {"project_id": PROJECT, "spec_id": KEY, "charge_type": "Postpay"}
    receipt = f"/api/inventory/prices/{KEY}"
    headers = {"x-csrf-token": "csrf"}
    with TestClient(create_app(cfg)) as web:
        with storage.connect(cfg.db) as db:
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (digest("browser"), "csrf", NOW + 30 * 86400))
            db.execute("INSERT INTO snapshots VALUES ('projects','',?,?,NULL)", (
                json.dumps([{"id": PROJECT}, {"id": "other-project"}]), NOW,
            ))
            db.execute("INSERT INTO snapshots VALUES ('catalog','',?,?,NULL)", (json.dumps([LISTING]), NOW))
        assert web.post("/api/inventory/prices", json=body, headers=headers).status_code == 401
        web.cookies.set(COOKIE, "browser")
        assert web.post("/api/inventory/prices", json=body).status_code == 403
        assert web.post("/api/inventory/prices", json=body | {"charge_type": "Spot"}, headers=headers).status_code == 422
        assert web.get(receipt, params={"project_id": "other-project", "charge_type": "Postpay"}).status_code == 404

        def request():
            response = web.post("/api/inventory/prices", json=body, headers=headers)
            assert response.status_code == 200
            return response.json()

        assert request()["state"] == request()["state"] == "queued"
        assert len(wire.sent) == 0 and worker.tick() is True
        account = request()
        assert (account["amount"], account["source"], account["billing_unit"], account["stale"]) == ("3.79", "account", "hour", False)
        assert request() == account and worker.tick() is False and len(wire.sent) == 1

        now[0] += 7 * 86400 - 1
        assert request() == account and worker.tick() is False and len(wire.sent) == 1
        now[0] += 1
        assert worker.tick() is True and len(wire.sent) == 2
        assert request()["amount"] == "3.99" and request()["source"] == "catalog"
        now[0] += 7 * 86400
        assert worker.tick() is True
        stale = request()
        assert stale["amount"] == "3.99" and stale["stale"]
        assert worker.tick() is False and len(wire.sent) == 4
        now[0] += 7 * 86400
        assert worker.tick() is True and len(wire.sent) == 5
        assert request()["amount"] == "3.5" and request()["source"] == "account"
        assert not request()["stale"]
        assert web.get(receipt, params={"project_id": PROJECT, "charge_type": "Day"}).status_code == 404
        assert web.post("/api/inventory/prices", json=body | {"charge_type": "Day"}, headers=headers).json()["state"] == "queued"
        assert worker.tick() is True and len(wire.sent) == 6
        assert web.get(receipt, params={"project_id": PROJECT, "charge_type": "Day"}).json()["billing_unit"] == "day"


def test_full_inventory_range_read_only_and_weekly_for_unvisited_specs(tmp_path, monkeypatch):
    now = [NOW]
    monkeypatch.setattr(storage, "now", lambda: now[0])
    cfg = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    second = {"gpu": 4, "cpu": 64, "memory_gb": 256}
    normal = LISTING | {"graphics_memory_gb": 24, "specs": [COMBO, second, second.copy()]}
    spot = normal | {"instance_type": "spot", "specs": [COMBO]}
    second_id = compute_spec_id(normal | second)
    spot_id = compute_spec_id(spot | COMBO)
    wire = Wire(
        {"ListPriceDetails": [{"ChargeType": "Postpay", "Instance": 0}]},
        {"PriceDetails": [{"ChargeType": "Postpay", "Instance": 4}]},
        {"PriceDetails": [{"ChargeType": "Postpay", "Instance": 8}]},
    )
    worker = InventoryPriceWorker(cfg.db, client(wire), clock=lambda: now[0])
    with TestClient(create_app(cfg)) as web, storage.connect(cfg.db) as db:
        from gpu_console.storage.availability import record_pool

        db.execute("INSERT INTO sessions VALUES (?,?,?)", (digest("browser"), "csrf", NOW + 90 * 86400))
        db.execute("INSERT INTO snapshots VALUES ('projects','',?,?,NULL)", (json.dumps([{"id": PROJECT}]), NOW))
        db.execute("INSERT INTO snapshots VALUES ('catalog','',?,?,NULL)", (json.dumps([normal, spot]), NOW))
        record_pool(db, normal["region"], NOW, provider_updated_at=NOW, counts={
            (normal["zone"], normal["gpu_model"], "exclusive"): 0,
            (normal["zone"], normal["gpu_model"], "spot"): 2,
        })
        db.execute("INSERT INTO inventory_prices VALUES (?,?,?,?,?,'ready',?,?,?,NULL)",
                   (PROJECT, KEY, "Postpay", NOW, NOW + 7 * 86400, "3.79", "account", NOW))
        path = "/api/inventory/overview"
        assert web.get(path).status_code == 401
        web.cookies.set(COOKIE, "browser")

        def overview(**kwargs):
            response = web.get(path, params={"project_id": PROJECT} | kwargs)
            assert response.status_code == 200
            return response.json()["data"]

        incomplete, = overview()
        assert incomplete["remaining_cards"] == 0 and incomplete["graphics_memory_gb"] == 24
        assert incomplete["card_counts"] == [2, 4]
        assert incomplete["hourly_range"] == {
            "total": 2, "quoted": 1, "minimum": None, "maximum": None, "failed": 0, "source": None, "stale": False,
        }
        assert len(overview(include_spot=True)) == 2
        assert db.execute("SELECT COUNT(*) FROM inventory_prices").fetchone()[0] == 1 and not wire.sent

        with storage.transaction(db):
            inventory_prices.sync_targets(db, now[0])
            inventory_prices.sync_targets(db, now[0] + 1)
        assert db.execute("SELECT spec_id FROM inventory_prices WHERE charge_type='Spot'").fetchone() is None
        assert db.execute("SELECT next_at FROM inventory_prices WHERE spec_id=?", (KEY,)).fetchone()[0] == NOW + 7 * 86400
        assert worker.tick() is True and worker.tick() is False and len(wire.sent) == 1
        complete, optional = overview(include_spot=True)
        assert optional["pool"] == "spot" and optional["specs"][0]["catalog_spec_id"] == spot_id
        assert complete["hourly_range"] == {
            "total": 2, "quoted": 2, "minimum": "0", "maximum": "3.79", "failed": 0, "source": "mixed", "stale": False,
        }
        assert [spec["available"] for spec in complete["specs"]] == [None, None]
        assert overview() == [complete] and len(wire.sent) == 1

        now[0] += 7 * 86400 - 1
        assert worker.tick() is False
        now[0] += 1
        assert overview()[0]["hourly_range"]["stale"]
        assert worker.tick() is True and worker.tick() is True and worker.tick() is False
        assert not overview()[0]["hourly_range"]["stale"]

        with storage.transaction(db):
            inventory_prices.trim(db, NOW + 40 * 86400)
            assert db.execute("SELECT COUNT(*) FROM inventory_prices").fetchone()[0] == 2
            db.execute("UPDATE snapshots SET error='平台请求失败' WHERE kind='catalog'")
            inventory_prices.sync_targets(db, now[0])
            assert db.execute("SELECT COUNT(*) FROM inventory_price_targets").fetchone()[0] == 2
            db.execute("UPDATE snapshots SET data=?,error=NULL WHERE kind='catalog'", (json.dumps([LISTING]),))
            inventory_prices.sync_targets(db, now[0])
            assert db.execute("SELECT spec_id FROM inventory_price_targets WHERE spec_id=?", (second_id,)).fetchone() is None
            inventory_prices.trim(db, NOW + 40 * 86400)
            assert db.execute("SELECT COUNT(*) FROM inventory_prices").fetchone()[0] == 1
