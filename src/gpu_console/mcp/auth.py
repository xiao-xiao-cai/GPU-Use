"""Issue and verify independently revocable MCP keys."""

import hashlib
import re
import secrets
import sqlite3
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from gpu_console import storage

TOKEN_PATTERN = re.compile(r"gpu_mcp_[A-Za-z0-9_-]{43}")
KEY_ID_PATTERN = r"^[0-9a-f]{32}$"
USAGE_INTERVAL = 300
SCHEMA = """
    CREATE TABLE mcp_credentials (
        id INTEGER PRIMARY KEY,
        credential_id TEXT NOT NULL UNIQUE,
        token_hash TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL,
        key_id TEXT UNIQUE,
        name TEXT NOT NULL DEFAULT '旧版 MCP Key',
        manage INTEGER NOT NULL DEFAULT 1,
        ssh INTEGER NOT NULL DEFAULT 1,
        expires_at INTEGER,
        last_used_at INTEGER,
        revoked_at INTEGER
    )
"""
ACTIVE = "key_id IS NOT NULL AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)"


@dataclass(frozen=True)
class Principal:
    credential_id: str
    key_id: str
    manage: bool
    ssh: bool


current_principal: ContextVar[Principal | None] = ContextVar("gpu_mcp_principal", default=None)


def initialize(conn: sqlite3.Connection) -> None:
    with storage.transaction(conn):
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(mcp_credentials)")}
        if not columns:
            conn.execute(SCHEMA)
        elif "key_id" not in columns:
            conn.execute("ALTER TABLE mcp_credentials RENAME TO legacy_mcp_credentials")
            conn.execute(SCHEMA)
            conn.execute("""
                INSERT INTO mcp_credentials(id,credential_id,token_hash,created_at,key_id)
                SELECT 2,credential_id,token_hash,created_at,credential_id FROM legacy_mcp_credentials
            """)
            conn.execute("DROP TABLE legacy_mcp_credentials")
        # ID 1 is reserved for the singleton on releases predating multi-key support.
        conn.execute("""
            UPDATE mcp_credentials SET id=MAX(2,(SELECT MAX(id)+1 FROM mcp_credentials)),key_id=credential_id
            WHERE id=1 AND key_id IS NULL
        """)


def clear_on_restore(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM mcp_credentials")


def require_principal() -> Principal:
    principal = current_principal.get()
    if principal is None:
        raise ValueError("需要有效的 MCP 令牌")
    return principal


def is_current(conn: sqlite3.Connection, credential_id: str, *, ssh: bool = False) -> bool:
    return conn.execute(
        f"SELECT 1 FROM mcp_credentials WHERE credential_id=? AND {ACTIVE} AND (NOT ? OR ssh=1)",
        (credential_id, storage.now(), ssh),
    ).fetchone() is not None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def authenticate(db: Path, token: str | None) -> Principal | None:
    if token is None or not TOKEN_PATTERN.fullmatch(token):
        return None
    now = storage.now()
    with storage.connect(db) as conn:
        row = conn.execute(
            f"SELECT credential_id,key_id,manage,ssh,last_used_at FROM mcp_credentials WHERE token_hash=? AND {ACTIVE}",
            (_digest(token), now),
        ).fetchone()
        if row is None:
            return None
        if row["last_used_at"] is None or row["last_used_at"] <= now - USAGE_INTERVAL:
            conn.execute(
                f"""UPDATE mcp_credentials SET last_used_at=? WHERE credential_id=? AND {ACTIVE}
                    AND (last_used_at IS NULL OR last_used_at<=?)""",
                (now, row["credential_id"], now, now - USAGE_INTERVAL),
            )
    return Principal(row["credential_id"], row["key_id"], bool(row["manage"]), bool(row["ssh"]))


def _public(row: sqlite3.Row, now: int) -> dict:
    status = "active"
    if row["revoked_at"] is not None:
        status = "revoked"
    elif row["expires_at"] is not None and row["expires_at"] <= now:
        status = "expired"
    return {
        "id": row["key_id"], "name": row["name"], "manage": bool(row["manage"]), "ssh": bool(row["ssh"]),
        "created_at": row["created_at"], "expires_at": row["expires_at"], "last_used_at": row["last_used_at"],
        "status": status,
    }


def list_keys(conn: sqlite3.Connection) -> list[dict]:
    now = storage.now()
    return [_public(row, now) for row in conn.execute(
        "SELECT * FROM mcp_credentials WHERE key_id IS NOT NULL ORDER BY created_at DESC,id DESC"
    )]


def _key(conn: sqlite3.Connection, key_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM mcp_credentials WHERE key_id=?", (key_id,)).fetchone()
    if row is None:
        raise KeyError("找不到 MCP Key")
    return row


def get_key(conn: sqlite3.Connection, key_id: str) -> dict:
    return _public(_key(conn, key_id), storage.now())


def issue(
    conn: sqlite3.Connection, *, name: str, manage: bool, ssh: bool, days: int | None, key_id: str | None = None,
) -> dict:
    token = "gpu_mcp_" + secrets.token_urlsafe(32)
    generation = secrets.token_hex(16)
    now = storage.now()
    expires = now + days * 86400 if days is not None else None
    if key_id is None:
        key_id = secrets.token_hex(16)
        conn.execute("""
            INSERT INTO mcp_credentials(id,credential_id,token_hash,created_at,key_id,name,manage,ssh,expires_at)
            VALUES ((SELECT MAX(2,COALESCE(MAX(id),0)+1) FROM mcp_credentials),?,?,?,?,?,?,?,?)
        """, (generation, _digest(token), now, key_id, name, manage, ssh, expires))
    else:
        previous = _key(conn, key_id)
        if previous["revoked_at"] is not None:
            raise ValueError("已撤销的 Key 不能轮换，请签发新 Key")
        conn.execute("""
            UPDATE mcp_credentials SET credential_id=?,token_hash=?,created_at=?,name=?,manage=?,ssh=?,
                expires_at=?,last_used_at=NULL WHERE key_id=?
        """, (generation, _digest(token), now, name, manage, ssh, expires, key_id))
        conn.execute("DELETE FROM mcp_previews WHERE credential_id=?", (previous["credential_id"],))
    return {"key": _public(_key(conn, key_id), now), "token": token}


def revoke(conn: sqlite3.Connection, key_id: str) -> None:
    row = _key(conn, key_id)
    conn.execute(
        "UPDATE mcp_credentials SET revoked_at=? WHERE key_id=? AND revoked_at IS NULL", (storage.now(), key_id),
    )
    conn.execute("DELETE FROM mcp_previews WHERE credential_id=?", (row["credential_id"],))
