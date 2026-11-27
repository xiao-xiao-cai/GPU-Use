import json
from datetime import datetime

import pytest
from pwdlib import PasswordHash

from gpu_console import storage
from gpu_console.application import automation_rules
from gpu_console.application.tasks import TaskStore
from gpu_console.storage import cardless
from gpu_console.worker.collection import Collector
from gpu_console.worker.tasks import TaskRunner
from test_api import console  # noqa: F401 - pytest fixture
from test_collection import PROJECT, Cloud, instance

INSTANCE = "uhost-test"
CPU, GPU, VRAM = automation_rules.METRICS


def beijing(value):
    return int(datetime.fromisoformat(value + "+08:00").timestamp())


def points(at, *, cpu=0, gpu=0, vram=0, stagger=False, omit_vram=False):
    values = [
        (CPU, "", at - 5 * stagger, cpu),
        (GPU, "GPU-1", at - 10 * stagger, 0),
        (GPU, "GPU-2", at - 10 * stagger, gpu),
        (VRAM, "GPU-1", at, 0),
    ]
    if not omit_vram:
        values.append((VRAM, "GPU-2", at, vram))
    return [dict(project_id=PROJECT, instance_id=INSTANCE, metric=metric, label=label, sampled_at=sampled_at, value=value)
            for metric, label, sampled_at, value in values]


class AutomationCloud(Cloud):
    def __init__(self, started_at):
        super().__init__()
        self.rows[0]["start_time"] = started_at
        self.writes = []

    def instances(self, project_id, _zones=None):
        return super().instances(project_id)

    def stop(self, target):
        self.writes.append(target["id"])
        self.rows[0]["state"] = "Stopped"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    now = [beijing("2026-11-22T23:57:00")]
    monkeypatch.setattr(storage, "now", lambda: now[0])
    path = tmp_path / "console.sqlite3"
    storage.initialize(path)
    cloud = AutomationCloud(now[0] - 300)
    collector = Collector(path, cloud)
    store = TaskStore(path, clock=lambda: now[0])
    return now, cloud, collector, store, TaskRunner(store, cloud)


def save(store, target, now, kind, *, duration=2, schedule=None):
    config = dict(duration_minutes=duration, cpu_threshold=10, gpu_threshold=10, vram_threshold=10)
    if schedule:
        config["schedule"] = schedule
    with storage.connect(store.path) as conn, storage.transaction(conn):
        return automation_rules.save_rule(conn, dict(project_id=PROJECT, instance_id=INSTANCE, name="训练规则",
                                                      kind=kind, config=config, channels=["site", "feishu"]), target, now)


def tick(harness, delta=60, **kwargs):
    now, cloud, collector, _, _ = harness
    now[0] += delta
    cloud.points = points(now[0], **kwargs)
    collector.tick()


def test_overnight_idle_rule_requires_whole_in_window_complete_staggered_samples(harness):
    now, cloud, _, store, runner = harness
    rule = save(store, cloud.rows[0], now[0], "idle_stop", schedule={
        "mode": "weekly", "windows": [{"weekday": 7, "start": "23:58", "end": "01:00"}],
    })
    tick(harness, 0, stagger=True)
    tick(harness, stagger=True)
    tick(harness, stagger=True, omit_vram=True)
    for _ in range(3):
        tick(harness, stagger=True)
    assert not store.list_runs(PROJECT, automation_rule_id=rule["id"])
    tick(harness, stagger=True)
    [run] = store.list_runs(PROJECT, automation_rule_id=rule["id"])
    assert run["snapshot"]["trigger"]["since_at"] == beijing("2026-11-23T00:00:00")
    assert run["deadline_at"] == beijing("2026-11-23T01:00:00") - 1
    tick(harness, stagger=True)
    tick(harness, stagger=True)
    tick(harness, stagger=True)
    assert now[0] - run["snapshot"]["trigger"]["sampled_at"] > automation_rules.SAMPLE_FRESHNESS
    runner.tick()
    now[0] += 15
    runner.tick()
    [run] = store.list_runs(PROJECT, automation_rule_id=rule["id"])
    assert run["state"] == "succeeded"
    assert cloud.writes == [INSTANCE]


@pytest.mark.parametrize("change", ["none", "cpu_busy", "missing", "wrong_mode", "new_boot", "unverified_profile"])
def test_cardless_cpu_rule_rechecks_mode_boot_and_cpu_before_stop(harness, change):
    now, cloud, collector, store, runner = harness
    vm = cloud.rows[0]
    vm.update(gpu_mode="without_gpu", gpu=0, charge_type="Postpay", support_without_gpu_start=True, cpu=8, memory_gb=16)
    with storage.connect(store.path) as conn:
        cardless.record_profile(conn, {"region": vm["region"], "zone": vm["zone"], "spec": "B", "cpu": 8, "memory_gb": 16})
    schedule = {"mode": "once", "start_at": now[0], "end_at": now[0] + 900}
    gpu_rule = save(store, vm, now[0], "idle_stop", schedule=schedule)
    cpu_rule = save(store, vm, now[0], "cardless_idle_stop", schedule=schedule)
    for delta in (0, 60, 60):
        now[0] += delta
        cloud.points = points(now[0])[:1]
        collector.tick()
    assert not store.list_runs(PROJECT, automation_rule_id=gpu_rule["id"])
    [queued] = store.list_runs(PROJECT, automation_rule_id=cpu_rule["id"])
    assert queued["state"] == "queued"
    if change == "cpu_busy":
        cloud.points = points(now[0], cpu=30)[:1]
    elif change == "missing":
        cloud.points = []
    elif change == "wrong_mode":
        vm.update(gpu_mode=None, gpu=2)
    elif change == "new_boot":
        vm["start_time"] = now[0]
    elif change == "unverified_profile":
        vm["cpu"] = 2
    runner.tick()
    assert cloud.writes == ([INSTANCE] if change == "none" else [])
    assert store.get_run(queued["id"])["state"] == ("reconciling" if change == "none" else "canceled")


def test_thirty_minute_idle_rule_fulfills_the_later_cloud_appointment(harness, monkeypatch):
    now, cloud, _, store, runner = harness
    vm = cloud.rows[0]
    vm.update(charge_type="Postpay", scheduler_status="known", scheduler_stop_time=None)
    target = now[0] + 3600
    monkeypatch.setattr(cloud, "set_stop", lambda _vm, at: vm.update(scheduler_stop_time=at), raising=False)
    monkeypatch.setattr(cloud, "cancel_stop", lambda _vm: vm.update(scheduler_stop_time=None), raising=False)
    appointment = store.submit_action({
        "kind": "shutdown", "project_id": PROJECT,
        "config": {"instance_id": INSTANCE, "region": vm["region"], "zone": vm["zone"], "shutdown_at": target},
    }, "idle-fallback")
    runner.tick()
    now[0] += 16
    runner.tick()
    assert store.get_run(appointment["id"])["result"]["shutdown_registration"] == "managed"
    rule = save(store, vm, now[0], "idle_stop", duration=30, schedule={
        "mode": "once", "start_at": now[0], "end_at": target + 60,
    })
    for delta in [0] + [60] * 30:
        tick(harness, delta)
    runner.tick()
    assert vm["state"] == "Stopped"
    vm["stop_time"] = now[0]
    now[0] += 16
    runner.tick()
    runner.tick()
    now[0] += 16
    runner.tick()
    [stop] = store.list_runs(PROJECT, automation_rule_id=rule["id"])
    result = store.get_run(appointment["id"])["result"]
    assert result["shutdown_outcome"] == "early"
    assert result["shutdown_cause"] == "idle_stop" and result["stop_run_id"] == stop["id"]
    assert not result["shutdown_cleanup_pending"] and vm["scheduler_stop_time"] is None
    assert cloud.writes == [INSTANCE]


def test_idle_pause_cancels_queued_stop_and_resume_requires_new_evidence(harness):
    now, cloud, _, store, runner = harness
    rule = save(store, cloud.rows[0], now[0], "idle_stop", schedule={
        "mode": "once", "start_at": now[0], "end_at": now[0] + 600,
    })
    for delta in (0, 60, 60):
        tick(harness, delta)
    with storage.connect(store.path) as conn, storage.transaction(conn):
        assert automation_rules.control_rule(conn, rule["id"], "pause", now[0])["status"] == "paused"
    runner.tick()
    with storage.connect(store.path) as conn, storage.transaction(conn):
        automation_rules.control_rule(conn, rule["id"], "resume", now[0])
    tick(harness)
    [run] = store.list_runs(PROJECT, automation_rule_id=rule["id"])
    assert run["state"] == "canceled" and not cloud.writes
    tick(harness)
    tick(harness)
    assert sorted(run["state"] for run in store.list_runs(PROJECT, automation_rule_id=rule["id"])) == ["canceled", "queued"]


def test_idle_recapture_clears_evidence_after_load_rebounds(harness):
    now, cloud, _, store, runner = harness
    rule = save(store, cloud.rows[0], now[0], "idle_stop", schedule={
        "mode": "once", "start_at": now[0], "end_at": now[0] + 600,
    })
    for delta in (0, 60, 60):
        tick(harness, delta)
    cloud.points = points(now[0], vram=10)
    runner.tick()
    [run] = store.list_runs(PROJECT, automation_rule_id=rule["id"])
    assert run["state"] == "canceled" and not cloud.writes
    tick(harness)
    tick(harness)
    assert len(store.list_runs(PROJECT, automation_rule_id=rule["id"])) == 1
    tick(harness)
    assert sorted(run["state"] for run in store.list_runs(PROJECT, automation_rule_id=rule["id"])) == ["canceled", "queued"]


def test_idle_sampling_lag_can_recover_from_one_missing_poll(harness):
    now, cloud, collector, store, _ = harness
    rule = save(store, cloud.rows[0], now[0], "idle_stop", schedule={
        "mode": "once", "start_at": now[0], "end_at": now[0] + 600,
    })
    tick(harness, 0)
    now[0] += 60
    cloud.points = []
    collector.tick()
    for _ in range(4):
        now[0] += 60
        cloud.points = points(now[0] - 90)
        collector.tick()
    [run] = store.list_runs(PROJECT, automation_rule_id=rule["id"])
    assert run["state"] == "queued"


def test_runtime_requires_five_full_minutes_and_never_retries_decision_in_same_boot(harness):
    now, cloud, _, store, runner = harness
    rule = save(store, cloud.rows[0], now[0], "runtime_stop", duration=6)
    tick(harness, 0)
    tick(harness)
    [run] = store.list_runs(PROJECT, automation_rule_id=rule["id"])
    assert run["state"] == "missed" and run["result"]["automation_reminded"]
    for _ in range(7):
        tick(harness)
        runner.tick()
    assert len(store.list_runs(PROJECT, automation_rule_id=rule["id"])) == 1
    assert cloud.writes == []
    with storage.connect(store.path) as conn:
        assert [row["channel"] for row in conn.execute("SELECT channel FROM notifications ORDER BY id")] == ["site", "feishu"]


def test_runtime_recapture_rejects_late_busy_sample_and_notifies_once(harness):
    now, cloud, _, store, runner = harness
    cloud.rows[0]["start_time"] = now[0]
    rule = save(store, cloud.rows[0], now[0], "runtime_stop", duration=6)
    tick(harness, 0)
    for _ in range(6):
        tick(harness)
    [run] = store.list_runs(PROJECT, automation_rule_id=rule["id"])
    assert run["state"] == "queued"
    cloud.points += points(now[0] - 20, cpu=80)
    runner.tick()
    [run] = store.list_runs(PROJECT, automation_rule_id=rule["id"])
    assert run["state"] == "missed" and run["result"]["automation_reminded"]
    assert "负载达到阈值" in run["reason"] and not cloud.writes
    tick(harness)
    runner.tick()
    with storage.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM notifications WHERE event_id=?", (run["id"] + ":result",)).fetchone()[0] == 2


def test_runtime_does_not_replay_cutoff_from_restored_data(harness):
    now, cloud, _, store, runner = harness
    rule = save(store, cloud.rows[0], now[0], "runtime_stop", duration=6)
    with storage.connect(store.path) as conn, storage.transaction(conn):
        storage.set_settings(conn, {"restore_gate": True})
    tick(harness)
    with storage.connect(store.path) as conn, storage.transaction(conn):
        automation_rules.restore_boundary(conn, now[0])
        storage.set_settings(conn, {"restore_gate": False})
    tick(harness)
    runner.tick()
    assert not store.list_runs(PROJECT, automation_rule_id=rule["id"])
    assert not cloud.writes


def test_rule_api_targets_current_instance_and_routes_history(console, monkeypatch):
    client, config = console
    now = beijing("2026-11-22T23:57:00")
    monkeypatch.setattr(storage, "now", lambda: now)
    login = {"username": "operator", "password": "stub-password-12345"}
    target = instance()
    target["start_time"] = now - 500
    with storage.connect(config.db) as conn:
        conn.execute("INSERT INTO administrators VALUES (1,?,?)", (login["username"], PasswordHash.recommended().hash(login["password"])))
        conn.execute("INSERT INTO snapshots VALUES ('instances',?,?,?,NULL)", (PROJECT, json.dumps([target]), now))
    assert client.get("/api/automation-rules").status_code == 401
    headers = {"x-csrf-token": client.post("/api/login", json=login).json()["csrf_token"]}
    body = dict(project_id=PROJECT, instance_id=INSTANCE, name="上限", kind="runtime_stop",
                config={"duration_minutes": 5}, channels=["feishu"])
    response = client.post("/api/automation-rules", json=body, headers=headers)
    assert response.status_code == 201, response.json()
    rule = response.json()
    assert rule["config"]["cpu_threshold"] == rule["config"]["gpu_threshold"] == rule["config"]["vram_threshold"] == 10
    assert rule["channels"] == ["site", "feishu"] and rule["warning"]
    assert client.get("/api/automation-rules", params={"project_id": "other"}, headers=headers).json() == []
    assert client.get("/api/runs", params={"automation_rule_id": rule["id"], "project_id": PROJECT}, headers=headers).json() == []
    path = "/api/automation-rules/" + rule["id"]
    assert client.post(path + "/pause", headers=headers).json()["status"] == "paused"
    assert client.post(path + "/resume", headers=headers).json()["status"] == "active"
    invalid = body | {"channels": ["email"]}
    assert client.put(path, json=invalid, headers=headers).status_code == 422
    assert client.put(path, json=body | {"name": "更新后的上限"}, headers=headers).json()["version"] == 4

    stopped = target | {"state": "Stopped", "gpu": 0}
    with storage.connect(config.db) as conn:
        conn.execute("UPDATE snapshots SET data=? WHERE kind='instances' AND scope=?", (json.dumps([stopped]), PROJECT))
    response = client.post("/api/automation-rules", json=body | {"name": "下次开机的上限"}, headers=headers)
    assert response.status_code == 201 and response.json()["warning"] is None
