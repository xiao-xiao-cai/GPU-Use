"""Persist hourly counts of observed inventory values."""

from sqlite3 import Connection, Row

from gpu_console import storage
from gpu_console.storage.availability import pool_inventory, slot_start

HOUR_SECONDS = 3600
SOURCES = {
    "pool": ("availability_pool_samples", "item_id", "remaining_cards", "s.provider_updated_at"),
    "capacity": ("capacity_samples", "key", "available", "s.observed_at"),
    "watch": ("availability_watch_samples", "watch_id", "available", "s.observed_at"),
    "catalog": ("availability_catalog_samples", "key", "available", "r.observed_at"),
}


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS availability_hourly_counts (
            source TEXT NOT NULL, key TEXT NOT NULL, hour INTEGER NOT NULL, value INTEGER NOT NULL,
            count INTEGER NOT NULL CHECK(count>0), PRIMARY KEY(source,key,hour,value)
        );
    """)
    for source, (table, key, value, _) in SOURCES.items():
        new_hour = f"NEW.slot / {HOUR_SECONDS} * {HOUR_SECONDS}"
        old_bucket = (f"source='{source}' AND key=OLD.{key}"
                      f" AND hour=OLD.slot / {HOUR_SECONDS} * {HOUR_SECONDS} AND value=OLD.{value}")
        increase = f"""
            INSERT INTO availability_hourly_counts
            SELECT '{source}',NEW.{key},{new_hour},NEW.{value},1 WHERE NEW.{value} IS NOT NULL
            ON CONFLICT(source,key,hour,value) DO UPDATE SET count=availability_hourly_counts.count+1;
        """
        decrease = f"""
            DELETE FROM availability_hourly_counts WHERE {old_bucket} AND count=1;
            UPDATE availability_hourly_counts SET count=count-1 WHERE {old_bucket};
        """
        conn.executescript(f"""
            CREATE TRIGGER IF NOT EXISTS availability_hourly_{source}_insert
            AFTER INSERT ON {table} BEGIN {increase} END;
            CREATE TRIGGER IF NOT EXISTS availability_hourly_{source}_delete
            AFTER DELETE ON {table} BEGIN {decrease} END;
            CREATE TRIGGER IF NOT EXISTS availability_hourly_{source}_update
            AFTER UPDATE OF {value} ON {table} WHEN OLD.{value} IS NOT NEW.{value}
            BEGIN {decrease} {increase} END;
        """)

    with storage.transaction(conn):
        if not conn.execute("INSERT OR IGNORE INTO availability_migrations VALUES ('hourly-counts-v1')").rowcount:
            return
        conn.execute("DELETE FROM availability_hourly_counts")
        for source, (table, key, value, _) in SOURCES.items():
            conn.execute(
                f"""INSERT INTO availability_hourly_counts
                    SELECT ?,{key},slot / {HOUR_SECONDS} * {HOUR_SECONDS} AS hour,{value},COUNT(*)
                    FROM {table} WHERE {value} IS NOT NULL GROUP BY {key},hour,{value}""", (source,),
            )


def distribution(
    conn: Connection, source: str, key: str, since: int, now: int, *, hourly: bool,
) -> list[Row]:
    """Read complete hours from the rollup and preserve exact inclusive time boundaries."""
    if since > now:
        return []
    table, key_field, value, observed = SOURCES[source]
    raw_from = f"{table} s"
    if source == "catalog":
        raw_from += " JOIN availability_catalog_rounds r ON r.slot=s.slot"
    raw = f"""SELECT s.slot / {HOUR_SECONDS} * {HOUR_SECONDS} AS hour,s.{value} AS value,COUNT(*) AS count
              FROM {raw_from} WHERE s.{key_field}=? AND s.slot BETWEEN ? AND ?
                AND {observed} BETWEEN ? AND ? AND s.{value} IS NOT NULL GROUP BY hour,s.{value}"""
    first_full = (since + HOUR_SECONDS - 1) // HOUR_SECONDS * HOUR_SECONDS
    after_full = (now + 1) // HOUR_SECONDS * HOUR_SECONDS
    clauses, params = [], []
    edges = [(since, now)]
    if first_full < after_full:
        clauses.append("SELECT hour,value,count FROM availability_hourly_counts WHERE source=? AND key=? AND hour>=? AND hour<?")
        params.extend((source, key, first_full, after_full))
        edges = [(since, first_full - 1), (after_full, now)]
    for start, end in edges:
        if start <= end:
            clauses.append(raw)
            params.extend((key, slot_start(start), slot_start(end), start, end))
    hour = "hour" if hourly else "0"
    grouped = "hour,value" if hourly else "value"
    return conn.execute(
        f"SELECT {hour} AS start,value,SUM(count) AS count FROM (" + " UNION ALL ".join(clauses) + f") GROUP BY {grouped}",
        params,
    ).fetchall()


def pool_distribution(
    conn: Connection, region: str, zone: str, gpu_model: str, pool: str, since: int, now: int, *, hourly: bool,
) -> list[Row]:
    item = conn.execute(
        "SELECT id FROM availability_pool_items WHERE region=? AND zone=? AND gpu_model=? AND pool=?",
        (region, zone, gpu_model, pool),
    ).fetchone()
    return distribution(conn, "pool", str(item["id"]), since, now, hourly=hourly) if item else []


def pool_references(conn: Connection, now: int, totals: dict[tuple, tuple[int, int]]) -> list[dict]:
    """Materialize one reference per pool at the existing inventory collection cadence."""
    references = []
    peaks = {(row["region"], row["zone"], row["gpu_model"], row["pool"]): row
             for row in conn.execute("SELECT * FROM availability_pool_peaks")}
    for row in pool_inventory(conn, now):
        if row["state"] != "known":
            continue
        identity = {field: row[field] for field in ("region", "zone", "gpu_model", "pool")}
        key = tuple(identity.values())
        updated_at, total = totals.get(key, (None, None))
        if (updated_at == row["provider_updated_at"] and type(total) is int
                and total > 0 and total >= row["remaining_cards"]):
            reference = {"cards": total, "source": "platform_total", "sample_count": None}
        else:
            peak = peaks.get(key)
            if peak is None or peak["cards"] < row["remaining_cards"]:
                continue
            reference = {"cards": peak["cards"], "source": "observed_peak", "sample_count": None,
                         "peak_at": peak["provider_updated_at"]}
        references.append(identity | {"provider_updated_at": row["provider_updated_at"], "reference": reference})
    return references
