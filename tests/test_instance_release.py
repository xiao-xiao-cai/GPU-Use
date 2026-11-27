import json

import pytest
from fastapi.testclient import TestClient

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.application import instance_release
from gpu_console.application.tasks import InstanceActionUnavailable, TaskStore, insert_run
from gpu_console.config import Config
from gpu_console.mcp import auth, instances, server
from gpu_console.mcp.server import create_mcp_app
from gpu_console.worker.tasks import INSTANCE_READBACK_SECONDS, TaskRunner

NOW = 1_800_000_000
PROJECT = "project-a"
INSTANCE = "uhost-test"
SCOPE = {"region": "cn-test", "zone": "cn-test-01"}
DEFAULT_TARGET = {"id": INSTANCE, "name": "测试实例", "project_id": PROJECT, **SCOPE, "instance_type": "UHost",
                  "charge_type": "Postpay", "state": "Stopped", "create_time": NOW - 300, "stop_time": NOW - 10}


class ReleaseCloud:
    last_request_id = "request-test"

    def __init__(self):
        self.instance = DEFAULT_TARGET.copy()
        self.read_error = None
        self.write_error = None
        self.writes = []

    def instances(self, project_id, zones):
        assert project_id == PROJECT and zones == [SCOPE]
        if self.read_error:
            raise self.read_error
        return [self.instance.copy()] if self.instance else []

    def terminate(self, instance):
        self.writes.append(instance["id"])
        if self.write_error:
            raise self.write_error
        return instance["id"]


@pytest.fixture
def runtime(tmp_path):
    path = tmp_path / "state.db"
    clock = [NOW]
    storage.initialize(path)
    cloud = ReleaseCloud()
    store = TaskStore(path, clock=lambda: clock[0])
    return store, cloud, clock, TaskRunner(store, cloud)


def save_snapshot(conn, instance, now=NOW):
    conn.execute("INSERT OR REPLACE INTO snapshots VALUES ('instances',?,?,?,NULL)",
                 (PROJECT, json.dumps([instance]), now))


def enqueue(store, target=DEFAULT_TARGET, *, charge_types=instance_release.DEFAULT_CHARGE_TYPES):
    with storage.connect(store.path) as conn, storage.transaction(conn):
        save_snapshot(conn, target)
        return instance_release.enqueue(conn, instance_release.identity(target), NOW + 300,
                                        "action:test-release", NOW, charge_types)


@pytest.mark.parametrize("charge_type", ["Postpay", "Day", "Month", "Spot"])
def test_instance_release_gates_supported_config_and_running_type(charge_type):
    target = DEFAULT_TARGET | {"charge_type": charge_type}
    if charge_type != "Postpay":
        with pytest.raises(ValueError, match="计费类型"):
            instance_release.check_target(target, instance_release.DEFAULT_CHARGE_TYPES)
    instance_release.check_target(target, {charge_type})
    for conflict in ({"state": "Running"}, {"instance_type": "Container"}, {"instance_type": None}):
        with pytest.raises(ValueError, match="已关机的普通虚机"):
            instance_release.check_target(target | conflict, {charge_type})


def test_release_requires_acknowledgement_and_complete_scoped_inventory(runtime):
    store, cloud, clock, runner = runtime
    run_id = enqueue(store)["id"]
    runner.tick()
    run = store.get_run(run_id)
    assert run["state"] == "reconciling" and run["phase"] == "verify_terminate"
    assert run["operations"][0]["data"]["acknowledged_instance_id"] == INSTANCE

    # Filtered or incomplete inventory cannot establish that the instance is gone.
    cloud.instance = None
    cloud.read_error = CloudError("incomplete_inventory", retryable=True)
    clock[0] += 60
    runner.tick()
    assert store.get_run(run_id)["state"] == "reconciling"
    cloud.read_error = None
    clock[0] += 60
    runner.tick()
    result = store.get_run(run_id)
    assert result["state"] == "succeeded" and result["result"]["disposition"] == "released"
    assert cloud.writes == [INSTANCE]


def test_confirmed_release_retires_only_the_matching_appointment_observations(runtime):
    store, cloud, clock, runner = runtime
    timer_at = NOW + 600
    specification = {
        "kind": "shutdown", "name": "预约关机", "project_id": PROJECT,
        "config": {"instance_id": INSTANCE, "shutdown_at": timer_at, **SCOPE},
        "channels": ["site", "feishu"], "window_seconds": 1200,
    }
    with storage.connect(store.path) as conn, storage.transaction(conn):
        local = [insert_run(conn, specification, timer_at, NOW, state="succeeded")["id"] for _ in range(2)]
        other = insert_run(conn, specification | {"project_id": "another-project"},
                           timer_at + 3600, NOW, state="succeeded")["id"]
        conn.executemany("INSERT INTO shutdown_owners VALUES (?,?,?,?,?,?,?)", [
            (PROJECT, INSTANCE, local[0], None, SCOPE["region"], SCOPE["zone"], timer_at),
            ("another-project", INSTANCE, other, None, SCOPE["region"], SCOPE["zone"], timer_at + 3600),
        ])
        conn.executemany("INSERT INTO external_shutdown_observations VALUES (?,?,?,?,?)", [
            (local[1], INSTANCE, SCOPE["region"], SCOPE["zone"], timer_at),
            (other, INSTANCE, SCOPE["region"], SCOPE["zone"], timer_at + 3600),
        ])

    release = enqueue(store)["id"]
    runner.tick()
    cloud.instance = None
    clock[0] += 30
    runner.tick()
    assert store.get_run(release)["state"] == "succeeded"
    clock[0] = timer_at + 300
    runner.tick()
    with storage.connect(store.path) as conn:
        assert [row[0] for row in conn.execute("SELECT run_id FROM shutdown_owners")] == [other]
        assert [row[0] for row in conn.execute("SELECT run_id FROM external_shutdown_observations")] == [other]
        assert conn.execute("SELECT COUNT(*) FROM notifications WHERE event_id GLOB '*:shutdown-outcome'").fetchone()[0] == 0


@pytest.mark.parametrize("code,uncertain,expected", [
    ("transport_error", True, "manual_review"),
    ("credential_invalid", False, "failed"),
    ("429", False, "failed"),
])
def test_uncertain_or_rejected_release_is_never_resent(runtime, code, uncertain, expected):
    store, cloud, clock, runner = runtime
    run_id = enqueue(store)["id"]
    cloud.write_error = CloudError(code, uncertain=uncertain, retryable=code != "credential_invalid")
    runner.tick()
    cloud.instance = None
    clock[0] += 60
    runner.tick()
    if uncertain:
        assert store.get_run(run_id)["state"] == "reconciling"
    clock[0] = NOW + INSTANCE_READBACK_SECONDS
    runner.tick()
    assert store.get_run(run_id)["state"] == expected
    assert cloud.writes == [INSTANCE]
    if uncertain:
        store.queue_instance_recheck(run_id)
        runner.tick()
        assert store.get_run(run_id)["state"] == "manual_review"
        assert cloud.writes == [INSTANCE]


def test_release_rechecks_current_instance(runtime):
    store, cloud, _, runner = runtime
    run_id = enqueue(store)["id"]
    cloud.instance["stop_time"] += 1
    runner.tick()
    assert store.get_run(run_id)["state"] == "failed"
    assert not cloud.writes


def test_worker_rechecks_release_billing_permission(runtime):
    store, cloud, _, runner = runtime
    cloud.instance["charge_type"] = "Spot"
    run_id = enqueue(store, cloud.instance, charge_types={"Spot"})["id"]
    runner.tick()
    assert store.get_run(run_id)["state"] == "failed"
    assert not cloud.writes


def test_release_lists_references_and_blocks_before_dispatch(runtime):
    store, cloud, _, runner = runtime
    task = store.create_task({
        "name": "启动", "kind": "start", "project_id": PROJECT,
        "schedule": {"type": "once", "at": NOW + 3600}, "window_seconds": 60,
        "config": {"instance_id": INSTANCE, **SCOPE}, "channels": ["site"],
    })
    with pytest.raises(ValueError, match="关联"):
        enqueue(store)
    store.control_task(task["id"], "pause")
    run_id = enqueue(store)["id"]
    with storage.connect(store.path) as conn, storage.transaction(conn):
        blocked = insert_run(conn, {"kind": "start", "name": "开机", "project_id": PROJECT,
                                   "config": {"instance_id": INSTANCE, **SCOPE}, "channels": ["site"],
                                   "window_seconds": 600}, NOW + 30, NOW)
        assert instance_release.blockers(conn, PROJECT, INSTANCE, exclude_run_id=run_id)["run_ids"] == [blocked["id"]]
    runner.tick(limit=1)
    assert store.get_run(run_id)["state"] == "failed"
    assert not cloud.writes


def test_pending_release_blocks_the_existing_immediate_action_entry_point(runtime):
    store, cloud, _, _ = runtime
    enqueue(store)
    with pytest.raises(InstanceActionUnavailable, match="释放"):
        store.submit_action({"kind": "start", "project_id": PROJECT, "config": {"instance_id": INSTANCE, **SCOPE}}, "start-conflict")
    assert not cloud.writes


@pytest.fixture
def mcp_instances(tmp_path, monkeypatch):
    config = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    monkeypatch.setattr(server, "register_tools", instances.register)
    with TestClient(create_mcp_app(config), base_url=f"http://{config.mcp_bind_ip}:{config.mcp_bind_port}") as client:
        now = storage.now()
        with storage.connect(config.db) as conn, storage.transaction(conn):
            token = auth.issue(conn, name="Test client", manage=True, ssh=True, days=None)["token"]
            save_snapshot(conn, DEFAULT_TARGET, now)
            conn.execute("INSERT INTO snapshots VALUES ('projects','',?,?,NULL)", (json.dumps([{"id": PROJECT}]), now))
            conn.execute("INSERT INTO snapshots VALUES ('catalog','',?,?,NULL)", (json.dumps([
                SCOPE | {"gpu_model": "4090", "instance_type": "uhost",
                         "disks": [{"purpose": "system", "type": "CLOUD_SSD", "default_size_gb": 100}]},
            ]), now))

        def call(name, args, *, error=False):
            response = client.post("/mcp", headers={
                "Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
            assert response.status_code == 200
            result = response.json()["result"]
            assert bool(result.get("isError")) is error, result
            return result.get("structuredContent")

        yield config, call


def test_mcp_release_consumes_project_bound_preview_and_replays_receipt(mcp_instances):
    config, call = mcp_instances
    target = {"project_id": PROJECT, "instance_id": INSTANCE}
    preview = call("gpu_prepare_instance_release", target)
    request = target | {"preview_id": preview["preview_id"], "request_id": "release-request"}
    call("gpu_release_instance", request | {"project_id": "other"}, error=True)
    receipt = call("gpu_release_instance", request)
    assert receipt["phase"] is None and receipt["state"] == "queued"
    assert call("gpu_release_instance", request) == receipt
    call("gpu_release_instance", request | {"request_id": "release-new-request"}, error=True)
    with storage.connect(config.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_operations").fetchone()[0] == 0


def test_mcp_create_requires_a_single_confirmed_project_quote(mcp_instances):
    config, call = mcp_instances
    spec = SCOPE | {"gpu_model": "4090", "gpu": 1, "cpu": 16, "memory_gb": 64, "cpu_platform": "AMD",
                    "image_id": "image-a", "charge_type": "Postpay",
                    "disks": [{"purpose": "system", "type": "CLOUD_SSD", "size_gb": 100}]}
    quote_request = {"project_id": PROJECT, "spec": spec, "request_id": "quote-request"}
    quote = call("gpu_request_instance_quote", quote_request)
    assert quote["state"] == "queued" and quote["spec"] == spec and quote["price_unit"] == "CNY/hour"
    assert call("gpu_request_instance_quote", quote_request) == quote

    with storage.connect(config.db) as conn, storage.transaction(conn):
        conn.execute("UPDATE instance_quotes SET state='ready',price='4.2',quoted_at=?,expires_at=? WHERE id=?",
                     (storage.now(), storage.now() + 300, quote["quote_id"]))
    quote_scope = {"project_id": PROJECT, "quote_id": quote["quote_id"]}
    assert call("gpu_get_instance_quote", quote_scope)["price"] == "4.2"
    call("gpu_get_instance_quote", quote_scope | {"project_id": "other"}, error=True)
    purchase = quote_scope | {"confirmed_price": "4.2", "request_id": "purchase-request"}
    call("gpu_create_instance", purchase | {"confirmed_price": "4.1"}, error=True)
    booked = call("gpu_create_instance", purchase)
    assert call("gpu_create_instance", purchase) == booked
    assert call("gpu_get_instance_quote", quote_scope)["state"] == "used"
    call("gpu_create_instance", purchase | {"request_id": "purchase-new-request"}, error=True)
    with storage.connect(config.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1


def test_mcp_queues_one_purchase_from_an_explicit_price_limit_without_a_quote(mcp_instances):
    config, call = mcp_instances
    draft = {
        "project_id": PROJECT, "max_price": "4.5", "window_seconds": 3600,
        "spec": SCOPE | {"gpu_model": "4090", "gpu": 1, "cpu": 16, "memory_gb": 64, "cpu_platform": "AMD",
                         "image_id": "image-a", "charge_type": "Postpay"},
    }
    preview = call("gpu_preview_instance_purchase_request", draft)
    assert preview["configuration"]["config"]["disks"] == [
        {"purpose": "system", "type": "CLOUD_SSD", "size_gb": 100},
    ]
    order = {"project_id": PROJECT, "preview_id": preview["preview_id"], "request_id": "retry-purchase"}
    call("gpu_submit_instance_purchase_request", order | {"project_id": "other"}, error=True)
    receipt = call("gpu_submit_instance_purchase_request", order)
    assert receipt["state"] == "queued"
    assert call("gpu_submit_instance_purchase_request", order) == receipt
    call("gpu_submit_instance_purchase_request", order | {"request_id": "use-preview-twice"}, error=True)
    run = TaskStore(config.db).get_run(receipt["run_id"])
    assert run["snapshot"]["purchase_retry"] is True and run["snapshot"]["config"]["max_price"] == "4.5"
    assert run["deadline_at"] - run["scheduled_at"] == 3600
