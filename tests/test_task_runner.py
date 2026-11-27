import json
from decimal import Decimal

import pytest

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.application.tasks import TaskStore, initialize, mark_restored
from gpu_console.storage import cardless
from gpu_console.worker.tasks import TaskRunner


NOW = 1_800_000_000
SYSTEM = {"purpose": "system", "type": "CLOUD_SSD", "size_gb": 80}
DATA = {"purpose": "data", "type": "CLOUD_SSD", "size_gb": 200}
ZONES = [{"region": "region1", "zone": "zone1"}, {"region": "region2", "zone": "zone2"}]
SPEC = {
    "gpu_model": "RTX4090", "gpu": 1, "cpu": 16, "memory_gb": 64, "cpu_platform": "AMD",
    "image_id": "image1", "disks": [SYSTEM, DATA], "charge_type": "Postpay",
}


def acquire(**config):
    return {
        "name": "实验", "kind": "acquire", "project_id": "p1", "schedule": {"type": "once", "at": NOW},
        "config": SPEC | {"zones": ZONES, "max_price": "5", **config}, "window_seconds": 20,
        "channels": ["site", "feishu"],
    }


def instance(iid="id1", state="Running", **fields):
    return SPEC | ZONES[1] | {
        "id": iid, "project_id": "p1", "state": state, "start_time": NOW,
        "gpu_mode": "with_gpu" if state == "Running" else None, "disks": [DATA, SYSTEM], **fields,
    }


class Clock:
    now = NOW

    def __call__(self):
        return self.now

    def advance(self, amount=16):
        self.now += amount


class FakeCloud:
    last_request_id = "request-fake"

    def __init__(self):
        self.instances_by_region = []
        self.writes = []
        self.reads = []
        self.stock = True
        self.quoted = Decimal("4")
        self.scan_error = None
        self.create_error = None
        self.start_error = None
        self.cancel_error = None
        self.reboot_error = None
        self.rename_error = None
        self.samples = []

    def instances(self, project_id, zones=None):
        self.reads.append(("inventory", project_id, zones))
        if zones and self.scan_error:
            raise self.scan_error
        return self.instances_by_region

    def capacity(self, spec):
        self.reads.append(("capacity", spec["zone"]))
        return self.stock

    def quote(self, spec):
        self.reads.append(("quote", spec))
        return self.quoted

    def create(self, spec, remark):
        self.writes.append(("create", spec, remark))
        if self.create_error:
            raise self.create_error
        return ["created1"]

    def start(self, instance):
        self.writes.append(("start", instance["id"]))
        if self.start_error:
            raise self.start_error

    def start_without_gpu(self, instance, spec):
        self.writes.append(("start_without_gpu", instance["id"], spec))
        if self.start_error:
            raise self.start_error

    def stop(self, instance):
        self.writes.append(("stop", instance["id"]))

    def reboot(self, instance):
        self.writes.append(("reboot", instance["id"]))
        if self.reboot_error:
            raise self.reboot_error

    def rename(self, instance, name):
        self.writes.append(("rename", instance["id"], name))
        if self.rename_error:
            raise self.rename_error

    def set_stop(self, instance, stop_at):
        self.writes.append(("set_shutdown", instance["id"], stop_at))

    def cancel_stop(self, instance):
        self.writes.append(("cancel_shutdown", instance["id"]))
        if self.cancel_error:
            raise self.cancel_error

    def monitor(self, *scope):
        return self.samples


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    storage.initialize(path)
    with storage.connect(path) as conn:
        initialize(conn)
        from gpu_console.application import purchase_options

        monkeypatch.setattr(purchase_options, "BLOCK_ZONES", {zone["zone"] for zone in ZONES})
        conn.execute("INSERT INTO snapshots VALUES ('projects','',?,?,NULL)",
                     (json.dumps([{"id": "p1"}]), storage.now()))
        conn.execute("INSERT INTO snapshots VALUES ('catalog','',?,?,NULL)", (json.dumps([
            zone | {"gpu_model": SPEC["gpu_model"], "instance_type": pool,
                    "specs": [{"gpu": gpu, "cpu": SPEC["cpu"], "memory_gb": SPEC["memory_gb"]} for gpu in (1, 2)], "disks": [
                {"purpose": "system", "type": "CLOUD_SSD", "default_size_gb": 80},
                {"purpose": "data", "type": "CLOUD_SSD", "min_size_gb": 20, "max_size_gb": 1000},
            ]} for zone in ZONES for pool in ("uhost", "spot")
        ]), storage.now()))
    clock = Clock()
    db = TaskStore(path, clock)
    cloud = FakeCloud()
    return db, cloud, clock, TaskRunner(db, cloud)


def enqueue(db, **config):
    task = db.create_task(acquire(**config))
    return task["id"]


def before_deadline(runtime, **config):
    db, cloud, clock, runner = runtime
    task_id = enqueue(db, **config)
    runner.tick()
    return next(run for run in db.list_runs(task_id=task_id) if run["kind"] == "acquire")


def test_reuses_running_match_in_second_zone_without_overwriting_shutdown(runtime):
    db, cloud, clock, runner = runtime
    cloud.instances_by_region = [
        instance("different", gpu_model="RTX5090"),
        instance("running", scheduler_stop_time=NOW + 1300),
    ]
    run = before_deadline(runtime, shutdown_at=NOW + 1000)
    assert run["state"] == "succeeded"
    assert run["result"]["instance_id"] == "running"
    assert run["result"]["disposition"] == "reused"
    assert run["result"]["shutdown_confirmation"] == "queued"
    assert not cloud.writes


@pytest.mark.parametrize(("requested", "actual"), [
    ("Amd/Auto", "Amd/Epyc2"), ("Intel/Auto", "Intel/Cascadelake"), ("Auto", None),
])
def test_cpu_auto_selector_reuses_a_matching_instance(runtime, requested, actual):
    _, cloud, _, _ = runtime
    cloud.instances_by_region = [instance(cpu_platform=actual)]
    run = before_deadline(runtime, cpu_platform=requested)
    assert run["state"] == "succeeded"
    assert run["result"]["disposition"] == "reused"
    assert not cloud.writes


def test_cpu_vendor_selector_excludes_other_vendor_and_blocks_uncertain_match(runtime):
    db, cloud, clock, runner = runtime
    cloud.instances_by_region = [instance(cpu_platform="Auto")]
    run = before_deadline(runtime, cpu_platform="Intel/Auto")
    assert run["state"] == "waiting"
    assert not cloud.writes
    cloud.instances_by_region = [instance(cpu_platform="Amd/Epyc2")]
    clock.advance()
    runner.tick()
    assert db.get_run(run["id"])["state"] == "reconciling"
    assert cloud.writes[0][0] == "create"


def test_incomplete_all_zone_scan_prevents_purchase(runtime):
    db, cloud, clock, runner = runtime
    cloud.scan_error = CloudError("incomplete_inventory", retryable=True)
    run = before_deadline(runtime)
    assert run["state"] == "waiting" and run["result"]["cloud_code"] == "incomplete_inventory"
    assert cloud.reads == [("inventory", "p1", ZONES)]
    assert not cloud.writes


def test_unknown_matching_spec_never_causes_purchase(runtime):
    _, cloud, _, _ = runtime
    cloud.instances_by_region = [instance(cpu_platform=None)]
    run = before_deadline(runtime)
    assert run["state"] == "waiting"
    assert not cloud.writes


def test_zero_reported_gpus_on_stopped_gpu_instance_does_not_allow_purchase(runtime):
    _, cloud, _, _ = runtime
    cloud.instances_by_region = [instance(state="Stopped", gpu=0)]
    run = before_deadline(runtime)
    assert run["state"] == "waiting"
    assert not cloud.writes


def test_pins_matching_stopped_instance_during_stock_shortage(runtime):
    db, cloud, clock, runner = runtime
    cloud.instances_by_region = [instance("stopped", "Stopped")]
    cloud.start_error = CloudError("226604", retryable=True)
    run = before_deadline(runtime)
    assert run["result"]["pinned_instance_id"] == "stopped"
    cloud.instances_by_region = [
        instance("stopped", "Stopped"), instance("another", "Stopped", **ZONES[0]),
    ]
    clock.advance()
    runner.tick()
    assert cloud.writes == [("start", "stopped"), ("start", "stopped")]


def scheduled_start(store, *, at=NOW, schedule_type="once", capacity_retry=True):
    config = ZONES[1] | {"instance_id": "id1"}
    if capacity_retry:
        config["capacity_retry"] = {"interval_seconds": 60}
    schedule = {"type": schedule_type, "at": at}
    if schedule_type == "interval":
        schedule["interval_seconds"] = 60
    return store.create_task({
        "name": "定时开机", "kind": "start", "project_id": "p1", "schedule": schedule,
        "config": config, "window_seconds": 20,
    })


def cardless_task(store, target):
    with storage.connect(store.path) as conn, storage.transaction(conn):
        conn.execute("INSERT INTO snapshots VALUES ('instances','p1',?,?,NULL)", (json.dumps([target]), NOW))
        cardless.record_profile(conn, ZONES[1] | {"spec": "B", "cpu": 8, "memory_gb": 16})
    return store.create_task({
        "name": "循环无卡开机", "kind": "start", "project_id": "p1",
        "schedule": {"type": "interval", "at": NOW, "interval_seconds": 60},
        "config": ZONES[1] | {"instance_id": "id1", "boot_mode": "without_gpu", "without_gpu_spec": "B",
                              "shutdown_after_seconds": 900},
    })


def test_cardless_cycle_binds_one_shutdown_to_actual_boot_and_never_adopts_the_next_cycle(runtime):
    store, cloud, clock, runner = runtime
    target = instance(state="Stopped", support_without_gpu_start=True, start_time=NOW - 1800,
                      scheduler_status="known", scheduler_stop_time=None)
    cloud.instances_by_region = [target]
    task = cardless_task(store, target)
    runner.tick()
    [first] = store.list_runs(task_id=task["id"])
    assert first["state"] == "reconciling" and cloud.writes == [("start_without_gpu", "id1", "B")]
    target.update(state="Running", gpu=0, gpu_mode="without_gpu", cpu=2, memory_gb=4, start_time=NOW + 3)
    runner = TaskRunner(store, cloud)
    clock.advance()
    runner.tick()
    assert store.get_run(first["id"])["state"] == "reconciling"
    target.update(cpu=8, memory_gb=16)
    clock.advance()
    runner.tick()
    finished = store.get_run(first["id"])
    child = store.get_run(finished["result"]["shutdown_run_id"])
    assert finished["state"] == "succeeded"
    assert child["scheduled_at"] == NOW + 903 and child["snapshot"]["config"]["instance_started_at"] == NOW + 3
    runner.tick()
    target["scheduler_stop_time"] = NOW + 903
    clock.advance(60)
    runner.tick()
    runner.tick()
    assert cloud.writes == [("start_without_gpu", "id1", "B"), ("set_shutdown", "id1", NOW + 903)]
    [repeat, original] = [run for run in store.list_runs(task_id=task["id"]) if run["kind"] == "start"]
    assert original["id"] == first["id"] and repeat["result"]["disposition"] == "reused"
    assert "shutdown_run_id" not in repeat["result"]


@pytest.mark.parametrize("current_mode", ["with_gpu", "wrong_cardless_tier", "unverified_tier"])
def test_cardless_mode_conflict_never_stops_or_starts_an_existing_run(runtime, current_mode):
    store, cloud, _, runner = runtime
    target = instance(support_without_gpu_start=True)
    if current_mode != "with_gpu":
        target.update(gpu=0, gpu_mode="without_gpu", cpu=2 if current_mode == "wrong_cardless_tier" else None, memory_gb=4)
    cloud.instances_by_region = [target]
    task = cardless_task(store, target)
    runner.tick()
    [run] = store.list_runs(task_id=task["id"])
    assert run["state"] == "missed" and "保留原运行和预约" in run["reason"]
    assert cloud.writes == []


@pytest.mark.parametrize("change", ["external_timer", "new_boot", "missing_boot"])
def test_cardless_cycle_never_overwrites_a_timer_or_follows_another_boot(runtime, change):
    store, cloud, clock, runner = runtime
    target = instance(state="Stopped", support_without_gpu_start=True, start_time=NOW - 1800,
                      scheduler_status="known", scheduler_stop_time=None)
    cloud.instances_by_region = [target]
    task = cardless_task(store, target)
    runner.tick()
    target.update(state="Running", gpu=0, gpu_mode="without_gpu", cpu=8, memory_gb=16, start_time=NOW + 1)
    clock.advance()
    runner.tick()
    first = next(run for run in store.list_runs(task_id=task["id"]) if run["kind"] == "start")
    child_id = first["result"]["shutdown_run_id"]
    if change == "external_timer":
        target["scheduler_stop_time"] = NOW + 600
    else:
        target["start_time"] = NOW + 10 if change == "new_boot" else None
    runner.tick()
    assert cloud.writes == [("start_without_gpu", "id1", "B")]
    assert store.get_run(child_id)["state"] == {"external_timer": "manual_review", "new_boot": "canceled",
                                                 "missing_boot": "waiting"}[change]


def test_unverified_cardless_profile_cannot_create_task_or_dispatch_queued_run(runtime):
    store, cloud, _, runner = runtime
    target = instance(state="Stopped", support_without_gpu_start=True)
    cloud.instances_by_region = [target]
    task = cardless_task(store, target)
    with storage.connect(store.path) as conn, storage.transaction(conn):
        conn.execute("DELETE FROM cardless_profiles")
    with pytest.raises(ValueError, match="未核实"):
        store.create_task(task)
    runner.tick()
    [run] = store.list_runs(task_id=task["id"])
    assert run["state"] == "failed" and cloud.writes == []


def test_uncertain_cardless_start_is_not_sent_again_or_given_a_shutdown_timer(runtime):
    store, cloud, clock, runner = runtime
    target = instance(state="Stopped", support_without_gpu_start=True, start_time=NOW - 1000)
    cloud.instances_by_region = [target]
    task = cardless_task(store, target)
    cloud.start_error = CloudError("110", retryable=True, uncertain=True)
    runner.tick()
    target.update(state="Running", gpu=0, gpu_mode="without_gpu", cpu=8, memory_gb=16, start_time=NOW + 1)
    clock.advance()
    runner.tick()
    [run] = store.list_runs(task_id=task["id"])
    assert run["result"]["disposition"] == "observed"
    assert not run["result"].get("shutdown_run_id") and run["result"]["shutdown_confirmation"] == "unconfirmed"
    assert cloud.writes == [("start_without_gpu", "id1", "B")]


def test_scheduled_capacity_retry_survives_restart_and_first_attempt_window(runtime):
    store, cloud, clock, runner = runtime
    target = instance(state="Stopped")
    cloud.instances_by_region = [target, instance("other", "Stopped")]
    cloud.start_error = CloudError("226604", retryable=True, request_id="no-resource")
    task = scheduled_start(store)
    runner.tick()
    run = store.list_runs(task_id=task["id"])[0]
    assert (run["state"], run["next_check"], run["deadline_at"]) == ("waiting", NOW + 60, NOW + 20)
    assert run["result"]["capacity_wait_since"] == NOW
    assert run["result"]["capacity_attempts"] == 1

    clock.advance(59)
    runner = TaskRunner(TaskStore(store.path, clock), cloud)
    runner.tick()
    assert cloud.writes == [("start", "id1")]
    clock.advance(1)
    runner.tick()
    run = store.get_run(run["id"])
    assert run["result"]["capacity_wait_since"] == NOW
    assert run["result"]["capacity_attempts"] == 2
    assert run["next_check"] == NOW + 120
    assert [(op["state"], op["cloud_code"]) for op in run["operations"]] == [("rejected", "226604")] * 2

    cloud.start_error = None
    clock.advance(60)
    runner.tick()
    assert store.get_run(run["id"])["state"] == "reconciling"
    target.update(state="Running", gpu_mode="with_gpu", start_time=clock())
    clock.advance()
    runner.tick()
    assert store.get_run(run["id"])["state"] == "succeeded"
    assert cloud.writes == [("start", "id1")] * 3


@pytest.mark.parametrize("capacity_retry", [False, True])
def test_long_capacity_run_preserves_total_and_caps_returned_operation_details(runtime, capacity_retry):
    store, _, _, _ = runtime
    task = scheduled_start(store, capacity_retry=capacity_retry)
    assert store.enqueue_due() == 1
    queued, = store.list_runs(task_id=task["id"])
    run_id = queued["id"]
    operation_states = [("start", "rejected")] * 120 + [
        ("start", "reserved"), ("start", "accepted"), ("start", "not_dispatched"), ("stop", "accepted"),
    ]
    with storage.connect(store.path) as conn, storage.transaction(conn):
        conn.executemany(
            """INSERT INTO task_operations(run_id,project_id,action,state,data,created_at,updated_at)
                 VALUES (?,'p1',?,?,'{}',?,?)""",
            [(run_id, action, state, NOW, NOW) for action, state in operation_states],
        )

    result = store.get_run(run_id)
    ids = list(range(1, len(operation_states) + 1))
    assert [op["id"] for op in result["operations"]] == (ids[-100:] if capacity_retry else ids)
    if capacity_retry:
        assert result["start_attempts"] == 122
    else:
        assert "start_attempts" not in result
    with storage.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM task_operations WHERE run_id=?", (run_id,)).fetchone()[0] == len(ids)


@pytest.mark.parametrize("capacity_retry", [False, True])
def test_only_explicit_confirmed_shortage_extends_start_window(runtime, capacity_retry):
    store, cloud, clock, runner = runtime
    cloud.instances_by_region = [instance(state="Stopped")]
    cloud.start_error = CloudError("226604" if not capacity_retry else "429", retryable=True)
    task = scheduled_start(store, capacity_retry=capacity_retry)
    runner.tick()
    clock.advance(61)
    runner.tick()
    run = store.list_runs(task_id=task["id"])[0]
    assert run["state"] == "expired"
    assert "capacity_wait_since" not in run["result"]
    assert cloud.writes == [("start", "id1")]


@pytest.mark.parametrize(("error", "state"), [
    (CloudError("226601"), "failed"),
    (CloudError("transport_error", uncertain=True, retryable=True), "manual_review"),
])
def test_capacity_retry_does_not_resend_uncertain_or_permanent_refusal(runtime, error, state):
    store, cloud, clock, runner = runtime
    cloud.instances_by_region = [instance(state="Stopped")]
    cloud.start_error = CloudError("226604", retryable=True)
    task = scheduled_start(store)
    runner.tick()
    clock.advance(60)
    cloud.start_error = error
    runner.tick()
    clock.advance(601)
    runner.tick()
    assert store.list_runs(task_id=task["id"])[0]["state"] == state
    assert cloud.writes == [("start", "id1")] * 2


@pytest.mark.parametrize("manual_stop", [False, True])
def test_duplicate_start_occurrences_skip_and_cancel_only_current_wait(runtime, manual_stop):
    store, cloud, clock, runner = runtime
    cloud.instances_by_region = [instance(state="Stopped")]
    cloud.start_error = CloudError("226604", retryable=True)
    first = scheduled_start(store, schedule_type="interval")
    second = scheduled_start(store, at=NOW + 60)
    runner.tick()
    waiting = store.list_runs(task_id=first["id"])[0]
    clock.advance(60)
    assert store.enqueue_due() == 0
    assert store.list_runs(task_id=second["id"])[0]["state"] == "missed"
    assert [run["state"] for run in store.list_runs(task_id=first["id"])] == ["missed", "waiting"]

    if manual_stop:
        immediate(store, "stop")
    else:
        store.cancel_action(waiting["id"])
    runner.tick()
    assert store.get_run(waiting["id"])["state"] == "canceled"
    assert cloud.writes == [("start", "id1")]
    assert store.get_task(first["id"])["next_at"] == NOW + 120
    clock.advance(60)
    runner.tick()
    assert cloud.writes == [("start", "id1")] * 2


def test_manual_stop_prevents_due_start_before_its_row_is_queued(runtime):
    store, cloud, clock, runner = runtime
    cloud.instances_by_region = [instance(state="Stopped")]
    task = scheduled_start(store, at=NOW + 60, schedule_type="interval")
    clock.advance(60)
    stop = immediate(store, "stop")
    runner.tick()
    skipped, = store.list_runs(task_id=task["id"])
    assert skipped["state"] == "missed" and "手动关机" in skipped["reason"]
    assert store.get_run(stop["id"])["state"] == "succeeded"
    assert cloud.writes == []

    explicit = store.trigger_task(task["id"], "run-now")
    assert explicit["state"] == "queued"
    store.cancel_action(explicit["id"])
    clock.advance(60)
    runner.tick()
    assert cloud.writes == [("start", "id1")]


def test_old_backup_restore_skips_waiting_capacity_run(runtime):
    store, cloud, clock, runner = runtime
    cloud.instances_by_region = [instance(state="Stopped")]
    cloud.start_error = CloudError("226604", retryable=True)
    task = scheduled_start(store)
    runner.tick()
    clock.advance(60)
    with storage.connect(store.path) as conn, storage.transaction(conn):
        mark_restored(conn, clock())
    runner.tick()
    assert store.list_runs(task_id=task["id"])[0]["state"] == "missed"
    assert cloud.writes == [("start", "id1")]


@pytest.mark.parametrize(("charge_type", "quantity"), [("Postpay", None), ("Spot", None), ("Day", 3), ("Month", 2)])
def test_four_products_honor_price_and_snapshot(runtime, charge_type, quantity):
    db, cloud, clock, runner = runtime
    cloud.instances_by_region = [instance()]
    run = before_deadline(runtime, skip_if_match=False, charge_type=charge_type, quantity=quantity)
    assert run["state"] == "reconciling"
    assert not any(read[0] == "inventory" for read in cloud.reads)
    spec = cloud.writes[0][1]
    assert spec["quantity"] == quantity if quantity else "quantity" not in spec
    assert spec["charge_type"] == charge_type
    assert run["result"]["quote"] == "4"
    assert cloud.reads == [("capacity", "zone1"), ("quote", spec), ("capacity", "zone1")]


def test_ambiguous_purchase_survives_window_and_worker_restart(runtime):
    db, cloud, clock, runner = runtime
    cloud.create_error = CloudError("503", uncertain=True, retryable=True)
    run = before_deadline(runtime)
    assert run["state"] == "reconciling"
    with pytest.raises(ValueError, match="等待运行"):
        db.reconcile_run(run["id"], "release")
    clock.advance(25)
    restarted = TaskRunner(db, cloud)
    restarted.tick()
    assert db.get_run(run["id"])["state"] == "reconciling"
    assert db.get_run(run["id"])["operations"][0]["state"] == "reserved"
    cloud.instances_by_region = [instance("created1", remark=run["id"], **ZONES[0])]
    clock.advance()
    restarted.tick()
    resolved = db.get_run(run["id"])
    assert resolved["state"] == "succeeded"
    assert resolved["operations"][0]["state"] == "accepted"
    assert len(cloud.writes) == 1
    with storage.connect(db.path) as conn:
        assert {row["channel"] for row in conn.execute("SELECT channel FROM notifications")} == {"site", "feishu"}


def test_name_recovers_missing_create_receipt_and_remark(runtime, monkeypatch):
    db, cloud, clock, _ = runtime
    cloud.create_error = CloudError("transport_error", uncertain=True, retryable=True)
    create = cloud.create

    def create_after_persisting_name(spec, remark):
        operation = db.get_run(remark)["operations"][0]
        assert operation["state"] == "reserved"
        assert operation["data"]["submitted_name"] == f"gpu-console-{remark}"
        return create(spec, remark)

    monkeypatch.setattr(cloud, "create", create_after_persisting_name)
    run = before_deadline(runtime)
    cloud.instances_by_region = [
        instance("created1", **ZONES[0], name=f"gpu-console-{run['id']}", remark="", create_time=NOW),
    ]
    clock.advance(25)
    TaskRunner(db, cloud).tick()
    resolved = db.get_run(run["id"])
    assert resolved["state"] == "succeeded"
    assert resolved["result"]["instance_id"] == "created1"
    assert resolved["operations"][0]["state"] == "accepted"
    assert ("inventory", "p1", None) in cloud.reads
    assert len(cloud.writes) == 1


@pytest.mark.parametrize("different", [
    {"project_id": "other"}, ZONES[1], {"cpu": 8}, {"disks": None}, {"remark": "other-run"},
    {"create_time": None}, {"create_time": NOW - 1}, {"create_time": NOW + 1000},
])
def test_name_without_matching_purchase_details_holds_reservation(runtime, different):
    db, cloud, clock, runner = runtime
    cloud.create_error = CloudError("transport_error", uncertain=True, retryable=True)
    run = before_deadline(runtime)
    cloud.instances_by_region = [
        instance("candidate", name=f"gpu-console-{run['id']}", **(ZONES[0] | {"create_time": NOW} | different)),
    ]
    clock.advance(25)
    runner.tick()
    result = db.get_run(run["id"])
    assert result["state"] == "reconciling" and result["result"]["reviewable"]
    assert result["operations"][0]["state"] == "reserved"
    assert len(cloud.writes) == 1


def test_name_and_remark_on_different_instances_require_manual_review(runtime):
    db, cloud, clock, runner = runtime
    cloud.create_error = CloudError("transport_error", uncertain=True, retryable=True)
    run = before_deadline(runtime)
    cloud.instances_by_region = [
        instance("by-name", **ZONES[0], name=f"gpu-console-{run['id']}", create_time=NOW),
        instance("by-remark", remark=run["id"]),
    ]
    clock.advance()
    runner.tick()
    result = db.get_run(run["id"])
    assert result["state"] == "manual_review"
    assert result["operations"][0]["state"] == "reserved"
    assert len(cloud.writes) == 1


def test_legacy_reservation_cannot_infer_an_unsent_name(runtime):
    db, cloud, clock, runner = runtime
    cloud.create_error = CloudError("transport_error", uncertain=True, retryable=True)
    run = before_deadline(runtime)
    with storage.connect(db.path) as conn:
        conn.execute(
            "UPDATE task_operations SET data=json_remove(data, '$.submitted_name') WHERE run_id=?", (run["id"],),
        )
    cloud.instances_by_region = [
        instance("candidate", **ZONES[0], name=f"gpu-console-{run['id']}", create_time=NOW),
    ]
    clock.advance()
    runner.tick()
    result = db.get_run(run["id"])
    assert result["state"] == "reconciling"
    assert result["operations"][0]["state"] == "reserved"
    assert len(cloud.writes) == 1


def test_double_matches_require_review_and_hold_other_run(runtime):
    db, cloud, clock, runner = runtime
    cloud.create_error = CloudError("transport_error", uncertain=True, retryable=True)
    run = before_deadline(runtime, skip_if_match=False)
    cloud.instances_by_region = [
        instance("one", remark=run["id"], **ZONES[0]), instance("two", remark=run["id"], **ZONES[0]),
    ]
    clock.advance()
    runner.tick()
    assert db.get_run(run["id"])["state"] == "manual_review"
    second = db.create_task(acquire(skip_if_match=False) | {"schedule": {"type": "once", "at": clock.now}})
    runner.tick()
    assert "未决采购" in db.list_runs(task_id=second["id"])[0]["reason"]
    db.reconcile_run(run["id"], "bind", "two", "订单核对完成")
    runner.tick()
    resolved = db.get_run(run["id"])
    assert resolved["state"] == "succeeded" and resolved["result"]["instance_id"] == "two"
    assert resolved["audit"][0]["action"] == "bind"
    assert len(cloud.writes) == 1


def test_restore_gate_has_no_writes_but_stock_runs(runtime):
    db, cloud, clock, runner = runtime
    with storage.connect(db.path) as conn:
        storage.set_settings(conn, {"restore_gate": True})
    task_id = enqueue(db)
    body = acquire() | {"kind": "stock"}
    stock = db.create_task(body)
    runner.tick()
    assert db.list_runs(task_id=task_id)[0]["state"] == "waiting"
    assert db.list_runs(task_id=stock["id"])[0]["state"] == "succeeded"
    assert not cloud.writes


def test_readiness_needs_unique_labels_of_latest_complete_sample(runtime):
    _, cloud, _, runner = runtime
    target = instance(gpu=2, gpu_mode=None)
    cloud.samples = [
        {"metric": "cloudwatch_gpu_util", "label": "GPU0", "sampled_at": NOW},
        {"metric": "cloudwatch_gpu_util", "label": "GPU0", "sampled_at": NOW},
        {"metric": "cloudwatch_gpu_util", "label": "GPU1", "sampled_at": NOW - 1},
    ]
    assert not runner._has_gpu(target)
    cloud.samples.append({"metric": "cloudwatch_gpu_util", "label": "GPU1", "sampled_at": NOW})
    assert runner._has_gpu(target)


def running_target(cloud, **fields):
    cloud.instances_by_region = [instance(scheduler_status="known", scheduler_stop_time=None) | fields]
    return cloud.instances_by_region[0]


def immediate(db, kind, key=None, **config):
    return db.submit_action(
        {"kind": kind, "project_id": "p1", "config": ZONES[1] | {"instance_id": "id1", **config}},
        key or kind,
    )


def test_early_stop_clears_only_the_original_timer_before_next_start(runtime):
    db, cloud, clock, runner = runtime
    target = NOW + 1200
    vm = running_target(cloud, charge_type="Postpay")
    appointment = immediate(db, "shutdown", shutdown_at=target)
    runner.tick()
    vm["scheduler_stop_time"] = target
    clock.advance()
    runner.tick()
    stop = immediate(db, "stop")
    runner.tick()
    vm.update(state="Stopped", stop_time=clock(), gpu_mode=None)
    clock.advance()
    runner.tick()
    result = db.get_run(appointment["id"])["result"]
    assert result["shutdown_outcome"] == "early" and result["shutdown_cleanup_pending"]
    assert (result["actual_stop_at"], result["shutdown_cause"], result["stop_run_id"]) == (
        vm["stop_time"], "manual", stop["id"],
    )

    runner = TaskRunner(db, cloud)
    start = immediate(db, "start")
    runner.tick()
    assert ("start", vm["id"]) not in cloud.writes
    assert db.get_run(start["id"])["reason"] == "旧预约清理待确认"
    assert cloud.writes.count(("cancel_shutdown", vm["id"])) == 1
    vm["scheduler_stop_time"] = None
    clock.advance()
    runner.tick()
    clock.advance()
    runner.tick()
    assert cloud.writes.count(("start", vm["id"])) == 1
    vm.update(state="Running", start_time=clock(), gpu_mode="with_gpu")
    clock.now = target + 60
    runner.tick()
    assert db.get_run(appointment["id"])["result"]["shutdown_outcome"] == "early"
    assert not db.get_run(appointment["id"])["result"]["shutdown_cleanup_pending"]
    with storage.connect(db.path) as conn:
        assert not conn.execute("SELECT 1 FROM notifications WHERE event_id=?",
                                (appointment["id"] + ":shutdown-outcome",)).fetchone()


def test_start_wait_expires_before_fallback_can_no_longer_be_registered(runtime):
    db, cloud, clock, runner = runtime
    running_target(cloud, state="Stopped", gpu_mode=None)
    cloud.start_error = CloudError("226604", retryable=True)
    task = db.create_task({
        "name": "截止前开机", "project_id": "p1", "kind": "start",
        "schedule": {"type": "once", "at": NOW}, "window_seconds": 20,
        "config": ZONES[1] | {"instance_id": "id1", "capacity_retry": {"interval_seconds": 60}, "shutdown_at": NOW + 900},
    })
    runner.tick()
    cloud.start_error = None
    clock.now = NOW + 601
    runner.tick()
    run = db.list_runs(task_id=task["id"])[0]
    assert run["state"] == "expired" and run["result"]["shutdown_cutoff_reached"]
    assert cloud.writes == [("start", "id1")]


def test_purchase_review_rejects_same_spec_instance_from_another_order(runtime):
    db, cloud, clock, runner = runtime
    cloud.create_error = CloudError("transport_error", uncertain=True)
    parent = before_deadline(runtime, skip_if_match=False, shutdown_at=NOW + 1200)
    running_target(cloud, id="old-instance", remark="different-order", create_time=NOW - 86400, **ZONES[0])
    clock.advance()
    runner.tick()
    db.reconcile_run(parent["id"], "bind", "old-instance", "规格相同的旧实例")
    runner.tick()
    run = db.get_run(parent["id"])
    assert run["state"] == "manual_review"
    assert run["operations"][-1]["state"] == "reserved"
    assert "shutdown_run_id" not in run["result"]


def test_explicitly_canceled_external_timer_is_removed_from_observation(runtime):
    db, cloud, clock, runner = runtime
    target = NOW + 1200
    vm = running_target(cloud, charge_type="Postpay", scheduler_stop_time=target)
    appointment = immediate(db, "shutdown", shutdown_at=target)
    runner.tick()
    cancel = immediate(db, "cancel_shutdown", expected_stop_at=target)
    runner.tick()
    vm["scheduler_stop_time"] = None
    clock.advance()
    runner.tick()
    clock.now = target + 300
    runner.tick()
    assert db.get_run(cancel["id"])["state"] == "succeeded"
    assert db.get_run(appointment["id"])["result"]["shutdown_outcome"] == "canceled"


def test_pause_keeps_linked_appointment_until_task_is_canceled(runtime):
    db, cloud, clock, runner = runtime
    parent = before_deadline(runtime, shutdown_at=NOW + 1200)
    vm = running_target(cloud, id="created1", remark=parent["id"])
    clock.advance()
    runner.tick()
    runner.tick()
    db.control_task(parent["task_id"], "pause")
    vm["scheduler_stop_time"] = NOW + 1200
    clock.advance()
    runner.tick()
    runner.tick()
    assert all(write[0] != "cancel_shutdown" for write in cloud.writes)
    db.control_task(parent["task_id"], "cancel")
    runner.tick()
    assert cloud.writes[-1] == ("cancel_shutdown", "created1")


def test_purchase_confirms_exactly_one_linked_shutdown(runtime):
    db, cloud, clock, runner = runtime
    run = before_deadline(runtime, shutdown_at=NOW + 1200)
    created = running_target(cloud, id="created1", remark="")
    clock.advance()
    runner.tick()
    verified = db.get_run(run["id"])
    link_id = verified["result"]["shutdown_run_id"]
    assert verified["state"] == "succeeded"
    assert verified["result"]["shutdown_confirmation"] == "queued"
    restarted = TaskRunner(db, cloud)
    restarted.tick()
    assert cloud.writes[-1] == ("set_shutdown", "created1", NOW + 1200)
    # Confirming only from the response would incorrectly report success before readback.
    assert db.get_run(link_id)["state"] == "reconciling"
    clock.advance()
    restarted.tick()
    assert db.get_run(run["id"])["result"]["shutdown_confirmation"] == "unconfirmed"
    created["scheduler_stop_time"] = NOW + 1200
    clock.advance()
    restarted.tick()
    assert db.get_run(link_id)["state"] == "succeeded"
    assert db.get_run(run["id"])["result"]["shutdown_confirmation"] == "confirmed"
    assert [item[0] for item in cloud.writes] == ["create", "set_shutdown"]


@pytest.mark.parametrize("appointment_replaced", [False, True])
def test_trashing_during_linked_shutdown_keeps_readback_and_checks_ownership(runtime, monkeypatch, appointment_replaced):
    db, cloud, clock, runner = runtime
    parent = before_deadline(runtime, shutdown_at=NOW + 1200)
    task_id = parent["task_id"]
    created = running_target(cloud, id="created1", remark="")
    clock.advance()
    runner.tick()
    assert db.get_task(task_id)["view"] == "current"
    link_id = db.get_run(parent["id"])["result"]["shutdown_run_id"]
    send = cloud.set_stop

    def trash_during_submission(instance, stop_at):
        send(instance, stop_at)
        db.change_trash("p1", task_id, 1, "trash")

    monkeypatch.setattr(cloud, "set_stop", trash_during_submission)
    runner.tick()
    assert db.get_run(link_id)["operations"][0]["state"] == "accepted"
    assert db.get_run(link_id)["stopped"]
    with pytest.raises(ValueError, match="待完成"):
        db.change_trash("p1", task_id, 2, "purge")

    created["scheduler_stop_time"] = NOW + 1200
    clock.advance()
    TaskRunner(db, cloud).tick(limit=1)
    cancellation = next(run for run in db.list_runs("p1", task_id) if run["kind"] == "cancel_shutdown")
    if appointment_replaced:
        created["scheduler_stop_time"] = NOW + 2400
        runner.tick()
        assert db.get_run(cancellation["id"])["state"] == "manual_review"
        assert all(write[0] != "cancel_shutdown" for write in cloud.writes)
        db.queue_shutdown_review(cancellation["id"])
    else:
        runner.tick()
        assert cloud.writes[-1] == ("cancel_shutdown", "created1")
        created["scheduler_stop_time"] = None
        clock.advance()
    runner.tick()
    assert db.get_run(cancellation["id"])["state"] == ("reviewed" if appointment_replaced else "succeeded")
    assert db.change_trash("p1", task_id, 2, "purge")["purged"]
    assert {run["id"] for run in db.list_runs("p1", task_id)} == {parent["id"], link_id, cancellation["id"]}


@pytest.mark.parametrize("linked_already_queued", [False, True])
def test_trash_cancels_unsubmitted_linked_shutdown(runtime, linked_already_queued):
    db, cloud, clock, runner = runtime
    parent = before_deadline(runtime, shutdown_at=NOW + 1200)
    running_target(cloud, id="created1", remark="")
    clock.advance()
    if linked_already_queued:
        runner.tick()
        assert db.get_run(parent["id"])["result"]["shutdown_confirmation"] == "queued"
    db.change_trash("p1", parent["task_id"], 1, "trash")
    runner.tick()
    result = db.get_run(parent["id"])["result"]
    assert result["shutdown_confirmation"] == "canceled"
    if linked_already_queued:
        assert db.get_run(result["shutdown_run_id"])["state"] == "canceled"
    assert [write[0] for write in cloud.writes] == ["create"]


def test_accepted_create_cannot_be_rebound_to_another_instance(runtime):
    db, cloud, clock, runner = runtime
    run = before_deadline(runtime, shutdown_at=NOW + 1200)
    clock.advance()
    runner.tick()
    assert db.get_run(run["id"])["result"]["reviewable"]

    with pytest.raises(ValueError, match="采购回执"):
        db.reconcile_run(run["id"], "bind", "preexisting")

    running_target(cloud, id="preexisting")
    clock.advance()
    runner.tick()
    assert db.get_run(run["id"])["state"] == "reconciling"
    assert [item[0] for item in cloud.writes] == ["create"]

    running_target(cloud, id="created1", remark=run["id"])
    clock.advance()
    runner.tick()
    runner.tick()
    assert db.get_run(run["id"])["result"]["instance_id"] == "created1"
    assert cloud.writes[-1] == ("set_shutdown", "created1", NOW + 1200)


def test_unknown_create_keeps_original_instance_after_remark_readback(runtime):
    db, cloud, clock, runner = runtime
    cloud.create_error = CloudError("transport_error", retryable=True, uncertain=True)
    run = before_deadline(runtime)
    running_target(cloud, id="bound", remark=run["id"], gpu_mode=None, **ZONES[0])
    clock.advance()
    runner.tick()
    assert db.get_run(run["id"])["result"]["instance_id"] == "bound"

    running_target(cloud, id="different", remark=run["id"], **ZONES[0])
    clock.advance()
    runner.tick()
    current = db.get_run(run["id"])
    assert current["state"] == "reconciling" and current["result"]["instance_id"] == "bound"
    with pytest.raises(ValueError, match="已核实实例"):
        db.reconcile_run(run["id"], "bind", "different")


@pytest.mark.parametrize("retryable,state,event,policy", [
    (False, "failed", "result", "shutdown_attention"),
    (True, "waiting", "shutdown-pending", None),
])
def test_linked_shutdown_rejection_emits_one_feishu_event(runtime, monkeypatch, retryable, state, event, policy):
    db, cloud, clock, runner = runtime
    monkeypatch.setattr(db, "feishu_configured", lambda conn: True)
    parent = before_deadline(runtime, shutdown_at=NOW + 1200)
    running_target(cloud, id="created1", remark=parent["id"])
    clock.advance()
    runner.tick()
    child_id = db.get_run(parent["id"])["result"]["shutdown_run_id"]

    def reject(instance, stop_at):
        raise CloudError("429" if retryable else "credential_invalid", retryable=retryable)

    monkeypatch.setattr(cloud, "set_stop", reject)
    runner.tick()
    assert db.get_run(child_id)["state"] == state
    assert db.get_run(parent["id"])["result"]["shutdown_confirmation"] == "unconfirmed"
    with storage.connect(db.path) as conn:
        rows = conn.execute(
            """SELECT event_id,json_extract(payload,'$.policy_event') FROM notifications
               WHERE channel='feishu' AND event_id GLOB ?""", (child_id + ":*",),
        ).fetchall()
        assert [tuple(row) for row in rows] == [(child_id + ":" + event, policy)]


@pytest.mark.parametrize("after_dispatch", [False, True])
def test_linked_shutdown_read_errors_notify_then_stop_at_deadline(runtime, after_dispatch):
    db, cloud, clock, runner = runtime
    run = before_deadline(runtime, shutdown_at=NOW + 1200)
    running_target(cloud, id="created1", remark=run["id"])
    clock.advance()
    runner.tick()
    link_id = db.get_run(run["id"])["result"]["shutdown_run_id"]
    if after_dispatch:
        runner.tick()
        clock.advance()
    cloud.scan_error = CloudError("8433", retryable=True)

    for _ in range(2):
        runner.tick()
        assert db.get_run(run["id"])["result"]["shutdown_confirmation"] == "unconfirmed"
        assert db.get_run(link_id)["state"] == ("reconciling" if after_dispatch else "waiting")
        clock.advance()
    with storage.connect(db.path) as conn:
        pending = conn.execute(
            "SELECT count(*) FROM notifications WHERE event_id=? AND channel='site'",
            (link_id + ":shutdown-pending",),
        ).fetchone()[0]
        assert pending == 1

    clock.advance(1000)
    runner.tick()
    assert db.get_run(link_id)["state"] == ("manual_review" if after_dispatch else "expired")
    assert db.get_run(run["id"])["state"] == "succeeded"
    assert [item[0] for item in cloud.writes] == (["create", "set_shutdown"] if after_dispatch else ["create"])


def test_too_late_for_associated_shutdown_keeps_running_success(runtime):
    db, cloud, clock, runner = runtime
    run = before_deadline(runtime, shutdown_at=NOW + 310)
    running_target(cloud, id="created1", remark=run["id"])
    clock.advance()
    runner.tick()
    runner.tick()
    verified = db.get_run(run["id"])
    assert verified["state"] == "succeeded"
    assert verified["result"]["shutdown_confirmation"] == "unconfirmed"
    assert "shutdown_run_id" not in verified["result"]
    assert [item[0] for item in cloud.writes] == ["create"]


def test_spot_and_unknown_schedules_never_issue_shutdown_writes(runtime):
    db, cloud, clock, runner = runtime
    with pytest.raises(ValueError, match="抢占式"):
        enqueue(db, charge_type="Spot", shutdown_at=NOW + 1200)
    cloud_row = running_target(cloud, charge_type="Spot")
    spot = immediate(db, "shutdown", key="spot", shutdown_at=NOW + 1200)
    runner.tick()
    assert db.get_run(spot["id"])["state"] == "failed"
    cloud_row.update(charge_type="Postpay", scheduler_status="unknown")
    unknown = immediate(db, "shutdown", key="unknown", shutdown_at=NOW + 1200)
    runner.tick()
    assert db.get_run(unknown["id"])["state"] == "waiting"
    assert not cloud.writes


def test_external_shutdown_conflict_requires_replacement_choice(runtime):
    db, cloud, clock, runner = runtime
    running_target(cloud, scheduler_stop_time=NOW + 700)
    no_replace = immediate(db, "shutdown", key="keep", shutdown_at=NOW + 1200)
    runner.tick()
    assert db.get_run(no_replace["id"])["state"] == "manual_review"
    assert not cloud.writes
    blind_replace = immediate(db, "shutdown", key="blind", shutdown_at=NOW + 1200, replace=True)
    runner.tick()
    assert db.get_run(blind_replace["id"])["state"] == "manual_review"
    assert not cloud.writes
    immediate(db, "shutdown", key="replace", shutdown_at=NOW + 1200, replace=True, expected_stop_at=NOW + 700)
    runner.tick()
    assert cloud.writes == [("set_shutdown", "id1", NOW + 1200)]


def test_shutdown_review_preserves_only_the_confirmed_current_appointment(runtime):
    db, cloud, clock, runner = runtime
    instance = running_target(cloud, scheduler_stop_time=NOW + 700)
    run = immediate(db, "shutdown", shutdown_at=NOW + 1200)
    runner.tick()
    original = db.get_run(run["id"])
    assert original["state"] == "manual_review" and original["shutdown_reviewable"]
    with pytest.raises(ValueError, match="先核对"):
        db.queue_shutdown_review(run["id"], NOW + 700)

    runner.read_only = True
    pending = db.queue_shutdown_review(run["id"])
    assert pending["phase"] == "review_shutdown" and pending["state"] == original["state"]
    assert not pending["shutdown_reviewable"]
    assert db.queue_shutdown_review(run["id"]) == pending
    assert db.list_runs("p1", attention=True)[0]["business"]["status"] == "in_progress"
    runner.tick()
    checked = db.get_run(run["id"])
    assert checked["result"]["shutdown_review"]["outcome"] == "different"

    # An external change after the confirmation must leave the issue actionable.
    instance["scheduler_stop_time"] = NOW + 900
    db.queue_shutdown_review(run["id"], NOW + 700)
    clock.advance()
    runner.tick()
    checked = db.get_run(run["id"])
    assert checked["shutdown_reviewable"]
    assert checked["result"]["shutdown_review"]["cloud_shutdown_at"] == NOW + 900
    db.queue_shutdown_review(run["id"], NOW + 900)
    runner.tick()
    reviewed = db.get_run(run["id"])
    evidence = reviewed["result"]["shutdown_review"]
    assert evidence["outcome"] == "kept" and evidence["resolved"]
    assert reviewed["state"] == original["state"] and reviewed["reason"] == original["reason"]
    assert reviewed["snapshot"] == original["snapshot"] and reviewed["operations"] == original["operations"]
    assert reviewed["result"] == original["result"] | {"shutdown_review": evidence}
    assert reviewed["audit"][-1]["data"] == evidence
    assert db.list_runs("p1", attention=True) == []
    assert instance["scheduler_stop_time"] == NOW + 900 and not cloud.writes
    with storage.connect(db.path) as conn:
        assert conn.execute("SELECT 1 FROM shutdown_owners").fetchone() is None


def test_missing_linked_shutdown_review_recognizes_exact_appointment(runtime):
    db, cloud, clock, runner = runtime
    target = NOW + 310
    run = before_deadline(runtime, shutdown_at=target)
    instance = running_target(cloud, id="created1", remark=run["id"])
    clock.advance()
    runner.tick()
    original = db.get_run(run["id"])
    assert original["result"]["shutdown_confirmation"] == "unconfirmed"
    assert not original["result"].get("shutdown_run_id") and original["shutdown_reviewable"]
    instance["scheduler_stop_time"] = target
    db.queue_shutdown_review(run["id"])
    runner.tick()
    reviewed = db.get_run(run["id"])
    assert reviewed["state"] == "succeeded"
    assert reviewed["result"]["shutdown_confirmation"] == "unconfirmed"
    assert reviewed["result"]["shutdown_review"]["outcome"] == "matching"
    assert db.list_runs("p1", attention=True) == []
    assert [item[0] for item in cloud.writes] == ["create"]
    clock.advance(400)
    runner.tick()
    [missed] = db.list_runs("p1", attention=True)
    assert missed["id"] == run["id"] and missed["shutdown_reviewable"]
    assert missed["result"]["shutdown_outcome"] == "missed"
    assert missed["business"]["label"] == "关机结果待处理"


def test_verified_linked_shutdown_is_the_only_attention_item(runtime):
    db, cloud, clock, runner = runtime
    run = before_deadline(runtime, shutdown_at=NOW + 1200)
    running_target(cloud, id="created1", remark=run["id"], scheduler_stop_time=NOW + 800)
    clock.advance()
    runner.tick()
    runner.tick()
    parent = db.get_run(run["id"])
    child_id = parent["result"]["shutdown_run_id"]
    assert parent["shutdown_followup_id"] == child_id
    assert parent["result"]["shutdown_confirmation"] == "unconfirmed" and not parent["shutdown_reviewable"]
    assert [item["id"] for item in db.list_runs("p1", attention=True)] == [child_id]
    with pytest.raises(ValueError, match="关联"):
        db.queue_shutdown_review(run["id"])
    db.queue_shutdown_review(child_id)
    runner.tick()
    db.queue_shutdown_review(child_id, NOW + 800)
    runner.tick()
    assert db.list_runs("p1", attention=True) == []
    assert [item[0] for item in cloud.writes] == ["create"]


def test_exact_external_shutdown_registration_is_observed_without_ownership(runtime, monkeypatch):
    db, cloud, clock, runner = runtime
    target = NOW + 600
    instance = running_target(cloud, scheduler_stop_time=target)
    run = db.submit_action({
        "kind": "shutdown", "project_id": "p1", "channels": ["site", "feishu"],
        "config": ZONES[1] | {"instance_id": "id1", "shutdown_at": target},
    }, "shutdown")
    runner.tick()
    registered = db.get_run(run["id"])
    assert registered["state"] == "succeeded" and "shutdown_outcome" not in registered["result"]
    with storage.connect(db.path) as conn:
        assert conn.execute("SELECT 1 FROM shutdown_owners").fetchone() is None
        assert conn.execute("SELECT stop_at FROM external_shutdown_observations").fetchone()[0] == target
    emitted = capture_notification_events(monkeypatch, runner)
    clock.advance(700)
    instance.update(state="Stopped", stop_time=target)
    runner.tick()
    observed = db.get_run(run["id"])["result"]
    assert observed["shutdown_outcome"] == "confirmed"
    assert observed["shutdown_observation"]["stop_time"] == target
    assert emitted == [("shutdown-outcome", None)]
    assert not cloud.writes
    with storage.connect(db.path) as conn:
        assert {entry["channel"] for entry in conn.execute(
            "SELECT channel FROM notifications WHERE event_id=?", (run["id"] + ":shutdown-outcome",),
        )} == {"site", "feishu"}


def test_missed_shutdown_review_requires_actual_stop_evidence(runtime):
    db, cloud, clock, runner = runtime
    target = NOW + 600
    instance = running_target(cloud)
    run = immediate(db, "shutdown", shutdown_at=target)
    runner.tick()
    instance["scheduler_stop_time"] = target
    clock.advance()
    runner.tick()
    registered = db.get_run(run["id"])
    assert registered["state"] == "succeeded" and "shutdown_outcome" not in registered["result"]

    clock.now = target + 300
    instance["stop_time"] = target
    runner.tick()
    missed = db.get_run(run["id"])
    assert missed["state"] == "succeeded" and missed["result"]["shutdown_outcome"] == "missed"
    db.queue_shutdown_review(run["id"])
    runner.tick()
    assert db.get_run(run["id"])["shutdown_reviewable"]
    assert db.get_run(run["id"])["result"]["shutdown_review"]["outcome"] == "unknown"

    instance.update(state="Stopped", scheduler_status="unknown")
    db.queue_shutdown_review(run["id"])
    runner.tick()
    reviewed = db.get_run(run["id"])
    assert reviewed["result"]["shutdown_outcome"] == "missed"
    evidence = reviewed["result"]["shutdown_review"]
    assert evidence["outcome"] == "stopped" and evidence["resolved"] and not evidence["schedule_known"]
    assert db.list_runs("p1", attention=True) == []
    assert cloud.writes == [("set_shutdown", "id1", target)]


def capture_notification_events(monkeypatch, runner):
    events = []
    emit = runner._emit

    def capture(conn, current, reason, result, event, **kwargs):
        events.append((event, kwargs.get("policy_event")))
        return emit(conn, current, reason, result, event, **kwargs)

    monkeypatch.setattr(runner, "_emit", capture)
    return events


@pytest.mark.parametrize("issue", ["running", "missing", "transport", "unconfigured"])
def test_managed_shutdown_waits_full_grace_then_records_unconfirmed_once(runtime, monkeypatch, issue):
    db, cloud, clock, runner = runtime
    monkeypatch.setattr(db, "feishu_configured", lambda conn: True)
    target = NOW + 600
    vm = running_target(cloud)
    run = immediate(db, "shutdown", shutdown_at=target)
    runner.tick()
    vm["scheduler_stop_time"] = target
    clock.advance()
    runner.tick()
    assert db.get_run(run["id"])["result"]["shutdown_registration"] == "managed"

    vm["stop_time"] = target
    if issue == "missing":
        cloud.instances_by_region = []
    elif issue == "transport":
        cloud.scan_error = CloudError("transport_error", retryable=True)
    elif issue == "unconfigured":
        runner.cloud = None

    emitted = capture_notification_events(monkeypatch, runner)
    clock.now = target + 299
    runner.tick()
    assert "shutdown_outcome" not in db.get_run(run["id"])["result"]
    assert not emitted
    clock.advance(1)
    runner.tick()
    runner.tick()
    observed = db.get_run(run["id"])["result"]
    assert observed["shutdown_outcome"] == "missed"
    assert observed["shutdown_observation"]["checked_at"] == target + 300
    assert observed["shutdown_observation"]["instance_state"] == ("Running" if issue == "running" else None)
    assert emitted == [("shutdown-outcome", "shutdown_attention")]
    with storage.connect(db.path) as conn:
        assert conn.execute("SELECT 1 FROM shutdown_owners").fetchone() is None

    runner.cloud = cloud
    cloud.instances_by_region = [vm]
    cloud.scan_error = None
    vm["state"] = "Stopped"
    db.queue_shutdown_review(run["id"])
    runner.tick()
    runner.tick()
    assert emitted == [("shutdown-outcome", "shutdown_attention"), ("shutdown-confirmed", "instance_stopped")]
    assert not db.get_run(run["id"])["shutdown_reviewable"]
    with storage.connect(db.path) as conn:
        assert [row["event_id"] for row in conn.execute(
            "SELECT event_id FROM notifications WHERE channel='feishu' ORDER BY id",
        )] == [run["id"] + ":shutdown-outcome", run["id"] + ":shutdown-confirmed"]


@pytest.mark.parametrize("historical", [False, True])
def test_managed_shutdown_confirms_stop_with_policy_only_for_new_appointment(runtime, monkeypatch, historical):
    db, cloud, clock, runner = runtime
    monkeypatch.setattr(db, "feishu_configured", lambda conn: True)
    target = NOW + 600
    vm = running_target(cloud)
    run = immediate(db, "shutdown", shutdown_at=target)
    runner.tick()
    vm["scheduler_stop_time"] = target
    clock.advance()
    runner.tick()
    if historical:
        with storage.connect(db.path) as conn:
            storage.set_settings(conn, {"feishu_policy_since": target + 1})

    emitted = capture_notification_events(monkeypatch, runner)
    clock.now = target + 60
    vm.update(state="Stopping", stop_time=target)
    runner.tick()
    assert not emitted and "shutdown_outcome" not in db.get_run(run["id"])["result"]
    clock.advance(30)
    vm["state"] = "Stopped"
    runner.tick()
    assert db.get_run(run["id"])["result"]["shutdown_outcome"] == "confirmed"
    assert emitted == [("shutdown-outcome", None if historical else "instance_stopped")]
    with storage.connect(db.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM notifications WHERE channel='feishu'").fetchone()[0] == int(not historical)


@pytest.mark.parametrize("replacement", [None, NOW + 1200], ids=["canceled", "replaced"])
def test_due_observation_discards_readback_if_managed_timer_changed(runtime, monkeypatch, replacement):
    db, cloud, clock, runner = runtime
    target = NOW + 600
    vm = running_target(cloud)
    run = immediate(db, "shutdown", shutdown_at=target)
    runner.tick()
    vm["scheduler_stop_time"] = target
    clock.advance()
    runner.tick()
    read = cloud.instances

    def read_after_owner_changed(project_id, zones=None):
        with storage.connect(db.path) as conn:
            if replacement is None:
                conn.execute("DELETE FROM shutdown_owners WHERE run_id=?", (run["id"],))
            else:
                conn.execute("UPDATE shutdown_owners SET stop_at=? WHERE run_id=?", (replacement, run["id"]))
        return read(project_id, zones)

    monkeypatch.setattr(cloud, "instances", read_after_owner_changed)
    vm["state"] = "Stopped"
    clock.now = target + 300
    emitted = capture_notification_events(monkeypatch, runner)
    runner.tick()
    assert not emitted and "shutdown_outcome" not in db.get_run(run["id"])["result"]


def test_failed_cancel_retains_original_time_and_owner(runtime):
    db, cloud, clock, runner = runtime
    cloud_row = running_target(cloud)
    reg = immediate(db, "shutdown", shutdown_at=NOW + 1200)
    runner.tick()
    cloud_row["scheduler_stop_time"] = NOW + 1200
    clock.advance()
    runner.tick()
    assert db.get_run(reg["id"])["state"] == "succeeded"
    with storage.connect(db.path) as conn:
        assert conn.execute("SELECT run_id FROM shutdown_owners").fetchone()[0] == reg["id"]
    cloud.cancel_error = CloudError("credential_invalid")
    cancel = immediate(db, "cancel_shutdown", expected_stop_at=NOW + 1200)
    runner.tick()
    failed = db.get_run(cancel["id"])
    assert failed["state"] == "manual_review" and "撤销待确认" in failed["reason"]
    assert failed["result"]["shutdown_at"] == NOW + 1200
    with storage.connect(db.path) as conn:
        assert conn.execute("SELECT run_id FROM shutdown_owners").fetchone()[0] == reg["id"]


@pytest.fixture
def cancellation_waiting_for_review(runtime):
    db, cloud, clock, runner = runtime
    target = running_target(cloud)
    stop_at = NOW + 3600
    owner = immediate(db, "shutdown", shutdown_at=stop_at)
    runner.tick()
    target["scheduler_stop_time"] = stop_at
    clock.advance()
    runner.tick()
    run = immediate(db, "cancel_shutdown", expected_stop_at=stop_at)
    runner.tick()
    clock.advance(181)
    runner.tick()
    prior = db.get_run(run["id"])
    assert prior["state"] == "manual_review"
    assert prior["operations"][0]["state"] == "accepted"
    clock.advance(1800)
    assert clock.now > prior["deadline_at"]
    cloud.reads.clear()
    cloud.writes.clear()
    return prior, owner, target


@pytest.mark.parametrize(("scenario", "outcome", "state"), [
    ("absent", "absent", "reviewed"),
    ("changed", "changed", "reviewed"),
    ("still-present", "still_present", "manual_review"),
    ("unknown-schedule", "unknown", "manual_review"),
    ("wrong-project", "unknown", "manual_review"),
    ("transport-failure", "unknown", "manual_review"),
    ("missing-credentials", "unknown", "manual_review"),
])
def test_operator_cancel_review_reads_cloud_without_replaying(
    runtime, cancellation_waiting_for_review, scenario, outcome, state,
):
    db, cloud, clock, runner = runtime
    prior, owner, target = cancellation_waiting_for_review
    runner.read_only = True
    with storage.connect(db.path) as conn:
        storage.set_settings(conn, {"restore_gate": True})
        notification_count = conn.execute("SELECT count(*) FROM notifications").fetchone()[0]
        if scenario == "changed":
            # An external change is already reflected in a newer local ownership row.
            conn.execute("UPDATE shutdown_owners SET stop_at=?", (NOW + 4500,))
    observed = None
    if scenario == "absent":
        target["scheduler_stop_time"] = None
    elif scenario == "changed":
        observed = target["scheduler_stop_time"] = NOW + 4500
    elif scenario == "still-present":
        observed = target["scheduler_stop_time"]
    elif scenario == "unknown-schedule":
        target.update(scheduler_status="unknown", scheduler_stop_time=None)
    elif scenario == "wrong-project":
        target["project_id"] = "another"
    elif scenario == "transport-failure":
        cloud.scan_error = CloudError("timeout", retryable=True)
    else:
        runner.cloud = None

    scheduled = db.queue_shutdown_review(prior["id"])
    assert scheduled["state"] == "reconciling"
    runner.tick()
    reviewed = db.get_run(prior["id"])
    evidence = {
        "checked_at": clock.now, "outcome": outcome,
        "original_shutdown_at": NOW + 3600, "cloud_shutdown_at": observed,
    }
    assert reviewed["state"] == state and reviewed["phase"] is None
    assert reviewed["result"] == prior["result"] | {"shutdown_review": evidence}
    assert reviewed["operations"] == prior["operations"]
    assert reviewed["audit"][-1]["action"] == "review_cancel_shutdown_result"
    assert reviewed["audit"][-1]["data"] == evidence
    assert not cloud.writes
    assert cloud.reads == ([] if scenario == "missing-credentials" else [("inventory", "p1", [ZONES[1]])])
    with storage.connect(db.path) as conn:
        assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == notification_count
        row = conn.execute("SELECT run_id,stop_at FROM shutdown_owners").fetchone()
        if scenario == "absent":
            assert row is None
        else:
            assert tuple(row) == (owner["id"], NOW + (4500 if scenario == "changed" else 3600))


def test_cancel_review_does_not_apply_outdated_readback(runtime, cancellation_waiting_for_review, monkeypatch):
    db, cloud, _, runner = runtime
    prior, owner, target = cancellation_waiting_for_review
    target["scheduler_stop_time"] = None
    queued = db.queue_shutdown_review(prior["id"])
    read = cloud.instances

    def read_after_run_changed(project_id, zones=None):
        with storage.connect(db.path) as conn:
            conn.execute(
                "UPDATE task_runs SET state='manual_review',phase=NULL,reason=? WHERE id=?",
                ("另一回读已结束", prior["id"]),
            )
        return read(project_id, zones)

    monkeypatch.setattr(cloud, "instances", read_after_run_changed)
    runner.tick()
    current = db.get_run(prior["id"])
    assert current["state"] == "manual_review"
    assert current["reason"] == "另一回读已结束"
    assert current["result"] == queued["result"]
    assert not any(entry["action"] == "review_cancel_shutdown_result" for entry in current["audit"])
    assert not cloud.writes
    with storage.connect(db.path) as conn:
        assert conn.execute("SELECT run_id FROM shutdown_owners").fetchone()[0] == owner["id"]


def test_cancel_review_with_current_schedule_and_unknown_original_time(runtime, cancellation_waiting_for_review):
    db, cloud, _, runner = runtime
    prior, _, target = cancellation_waiting_for_review
    target["scheduler_stop_time"] = NOW + 4500
    with storage.connect(db.path) as conn:
        conn.execute(
            """UPDATE task_runs SET snapshot=json_remove(snapshot,'$.config.expected_stop_at'),
               result=json_remove(result,'$.shutdown_at') WHERE id=?""",
            (prior["id"],),
        )
    db.queue_shutdown_review(prior["id"])
    runner.tick()
    reviewed = db.get_run(prior["id"])
    assert reviewed["state"] == "manual_review"
    evidence = reviewed["result"]["shutdown_review"]
    assert evidence["outcome"] == "unknown"
    assert evidence["original_shutdown_at"] is None
    assert evidence["cloud_shutdown_at"] == NOW + 4500
    assert not cloud.writes


def task_shutdown(db, interval=False):
    return db.create_task({
        "name": "关机任务", "project_id": "p1", "kind": "shutdown",
        "schedule": {"type": "interval", "at": NOW + 400, "interval_seconds": 1200}
        if interval else {"type": "once", "at": NOW + 600},
        "config": ZONES[1] | {"instance_id": "id1"},
    })


def test_cancel_never_cancels_an_externally_changed_time(runtime):
    db, cloud, clock, runner = runtime
    row = running_target(cloud)
    task = task_shutdown(db)
    runner.tick()
    row["scheduler_stop_time"] = NOW + 600
    clock.advance()
    runner.tick()
    db.control_task(task["id"], "cancel")
    row["scheduler_stop_time"] = NOW + 950
    runner.tick()
    cancel = next(run for run in db.list_runs(task_id=task["id"]) if run["kind"] == "cancel_shutdown")
    assert cancel["state"] == "manual_review" and cancel["result"]["cloud_shutdown_at"] == NOW + 950
    assert cloud.writes == [("set_shutdown", "id1", NOW + 600)]


def test_cancel_during_registration_cancels_only_after_ownership_readback(runtime):
    db, cloud, clock, runner = runtime
    row = running_target(cloud)
    task = task_shutdown(db)
    runner.tick()
    db.control_task(task["id"], "cancel")
    assert cloud.writes == [("set_shutdown", "id1", NOW + 600)]
    row["scheduler_stop_time"] = NOW + 600
    clock.advance()
    runner.tick()
    runner.tick()
    assert cloud.writes[-1] == ("cancel_shutdown", "id1")
    row["scheduler_stop_time"] = None
    clock.advance()
    runner.tick()
    cancel = next(run for run in db.list_runs(task_id=task["id"]) if run["kind"] == "cancel_shutdown")
    assert cancel["state"] == "succeeded"


def test_worker_offline_through_two_periods_records_both_missed_and_checks_registered_one(runtime):
    db, cloud, clock, runner = runtime
    row = running_target(cloud)
    task = task_shutdown(db, interval=True)
    runner.tick()
    row["scheduler_stop_time"] = NOW + 400
    clock.advance()
    runner.tick()
    clock.advance(3300)
    row.update(state="Stopped", stop_time=NOW + 400)
    runner.tick()
    runner.tick()
    runner.tick()
    runs = db.list_runs(task_id=task["id"])
    assert {run["scheduled_at"] for run in runs if run["state"] == "missed"} == {NOW + 1600, NOW + 2800}
    registered = next(run for run in runs if run["scheduled_at"] == NOW + 400)
    assert registered["result"]["shutdown_outcome"] == "confirmed"
    row["state"] = "Running"
    clock.advance()
    runner.tick()
    assert cloud.writes == [
        ("set_shutdown", "id1", NOW + 400),
        ("set_shutdown", "id1", NOW + 4000),
    ]


def test_recurring_shutdown_previous_time_is_scoped_to_original_instance(runtime):
    db, cloud, clock, runner = runtime
    original = running_target(cloud)
    task = task_shutdown(db, interval=True)
    runner.tick()
    original["scheduler_stop_time"] = NOW + 400
    clock.advance()
    runner.tick()
    db.update_task(task["id"], task | {"config": task["config"] | {"instance_id": "id2", "replace": True}})

    clock.advance(460)
    original.update(state="Stopped", stop_time=NOW + 400)
    cloud.instances_by_region.append(instance("id2", scheduler_status="known", scheduler_stop_time=NOW + 400))
    runner.tick()

    runs = db.list_runs(task_id=task["id"])
    first = next(run for run in runs if run["scheduled_at"] == NOW + 400)
    second = next(run for run in runs if run["scheduled_at"] == NOW + 1600)
    assert first["result"]["shutdown_outcome"] == "confirmed"
    assert second["state"] == "manual_review"
    assert cloud.writes == [("set_shutdown", "id1", NOW + 400)]


def test_reconciled_purchases_from_before_restore_count_against_task_limit(runtime):
    db, cloud, _, runner = runtime
    task_id = enqueue(db)
    with storage.connect(db.path) as conn:
        conn.execute("INSERT INTO recovery_purchases(task_id,adjustment) VALUES (?,?)", (task_id, 1))
    runner.tick()
    assert db.list_runs(task_id=task_id)[0]["reason"] == "已达到任务总购买次数上限"
    assert not cloud.writes


def test_pause_between_reservation_and_network_dispatch(runtime, monkeypatch):
    db, cloud, clock, runner = runtime
    task_id = enqueue(db)
    claim = runner._claim

    def paused_claim(run, *args):
        saved = claim(run, *args)
        db.control_task(task_id, "pause")
        return saved

    monkeypatch.setattr(runner, "_claim", paused_claim)
    runner.tick()
    run = db.get_run(db.list_runs(task_id=task_id)[0]["id"])
    assert run["state"] == "canceled"
    assert run["operations"][0]["state"] == "not_dispatched"
    assert not cloud.writes


def test_unconfirmed_start_ownership_does_not_attach_shutdown(runtime):
    db, cloud, clock, runner = runtime
    row = running_target(cloud, state="Stopped", gpu_mode=None)
    cloud.start_error = CloudError("503", uncertain=True)
    operation = immediate(db, "start", shutdown_at=NOW + 1200)
    runner.tick()
    row.update(state="Running", gpu_mode="with_gpu")
    clock.advance()
    runner.tick()
    result = db.get_run(operation["id"])["result"]
    assert result["disposition"] == "observed"
    assert result["shutdown_confirmation"] == "unconfirmed"
    assert cloud.writes == [("start", "id1")]


def test_confirmed_replacement_cannot_override_a_later_external_change(runtime):
    db, cloud, _, runner = runtime
    running_target(cloud, scheduler_stop_time=NOW + 700)
    run = immediate(db, "shutdown", shutdown_at=NOW + 1200,
                    expected_stop_at=NOW + 600, replace=True)
    runner.tick()
    assert db.get_run(run["id"])["state"] == "manual_review"
    assert not cloud.writes


def test_cancel_direct_start_preserves_submitted_readback(runtime):
    store, cloud, clock, runner = runtime
    target = instance("existing", "Stopped")
    cloud.instances_by_region = [target]
    request = {
        "project_id": "p1", "kind": "start",
        "config": {"instance_id": target["id"], "region": target["region"], "zone": target["zone"]},
    }
    queued = store.submit_action(request, "pending")
    assert store.cancel_action(queued["id"])["state"] == "canceled"
    runner.tick()
    assert not cloud.writes

    submitted = store.submit_action(request, "submitted")
    runner.tick()
    assert cloud.writes == [("start", target["id"])]
    stopped = store.cancel_action(submitted["id"])
    assert stopped["stopped"] == 1 and stopped["phase"] == "verify_start"
    cloud.instances_by_region = [instance(target["id"], start_time=clock())]
    clock.advance()
    runner.tick()
    assert store.get_run(submitted["id"])["state"] == "succeeded"
    assert cloud.writes == [("start", target["id"])]


def test_reboot_requires_readback_after_worker_restart(runtime):
    store, cloud, clock, runner = runtime
    row = running_target(cloud, start_time=NOW - 100)
    run = immediate(store, "reboot")
    runner.tick()
    pending = store.get_run(run["id"])
    assert pending["state"] == "reconciling"
    assert pending["operations"][0]["data"]["previous_start_time"] == NOW - 100

    clock.advance()
    runner.tick()
    assert store.get_run(run["id"])["state"] == "reconciling"
    row["state"] = "Rebooting"
    clock.advance()
    runner.tick()
    assert store.get_run(run["id"])["result"]["observed_rebooting"] is True

    row["state"] = "Running"
    clock.advance()
    TaskRunner(store, cloud).tick()
    assert store.get_run(run["id"])["state"] == "succeeded"
    assert cloud.writes == [("reboot", "id1")]


def test_unknown_reboot_receipt_can_be_confirmed_by_new_start_time(runtime):
    store, cloud, clock, runner = runtime
    row = running_target(cloud, start_time=NOW - 100)
    cloud.reboot_error = CloudError("503", uncertain=True, retryable=True)
    run = immediate(store, "reboot")
    runner.tick()
    assert store.get_run(run["id"])["operations"][0]["state"] == "reserved"
    clock.advance()
    row["start_time"] = clock()
    TaskRunner(store, cloud).tick()
    assert store.get_run(run["id"])["state"] == "succeeded"
    assert cloud.writes == [("reboot", "id1")]


@pytest.mark.parametrize("scope", [{"state": "Stopped"}, {"project_id": "p2"}, {"zone": "another-zone"}])
def test_reboot_requires_a_running_instance_in_the_confirmed_scope(runtime, scope):
    store, cloud, _, runner = runtime
    running_target(cloud, **scope)
    run = immediate(store, "reboot")
    runner.tick()
    assert store.get_run(run["id"])["state"] == "failed"
    assert cloud.writes == []


def test_conflicting_instance_operations_preserve_same_request_id(runtime):
    store, cloud, _, runner = runtime
    running_target(cloud, name="原名")
    run = immediate(store, "reboot")
    assert immediate(store, "reboot")["id"] == run["id"]
    with pytest.raises(ValueError, match="操作在途"):
        immediate(store, "rename", name="新名", expected_name="原名")
    runner.tick()
    with pytest.raises(ValueError, match="操作在途"):
        immediate(store, "stop")
    assert cloud.writes == [("reboot", "id1")]


def test_rename_validates_current_name_and_waits_for_exact_new_name(runtime):
    store, cloud, clock, runner = runtime
    row = running_target(cloud, name="原名", state="Stopped")
    new_name = "训练_gpu,01:OK-2.3"
    stale = immediate(store, "rename", "stale", name=new_name, expected_name="旧快照")
    runner.tick()
    assert store.get_run(stale["id"])["state"] == "failed"
    assert not cloud.writes

    actual = immediate(store, "rename", "actual", name=new_name, expected_name="原名")
    runner.tick()
    assert store.get_run(actual["id"])["state"] == "reconciling"
    assert cloud.writes == [("rename", "id1", new_name)]
    row["name"] = new_name
    clock.advance()
    TaskRunner(store, cloud).tick()
    assert store.get_run(actual["id"])["state"] == "succeeded"

    same = immediate(store, "rename", "same", name=new_name, expected_name="旧快照")
    runner.tick()
    assert store.get_run(same["id"])["result"]["disposition"] == "unchanged"
    assert cloud.writes == [("rename", "id1", new_name)]


@pytest.mark.parametrize("new_name", [False, True])
def test_rename_protects_names_of_unresolved_purchases(runtime, new_name):
    store, cloud, _, runner = runtime
    cloud.create_error = CloudError("503", uncertain=True, retryable=True)
    pending = before_deadline(runtime)
    marker = store.get_run(pending["id"])["operations"][0]["data"]["submitted_name"]
    previous = "其他实例" if new_name else marker
    running_target(cloud, name=previous)
    run = immediate(store, "rename", name=marker if new_name else "变更", expected_name=previous)
    runner.tick()
    assert "待核对的采购" in store.get_run(run["id"])["reason"]
    assert [item[0] for item in cloud.writes] == ["create"]


@pytest.mark.parametrize("kind", ["reboot", "rename"])
def test_unclear_instance_change_becomes_manual_review_without_resending(runtime, kind, monkeypatch):
    store, cloud, clock, runner = runtime
    running_target(cloud, name="原名")
    config = {"name": "新名", "expected_name": "原名"} if kind == "rename" else {}
    run = immediate(store, kind, **config)
    runner.tick()

    def inventory_unavailable(*_):
        raise CloudError("transport_error")

    monkeypatch.setattr(cloud, "instances", inventory_unavailable)
    clock.advance(601)
    TaskRunner(store, cloud).tick()
    assert store.get_run(run["id"])["state"] == "manual_review"
    clock.advance()
    runner.tick()
    assert [item[0] for item in cloud.writes] == [kind]


@pytest.mark.parametrize("kind", ["start", "stop", "reboot", "rename"])
def test_instance_operation_rechecks_after_ten_minutes_without_replaying(runtime, kind):
    store, cloud, clock, runner = runtime
    row = running_target(cloud, name="原名", start_time=NOW - 10)
    if kind == "start":
        row.update(state="Stopped", gpu_mode=None)
    run = immediate(store, kind, **({"name": "新名", "expected_name": "原名"} if kind == "rename" else {}))
    runner.tick()
    clock.advance(600)
    reads = len(cloud.reads)
    runner.tick()
    assert len(cloud.reads) == reads
    assert store.get_run(run["id"])["state"] == "manual_review"
    with pytest.raises(ValueError, match="操作在途"):
        immediate(store, "stop", "blocked")

    store.queue_instance_recheck(run["id"])
    store.queue_instance_recheck(run["id"])
    TaskRunner(store, cloud, read_only=True).tick()
    unknown = store.get_run(run["id"])
    assert unknown["state"] == "manual_review"
    assert unknown["result"]["instance_review"]["checked_at"] == clock()
    assert [item["action"] for item in unknown["audit"]].count("instance_recheck_requested") == 1

    clock.advance()
    if kind == "start":
        row.update(state="Running", gpu_mode="with_gpu")
    elif kind == "stop":
        row.update(state="Stopped")
    elif kind == "reboot":
        row.update(start_time=clock())
    else:
        row.update(name="新名")
    store.queue_instance_recheck(run["id"])
    TaskRunner(store, cloud, read_only=True).tick()
    checked = store.get_run(run["id"])
    assert checked["state"] == "succeeded" and not checked["phase"]
    assert checked["result"]["instance_review"]["checked_at"] == clock()
    assert [item[0] for item in cloud.writes] == [kind]
    assert not store.list_runs("p1", attention=True)
    assert immediate(store, "stop", "allowed")["state"] == "queued"


def test_cloud_lock_rejects_our_reboot_and_does_not_submit_later(runtime):
    store, cloud, clock, runner = runtime
    row = running_target(cloud)
    cloud.reboot_error = CloudError("InstanceOperationInProgress", retryable=True)
    run = immediate(store, "reboot")
    runner.tick()
    row["state"] = "Rebooting"
    clock.advance()
    runner.tick()
    row["state"] = "Running"
    clock.advance()
    runner.tick()
    failed = store.get_run(run["id"])
    assert failed["state"] == "failed" and failed["operations"][0]["state"] == "rejected"
    assert cloud.writes == [("reboot", "id1")]


def test_instance_history_includes_all_runs_resolved_to_the_instance(runtime):
    store, cloud, _, _ = runtime
    running_target(cloud)
    acquire_run = before_deadline(runtime)
    stop_run = immediate(store, "stop")
    immediate(store, "stop", "another-instance", instance_id="id2")
    assert {run["id"] for run in store.list_runs("p1", instance_id="id1")} == {acquire_run["id"], stop_run["id"]}
    assert [run["id"] for run in store.list_runs("p1", acquire_run["task_id"], "id1")] == [acquire_run["id"]]
    assert not store.list_runs("p2", instance_id="id1")


def test_accepted_create_conflicts_and_appears_in_history_before_binding(runtime):
    store, cloud, _, runner = runtime
    pending = before_deadline(runtime, skip_if_match=False)
    assert pending["state"] == "reconciling"
    assert pending["result"]["submitted_ids"] == ["created1"]
    assert "instance_id" not in pending["result"]
    assert [run["id"] for run in store.list_runs("p1", instance_id="created1")] == [pending["id"]]
    assert not store.list_runs("p1", instance_id="created")

    with pytest.raises(ValueError, match="操作在途"):
        immediate(store, "reboot", instance_id="created1", **ZONES[0])

    cloud.instances_by_region = [instance("created1", **ZONES[0])]
    stop = immediate(store, "stop", instance_id="created1", **ZONES[0])
    runner.tick()
    assert store.get_run(stop["id"])["state"] == "waiting"
    assert [call[0] for call in cloud.writes] == ["create"]
