import json

import pytest

from gpu_console import storage
from test_task_runner import acquire, runtime


def template(db, *, version=1, charge_type="Postpay", quantity=None, gpu=1):
    config = acquire(charge_type=charge_type, quantity=quantity, gpu=gpu)["config"]
    with storage.connect(db.path) as conn:
        conn.execute(
            """INSERT INTO templates(id,project_id,name,config,version) VALUES ('template-1','p1','单卡',?,?)
               ON CONFLICT(id) DO UPDATE SET config=excluded.config,version=excluded.version""",
            (json.dumps(config), version),
        )
    return config


def task_body(**fields):
    return acquire() | {
        "template_id": "template-1", "template_version": 1,
        "config": {"max_price": "5", "purchase_limit": 2, "skip_if_match": False},
        **fields,
    }


def test_updated_template_applies_only_to_future_run_snapshots(runtime):
    db, _, clock, _ = runtime
    template(db)
    task = db.create_task(task_body())
    first = db.trigger_task(task["id"], "first")
    template(db, version=2, gpu=2)
    clock.advance()
    second = db.trigger_task(task["id"], "second")
    assert first["snapshot"]["config"]["gpu"] == 1
    assert first["snapshot"]["template_version"] == 1
    assert second["snapshot"]["config"]["gpu"] == 2
    assert second["snapshot"]["template_version"] == 2
    assert second["snapshot"]["config"]["max_price"] == "5"
    assert second["snapshot"]["config"]["purchase_limit"] == 2


def test_stale_template_preview_requires_latest_version(runtime):
    db, _, _, _ = runtime
    template(db, version=2)
    with pytest.raises(ValueError, match="模板已更新"):
        db.preview(task_body())
    preview = db.preview(task_body(template_version=2))
    assert preview["template_version"] == 2
    assert preview["max_price"] == "5"


def test_billing_change_cannot_reinterpret_saved_budget(runtime):
    db, cloud, clock, runner = runtime
    template(db)
    task = db.create_task(task_body())
    template(db, version=2, charge_type="Day", quantity=1)
    blocked = db.trigger_task(task["id"], "would-reinterpret")
    assert blocked["state"] == "manual_review"
    assert "重新预览并保存预算" in blocked["reason"]
    assert not cloud.writes
    clock.advance()
    editable = task_body(template_version=2, schedule={"type": "once", "at": clock.now})
    db.update_task(task["id"], editable)
    confirmed = db.trigger_task(task["id"], "new-budget")
    assert confirmed["state"] == "queued"
    assert confirmed["snapshot"]["config"]["charge_type"] == "Day"


@pytest.mark.parametrize("legacy", [False, True])
def test_only_previewed_repair_resolves_blocked_trigger_without_rescheduling(runtime, legacy):
    db, cloud, clock, runner = runtime
    template(db)
    task = db.create_task(task_body())
    template(db, version=2, charge_type="Day", quantity=1)
    runner.tick()
    [blocked] = db.list_runs(project_id="p1", attention=True)
    assert blocked["business"]["destination"]["task_id"] == task["id"]
    if legacy:
        with storage.connect(db.path) as conn:
            conn.execute("UPDATE task_runs SET result='{}' WHERE id=?", (blocked["id"],))
    completed = db.get_task(task["id"])
    assert completed["status"] == "completed" and completed["next_at"] is None
    clock.advance(3600)
    repaired = task_body(template_version=2)
    unpreviewed = db.update_task(task["id"], repaired)
    assert db.get_run(blocked["id"])["state"] == "manual_review"
    edit = {"task_id": task["id"], "task_version": unpreviewed["version"]}
    first = db.preview(repaired | edit)
    latest = db.preview(repaired | edit)
    assert latest["scheduled_at"] is None and latest["deadline_at"] is None
    with pytest.raises(ValueError, match="重新预览"):
        db.update_task(task["id"], repaired | {"preview_token": first["preview_token"]})
    with pytest.raises(ValueError, match="重新预览"):
        db.update_task(task["id"], repaired | {"name": "未预览的修改", "preview_token": latest["preview_token"]})
    saved = db.update_task(task["id"], repaired | {"preview_token": latest["preview_token"]})
    assert saved["status"] == "completed" and saved["next_at"] is None
    assert db.list_runs(project_id="p1", attention=True) == []
    resolved = db.get_run(blocked["id"])
    assert resolved["state"] == "reviewed" and resolved["snapshot"] == blocked["snapshot"]
    assert resolved["result"]["task_config_resolution"] == {"task_version": saved["version"], "checked_at": clock.now}
    assert resolved["audit"][-1]["data"]["original_reason"] == blocked["reason"]
    assert runner.tick() == 0 and cloud.writes == []
    assert [run["id"] for run in db.list_runs(task_id=task["id"])] == [blocked["id"]]


def test_template_cannot_be_read_outside_project(runtime):
    db, _, _, _ = runtime
    template(db)
    with pytest.raises(ValueError, match="模板"):
        db.create_task(task_body(project_id="p2"))


def test_legacy_purchase_uses_original_cpu_until_previewed_edit(runtime):
    db, _, _, _ = runtime
    legacy = acquire(cpu_platform="Intel/Auto")
    task = db.create_task(legacy)
    run = db.trigger_task(task["id"], "legacy-locked")
    assert run["snapshot"]["config"]["cpu_platform"] == "Intel/Auto"
    edit = legacy | {"config": {key: value for key, value in legacy["config"].items() if key != "cpu_platform"}}
    with pytest.raises(ValueError, match="重新预览"):
        db.update_task(task["id"], edit)
    preview = db.preview(edit | {"task_id": task["id"], "task_version": task["version"]})
    saved = db.update_task(task["id"], edit | {"preview_token": preview["preview_token"]})
    assert saved["config"]["cpu_auto"] and saved["config"]["cpu_platform"] == "Auto"
    assert db.get_run(run["id"])["snapshot"]["config"]["cpu_platform"] == "Intel/Auto"
