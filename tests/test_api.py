import json
from dataclasses import replace
from stat import S_IMODE
from time import time

import httpx
import pytest
from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from gpu_console import storage
from gpu_console.adapters.notifications import Channels
from gpu_console.api import create_app
from gpu_console.application.tasks import TaskStore, insert_run
from gpu_console.config import Config
from gpu_console.storage.feishu import FeishuAppSettings, FeishuSettings, read_feishu_settings
from gpu_console.worker.outbox import Outbox


@pytest.fixture
def console(tmp_path):
    config = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    config.static_dir.mkdir()
    (config.static_dir / "index.html").write_text("<h1>GPU 管理台</h1>")
    with TestClient(create_app(config)) as client:
        yield client, config


def test_admin_session_and_csrf_guard(console):
    client, config = console
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
    assert client.get("/api/session").json() == {"initialized": False, "authenticated": False}
    assert client.get("/api/projects").status_code == 401
    body = {"username": "operator", "password": "stub-password-12345"}
    assert client.post("/api/login", json=body).status_code == 409
    with storage.connect(config.db) as conn:
        conn.execute(
            "INSERT INTO administrators VALUES (1,?,?)",
            (body["username"], PasswordHash.recommended().hash(body["password"])),
        )
        conn.execute("INSERT INTO snapshots VALUES ('projects','',?,42,NULL)", (json.dumps([{"id": "proj"}]),))
    assert client.post("/api/login", json=body, headers={"Origin": "http://other.invalid"}).status_code == 403
    login = client.post("/api/login", json=body)
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=strict" in login.headers["set-cookie"]
    headers = {"x-csrf-token": login.json()["csrf_token"]}
    assert client.get("/api/projects").json() == {"data": [{"id": "proj"}], "success_at": 42, "error": None}
    assert client.get("/api/projects").headers["cache-control"] == "no-store"
    request = {"kind": "instances", "project_id": "proj"}
    assert client.post("/api/refresh", json=request).status_code == 403
    for _ in range(2):
        assert client.post("/api/refresh", json=request, headers=headers).status_code == 202
    with storage.connect(config.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM refresh_requests").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert client.post("/api/logout", headers=headers).status_code == 200
    assert client.get("/api/projects").status_code == 401


def test_web_task_lists_and_recoverable_deletion(console):
    from gpu_console.api.auth import COOKIE, digest

    client, config = console
    now = storage.now()
    with storage.connect(config.db) as conn:
        conn.execute("INSERT INTO sessions VALUES (?,?,?)", (digest("task-browser"), "csrf", now + 60))
    client.cookies.set(COOKIE, "task-browser")
    headers = {"x-csrf-token": "csrf"}
    task = client.post("/api/tasks", headers=headers, json={
        "project_id": "p1", "name": "待清理的巡检", "kind": "inspection",
        "schedule": {"type": "daily", "at": now + 600}, "config": {},
    }).json()
    base = f"/api/tasks/{task['id']}"
    assert client.get("/api/tasks?project_id=p1&view=current").json() == [task]
    run = client.post(base + "/trigger", json={"request_id": "test"}, headers=headers).json()
    body = {"project_id": "p1", "expected_version": task["version"]}
    assert client.post(base + "/trash", json=body).status_code == 403
    assert client.post(base + "/trash", json=body | {"project_id": "p2"}, headers=headers).status_code == 404
    response = client.post(base + "/trash", json=body, headers=headers)
    assert response.status_code == 200
    removed = response.json()
    assert client.get("/api/tasks?project_id=p1").json() == []
    assert client.get("/api/tasks?project_id=p1&view=trash").json() == [removed]
    assert client.post(base + "/resume", headers=headers).status_code == 409
    restored = client.post(base + "/trash/restore", json=body | {"expected_version": removed["version"]}, headers=headers)
    assert restored.status_code == 200 and restored.json()["status"] == "paused"
    removed = client.post(
        base + "/trash", json=body | {"expected_version": restored.json()["version"]}, headers=headers,
    ).json()
    assert client.post(base + "/trash/purge", json=body, headers=headers).status_code == 409
    reply = client.post(base + "/trash/purge", json=body | {"expected_version": removed["version"]}, headers=headers)
    assert reply.json() == {"id": task["id"], "project_id": "p1", "purged": True}
    assert client.get(base).status_code == 404
    assert [row["id"] for row in client.get(f"/api/runs?project_id=p1&task_id={task['id']}").json()] == [run["id"]]


def test_feishu_settings_keep_credentials_private_and_apply_to_running_worker(console, monkeypatch):
    client, config = console
    login = {"username": "operator", "password": "stub-password-12345"}
    settings_path = "/api/settings/feishu"
    test_path = "/api/notifications/test"
    test_body = {"channel": "feishu"}
    secret = "virtual-signing-secret"
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/virtual-webhook-token"
    legacy_webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/legacy-webhook-token"
    body = {"webhook_url": webhook, "signing_secret": secret, "keyword": "GPU 提醒"}
    config.secrets_dir.mkdir()
    (config.secrets_dir / "feishu-settings").mkdir(mode=0o700)
    with storage.connect(config.db) as conn:
        conn.execute(
            "INSERT INTO administrators VALUES (1,?,?)",
            (login["username"], PasswordHash.recommended().hash(login["password"])),
        )

    calls = []

    def send(url, *, json, timeout):
        calls.append((str(url), json))
        return httpx.Response(200, json={"code": 0})

    monkeypatch.setattr(httpx, "post", send)
    assert client.put(settings_path, json=body).status_code == 401
    headers = {"x-csrf-token": client.post("/api/login", json=login).json()["csrf_token"]}
    assert client.put(settings_path, json=body).status_code == 403
    assert client.get("/api/settings").json()["feishu"] is None
    assert client.post(test_path, json=test_body, headers=headers).status_code >= 400

    (config.secrets_dir / "feishu_webhook_url").write_text(legacy_webhook)
    (config.secrets_dir / "feishu_sign_secret").write_text("legacy-signing-secret")
    channels = Channels(config.secrets_dir)
    outbox = Outbox(config.db, channels)
    with storage.connect(config.db) as conn:
        storage.record_status(conn, "channel_feishu", True, "configured")
    assert client.get("/api/settings").json()["channels"]["feishu"] is True
    assert client.post(test_path, json=test_body, headers=headers).status_code == 202
    outbox.tick()
    assert calls[0][0] == legacy_webhook

    for invalid in (
        {"webhook_url": "https://other.invalid/" + secret},
        {"webhook_url": webhook + "/extra"},
        {"webhook_url": webhook + "?token=" + secret},
        {"webhook_url": webhook.replace("https://", "https://user@")},
        {"signing_secret": {"value": secret}},
    ):
        response = client.put(settings_path, json=body | invalid, headers=headers)
        assert response.status_code == 422
        assert secret not in response.text and "virtual-webhook-token" not in response.text

    saved = client.put(settings_path, json=body, headers=headers)
    assert saved.status_code == 200
    assert saved.json()["feishu"] == {"signing_configured": True, "keyword": "GPU 提醒"}
    assert saved.json()["channels"]["feishu"] is True
    assert secret not in saved.text and "virtual-webhook-token" not in saved.text
    config_file, = (config.secrets_dir / "feishu-settings").glob("*.json")
    assert S_IMODE(config_file.stat().st_mode) == 0o600

    changed = client.put(settings_path, json={"keyword": "训练告警"}, headers=headers)
    assert changed.status_code == 200
    assert changed.json()["feishu"] == {"signing_configured": True, "keyword": "训练告警"}
    queued = client.post(test_path, json=test_body, headers=headers)
    assert queued.status_code == 202
    outbox.tick()
    target, message = calls[-1]
    assert target == webhook and message["sign"]
    assert message["content"]["text"].startswith("训练告警\n")
    receipt = client.get("/api/notifications", params={"event_id": queued.json()["event_id"]})
    assert receipt.json()["items"][0]["state"] == "delivered"
    assert secret not in receipt.text and "virtual-webhook-token" not in receipt.text

    unsigned = client.put(settings_path, json={"signing_secret": None, "keyword": ""}, headers=headers)
    assert unsigned.status_code == 200
    with TestClient(create_app(config), cookies=client.cookies) as restarted:
        assert restarted.get("/api/settings").json()["feishu"] == {"signing_configured": False, "keyword": ""}
    assert client.post(test_path, json=test_body, headers=headers).status_code == 202
    outbox.tick()
    target, message = calls[-1]
    assert target == webhook and "sign" not in message and "timestamp" not in message

    config_file.write_text("{")
    settings = client.get("/api/settings").json()
    assert settings["feishu"] is None and not settings["channels"]["feishu"]
    assert client.post(test_path, json=test_body, headers=headers).status_code >= 400
    assert client.put(settings_path, json=body, headers=headers).status_code == 200


def test_feishu_app_settings_switch_modes_and_keep_the_secret_write_only(console):
    client, config = console
    settings_path = "/api/settings/feishu"
    login = {"username": "operator", "password": "stub-password-12345"}
    public = {"mode": "app", "app_id": "cli_demo123", "receive_id_type": "email", "receive_id": "stub@example.test"}
    secret = "virtual-feishu-app-secret"
    body = public | {"app_secret": secret}
    with storage.connect(config.db) as conn:
        conn.execute(
            "INSERT INTO administrators VALUES (1,?,?)",
            (login["username"], PasswordHash.recommended().hash(login["password"])),
        )
    assert client.put(settings_path, json=body).status_code == 401
    response = client.post("/api/login", json=login)
    headers = {"x-csrf-token": response.json()["csrf_token"]}
    assert client.put(settings_path, json=body).status_code == 403

    webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/virtual-webhook"
    assert client.put(settings_path, json={"keyword": "GPU", "webhook_url": webhook_url}, headers=headers).is_success
    response = client.put(settings_path, json=public, headers=headers)
    assert response.status_code == 422
    assert read_feishu_settings(config.secrets_dir) == FeishuSettings(webhook_url, keyword="GPU")
    for changes in (
        {"app_id": "invalid"},
        {"app_secret": [secret]},
        {"receive_id": "invalid"},
        {"receive_id_type": "open_id"},
        {"receive_id_type": "user_id"},
    ):
        invalid = client.put(settings_path, json=body | changes, headers=headers)
        assert invalid.status_code == 422
        assert secret not in invalid.text

    saved = client.put(settings_path, json=body, headers=headers)
    assert saved.status_code == 200
    assert saved.json()["feishu"] == public
    assert saved.json()["channels"]["feishu"] is True
    assert secret not in saved.text and "app_secret" not in saved.text
    assert read_feishu_settings(config.secrets_dir) == FeishuAppSettings(
        public["app_id"], secret, "email", public["receive_id"],
    )
    config_file, = (config.secrets_dir / "feishu-settings").glob("*.json")
    assert S_IMODE(config_file.stat().st_mode) == 0o600

    changed = public | {"receive_id_type": "chat_id", "receive_id": "oc_demo123"}
    assert client.put(settings_path, json=changed, headers=headers).json()["feishu"] == changed
    assert client.put(settings_path, json=changed | {"app_id": "cli_demo456"}, headers=headers).status_code == 422
    assert read_feishu_settings(config.secrets_dir) == FeishuAppSettings(changed["app_id"], secret, "chat_id", "oc_demo123")
    with TestClient(create_app(config), cookies=client.cookies) as restarted:
        response = restarted.get("/api/settings")
        assert response.json()["feishu"] == changed
        assert secret not in response.text
        assert restarted.post("/api/notifications/test", json={"channel": "feishu"}, headers=headers).status_code == 202

    webhook = {"mode": "webhook", "keyword": ""}
    assert client.put(settings_path, json=webhook, headers=headers).status_code == 422
    reverted = client.put(settings_path, json=webhook | {"webhook_url": webhook_url}, headers=headers)
    assert reverted.json()["feishu"] == {"keyword": "", "signing_configured": False}
    assert read_feishu_settings(config.secrets_dir) == FeishuSettings(webhook_url)


def test_instance_operation_api_preserves_scope_and_instance_history(console):
    client, config = console
    login = {"username": "operator", "password": "stub-password-12345"}
    row = {"id": "instance-1", "name": "原名称", "region": "r", "zone": "z"}
    with storage.connect(config.db) as conn:
        conn.execute(
            "INSERT INTO administrators VALUES (1,?,?)",
            (login["username"], PasswordHash.recommended().hash(login["password"])),
        )
        for project in ("p1", "p2"):
            conn.execute(
                "INSERT INTO snapshots VALUES ('instances',?,?,42,NULL)",
                (project, json.dumps([row | {"project_id": project}])),
            )
    headers = {"x-csrf-token": client.post("/api/login", json=login).json()["csrf_token"]}
    reboot = {"project_id": "p1", "instance_id": row["id"], "kind": "reboot", "request_id": "reboot-1"}
    rename = reboot | {"project_id": "p2", "kind": "rename", "request_id": "rename-1", "expected_name": "原名称"}

    assert client.post("/api/actions", json=reboot).status_code == 403
    assert client.post("/api/actions", json=reboot | {"project_id": "missing"}, headers=headers).status_code == 409
    assert client.post("/api/actions", json=rename | {"name": "<script>"}, headers=headers).status_code == 422
    assert client.post("/api/actions", json=rename | {"name": "新名称", "expected_name": None}, headers=headers).status_code == 422
    first = client.post("/api/actions", json=reboot, headers=headers)
    assert first.status_code == 202 and first.json()["state"] == "queued"
    assert client.post("/api/actions", json=reboot, headers=headers).json()["id"] == first.json()["id"]
    second = client.post("/api/actions", json=rename | {"name": "新名称"}, headers=headers)
    assert second.status_code == 202 and second.json()["snapshot"]["config"]["name"] == "新名称"

    assert client.get("/api/runs", params={"instance_id": row["id"]}).status_code == 422
    history = client.get("/api/runs", params={"project_id": "p1", "instance_id": row["id"]})
    assert [run["id"] for run in history.json()] == [first.json()["id"]]
    assert client.get("/api/runs", params={"project_id": "p1", "instance_id": "another"}).json() == []


def test_scheduled_start_capacity_policy_api_and_run_cancellation(console):
    client, config = console
    login = {"username": "operator", "password": "stub-password-12345"}
    with storage.connect(config.db) as conn:
        conn.execute(
            "INSERT INTO administrators VALUES (1,?,?)",
            (login["username"], PasswordHash.recommended().hash(login["password"])),
        )
    headers = {"x-csrf-token": client.post("/api/login", json=login).json()["csrf_token"]}
    target = {"instance_id": "instance-1", "region": "r", "zone": "z"}
    retry = {"interval_seconds": 60}
    body = {
        "name": "每日开机", "project_id": "p1", "kind": "start",
        "schedule": {"type": "daily", "at": int(time())}, "config": target | {"capacity_retry": retry},
    }
    invalid = body | {"config": target | {"capacity_retry": {"interval_seconds": 29}}}
    assert client.post("/api/tasks/preview", json=invalid, headers=headers).status_code == 422
    assert client.post("/api/tasks", json=body | {"kind": "stop"}, headers=headers).status_code == 422

    saved = client.post("/api/tasks", json=body, headers=headers)
    assert saved.status_code == 201 and saved.json()["config"]["capacity_retry"] == retry
    assert TaskStore(config.db).enqueue_due() == 1
    run, = client.get("/api/runs", params={"task_id": saved.json()["id"]}).json()
    assert run["snapshot"]["config"]["capacity_retry"] == retry
    canceled = client.post(f"/api/runs/{run['id']}/cancel", headers=headers)
    assert canceled.status_code == 200 and canceled.json()["state"] == "canceled"
    assert client.get(f"/api/tasks/{saved.json()['id']}").json()["status"] == "active"


def test_cancel_shutdown_warning_can_be_dismissed_and_restored(console):
    client, config = console
    login = {"username": "operator", "password": "stub-password-12345"}
    at = 1_000
    with storage.connect(config.db) as conn:
        conn.execute(
            "INSERT INTO administrators VALUES (1,?,?)",
            (login["username"], PasswordHash.recommended().hash(login["password"])),
        )
        runs = {}
        for key, project, kind, state in (
            ("target", "p1", "cancel_shutdown", "manual_review"),
            ("other_project", "p2", "cancel_shutdown", "manual_review"),
            ("other_kind", "p1", "acquire", "manual_review"),
            ("other_state", "p1", "cancel_shutdown", "succeeded"),
        ):
            snapshot = {
                "project_id": project, "name": kind, "kind": kind, "window_seconds": 1800,
                "config": {"instance_id": "instance-1"},
            }
            runs[key] = insert_run(conn, snapshot, at, at, state=state, reason="存档回执")["id"]
        conn.execute(
            "UPDATE task_runs SET phase='verify_cancel_shutdown', result=? WHERE id=?",
            (json.dumps({"shutdown_at": at + 300, "cloud_shutdown_at": None}), runs["target"]),
        )
        conn.execute(
            """INSERT INTO task_operations(run_id,project_id,action,state,data,created_at,updated_at)
               VALUES (?,'p1','cancel_shutdown','accepted','{}',?,?)""",
            (runs["target"], at, at),
        )

    path = "/api/runs/" + runs["target"]
    body = {"dismissed": True}
    assert client.patch(path + "/warning", json=body).status_code == 401
    headers = {"x-csrf-token": client.post("/api/login", json=login).json()["csrf_token"]}
    before = client.get(path).json()
    assert before["warning_dismissed"] is False
    assert client.patch(path + "/warning", json=body).status_code == 403
    assert client.patch(path + "/warning", json={"dismissed": "true"}, headers=headers).status_code == 422
    assert client.patch("/api/runs/absent/warning", json=body, headers=headers).status_code == 404
    for key in ("other_kind", "other_state"):
        response = client.patch(f"/api/runs/{runs[key]}/warning", json=body, headers=headers)
        assert response.status_code == 422

    for _ in range(2):
        response = client.patch(path + "/warning", json=body, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"id": runs["target"], "warning_dismissed": True}

    with TestClient(create_app(config), cookies=client.cookies) as restarted:
        assert restarted.get(path).json() == before | {"warning_dismissed": True}
        for project, expected in (("p1", {runs["target"]}), ("p2", set())):
            response = restarted.get("/api/runs", params={"project_id": project, "instance_id": "instance-1"})
            assert response.status_code == 200
            assert {run["id"] for run in response.json() if run["warning_dismissed"]} == expected
        for _ in range(2):
            response = restarted.patch(path + "/warning", json={"dismissed": False}, headers=headers)
            assert response.status_code == 200
            assert response.json() == {"id": runs["target"], "warning_dismissed": False}
        assert restarted.get(path).json() == before


def test_cancel_shutdown_review_queues_readback_once(console, monkeypatch):
    client, config = console
    login = {"username": "operator", "password": "stub-password-12345"}
    at, queued_at = 1_000, 4_000
    with storage.connect(config.db) as conn:
        conn.execute(
            "INSERT INTO administrators VALUES (1,?,?)",
            (login["username"], PasswordHash.recommended().hash(login["password"])),
        )
        runs = {}
        for key, project, kind, state in (
            ("target", "p1", "cancel_shutdown", "manual_review"),
            ("other_project", "p2", "cancel_shutdown", "manual_review"),
            ("other_kind", "p1", "acquire", "manual_review"),
            ("other_state", "p1", "cancel_shutdown", "succeeded"),
        ):
            snapshot = {
                "project_id": project, "name": kind, "kind": kind, "window_seconds": 1800,
                "config": {"instance_id": "instance-1", "expected_stop_at": at + 300},
            }
            runs[key] = insert_run(conn, snapshot, at, at, state=state, reason="原预约撤销结果待核对")["id"]
        conn.execute(
            "UPDATE task_runs SET phase='verify_cancel_shutdown', result=? WHERE id=?",
            (json.dumps({"shutdown_at": at + 300, "cloud_shutdown_at": None}), runs["target"]),
        )
        conn.execute(
            """INSERT INTO task_operations(run_id,project_id,action,state,data,created_at,updated_at)
               VALUES (?,'p1','cancel_shutdown','accepted','{}',?,?)""",
            (runs["target"], at, at),
        )

    path = "/api/runs/" + runs["target"]
    review_path = path + "/review-shutdown"
    assert client.post(review_path).status_code == 401
    headers = {"x-csrf-token": client.post("/api/login", json=login).json()["csrf_token"]}
    assert client.post(review_path).status_code == 403
    assert client.post("/api/runs/absent/review-shutdown", headers=headers).status_code == 404
    for key in ("other_kind", "other_state"):
        assert client.post(f"/api/runs/{runs[key]}/review-shutdown", headers=headers).status_code == 422
    assert client.patch(path + "/warning", json={"dismissed": True}, headers=headers).status_code == 200
    before = client.get(path).json()
    assert queued_at > before["deadline_at"]
    other_path = "/api/runs/" + runs["other_project"]
    other_before = client.get(other_path).json()

    monkeypatch.setattr(TaskStore, "now", lambda self: queued_at)
    response = client.post(review_path, headers=headers)
    assert response.status_code == 202
    changes = {
        "state": "reconciling", "phase": "review_cancel_shutdown", "reason": "正在核对云端关机预约",
        "next_check": queued_at, "updated_at": queued_at,
    }
    accepted = response.json()
    assert accepted == {key: value for key, value in (before | changes).items()
                        if key not in {"operations", "audit", "warning_dismissed"}}
    monkeypatch.setattr(TaskStore, "now", lambda self: queued_at + 5)
    replay = client.post(review_path, headers=headers)
    assert replay.status_code == 202 and replay.json() == accepted
    queued = client.get(path).json()
    assert queued == before | changes | {"audit": [{
        "id": queued["audit"][0]["id"], "run_id": runs["target"], "created_at": queued_at,
        "action": "review_cancel_shutdown",
        "data": {"original_reason": before["reason"], "original_phase": before["phase"]},
    }]}
    assert client.get(other_path).json() == other_before
    with storage.connect(config.db) as conn:
        conn.execute("UPDATE task_runs SET state='reviewed',phase=NULL WHERE id=?", (runs["target"],))
    replay = client.post(review_path, headers=headers)
    assert replay.status_code == 202 and replay.json()["state"] == "reviewed"
    assert client.get(path).json()["audit"] == queued["audit"]


def test_notification_filters_cursor_and_current_result(console):
    client, config = console
    login = {"username": "operator", "password": "stub-password-12345"}
    original = {"title": "取消预约", "message": "原通知仍待人工核对", "project_id": "p1"}
    with storage.connect(config.db) as conn, storage.transaction(conn):
        conn.execute(
            "INSERT INTO administrators VALUES (1,?,?)",
            (login["username"], PasswordHash.recommended().hash(login["password"])),
        )
        run = insert_run(conn, {
            "project_id": "p1", "name": "取消预约", "kind": "cancel_shutdown", "window_seconds": 1800,
            "config": {"instance_id": "instance-1"},
        }, 1000, 1000, state="manual_review", reason="需要核对")
        payload = original | {"run_id": run["id"]}
        event_id = run["id"] + ":review"
        storage.emit(conn, event_id, payload, ["site", "email", "feishu"])
        storage.emit(conn, "unknown:review", original | {"run_id": "missing"}, ["site"])
        storage.emit(conn, "test-manual", {"title": "测试"}, ["site"])
        storage.emit(conn, "legacy:review", payload | {"project_id": "p2"}, ["site"])
        conn.execute(
            "INSERT INTO snapshots VALUES ('instances','p1',?,1000,NULL)",
            (json.dumps([{"id": "instance-1", "project_id": "p1"}]),),
        )
        storage.emit(conn, "idle:p1:instance-1:1000:1001", {
            "title": "空闲", "instance_id": "instance-1", "project_id": "p1", "run_id": run["id"],
        }, ["site"])
    path = "/api/notifications"
    assert client.get(path).status_code == 401
    headers = {"x-csrf-token": client.post("/api/login", json=login).json()["csrf_token"]}
    assert isinstance(client.get(path, params={"limit": 1}).json(), list)
    params = {"channel": "site", "project_id": "p1", "event_type": "review", "limit": 1}
    first = client.get(path, params=params).json()
    [item] = first["items"]
    assert item["payload"]["run_id"] == "missing"
    assert item["business"]["destination"] is None
    assert first["next_cursor"] == item["id"]
    # A newly inserted record cannot move the boundary of the existing page.
    with storage.connect(config.db) as conn:
        storage.emit(conn, "new:review", original, ["site"])
    second = client.get(path, params=params | {"cursor": first["next_cursor"]}).json()
    [item] = second["items"]
    assert second["next_cursor"] is None
    assert item["id"] < first["next_cursor"] and item["payload"] == payload
    assert item["business"]["attention"] is True
    assert item["business"]["destination"] == {"page": "tasks", "project_id": "p1", "run_id": run["id"]}
    assert client.post(f"{path}/{item['id']}/read", headers=headers).json() == {"read": True}
    site = {"event_id": event_id, "channel": "site"}
    [read_item] = client.get(path, params=site).json()["items"]
    assert read_item["read_at"] is not None and read_item["business"] == item["business"]
    with storage.connect(config.db) as conn:
        conn.execute("UPDATE task_runs SET state='reviewed',reason='当前无预约',result=? WHERE id=?", (
            json.dumps({"shutdown_review": {"checked_at": 2000, "outcome": "absent"}}), run["id"],
        ))
    [reviewed] = client.get(path, params=site).json()["items"]
    assert reviewed["payload"] == item["payload"] and reviewed["read_at"] == read_item["read_at"]
    assert reviewed["business"] == item["business"] | {
        "status": "resolved", "label": "已核对", "attention": False, "detail": "当前无预约", "observed_at": 2000,
    }
    external = client.get(path, params={"channel": "external", "event_id": event_id}).json()
    assert {row["channel"] for row in external["items"]} == {"email", "feishu"}
    assert all("business" not in row for row in external["items"])
    [mismatched] = client.get(path, params={"project_id": "p2"}).json()["items"]
    assert mismatched["business"]["destination"] is None
    [idle] = client.get(path, params={"event_type": "idle"}).json()["items"]
    assert idle["business"]["destination"] == {"page": "instances", "project_id": "p1", "instance_id": "instance-1"}


def test_run_attention_filters_before_limit_and_ignores_read_dismissal(console):
    client, config = console
    login = {"username": "operator", "password": "stub-password-12345"}
    base = {"project_id": "p1", "name": "核对用例", "window_seconds": 1800, "config": {}}
    with storage.connect(config.db) as conn, storage.transaction(conn):
        conn.execute(
            "INSERT INTO administrators VALUES (1,?,?)",
            (login["username"], PasswordHash.recommended().hash(login["password"])),
        )
        target = insert_run(conn, base | {"kind": "cancel_shutdown"}, 100, 100, state="manual_review")["id"]
        conn.execute("INSERT INTO task_run_warning_dismissals VALUES (?)", (target,))
        for index in range(201):
            insert_run(conn, base | {"kind": "start"}, 1000 + index, 1000 + index, state="succeeded")
        insert_run(conn, base | {"kind": "stop", "project_id": "p2"}, 100, 100, state="manual_review")
        pending = insert_run(conn, base | {"kind": "acquire"}, 200, 200, state="reconciling")["id"]
        conn.execute("UPDATE task_runs SET phase='verify_create',result=? WHERE id=?", (
            json.dumps({"reviewable": True}), pending,
        ))
        missed = insert_run(conn, base | {"kind": "shutdown"}, 900, 300, state="succeeded")["id"]
        conn.execute("UPDATE task_runs SET result=? WHERE id=?", (json.dumps({"shutdown_outcome": "missed"}), missed))
    client.post("/api/login", json=login)
    assert client.get("/api/runs", params={"attention": True}).status_code == 422
    ordinary = client.get("/api/runs", params={"project_id": "p1"}).json()
    assert len(ordinary) == 200 and target not in {run["id"] for run in ordinary}
    queue = {"project_id": "p1", "attention": True}
    attention = client.get("/api/runs", params=queue).json()
    assert [run["id"] for run in attention] == [missed, pending, target]
    assert all(run["business"]["attention"] for run in attention)
    assert attention[-1]["warning_dismissed"] is True
    with storage.connect(config.db) as conn:
        conn.execute("UPDATE task_runs SET state='reviewed' WHERE id=?", (target,))
    assert [run["id"] for run in client.get("/api/runs", params=queue).json()] == [missed, pending]


def test_procurement_release_requires_order_confirmation_and_audit_note(console):
    client, config = console
    login = {"username": "operator", "password": "stub-password-12345"}
    with storage.connect(config.db) as conn, storage.transaction(conn):
        conn.execute(
            "INSERT INTO administrators VALUES (1,?,?)",
            (login["username"], PasswordHash.recommended().hash(login["password"])),
        )
        run_id = insert_run(conn, {
            "project_id": "p1", "name": "采购待核对", "kind": "acquire", "window_seconds": 1800, "config": {},
        }, 1000, 1000, state="reconciling")["id"]
        conn.execute("UPDATE task_runs SET phase='verify_create',result=? WHERE id=?", (
            json.dumps({"reviewable": True}), run_id,
        ))
        conn.execute(
            """INSERT INTO task_operations(run_id,project_id,action,state,data,created_at,updated_at)
               VALUES (?,'p1','create','reserved','{}',1000,1000)""", (run_id,),
        )
        storage.emit(conn, run_id + ":review", {"run_id": run_id, "project_id": "p1", "message": "结果待核对"}, ["site"])
    headers = {"x-csrf-token": client.post("/api/login", json=login).json()["csrf_token"]}
    path = f"/api/runs/{run_id}"
    queue = {"project_id": "p1", "attention": True}
    assert [run["id"] for run in client.get("/api/runs", params=queue).json()] == [run_id]
    for fields in ({"note": "未成交"}, {"confirmed_no_order": True},
                   {"confirmed_no_order": True, "note": "  "}, {"confirmed_no_order": "true", "note": "未成交"}):
        response = client.post(path + "/reconcile", json={"action": "release", **fields}, headers=headers)
        assert response.status_code == 422
    assert client.get(path).json()["operations"][0]["state"] == "reserved"
    body = {"action": "release", "confirmed_no_order": True, "note": " 已核对平台订单，无对应成交记录。 "}
    response = client.post(path + "/reconcile", json=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["state"] == "failed"
    assert response.json()["result"]["purchase_review"]["outcome"] == "not_purchased"
    released = client.get(path).json()
    assert released["operations"][0]["state"] == "released"
    assert released["audit"][-1]["data"] == {
        "instance_id": None, "note": body["note"].strip(), "confirmed_no_order": True,
    }
    assert client.get("/api/runs", params=queue).json() == []
    [notice] = client.get("/api/notifications", params={"channel": "site"}).json()["items"]
    assert notice["payload"]["message"] == "结果待核对"
    assert notice["business"]["status"] == "resolved"


@pytest.mark.parametrize("gate", ["read_only", "restore_gate"])
def test_cloud_action_admission_gate_keeps_existing_replay(console, gate):
    client, config = console
    login = {"username": "operator", "password": "stub-password-12345"}
    row = {"id": "instance-1", "name": "原名称", "region": "r", "zone": "z"}
    with storage.connect(config.db) as conn:
        conn.execute(
            "INSERT INTO administrators VALUES (1,?,?)",
            (login["username"], PasswordHash.recommended().hash(login["password"])),
        )
        conn.execute(
            "INSERT INTO snapshots VALUES ('instances','p1',?,42,NULL)",
            (json.dumps([row | {"project_id": "p1"}]),),
        )
    headers = {"x-csrf-token": client.post("/api/login", json=login).json()["csrf_token"]}
    existing = {"project_id": "p1", "instance_id": row["id"], "kind": "stop", "request_id": "original"}
    request = client.post("/api/actions", json=existing, headers=headers)
    assert request.status_code == 202
    if gate == "read_only":
        client.app.state.config = replace(config, read_only=True)
    with storage.connect(config.db) as conn:
        if gate == "restore_gate":
            storage.set_settings(conn, {"restore_gate": True})
        conn.execute("DELETE FROM snapshots WHERE kind='instances'")

    for kind in ("start", "stop", "shutdown", "cancel_shutdown", "reboot", "rename"):
        new = existing | {"kind": kind, "request_id": "new-" + kind, "stop_at": int(time()) + 600,
                          "name": "新名称", "expected_name": "原名称"}
        rejected = client.post("/api/actions", json=new, headers=headers)
        assert rejected.status_code == 409 and "只读或恢复核对" in rejected.json()["detail"]
    replay = client.post("/api/actions", json=existing, headers=headers)
    assert replay.status_code == 202 and replay.json()["id"] == request.json()["id"]
    assert client.post("/api/actions", json=existing | {"kind": "start"}, headers=headers).status_code == 422
    with storage.connect(config.db) as conn:
        assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 1
