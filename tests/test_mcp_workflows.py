import asyncio
import json

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from gpu_console import storage
from gpu_console.application.tasks import TaskStore
from gpu_console.config import Config
from gpu_console.mcp import auth, common
from gpu_console.mcp.server import create_mcp_app


def configuration(tmp_path):
    return Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False,
                  mcp_bind_ip="127.0.0.1", mcp_bind_port=18081)


def test_mcp_receipt_is_atomic_with_nested_store_and_consumed_preview(tmp_path):
    config = configuration(tmp_path)
    storage.initialize(config.db)
    with storage.connect(config.db) as conn, storage.transaction(conn):
        token = auth.issue(conn, name="Test client", manage=True, ssh=True, days=None)["token"]
    principal = auth.authenticate(config.db, token)
    context = auth.current_principal.set(principal)
    try:
        preview = common.issue_preview(config, "schedule", {"project_id": "fixture", "name": "example"})
        payload = {"preview_id": preview}

        def create(conn):
            data = common.read_preview(conn, "schedule", preview)
            return TaskStore(config.db).create_task(data | {
                "kind": "inspection", "config": {}, "schedule": {"type": "once", "at": storage.now() + 600},
            })

        def fail(conn):
            create(conn)
            raise ValueError("failed after nested task write")

        with pytest.raises(ValueError, match="failed after nested"):
            common.mutate(config, "schedule", "submission", payload, fail)
        assert TaskStore(config.db).list_tasks("fixture") == []
        stored = common.mutate(config, "schedule", "submission", payload, create)
        assert common.mutate(config, "schedule", "submission", payload, fail) == stored
        with pytest.raises(ValueError, match="request_id"):
            common.mutate(config, "schedule", "submission", {"preview_id": "other"}, create)
        with pytest.raises(ValueError, match="已使用或已过期"):
            common.mutate(config, "schedule", "second-attempt", payload, create)
        with storage.connect(config.db) as conn, storage.transaction(conn):
            replacement = auth.issue(
                conn, name="Test client", manage=True, ssh=True, days=None, key_id=principal.key_id,
            )["token"]
        with pytest.raises(ValueError, match="令牌已失效"):
            common.mutate(config, "schedule", "submission", payload, fail)
    finally:
        auth.current_principal.reset(context)
    new_context = auth.current_principal.set(auth.authenticate(config.db, replacement))
    try:
        assert common.mutate(config, "schedule", "submission", payload, fail) == stored
    finally:
        auth.current_principal.reset(new_context)


def test_real_mcp_client_discovers_tools_and_queries_scoped_project_and_operation(tmp_path):
    config = configuration(tmp_path)
    app = create_mcp_app(config)

    async def exercise():
        async with app.router.lifespan_context(app):
            now = storage.now()
            projects = [{"id": "first", "name": "First"}, {"id": "other", "name": "Other"}]
            instance = {"id": "test-node", "name": "Test", "state": "Stopped", "project_id": "first",
                        "region": "fixture-region", "zone": "fixture-zone", "gpu": 2, "instance_type": "UHost",
                        "charge_type": "Postpay", "ssh": {"password": "hidden-password"},
                        "software_links": [{"url": "https://example.invalid/private-token"}]}
            with storage.connect(config.db) as conn, storage.transaction(conn):
                token = auth.issue(conn, name="Test client", manage=True, ssh=True, days=None)["token"]
                conn.executemany("INSERT INTO snapshots VALUES (?,?,?,?,NULL)", [
                    ("projects", "", json.dumps(projects), now),
                    ("instances", "first", json.dumps([instance]), now),
                    ("instances", "other", "[]", now),
                ])
                conn.execute("INSERT INTO metrics VALUES (?,?,?,?,?,?)",
                             ("first", "test-node", "cloudwatch_gpu_util", "GPU 0", now, 0))

            url = f"http://{config.mcp_bind_ip}:{config.mcp_bind_port}"
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                          headers={"Authorization": f"Bearer {token}"}) as client:
                health = await client.get(url + "/healthz")
                assert health.json() == {"status": "ok"}
                async with streamable_http_client(url + "/mcp", http_client=client) as streams:
                    async with ClientSession(*streams) as session:
                        assert (await session.initialize()).server_info.name == "gpu_console_mcp"
                        names = {tool.name for tool in (await session.list_tools()).tools}
                        assert {"gpu_list_projects", "gpu_get_instance", "gpu_create_instance", "gpu_prepare_instance_release",
                                "gpu_delete_task", "gpu_delete_rule", "gpu_update_template", "gpu_delete_watch",
                                "gpu_update_notification_settings"} <= names

                        async def call(name, **arguments):
                            result = await session.call_tool(name, arguments=arguments)
                            assert not result.is_error, result.content
                            assert result.structured_content is not None
                            return result.structured_content

                        page = await call("gpu_list_projects", limit=1)
                        assert page["count"] == 1 and page["next_offset"] == 1
                        view = await call("gpu_get_instance", project_id="first", instance_id="test-node")
                        assert view["instance"]["gpu"] == 2
                        assert "shutdown_cleanup_pending" not in view
                        assert "hidden-password" not in str(view) and "private-token" not in str(view)
                        wrong = await session.call_tool("gpu_get_instance", {"project_id": "other", "instance_id": "test-node"})
                        assert wrong.is_error
                        assert "找不到" in wrong.content[0].text
                        metric = await call("gpu_get_metrics", project_id="first", instance_id="test-node")
                        assert metric["items"][0]["value"] == 0
                        params = {"project_id": "first", "instance_id": "test-node", "request_id": "start-idempotent"}
                        queued = await call("gpu_start_instance", **params)
                        assert await call("gpu_start_instance", **params) == queued
                        run_id = queued["run_id"]
                        active = await call("gpu_get_run", project_id="first", run_id=run_id)
                        assert active["state"] == "queued"
                        canceled = await call("gpu_cancel_run", project_id="first", run_id=run_id, request_id="cancel-once")
                        assert canceled["state"] == "canceled"
                        with storage.connect(config.db) as conn, storage.transaction(conn):
                            storage.set_settings(conn, {"restore_gate": True})
                        blocked = await session.call_tool("gpu_start_instance", params | {"request_id": "blocked"})
                        assert blocked.is_error
                        assert "恢复核对" in blocked.content[0].text
                        runs = await call("gpu_list_runs", project_id="first")
                        assert [item["id"] for item in runs["items"]] == [run_id]

    asyncio.run(exercise())
