from gpu_console import storage
from gpu_console.application.tasks import TaskStore
from gpu_console.worker.collection import _idle_event
from gpu_console.worker.tasks import TaskRunner

SPEC = {
    "zones": [{"region": "r", "zone": "z"}],
    "gpu_model": "5090", "gpu": 1, "cpu": 8, "memory_gb": 16,
    "cpu_platform": "Intel/Auto", "image_id": "image",
    "charge_type": "Postpay",
    "disks": [{"purpose": "system", "type": "CLOUD_SSD", "size_gb": 100}],
}


def test_event_choices_are_pinned_to_runs_and_mute_only_the_notice(tmp_path):
    database = tmp_path / "test.db"
    storage.initialize(database)
    moment = [1000]
    store = TaskStore(database, clock=lambda: moment[0])
    task = {
        "kind": "stock", "name": "库存", "project_id": "p", "schedule": {"type": "once", "at": 2000},
        "config": SPEC, "channels": ["feishu"], "window_seconds": 30,
        "notification_events": ["acquisition_succeeded"],
    }
    item = store.create_task(task)
    first = store.trigger_task(item["id"], "first")
    store.update_task(item["id"], task | {"notification_events": []})
    second = store.trigger_task(item["id"], "second")

    class Capacity:
        def capacity(self, spec):
            return True

    TaskRunner(store, Capacity()).tick()
    assert store.get_run(first["id"])["state"] == store.get_run(second["id"])["state"] == "succeeded"
    with storage.connect(database) as conn:
        assert [tuple(row) for row in conn.execute(
            "SELECT event_id,channel FROM notifications ORDER BY id",
        )] == [(first["id"] + ":result", "site"), (first["id"] + ":result", "feishu")]


def test_idle_subscription_applies_to_boot_confirmed_by_the_task(tmp_path):
    database = tmp_path / "test.db"
    storage.initialize(database)
    store = TaskStore(database, clock=lambda: 1000)
    config = {"instance_id": "uhost-test", "region": "r", "zone": "z"}
    task = store.create_task({
        "kind": "start", "name": "启动", "project_id": "p", "schedule": {"type": "once", "at": 2000},
        "config": config, "notification_events": [],
    })
    run = store.trigger_task(task["id"], "start")
    instance = SPEC | config | {
        "id": config["instance_id"], "project_id": "p", "name": "GPU", "start_time": 1000,
    }
    TaskRunner(store, None)._complete_instance(run, instance, "started")
    with storage.connect(database) as conn, storage.transaction(conn):
        settings = storage.get_settings(conn)
        _idle_event(conn, instance, 1100, 3000, settings)
        assert not conn.execute("SELECT 1 FROM notifications").fetchone()
        _idle_event(conn, instance | {"start_time": 4000}, 4100, 6000, settings)
        assert conn.execute("SELECT channel FROM notifications").fetchone()[0] == "site"
