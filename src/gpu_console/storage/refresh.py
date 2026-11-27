"""Persist collection requests and record the outcome of each requested scope."""

from sqlite3 import Connection
from uuid import uuid4

from gpu_console import storage


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS refresh_batches (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, project_id TEXT,
            requested_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS refresh_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL REFERENCES refresh_batches(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, scope TEXT NOT NULL, finished_at INTEGER, error TEXT,
            UNIQUE(batch_id, scope)
        );
        CREATE INDEX IF NOT EXISTS refresh_outcomes_pending ON refresh_outcomes(kind, scope, id)
            WHERE finished_at IS NULL;
    """)


def submit(conn: Connection, kind: str, project_id: str | None, scopes: list[str]) -> str:
    batch_id = uuid4().hex
    conn.execute("INSERT INTO refresh_batches VALUES (?,?,?,?)", (batch_id, kind, project_id, storage.now()))
    conn.executemany(
        "INSERT INTO refresh_outcomes(batch_id,kind,scope) VALUES (?,?,?)",
        [(batch_id, kind, scope) for scope in scopes],
    )
    return batch_id


def get(conn: Connection, batch_id: str) -> dict | None:
    batch = conn.execute("SELECT * FROM refresh_batches WHERE id=?", (batch_id,)).fetchone()
    if batch is None:
        return None
    outcomes = [dict(row) for row in conn.execute(
        "SELECT scope,finished_at,error FROM refresh_outcomes WHERE batch_id=? ORDER BY id", (batch_id,),
    )]
    state = "queued" if any(row["finished_at"] is None for row in outcomes) else (
        "failed" if any(row["error"] for row in outcomes) else "succeeded"
    )
    return dict(batch) | {"state": state, "outcomes": outcomes}


def pending(conn: Connection) -> dict[tuple[str, str], int]:
    """Capture the latest request the core collector may complete for each scope."""
    return {(row["kind"], row["scope"]): row["through_id"] for row in conn.execute(
        """SELECT kind,scope,MAX(id) AS through_id FROM refresh_outcomes
           WHERE finished_at IS NULL AND kind<>'images' GROUP BY kind,scope""",
    )}


def finish(
    conn: Connection, kind: str, scope: str, through_id: int, timestamp: int, error: str | None,
) -> None:
    if through_id:
        conn.execute(
            """UPDATE refresh_outcomes SET finished_at=?,error=?
               WHERE kind=? AND scope=? AND id<=? AND finished_at IS NULL""",
            (timestamp, error, kind, scope, through_id),
        )


def abandon_watch(conn: Connection, watch_id: str) -> None:
    conn.execute(
        """UPDATE refresh_outcomes SET finished_at=?,error='关注项已删除'
           WHERE kind='stock' AND scope=? AND finished_at IS NULL""",
        (storage.now(), watch_id),
    )


def finish_unavailable(conn: Connection) -> None:
    conn.execute(
        """UPDATE refresh_outcomes SET finished_at=?,error='优云智算凭据未配置'
           WHERE finished_at IS NULL AND kind<>'images'""",
        (storage.now(),),
    )


def trim(conn: Connection, cutoff: int) -> None:
    conn.execute(
        """DELETE FROM refresh_batches WHERE id IN (
            SELECT b.id FROM refresh_batches AS b WHERE b.requested_at<? AND NOT EXISTS (
                SELECT 1 FROM refresh_outcomes AS o WHERE o.batch_id=b.id AND o.finished_at IS NULL
            ) LIMIT 5000
        )""",
        (cutoff,),
    )
