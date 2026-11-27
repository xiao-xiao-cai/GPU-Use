from concurrent.futures import ThreadPoolExecutor

import pytest

from gpu_console import storage
from gpu_console.application.tasks import TRASH_RETENTION_SECONDS, TaskStore, TaskTrashConflict, initialize, insert_run, mark_restored
from gpu_console.worker.tasks import TaskRunner


NOW = 1_800_000_000


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "state.db"
    storage.initialize(path)
    with storage.connect(path) as conn:
        initialize(conn)
    return TaskStore(path, lambda: NOW)


def task_body(**changes):
    return {
        "name": "每日巡检",
        "kind": "inspection",
        "project_id": "p1",
        "schedule": {"type": "daily", "at": NOW},
        "config": {},
        "window_seconds": 30,
        **changes,
    }


def test_scheduled_and_manual_runs_hold_one_version_per_trigger(store):
    task = store.create_task(task_body())
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: store.enqueue_due(), range(2))) == 1
        first, duplicate = pool.map(lambda _: store.trigger_task(task["id"], "double-click"), range(2))
        assert first["id"] == duplicate["id"]
    edited = store.update_task(task["id"], task_body(name="新名称"))
    assert edited["version"] == 2
    runs = store.list_runs(task_id=task["id"])
    assert len(runs) == 2
    assert all(run["snapshot"]["version"] == 1 and run["name"] == "每日巡检" for run in runs)
    assert store.get_task(task["id"])["next_at"] == NOW + 86400


def test_restore_goes_to_first_future_occurrence(store):
    task = store.create_task(task_body())
    run = store.trigger_task(task["id"], "immediate")
    with storage.connect(store.path) as conn, storage.transaction(conn):
        mark_restored(conn, NOW + 86400)
    assert store.enqueue_due() == 0
    assert store.get_run(run["id"])["state"] == "missed"
    assert store.get_task(task["id"])["next_at"] == NOW + 2 * 86400


def test_pause_cancels_queued_attempts_and_resume_keeps_schedule(store):
    task = store.create_task(task_body())
    store.enqueue_due()
    store.control_task(task["id"], "pause")
    assert store.list_runs(task_id=task["id"])[0]["state"] == "canceled"
    with pytest.raises(ValueError):
        store.trigger_task(task["id"], "while-paused")
    assert store.control_task(task["id"], "resume")["next_at"] == NOW + 86400


def test_one_shot_archives_when_the_run_finishes_and_can_be_reused(store):
    once = store.create_task(task_body(schedule={"type": "once", "at": NOW}))
    recurring = store.create_task(task_body())
    canceled = store.create_task(task_body(name="旧版取消任务"))
    store.control_task(canceled["id"], "cancel")
    assert store.enqueue_due() == 2
    assert store.get_task(once["id"])["status"] == "completed"
    assert {task["id"] for task in store.list_tasks("p1", view="current")} == {once["id"], recurring["id"]}
    assert {task["id"] for task in store.list_tasks("p1", view="history")} == {canceled["id"]}

    inspections = type("Inspections", (), {"instances": lambda self, project, zones=None: []})()
    TaskRunner(store, inspections).tick()
    assert {task["id"] for task in store.list_tasks("p1", view="history")} == {once["id"], canceled["id"]}
    assert store.get_task(once["id"])["last_run"]["state"] == "succeeded"
    assert store.get_task(recurring["id"])["view"] == "current"
    assert store.list_tasks("p1", view="trash") == []
    restored = store.change_trash("p1", once["id"], 1, "trash")
    assert store.change_trash("p1", once["id"], restored["version"], "restore")["view"] == "history"
    store.trigger_task(once["id"], "run-again")
    assert store.get_task(once["id"])["view"] == "current"


def test_restore_skips_deleted_schedule_and_version_prevents_stale_purge(store):
    clock = [NOW]
    store = TaskStore(store.path, lambda: clock[0])
    daily = store.create_task(task_body())
    once = store.create_task(task_body(schedule={"type": "once", "at": NOW}))
    store.enqueue_due()
    daily_trash = store.change_trash("p1", daily["id"], daily["version"], "trash")
    once_trash = store.change_trash("p1", once["id"], once["version"], "trash")
    assert daily_trash["purge_at"] == NOW + TRASH_RETENTION_SECONDS
    with pytest.raises(TaskTrashConflict, match="回收站"):
        store.control_task(daily["id"], "resume")
    with pytest.raises(TaskTrashConflict, match="回收站"):
        store.trigger_task(daily["id"], "while-deleted")
    with pytest.raises(TaskTrashConflict, match="回收站"):
        store.update_task(daily["id"], task_body())
    clock[0] += 2 * 86400
    assert store.enqueue_due() == 0
    restored = store.change_trash("p1", daily["id"], daily_trash["version"], "restore")
    assert (restored["status"], restored["view"], restored["next_at"]) == ("paused", "current", NOW + 3 * 86400)
    assert store.change_trash("p1", once["id"], once_trash["version"], "restore")["view"] == "history"
    new_trash = store.change_trash("p1", daily["id"], restored["version"], "trash")
    with pytest.raises(TaskTrashConflict, match="版本"):
        store.change_trash("p1", daily["id"], daily_trash["version"], "purge")
    assert new_trash["purge_at"] == clock[0] + TRASH_RETENTION_SECONDS
    assert [run["state"] for run in store.list_runs("p1", daily["id"])] == ["canceled"]


def test_expired_trash_resumes_cleanup_after_restart_when_readback_finishes(store):
    task = store.create_task(task_body())
    run = store.trigger_task(task["id"], "in-flight")
    with storage.connect(store.path) as conn:
        conn.execute("UPDATE task_runs SET phase='verify_start',state='reconciling' WHERE id=?", (run["id"],))
    task = store.change_trash("p1", task["id"], task["version"], "trash")
    almost_expired = TaskStore(store.path, lambda: task["purge_at"] - 1)
    assert almost_expired.purge_expired_tasks() == 0
    restarted = TaskStore(store.path, lambda: task["purge_at"])
    assert restarted.purge_expired_tasks() == 0
    with pytest.raises(TaskTrashConflict, match="期限已过"):
        restarted.change_trash("p1", task["id"], task["version"], "restore")
    with pytest.raises(TaskTrashConflict, match="待完成"):
        restarted.change_trash("p1", task["id"], task["version"], "purge")
    with storage.connect(store.path) as conn:
        conn.execute("UPDATE task_runs SET phase=NULL,state='succeeded' WHERE id=?", (run["id"],))

    def restore():
        with pytest.raises((KeyError, TaskTrashConflict)):
            restarted.change_trash("p1", task["id"], task["version"], "restore")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(restore), pool.submit(restarted.purge_expired_tasks)]
        assert [future.result() for future in futures] == [None, 1]
    assert restarted.purge_expired_tasks() == 0
    assert restarted.get_run(run["id"])["task_id"] is None
    assert restarted.list_runs("p1", task["id"])[0]["id"] == run["id"]


def test_restoring_recurring_shutdown_skips_the_future_run_that_was_already_queued(store):
    task = store.create_task(task_body(
        kind="shutdown",
        config={"instance_id": "instance1", "region": "r", "zone": "z"},
        schedule={"type": "daily", "at": NOW + 1200},
    ))
    assert store.enqueue_due() == 1
    trashed = store.change_trash("p1", task["id"], task["version"], "trash")
    restored = store.change_trash("p1", task["id"], trashed["version"], "restore")
    assert (restored["status"], restored["next_at"]) == ("paused", NOW + 1200 + 86400)
    assert [run["state"] for run in store.list_runs("p1", task["id"])] == ["canceled"]


def test_migration_preserves_legacy_runs_ownership_and_purchase_evidence(store):
    task = store.create_task(task_body())
    parent = store.trigger_task(task["id"], "before-upgrade")
    with storage.connect(store.path) as conn, storage.transaction(conn):
        linked = insert_run(conn, {
            "project_id": "p1", "name": "旧关机预约", "kind": "shutdown",
            "config": {"parent_run_id": parent["id"], "shutdown_at": NOW + 1200},
        }, NOW + 1200, NOW, request_id="legacy-linked", state="succeeded")
        conn.execute("UPDATE task_runs SET state='succeeded' WHERE id=?", (parent["id"],))
        conn.execute(
            "INSERT INTO shutdown_owners VALUES ('p1','i1',?,NULL,'r1','z1',?)", (linked["id"], NOW + 1200),
        )
        conn.execute("DROP INDEX task_runs_source")
        conn.execute("ALTER TABLE task_runs DROP COLUMN source_task_id")
        conn.execute("ALTER TABLE task_runs DROP COLUMN cancel_shutdown_on_confirmation")
        conn.execute("DROP INDEX tasks_trash_due")
        conn.execute("ALTER TABLE tasks DROP COLUMN deleted_at")
        conn.execute("ALTER TABLE tasks DROP COLUMN status_before_delete")
        conn.execute("DROP TABLE recovery_purchases")
        conn.execute("CREATE TABLE recovery_purchases(task_id TEXT PRIMARY KEY REFERENCES tasks(id), adjustment INTEGER NOT NULL)")
        conn.execute("INSERT INTO recovery_purchases VALUES (?,1)", (task["id"],))
    storage.initialize(store.path)
    storage.initialize(store.path)
    assert store.get_run(linked["id"])["source_task_id"] == task["id"]
    assert store.get_task(task["id"])["pending_reason"] == "仍有关机预约待处理"
    with storage.connect(store.path) as conn:
        conn.execute("DELETE FROM shutdown_owners")
    trashed = store.change_trash("p1", task["id"], task["version"], "trash")
    assert store.change_trash("p1", task["id"], trashed["version"], "purge")["purged"]
    assert {run["id"] for run in store.list_runs("p1", task["id"])} == {parent["id"], linked["id"]}
    with storage.connect(store.path) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT adjustment FROM recovery_purchases WHERE task_id=?", (task["id"],)).fetchone()[0] == 1


def test_immediate_action_request_replays_and_disallows_conflicting_body(store):
    body = {"kind": "start", "project_id": "p1", "config": {"region": "r", "zone": "z", "instance_id": "id"}}
    run = store.submit_action(body, "token")
    assert store.submit_action(body, "token")["id"] == run["id"]
    with pytest.raises(ValueError):
        store.submit_action(body | {"kind": "stop"}, "token")


def test_shutdown_target_is_previewed_in_shanghai_and_enqueued_in_advance(store):
    body = task_body(
        kind="shutdown",
        config={"instance_id": "instance1", "region": "r", "zone": "z"},
        schedule={"type": "once", "at": NOW + 1200},
    )
    preview = store.preview(body)
    assert preview["deadline_at"] == NOW + 900
    assert preview["scheduled_at_shanghai"].endswith("+08:00")
    task = store.create_task(body)
    assert store.enqueue_due() == 1
    assert store.list_runs(task_id=task["id"])[0]["scheduled_at"] == NOW + 1200
