import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.api import create_app
from gpu_console.application import instance_quotes
from gpu_console.application.tasks import InstanceActionUnavailable, TaskStore
from gpu_console.config import Config
from gpu_console.storage.maintenance import backup, confirm_restore, restore
from gpu_console.worker.instance_quotes import QuoteWorker
from gpu_console.worker.tasks import TaskRunner


SPEC = {
    "region": "r1", "zone": "z1", "gpu_model": "RTX4090", "gpu": 1, "cpu": 16, "memory_gb": 64,
    "cpu_platform": "AMD", "image_id": "image1", "charge_type": "Day", "quantity": 2,
    "disks": [{"purpose": "system", "type": "CLOUD_SSD", "size_gb": 80}],
}


class Clock:
    def __init__(self):
        self.now = storage.now()

    def __call__(self):
        return self.now


class Cloud:
    def __init__(self):
        self.price = Decimal("40.50")
        self.stock = True
        self.reads = []
        self.writes = []
        self.creation_error = None
        self.instances_readback = []

    def capacity(self, spec):
        self.reads.append(("capacity", spec))
        return self.stock

    def quote(self, spec):
        self.reads.append(("quote", spec))
        return self.price

    def create(self, spec, remark):
        self.writes.append((spec, remark))
        if self.creation_error is not None:
            raise self.creation_error
        return ["created"]

    def instances(self, project_id):
        assert project_id == "p1"
        return self.instances_readback


def seed_projects(path):
    with storage.connect(path) as conn:
        conn.execute(
            "INSERT INTO snapshots(kind,scope,data,success_at) VALUES ('projects','',?,?)",
            (json.dumps([{"id": "p1"}]), storage.now()),
        )
        conn.execute("INSERT INTO snapshots VALUES ('catalog','',?,?,NULL)", (json.dumps([{
            "region": SPEC["region"], "zone": SPEC["zone"], "gpu_model": SPEC["gpu_model"], "instance_type": "uhost",
            "disks": [{"purpose": "system", "type": "CLOUD_SSD", "default_size_gb": 80}],
            "specs": [{"gpu": 1, "cpu": 16, "memory_gb": 64, "cpu_platforms": ["Intel/Auto", "Amd/Auto"]}],
        }]), storage.now()))


@pytest.fixture
def runtime(tmp_path):
    path = tmp_path / "purchase.db"
    storage.initialize(path)
    seed_projects(path)
    clock, cloud = Clock(), Cloud()
    return TaskStore(path, clock), clock, cloud, QuoteWorker(path, cloud, clock=clock)


def quote(store, clock, worker):
    with storage.connect(store.path) as conn:
        queued = instance_quotes.create(conn, "p1", SPEC, clock())
    assert queued["state"] == "queued"
    assert worker.tick()
    with storage.connect(store.path) as conn:
        return instance_quotes.view(instance_quotes.get(conn, queued["id"]), clock())


def test_quote_and_create_through_authenticated_http(tmp_path):
    config = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    credentials = {"username": "operator", "password": "simulation-password-123"}
    with TestClient(create_app(config)) as client:
        seed_projects(config.db)
        with storage.connect(config.db) as conn:
            conn.execute("INSERT INTO administrators VALUES (1,?,?)", (
                credentials["username"], PasswordHash.recommended().hash(credentials["password"]),
            ))
        quote_body = {"project_id": "p1", "spec": SPEC}
        assert client.post("/api/instance-quotes", json=quote_body).status_code == 401
        assert client.post("/api/instances", json={"quote_id": "unknown", "request_id": "r"}).status_code == 401
        headers = {"x-csrf-token": client.post("/api/login", json=credentials).json()["csrf_token"]}
        assert client.post("/api/instance-quotes", json=quote_body).status_code == 403
        assert client.post("/api/instance-quotes", headers=headers, json=quote_body | {"project_id": "unknown"}).status_code == 422
        response = client.post("/api/instance-quotes", headers=headers, json=quote_body)
        assert response.status_code == 202
        queued = response.json()
        quote_path = "/api/instance-quotes/" + queued["id"]
        assert client.get(quote_path).json() == queued
        assert queued["price"] is None and queued["state"] == "queued"
        cloud = Cloud()
        assert QuoteWorker(config.db, cloud).tick()
        ready = client.get(quote_path).json()
        assert ready["state"] == "ready"
        assert ready["price"] == "40.50" and ready["expires_at"] - ready["quoted_at"] == 300
        assert cloud.reads == [(action, SPEC | {"project_id": "p1"}) for action in ("capacity", "quote")]
        assert cloud.writes == []
        purchase = {"quote_id": ready["id"], "request_id": "request-one"}
        assert client.post("/api/instances", json=purchase).status_code == 403
        purchase |= {"channels": ["email"], "email": "operator@example.com"}
        assert client.post("/api/instances", headers=headers, json=purchase).status_code == 422
        with storage.connect(config.db) as conn:
            storage.record_status(conn, "channel_email", True, "configured")
        invalid_email = purchase | {"email": "invalid"}
        assert client.post("/api/instances", headers=headers, json=invalid_email).status_code == 422
        accepted = client.post("/api/instances", headers=headers, json=purchase)
        assert accepted.status_code == 202
        run = accepted.json()
        assert run["state"] == "queued" and run["task_id"] is None
        assert run["snapshot"]["config"]["max_price"] == ready["price"]
        assert run["snapshot"]["config"]["quantity"] == SPEC["quantity"]
        assert run["snapshot"]["channels"] == ["site", "email"]
        assert run["snapshot"]["config"]["email"] == "operator@example.com"
        assert client.post("/api/instances", headers=headers, json=purchase).json()["id"] == run["id"]
        assert client.post("/api/instances", headers=headers, json=purchase | {
            "email": "other@example.com",
        }).status_code == 409
        used = client.get(quote_path).json()
        assert used["state"] == "used" and used["run_id"] == run["id"]
        assert client.get("/api/runs/" + run["id"]).json()["id"] == run["id"]
        conflict = client.post("/api/instances", headers=headers, json=purchase | {"request_id": "another"})
        assert conflict.status_code == 409
        canceled = client.post(f"/api/runs/{run['id']}/cancel", headers=headers)
        assert canceled.json()["state"] == "canceled"
        TaskRunner(TaskStore(config.db), cloud).tick()
        assert cloud.writes == []


def test_quote_expiry_and_idempotent_replay_after_gate(runtime):
    store, clock, cloud, worker = runtime
    expired = quote(store, clock, worker)
    clock.now = expired["expires_at"]
    with pytest.raises(InstanceActionUnavailable, match="报价已过期"):
        store.submit_purchase(expired["id"], "never")
    confirmed = quote(store, clock, worker)
    with pytest.raises(InstanceActionUnavailable, match="只读或恢复核对"):
        store.submit_purchase(confirmed["id"], "never-read-only", read_only=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(lambda _: store.submit_purchase(confirmed["id"], "same"), range(2)))
    assert runs[0]["id"] == runs[1]["id"]
    clock.now = confirmed["expires_at"]
    assert store.submit_purchase(confirmed["id"], "same", read_only=True)["id"] == runs[0]["id"]
    with pytest.raises(InstanceActionUnavailable, match="请求标识已用于另一项操作"):
        store.submit_purchase(expired["id"], "same")
    TaskRunner(store, cloud).tick()
    assert store.get_run(runs[0]["id"])["state"] == "expired"
    assert cloud.writes == []


def test_catalog_change_before_confirming_quote_leaves_no_run(runtime):
    store, clock, cloud, worker = runtime
    confirmed = quote(store, clock, worker)
    with storage.connect(store.path) as conn:
        conn.execute("""UPDATE snapshots SET data=json_set(data, '$[0].disks[0].default_size_gb', 100)
                        WHERE kind='catalog' AND scope=''""")
    with pytest.raises(InstanceActionUnavailable, match="系统盘与已核实的平台默认值不符"):
        store.submit_purchase(confirmed["id"], "request-one")
    assert store.list_runs() == [] and cloud.writes == []


@pytest.mark.parametrize(("outcome", "writes", "message"), [
    ("price-rise", 0, "高于已确认金额"),
    ("sold-out", 0, "当前缺货"),
    ("stock-unknown", 0, "无法核实目标规格的库存"),
    ("rejected", 1, "当前资源不足"),
    ("restore-gate", 0, "云端写操作暂停"),
])
def test_confirmed_purchase_rechecks_and_stops_on_known_failure(runtime, outcome, writes, message):
    store, clock, cloud, worker = runtime
    confirmed = quote(store, clock, worker)
    run = store.submit_purchase(confirmed["id"], "request-one")
    if outcome == "price-rise":
        cloud.price = Decimal("40.500001")
    elif outcome == "sold-out":
        cloud.stock = False
    elif outcome == "stock-unknown":
        cloud.stock = None
    elif outcome == "rejected":
        cloud.creation_error = CloudError("226604", retryable=True)
    elif outcome == "restore-gate":
        with storage.connect(store.path) as conn:
            storage.set_settings(conn, {"restore_gate": True})
    runner = TaskRunner(store, cloud)
    runner.tick()
    result = store.get_run(run["id"])
    assert result["state"] == "failed" and message in result["reason"]
    clock.now += 20
    runner.tick()
    assert len(cloud.writes) == writes


def test_unknown_create_keeps_reservation_and_reconciles_after_restart(runtime):
    store, clock, cloud, worker = runtime
    confirmed = quote(store, clock, worker)
    run = store.submit_purchase(confirmed["id"], "request-one")
    cloud.price = Decimal("39")
    cloud.creation_error = CloudError("503", uncertain=True, retryable=True)
    TaskRunner(store, cloud).tick()
    assert cloud.writes == [(SPEC | {"project_id": "p1"}, run["id"])]
    reconciling = store.get_run(run["id"])
    assert reconciling["state"] == "reconciling"
    assert reconciling["operations"][0]["state"] == "reserved"
    clock.now = confirmed["expires_at"] + 1
    restarted = TaskRunner(TaskStore(store.path, clock), cloud)
    assert restarted.tick() == 1
    assert store.get_run(run["id"])["result"]["reviewable"] is True
    competing = store.submit_purchase(quote(store, clock, worker)["id"], "request-two")
    restarted.tick()
    assert "未决采购" in store.get_run(competing["id"])["reason"]
    cloud.instances_readback = [SPEC | {
        "id": "created", "project_id": "p1", "remark": run["id"], "state": "Running", "gpu_mode": "with_gpu",
    }]
    clock.now += 20
    restarted.tick()
    result = store.get_run(run["id"])
    assert result["state"] == "succeeded" and result["result"]["instance_id"] == "created"
    assert len(cloud.writes) == 1


def test_quote_expiring_during_final_check_prevents_dispatch(runtime, monkeypatch):
    store, clock, cloud, worker = runtime
    confirmed = quote(store, clock, worker)
    run = store.submit_purchase(confirmed["id"], "request-one")

    def advance_during_quote(_spec):
        clock.now = confirmed["expires_at"]
        return cloud.price

    monkeypatch.setattr(cloud, "quote", advance_during_quote)
    runner = TaskRunner(store, cloud)
    runner.tick()
    runner.tick()
    expired = store.get_run(run["id"])
    assert expired["state"] == "expired" and expired["operations"] == []
    assert cloud.writes == []


def test_restore_of_pre_purchase_backup_invalidates_preexisting_quote(runtime, tmp_path):
    store, clock, cloud, worker = runtime
    confirmed = quote(store, clock, worker)
    before = tmp_path / "before.sqlite3"
    backup(store.path, before)
    store.submit_purchase(confirmed["id"], "request-one")
    TaskRunner(store, cloud).tick()
    assert len(cloud.writes) == 1
    restore(store.path, before)
    with pytest.raises(InstanceActionUnavailable, match="只读或恢复核对"):
        store.submit_purchase(confirmed["id"], "request-one")
    confirm_restore(store.path, "已核对模拟云端的订单和实例")
    with pytest.raises(InstanceActionUnavailable, match="请重新询价"):
        store.submit_purchase(confirmed["id"], "request-one")
    assert TaskRunner(store, cloud).tick() == 0
    assert len(cloud.writes) == 1


def test_auto_cpu_tries_official_platforms_and_pins_the_quote(runtime, monkeypatch):
    store, clock, cloud, worker = runtime
    spec = {key: value for key, value in SPEC.items() if key != "cpu_platform"}
    attempts = []

    def capacity(candidate):
        attempts.append((candidate["cpu_platform"], candidate["gpu"], candidate["cpu"], candidate["memory_gb"]))
        return candidate["cpu_platform"] == "Intel/Auto"

    monkeypatch.setattr(cloud, "capacity", capacity)
    with storage.connect(store.path) as conn:
        with pytest.raises(ValueError, match="平台目录没有"):
            instance_quotes.create(conn, "p1", spec | {"cpu": 32}, clock())
        queued = instance_quotes.create(conn, "p1", spec, clock())
    assert worker.tick()
    assert attempts == [("Amd/Auto", 1, 16, 64), ("Intel/Auto", 1, 16, 64)]
    run = store.submit_purchase(queued["id"], "auto-cpu")
    assert run["snapshot"]["config"]["cpu_platform"] == "Intel/Auto"

    def changed_stock(candidate):
        attempts.append((candidate["cpu_platform"], candidate["gpu"], candidate["cpu"], candidate["memory_gb"]))
        return candidate["cpu_platform"] == "Amd/Auto"

    monkeypatch.setattr(cloud, "capacity", changed_stock)
    TaskRunner(store, cloud).tick()
    assert attempts[-1] == ("Intel/Auto", 1, 16, 64)
    assert len(attempts) == 3 and not cloud.writes
