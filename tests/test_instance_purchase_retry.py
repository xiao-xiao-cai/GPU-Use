import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.api import create_app
from gpu_console.application.tasks import InstanceActionUnavailable, TaskStore
from gpu_console.config import Config
from gpu_console.worker.tasks import TaskRunner


SYSTEM_DISK = {"purpose": "system", "type": "CLOUD_SSD", "size_gb": 100}
SPEC = {
    "region": "cn-wlcb", "zone": "cn-wlcb-01", "gpu_model": "5090", "gpu": 1, "cpu": 16,
    "memory_gb": 96, "cpu_platform": "Intel/Auto", "image_id": "image-1", "charge_type": "Postpay", "disks": [],
}
PREVIEW = {"project_id": "p1", "spec": SPEC, "max_price": "4.50", "window_seconds": 300}


def seed_catalog(path):
    with storage.connect(path) as conn:
        conn.executemany("INSERT INTO snapshots(kind,scope,data,success_at) VALUES(?,'',?,?)", [
            ("projects", json.dumps([{"id": "p1"}]), storage.now()),
            ("catalog", json.dumps([{
                **{key: SPEC[key] for key in ("region", "zone", "gpu_model")}, "instance_type": "uhost",
                "disks": [{"purpose": "system", "type": "CLOUD_SSD", "min_size_gb": 100, "max_size_gb": 100}],
            }]), storage.now()),
        ])


class Clock:
    def __init__(self):
        self.time = storage.now()

    def __call__(self):
        return self.time


class Cloud:
    def __init__(self):
        self.stock = False
        self.price = Decimal("4.00")
        self.create_error = None
        self.creates = []
        self.items = []

    def capacity(self, spec):
        assert spec["disks"] == [SYSTEM_DISK]
        return self.stock

    def quote(self, spec):
        return self.price

    def create(self, spec, remark):
        self.creates.append((spec, remark))
        if self.create_error is not None:
            raise self.create_error
        return ["created-1"]

    def instances(self, project_id):
        assert project_id == "p1"
        return self.items


@pytest.fixture
def runtime(tmp_path):
    path = tmp_path / "retry.db"
    storage.initialize(path)
    seed_catalog(path)
    clock, cloud = Clock(), Cloud()
    return TaskStore(path, clock), clock, cloud


def submit(store, **options):
    preview = store.preview_purchase_retry(PREVIEW | options)
    return store.submit_purchase_retry(preview["preview_id"], "request-one")


def tick_due(store, clock, cloud, run_id):
    clock.time = store.get_run(run_id)["next_check"]
    TaskRunner(store, cloud).tick()
    return store.get_run(run_id)


def test_preview_and_submit_share_resolved_confirmation_over_http(tmp_path):
    config = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    credentials = {"username": "operator", "password": "simulation-password-123"}
    base = "/api/instance-purchase-requests"
    with TestClient(create_app(config)) as client:
        seed_catalog(config.db)
        with storage.connect(config.db) as conn:
            conn.execute("INSERT INTO administrators VALUES (1,?,?)", (
                credentials["username"], PasswordHash.recommended().hash(credentials["password"]),
            ))
        assert client.post(base + "/preview", json=PREVIEW).status_code == 401
        headers = {"x-csrf-token": client.post("/api/login", json=credentials).json()["csrf_token"]}
        assert client.post(base + "/preview", json=PREVIEW).status_code == 403
        response = client.post(base + "/preview", headers=headers, json=PREVIEW)
        assert response.status_code == 200
        preview = response.json()
        assert preview["spec"]["disks"] == [SYSTEM_DISK]
        assert preview["price_unit"] == "CNY/hour"
        assert preview["retry"] is True and preview["channels"] == ["site"]
        assert client.get("/api/runs").json() == []
        request = {"preview_id": preview["preview_id"], "request_id": "request-one"}
        accepted = client.post(base, headers=headers, json=request)
        assert accepted.status_code == 202
        run = accepted.json()
        assert run["state"] == "queued" and run["snapshot"]["config"]["max_price"] == "4.5"
        assert run["deadline_at"] - run["scheduled_at"] == 300
        assert client.post(base, headers=headers, json=request).json()["id"] == run["id"]
        assert client.post(base, headers=headers, json=request | {"request_id": "different"}).status_code == 409


def test_shortage_known_rejection_and_success_send_at_most_one_order(runtime):
    store, clock, cloud = runtime
    run_id = submit(store)["id"]
    queued = tick_due(store, clock, cloud, run_id)
    assert queued["state"] == "waiting" and queued["next_check"] - clock.time >= 60
    assert cloud.creates == []

    cloud.stock = True
    cloud.create_error = CloudError("226604", retryable=True)
    rejected = tick_due(store, clock, cloud, run_id)
    assert rejected["state"] == "waiting"
    assert [item["state"] for item in rejected["operations"]] == ["rejected"]

    cloud.create_error = None
    pending = tick_due(store, clock, cloud, run_id)
    assert pending["state"] == "reconciling"
    assert [item["state"] for item in pending["operations"]] == ["rejected", "accepted"]
    cloud.items = [{"id": "created-1", "project_id": "p1", "state": "Running", "gpu_mode": "with_gpu", **SPEC}]
    success = tick_due(store, clock, cloud, run_id)
    assert success["state"] == "succeeded" and success["purchase_attempts"] == 2
    tick_due(store, clock, cloud, run_id)
    assert len(cloud.creates) == 2
    with storage.connect(store.path) as conn:
        assert [tuple(row) for row in conn.execute("SELECT event_id,channel FROM notifications")] == [(run_id + ":result", "site")]


def test_price_cap_and_exact_expiry_prevent_dispatch(runtime):
    store, clock, cloud = runtime
    cloud.stock = True
    cloud.price = Decimal("4.51")
    run = submit(store, window_seconds=60)
    waiting = tick_due(store, clock, cloud, run["id"])
    assert waiting["state"] == "waiting" and "超出预算" in waiting["reason"]
    assert waiting["next_check"] == run["deadline_at"]
    cloud.price = Decimal("4.00")
    assert tick_due(store, clock, cloud, run["id"])["state"] == "expired"
    assert cloud.creates == []


def test_uncertain_send_remains_reserved_and_stopping_never_resends(runtime):
    store, clock, cloud = runtime
    cloud.stock = True
    cloud.create_error = CloudError("transport_error", uncertain=True, retryable=True)
    run = submit(store)
    waiting = tick_due(store, clock, cloud, run["id"])
    assert waiting["state"] == "reconciling" and waiting["operations"][0]["state"] == "reserved"
    assert store.cancel_action(run["id"])["stopped"]
    clock.time = run["deadline_at"] + 1
    TaskRunner(store, cloud).tick()
    assert store.get_run(run["id"])["state"] == "reconciling" and len(cloud.creates) == 1
    clock.time = waiting["result"]["creation_readback_deadline_at"]
    TaskRunner(store, cloud).tick()
    paused = store.get_run(run["id"])
    assert paused["state"] == "manual_review" and paused["result"]["reviewable"]
    assert paused["operations"][0]["state"] == "reserved" and len(cloud.creates) == 1

    assert store.queue_instance_recheck(run["id"])["result"]["instance_recheck_pending"]
    rechecked = tick_due(store, clock, cloud, run["id"])
    assert rechecked["state"] == "manual_review" and rechecked["result"]["instance_recheck_pending"] is False
    assert rechecked["operations"][0]["state"] == "reserved" and len(cloud.creates) == 1
    assert rechecked["audit"][-1]["action"] == "instance_recheck_result"

    cloud.items = [SPEC | {"id": "created-1", "project_id": "p1", "state": "Running",
                           "gpu_mode": "with_gpu", "disks": [SYSTEM_DISK], "remark": run["id"]}]
    store.queue_instance_recheck(run["id"])
    success = tick_due(store, clock, cloud, run["id"])
    assert success["state"] == "succeeded" and success["result"]["instance_recheck_pending"] is False
    assert len(cloud.creates) == 1


def test_gpu_success_reports_mount_unknown_after_bounded_readback(runtime):
    store, clock, cloud = runtime
    with storage.connect(store.path) as conn:
        conn.execute("INSERT INTO snapshots(kind,scope,data,success_at) VALUES('images',?,?,?)", (
            'system:["p1","cn-wlcb","cn-wlcb-01"]', json.dumps([{"id": "image-1", "runtime": "container"}]), storage.now(),
        ))
    cloud.stock = True
    run_id = submit(store, spec=SPEC | {"shared_storage": {"mode": "us3"}})["id"]
    pending = tick_due(store, clock, cloud, run_id)
    assert pending["state"] == "reconciling"
    cloud.items = [SPEC | {
        "id": "created-1", "project_id": "p1", "state": "Running", "gpu_mode": "with_gpu", "disks": [SYSTEM_DISK],
    }]
    pending = tick_due(store, clock, cloud, run_id)
    assert pending["state"] == "reconciling" and pending["result"]["storage_state"] == "attachment_pending"
    clock.time = pending["result"]["creation_readback_deadline_at"]
    TaskRunner(store, cloud).tick()
    success = store.get_run(run_id)
    assert success["state"] == "succeeded" and success["result"]["storage_state"] == "attachment_unverified"
    assert "挂载尚未核实" in success["reason"] and len(cloud.creates) == 1
    with storage.connect(store.path) as conn:
        notice = conn.execute("SELECT payload FROM notifications WHERE event_id=?", (run_id + ":result",)).fetchone()
        assert "挂载尚未核实" in json.loads(notice["payload"])["message"]


def test_second_preview_for_same_active_spec_cannot_create_another_run(runtime):
    store, _, _ = runtime
    submit(store)
    preview = store.preview_purchase_retry(PREVIEW | {"max_price": "5.00"})
    with pytest.raises(InstanceActionUnavailable, match="已有创建申请"):
        store.submit_purchase_retry(preview["preview_id"], "second-request")


def test_stock_from_template_uses_each_zone_resolved_disks(runtime, monkeypatch):
    store, clock, cloud = runtime
    other_disk = SYSTEM_DISK | {"size_gb": 50}
    zones = [
        {"region": SPEC["region"], "zone": SPEC["zone"], "resolved_disks": [SYSTEM_DISK]},
        {"region": "r2", "zone": "z2", "resolved_disks": [other_disk], "shared_storage": {"mode": "us3"}},
    ]
    with storage.connect(store.path) as conn:
        conn.execute("INSERT INTO templates VALUES ('template','p1','template',?,1)", (
            json.dumps(SPEC | {"zones": zones, "disks": [SYSTEM_DISK]}),
        ))
    calls = []

    def capacity(spec):
        calls.append(spec)
        return False

    monkeypatch.setattr(cloud, "capacity", capacity)
    task = store.create_task({
        "project_id": "p1", "kind": "stock", "name": "stock", "schedule": {"type": "once", "at": clock.time},
        "template_id": "template", "config": {"cpu_platform": SPEC["cpu_platform"]},
    })
    TaskRunner(store, cloud).tick()
    assert store.list_runs(task_id=task["id"])[0]["state"] == "waiting"
    assert [(item["zone"], item["disks"]) for item in calls] == [(SPEC["zone"], [SYSTEM_DISK]), ("z2", [other_disk])]
    assert all("shared_storage" not in item for item in calls)


def test_preview_expiry_and_single_attempt_mode(runtime):
    store, clock, cloud = runtime
    preview = store.preview_purchase_retry(PREVIEW)
    clock.time = preview["expires_at"]
    with pytest.raises(InstanceActionUnavailable, match="过期"):
        store.submit_purchase_retry(preview["preview_id"], "expired")

    cloud.stock = True
    cloud.create_error = CloudError("226604", retryable=True)
    run = submit(store, retry=False, window_seconds=None)
    assert run["deadline_at"] - run["scheduled_at"] == 300
    assert tick_due(store, clock, cloud, run["id"])["state"] == "failed"
    tick_due(store, clock, cloud, run["id"])
    assert len(cloud.creates) == 1


def test_stock_only_task_reuses_exact_capacity_without_live_probe(runtime, monkeypatch):
    from capacity_fixtures import record_capacity
    from gpu_console.storage import availability

    store, clock, cloud = runtime
    fields = ("gpu", "cpu", "memory_gb")
    spec = {key: SPEC[key] for key in (*fields, "gpu_model", "charge_type")}
    location = {key: SPEC[key] for key in ("region", "zone")}
    with storage.connect(store.path) as conn:
        row = location | {"gpu_model": SPEC["gpu_model"], "instance_type": "uhost",
                          "specs": [{key: SPEC[key] for key in fields}]}
        conn.execute("UPDATE snapshots SET data=? WHERE kind='catalog'", (json.dumps([row]),))
        availability.record_pool(conn, location["region"], clock.time, provider_updated_at=clock.time,
                                 counts={(location["zone"], SPEC["gpu_model"], "exclusive"): 1})
    task = store.create_task({"project_id": "p1", "kind": "stock", "name": "目标池余量",
                              "schedule": {"type": "once", "at": clock.time},
                              "config": spec | {"zones": [location]}})

    def unexpected_capacity(_spec):
        pytest.fail("库存通知应复用已记录的规格检查")

    monkeypatch.setattr(cloud, "capacity", unexpected_capacity)
    TaskRunner(store, cloud).tick()
    [run] = store.list_runs(task_id=task["id"])
    assert run["state"] == "waiting"
    with storage.connect(store.path) as conn:
        conn.execute("UPDATE task_runs SET next_check=? WHERE id=?", (clock.time, run["id"]))
        record_capacity(conn, "p1", (location["region"], location["zone"], SPEC["gpu_model"], "uhost"),
                        {tuple(SPEC[key] for key in fields): True}, clock.time)
    TaskRunner(store, cloud).tick()
    [run] = store.list_runs(task_id=task["id"])
    assert run["state"] == "succeeded"
    assert run["result"]["remaining_cards"] == run["result"]["required_cards"] == 1
