import json

from capacity_fixtures import record_capacity
from fastapi.testclient import TestClient

from gpu_console import storage
from gpu_console.adapters.notifications import DeliveryError
from gpu_console.api import create_app
from gpu_console.api.auth import COOKIE, digest
from gpu_console.application import availability, watches
from gpu_console.config import Config
from gpu_console.mcp import auth
from gpu_console.mcp.server import create_mcp_app
from gpu_console.storage import availability as observations
from gpu_console.worker.outbox import Outbox


def call_tool(client, token, name, arguments):
    response = client.post("/mcp", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}})
    assert response.status_code == 200
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def seed_watch_catalog(conn, spec, now):
    fields = ("gpu", "cpu", "memory_gb")
    listing = {key: spec[key] for key in ("region", "zone", "gpu_model")} | {
        "instance_type": "uhost", "status": "Normal", "specs": [{key: spec[key] for key in fields}],
    }
    conn.execute("INSERT INTO snapshots VALUES ('catalog','',?,?,NULL)", (json.dumps([listing]), now))
    conn.execute("INSERT INTO watches VALUES ('watch-a','project-a','GPU',?)", (json.dumps(spec),))
    watches.sync_bindings(conn)


def test_dashboard_web_and_mcp_share_project_scoped_data(tmp_path, monkeypatch):
    now = 1_797_926_400
    monkeypatch.setattr(storage, "now", lambda: now)
    cfg = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    origin = f"http://{cfg.mcp_bind_ip}:{cfg.mcp_bind_port}"
    with (
        TestClient(create_app(cfg), base_url=origin) as client,
        TestClient(create_mcp_app(cfg), base_url=origin) as mcp_client,
    ):
        with storage.connect(cfg.db) as conn:
            conn.execute("INSERT INTO sessions VALUES (?,?,?)", (digest("browser"), "csrf", now + 60))
            conn.execute("INSERT INTO snapshots(kind,scope,data,success_at) VALUES ('projects','',?,?)", (
                json.dumps([{"id": "project-a"}, {"id": "project-b"}]), now,
            ))
            seed_watch_catalog(conn, {"region": "r1", "zone": "z1", "gpu_model": "4090",
                                      "gpu": 2, "cpu": 16, "memory_gb": 64}, now)
            token = auth.issue(conn, name="Test client", manage=True, ssh=True, days=None)["token"]

        assert client.get("/api/dashboard/usage?project_id=project-a").status_code == 401
        client.cookies.set(COOKIE, "browser")
        usage = client.get("/api/dashboard/usage?project_id=project-a").json()
        assert usage["range"] == "7d" and usage["gpu_hours"] is None
        assert client.get("/api/dashboard/usage?project_id=unknown").status_code == 404

        assert call_tool(mcp_client, token, "gpu_get_usage_summary", {"project_id": "project-a"}) == usage
        filters = {"source": "watch", "project_id": "project-a", "key": "watch-a"}
        web = client.get("/api/dashboard/availability", params=filters).json()
        assert web["detail"]["reminder"] == {"mode": "off", "channels": ["site"]}
        mcp = call_tool(mcp_client, token, "gpu_get_availability_trends", filters | {"include_hourly": True})
        assert mcp.pop("next_offset") is None and mcp == web
        assert client.get("/api/dashboard/availability", params=filters | {"project_id": "project-b"}).status_code == 404

        path = "/api/dashboard/watches/watch-a/reminder"
        write_headers = {"Origin": origin, "x-csrf-token": "csrf"}
        assert client.put(path, params={"project_id": "project-a"}, json={"mode": "advance"}).status_code == 403
        assert client.put(path, params={"project_id": "project-b"}, json={"mode": "advance"},
                          headers=write_headers).status_code == 404
        assert client.put(path, params={"project_id": "project-a"}, json={"mode": "advance", "channels": ["feishu"]},
                          headers=write_headers).status_code == 409
        changed = client.put(path, params={"project_id": "project-a"}, json={"mode": "when_available"},
                             headers=write_headers)
        assert changed.status_code == 200 and changed.json() == {"mode": "when_available", "channels": ["site"]}
        assert client.get("/api/dashboard/availability", params=filters).json()["detail"]["reminder"] == changed.json()


def test_catalog_watch_and_dashboard_distinguish_capacity_from_pool_counts(tmp_path, monkeypatch):
    now = 1_797_926_400
    monkeypatch.setattr(storage, "now", lambda: now)
    cfg = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    origin = f"http://{cfg.mcp_bind_ip}:{cfg.mcp_bind_port}"
    parsed = {"region": "cn-wlcb", "zone": "cn-wlcb-01", "gpu_model": "5090"}
    listing = parsed | {"status": "Normal", "instance_type": "uhost",
                        "specs": [{"gpu": n, "cpu": 32, "memory_gb": 192} for n in (2, 8)] + [{"gpu": 2, "cpu": 48, "memory_gb": 256}]}
    with (
        TestClient(create_app(cfg), base_url=origin) as client,
        TestClient(create_mcp_app(cfg), base_url=origin) as mcp_client,
    ):
        with storage.connect(cfg.db) as conn, storage.transaction(conn):
            conn.execute("INSERT INTO sessions VALUES (?,?,?)", (digest("browser"), "csrf", now + 60))
            conn.execute("INSERT INTO snapshots(kind,scope,data,success_at) VALUES ('catalog','',?,?)", (
                json.dumps([listing, listing | {"gpu_model": "A100"},
                            listing | {"gpu_model": "2080Ti", "specs": [{"gpu": 4, "cpu": 32, "memory_gb": 192}]}]), now,
            ))
            observations.record_catalog(conn, [listing], now - 900)
            observations.record_catalog(conn, [listing], now)
            observations.record_pool(
                conn, parsed["region"], now, provider_updated_at=now - 5,
                counts={(parsed["zone"], "5090", "exclusive"): 12, (parsed["zone"], "5090", "spot"): 0},
            )
            observations.reconcile_pool_catalog(conn, observations.catalog_pools(storage.snapshot(conn, "catalog")["data"]))
            watch = {"id": "watch-a", "project_id": "project-a", "name": "双卡配置",
                     "spec": json.dumps(parsed | {"gpu": 2, "cpu": 32, "memory_gb": 192})}
            conn.execute("INSERT INTO watches VALUES (:id,:project_id,:name,:spec)", watch)
            watches.sync_bindings(conn)
            observations.record_watch(conn, watch, True, now)
            scope = parsed["region"], parsed["zone"], parsed["gpu_model"], "uhost"
            conn.execute("INSERT INTO snapshots VALUES ('projects','',?,?,NULL)",
                         (json.dumps([{"id": "project-a"}]), now))
            record_capacity(conn, "project-a", scope, {(2, 32, 192): False, (2, 48, 256): False}, now - 1)
            token = auth.issue(conn, name="Test client", manage=True, ssh=True, days=None)["token"]

        client.cookies.set(COOKIE, "browser")
        inventory = client.get("/api/inventory/pools").json()["data"]
        assert [(row["gpu_model"], row["pool"], row["remaining_cards"], row["state"]) for row in inventory] == [
            ("2080Ti", "exclusive", None, "unknown"), ("5090", "exclusive", 12, "known"),
            ("A100", "exclusive", None, "unknown"),
        ]
        assert inventory[1]["provider_updated_at"] == now - 5
        mcp = call_tool(mcp_client, token, "gpu_list_inventory", {"source": "pool"})
        assert mcp["items"] == inventory

        path = "/api/dashboard/availability"
        filters = parsed | {"source": "pool", "project_id": "project-a", "card_threshold": 2}
        trends = client.get(path, params=filters).json()
        exclusive, = trends["items"]
        assert trends["card_threshold"] == 2
        assert exclusive["remaining_cards"] == exclusive["median_cards"] == 12
        assert exclusive["known_count"] == 1 and exclusive["available_count"] == 0
        assert exclusive["available"] is False and exclusive["capacity_checked_at"] == now - 1
        browse = {"project_id": "project-a", "card_threshold": 2, "limit": 1}
        page_one = client.get(path, params=browse).json()
        page_two = client.get(path, params=browse | {"offset": 1}).json()
        assert page_one["total"] == page_two["total"] == 2
        assert page_one["items"] == [exclusive]
        unseen, = page_two["items"]
        assert unseen["gpu_model"] == "A100" and unseen["available"] is None and unseen["known_count"] == 0
        page_four = client.get(path, params=browse | {"card_threshold": 4}).json()
        four_card, = page_four["items"]
        assert four_card["gpu_model"] == "2080Ti"
        assert client.get(path, params=browse | {"key": four_card["key"]}).status_code == 404
        mcp_page = call_tool(mcp_client, token, "gpu_get_availability_trends", browse | {"offset": 1})
        assert mcp_page.pop("next_offset") is None and mcp_page == page_two
        assert client.get(path, params=filters | {"card_threshold": 3}).status_code == 422
        detail_filters = filters | {"key": exclusive["key"]}
        web = client.get(path, params=detail_filters).json()
        mcp = call_tool(mcp_client, token, "gpu_get_availability_trends", detail_filters | {"include_hourly": True})
        assert mcp.pop("next_offset") is None and mcp == web
        assert [h["median_cards"] for h in web["detail"]["hours"] if h["median_cards"] is not None] == [12]

        catalog_key = call_tool(mcp_client, token, "gpu_get_availability_trends", parsed | {"source": "catalog"})["items"][0]["key"]
        legacy = call_tool(mcp_client, token, "gpu_get_availability_trends", {"source": "catalog", "key": catalog_key})["detail"]
        assert legacy["item"]["known_count"] == legacy["item"]["available_count"] == 2
        assert legacy["recommended"] == legacy["tight"] == []
        watch_result = client.get(path, params={"source": "watch", "project_id": "project-a", "key": "watch-a"}).json()
        assert watch_result["detail"]["item"]["available"] is False
        assert watch_result["detail"]["item"]["remaining_cards"] == 12
        assert watch_result["detail"]["item"]["available_count"] == 0
        assert watch_result["detail"]["item"]["provider_updated_at"] == exclusive["provider_updated_at"]
        combos = client.get("/api/catalog").json()["data"][0]["specs"]
        assert [(item["gpu"], item["remaining_cards"], item["available"]) for item in combos] == [
            (2, 12, False), (8, 12, None), (2, 12, False),
        ]
        assert client.get("/api/catalog").json()["data"][0]["status"] == "Normal"

        receipt = client.post("/api/refresh", json={"kind": "pool"}, headers={"Origin": origin, "x-csrf-token": "csrf"})
        assert receipt.status_code == 202
        assert client.get("/api/refresh/" + receipt.json()["id"]).json()["state"] == "queued"


def test_unsubscribing_during_failed_delivery_does_not_requeue(tmp_path, monkeypatch):
    path = tmp_path / "console.sqlite3"
    now = 1_797_926_400
    monkeypatch.setattr(storage, "now", lambda: now)
    storage.initialize(path)
    subscription = {"project_id": "project-a", "watch_id": "watch-a"}
    with storage.connect(path) as conn, storage.transaction(conn):
        spec = {"region": "r1", "zone": "z1", "gpu_model": "4090", "gpu": 2, "cpu": 16, "memory_gb": 64}
        seed_watch_catalog(conn, spec, now)
        availability.set_reminder(conn, **subscription, mode="advance", channels=["feishu"])
        storage.emit(conn, "rental-window:project-a:watch-a:2026-12-22", subscription | {
            "mode": "advance", "window_start": now + 600, "window_end": now + 4200,
            "catalog_spec_id": watches.catalog_spec_id(spec),
        }, ["feishu"])

    class FailingChannel:
        def send(self, channel, payload, *, event_id):
            with storage.connect(path) as conn, storage.transaction(conn):
                availability.set_reminder(conn, **subscription, mode="off", channels=[])
            raise DeliveryError("simulated delivery timeout")

    Outbox(path, FailingChannel()).tick()
    with storage.connect(path) as conn:
        row = conn.execute("SELECT state,attempts FROM notifications").fetchone()
        assert dict(row) == {"state": "skipped", "attempts": 0}


def test_watch_writes_bind_only_official_combinations(tmp_path, monkeypatch):
    now = 1_797_926_400
    monkeypatch.setattr(storage, "now", lambda: now)
    cfg = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    spec = {"region": "r1", "zone": "z1", "gpu_model": "4090", "gpu": 2, "cpu": 16, "memory_gb": 64}
    with TestClient(create_app(cfg)) as client, storage.connect(cfg.db) as conn:
        conn.execute("INSERT INTO sessions VALUES (?,?,?)", (digest("browser"), "csrf", now + 60))
        conn.execute("INSERT INTO snapshots VALUES ('projects','',?,?,NULL)", (json.dumps([{"id": "project-a"}]), now))
        seed_watch_catalog(conn, spec, now)
        client.cookies.set(COOKIE, "browser")
        headers = {"x-csrf-token": "csrf"}
        key = client.get("/api/catalog").json()["data"][0]["specs"][0]["catalog_spec_id"]
        body = {"project_id": "project-a", "name": "两卡", "catalog_spec_id": key}
        duplicate = client.post("/api/watches", json=body, headers=headers)
        assert duplicate.status_code == 200
        assert duplicate.json()["id"] == "watch-a" and not duplicate.json()["created"]
        assert client.post("/api/watches", json=body | {"catalog_spec_id": "bad"}, headers=headers).status_code == 422
        assert client.post("/api/watches", json=body | {"project_id": "missing"}, headers=headers).status_code == 422
        conn.execute("DELETE FROM watches WHERE id='watch-a'")
        new = client.post("/api/watches", json=body, headers=headers)
        assert new.status_code == 201 and new.json()["binding_status"] == "bound"
        assert "cpu_platform" not in new.json()["spec"] and "image_id" not in new.json()["spec"]
