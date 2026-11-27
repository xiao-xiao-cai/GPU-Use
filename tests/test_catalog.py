import json

from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from gpu_console import storage
from gpu_console.adapters.compshare import PAGE_SIZE, CloudError, ImagePage
from gpu_console.api import create_app
from gpu_console.config import Config
from gpu_console.storage import catalog, refresh
from gpu_console.storage.catalog import IMAGE_INTERVAL, IMAGE_KINDS
from gpu_console.worker.catalog import ImageCollector
from gpu_console.worker.collection import Collector


PROJECT, OTHER_PROJECT = "org-one", "org-two"
REGION, ZONE, OTHER_ZONE = "cn-bj2", "cn-bj2-02", "cn-bj2-03"
SCOPE = {"project_id": PROJECT, "region": REGION, "zone": ZONE}


class ImageCloud:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.catalog_fail = False

    def projects(self):
        return [{"id": pid, "name": pid} for pid in (PROJECT, OTHER_PROJECT)]

    def zones(self):
        return [
            {"region": REGION, "zone": ZONE, "name": "平台北京二B", "is_pod": False},
            {"region": REGION, "zone": OTHER_ZONE, "name": OTHER_ZONE, "is_pod": False},
            {"region": "other", "zone": "unknown-zone", "name": "unknown-zone", "is_pod": False},
        ]

    def catalog(self, *, zones):
        assert zones == self.zones()
        if self.catalog_fail:
            raise CloudError("503")
        return []

    def instances(self, _project_id):
        return []

    def image_page(self, project_id, region, zone, kind, offset):
        self.calls.append((project_id, region, zone, kind, offset))
        if kind == "custom" and not offset:
            return ImagePage([], {f"filtered-{i}" for i in range(PAGE_SIZE)}, None)
        if kind == "custom" and self.fail:
            return ImagePage([], {"filtered-0"}, PAGE_SIZE + 1)
        image_id = f"{project_id}-{zone}-{kind}"
        return ImagePage([{"id": image_id, "name": image_id, "kind": kind}], {image_id}, offset + 1)


def test_account_scoped_image_refresh_tracks_each_kind_and_preserves_failed_cache(tmp_path, monkeypatch):
    config = Config(tmp_path, tmp_path / "secrets", tmp_path / "static", False, False)
    now = [1000]
    monkeypatch.setattr(storage, "now", lambda: now[0])
    cloud = ImageCloud()
    collector = ImageCollector(config.db, cloud)
    login = {"username": "operator", "password": "stub-password-12345"}
    with TestClient(create_app(config)) as client, storage.connect(config.db) as conn:
        assert client.get("/api/locations").status_code == 401
        conn.execute(
            "INSERT INTO administrators VALUES (1,?,?)",
            (login["username"], PasswordHash.recommended().hash(login["password"])),
        )
        headers = {"x-csrf-token": client.post("/api/login", json=login).json()["csrf_token"]}
        cloud.catalog_fail = True
        Collector(config.db, cloud).tick()
        assert client.get("/api/catalog").json()["error"]
        locations = client.get("/api/locations").json()
        assert locations["success_at"] == now[0]
        by_zone = {row["zone"]: row for row in locations["data"]}
        assert by_zone[ZONE]["zone_name"] == "平台北京二B"
        assert by_zone[OTHER_ZONE]["zone_name"] == "华北（北京2）可用区C"
        assert by_zone[ZONE]["region_name"] == "华北（北京2）"
        assert by_zone["unknown-zone"]["zone_name"] == "unknown-zone"

        def get(scope=SCOPE):
            response = client.get("/api/images", params=scope)
            assert response.status_code == 200
            return response.json()

        def queue(scope=SCOPE):
            response = client.post("/api/images/refresh", json=scope, headers=headers)
            assert response.status_code == 202
            return response.json()["id"]

        def tick(count):
            before = len(cloud.calls)
            for _ in range(count):
                now[0] += 2
                collector.tick()
            assert len(cloud.calls) == before + count

        assert get()["refresh_id"] is None
        assert not conn.execute("SELECT 1 FROM image_sources").fetchone()
        assert client.post("/api/images/refresh", json=SCOPE).status_code == 403
        assert client.post("/api/images/refresh", json=SCOPE | {"project_id": "unlisted"}, headers=headers).status_code == 404
        other_project = SCOPE | {"project_id": OTHER_PROJECT}
        other_zone = SCOPE | {"zone": OTHER_ZONE}
        batch_id, other_batch = queue(), queue(other_project)
        assert queue() == batch_id and get()["refresh_id"] == batch_id
        assert client.get(f"/api/refresh/{batch_id}").json()["state"] == "queued"

        tick(3)
        assert get()["custom"]["success_at"] is None
        tick(7)
        for receipt in (batch_id, other_batch):
            result = client.get(f"/api/refresh/{receipt}").json()
            assert result["state"] == "succeeded" and len(result["outcomes"]) == 4
        cached = get()
        assert cached["refresh_id"] is None
        assert [kind for kind in IMAGE_KINDS if cached[kind]["data"]] == list(IMAGE_KINDS)
        assert cached["custom"]["data"][0]["id"] == f"{PROJECT}-{ZONE}-custom"
        assert get(other_project)["custom"]["data"][0]["id"] == f"{OTHER_PROJECT}-{ZONE}-custom"
        assert get(other_zone)["custom"]["data"] == []

        cloud.fail = True
        failed_batch = queue()
        tick(5)
        failed = client.get(f"/api/refresh/{failed_batch}").json()
        assert failed["state"] == "failed"
        assert sum(bool(outcome["error"]) for outcome in failed["outcomes"]) == 1
        latest = get()
        assert latest["custom"]["data"] == cached["custom"]["data"]
        assert latest["custom"]["success_at"] == cached["custom"]["success_at"] and latest["custom"]["error"]
        assert latest["shared"]["success_at"] > cached["shared"]["success_at"] and latest["shared"]["error"] is None

        now[0] += IMAGE_INTERVAL
        cloud.fail = False
        tick(10)
        assert get()["custom"]["error"] is None
        assert get()["refresh_id"] is None


def test_upgrade_requeues_existing_image_sources_once_and_retains_receipts(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.sqlite"
    now = [1000]
    monkeypatch.setattr(storage, "now", lambda: now[0])
    storage.initialize(path)
    cached_scope = json.dumps([PROJECT, REGION, ZONE], separators=(",", ":"))
    pending_scope = json.dumps([OTHER_PROJECT, REGION, ZONE], separators=(",", ":"))
    cached_image = {"id": "old-image", "name": "Ubuntu", "kind": "system"}

    with storage.connect(path) as conn, storage.transaction(conn):
        conn.execute("DELETE FROM image_catalog_migrations")
        conn.executemany("INSERT INTO image_sources VALUES (?,?,?,NULL)",
                         [(cached_scope, kind, now[0] + IMAGE_INTERVAL) for kind in IMAGE_KINDS])
        conn.execute("INSERT INTO snapshots VALUES ('images',?,?,?,NULL)",
                     (f"system:{cached_scope}", json.dumps([cached_image]), now[0]))
        existing_receipt = catalog.queue_images(conn, OTHER_PROJECT, REGION, ZONE)
        finished = conn.execute("SELECT * FROM image_sources WHERE scope=? AND kind='system'", (pending_scope,)).fetchone()
        catalog.finish_image_source(conn, finished, now[0], [cached_image], None)

    now[0] += 1
    storage.initialize(path)
    with storage.connect(path) as conn:
        upgraded = catalog.image_snapshots(conn, PROJECT, REGION, ZONE)
        assert upgraded["system"]["data"] == [cached_image]
        new_receipt = upgraded["refresh_id"]
        assert new_receipt and new_receipt != existing_receipt
        assert len(refresh.get(conn, new_receipt)["outcomes"]) == len(IMAGE_KINDS)
        assert catalog.image_snapshots(conn, OTHER_PROJECT, REGION, ZONE)["refresh_id"] == existing_receipt
        prior = refresh.get(conn, existing_receipt)
        assert sum(outcome["finished_at"] is not None for outcome in prior["outcomes"]) == 1
        assert conn.execute("SELECT next_at FROM image_sources WHERE scope=? AND kind='system'", (pending_scope,)).fetchone()[0] == now[0]
        next_sync = now[0] + IMAGE_INTERVAL
        conn.execute("UPDATE image_sources SET next_at=? WHERE scope=? AND kind='system'", (next_sync, pending_scope))

    now[0] += 1
    storage.initialize(path)
    with storage.connect(path) as conn:
        assert catalog.image_snapshots(conn, PROJECT, REGION, ZONE)["refresh_id"] == new_receipt
        assert catalog.image_snapshots(conn, OTHER_PROJECT, REGION, ZONE)["refresh_id"] == existing_receipt
        assert conn.execute("SELECT next_at FROM image_sources WHERE scope=? AND kind='system'", (pending_scope,)).fetchone()[0] == next_sync
