import base64
import json
from dataclasses import replace
from threading import Event

import pytest
from fastapi.testclient import TestClient

from gpu_console import storage
from gpu_console.adapters.openssh import Master
from gpu_console.application.ssh_access import TARGETS_FILE, resolve_target
from gpu_console.config import Config
from gpu_console.mcp import auth
from gpu_console.mcp.server import create_mcp_app
from gpu_console.worker.ssh_sessions import serve_ssh_sessions


@pytest.fixture
def enrolled(tmp_path):
    config = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False,
                    ssh_proxy="proxy.example.test:3128")
    storage.initialize(config.db)
    config.secrets_dir.mkdir()
    algorithm = b"ssh-ed25519"
    key_blob = b"\x00\x00\x00\x0b" + algorithm + b"\x00\x00\x00\x20" + b"\x00" * 32
    key = f"{algorithm.decode()} {base64.b64encode(key_blob).decode()}"
    instance = {
        "id": "node", "project_id": "project", "region": "region", "zone": "zone",
        "state": "Running", "login_mode": "Password",
        "ssh": {"user": "root", "host": "192.0.2.10", "port": 2222, "password": "private-fixture"},
    }
    (config.secrets_dir / TARGETS_FILE).write_text(json.dumps([
        {"project_id": "project", "instance_id": "node", "host_key": key},
    ]))
    with storage.connect(config.db) as conn, storage.transaction(conn):
        token = auth.issue(conn, name="Test client", manage=True, ssh=True, days=None)["token"]
        conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,NULL)",
                     ("instances", "project", json.dumps([instance]), storage.now()))
    return config, token


def rpc(client, token, name, **arguments):
    reply = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
    )
    assert reply.status_code == 200
    assert "private-fixture" not in reply.text
    return reply.json()["result"]


def test_mcp_ssh_open_without_proxy_does_not_attempt_a_connection(enrolled, monkeypatch):
    config, token = enrolled
    config = replace(config, ssh_proxy=None)

    def unexpected_open(*_args):
        pytest.fail("SSH must not connect without an explicit proxy")

    monkeypatch.setattr(Master, "open", unexpected_open)
    with serve_ssh_sessions(config), TestClient(create_mcp_app(config), base_url="http://127.0.0.1:18081") as client:
        result = rpc(client, token, "gpu_ssh_open", project_id="project", instance_id="node")
    assert result["isError"] and "GPU_SSH_PROXY" in result["content"][0]["text"]


def test_mcp_ssh_uses_worker_session_and_deduplicates_input(enrolled, monkeypatch):
    config, token = enrolled
    principal = auth.authenticate(config.db, token)
    opened = set()
    calls = []

    def open_mock(master, data_dir, proxy):
        assert data_dir == config.data_dir and proxy == config.ssh_proxy
        opened.add(master.path)

    def close_mock(master):
        opened.discard(master.path)

    def run_mock(master, command):
        calls.append(command)
        return {"state": "finished", "exit_code": 0, "stdout": "fixture", "stderr": "", "truncated": False}

    monkeypatch.setattr(Master, "open", open_mock)
    monkeypatch.setattr(Master, "close", close_mock)
    monkeypatch.setattr(Master, "alive", lambda master: master.path in opened)
    monkeypatch.setattr(Master, "run", run_mock)
    target = {"project_id": "project", "instance_id": "node"}

    with serve_ssh_sessions(config), TestClient(create_mcp_app(config), base_url="http://127.0.0.1:18081") as client:
        info = rpc(client, token, "gpu_get_ssh_connection", **target)["structuredContent"]
        assert info["host"] == "192.0.2.10" and info["port"] == 2222
        assert info["login_mode"] == "Password" and info["instance_state"] == "Running"
        blocked = rpc(client, token, "gpu_ssh_open", **(target | {"project_id": "other"}))
        assert blocked["isError"]
        first = rpc(client, token, "gpu_ssh_open", **target)["structuredContent"]
        assert rpc(client, token, "gpu_ssh_open", **target)["structuredContent"] == first
        session_id = first["session_id"]
        args = {"session_id": session_id, "request_id": "once", "line": "cd /tmp"}
        sent = rpc(client, token, "gpu_ssh_terminal_send", **args)["structuredContent"]
        assert "tmux send-keys" in calls[-1] and sent["state"] == "finished"
        assert rpc(client, token, "gpu_ssh_terminal_send", **args)["structuredContent"] == sent
        assert len(calls) == 1
        assert rpc(client, token, "gpu_ssh_terminal_send", **(args | {"line": "exit"}))["isError"]
        read = rpc(client, token, "gpu_ssh_terminal_read", session_id=session_id, lines=5)["structuredContent"]
        assert read["stdout"] == "fixture" and "tail -n 5" in calls[-1]
        with storage.connect(config.db) as conn, storage.transaction(conn):
            other_token = auth.issue(conn, name="Another SSH client", manage=False, ssh=True, days=7)["token"]
        other = rpc(client, other_token, "gpu_ssh_open", **target)["structuredContent"]
        assert other != first and len(opened) == 2
        assert rpc(client, other_token, "gpu_ssh_terminal_read", session_id=session_id)["isError"]
        assert rpc(client, token, "gpu_ssh_open", **target)["structuredContent"] == first
        with storage.connect(config.db) as conn, storage.transaction(conn):
            new_token = auth.issue(
                conn, name="Test client", manage=True, ssh=True, days=None, key_id=principal.key_id,
            )["token"]
        assert rpc(client, new_token, "gpu_ssh_terminal_read", session_id=session_id)["isError"]
        new = rpc(client, new_token, "gpu_ssh_open", **target)["structuredContent"]
        assert new["session_id"] != session_id and len(opened) == 2
        assert rpc(client, new_token, "gpu_ssh_close", session_id=new["session_id"])["structuredContent"] == {"closed": True}
        assert rpc(client, other_token, "gpu_ssh_close", session_id=other["session_id"])["structuredContent"] == {"closed": True}
        assert not opened


def test_revocation_closes_worker_connection_without_a_followup_request(enrolled, monkeypatch):
    config, token = enrolled
    principal = auth.authenticate(config.db, token)
    closed = Event()
    monkeypatch.setattr(Master, "open", lambda *args: None)
    monkeypatch.setattr(Master, "close", lambda *args: closed.set())
    with serve_ssh_sessions(config), TestClient(create_mcp_app(config), base_url="http://127.0.0.1:18081") as client:
        assert not rpc(client, token, "gpu_ssh_open", project_id="project", instance_id="node").get("isError")
        with storage.connect(config.db) as conn, storage.transaction(conn):
            auth.revoke(conn, principal.key_id)
        assert closed.wait(5)


def test_expired_and_unregistered_targets_do_not_resolve(enrolled):
    config, _ = enrolled
    assert resolve_target(config, "project", "node").user == "root"
    with storage.connect(config.db) as conn, storage.transaction(conn):
        conn.execute("UPDATE snapshots SET success_at=?", (storage.now() - 181,))
    with pytest.raises(ValueError, match="待更新"):
        resolve_target(config, "project", "node")
    (config.secrets_dir / TARGETS_FILE).unlink()
    with pytest.raises(ValueError, match="尚未启用"):
        resolve_target(config, "project", "node")


def test_command_output_is_bounded_and_timeout_is_unknown(enrolled, monkeypatch):
    from gpu_console.adapters import openssh

    config, _ = enrolled
    master = Master(resolve_target(config, "project", "node"), config.data_dir / "unused.sock")
    monkeypatch.setattr(openssh, "OUTPUT_LIMIT", 8)
    monkeypatch.setattr(Master, "command", lambda _, command: ["/bin/sh", "-c", command])
    outcome = master.run("printf %s abcdefghijk")
    assert outcome["state"] == "finished" and outcome["truncated"]
    assert outcome["stdout"] == "abcdefgh"
    monkeypatch.setattr(openssh, "COMMAND_SECONDS", 0.1)
    assert master.run("exec sleep 5")["state"] == "unknown"
