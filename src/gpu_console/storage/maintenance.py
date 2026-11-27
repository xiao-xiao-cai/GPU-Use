"""Online SQLite copies and deliberate recovery from older snapshots."""

import fcntl
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from gpu_console import storage
from gpu_console.application.availability import cancel_restored
from gpu_console.application.automation_rules import restore_boundary
from gpu_console.application.feishu_notices import cancel_restored as cancel_restored_feishu
from gpu_console.application.tasks import mark_restored
from gpu_console.mcp import auth as mcp_auth, common as mcp_tools
from gpu_console.storage import dashboard_billed_cost_rollups, dashboard_cost, dashboard_cost_rollups, dashboard_usage


@contextmanager
def process_lock(directory: Path, name: str, *, shared: bool = False, blocking: bool = False) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / name).open("a") as lock:
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(lock, operation if blocking else operation | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("数据目录正在使用，请确认 web、mcp 和 worker 进程状态") from None
        yield


def copy_database(source: Path, destination: Path) -> None:
    with storage.connect(destination) as target:
        conn = sqlite3.connect(source.absolute().as_uri() + "?mode=ro", uri=True)
        try:
            conn.backup(target, pages=128, sleep=0.05)
        finally:
            conn.close()


def backup(path: Path, output: Path) -> int:
    if output.exists() or output.resolve() == path.resolve():
        raise ValueError("备份目标须为不存在的文件")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = storage.now()
    with process_lock(path.parent, ".maintenance.lock", shared=True):
        with TemporaryDirectory(dir=output.parent) as temp:
            copy = Path(temp) / "backup.sqlite3"
            os.close(os.open(copy, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            copy_database(path, copy)
            with storage.connect(copy) as conn, storage.transaction(conn):
                conn.execute("CREATE TABLE gpu_backup_metadata (backup_at INTEGER NOT NULL)")
                conn.execute("INSERT INTO gpu_backup_metadata VALUES (?)", (timestamp,))
            os.link(copy, output)
    return timestamp


def restore(path: Path, source: Path) -> tuple[int, int]:
    if source.resolve() == path.resolve() or not source.is_file():
        raise ValueError("需要 gpu-admin backup 创建的备份文件")
    restored_at = storage.now()
    with process_lock(path.parent, ".maintenance.lock"):
        with TemporaryDirectory(dir=path.parent) as temp:
            prepared = Path(temp) / "restore.sqlite3"
            try:
                copy_database(source, prepared)
                with storage.connect(prepared) as conn:
                    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("备份数据库完整性验证失败")
                    row = conn.execute("SELECT backup_at FROM gpu_backup_metadata").fetchone()
                    if row is None or not isinstance(row["backup_at"], int) or not 0 < row["backup_at"] <= restored_at:
                        raise ValueError("备份时间无效")
                    backup_at = row["backup_at"]
            except sqlite3.Error:
                raise ValueError("无法读取管理台备份或其时间戳") from None
            storage.initialize(prepared)
            with storage.connect(prepared) as conn, storage.transaction(conn):
                conn.execute("DROP TABLE gpu_backup_metadata")
                conn.execute("DELETE FROM sessions")
                conn.execute("DELETE FROM login_attempts")
                mcp_auth.clear_on_restore(conn)
                mcp_tools.clear_previews(conn)
                conn.execute("DELETE FROM instance_resize_quotes")
                conn.execute("DELETE FROM system_status")
                conn.execute("UPDATE snapshots SET error='恢复后等待下一次同步'")
                storage.set_settings(conn, {"restore_gate": True, "feishu_policy_since": restored_at})
                mark_restored(conn, restored_at)
                restore_boundary(conn, restored_at)
                dashboard_usage.restore_boundary(conn, restored_at)
                dashboard_cost.restore_boundary(conn, restored_at)
                dashboard_cost_rollups.reset(conn)
                dashboard_billed_cost_rollups.reset(conn)
                cancel_restored(conn)
                cancel_restored_feishu(conn)
                conn.execute("INSERT INTO recovery_log(backup_at,restored_at) VALUES (?,?)", (backup_at, restored_at))
            previous_exists = path.exists()
            copy_database(prepared, path)
            if not previous_exists:
                path.chmod(0o600)
    return backup_at, restored_at


def restore_purchases(path: Path, task_id: str, additional: int) -> None:
    if additional < 0:
        raise ValueError("必须填写已核对的新增订单次数")
    with storage.connect(path) as conn, storage.transaction(conn):
        if not storage.get_settings(conn)["restore_gate"]:
            raise ValueError("当前没有需要核对的备份恢复")
        row = conn.execute(
            "SELECT json_extract(definition,'$.kind') FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None or row[0] != "acquire":
            raise ValueError("采购任务不存在")
        conn.execute(
            """INSERT INTO recovery_purchases(task_id,adjustment) VALUES (?,?)
               ON CONFLICT(task_id) DO UPDATE SET adjustment=excluded.adjustment""",
            (task_id, additional),
        )


def confirm_restore(path: Path, note: str) -> None:
    note = note.strip()
    if not note or len(note) > 1000:
        raise ValueError("需要记录已核对的采购订单、云端实例和预约情况")
    with storage.connect(path) as conn, storage.transaction(conn):
        if not storage.get_settings(conn)["restore_gate"]:
            raise ValueError("当前没有需要核对的备份恢复")
        timestamp = storage.now()
        if not conn.execute(
            """UPDATE recovery_log SET reviewed_at=?,note=?
               WHERE id=(SELECT MAX(id) FROM recovery_log) AND reviewed_at IS NULL""",
            (timestamp, note),
        ).rowcount:
            raise ValueError("找不到待核对的恢复记录")
        mark_restored(conn, timestamp)
        restore_boundary(conn, timestamp)
        storage.set_settings(conn, {"restore_gate": False})
