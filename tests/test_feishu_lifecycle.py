import json
from time import time

import httpx
import pytest
from fastapi.testclient import TestClient

from gpu_console import storage
from gpu_console.adapters.notifications import Channels
from gpu_console.api import create_app
from gpu_console.api.auth import COOKIE, digest
from gpu_console.api.tasks import task_store
from gpu_console.application.feishu_notices import DEFAULT_POLICY, emit
from gpu_console.application.tasks import TaskStore
from gpu_console.config import Config
from gpu_console.mcp import auth
from gpu_console.mcp.server import create_mcp_app
from gpu_console.storage import maintenance
from gpu_console.storage.feishu import save_feishu_settings
from gpu_console.worker.outbox import Outbox
from gpu_console.worker.tasks import TaskRunner


SCOPE = {"instance_id": "gpu-fixture", "region": "region1", "zone": "zone1"}
PROJECT = "project-fixture"


class Cloud:
    last_request_id = "request-fixture"

    def __init__(self):
        self.state = "Stopped"
        self.writes = []

    def instances(self, project_id, zones):
        return [SCOPE | {"id": SCOPE["instance_id"], "project_id": project_id, "state": self.state,
                         "gpu": 1, "gpu_mode": "with_gpu" if self.state == "Running" else None}]

    def start(self, instance):
        self.writes.append("start")

    def stop(self, instance):
        self.writes.append("stop")


def deliveries(db):
    with storage.connect(db) as conn:
        return [dict(row) | {"payload": json.loads(row["payload"])} for row in conn.execute(
            "SELECT * FROM notifications WHERE channel='feishu' ORDER BY id",
        )]


@pytest.fixture
def console(tmp_path, monkeypatch):
    now = [int(time())]
    monkeypatch.setattr(storage, "now", lambda: now[0])
    config = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    (config.secrets_dir / "feishu-settings").mkdir(parents=True)
    save_feishu_settings(config.secrets_dir, {
        "mode": "app", "app_id": "cli_fixture", "app_secret": "secret-fixture", "receive_id_type": "open_id",
        "receive_id": "ou_fixture",
    })
    origin = f"http://{config.mcp_bind_ip}:{config.mcp_bind_port}"
    cloud = Cloud()
    channels = Channels(config.secrets_dir, console_origin=origin)
    store = TaskStore(config.db, lambda: now[0], feishu_configured=lambda _conn: channels.configured("feishu"))
    app = create_app(config)
    app.dependency_overrides[task_store] = lambda: store
    with (
        TestClient(app, base_url=origin) as client,
        TestClient(create_mcp_app(config), base_url=origin) as mcp_client,
    ):
        with storage.connect(config.db) as conn:
            conn.execute("INSERT INTO sessions VALUES (?,?,?)", (digest("fixture-browser"), "fixture-csrf", now[0] + 3600))
            conn.execute("INSERT INTO snapshots VALUES ('projects','',?,?,NULL)", (
                json.dumps([{"id": PROJECT}]), now[0],
            ))
            conn.execute("INSERT INTO snapshots VALUES ('instances',?,?,?,NULL)", (
                PROJECT, json.dumps(cloud.instances(PROJECT, [])), now[0],
            ))
            token = auth.issue(conn, name="Test client", manage=True, ssh=True, days=None)["token"]
        client.cookies.set(COOKIE, "fixture-browser")
        yield client, store, TaskRunner(store, cloud), cloud, now, channels, token, mcp_client


def mcp(client, token, name, arguments):
    response = client.post("/mcp", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert response.status_code == 200
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def test_policy_web_and_mcp_settings_agree_and_keep_previous_rules(console):
    client, store, _, _, _, _, token, mcp_client = console
    web = client.get("/api/settings").json()
    assert web["feishu_event_policy"] == DEFAULT_POLICY
    assert "feishu_policy_since" not in web
    assert mcp(mcp_client, token, "gpu_get_notification_settings", {})["feishu_event_policy"] == DEFAULT_POLICY
    headers = {"x-csrf-token": "fixture-csrf"}
    disabled = {"events": [], "capacity_wait_minutes": 18}
    assert client.patch("/api/settings", json={"feishu_event_policy": {"capacity_wait_minutes": 18}},
                        headers=headers).status_code == 422
    response = client.patch("/api/settings", json={"feishu_event_policy": disabled}, headers=headers)
    assert response.status_code == 200 and response.json()["feishu_event_policy"] == disabled
    assert mcp(mcp_client, token, "gpu_get_notification_settings", {})["feishu_event_policy"] == disabled
    selected = {"events": ["instance_started", "capacity_wait"], "capacity_wait_minutes": 30}
    saved = mcp(mcp_client, token, "gpu_update_notification_settings", {
        "request_id": "test-policy", "changes": {"feishu_event_policy": selected},
    })
    assert saved["feishu_event_policy"] == selected
    latest = client.get("/api/settings").json()
    assert latest["feishu_event_policy"] == selected and latest["notification_rules"] == web["notification_rules"]
    assert deliveries(store.path) == []


def test_confirmed_start_and_stop_and_noop_through_web_api(console, monkeypatch):
    client, store, runner, cloud, now, channels, _, _ = console

    def action(kind, request_id):
        response = client.post("/api/actions", headers={"x-csrf-token": "fixture-csrf"}, json={
            "kind": kind, "project_id": PROJECT, "instance_id": SCOPE["instance_id"], "request_id": request_id,
        })
        assert response.status_code == 202
        return response.json()["id"]

    start = action("start", "web-start")
    runner.tick()
    assert store.get_run(start)["state"] == "reconciling" and deliveries(store.path) == []
    cloud.state = "Running"
    now[0] += 16
    runner.tick()
    assert store.get_run(start)["state"] == "succeeded"
    assert action("start", "web-start") == start
    runner.tick()

    action("start", "reused-start")
    runner.tick()
    assert cloud.writes == ["start"]
    stop = action("stop", "web-stop")
    runner.tick()
    assert len(deliveries(store.path)) == 1 and store.get_run(stop)["state"] == "reconciling"
    cloud.state = "Stopped"
    now[0] += 16
    runner.tick()
    action("stop", "already-stopped")
    runner.tick()
    assert cloud.writes == ["start", "stop"]
    assert [(item["event_id"], item["payload"]["policy_event"]) for item in deliveries(store.path)] == [
        (start + ":result", "instance_started"), (stop + ":result", "instance_stopped"),
    ]

    sent = []

    def post(url, **options):
        if url.endswith("/token"):
            return httpx.Response(200, json={"code": 0, "access_token": "token-fixture", "expires_in": 7200})
        sent.append(options["json"])
        if len(sent) == 1:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_fixture"}})

    monkeypatch.setattr(httpx, "post", post)
    outbox = Outbox(store.path, channels)
    outbox.tick()
    assert deliveries(store.path)[0]["state"] == "pending"
    outbox.tick()  # Stop notification can be delivered while the start notification is waiting to retry.
    now[0] += 30
    outbox.tick()
    assert [item["state"] for item in deliveries(store.path)] == ["delivered", "delivered"]
    assert sent[0]["uuid"] == sent[2]["uuid"] != sent[1]["uuid"]
    message = json.loads(sent[0]["content"])["text"]
    assert message.startswith("实例开机已确认\n")
    assert PROJECT in message and SCOPE["instance_id"] in message and "+08:00" in message
    assert f"/#tasks?project={PROJECT}&run={start}" in message
    assert cloud.writes == ["start", "stop"]


def test_explicit_subscription_and_review_then_completed(console):
    _, store, runner, cloud, now, _, _, _ = console
    with storage.connect(store.path) as conn:
        storage.set_settings(conn, {"feishu_event_policy": {"events": [], "capacity_wait_minutes": 15}})
    definition = {
        "name": "手工订阅的预约开机", "kind": "start", "project_id": PROJECT, "config": SCOPE,
        "channels": ["site", "feishu"], "schedule": {"type": "once", "at": now[0]},
    }
    store.create_task(definition)
    runner.tick()
    run, = store.list_runs()
    now[0] += 601
    runner.tick()
    assert store.get_run(run["id"])["state"] == "manual_review"
    assert len(deliveries(store.path)) == 1 and not deliveries(store.path)[0]["payload"].get("feishu_policy_only")

    with storage.connect(store.path) as conn:
        storage.set_settings(conn, {"feishu_event_policy": DEFAULT_POLICY})
    cloud.state = "Running"
    store.queue_instance_recheck(run["id"])
    runner.tick()
    assert [(item["event_id"], item["payload"]["policy_event"]) for item in deliveries(store.path)] == [
        (run["id"] + ":review", "operation_review"), (run["id"] + ":result", "instance_started"),
    ]
    assert cloud.writes == ["start"]


def test_restore_skips_queued_global_notices_and_keeps_explicit_subscriptions(console, tmp_path):
    _, store, _, _, now, _, _, _ = console
    with storage.connect(store.path) as conn, storage.transaction(conn):
        emit(conn, "global:result", {"title": "试验实例"}, ["site"],
             policy_event="instance_started", feishu_configured=True)
        emit(conn, "explicit:result", {"title": "试验实例"}, ["feishu"],
             policy_event="instance_started", feishu_configured=True)
    backup = tmp_path / "saved.sqlite3"
    backup_at = maintenance.backup(store.path, backup)
    now[0] += 60
    assert maintenance.restore(store.path, backup) == (backup_at, now[0])
    with storage.connect(store.path) as conn:
        settings = storage.get_settings(conn)
        assert settings["feishu_policy_since"] == now[0] and settings["restore_gate"]
    assert [(row["event_id"], row["state"]) for row in deliveries(store.path)] == [
        ("global:result", "skipped"), ("explicit:result", "pending"),
    ]
