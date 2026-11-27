import json

import pytest
from pydantic import ValidationError

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.adapters.notifications import DeliveryError
from gpu_console.application.feishu_notices import (
    DEFAULT_POLICY,
    FeishuEventPolicy,
    cancel_restored,
    capacity_wait_tick,
    delivery_hold,
    emit,
)
from gpu_console.application.tasks import TaskStore
from gpu_console.worker.outbox import Outbox
from gpu_console.worker.tasks import TaskRunner


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "console.db"
    storage.initialize(path)
    return path


def deliveries(conn):
    return {
        (row["event_id"], row["channel"]): dict(row) | {"payload": json.loads(row["payload"])}
        for row in conn.execute("SELECT * FROM notifications")
    }


def test_policy_model_defaults_and_allows_disabling_every_event():
    assert FeishuEventPolicy.model_validate(DEFAULT_POLICY).model_dump() == DEFAULT_POLICY == {
        "events": ["instance_started", "instance_stopped", "operation_failed", "operation_review", "shutdown_attention"],
        "capacity_wait_minutes": 15,
    }
    assert FeishuEventPolicy(events=[], capacity_wait_minutes=1).events == []


@pytest.mark.parametrize("data", [
    DEFAULT_POLICY | {"events": ["instance_started", "instance_started"]},
    DEFAULT_POLICY | {"events": ["invalid"]},
    DEFAULT_POLICY | {"capacity_wait_minutes": 0},
    DEFAULT_POLICY | {"capacity_wait_minutes": 1441},
    DEFAULT_POLICY | {"capacity_wait_minutes": True},
    DEFAULT_POLICY | {"unknown": True},
    {"events": []},
    {"capacity_wait_minutes": 20},
])
def test_policy_rejects_invalid_settings(data):
    with pytest.raises(ValidationError):
        FeishuEventPolicy.model_validate(data)


def test_global_policy_adds_feishu_without_displacing_existing_subscriptions(database):
    with storage.connect(database) as conn, storage.transaction(conn):
        emit(conn, "start:result", {"title": "训练任务"}, ["site"], policy_event="instance_started", feishu_configured=True)
        emit(conn, "start:result", {}, ["site"], policy_event="instance_started", feishu_configured=True)
        emit(conn, "overlap:result", {}, ["site", "feishu"], policy_event="instance_stopped", feishu_configured=True)
        emit(conn, "unconfigured:result", {}, ["site"], policy_event="operation_failed")

        storage.set_settings(conn, {"feishu_event_policy": DEFAULT_POLICY | {"events": []}})
        emit(conn, "disabled:result", {}, ["site"], policy_event="instance_started", feishu_configured=True)
        emit(conn, "explicit:result", {}, ["feishu", "email"], policy_event="instance_started", feishu_configured=True)

        rows = deliveries(conn)
        assert set(rows) == {
            ("start:result", "site"), ("start:result", "feishu"), ("overlap:result", "site"), ("overlap:result", "feishu"),
            ("unconfigured:result", "site"), ("disabled:result", "site"),
            ("explicit:result", "feishu"), ("explicit:result", "email"),
        }
        assert rows["start:result", "feishu"]["payload"] == {
            "title": "训练任务", "policy_event": "instance_started", "feishu_title": "实例开机已确认",
            "feishu_policy_only": True,
        }
        assert rows["start:result", "feishu"]["state"] == "pending"
        assert "feishu_policy_only" not in rows["overlap:result", "feishu"]["payload"]
        assert rows["explicit:result", "feishu"]["payload"] == {
            "policy_event": "instance_started", "feishu_title": "实例开机已确认",
        }


def test_restore_skips_only_pending_feishu_added_by_the_global_policy(database):
    with storage.connect(database) as conn, storage.transaction(conn):
        emit(conn, "policy:review", {}, ["site"], policy_event="operation_review", feishu_configured=True)
        emit(conn, "explicit:review", {}, ["feishu"], policy_event="operation_review", feishu_configured=True)
        emit(conn, "sent:review", {}, ["site"], policy_event="operation_review", feishu_configured=True)
        conn.execute("UPDATE notifications SET state='delivered' WHERE event_id='sent:review' AND channel='feishu'")

        cancel_restored(conn)
        storage.set_settings(conn, {"restore_gate": True})
        emit(conn, "during-restore:review", {}, ["site"], policy_event="operation_review", feishu_configured=True)
        rows = deliveries(conn)
        assert rows["policy:review", "feishu"]["state"] == "skipped"
        assert rows["policy:review", "site"]["state"] == "delivered"
        assert rows["explicit:review", "feishu"]["state"] == "pending"
        assert rows["sent:review", "feishu"]["state"] == "delivered"
        assert ("during-restore:review", "feishu") not in rows


def test_capacity_reminder_uses_confirmed_waiting_state_and_emits_once_before_next_cloud_retry(database, monkeypatch):
    class Cloud:
        start_calls = 0

        def instances(self, project_id, scopes):
            return [{"id": "gpu", "project_id": project_id, "state": "Stopped", **scopes[0]}]

        def start(self, instance):
            self.start_calls += 1
            raise CloudError("226604", retryable=True)

    first_attempt = 1000
    store = TaskStore(database, clock=lambda: first_attempt)
    task = store.create_task({
        "name": "预约训练", "kind": "start", "project_id": "project", "window_seconds": 3,
        "schedule": {"type": "once", "at": first_attempt},
        "config": {"instance_id": "gpu", "region": "region", "zone": "zone",
                   "capacity_retry": {"interval_seconds": 3600}},
    })
    cloud = Cloud()
    TaskRunner(store, cloud).tick()
    run, = store.list_runs(task_id=task["id"])
    assert run["state"] == "waiting"
    assert run["next_check"] == first_attempt + 3600

    threshold = first_attempt + 15 * 60
    with storage.connect(database) as conn, storage.transaction(conn):
        capacity_wait_tick(conn, threshold, feishu_configured=True)
        assert deliveries(conn) == {}
        storage.set_settings(conn, {"feishu_event_policy": DEFAULT_POLICY | {"events": ["capacity_wait"]}})
        capacity_wait_tick(conn, threshold, feishu_configured=False)
        capacity_wait_tick(conn, threshold - 1, feishu_configured=True)
        conn.execute("UPDATE task_runs SET reason='已有实例正在运行' WHERE id=?", (run["id"],))
        capacity_wait_tick(conn, threshold, feishu_configured=True)
        conn.execute("UPDATE task_runs SET reason=?,stopped=1 WHERE id=?", (run["reason"], run["id"]))
        capacity_wait_tick(conn, threshold, feishu_configured=True)
        assert deliveries(conn) == {}
        conn.execute("UPDATE task_runs SET stopped=0 WHERE id=?", (run["id"],))
        capacity_wait_tick(conn, threshold, feishu_configured=True)

    with storage.connect(database) as conn, storage.transaction(conn):
        capacity_wait_tick(conn, threshold + 5 * 60, feishu_configured=True)
        rows = deliveries(conn)
        key = (run["id"] + ":capacity-wait", "feishu")
        assert set(rows) == {key}
        payload = rows[key]["payload"]
        assert payload["instance_id"] == "gpu" and payload["confirmed_at"] == threshold
        assert payload["policy_event"] == "capacity_wait" and "15 分钟" in payload["message"]
        queued = conn.execute("SELECT * FROM notifications").fetchone()
        storage.set_settings(conn, {"restore_gate": True})
        assert delivery_hold(conn, queued, payload, threshold)[0] == "skipped"
        storage.set_settings(conn, {"restore_gate": False, "feishu_event_policy": DEFAULT_POLICY})
        assert delivery_hold(conn, queued, payload, threshold) is None
        conn.execute("UPDATE task_runs SET reason='已有实例正在运行' WHERE id=?", (run["id"],))
        assert delivery_hold(conn, queued, payload, threshold)[0] == "skipped"
        conn.execute("UPDATE task_runs SET reason=?,stopped=1 WHERE id=?", (run["reason"], run["id"]))
        assert delivery_hold(conn, queued, payload, threshold)[0] == "skipped"
        conn.execute("UPDATE task_runs SET stopped=0 WHERE id=?", (run["id"],))

    current = store.get_run(run["id"])
    assert current["next_check"] == run["next_check"] and current["state"] == "waiting"
    assert cloud.start_calls == 1

    class Feishu:
        attempts = 0

        def send(self, channel, payload, *, event_id):
            assert (event_id, channel) == key
            self.attempts += 1
            raise DeliveryError("模拟飞书限流", retryable=True)

    clock = [queued["next_at"]]
    monkeypatch.setattr(storage, "now", lambda: clock[0])
    feishu = Feishu()
    outbox = Outbox(database, feishu)
    outbox.tick()
    with storage.connect(database) as conn:
        delivery = deliveries(conn)[key]
        assert delivery["state"] == "pending" and delivery["attempts"] == 1
        conn.execute("UPDATE task_runs SET state='succeeded' WHERE id=?", (run["id"],))
    clock[0] = delivery["next_at"]
    outbox.tick()
    with storage.connect(database) as conn:
        delivery = deliveries(conn)[key]
        assert delivery["state"] == "skipped" and delivery["attempts"] == feishu.attempts == 1
