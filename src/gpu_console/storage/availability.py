"""Persist five-minute inventory observations and resource pool records."""

import json
from collections.abc import Collection
from sqlite3 import Connection, Row

from gpu_console import storage
from gpu_console.storage import catalog

SLOT_SECONDS = 300
POOL_FRESHNESS = 2 * SLOT_SECONDS
WINDOW_SECONDS = 7 * 86400
RETENTION_SECONDS = 8 * 86400
POOL_FIELDS = ("region", "zone", "gpu_model", "pool")
PoolKey = tuple[str, str, str, str]


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS availability_catalog_items (
            key TEXT PRIMARY KEY, region TEXT NOT NULL, zone TEXT NOT NULL,
            gpu_model TEXT NOT NULL, instance_type TEXT NOT NULL, listed_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS availability_catalog_rounds (
            slot INTEGER PRIMARY KEY, observed_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS availability_catalog_samples (
            key TEXT NOT NULL REFERENCES availability_catalog_items(key) ON DELETE CASCADE,
            slot INTEGER NOT NULL REFERENCES availability_catalog_rounds(slot) ON DELETE CASCADE,
            available INTEGER CHECK(available IN (0,1)), PRIMARY KEY(key,slot)
        );
        CREATE INDEX IF NOT EXISTS availability_catalog_slots ON availability_catalog_samples(slot);
        CREATE TABLE IF NOT EXISTS availability_watch_samples (
            watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE, slot INTEGER NOT NULL,
            observed_at INTEGER NOT NULL, available INTEGER CHECK(available IN (0,1)),
            PRIMARY KEY(watch_id,slot)
        );
        CREATE INDEX IF NOT EXISTS availability_watch_slots ON availability_watch_samples(slot);
        CREATE TABLE IF NOT EXISTS availability_pool_items (
            id INTEGER PRIMARY KEY, region TEXT NOT NULL, zone TEXT NOT NULL,
            gpu_model TEXT NOT NULL, pool TEXT NOT NULL CHECK(pool IN ('exclusive','spot')),
            listed_at INTEGER NOT NULL, remaining_cards INTEGER CHECK(remaining_cards >= 0),
            provider_updated_at INTEGER, observed_at INTEGER NOT NULL,
            unsupported INTEGER NOT NULL CHECK(unsupported IN (0,1)),
            catalog_listed INTEGER NOT NULL DEFAULT 1 CHECK(catalog_listed IN (0,1)),
            UNIQUE(region,zone,gpu_model,pool)
        );
        CREATE TABLE IF NOT EXISTS availability_pool_samples (
            item_id INTEGER NOT NULL REFERENCES availability_pool_items(id) ON DELETE CASCADE,
            slot INTEGER NOT NULL, provider_updated_at INTEGER NOT NULL,
            remaining_cards INTEGER NOT NULL CHECK(remaining_cards >= 0),
            PRIMARY KEY(item_id,slot)
        );
        CREATE INDEX IF NOT EXISTS availability_pool_slots ON availability_pool_samples(slot);
        CREATE TABLE IF NOT EXISTS availability_pool_peaks (
            region TEXT NOT NULL, zone TEXT NOT NULL, gpu_model TEXT NOT NULL,
            pool TEXT NOT NULL CHECK(pool IN ('exclusive','spot')),
            cards INTEGER NOT NULL CHECK(cards > 0), provider_updated_at INTEGER NOT NULL,
            PRIMARY KEY(region,zone,gpu_model,pool)
        );
        CREATE TABLE IF NOT EXISTS availability_reminders (
            watch_id TEXT PRIMARY KEY REFERENCES watches(id) ON DELETE CASCADE,
            mode TEXT NOT NULL CHECK(mode IN ('advance','when_available')), channels TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS catalog_watch_bindings (
            watch_id TEXT PRIMARY KEY REFERENCES watches(id) ON DELETE CASCADE,
            project_id TEXT NOT NULL, catalog_spec_id TEXT NOT NULL,
            baseline_at INTEGER, baseline_available INTEGER CHECK(baseline_available IN (0,1)),
            UNIQUE(project_id,catalog_spec_id)
        );
        CREATE TABLE IF NOT EXISTS availability_migrations (name TEXT PRIMARY KEY);
    """)
    update_peak = """ON CONFLICT(region,zone,gpu_model,pool) DO UPDATE
        SET cards=excluded.cards,provider_updated_at=excluded.provider_updated_at
        WHERE excluded.cards>availability_pool_peaks.cards"""
    for event in ("INSERT", "UPDATE"):
        conn.executescript(f"""
            CREATE TRIGGER IF NOT EXISTS availability_pool_peak_{event.lower()}
            AFTER {event} ON availability_pool_samples WHEN NEW.remaining_cards>0
            BEGIN
                INSERT INTO availability_pool_peaks
                SELECT region,zone,gpu_model,pool,NEW.remaining_cards,NEW.provider_updated_at
                FROM availability_pool_items WHERE id=NEW.item_id
                {update_peak};
            END;
        """)
    timestamp = storage.now()
    with storage.transaction(conn):
        if "catalog_listed" not in {row["name"] for row in conn.execute("PRAGMA table_info(availability_pool_items)")}:
            conn.execute("ALTER TABLE availability_pool_items ADD COLUMN catalog_listed INTEGER NOT NULL DEFAULT 1 "
                         "CHECK(catalog_listed IN (0,1))")
        migrated = conn.execute("INSERT OR IGNORE INTO availability_migrations VALUES ('watch-history')")
        if migrated.rowcount:
            conn.execute(
                """INSERT OR IGNORE INTO availability_watch_samples
                   SELECT watch_id,slot,observed_at,available FROM (
                       SELECT h.watch_id,h.sampled_at / :slot * :slot AS slot,h.sampled_at AS observed_at,
                              CASE WHEN h.error IS NULL AND h.available IN (0,1) THEN h.available END AS available,
                              ROW_NUMBER() OVER (
                                  PARTITION BY h.watch_id,h.sampled_at / :slot ORDER BY h.sampled_at DESC
                              ) AS ordinal
                       FROM stock_history h JOIN watches w ON w.id=h.watch_id
                       WHERE h.sampled_at BETWEEN :since AND :now
                   ) WHERE ordinal=1""",
                {"slot": SLOT_SECONDS, "since": timestamp - RETENTION_SECONDS, "now": timestamp},
            )
        if conn.execute("INSERT OR IGNORE INTO availability_migrations VALUES ('pool-peaks-v1')").rowcount:
            conn.execute(f"""
                INSERT INTO availability_pool_peaks
                SELECT i.region,i.zone,i.gpu_model,i.pool,s.remaining_cards,s.provider_updated_at
                FROM (
                    SELECT item_id,remaining_cards,provider_updated_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY item_id ORDER BY remaining_cards DESC,provider_updated_at
                           ) AS ordinal
                    FROM availability_pool_samples WHERE remaining_cards>0
                ) s JOIN availability_pool_items i ON i.id=s.item_id
                WHERE s.ordinal=1
                {update_peak}
            """)
        saved = storage.snapshot(conn, "catalog")
        if saved["success_at"] is not None:
            reconcile_pool_catalog(conn, catalog_pools(saved["data"]))


def catalog_pools(rows: list[dict]) -> set[PoolKey]:
    return {
        (spec["region"], spec["zone"], spec["gpu_model"], catalog.POOL_BY_INSTANCE_TYPE[spec["instance_type"]])
        for spec in catalog.compute_specs(rows).values()
    }


def reconcile_pool_catalog(conn: Connection, listed: set[PoolKey]) -> None:
    """Keep observations for previously listed pools and remove legacy unlisted placeholders."""
    migrating = conn.execute("INSERT OR IGNORE INTO availability_migrations VALUES ('catalog-backed-pools-v1')").rowcount
    historical = set()
    if migrating:
        historical.update(
            (row["region"], row["zone"], row["gpu_model"], catalog.POOL_BY_INSTANCE_TYPE[row["instance_type"]])
            for row in conn.execute("SELECT region,zone,gpu_model,instance_type FROM availability_catalog_items")
            if row["instance_type"] in catalog.POOL_BY_INSTANCE_TYPE
        )
    for row in conn.execute("SELECT * FROM availability_pool_items").fetchall():
        identity = tuple(row[field] for field in POOL_FIELDS)
        active = identity in listed
        if migrating and not active and identity not in historical:
            conn.execute("DELETE FROM availability_pool_items WHERE id=?", (row["id"],))
            conn.execute(
                "DELETE FROM availability_pool_peaks WHERE region=? AND zone=? AND gpu_model=? AND pool=?", identity,
            )
        elif row["catalog_listed"] != active:
            conn.execute(
                """UPDATE availability_pool_items SET catalog_listed=?,remaining_cards=NULL,
                   provider_updated_at=NULL,unsupported=0 WHERE id=?""",
                (active, row["id"]),
            )
    saved = storage.snapshot(conn, "pool")["data"]
    references = [row for row in saved if tuple(row.get(field) for field in POOL_FIELDS) in listed]
    if len(references) != len(saved):
        conn.execute("UPDATE snapshots SET data=? WHERE kind='pool' AND scope=''",
                     (json.dumps(references, ensure_ascii=False),))


def slot_start(timestamp: int) -> int:
    return timestamp // SLOT_SECONDS * SLOT_SECONDS


def record_catalog(conn: Connection, rows: list[dict] | None, observed_at: int) -> None:
    slot = slot_start(observed_at)
    changed = conn.execute(
        """INSERT INTO availability_catalog_rounds VALUES (?,?)
           ON CONFLICT(slot) DO UPDATE SET observed_at=excluded.observed_at
           WHERE excluded.observed_at>=availability_catalog_rounds.observed_at""",
        (slot, observed_at),
    )
    if not changed.rowcount:
        return

    # A newer round replaces the entire bucket. Missing or failed results leave it unknown.
    conn.execute("DELETE FROM availability_catalog_samples WHERE slot=?", (slot,))
    statuses: dict[tuple[str, str, str, str], bool | None] = {}
    for row in rows or []:
        region, zone = row.get("region"), row.get("zone")
        model, instance_type = row.get("gpu_model"), row.get("instance_type")
        if not all(isinstance(value, str) and value for value in (region, zone, model, instance_type)):
            continue
        key = region, zone, model, instance_type
        status = {"Normal": True, "SoldOut": False}.get(row.get("status"))
        statuses[key] = status if statuses.get(key, status) == status else None

    listed = [
        (json.dumps(key, ensure_ascii=False, separators=(",", ":")), *key, observed_at, status)
        for key, status in statuses.items()
    ]
    conn.executemany(
        """INSERT INTO availability_catalog_items VALUES (?,?,?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET listed_at=MAX(availability_catalog_items.listed_at,excluded.listed_at)""",
        [row[:-1] for row in listed],
    )
    conn.executemany(
        "INSERT INTO availability_catalog_samples VALUES (?,?,?)",
        [(row[0], slot, row[-1]) for row in listed],
    )


def record_watch(conn: Connection, watch: Row | dict, available: bool | None, observed_at: int) -> None:
    conn.execute(
        """INSERT INTO availability_watch_samples VALUES (?,?,?,?)
           ON CONFLICT(watch_id,slot) DO UPDATE SET
               observed_at=excluded.observed_at,available=excluded.available
           WHERE excluded.observed_at>=availability_watch_samples.observed_at""",
        (watch["id"], slot_start(observed_at), observed_at, available),
    )


def record_pool(
    conn: Connection, region: str, observed_at: int, *, provider_updated_at: int | None = None,
    counts: dict[tuple[str, str, str], int] | None = None,
    unsupported_spot: Collection[tuple[str, str]] = (),
) -> None:
    if provider_updated_at is not None and not 0 < provider_updated_at <= observed_at:
        provider_updated_at = None

    # In a new round, an omitted model or failed result makes its previous value unknown.
    conn.execute(
        """UPDATE availability_pool_items SET remaining_cards=NULL,unsupported=0,
               provider_updated_at=?,observed_at=? WHERE region=? AND catalog_listed=1""",
        (provider_updated_at, observed_at, region),
    )
    if provider_updated_at is None:
        return

    rows = {key: (count, False) for key, count in (counts or {}).items()}
    rows.update({(zone, model, "spot"): (None, True) for zone, model in unsupported_spot})

    slot = slot_start(provider_updated_at)
    fresh = observed_at - provider_updated_at <= POOL_FRESHNESS
    for (zone, model, pool), (count, unsupported) in rows.items():
        identity = conn.execute(
            """INSERT INTO availability_pool_items
               (region,zone,gpu_model,pool,listed_at,remaining_cards,provider_updated_at,observed_at,unsupported)
               VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(region,zone,gpu_model,pool) DO UPDATE SET
                   listed_at=excluded.listed_at,remaining_cards=excluded.remaining_cards,
                   provider_updated_at=excluded.provider_updated_at,observed_at=excluded.observed_at,catalog_listed=1,
                   unsupported=excluded.unsupported RETURNING id""",
            (region, zone, model, pool, observed_at, count, provider_updated_at, observed_at, unsupported),
        ).fetchone()[0]
        if fresh and count is not None:
            conn.execute(
                """INSERT INTO availability_pool_samples VALUES (?,?,?,?)
                   ON CONFLICT(item_id,slot) DO UPDATE SET remaining_cards=excluded.remaining_cards,
                       provider_updated_at=excluded.provider_updated_at
                   WHERE excluded.provider_updated_at>availability_pool_samples.provider_updated_at""",
                (identity, slot, provider_updated_at, count),
            )


def pool_inventory(conn: Connection, now: int) -> list[dict]:
    rows = []
    for row in conn.execute(
        """SELECT region,zone,gpu_model,pool,remaining_cards,provider_updated_at,observed_at,unsupported
           FROM availability_pool_items WHERE catalog_listed=1 AND listed_at>=? ORDER BY region,zone,gpu_model,pool""",
        (now - RETENTION_SECONDS,),
    ):
        value = dict(row)
        updated_at = value["provider_updated_at"]
        unsupported = value.pop("unsupported")
        if updated_at is None or updated_at > now or (not unsupported and value["remaining_cards"] is None):
            state = "unknown"
        elif now - updated_at > POOL_FRESHNESS:
            state = "stale"
        else:
            state = "unsupported" if unsupported else "known"
        rows.append(value | {"state": state, "remaining_cards": value["remaining_cards"] if state == "known" else None})
    return rows


def pool_history(
    conn: Connection, region: str, zone: str, gpu_model: str, pool: str, since: int, now: int,
) -> list[dict]:
    return [dict(row) for row in conn.execute(
        """SELECT s.provider_updated_at,s.remaining_cards
           FROM availability_pool_items i JOIN availability_pool_samples s ON s.item_id=i.id
           WHERE i.region=? AND i.zone=? AND i.gpu_model=? AND i.pool=? AND s.slot BETWEEN ? AND ?
               AND s.provider_updated_at BETWEEN ? AND ? ORDER BY s.slot""",
        (region, zone, gpu_model, pool, slot_start(since), slot_start(now), since, now),
    )]


def trim(conn: Connection, now: int) -> None:
    cutoff = slot_start(now - RETENTION_SECONDS)
    conn.execute("DELETE FROM availability_catalog_rounds WHERE slot<?", (cutoff,))
    conn.execute("DELETE FROM availability_catalog_items WHERE listed_at<?", (cutoff,))
    conn.execute("DELETE FROM availability_watch_samples WHERE slot<?", (cutoff,))
    conn.execute("DELETE FROM availability_pool_samples WHERE slot<?", (cutoff,))
    conn.execute("DELETE FROM availability_pool_items WHERE listed_at<?", (cutoff,))


def remove_watch(conn: Connection, watch_id: str) -> None:
    conn.execute("DELETE FROM availability_watch_samples WHERE watch_id=?", (watch_id,))
    conn.execute("DELETE FROM availability_reminders WHERE watch_id=?", (watch_id,))
    conn.execute(
        """UPDATE notifications SET state='skipped',error='关注规格已删除或变更'
           WHERE (event_id GLOB 'rental-window:*' OR event_id GLOB 'watch-stock:*')
             AND state='pending' AND json_extract(payload,'$.watch_id')=?""",
        (watch_id,),
    )


def list_catalog(
    conn: Connection, filters: dict, *, key: str | None, since: int, now: int, limit: int, offset: int,
) -> tuple[list[Row], int]:
    where = """listed_at BETWEEN :since AND :now AND (:key IS NULL OR key=:key)
               AND (:region IS NULL OR region=:region) AND (:zone IS NULL OR zone=:zone)
               AND (:gpu_model IS NULL OR gpu_model=:gpu_model)
               AND (:instance_type IS NULL OR instance_type=:instance_type)"""
    params = filters | {"key": key, "since": since, "now": now, "limit": limit, "offset": offset}
    rows = conn.execute(
        f"""SELECT * FROM availability_catalog_items WHERE {where}
            ORDER BY gpu_model,region,zone,instance_type LIMIT :limit OFFSET :offset""",
        params,
    ).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM availability_catalog_items WHERE {where}", params).fetchone()[0]
    return rows, total


def list_watches(
    conn: Connection, filters: dict, *, project_id: str, key: str | None, limit: int, offset: int,
) -> tuple[list[Row], int]:
    where = """project_id=:project_id AND (:key IS NULL OR id=:key)
               AND (:region IS NULL OR json_extract(spec,'$.region')=:region)
               AND (:zone IS NULL OR json_extract(spec,'$.zone')=:zone)
               AND (:gpu_model IS NULL OR json_extract(spec,'$.gpu_model')=:gpu_model)
               AND (:instance_type IS NULL OR :instance_type=
                    CASE WHEN json_extract(spec,'$.charge_type')='Spot' THEN 'spot' ELSE 'uhost' END)"""
    params = filters | {"project_id": project_id, "key": key, "limit": limit, "offset": offset}
    rows = conn.execute(f"SELECT * FROM watches WHERE {where} ORDER BY name,id LIMIT :limit OFFSET :offset", params)
    total = conn.execute(f"SELECT COUNT(*) FROM watches WHERE {where}", params).fetchone()[0]
    return rows.fetchall(), total


def sample_counts(conn: Connection, source: str, key: str, since: int, now: int, *, hourly: bool) -> list[dict]:
    from gpu_console.storage.availability_rollups import distribution

    totals = {} if hourly else {0: {"available_count": 0, "known_count": 0}}
    for row in distribution(conn, source, key, since, now, hourly=hourly):
        item = totals.setdefault(row["start"], {"available_count": 0, "known_count": 0})
        item["available_count"] += row["value"] * row["count"]
        item["known_count"] += row["count"]
    return [({"start": start} if hourly else {}) | totals[start] for start in sorted(totals)]


def latest_catalog_round(conn: Connection, now: int) -> Row | None:
    return conn.execute(
        """SELECT slot,observed_at FROM availability_catalog_rounds
           WHERE slot<=? AND observed_at<=? ORDER BY slot DESC LIMIT 1""",
        (slot_start(now), now),
    ).fetchone()


def latest_catalog(conn: Connection, key: str, latest: Row | None) -> tuple[bool | None, int | None]:
    if latest is None:
        return None, None
    row = conn.execute(
        "SELECT available FROM availability_catalog_samples WHERE key=? AND slot=?", (key, latest["slot"]),
    ).fetchone()
    return (None if row is None or row["available"] is None else bool(row["available"])), latest["observed_at"]


def latest_watch(conn: Connection, watch_id: str, now: int) -> tuple[bool | None, int | None]:
    row = conn.execute(
        """SELECT available,observed_at FROM availability_watch_samples
           WHERE watch_id=? AND slot<=? AND observed_at<=? ORDER BY slot DESC LIMIT 1""",
        (watch_id, slot_start(now), now),
    ).fetchone()
    return (None, None) if row is None else (
        (None if row["available"] is None else bool(row["available"])), row["observed_at"],
    )


def reminder(conn: Connection, watch_id: str) -> dict:
    row = conn.execute("SELECT mode,channels FROM availability_reminders WHERE watch_id=?", (watch_id,)).fetchone()
    return {"mode": "off", "channels": ["site"]} if row is None else {
        "mode": row["mode"], "channels": json.loads(row["channels"]),
    }


def save_reminder(conn: Connection, watch_id: str, mode: str, channels: list[str]) -> None:
    if mode == "off":
        conn.execute("DELETE FROM availability_reminders WHERE watch_id=?", (watch_id,))
    else:
        conn.execute(
            """INSERT INTO availability_reminders VALUES (?,?,?) ON CONFLICT(watch_id) DO UPDATE SET
               mode=excluded.mode,channels=excluded.channels""",
            (watch_id, mode, json.dumps(channels)),
        )
    conn.execute(
        """UPDATE notifications SET state='skipped',error='订阅设置已变更'
           WHERE event_id GLOB 'rental-window:*' AND state='pending' AND json_extract(payload,'$.watch_id')=?
           AND (?='off' OR json_extract(payload,'$.mode')<>? OR channel NOT IN (SELECT value FROM json_each(?)))""",
        (watch_id, mode, mode, json.dumps(channels)),
    )
