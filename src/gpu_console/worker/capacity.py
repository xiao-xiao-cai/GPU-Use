"""Check one group of catalog specifications at a time through CompShare."""

from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError, CompShare
from gpu_console.application import watches
from gpu_console.application.purchase_options import create_options
from gpu_console.storage import capacity, catalog, refresh

REQUEST_GAP = 10
RATE_LIMIT_BACKOFF = 30


@dataclass
class Target:
    project_id: str
    scope: capacity.Scope
    members: dict[str, dict]
    image_kind: str
    probe: dict | None
    error: str | None


def _targets(conn: Connection, now: int, groups: dict[capacity.Scope, dict]) -> list[Target]:
    targets = []
    pod_locations = {(row["region"], row["zone"]) for row in storage.snapshot(conn, "locations")["data"]
                     if row.get("is_pod") is True}
    for project in storage.snapshot(conn, "projects")["data"]:
        project_id = project["id"]
        images_by_zone = {}
        for identity, members in groups.items():
            region, zone, model, kind = identity
            pod = (region, zone) in pod_locations
            image_kind = "app" if pod else "system"
            if (region, zone) not in images_by_zone:
                image_snapshot = catalog.image_snapshots(conn, project_id, region, zone)[image_kind]
                stamp = image_snapshot["success_at"]
                images = (image_snapshot["data"]
                          if stamp is not None and 0 <= now - stamp <= catalog.IMAGE_INTERVAL * 2
                          and not image_snapshot["error"] else [])
                images_by_zone[(region, zone)] = sorted(
                    (image for image in images if image.get("id") and (image.get("runtime") == "container") == pod),
                    key=lambda image: ("ubuntu" not in image.get("name", "").casefold(), image["id"]),
                )
            images = images_by_zone[(region, zone)]
            probe = None
            image_label = "容器镜像" if pod else "系统镜像"
            error = "平台默认系统盘或镜像兼容性待核实" if images else f"无可用官方{image_label}"
            for image in images:
                options = create_options(conn, project_id, region, zone, model, kind, image["id"])
                if options["system_disk"] is not None:
                    probe = {
                        "project_id": project_id, "region": region, "zone": zone, "gpu_model": model,
                        "charge_type": "Spot" if kind == "spot" else "Postpay", "cpu_platform": "Auto",
                        "image_id": image["id"], "disks": [options["system_disk"]],
                    }
                    error = None
                    break
            targets.append(Target(project_id, identity, members, image_kind, probe, error))
    return targets


class CapacityCollector:
    def __init__(self, path: Path, cloud: CompShare):
        self.path = path
        self.cloud = cloud
        self.next_tick = 0

    def tick(self) -> None:
        now = storage.now()
        if now < self.next_tick:
            return
        self.next_tick = now + REQUEST_GAP
        with storage.connect(self.path) as conn:
            with storage.transaction(conn, immediate=False):
                combos = watches.current_catalog(conn)
                targets = _targets(conn, now, capacity.groups(combos))
            with storage.transaction(conn):
                sources = {(target.project_id, *target.scope[:2], target.image_kind) for target in targets}
                for project, region, zone, kind in sources:
                    catalog.ensure_official_images(conn, project, region, zone, kind, now)
                pending = self._pending(conn, combos, now)
                request = self._select(conn, targets, pending, now)
        if request is None:
            return
        target, version, receipts = request
        assert target.probe is not None

        try:
            matrix, error, backoff = self.cloud.capacity_matrix(target.probe), None, False
        except CloudError as failure:
            matrix, error, backoff = {}, str(failure), failure.code == "429"
        checked_at = storage.now()
        if backoff:
            self.next_tick = max(self.next_tick, checked_at + RATE_LIMIT_BACKOFF)
        results = capacity.project_results(target.members, matrix)
        interval = RATE_LIMIT_BACKOFF if backoff else capacity.INTERVAL
        with storage.connect(self.path) as conn, storage.transaction(conn):
            # A catalog update while the request was in flight invalidates its refresh receipts.
            members = capacity.groups(watches.current_catalog(conn)).get(target.scope)
            if members is None or members.keys() != target.members.keys():
                return
            if not capacity.record(conn, target.project_id, target.scope, version, results,
                                   checked_at, max(now + interval, checked_at), error):
                return
            for watch_id, key, through_id in receipts:
                conn.execute("DELETE FROM refresh_requests WHERE kind='stock' AND scope=?", (watch_id,))
                refresh.finish(conn, "stock", watch_id, through_id, checked_at,
                               error or ("平台未明确返回该规格是否可租" if results[key] is None else None))
            watches.observe(conn, checked_at, project_id=target.project_id, spec_ids=target.members)

    @staticmethod
    def _pending(conn: Connection, combos: dict, now: int) -> dict[tuple, list[tuple[str, str, int]]]:
        grouped = {}
        for row in conn.execute("""
            WITH pending AS (
                SELECT scope,MAX(id) AS through_id FROM refresh_outcomes
                WHERE kind='stock' AND finished_at IS NULL GROUP BY scope
                UNION ALL SELECT scope,0 FROM refresh_requests WHERE kind='stock'
            ), requests AS (SELECT scope,MAX(through_id) AS through_id FROM pending GROUP BY scope)
            SELECT r.*,b.project_id,b.catalog_spec_id,
                   EXISTS(SELECT 1 FROM watches WHERE id=r.scope) AS watch_exists
            FROM requests r LEFT JOIN catalog_watch_bindings b ON b.watch_id=r.scope
        """).fetchall():
            key = row["catalog_spec_id"]
            if key not in combos or not catalog.project_known(conn, row["project_id"]):
                error = "请重新绑定平台目录规格" if row["watch_exists"] else "关注项已删除"
                refresh.finish(conn, "stock", row["scope"], row["through_id"], now, error)
                conn.execute("DELETE FROM refresh_requests WHERE kind='stock' AND scope=?", (row["scope"],))
                continue
            identity = row["project_id"], capacity.scope(combos[key])
            grouped.setdefault(identity, []).append((row["scope"], key, row["through_id"]))
        return grouped

    @staticmethod
    def _select(
        conn: Connection, targets: list[Target], pending: dict[tuple, list], now: int,
    ) -> tuple[Target, str, list] | None:
        existing = {(row["project_id"], row["scope"]): row for row in conn.execute("SELECT * FROM capacity_profiles")}
        due = []
        for target in targets:
            identity = target.project_id, capacity.encode(target.scope)
            old = existing.pop(identity, None)
            members = capacity.encode(sorted(target.members))
            unchanged_scope = old is not None and old["members"] == members
            version = (old["revision"] if target.probe is None and unchanged_scope else
                       capacity.revision(target.project_id, target.scope, members, target.probe))
            receipts = pending.get((target.project_id, target.scope), [])
            changed = old is None or old["revision"] != version
            if changed:
                conn.execute(
                    """INSERT INTO capacity_profiles(project_id,scope,members,revision,next_at,error)
                       VALUES (?,?,?,?,?,?) ON CONFLICT(project_id,scope) DO UPDATE SET
                         members=excluded.members,revision=excluded.revision,next_at=excluded.next_at,
                         checked_at=NULL,results='{}',error=excluded.error""",
                    (*identity, members, version, now, target.error),
                )
                conn.execute(
                    """UPDATE catalog_watch_bindings SET baseline_at=NULL,baseline_available=NULL
                       WHERE project_id=? AND catalog_spec_id IN (SELECT value FROM json_each(?))""",
                    (target.project_id, members),
                )
            if target.probe is None:
                if not changed and (old["results"] != "{}" or old["error"] != target.error):
                    conn.execute("UPDATE capacity_profiles SET results='{}',error=? WHERE project_id=? AND scope=?",
                                 (target.error, *identity))
                for watch_id, _, through_id in receipts:
                    refresh.finish(conn, "stock", watch_id, through_id, now, target.error)
                    conn.execute("DELETE FROM refresh_requests WHERE kind='stock' AND scope=?", (watch_id,))
                continue
            next_at = now if changed else old["next_at"]
            if receipts and not changed:
                # Interactive refresh shares the queue. An already sampled group can be advanced
                # once per minute without letting repeated clicks displace scheduled groups.
                next_at = min(next_at, max((old["checked_at"] or 0) + 60, now))
            if next_at <= now:
                due.append((next_at, not bool(receipts), identity, target, version, receipts))
        conn.executemany("DELETE FROM capacity_profiles WHERE project_id=? AND scope=?", existing)
        if not due:
            return None
        _, _, _, target, version, receipts = min(due, key=lambda item: item[:3])
        return target, version, receipts
