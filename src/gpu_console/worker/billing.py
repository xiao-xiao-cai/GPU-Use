"""Read complete paid-order cycles and verify project and instance attribution."""

import json
from collections import Counter, defaultdict
from itertools import groupby
from pathlib import Path
from sqlite3 import Connection, IntegrityError

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.domain.billing import Category, Order
from gpu_console.storage import billing, instance_history, storage_cost

COMPUTE = {"uhost", "compsharecpod"}
SYSTEM_DISKS = {"udisk_system_disk", "OBJ_TYPE_SYS_UDISK_SSD", "compsharecvolumn"}
STORAGE = SYSTEM_DISKS | {"udisk", "udisk_ssd", "rssd", "ufs", "ufile", "snapshot", "UFS-SMB",
                          "SSD Essential", "RSSD Essential"}
IMAGES = {"uimage", "compshareImage", "commImage"}


def _inventory(conn: Connection, project_id: str) -> tuple[set[str], set[str], dict[str, str]]:
    instances = instance_history.list_project(conn, project_id) + storage.snapshot(conn, "instances", project_id)["data"]
    storage_rows = storage.snapshot(conn, "storage", project_id)["data"]
    known = {row["id"] for row in instances + storage_rows}
    disk_hosts: dict[str, set[str]] = defaultdict(set)
    for instance in instances:
        for disk in instance.get("disks") or ():
            if disk.get("id"):
                known.add(disk["id"])
                if disk.get("purpose") == "system":
                    disk_hosts[disk["id"]].add(instance["id"])
    fixed_owners = {disk: next(iter(hosts)) for disk, hosts in disk_hosts.items() if len(hosts) == 1}
    return known, {row["id"] for row in storage_rows}, fixed_owners


def _mounts(
    conn: Connection, project_id: str, resources: set[str], since: int | None,
) -> dict[str, list[tuple[int, int, str]]]:
    spans: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    if not resources or since is None:
        return spans
    restored = billing.restored_at(conn)
    rows = conn.execute("""
        SELECT * FROM storage_cost_samples WHERE project_id=? AND sampled_at>=?
        ORDER BY region,zone,kind,sampled_at
    """, (project_id, since - storage_cost.MAX_GAP))
    for _, scope in groupby(rows, lambda row: (row["region"], row["zone"], row["kind"])):
        previous = None
        for row in scope:
            at = row["sampled_at"]
            current = json.loads(row["resources"]) if row["resources"] is not None else {}
            if previous and at - previous[0] <= storage_cost.MAX_GAP and not previous[0] < restored <= at:
                start, before = previous
                for key in before.keys() & current.keys() & resources:
                    if owner := storage_cost._exclusive_mount(before[key], current[key]):
                        seen = spans[key]
                        if seen and seen[-1][1] == start and seen[-1][2] == owner:
                            start_at, _, _ = seen[-1]
                            seen[-1] = (start_at, at, owner)
                        else:
                            seen.append((start, at, owner))
            previous = at, current
    return spans


def _category(order: Order, storage_ids: set[str]) -> Category:
    if order.resource_type in COMPUTE:
        return "compute"
    if order.resource_type in IMAGES:
        return "image"
    if order.resource_type in STORAGE or order.resource_id in storage_ids:
        return "storage"
    return "other"


def _attribution(
    conn: Connection, project_id: str, cycle: str, orders: list[Order], storage_ids: set[str], system_owners: dict[str, str],
) -> list[tuple[Order, Category, str | None]]:
    classified = [(order, _category(order, storage_ids)) for order in orders]
    independent = [order for order, category in classified if category == "storage"
                   and order.resource_id and order.resource_type not in SYSTEM_DISKS and order.start_at and order.end_at]
    spans = _mounts(conn, project_id, {item.resource_id for item in independent},
                    min((item.start_at for item in independent), default=None))
    previous = {row["order_no"]: row for row in conn.execute("""
        SELECT order_no,resource_id,resource_type,start_at,end_at,category,instance_id FROM billing_orders
        WHERE project_id=? AND cycle=?
    """, (project_id, cycle))}
    result = []
    for order, category in classified:
        owner = None
        saved = previous.get(order.order_no)
        same_resource = saved and (order.resource_id, order.resource_type, order.start_at, order.end_at) == (
            saved["resource_id"], saved["resource_type"], saved["start_at"], saved["end_at"],
        )
        if category == "other" and same_resource:
            category = saved["category"]
        if category == "compute":
            owner = order.resource_id
        elif category == "storage" and order.resource_type in SYSTEM_DISKS:
            owner = system_owners.get(order.resource_id)
        elif category == "storage" and order.start_at and order.end_at and order.start_at < order.end_at:
            overlapping = [(start, end, host) for start, end, host in spans.get(order.resource_id, ())
                           if start < order.end_at and end > order.start_at]
            if len(overlapping) == 1:
                start, end, host = overlapping[0]
                if start <= order.start_at and end >= order.end_at:
                    owner = host
        if owner is None and same_resource:
            owner = saved["instance_id"]
        result.append((order, category, owner))
    return result


class BillingCollector:
    def __init__(self, path: Path, cloud) -> None:
        self.path = path
        self.cloud = cloud

    def tick(self) -> None:
        attempted = storage.now()
        with storage.connect(self.path) as conn:
            snapshot = storage.snapshot(conn, "projects")
            if snapshot["error"]:
                return
            projects = snapshot["data"]
            job = billing.next_sync(conn, projects, attempted)
            if job is None or not billing.start_sync(conn, job[0]["id"], job[1], attempted):
                return
        self.sync(*job, projects, attempted)

    def sync(self, project: dict, cycle: str, projects: list[dict], attempted: int) -> None:
        project_id, name = project["id"], project.get("name") or project["id"]
        try:
            with storage.connect(self.path) as conn:
                provider, aliases = billing.binding(conn, project_id)
            counts = Counter(item.get("name") or item["id"] for item in projects)
            names = list(dict.fromkeys((name, *aliases)))
            if any(counts[alias] > int(alias == name) for alias in names):
                raise ValueError("账单项目名称冲突")
            rows, verified_names = {}, set(aliases)
            for alias in names:
                items = self.cloud.billing_orders(alias, cycle)
                if items:
                    verified_names.add(alias)
                for order in items:
                    if order.order_no in rows:
                        raise ValueError("账期内出现重复订单")
                    rows[order.order_no] = order
            orders = list(rows.values())
            with storage.connect(self.path) as conn, storage.transaction(conn, immediate=False):
                known, storage_ids, system_owners = _inventory(conn, project_id)
                billing_ids = {row.billing_project_id for row in orders}
                if orders and provider is None:
                    if len(billing_ids) != 1 or not any(row.resource_id in known for row in orders):
                        raise ValueError("账单项目归属待核对")
                    provider = next(iter(billing_ids))
                if orders and billing_ids != {provider}:
                    raise ValueError("账单项目 ID 与已核实的项目不一致")
                attributed = _attribution(conn, project_id, cycle, orders, storage_ids, system_owners)
            with storage.connect(self.path) as conn, storage.transaction(conn):
                billing.publish(conn, project_id, cycle, attempted, storage.now(), provider, sorted(verified_names), attributed)
        except (CloudError, IntegrityError, ValueError) as error:
            if isinstance(error, IntegrityError):
                diagnostic = "账单项目已有绑定或订单跨账期重复"
            elif isinstance(error, CloudError):
                diagnostic = str(error)
            else:
                diagnostic = str(error)
            with storage.connect(self.path) as conn:
                billing.sync_failed(conn, project_id, cycle, attempted, diagnostic)
