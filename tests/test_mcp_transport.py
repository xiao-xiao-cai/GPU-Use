import hashlib
import json
import subprocess
import sys
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from gpu_console import storage
from gpu_console.api import auth as web_auth
from gpu_console.api import create_app
from gpu_console.config import Config
from gpu_console.mcp import auth, server
from gpu_console.storage import maintenance
from gpu_console.worker.main import _console_origin

PROTOCOL = "2025-11-25"
ACCEPT = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
INITIALIZE = {
    "protocolVersion": PROTOCOL,
    "capabilities": {},
    "clientInfo": {"name": "gpu-console-tests", "version": "1"},
}


@pytest.fixture
def mcp_transport(tmp_path, monkeypatch):
    config = Config(
        tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False,
        mcp_bind_ip="10.55.0.2", mcp_bind_port=9444,
        mcp_trusted_hosts=("gpu.example.test",), mcp_trusted_origins=("https://gpu.example.test",),
    )

    def register(mcp, _config):
        @mcp.scoped_tool("read", name="gpu_test_principal", description="Return the active test credential identifier")
        def principal() -> dict[str, str]:
            return {"credential_id": auth.require_principal().credential_id}

    monkeypatch.setattr(server, "register_tools", register)
    with TestClient(server.create_mcp_app(config), base_url="http://10.55.0.2:9444", follow_redirects=False) as client:
        yield client, config


def rpc(client, method, params=None, *, token=None, headers=None):
    headers = ACCEPT | (headers or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        "/mcp", headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def issue(config, *, key_id=None, manage=False, ssh=False, days=90):
    with storage.connect(config.db) as conn, storage.transaction(conn):
        return auth.issue(conn, key_id=key_id, name="Test client", manage=manage, ssh=ssh, days=days)["token"]


def test_mcp_protocol_authentication_rotation_and_revoke(mcp_transport):
    client, config = mcp_transport
    anonymous = rpc(client, "initialize", INITIALIZE)
    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"] == "Bearer"
    assert anonymous.headers["cache-control"] == "no-store"

    first = issue(config)
    principal = auth.authenticate(config.db, first)
    assert principal is not None
    with storage.connect(config.db) as conn:
        stored = conn.execute("SELECT token_hash FROM mcp_credentials").fetchone()["token_hash"]
        assert stored != first and len(stored) == 64
        assert auth.is_current(conn, principal.credential_id)

    assert rpc(client, "initialize", INITIALIZE, token=first + "bad").status_code == 401
    initialized = rpc(client, "initialize", INITIALIZE, token=first)
    assert initialized.status_code == 200
    assert initialized.json()["result"]["serverInfo"]["name"] == "gpu_console_mcp"
    assert "mcp-session-id" not in initialized.headers
    assert first not in initialized.text
    listing = rpc(client, "tools/list", token=first)
    assert listing.status_code == 200
    assert [tool["name"] for tool in listing.json()["result"]["tools"]] == ["gpu_test_principal"]
    call = rpc(client, "tools/call", {"name": "gpu_test_principal", "arguments": {}}, token=first)
    assert call.json()["result"]["structuredContent"] == {"credential_id": principal.credential_id}
    assert auth.current_principal.get() is None
    assert client.get("/api/projects", headers={"Authorization": f"Bearer {first}"}).status_code == 404
    assert rpc(client, "tools/list", token=first).headers["cache-control"] == "no-store"

    second = issue(config, key_id=principal.key_id)
    assert rpc(client, "tools/list", token=first).status_code == 401
    assert rpc(client, "tools/list", token=second).status_code == 200
    with storage.connect(config.db) as conn, storage.transaction(conn):
        assert not auth.is_current(conn, principal.credential_id)
        auth.revoke(conn, principal.key_id)
    assert rpc(client, "tools/list", token=second).status_code == 401


def test_mcp_transport_requires_verified_host_and_origin(mcp_transport):
    client, config = mcp_transport
    token = issue(config)
    assert rpc(client, "initialize", INITIALIZE, token=token, headers={
        "Origin": "http://10.55.0.2:9444",
    }).status_code == 200
    assert rpc(client, "initialize", INITIALIZE, token=token, headers={
        "Host": "gpu.example.test", "Origin": "https://gpu.example.test",
    }).status_code == 200
    assert rpc(client, "initialize", INITIALIZE, token=token, headers={
        "Host": "10.55.0.2:18080", "X-Forwarded-Host": "10.55.0.2:9444",
    }).status_code == 421
    assert rpc(client, "initialize", INITIALIZE, token=token, headers={
        "Origin": "https://untrusted.example.test", "X-Forwarded-Host": "10.55.0.2:9444",
    }).status_code == 403
    with pytest.raises(ValueError, match="GPU_MCP_TRUSTED_HOSTS"):
        server.create_mcp_app(replace(config, mcp_trusted_hosts=("gpu.example.test:*",)))
    with pytest.raises(ValueError, match="GPU_MCP_TRUSTED_ORIGINS"):
        server.create_mcp_app(replace(config, mcp_trusted_origins=("https://gpu.example.test/path",)))


def test_config_default_endpoints_are_loopback_and_ssh_requires_opt_in(monkeypatch):
    for name in ("GPU_BIND_IP", "GPU_BIND_PORT", "GPU_MCP_BIND_PORT", "GPU_SSH_PROXY"):
        monkeypatch.delenv(name, raising=False)
    config = Config.from_env()
    assert (config.mcp_bind_ip, config.console_bind_port, config.mcp_bind_port) == ("127.0.0.1", 18080, 18081)
    assert config.ssh_proxy is None
    monkeypatch.setenv("GPU_SSH_PROXY", "proxy.example.test:3128")
    assert Config.from_env().ssh_proxy == "proxy.example.test:3128"


def test_mcp_port_and_proxy_address_are_independent_of_web_notification_links(monkeypatch):
    monkeypatch.setenv("GPU_BIND_IP", "10.55.0.2")
    monkeypatch.setenv("GPU_BIND_PORT", "9444")
    monkeypatch.setenv("GPU_MCP_BIND_PORT", "9445")
    monkeypatch.setenv("GPU_MCP_PUBLIC_URL", "https://mcp.example.test/mcp")
    config = Config.from_env()
    assert server.connection_urls(config) == ("https://mcp.example.test/mcp", "http://127.0.0.1:9445/mcp")
    assert config.mcp_bind_port == 9445
    assert _console_origin(config) == ("https" if config.cookie_secure else "http") + "://10.55.0.2:9444"


def test_mcp_restore_invalidates_token_even_after_restore_gate_is_cleared(mcp_transport, tmp_path):
    client, config = mcp_transport
    token = issue(config)
    backup = tmp_path / "mcp-backup.sqlite3"
    maintenance.backup(config.db, backup)
    maintenance.restore(config.db, backup)
    maintenance.confirm_restore(config.db, "测试回读")
    result = rpc(client, "initialize", INITIALIZE, token=token)
    assert result.status_code == 401
    assert token not in result.text


def test_mcp_token_cli_displays_only_fresh_token(mcp_transport, monkeypatch):
    client, config = mcp_transport
    monkeypatch.setenv("GPU_DATA_DIR", str(config.data_dir))

    def command(action, *args):
        return subprocess.run(
            [sys.executable, "-m", "gpu_console.cli", "mcp-token", action, *args],
            capture_output=True, check=True, text=True,
        ).stdout.strip()

    assert json.loads(command("status")) == []
    first = issue(config)
    key_id = json.loads(command("status"))[0]["id"]
    token = command("rotate", "--key-id", key_id)
    assert auth.TOKEN_PATTERN.fullmatch(token)
    assert first not in command("status") and token not in command("status")
    assert rpc(client, "tools/list", token=first).status_code == 401
    assert rpc(client, "tools/list", token=token).status_code == 200
    assert command("revoke", "--key-id", key_id) == "MCP 令牌已撤销"
    assert rpc(client, "tools/list", token=token).status_code == 401


def test_web_key_lifecycle_permissions_and_service_split(tmp_path, monkeypatch):
    now = [1_797_926_400]
    monkeypatch.setattr(storage, "now", lambda: now[0])
    cfg = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    url = f"http://{cfg.mcp_bind_ip}:{cfg.mcp_bind_port}"
    with (
        TestClient(create_app(cfg), base_url=f"http://{cfg.mcp_bind_ip}:{cfg.console_bind_port}") as web,
        TestClient(server.create_mcp_app(cfg), base_url=url) as mcp,
    ):
        assert web.get("/api/mcp/keys").status_code == 401
        cookie = "web-session-fixture"
        with storage.connect(cfg.db) as conn:
            conn.execute("INSERT INTO sessions VALUES (?,?,?)", (web_auth.digest(cookie), "csrf", now[0] + 86400))
        web.cookies.set(web_auth.COOKIE, cookie)
        mcp.cookies.set(web_auth.COOKIE, cookie)
        assert rpc(mcp, "tools/list").status_code == 401
        assert web.post("/mcp").status_code == 404
        assert web.post("/api/mcp/keys", json={"name": "Example"}).status_code == 403
        headers = {"X-CSRF-Token": "csrf"}
        assert web.post("/api/mcp/keys", json={"name": " "}, headers=headers).status_code == 422
        assert web.post("/api/mcp/keys", json={"name": "Example", "days": 1}, headers=headers).status_code == 422

        def create(name, **kwargs):
            response = web.post("/api/mcp/keys", json={"name": name, **kwargs}, headers=headers)
            assert response.status_code == 201
            assert response.headers["cache-control"] == "no-store"
            return response.json()

        readonly = create("Reader")
        manager = create("Manager", manage=True)
        terminal = create("SSH", ssh=True, days=None)
        assert readonly["key"]["expires_at"] == now[0] + 90 * 86400
        assert terminal["key"]["expires_at"] is None
        assert mcp.get("/healthz").json() == {"status": "ok"}

        def tools(token):
            listing = rpc(mcp, "tools/list", token=token).json()["result"]["tools"]
            return {tool["name"] for tool in listing}

        reader_tools = tools(readonly["token"])
        assert {"gpu_list_projects", "gpu_list_tasks", "gpu_get_cost_summary"} <= reader_tools
        assert reader_tools < tools(manager["token"])
        assert reader_tools < tools(terminal["token"])
        assert tools(terminal["token"]) & tools(manager["token"]) == reader_tools
        for issued, name in (
            (readonly, "gpu_refresh_data"), (readonly, "gpu_get_instance_create_options"),
            (readonly, "gpu_preview_task"), (manager, "gpu_ssh_open"),
            (terminal, "gpu_stop_instance"),
        ):
            args = {"name": name, "arguments": {}}
            outcome = rpc(mcp, "tools/call", args, token=issued["token"]).json()["result"]
            assert outcome["isError"] and "无权" in outcome["content"][0]["text"]
        assert terminal["token"] not in web.get("/api/mcp/keys").text
        assert mcp.get("/api/mcp/keys").status_code == 404

        now[0] += 10
        tools(readonly["token"])
        rows = web.get("/api/mcp/keys").json()
        assert next(row for row in rows if row["id"] == readonly["key"]["id"])["last_used_at"] == now[0] - 10
        now[0] += 300
        tools(readonly["token"])
        rows = web.get("/api/mcp/keys").json()
        assert next(row for row in rows if row["id"] == readonly["key"]["id"])["last_used_at"] == now[0]

        path = "/api/mcp/keys/" + manager["key"]["id"]
        rotated = web.post(path + "/rotate", headers=headers, json={"name": "New name", "days": 7}).json()
        assert rotated["key"]["id"] == manager["key"]["id"]
        assert tools(rotated["token"]) == reader_tools
        assert rpc(mcp, "tools/list", token=manager["token"]).status_code == 401
        assert web.delete(path, headers=headers).status_code == 204
        assert rpc(mcp, "tools/list", token=rotated["token"]).status_code == 401
        assert web.post(path + "/rotate", headers=headers, json={"name": "Denied"}).status_code == 422
        assert "gpu_ssh_open" in tools(terminal["token"])

        now[0] = readonly["key"]["expires_at"]
        assert rpc(mcp, "tools/list", token=readonly["token"]).status_code == 401
        assert "gpu_ssh_open" in tools(terminal["token"])


def test_migrated_singleton_keeps_identity_but_cannot_revive_on_rollback(tmp_path):
    cfg = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    cfg.data_dir.mkdir()
    key_id = "a" * 32
    legacy = "gpu_mcp_" + "A" * 43
    with storage.connect(cfg.db) as conn:
        conn.execute("""
            CREATE TABLE mcp_credentials(
                id INTEGER PRIMARY KEY CHECK(id=1), credential_id TEXT NOT NULL,
                token_hash TEXT NOT NULL, created_at INTEGER NOT NULL
            )
        """)
        conn.execute("INSERT INTO mcp_credentials VALUES (1,?,?,?)", (
            key_id, hashlib.sha256(legacy.encode()).hexdigest(), 42,
        ))
    storage.initialize(cfg.db)
    storage.initialize(cfg.db)
    principal = auth.authenticate(cfg.db, legacy)
    assert (principal.credential_id, principal.key_id, principal.manage, principal.ssh) == (key_id, key_id, True, True)
    with storage.connect(cfg.db) as conn, storage.transaction(conn):
        assert len(auth.list_keys(conn)) == 1
        assert auth.get_key(conn, key_id)["name"] == "旧版 MCP Key"
        assert conn.execute("SELECT * FROM mcp_credentials WHERE id=1").fetchone() is None
        auth.revoke(conn, key_id)
    storage.initialize(cfg.db)
    assert auth.authenticate(cfg.db, legacy) is None
