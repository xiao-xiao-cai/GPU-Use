"""Location names and project-scoped image catalog snapshots."""

import json
from hashlib import sha256
from sqlite3 import Connection, Row

from gpu_console import storage
from gpu_console.domain.locations import REGION_NAMES, ZONE_NAMES
from gpu_console.storage import refresh

IMAGE_KINDS = ("system", "app", "custom", "shared")
IMAGE_INTERVAL = 6 * 3600
COMPUTE_FIELDS = ("region", "zone", "gpu_model", "instance_type", "gpu", "cpu", "memory_gb")
POOL_BY_INSTANCE_TYPE = {"uhost": "exclusive", "spot": "spot"}


def instance_type(charge_type: str) -> str:
    return "spot" if charge_type == "Spot" else "uhost"


def compute_spec_id(spec: dict) -> str:
    values = [spec.get(key) for key in COMPUTE_FIELDS]
    key = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return sha256(key.encode()).hexdigest()


def compute_specs(rows: list[dict]) -> dict[str, dict]:
    result = {}
    for row in rows:
        scope = {field: row.get(field) for field in COMPUTE_FIELDS[:4]}
        scope["instance_type"] = scope["instance_type"] or "uhost"
        if scope["instance_type"] not in POOL_BY_INSTANCE_TYPE or not all(scope.values()):
            continue
        for combo in row.get("specs") or []:
            counts = {field: combo.get(field) for field in COMPUTE_FIELDS[4:]}
            if not all(type(value) is int and value > 0 for value in counts.values()):
                continue
            spec = scope | counts
            key = compute_spec_id(spec)
            platforms = {value for value in (combo.get("cpu_platforms") or row.get("cpu_platforms") or [])
                         if isinstance(value, str) and value} or {"Auto"}
            current = result.setdefault(key, spec | {"catalog_spec_id": key, "cpu_platforms": []})
            current["cpu_platforms"] = sorted(set(current["cpu_platforms"]) | platforms)
    return result


def resolve_compute(conn: Connection, config: dict) -> dict:
    scope = config | {"instance_type": instance_type(config["charge_type"])}
    candidate = compute_specs(storage.snapshot(conn, "catalog")["data"]).get(compute_spec_id(scope))
    if candidate is None:
        raise ValueError("平台目录没有该 GPU、CPU 核数与内存组合，请同步目录并重新选择")
    return candidate


def automatic_platforms(spec: dict) -> list[str]:
    platforms = {value.casefold(): value for value in spec["cpu_platforms"]}
    return sorted(platforms.values(), key=lambda value: (
        value.casefold() != "auto", not value.casefold().endswith("/auto"), value.casefold(),
    ))


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS image_sources (
            scope TEXT NOT NULL, kind TEXT NOT NULL, next_at INTEGER NOT NULL,
            batch_id TEXT, PRIMARY KEY(scope,kind)
        );
        CREATE INDEX IF NOT EXISTS image_sources_due ON image_sources(next_at);
        CREATE TABLE IF NOT EXISTS image_catalog_migrations (name TEXT PRIMARY KEY);
    """)
    with storage.transaction(conn):
        migrated = conn.execute("INSERT OR IGNORE INTO image_catalog_migrations VALUES ('container-images')")
        if migrated.rowcount:
            for scope, batch_id in conn.execute("SELECT scope,MAX(batch_id) FROM image_sources GROUP BY scope").fetchall():
                if batch_id is None:
                    queue_images(conn, *json.loads(scope))
                else:
                    # Keep in-flight receipts and also refresh categories finished by the old release.
                    conn.execute(
                        "UPDATE image_sources SET next_at=MIN(next_at,?) WHERE scope=? AND batch_id IS NULL",
                        (storage.now(), scope),
                    )


def _platform_locations(conn: Connection) -> list[dict]:
    locations = storage.snapshot(conn, "locations")
    if locations["success_at"] is not None:
        return locations["data"]
    # Before the first location sync, keep locations already present in the previous release's catalog.
    return [
        {"region": row["region"], "zone": row["zone"], "name": row.get("zone_name")}
        for row in storage.snapshot(conn, "catalog")["data"] if row.get("region") and row.get("zone")
    ]


def locations(conn: Connection) -> dict:
    snapshot = storage.snapshot(conn, "locations")
    catalog = {
        (zone.rsplit("-", 1)[0], zone): {"region": zone.rsplit("-", 1)[0], "zone": zone}
        for zone in ZONE_NAMES
    }
    catalog.update({(row["region"], row["zone"]): row for row in _platform_locations(conn)})
    rows = []
    for (region, zone), row in sorted(catalog.items()):
        platform_name = row.get("name")
        rows.append({
            "region": region, "zone": zone, "region_name": REGION_NAMES.get(region, region),
            "zone_name": platform_name if platform_name and platform_name != zone else ZONE_NAMES.get(zone, zone),
            "is_pod": row.get("is_pod", False),
        })
    return snapshot | {"data": rows}


def location_known(conn: Connection, region: str, zone: str) -> bool:
    return any((row["region"], row["zone"]) == (region, zone) for row in _platform_locations(conn))


def project_known(conn: Connection, project_id: str) -> bool:
    return any(row.get("id") == project_id for row in storage.snapshot(conn, "projects")["data"])


def _image_scope(project_id: str, region: str, zone: str) -> str:
    return json.dumps([project_id, region, zone], separators=(",", ":"))


def _cache_key(scope: str, kind: str) -> str:
    return f"{kind}:{scope}"


def _pending_batch(conn: Connection, scope: str) -> str | None:
    row = conn.execute(
        "SELECT batch_id FROM image_sources WHERE scope=? AND batch_id IS NOT NULL LIMIT 1", (scope,),
    ).fetchone()
    return row["batch_id"] if row else None


def image_snapshots(conn: Connection, project_id: str, region: str, zone: str) -> dict:
    scope = _image_scope(project_id, region, zone)
    return {kind: storage.snapshot(conn, "images", _cache_key(scope, kind)) for kind in IMAGE_KINDS} | {
        "refresh_id": _pending_batch(conn, scope),
    }


def ensure_official_images(conn: Connection, project_id: str, region: str, zone: str, kind: str, now: int) -> None:
    conn.execute("INSERT OR IGNORE INTO image_sources(scope,kind,next_at) VALUES (?,?,?)",
                 (_image_scope(project_id, region, zone), kind, now))


def queue_images(conn: Connection, project_id: str, region: str, zone: str) -> str:
    scope = _image_scope(project_id, region, zone)
    pending = _pending_batch(conn, scope)
    if pending:
        return pending
    batch_id = refresh.submit(conn, "images", project_id, [_cache_key(scope, kind) for kind in IMAGE_KINDS])
    conn.executemany(
        """INSERT INTO image_sources(scope,kind,next_at,batch_id) VALUES (?,?,?,?)
           ON CONFLICT(scope,kind) DO UPDATE SET next_at=excluded.next_at,batch_id=excluded.batch_id""",
        [(scope, kind, storage.now(), batch_id) for kind in IMAGE_KINDS],
    )
    return batch_id


def due_image_source(conn: Connection, now: int) -> Row | None:
    return conn.execute(
        "SELECT * FROM image_sources WHERE next_at<=? ORDER BY next_at,rowid LIMIT 1", (now,),
    ).fetchone()


def finish_image_source(
    conn: Connection, source: Row, timestamp: int, images: list[dict] | None, error: str | None,
    *, retryable: bool = False,
) -> None:
    # A refresh requested while this page was in flight belongs to a newer batch.
    updated = conn.execute(
        """UPDATE image_sources SET next_at=?,batch_id=NULL
           WHERE scope=? AND kind=? AND batch_id IS ?""",
        (timestamp + (60 if retryable and source["kind"] in {"system", "app"} else IMAGE_INTERVAL),
         source["scope"], source["kind"], source["batch_id"]),
    )
    if not updated.rowcount:
        return
    cache_key = _cache_key(source["scope"], source["kind"])
    conn.execute(
        """INSERT INTO snapshots(kind,scope,data,success_at,error) VALUES ('images',?,?,?,?)
           ON CONFLICT(kind,scope) DO UPDATE SET
             data=CASE WHEN excluded.error IS NULL THEN excluded.data ELSE snapshots.data END,
             success_at=COALESCE(excluded.success_at,snapshots.success_at),error=excluded.error""",
        (cache_key, json.dumps(images or [], ensure_ascii=False), timestamp if error is None else None, error),
    )
    if source["batch_id"]:
        receipt = conn.execute(
            "SELECT id FROM refresh_outcomes WHERE batch_id=? AND scope=?",
            (source["batch_id"], cache_key),
        ).fetchone()
        if receipt:
            refresh.finish(conn, "images", cache_key, receipt["id"], timestamp, error)


def defer_image_page(conn: Connection, source: Row, timestamp: int) -> None:
    conn.execute(
        "UPDATE image_sources SET next_at=? WHERE scope=? AND kind=? AND batch_id IS ?",
        (timestamp + 1, source["scope"], source["kind"], source["batch_id"]),
    )
