import json

import pytest

from gpu_console import storage
from gpu_console.application.notifications import list_notifications
from gpu_console.application.tasks import TaskStore
from gpu_console.worker.tasks import TaskRunner
from test_collection import PROJECT, all_cards, point, setup
from test_notification_events import SPEC


def notice(path, event_type):
    with storage.connect(path) as conn:
        page = list_notifications(conn, limit=10, channel="site", project_id=None,
                                  event_type=event_type, event_id=None, cursor=None)
    [item] = page["items"]
    return item


def idle_episode(tmp_path, monkeypatch):
    time, cloud, collector = setup(tmp_path, monkeypatch)
    for timestamp in (1060, 1120, 1180):
        time[0] = timestamp
        cloud.points = all_cards(timestamp)
        collector.tick()
    item = notice(collector.path, "idle")
    assert item["business"]["attention"] is True
    assert item["business"]["destination"]["project_id"] == PROJECT
    assert item["business"]["destination"]["instance_id"] == cloud.rows[0]["id"]
    return time, cloud, collector, item


@pytest.mark.parametrize("legacy", [True, False])
def test_idle_unknown_gaps_and_recovery_use_original_threshold(tmp_path, monkeypatch, legacy):
    time, cloud, collector, original = idle_episode(tmp_path, monkeypatch)
    if legacy:
        # An older release stored the original threshold in the notice text.
        with storage.connect(collector.path) as conn:
            conn.execute(
                "UPDATE notifications SET payload=json_remove(payload,'$.idle_threshold','$.gpu_count') WHERE id=?",
                (original["id"],),
            )
    time[0] = 1240
    cloud.points = [point(1240, "GPU-1", 30), point(1240, "GPU-2", 30) | {"project_id": "outside"}]
    collector.tick()
    assert notice(collector.path, "idle")["business"]["status"] == "unknown"
    time[0] = 1480
    cloud.points = all_cards(1300, 30)
    collector.tick()
    assert notice(collector.path, "idle")["business"]["status"] == "unknown"
    time[0] = 1540
    cloud.points = all_cards(1540, 15)
    with storage.connect(collector.path) as conn:
        storage.set_settings(conn, {"idle_threshold": 20})
    collector.tick()
    item = notice(collector.path, "idle")
    assert item["payload"]["message"] == original["payload"]["message"]
    assert item["business"]["status"] == "resolved" and item["business"]["observed_at"] == 1540
    time[0] = 1800
    cloud.monitor_error = True
    collector.tick()
    assert notice(collector.path, "idle")["business"] == item["business"]


@pytest.mark.parametrize(("state", "boot", "label"), [
    ("Stopped", 1000, "实例已关机"), ("Running", 1260, "实例已重新启动"),
])
def test_idle_requires_confirmed_stop_or_a_later_boot(tmp_path, monkeypatch, state, boot, label):
    time, cloud, collector, _ = idle_episode(tmp_path, monkeypatch)
    time[0] = 1240
    cloud.rows[0]["state"] = "Stopping"
    collector.tick()
    assert notice(collector.path, "idle")["business"]["status"] == "unknown"
    time[0] = 1300
    cloud.rows[0].update(state=state, start_time=boot)
    collector.tick()
    resolved = notice(collector.path, "idle")["business"]
    assert resolved["status"] == "resolved" and resolved["label"] == label and resolved["observed_at"] == 1300


def test_stock_notice_targets_only_the_scoped_winning_spec(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    storage.initialize(path)
    monkeypatch.setattr(storage, "now", lambda: 1000)
    store = TaskStore(path, clock=storage.now)
    wanted = {"region": "r2", "zone": "z2"}
    task = store.create_task({
        "kind": "stock", "name": "等待 5090", "project_id": "p1", "schedule": {"type": "once", "at": 1000},
        "config": SPEC | {"zones": [SPEC["zones"][0], wanted]},
    })

    class Capacity:
        def capacity(self, spec):
            return spec["zone"] == wanted["zone"]

    TaskRunner(store, Capacity()).tick()
    [run] = store.list_runs(task_id=task["id"])
    from gpu_console.application import watches
    from gpu_console.storage import availability

    expected = {key: SPEC[key] for key in ("gpu_model", "gpu", "cpu", "memory_gb", "charge_type")} | wanted
    with storage.connect(path) as conn:
        listing = expected | {"instance_type": "uhost", "specs": [expected]}
        conn.execute("INSERT INTO snapshots VALUES ('catalog','',?,1000,NULL)", (json.dumps([listing]),))
        for watch_id, project_id, spec in (
            ("other-project", "p2", expected), ("other-zone", "p1", expected | SPEC["zones"][0]),
            ("other-combination", "p1", expected | {"gpu": 8}),
        ):
            conn.execute("INSERT INTO watches VALUES (?,?,?,?)", (watch_id, project_id, watch_id, json.dumps(spec)))
        watches.sync_bindings(conn)
    original = notice(path, "stock")
    assert original["payload"]["run_id"] == run["id"]
    assert original["business"]["destination"] == {"page": "inventory", "project_id": "p1", "spec": expected}
    with storage.connect(path) as conn:
        conn.execute("INSERT INTO watches VALUES ('exact','p1','关注',?)", (json.dumps(expected | {"image_id": "other"}),))
        watches.sync_bindings(conn)
        availability.record_pool(conn, "r2", 1000, provider_updated_at=1000,
                                 counts={("z2", expected["gpu_model"], "exclusive"): 0})
    exact = notice(path, "stock")
    assert exact["payload"] == original["payload"]
    assert exact["business"]["destination"] == {"page": "inventory", "project_id": "p1", "watch_id": "exact"}
    assert exact["business"]["label"] == "规格可租状态待核对" and exact["business"]["observed_at"] is None
