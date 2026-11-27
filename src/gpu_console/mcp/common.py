"""Persist MCP mutation receipts and short-lived, single-use operation previews."""

import hashlib
import json
import re
import secrets
from collections.abc import Callable, Sequence
from sqlite3 import Connection

from gpu_console import storage
from gpu_console.config import Config
from gpu_console.mcp import auth

REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,100}")


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mcp_receipts (
            credential_id TEXT NOT NULL, request_id TEXT NOT NULL, operation TEXT NOT NULL,
            payload_hash TEXT NOT NULL, project_id TEXT, result TEXT NOT NULL, created_at INTEGER NOT NULL,
            PRIMARY KEY(credential_id,request_id)
        );
        CREATE TABLE IF NOT EXISTS mcp_previews (
            id TEXT PRIMARY KEY, credential_id TEXT NOT NULL, operation TEXT NOT NULL,
            data TEXT NOT NULL, expires_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS mcp_previews_expiry ON mcp_previews(expires_at);
    """)


def clear_previews(conn: Connection) -> None:
    conn.execute("DELETE FROM mcp_previews")


def _encode(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def require_manager(conn: Connection) -> auth.Principal:
    principal = auth.current_principal.get()
    if principal is None or not auth.is_current(conn, principal.credential_id):
        raise ValueError("MCP 令牌已失效，请更新客户端凭据")
    if not principal.manage:
        raise ValueError("这个 MCP Key 缺少管理权限")
    return principal


def mutate(
    config: Config,
    operation: str,
    request_id: str,
    data: dict,
    execute: Callable[[Connection], dict],
    *,
    allow_read_only: bool = False,
) -> dict:
    """Save the edit and its receipt atomically, or replay the original receipt."""
    if not REQUEST_ID.fullmatch(request_id):
        raise ValueError("request_id 须为 1 至 100 个字母、数字或 _ . : - 字符")
    fingerprint = hashlib.sha256(_encode(data).encode()).hexdigest()
    with storage.shared_write(config.db) as conn:
        key_id = require_manager(conn).key_id
        # Existing receipt column stores the stable key identity across rotations.
        previous = conn.execute(
            "SELECT operation,payload_hash,result FROM mcp_receipts WHERE credential_id=? AND request_id=?",
            (key_id, request_id),
        ).fetchone()
        if previous:
            if previous["operation"] != operation or previous["payload_hash"] != fingerprint:
                raise ValueError("request_id 已关联另一项请求，请使用新的标识")
            return json.loads(previous["result"])
        if not allow_read_only and (config.read_only or storage.get_settings(conn)["restore_gate"]):
            raise ValueError("当前处于只读或恢复核对状态，暂不能修改 GPU 管理台")
        result = execute(conn)
        conn.execute(
            "INSERT INTO mcp_receipts VALUES (?,?,?,?,?,?,?)",
            (key_id, request_id, operation, fingerprint, data.get("project_id") or result.get("project_id"),
             _encode(result), storage.now()),
        )
        return result


def issue_preview(config: Config, operation: str, data: dict, *, ttl: int = 300) -> str:
    preview_id = secrets.token_urlsafe(24)
    timestamp = storage.now()
    with storage.connect(config.db) as conn, storage.transaction(conn):
        credential_id = require_manager(conn).credential_id
        conn.execute("DELETE FROM mcp_previews WHERE expires_at<=?", (timestamp,))
        conn.execute(
            "INSERT INTO mcp_previews VALUES (?,?,?,?,?)",
            (preview_id, credential_id, operation, _encode(data), timestamp + ttl),
        )
    return preview_id


def read_preview(conn: Connection, operation: str, preview_id: str) -> dict:
    """Consume a preview inside the successful operation's write transaction."""
    if not conn.in_transaction:
        raise RuntimeError("MCP preview consumption requires a write transaction")
    credential_id = require_manager(conn).credential_id
    row = conn.execute(
        "DELETE FROM mcp_previews WHERE id=? AND credential_id=? AND operation=? AND expires_at>? RETURNING data",
        (preview_id, credential_id, operation, storage.now()),
    ).fetchone()
    if not row:
        raise ValueError("预览无效、已使用或已过期，请重新预览")
    return json.loads(row["data"])


def page(items: Sequence[dict], limit: int = 50, offset: int = 0) -> dict:
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("分页大小须为 1 至 100，起始位置须大于等于 0")
    total = len(items)
    selected = list(items[offset:offset + limit])
    next_offset = offset + len(selected)
    has_more = next_offset < total
    return {"items": selected, "count": len(selected), "total": total, "has_more": has_more,
            "next_offset": next_offset if has_more else None}
