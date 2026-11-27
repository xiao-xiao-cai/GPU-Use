import json

import pytest
from fastapi.testclient import TestClient

from gpu_console import storage
from gpu_console.application.tasks import TaskStore, insert_run
from gpu_console.config import Config
from gpu_console.mcp import auth, management, server
from gpu_console.mcp.server import create_mcp_app

PROJECT = "project-alpha"
OTHER = "project-beta"
INSTANCE = "fake-gpu"


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    cfg = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, False)
    monkeypatch.setattr(server, "register_tools", management.register)
    with TestClient(create_mcp_app(cfg), base_url=f"http://{cfg.mcp_bind_ip}:{cfg.mcp_bind_port}") as client:
        with storage.connect(cfg.db) as conn:
            token = auth.issue(conn, name="Test client", manage=True, ssh=True, days=None)["token"]
            for kind, scope, data in (
                ("projects", "", [{"id": PROJECT}, {"id": OTHER}]),
                ("catalog", "", [{"region": "r1", "zone": "z1", "gpu_model": "A100", "instance_type": "uhost",
                                  "specs": [{"gpu": n, "cpu": cpu, "memory_gb": 32} for n in (1, 2) for cpu in (8, 16)],
                                  "disks": [{"purpose": "system", "type": "CLOUD_SSD", "default_size_gb": 60}]}]),
                ("instances", PROJECT, [{"id": INSTANCE, "project_id": PROJECT, "region": "r1", "zone": "z1",
                                         "state": "Running", "start_time": storage.now() - 60}]),
            ):
                conn.execute(
                    "INSERT INTO snapshots(kind,scope,data,success_at) VALUES (?,?,?,?)",
                    (kind, scope, json.dumps(data), storage.now()),
                )

        def call(name, *, error=None, **arguments):
            response = client.post("/mcp", headers={
                "Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            }, json={"jsonrpc": "2.0", "id": "test", "method": "tools/call",
                     "params": {"name": "gpu_" + name, "arguments": arguments}})
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            if error is None:
                assert not result.get("isError"), result
                return result["structuredContent"]
            assert result.get("isError"), result
            assert error in str(result["content"])
            return None

        yield call, cfg


def schedule():
    return {"type": "daily", "at": storage.now() + 1800}


def machine_spec():
    return {"gpu_model": "A100", "gpu": 1, "cpu": 8, "memory_gb": 32, "image_id": "fake-image",
            "charge_type": "Postpay", "cpu_platform": "Auto",
            "disks": [{"purpose": "system", "type": "CLOUD_SSD", "size_gb": 60}]}


def test_task_version_scope_replays_and_archive_history(mcp):
    call, cfg = mcp
    definition = {"name": "calm task", "kind": "start", "schedule": schedule(), "config": {"instance_id": INSTANCE}}
    args = {"project_id": PROJECT, "definition": definition, "request_id": "create-start"}
    task = call("create_task", **args)
    identifier = task["id"]
    assert task["config"] == {"instance_id": INSTANCE, "region": "r1", "zone": "z1"}
    assert task == call("create_task", **args)
    call("create_task", **(args | {"definition": definition | {"name": "different"}}), error="request_id")
    call("get_task", project_id=OTHER, task_id=identifier, error="找不到该任务")
    edit = {"project_id": PROJECT, "task_id": identifier, "definition": definition | {"name": "changed"}}
    task = call("update_task", **edit, expected_version=1, request_id="edit-start")
    assert task["version"] == 2 and task["name"] == "changed"
    call("update_task", **edit, expected_version=1, request_id="stale-edit", error="版本已变化")
    run = call("trigger_task", project_id=PROJECT, task_id=identifier, request_id="run")
    assert run["state"] == "queued"
    call("delete_task", project_id=PROJECT, task_id=identifier, request_id="delete-start")
    assert call("list_tasks", project_id=PROJECT)["items"] == []
    assert call("list_tasks", project_id=PROJECT, include_archived=True)["items"][0]["status"] == "canceled"
    assert TaskStore(cfg.db).get_run(run["run_id"])["state"] == "canceled"
    call("resume_task", project_id=PROJECT, task_id=identifier, request_id="reopen", error="已取消")


def test_mcp_task_trash_restore_and_purge_preserve_idempotency(mcp):
    call, _ = mcp
    definition = {"name": "待清理", "kind": "start", "schedule": schedule(), "config": {"instance_id": INSTANCE}}
    create_args = {"project_id": PROJECT, "definition": definition, "request_id": "create-trash"}
    task = call("create_task", **create_args)
    ref = {"project_id": PROJECT, "task_id": task["id"]}
    trash_args = ref | {"expected_version": task["version"], "request_id": "to-trash"}
    removed = call("trash_task", **trash_args)
    assert call("trash_task", **trash_args) == removed
    assert call("list_tasks", project_id=PROJECT)["items"] == []
    assert call("list_tasks", project_id=PROJECT, view="trash")["items"][0]["id"] == task["id"]
    call("restore_task", **(ref | {"project_id": OTHER}), expected_version=removed["version"],
         request_id="wrong-project", error="找不到该任务")
    restored = call("restore_task", **ref, expected_version=removed["version"], request_id="restore")
    assert restored["view"] == "current" and call("get_task", **ref)["status"] == "paused"
    removed = call("trash_task", **ref, expected_version=restored["version"], request_id="remove-again")
    purge_args = ref | {"expected_version": removed["version"], "request_id": "purge"}
    receipt = {"id": task["id"], "project_id": PROJECT, "purged": True}
    assert call("purge_task", **purge_args) == call("purge_task", **purge_args) == receipt
    assert call("create_task", **create_args) == receipt
    call("get_task", **ref, error="找不到该任务")


def test_purchase_and_template_previews_bind_configuration_and_affected_tasks(mcp):
    call, _ = mcp
    base = machine_spec() | {"zones": [{"region": "r1", "zone": "z1"}]}
    template = call("create_template", project_id=PROJECT,
                    definition={"name": "base", "config": base}, request_id="template-create")
    buy = {"name": "acquire with cap", "kind": "acquire", "schedule": schedule(), "template_id": template["id"],
           "config": {"max_price": "10", "purchase_limit": 2}}
    call("create_task", project_id=PROJECT, definition=buy, request_id="buy-no-preview", error="先预览")
    preview = call("preview_task", operation="create", project_id=PROJECT, definition=buy)
    assert preview["definition"]["config"]["purchase_limit"] == 2
    intent = {"project_id": PROJECT, "preview_id": preview["preview_id"], "request_id": "buy-with-preview"}
    call("create_task", **intent, definition=buy | {"name": "changed"}, error="已变化")
    task = call("create_task", **intent, definition=buy)
    assert task == call("create_task", **intent, definition=buy)
    call("create_task", **(intent | {"request_id": "buy-used-preview"}), definition=buy, error="无效")

    change = {"project_id": PROJECT, "template_id": template["id"], "expected_version": 1,
              "definition": {"name": "base", "config": base | {"cpu": 16}}}
    call("update_template", **change, request_id="template-no-preview", error="先预览")
    template_preview = call("preview_template_update", **change)
    assert [item["id"] for item in template_preview["affected_tasks"]] == [task["id"]]
    assert call("update_template", **change, request_id="template-with-preview",
                preview_id=template_preview["preview_id"])["version"] == 2

    trigger = {"project_id": PROJECT, "task_id": task["id"]}
    call("trigger_task", **trigger, request_id="trigger-without-preview", error="先预览")
    trigger_preview = call("preview_task", operation="trigger", **trigger)
    assert trigger_preview["definition"]["config"]["cpu"] == 16
    run = call("trigger_task", **trigger, request_id="confirmed-trigger", preview_id=trigger_preview["preview_id"])
    assert run["state"] == "queued"
    call("delete_template", project_id=PROJECT, template_id=template["id"], request_id="blocked-remove", error="仍引用")
    call("delete_task", project_id=PROJECT, task_id=task["id"], request_id="cancel-purchase")
    assert call("delete_template", project_id=PROJECT, template_id=template["id"], request_id="remove")["deleted"]


def test_rule_archive_keeps_known_run_but_cancels_unsent(mcp):
    call, cfg = mcp
    rule = call("create_rule", project_id=PROJECT, request_id="rule-new", definition={
        "name": "short gpu run", "kind": "runtime_stop", "instance_id": INSTANCE, "config": {"duration_minutes": 30},
    })
    snapshot = {
        "project_id": PROJECT, "name": "timed stop", "kind": "stop", "channels": ["site"], "window_seconds": 60,
        "config": {"instance_id": INSTANCE, "region": "r1", "zone": "z1"},
        "trigger": {"automation_rule_id": rule["id"]},
    }
    with storage.connect(cfg.db) as conn:
        now = storage.now()
        queued = insert_run(conn, snapshot, now, now)
        sent = insert_run(conn, snapshot, now, now)
        conn.execute("UPDATE task_runs SET state='reconciling',phase='verify_stop' WHERE id=?", (sent["id"],))
    args = {"project_id": PROJECT, "rule_id": rule["id"]}
    call("delete_rule", **(args | {"project_id": OTHER}), request_id="different-project", error="不存在")
    call("delete_rule", **args, request_id="archive-rule")
    assert call("list_rules", project_id=PROJECT)["items"] == []
    assert call("get_rule", **args)["status"] == "archived"
    call("resume_rule", **args, request_id="revive", error="无法恢复")
    assert TaskStore(cfg.db).get_run(queued["id"])["state"] == "canceled"
    assert TaskStore(cfg.db).get_run(sent["id"])["state"] == "reconciling"


def test_watch_can_rebind_to_an_official_spec_and_duplicate_create_is_idempotent(mcp):
    from gpu_console.application.watches import catalog_spec_id

    call, cfg = mcp
    specification = machine_spec() | {"region": "r1", "zone": "z1"}
    key = catalog_spec_id(specification)
    original = call("create_watch", project_id=PROJECT, request_id="watch1",
                    definition={"name": "GPUs", "catalog_spec_id": key})
    duplicate = call("create_watch", project_id=PROJECT, request_id="watch2",
                     definition={"name": "Other name", "catalog_spec_id": key})
    assert duplicate["id"] == original["id"] and not duplicate["created"]
    other_key = catalog_spec_id(specification | {"gpu": 2})
    updated = call("update_watch", project_id=PROJECT, watch_id=original["id"], request_id="change-watch",
                   expected_version=original["version"], definition={"name": "GPUs", "catalog_spec_id": other_key})
    assert updated["version"] != original["version"] and updated["available"] is None
    assert updated["catalog_spec_id"] == other_key and updated["spec"]["gpu"] == 2
    with storage.connect(cfg.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM catalog_watch_bindings").fetchone()[0] == 1


def test_notification_strategy_and_site_receipt_are_scoped(mcp):
    call, cfg = mcp
    config = call("update_notification_settings", changes={"idle_threshold": 8}, request_id="idle-threshold")
    assert config["idle_threshold"] == 8 and config["channels"] == {"site": True, "feishu": False, "email": False}
    args = {"project_id": PROJECT, "channel": "site", "request_id": "site-test"}
    notice = call("test_notification", **args)
    assert call("test_notification", **args) == notice
    result = call("list_notifications", project_id=PROJECT, event_id=notice["event_id"])
    assert len(result["items"]) == 1 and "recipient" not in result["items"][0]
    identifier = result["items"][0]["id"]
    call("read_notification", project_id=OTHER, notification_id=identifier, request_id="unowned", error="找不到")
    call("read_notification", project_id=PROJECT, notification_id=identifier, request_id="owned")
    assert call("list_notifications", project_id=PROJECT, event_id=notice["event_id"])["items"][0]["read_at"]
    with storage.connect(cfg.db) as conn:
        storage.set_settings(conn, {"restore_gate": True})
    call("test_notification", **(args | {"request_id": "read-only"}), error="只读")
