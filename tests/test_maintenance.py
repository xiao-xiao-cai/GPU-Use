import pytest

from gpu_console import storage
from gpu_console.application.tasks import TaskStore
from gpu_console.storage import maintenance


def test_restore_blocks_writes_and_never_replays_historical_trigger(tmp_path, monkeypatch):
    db = tmp_path / "app" / "console.sqlite3"
    backup = tmp_path / "copies" / "before.sqlite3"
    storage.initialize(db)
    moment = 1000
    monkeypatch.setattr(storage, "now", lambda: moment)
    store = TaskStore(db, clock=lambda: moment)
    task = store.create_task({
        "name": "巡检", "project_id": "test", "kind": "inspection",
        "schedule": {"type": "interval", "at": 1100, "interval_seconds": 100},
        "window_seconds": 50,
    })
    with maintenance.process_lock(db.parent, ".maintenance.lock", shared=True):
        assert maintenance.backup(db, backup) == 1000
        with pytest.raises(ValueError, match="正在使用"):
            maintenance.restore(db, backup)
    assert backup.stat().st_mode & 0o777 == 0o600
    moment = 1270
    assert maintenance.restore(db, backup) == (1000, 1270)
    assert store.enqueue_due() == 0
    assert store.get_task(task["id"])["next_at"] == 1300
    assert store.list_runs()[0]["state"] == "missed"
    with storage.connect(db) as conn:
        assert storage.get_settings(conn)["restore_gate"]
    maintenance.confirm_restore(db, "模拟核对完成")
    with storage.connect(db) as conn:
        assert not storage.get_settings(conn)["restore_gate"]
        assert conn.execute("SELECT reviewed_at FROM recovery_log").fetchone()[0] == 1270
