"""Persist project-scoped scheduling evidence for catalog configurations."""

import json
from hashlib import sha256
from sqlite3 import Connection

from gpu_console import storage
from gpu_console.storage import availability, catalog

INTERVAL = availability.SLOT_SECONDS
FRESHNESS = 2 * INTERVAL
Scope = tuple[str, str, str, str]


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS capacity_profiles (
            project_id TEXT NOT NULL, scope TEXT NOT NULL, members TEXT NOT NULL,
            revision TEXT NOT NULL, checked_at INTEGER, next_at INTEGER NOT NULL,
            results TEXT NOT NULL DEFAULT '{}', error TEXT,
            PRIMARY KEY(project_id,scope)
        );
        CREATE TABLE IF NOT EXISTS capacity_samples (
            key TEXT NOT NULL, slot INTEGER NOT NULL, observed_at INTEGER NOT NULL,
            available INTEGER CHECK(available IN (0,1)), PRIMARY KEY(key,slot)
        );
        CREATE INDEX IF NOT EXISTS capacity_sample_slots ON capacity_samples(slot);
    """)
    with storage.transaction(conn):
        if conn.execute("INSERT OR IGNORE INTO availability_migrations VALUES ('scheduling-evidence-v1')").rowcount:
            conn.execute("UPDATE catalog_watch_bindings SET baseline_at=NULL,baseline_available=NULL")
            conn.execute("""
                UPDATE notifications SET state='skipped',error='库存判断依据已升级'
                WHERE state='pending' AND (
                    event_id GLOB 'watch-stock:*' OR event_id GLOB 'rental-window:*'
                    OR event_id IN (
                        SELECT id || ':result' FROM task_runs WHERE json_extract(snapshot,'$.kind')='stock'
                            AND json_extract(snapshot,'$.config.stock_pool')=1
                    )
                )
            """)


def encode(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def scope(spec: dict) -> Scope:
    kind = spec.get("instance_type") or catalog.instance_type(spec.get("charge_type", "Postpay"))
    return spec["region"], spec["zone"], spec["gpu_model"], kind


def groups(combos: dict[str, dict]) -> dict[Scope, dict[str, dict]]:
    result: dict[Scope, dict[str, dict]] = {}
    for key, combo in combos.items():
        result.setdefault(scope(combo), {})[key] = combo
    return result


def read_project(conn: Connection, project_id: str | None) -> str | None:
    if project_id is not None:
        return project_id
    projects = {row["id"] for row in storage.snapshot(conn, "projects")["data"]}
    default = storage.get_settings(conn)["project_id"]
    if default in projects:
        return default
    return next(iter(projects)) if len(projects) == 1 else None


def profiles(conn: Connection, project_id: str | None, combos: dict[str, dict]) -> dict[Scope, dict]:
    if project_id is None:
        return {}
    members = groups(combos)
    results = {}
    for row in conn.execute("SELECT * FROM capacity_profiles WHERE project_id=?", (project_id,)):
        identity = tuple(json.loads(row["scope"]))
        if identity in members and row["members"] == encode(sorted(members[identity])):
            results[identity] = dict(row) | {"results": json.loads(row["results"])}
    return results


def revision(project_id: str, identity: Scope, members: str, probe: dict | None) -> str:
    return sha256(encode([project_id, identity, members, probe]).encode()).hexdigest()


def card_key(cards: int) -> str:
    return f"cards:{cards}"


def series(profile: dict | None, key: str) -> str | None:
    return f"{profile['revision']}:{key}" if profile is not None else None


def current(profile: dict | None, key: str, now: int) -> tuple[bool | None, int | None]:
    checked_at = profile["checked_at"] if profile is not None and key in profile["results"] else None
    if checked_at is None or not 0 <= now - checked_at <= FRESHNESS:
        return None, checked_at
    return profile["results"].get(key), checked_at


def project_results(members: dict[str, dict], matrix: dict[tuple[int, int, int], bool | None]) -> dict[str, bool | None]:
    results, by_cards = {}, {}
    for key, spec in members.items():
        result = matrix.get((spec["gpu"], spec["cpu"], spec["memory_gb"]))
        results[key] = result
        by_cards.setdefault(spec["gpu"], []).append(result)
    for cards, values in by_cards.items():
        results[card_key(cards)] = True if True in values else False if all(value is False for value in values) else None
    return results


def record(
    conn: Connection, project_id: str, identity: Scope, version: str, results: dict[str, bool | None],
    checked_at: int, next_at: int, error: str | None,
) -> bool:
    updated = conn.execute(
        """UPDATE capacity_profiles SET checked_at=?,next_at=?,results=?,error=?
           WHERE project_id=? AND scope=? AND revision=? AND (checked_at IS NULL OR checked_at<=?)""",
        (checked_at, next_at, encode(results), error, project_id, encode(identity), version, checked_at),
    )
    if not updated.rowcount:
        return False
    conn.executemany(
        """INSERT INTO capacity_samples VALUES (?,?,?,?) ON CONFLICT(key,slot) DO UPDATE SET
               available=excluded.available,observed_at=excluded.observed_at
           WHERE excluded.observed_at>=capacity_samples.observed_at
               AND (excluded.available IS NOT NULL OR capacity_samples.available IS NULL)""",
        [(f"{version}:{key}", availability.slot_start(checked_at), checked_at, answer) for key, answer in results.items()],
    )
    return True


def history(
    conn: Connection, key: str | None, since: int, now: int, *, known_only: bool = False, limit: int = 5000,
) -> list[dict]:
    if key is None:
        return []
    rows = conn.execute(
        """SELECT slot,observed_at,available FROM capacity_samples
           WHERE key=? AND slot BETWEEN ? AND ? AND observed_at BETWEEN ? AND ?
             AND (NOT ? OR available IS NOT NULL) ORDER BY slot DESC LIMIT ?""",
        (key, availability.slot_start(since), availability.slot_start(now), since, now, known_only, limit),
    ).fetchall()
    return [{"slot": row["slot"], "observed_at": row["observed_at"],
             "available": None if row["available"] is None else bool(row["available"])} for row in reversed(rows)]


def trim(conn: Connection, now: int) -> None:
    conn.execute("DELETE FROM capacity_samples WHERE slot<?", (availability.slot_start(now - availability.RETENTION_SECONDS),))
