import json
from datetime import datetime

from pwdlib import PasswordHash

from gpu_console import storage
from gpu_console.adapters.notifications import DeliveryError
from gpu_console.application.notice_policies import in_idle_window
from gpu_console.worker.collection import Collector
from gpu_console.worker.outbox import Outbox
from test_api import console  # noqa: F401 - pytest fixture
from test_collection import PROJECT, all_cards, instance, setup


def beijing(value):
    return int(datetime.fromisoformat(value + "+08:00").timestamp())


def deliveries(path):
    with storage.connect(path) as conn:
        return list(conn.execute("SELECT * FROM notifications ORDER BY id"))


def test_idle_window_uses_beijing_time_and_fresh_post_open_sample(tmp_path, monkeypatch):
    time, cloud, collector = setup(tmp_path, monkeypatch)
    schedule = {"mode": "scheduled", "windows": [{"weekday": 7, "start": "23:00", "end": "01:00"}]}
    with storage.connect(collector.path) as conn:
        storage.set_settings(conn, {"idle_schedule": schedule, "collection_seconds": 15})
    assert not in_idle_window(schedule, beijing("2026-11-22T22:59:59"))
    assert in_idle_window(schedule, beijing("2026-11-23T00:59:59"))
    assert not in_idle_window(schedule, beijing("2026-11-23T01:00:00"))

    cloud.rows[0]["start_time"] = beijing("2026-11-22T22:50:00")
    for wall_time in ("22:57:50", "22:58:50", "22:59:50"):
        time[0] = beijing("2026-11-22T" + wall_time)
        cloud.points = all_cards(time[0])
        collector.tick()
    assert not deliveries(collector.path)
    time[0] = beijing("2026-11-22T23:00:05")
    collector.tick()
    assert not deliveries(collector.path)
    time[0] += 15
    cloud.points = all_cards(time[0])
    collector.tick()
    [notice] = deliveries(collector.path)
    assert json.loads(notice["payload"])["confirmed_at"] == time[0]


def test_recurring_idle_has_independent_persistent_channel_cadence(tmp_path, monkeypatch):
    time, cloud, collector = setup(tmp_path, monkeypatch)
    with storage.connect(collector.path) as conn:
        storage.set_settings(conn, {
            "idle_channels": ["site", "feishu", "email"], "idle_email_recipient": "stub@example.invalid",
            "notification_repeat_minutes": {"site": 1, "feishu": 2, "email": 0},
        })

    class Accepting:
        def send(self, channel, payload, *, event_id):
            return None

    outbox = Outbox(collector.path, Accepting())
    for sampled_at in (1060, 1120, 1180, 1240, 1300):
        if sampled_at == 1240:
            collector = Collector(collector.path, cloud)
        time[0] = sampled_at
        cloud.points = all_cards(time[0])
        collector.tick()
        outbox.tick()
        outbox.tick()

    rows = deliveries(collector.path)
    assert [(row["channel"], row["delivered_at"]) for row in rows] == [
        ("site", 1180), ("feishu", 1180), ("email", 1180), ("site", 1240), ("site", 1300), ("feishu", 1300),
    ]
    assert {row["event_id"].split(":repeat:")[0] for row in rows} == {f"idle:{PROJECT}:uhost-test:1000:1060"}


def test_pending_idle_retry_waits_while_snoozed_and_skips_after_confirmed_recovery(tmp_path, monkeypatch):
    time, cloud, collector = setup(tmp_path, monkeypatch)
    with storage.connect(collector.path) as conn:
        storage.set_settings(conn, {"idle_channels": ["feishu"]})

    failures = []

    class Unavailable:
        def send(self, channel, payload, *, event_id):
            failures.append(event_id)
            raise DeliveryError("测试投递失败", retryable=True)

    for sampled_at in (1060, 1120, 1180):
        time[0] = sampled_at
        cloud.points = all_cards(time[0])
        collector.tick()
    outbox = Outbox(collector.path, Unavailable())
    outbox.tick()
    with storage.connect(collector.path) as conn:
        conn.execute("INSERT INTO idle_snoozes VALUES (?,?,NULL)", (PROJECT, "uhost-test"))
    time[0] = 1210
    outbox.tick()
    [notice] = deliveries(collector.path)
    assert notice["state"] == "pending" and notice["attempts"] == 1
    assert "时段" in notice["error"]

    time[0] = 1240
    cloud.points = all_cards(time[0], 50)
    collector.tick()
    outbox.tick()
    [notice] = deliveries(collector.path)
    assert notice["state"] == "skipped" and notice["error"] == "原空闲状态已结束"
    assert len(failures) == 1


def test_idle_schedule_and_instance_snooze_api_are_validated_and_scoped(console, monkeypatch):
    client, config = console
    now = beijing("2026-11-23T09:00:00")
    monkeypatch.setattr(storage, "now", lambda: now)
    login = {"username": "operator", "password": "stub-password-12345"}
    with storage.connect(config.db) as conn:
        conn.execute("INSERT INTO administrators VALUES (1,?,?)", (login["username"], PasswordHash.recommended().hash(login["password"])))
        conn.execute("INSERT INTO snapshots VALUES ('instances',?,?,?,NULL)", (PROJECT, json.dumps([instance()]), now))
    headers = {"x-csrf-token": client.post("/api/login", json=login).json()["csrf_token"]}
    settings = client.get("/api/settings").json()
    assert settings["idle_schedule"] == {"mode": "all_day", "windows": []}
    assert settings["notification_repeat_minutes"] == {"site": 0, "feishu": 0, "email": 0}
    invalid = {"idle_schedule": {"mode": "scheduled", "windows": [{"weekday": 1, "start": "09:00", "end": "09:00"}]}}
    assert client.patch("/api/settings", json=invalid, headers=headers).status_code == 422
    invalid = {"notification_repeat_minutes": {"site": 0, "feishu": 30.5, "email": 0}}
    assert client.patch("/api/settings", json=invalid, headers=headers).status_code == 422

    detail = "/api/instances/uhost-test"
    params = {"project_id": PROJECT}
    endpoint = detail + "/idle-snooze"
    assert client.put(endpoint, params=params, json={"until": now}, headers=headers).status_code == 422
    wrong = client.put(endpoint, params={"project_id": "other"}, json={"until": None}, headers=headers)
    assert wrong.status_code in (404, 503)
    assert client.put(endpoint, params=params, json={"until": None}, headers=headers).json() == {"paused": True, "until": None}
    assert client.get(detail, params=params).json()["idle_snooze"]["paused"] is True
    assert client.delete(endpoint, params=params, headers=headers).json() == {"paused": False, "until": None}
    assert client.get(detail, params=params).json()["idle_snooze"]["paused"] is False
