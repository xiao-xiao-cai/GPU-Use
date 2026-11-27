import json
from copy import deepcopy

from pwdlib import PasswordHash

from gpu_console import storage
from gpu_console.application.notifications import list_notifications
from gpu_console.worker.collection import Collector
from gpu_console.worker.outbox import Outbox
from test_api import console  # noqa: F401 - pytest fixture
from test_collection import PROJECT, all_cards, point, setup


def enable(path, rule, **updates):
    with storage.connect(path) as conn:
        settings = storage.get_settings(conn)
        rules = deepcopy(settings["notification_rules"])
        rules[rule].update(enabled=True, **updates)
        storage.set_settings(conn, {"notification_rules": rules})


def records(path, event_type):
    with storage.connect(path) as conn:
        return list_notifications(conn, limit=20, channel="site", project_id=PROJECT,
                                  event_type=event_type, event_id=None, cursor=None)["items"]


def memory(at, card, value):
    return point(at, f"GPU-{card}", value) | {"metric": "cloudwatch_gpu_memory_usage"}


def test_memory_reminders_require_same_card_streak_and_verified_recovery(tmp_path, monkeypatch):
    time, cloud, collector = setup(tmp_path, monkeypatch)
    enable(collector.path, "gpu_memory_high", duration_minutes=2, channels=["site", "feishu"])
    with storage.connect(collector.path) as conn:
        storage.set_settings(conn, {"notification_repeat_minutes": {"site": 1, "feishu": 0, "email": 0}})

    def poll(at, first, second=None):
        time[0] = at
        cloud.points = [memory(at, 1, first)]
        if second is not None:
            cloud.points.append(memory(at, 2, second))
        collector.tick()
        return records(collector.path, "gpu-memory-high")

    assert poll(1060, 99, 40) == []
    assert poll(1120, 40, 99) == []
    assert poll(1180, 99, 40) == []
    assert poll(1240, 99, 40) == []
    [initial] = poll(1300, 99, 40)
    assert initial["business"]["attention"] and "GPU-1 99%" in initial["payload"]["message"]
    collector = Collector(collector.path, cloud)
    repeat, original = poll(1360, 99, 40)
    assert repeat["event_id"].startswith(original["event_id"] + ":repeat:")
    assert len(poll(1420, 20)) == 2
    assert all(item["business"]["status"] == "resolved" for item in poll(1480, 20, 40))

    class NoSends:
        def send(self, *_args, **_kwargs):
            raise AssertionError("恢复以后不能发原高占用消息")

    Outbox(collector.path, NoSends()).tick()
    with storage.connect(collector.path) as conn:
        [state] = conn.execute("SELECT state FROM notifications WHERE channel='feishu'").fetchone()
    assert state == "skipped"


def test_monitoring_outage_requires_baseline_and_recovers_from_complete_samples(tmp_path, monkeypatch):
    time, cloud, collector = setup(tmp_path, monkeypatch)
    enable(collector.path, "monitoring")
    for sampled_at in range(1000, 1360, 60):
        time[0] = sampled_at
        collector.tick()
    assert records(collector.path, "monitoring-interrupted") == []
    time[0] = 1360
    cloud.points = all_cards(1360)
    collector.tick()
    cloud.monitor_error = True
    for sampled_at in range(1420, 1660, 60):
        time[0] = sampled_at
        collector.tick()
    assert records(collector.path, "monitoring-interrupted") == []
    time[0] = 1660
    collector.tick()
    [interrupted] = records(collector.path, "monitoring-interrupted")
    assert interrupted["payload"]["last_sample_at"] == 1360
    assert interrupted["business"]["attention"]

    time[0] = 1720
    cloud.monitor_error = False
    cloud.points = [point(1720, "GPU-1")]
    collector.tick()
    assert records(collector.path, "monitoring-restored") == []
    time[0] = 1780
    cloud.points = all_cards(1780)
    collector.tick()
    [restored] = records(collector.path, "monitoring-restored")
    assert restored["payload"]["confirmed_at"] == 1780
    assert records(collector.path, "monitoring-interrupted")[0]["business"]["status"] == "resolved"


def test_watch_notice_ignores_initial_available_and_unknown_data(tmp_path, monkeypatch):
    from gpu_console.application import watches
    from gpu_console.storage import availability
    from capacity_fixtures import record_capacity

    _, _, collector = setup(tmp_path, monkeypatch)
    combo = {"region": "cn-wlcb", "zone": "cn-wlcb-01", "gpu_model": "4090",
             "gpu": 2, "cpu": 16, "memory_gb": 64, "instance_type": "uhost"}
    with storage.connect(collector.path) as conn:
        conn.execute("INSERT INTO snapshots VALUES ('catalog','',?,1000,NULL)", (json.dumps([combo | {"specs": [combo]}]),))
        conn.execute("INSERT INTO watches VALUES (?,?,?,?)", ("watch", PROJECT, "4090", json.dumps(combo)))
        watches.sync_bindings(conn)
    enable(collector.path, "watch_stock")

    def observe(time, answer):
        count = 8
        monkeypatch.setattr(storage, "now", lambda: time)
        with storage.connect(collector.path) as conn, storage.transaction(conn):
            availability.record_pool(conn, "cn-wlcb", time, provider_updated_at=time if count is not None else None,
                counts={("cn-wlcb-01", "4090", "exclusive"): count} if count is not None else None)
            members = record_capacity(
                conn, PROJECT, ("cn-wlcb", "cn-wlcb-01", "4090", "uhost"), {(2, 16, 64): answer}, time,
            )
            watches.observe(conn, time, project_id=PROJECT, spec_ids=members)

    for time, answer in ((1000, True), (1300, False), (1600, None)):
        observe(time, answer)
        assert records(collector.path, "watch-stock") == []
    observe(1900, True)
    observe(1900, True)
    [item] = records(collector.path, "watch-stock")
    assert item["business"]["label"] == "规格当前可租"
    assert item["business"]["destination"] == {"page": "inventory", "project_id": PROJECT, "watch_id": "watch"}
    observe(2200, False)
    [item] = records(collector.path, "watch-stock")
    assert item["business"]["label"] == "规格当前不可租"


def test_idle_recovery_is_per_episode_and_honors_instance_pause(tmp_path, monkeypatch):
    time, cloud, collector = setup(tmp_path, monkeypatch)
    enable(collector.path, "idle_recovery")

    def poll(at, value=0):
        time[0] = at
        cloud.points = all_cards(at, value)
        collector.tick()

    for sampled_at in (1060, 1120, 1180):
        poll(sampled_at)
    with storage.connect(collector.path) as conn:
        conn.execute("INSERT INTO idle_snoozes VALUES (?,?,NULL)", (PROJECT, "uhost-test"))
    poll(1240, 50)
    assert records(collector.path, "idle-recovery") == []
    assert records(collector.path, "idle")[0]["business"]["status"] == "resolved"
    with storage.connect(collector.path) as conn:
        conn.execute("UPDATE idle_snoozes SET until=1240")
    for sampled_at in (1300, 1360, 1420):
        poll(sampled_at)
    poll(1480, 50)
    poll(1540, 50)
    [item] = records(collector.path, "idle-recovery")
    assert item["payload"]["confirmed_at"] == 1480
    assert item["business"]["status"] == "resolved"


def test_resource_notification_settings_validate_channel_choices_and_thresholds(console):
    client, config = console
    login = {"username": "operator", "password": "stub-password-12345"}
    with storage.connect(config.db) as conn:
        conn.execute("INSERT INTO administrators VALUES (1,?,?)", (login["username"], PasswordHash.recommended().hash(login["password"])))
    headers = {"x-csrf-token": client.post("/api/login", json=login).json()["csrf_token"]}
    rules = client.get("/api/settings").json()["notification_rules"]
    assert all(not rule["enabled"] for rule in rules.values() if isinstance(rule, dict))
    rules["monitoring"] = {"enabled": True, "channels": ["email"]}
    assert client.patch("/api/settings", headers=headers, json={"notification_rules": rules}).status_code == 422
    rules["email_recipient"] = "watcher@example.invalid"
    rules["gpu_memory_high"]["threshold"] = 5
    assert client.patch("/api/settings", headers=headers, json={"notification_rules": rules}).status_code == 422
    rules["gpu_memory_high"]["threshold"] = 90
    response = client.patch("/api/settings", headers=headers, json={"notification_rules": rules})
    assert response.status_code == 200 and response.json()["notification_rules"] == rules
