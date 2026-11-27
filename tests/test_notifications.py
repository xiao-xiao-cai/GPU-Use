import json
from uuid import UUID

import httpx
import pytest

from gpu_console import storage
from gpu_console.adapters.notifications import Channels, DeliveryError
from gpu_console.storage.feishu import save_feishu_settings
from gpu_console.worker.outbox import Outbox


def test_channel_retry_does_not_recreate_site_event(tmp_path, monkeypatch):
    time = 1000
    monkeypatch.setattr(storage, "now", lambda: time)
    db = tmp_path / "console.sqlite3"
    storage.initialize(db)
    payload = {"title": "云端已确认运行", "instance_id": "stub-1"}
    with storage.connect(db) as conn, storage.transaction(conn):
        storage.emit(conn, "run-1", payload, ["site", "feishu"])
        storage.emit(conn, "run-1", payload, ["site", "feishu"])

    class MockChannels:
        attempts = 0

        def send(self, channel, message, *, event_id):
            assert (channel, message) == ("feishu", payload)
            assert event_id == "run-1"
            self.attempts += 1
            if self.attempts == 1:
                raise DeliveryError("模拟暂时失败")

    channels = MockChannels()
    outbox = Outbox(db, channels)
    outbox.tick()
    outbox.tick()
    assert channels.attempts == 1
    time += 31
    outbox.tick()
    outbox.tick()
    with storage.connect(db) as conn:
        results = [
            tuple(row) for row in conn.execute("SELECT channel,state,attempts FROM notifications ORDER BY id")
        ]
    assert results == [("site", "delivered", 0), ("feishu", "delivered", 2)]


def test_feishu_delivery_checks_signature_keyword_and_business_receipt(tmp_path, monkeypatch):
    (tmp_path / "feishu_webhook_url").write_text("https://open.feishu.cn/open-apis/bot/v2/hook/stub")
    (tmp_path / "feishu_sign_secret").write_text("secret-stub")
    (tmp_path / "feishu_keyword").write_text("训练告警")
    monkeypatch.setattr("gpu_console.adapters.notifications.time.time", lambda: 1000)
    replies = iter([{"code": 0}, {"code": False}, {"code": 19024}])

    def post(url, *, json, timeout):
        assert url.endswith("/stub")
        assert json["timestamp"] == "1000"
        assert json["sign"] == "RlDjptjEP1Wg5fqei33mLzNNTBiux0/lA3cG3yvr6kg="
        assert json["content"]["text"].startswith("训练告警\n")
        assert "+08:00" in json["content"]["text"]
        return httpx.Response(200, json=next(replies))

    monkeypatch.setattr(httpx, "post", post)
    channels = Channels(tmp_path)
    message = {"title": "实例已运行", "confirmed_at": 1000}
    channels.send("feishu", message)
    with pytest.raises(DeliveryError, match="未返回有效业务回执"):
        channels.send("feishu", message)
    with pytest.raises(DeliveryError, match="19024") as failed:
        channels.send("feishu", message)
    assert not failed.value.retryable


def test_feishu_app_outbox_retries_same_request_and_reuses_token(tmp_path, monkeypatch):
    db = tmp_path / "console.sqlite3"
    storage.initialize(db)
    settings = {
        "mode": "app", "app_id": "cli_stub", "app_secret": "secret-stub",
        "receive_id_type": "email", "receive_id": "tester@example.invalid",
    }
    save_feishu_settings(tmp_path, settings)
    payload = {"title": "处理已完成", "message": "请检查实例\n运行正常"}
    timestamp = 1000
    monkeypatch.setattr(storage, "now", lambda: timestamp)
    auth_requests = []
    sent = []

    def respond(request):
        body = json.loads(request.content)
        if request.url == "https://accounts.feishu.cn/oauth/v3/token":
            auth_requests.append(body)
            return httpx.Response(200, json={
                "code": 0, "access_token": f"token-stub-{len(auth_requests)}", "expires_in": 7200,
            })
        assert request.url == "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=email"
        assert body["receive_id"] == "tester@example.invalid" and body["msg_type"] == "text"
        assert json.loads(body["content"]) == {"text": "处理已完成\n请检查实例\n运行正常"}
        assert UUID(body["uuid"]).version == 5
        assert request.headers["Authorization"] == f"Bearer token-stub-{len(auth_requests)}"
        sent.append(body)
        if len(sent) == 1:
            raise httpx.ReadTimeout("raw response includes secret-stub", request=request)
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_stub"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(httpx, "post", client.post)
        with storage.connect(db) as conn:
            storage.emit(conn, "run-1", payload, ["site", "feishu"])
        Outbox(db, Channels(tmp_path)).tick()
        with storage.connect(db) as conn:
            retry = conn.execute("SELECT state,error FROM notifications WHERE channel='feishu'").fetchone()
        assert tuple(retry) == ("pending", "飞书发送消息连接失败")

        timestamp += 31
        outbox = Outbox(db, Channels(tmp_path))
        outbox.tick()
        with storage.connect(db) as conn:
            storage.emit(conn, "run-2", payload, ["feishu"])
        outbox.tick()
        assert len(auth_requests) == 2
        assert sent[0] == sent[1] and sent[1]["uuid"] != sent[2]["uuid"]
        assert auth_requests[0] == auth_requests[1] == {
            "grant_type": "client_credentials", "client_id": "cli_stub", "client_secret": "secret-stub",
        }

        save_feishu_settings(tmp_path, settings | {"app_secret": "rotated-secret-stub"})
        with storage.connect(db) as conn:
            storage.emit(conn, "run-3", payload, ["feishu"])
        outbox.tick()
        assert len(auth_requests) == 3
        assert auth_requests[-1]["client_secret"] == "rotated-secret-stub"
    with storage.connect(db) as conn:
        deliveries = conn.execute("SELECT channel,state,attempts FROM notifications ORDER BY id")
        assert [tuple(row) for row in deliveries] == [
            ("site", "delivered", 0), ("feishu", "delivered", 2),
            ("feishu", "delivered", 1), ("feishu", "delivered", 1),
        ]


@pytest.mark.parametrize("recovers", [True, False])
def test_feishu_app_retries_token_transport_once(tmp_path, monkeypatch, recovers):
    save_feishu_settings(tmp_path, {
        "mode": "app", "app_id": "cli_stub", "app_secret": "secret-stub",
        "receive_id_type": "open_id", "receive_id": "ou_stub",
    })
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url == "https://accounts.feishu.cn/oauth/v3/token":
            if len(requests) == 1 or not recovers:
                raise httpx.ConnectTimeout("raw timeout includes secret-stub", request=request)
            return httpx.Response(200, json={"code": 0, "access_token": "token-stub", "expires_in": 7200})
        assert request.url == "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
        assert request.headers["Authorization"] == "Bearer token-stub"
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_stub"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(httpx, "post", client.post)
        channels = Channels(tmp_path)
        if recovers:
            channels.send("feishu", {"title": "测试通知"})
        else:
            with pytest.raises(DeliveryError, match="^飞书获取访问凭证请求超时$") as failure:
                channels.send("feishu", {"title": "测试通知"})
            assert failure.value.retryable

    expected = ["/oauth/v3/token", "/oauth/v3/token"]
    if recovers:
        expected.append("/open-apis/im/v1/messages")
    assert requests == expected


@pytest.mark.parametrize(("during_auth", "status", "receipt", "message", "retryable"), [
    (True, 400, {"code": 20002, "error_description": "secret-stub"}, "20002.*核对 App ID", False),
    (True, 200, {"code": 0, "access_token": "bad\nheader", "expires_in": 7200}, "有效访问凭证", True),
    (False, 400, {"code": 230013, "msg": "secret-stub"}, "230013.*可用范围", False),
    (False, 400, {"code": 230020, "msg": "secret-stub"}, "230020", True),
    (False, 200, {"code": 0, "data": {"message_id": ""}}, "有效消息回执", True),
])
def test_feishu_app_rejects_invalid_receipts(tmp_path, monkeypatch, during_auth, status, receipt, message, retryable):
    save_feishu_settings(tmp_path, {
        "mode": "app", "app_id": "cli_stub", "app_secret": "secret-stub",
        "receive_id_type": "open_id", "receive_id": "ou_stub",
    })
    requests = []

    def post(url, **kwargs):
        requests.append(url)
        if url.endswith("/token") and not during_auth:
            return httpx.Response(200, json={"code": 0, "access_token": "token-stub", "expires_in": 7200})
        assert url.endswith("/token") == during_auth
        return httpx.Response(status, json=receipt)

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(DeliveryError, match=message) as failure:
        Channels(tmp_path).send("feishu", {"title": "测试通知"})
    assert failure.value.retryable is retryable
    assert "secret-stub" not in str(failure.value)
    assert len(requests) == (1 if during_auth else 2)


def test_feishu_app_refreshes_expiring_and_rejected_access_tokens(tmp_path, monkeypatch):
    save_feishu_settings(tmp_path, {
        "mode": "app", "app_id": "cli_stub", "app_secret": "secret-stub",
        "receive_id_type": "chat_id", "receive_id": "oc_stub",
    })
    timestamp = 1000
    monkeypatch.setattr("gpu_console.adapters.notifications.time.monotonic", lambda: timestamp)
    auth_count = 0
    sent = []

    def post(url, *, json, params, headers, timeout):
        nonlocal auth_count
        if url.endswith("/token"):
            auth_count += 1
            return httpx.Response(200, json={
                "code": 0, "access_token": f"token-stub-{auth_count}", "expires_in": 120,
            })
        assert params == {"receive_id_type": "chat_id"} and json["receive_id"] == "oc_stub"
        sent.append(headers["Authorization"])
        if len(sent) == 3:
            return httpx.Response(400, json={"code": 99991663})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_stub"}})

    monkeypatch.setattr(httpx, "post", post)
    channels = Channels(tmp_path)
    payload = {"title": "测试通知"}
    channels.send("feishu", payload)
    timestamp += 110
    channels.send("feishu", payload)
    with pytest.raises(DeliveryError, match="99991663") as failure:
        channels.send("feishu", payload)
    assert failure.value.retryable
    channels.send("feishu", payload)
    assert auth_count == 3
    assert sent == ["Bearer token-stub-1", "Bearer token-stub-2", "Bearer token-stub-2", "Bearer token-stub-3"]
