"""Bind project subscriptions to catalog combinations and scheduling evidence."""

import json
from hashlib import sha256
from sqlite3 import Connection, Row
from uuid import uuid4

from gpu_console import storage
from gpu_console.storage import availability, capacity, catalog, refresh


def pool_key(spec: dict) -> tuple[str, str, str, str]:
    kind = spec.get("instance_type") or catalog.instance_type(spec.get("charge_type", "Postpay"))
    return spec["region"], spec["zone"], spec["gpu_model"], catalog.POOL_BY_INSTANCE_TYPE[kind]


def pool_rows(conn: Connection, now: int) -> dict[tuple, dict]:
    return {(row["region"], row["zone"], row["gpu_model"], row["pool"]): row
            for row in availability.pool_inventory(conn, now)}


def pool_status(spec: dict, pools: dict[tuple, dict]) -> dict:
    key = pool_key(spec)
    pool = pools.get(key, {})
    count, needed = pool.get("remaining_cards"), spec["gpu"]
    return {
        "pool": key[-1], "required_cards": needed, "remaining_cards": count,
        "state": pool.get("state", "unknown"), "provider_updated_at": pool.get("provider_updated_at"),
    }


def catalog_spec_id(spec: dict) -> str:
    return catalog.compute_spec_id(spec | {"instance_type": spec.get("instance_type")
                             or catalog.instance_type(spec.get("charge_type", "Postpay"))})


def _spec(combo: dict) -> dict:
    spec = {key: combo[key] for key in (*catalog.COMPUTE_FIELDS, "catalog_spec_id")}
    return spec | {"charge_type": "Spot" if combo["instance_type"] == "spot" else "Postpay"}


def spec_stock(spec: dict, pools: dict, profiles: dict, now: int) -> dict:
    profile = profiles.get(capacity.scope(spec))
    available, checked_at = capacity.current(profile, catalog_spec_id(spec), now)
    return pool_status(spec, pools) | {
        "available": available, "capacity_checked_at": checked_at,
        "capacity_error": profile["error"] if profile else None,
    }


def catalog_view(conn: Connection, now: int, project_id: str | None = None) -> dict:
    snapshot = storage.snapshot(conn, "catalog")
    pools = pool_rows(conn, now)
    combos = catalog.compute_specs(snapshot["data"])
    profiles = capacity.profiles(conn, capacity.read_project(conn, project_id), combos)
    by_scope: dict[tuple, dict] = {}
    for key, combo in combos.items():
        scope = tuple(combo[field] for field in catalog.COMPUTE_FIELDS[:4])
        by_scope.setdefault(scope, {})[key] = _spec(combo) | spec_stock(combo, pools, profiles, now)
    data = []
    for row in snapshot["data"]:
        scope = tuple(row.get(field) if field != "instance_type" else row.get(field) or "uhost"
                      for field in catalog.COMPUTE_FIELDS[:4])
        data.append({key: value for key, value in row.items() if key != "cpu_platforms"}
                    | {"specs": list(by_scope.get(scope, {}).values())})
    return snapshot | {"data": data}


def current_catalog(conn: Connection) -> dict[str, dict]:
    return catalog.compute_specs(storage.snapshot(conn, "catalog")["data"])


def _bound(conn: Connection, project_id: str, key: str) -> str | None:
    row = conn.execute("SELECT watch_id FROM catalog_watch_bindings WHERE project_id=? AND catalog_spec_id=?",
                       (project_id, key)).fetchone()
    return row["watch_id"] if row else None


def sync_bindings(conn: Connection) -> None:
    combos = current_catalog(conn)
    rows = conn.execute("""SELECT w.* FROM watches w
        LEFT JOIN catalog_watch_bindings b ON b.watch_id=w.id
        LEFT JOIN availability_reminders r ON r.watch_id=w.id
        WHERE b.watch_id IS NULL ORDER BY r.watch_id IS NULL,w.rowid""").fetchall()
    for row in rows:
        spec = json.loads(row["spec"])
        key = catalog_spec_id(spec)
        if key in combos:
            conn.execute("INSERT OR IGNORE INTO catalog_watch_bindings(watch_id,project_id,catalog_spec_id) VALUES (?,?,?)",
                         (row["id"], row["project_id"], key))
    for row in conn.execute("SELECT watch_id,catalog_spec_id FROM catalog_watch_bindings WHERE baseline_at IS NOT NULL"):
        if row["catalog_spec_id"] not in combos:
            conn.execute("UPDATE catalog_watch_bindings SET baseline_at=NULL,baseline_available=NULL WHERE watch_id=?",
                         (row["watch_id"],))


def view(conn: Connection, row: Row | dict, now: int, *, combos: dict | None = None, pools: dict | None = None,
         profiles: dict | None = None, history: bool = False) -> dict:
    if combos is None:
        combos = current_catalog(conn)
    if pools is None:
        pools = pool_rows(conn, now)
    if profiles is None:
        profiles = capacity.profiles(conn, row["project_id"], combos)
    saved = json.loads(row["spec"])
    key = catalog_spec_id(saved)
    primary = _bound(conn, row["project_id"], key)
    binding = "duplicate" if primary and primary != row["id"] else "bound" if primary and key in combos else "unlisted"
    spec = (_spec(combos[key]) if key in combos else
            {field: saved[field] for field in (*catalog.COMPUTE_FIELDS, "charge_type") if field in saved})
    version = sha256(json.dumps([row["name"], key], ensure_ascii=False).encode()).hexdigest()
    stock = {"pool": None, "required_cards": spec.get("gpu"), "remaining_cards": None, "state": "unknown",
             "provider_updated_at": None, "available": None, "capacity_checked_at": None, "capacity_error": None}
    if binding == "bound":
        stock = spec_stock(spec, pools, profiles, now)
    result = {key: row[key] for key in ("id", "project_id", "name")} | stock | {
        "spec": spec, "catalog_spec_id": key, "binding_status": binding, "primary_watch_id": primary,
        "version": version, "success_at": stock["capacity_checked_at"] if stock["available"] is not None else None,
        "checked_at": stock["capacity_checked_at"],
        "error": stock["capacity_error"],
    }
    if history:
        result["history"] = []
        if binding == "bound":
            series = capacity.series(profiles.get(capacity.scope(spec)), key)
            samples = capacity.history(conn, series, now - availability.WINDOW_SECONDS, now, limit=50)
            result["history"] = [
                {"sampled_at": sample["observed_at"], "available": sample["available"],
                 "remaining_cards": None, "error": None} for sample in reversed(samples)
            ]
    return result


def list_project(conn: Connection, project_id: str, now: int, *, history: bool = False) -> list[dict]:
    combos, pools = current_catalog(conn), pool_rows(conn, now)
    profiles = capacity.profiles(conn, project_id, combos)
    return [view(conn, row, now, combos=combos, pools=pools, profiles=profiles, history=history) for row in conn.execute(
        "SELECT * FROM watches WHERE project_id=? ORDER BY name,id", (project_id,))]


def save(conn: Connection, project_id: str, name: str, key: str | None, legacy: dict | None = None,
         *, watch_id: str | None = None, expected_version: str | None = None) -> tuple[dict, bool]:
    if not catalog.project_known(conn, project_id):
        raise ValueError("平台目录中没有此项目，请同步项目后重试")
    if key is None and legacy is not None:
        key = catalog_spec_id(legacy)
    now = storage.now()
    combos = current_catalog(conn)
    if key not in combos:
        raise ValueError("请选择平台目录已有的规格")
    if watch_id is not None:
        row = conn.execute("SELECT * FROM watches WHERE project_id=? AND id=?", (project_id, watch_id)).fetchone()
        if row is None:
            raise KeyError("当前项目中找不到该关注项")
        if expected_version is not None and view(conn, row, now, combos=combos)["version"] != expected_version:
            raise ValueError("关注项已变更，请刷新后重试")
    primary = _bound(conn, project_id, key)
    if primary and primary != watch_id:
        row = conn.execute("SELECT * FROM watches WHERE id=?", (primary,)).fetchone()
        return view(conn, row, now, combos=combos), False
    spec = json.dumps(_spec(combos[key]), ensure_ascii=False)
    created = watch_id is None
    if created:
        watch_id = uuid4().hex
        conn.execute("INSERT INTO watches VALUES (?,?,?,?)", (watch_id, project_id, name, spec))
    else:
        conn.execute("UPDATE watches SET name=?,spec=? WHERE id=?", (name, spec, watch_id))
        if primary != watch_id:
            conn.execute("DELETE FROM catalog_watch_bindings WHERE watch_id=?", (watch_id,))
            refresh.abandon_watch(conn, watch_id)
    profiles = capacity.profiles(conn, project_id, combos)
    stock = spec_stock(combos[key], pool_rows(conn, now), profiles, now)
    conn.execute("""INSERT OR IGNORE INTO catalog_watch_bindings
                    (watch_id,project_id,catalog_spec_id,baseline_at,baseline_available) VALUES (?,?,?,?,?)""",
                 (watch_id, project_id, key, stock["capacity_checked_at"] if stock["available"] is not None else None,
                  stock["available"]))
    row = conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
    return view(conn, row, now, combos=combos), created


def observe(conn: Connection, now: int, *, project_id: str, spec_ids: dict) -> None:
    from gpu_console.application import resource_notices

    settings = storage.get_settings(conn)
    rule = settings["notification_rules"]["watch_stock"]
    if not rule["enabled"] or settings["restore_gate"]:
        conn.execute("UPDATE catalog_watch_bindings SET baseline_at=NULL,baseline_available=NULL WHERE baseline_at IS NOT NULL")
        return
    combos, pools = current_catalog(conn), pool_rows(conn, now)
    profiles = capacity.profiles(conn, project_id, combos)
    rows = conn.execute(
        """SELECT w.*,b.catalog_spec_id,b.baseline_at,b.baseline_available FROM catalog_watch_bindings b
           JOIN watches w ON w.id=b.watch_id WHERE b.project_id=?
           AND b.catalog_spec_id IN (SELECT value FROM json_each(?))""",
        (project_id, capacity.encode(list(spec_ids))),
    )
    for row in rows.fetchall():
        spec = combos.get(row["catalog_spec_id"])
        if spec is None:
            continue
        stock = spec_stock(spec, pools, profiles, now)
        at, available = stock["capacity_checked_at"], stock["available"]
        if available is None or (row["baseline_at"] is not None and at <= row["baseline_at"]):
            continue
        if available and row["baseline_available"] == 0:
            resource_notices.emit_watch_stock(conn, row, stock, spec, settings)
        conn.execute("UPDATE catalog_watch_bindings SET baseline_at=?,baseline_available=? WHERE watch_id=?",
                     (at, available, row["id"]))
