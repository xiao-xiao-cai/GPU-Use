"""Store verified CompShare orders and read cash summaries for one console project."""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from sqlite3 import Connection, IntegrityError, Row
from typing import Iterator

from gpu_console.domain.billing import CATEGORIES, SERVICE_ORDERS, Amounts, Basis, Category, Order, amount_view, apportioned, cycles
from gpu_console.storage.dashboard_usage import PERIOD_DAYS, UsageRange, _local_date, _midnight

SYNC_WINDOW = (5 * 3600, 6 * 3600)
SYNC_INTERRUPTED = "账单同步未完成"
ZERO: Amounts = (0, 0, 0, 0)


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS billing_projects (
            project_id TEXT PRIMARY KEY, billing_project_id TEXT NOT NULL UNIQUE, names TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS billing_sync (
            project_id TEXT NOT NULL, cycle TEXT NOT NULL, attempted_at INTEGER NOT NULL,
            synced_at INTEGER, error TEXT, PRIMARY KEY(project_id,cycle)
        );
        CREATE TABLE IF NOT EXISTS billing_orders (
            billing_project_id TEXT NOT NULL, order_no TEXT NOT NULL, project_id TEXT NOT NULL, cycle TEXT NOT NULL,
            order_type TEXT NOT NULL, resource_id TEXT, resource_type TEXT NOT NULL, name TEXT, charge_type TEXT,
            created_at INTEGER, start_at INTEGER, end_at INTEGER, category TEXT NOT NULL, instance_id TEXT,
            cash_cents INTEGER, order_cents INTEGER, coupon_cents INTEGER, credit_cents INTEGER,
            PRIMARY KEY(billing_project_id,order_no)
        );
        CREATE INDEX IF NOT EXISTS billing_orders_cycle ON billing_orders(project_id,cycle);
        CREATE INDEX IF NOT EXISTS billing_orders_date ON billing_orders(project_id,created_at);
        CREATE INDEX IF NOT EXISTS billing_orders_service ON billing_orders(project_id,end_at);
    """)


def restored_at(conn: Connection) -> int:
    return conn.execute("SELECT MAX(restored_at) FROM recovery_log").fetchone()[0] or 0


def next_sync(conn: Connection, projects: list[dict], now: int) -> tuple[dict, str] | None:
    today = _local_date(now)
    midnight = _midnight(today)
    if not SYNC_WINDOW[0] <= now - midnight < SYNC_WINDOW[1]:
        return None
    records = {(row["project_id"], row["cycle"]): row for row in conn.execute("SELECT * FROM billing_sync")}
    restore = restored_at(conn)
    due = []
    for index, cycle in enumerate(cycles(today)):
        for project in projects:
            record = records.get((project["id"], cycle))
            if record is None:
                due.append((index, 0, project["id"], project, cycle))
                continue
            synced, attempted = record["synced_at"], record["attempted_at"]
            if attempted < midnight and (index < 2 or record["error"] or synced is None or synced < restore):
                due.append((index, attempted, project["id"], project, cycle))
    if not due:
        return None
    _, _, _, project, cycle = min(due, key=lambda job: job[:3])
    return project, cycle


def binding(conn: Connection, project_id: str) -> tuple[str | None, list[str]]:
    row = conn.execute("SELECT * FROM billing_projects WHERE project_id=?", (project_id,)).fetchone()
    return (row["billing_project_id"], json.loads(row["names"])) if row else (None, [])


def start_sync(conn: Connection, project_id: str, cycle: str, attempted: int) -> bool:
    return bool(conn.execute("""
        INSERT INTO billing_sync VALUES (?,?,?,NULL,?)
        ON CONFLICT(project_id,cycle) DO UPDATE SET attempted_at=excluded.attempted_at,error=excluded.error
        WHERE billing_sync.attempted_at<?
    """, (project_id, cycle, attempted, SYNC_INTERRUPTED, _midnight(_local_date(attempted)))).rowcount)


def sync_failed(conn: Connection, project_id: str, cycle: str, attempted: int, error: str) -> None:
    conn.execute("""
        INSERT INTO billing_sync VALUES (?,?,?,NULL,?)
        ON CONFLICT(project_id,cycle) DO UPDATE SET attempted_at=excluded.attempted_at,error=excluded.error
        WHERE billing_sync.attempted_at<excluded.attempted_at
           OR (billing_sync.attempted_at=excluded.attempted_at AND billing_sync.error=?)
    """, (project_id, cycle, attempted, error, SYNC_INTERRUPTED))


def publish(
    conn: Connection, project_id: str, cycle: str, attempted: int, now: int,
    provider_id: str | None, names: list[str], rows: list[tuple[Order, Category, str | None]],
) -> None:
    """Publish a complete month in the caller's write transaction."""
    previous = conn.execute("SELECT attempted_at,error FROM billing_sync WHERE project_id=? AND cycle=?",
                            (project_id, cycle)).fetchone()
    if attempted < restored_at(conn) or (previous is not None and (
        previous["attempted_at"] > attempted
        or (previous["attempted_at"] == attempted and previous["error"] != SYNC_INTERRUPTED)
    )):
        return
    bound, _ = binding(conn, project_id)
    if bound is not None and bound != provider_id:
        raise ValueError("账单项目 ID 与已核实的项目不一致")
    if provider_id is not None:
        conn.execute("""
            INSERT INTO billing_projects VALUES (?,?,?)
            ON CONFLICT(project_id) DO UPDATE SET names=excluded.names
        """, (project_id, provider_id, json.dumps(names, ensure_ascii=False)))
    # Apply the complete snapshot without rewriting orders whose persisted fields stayed the same.
    existing = {tuple(row) for row in conn.execute(
        "SELECT * FROM billing_orders WHERE project_id=? AND cycle=?", (project_id, cycle),
    )}
    incoming = [
        (order.billing_project_id, order.order_no, project_id, cycle, order.order_type, order.resource_id,
         order.resource_type, order.resource_name, order.charge_type, order.created_at, order.start_at, order.end_at,
         category, instance_id, *(order.amounts if order.amounts is not None else (None,) * 4))
        for order, category, instance_id in rows
    ]
    if len({row[:2] for row in incoming}) != len(incoming):
        raise IntegrityError("UNIQUE constraint failed: billing_orders.billing_project_id, billing_orders.order_no")
    conn.executemany(
        "DELETE FROM billing_orders WHERE billing_project_id=? AND order_no=?",
        (row[:2] for row in existing.difference(incoming)),
    )
    conn.executemany("INSERT INTO billing_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        row for row in incoming if row not in existing
    ))
    conn.execute("""
        INSERT INTO billing_sync VALUES (?,?,?,?,NULL)
        ON CONFLICT(project_id,cycle) DO UPDATE SET
            attempted_at=excluded.attempted_at,synced_at=excluded.synced_at,error=NULL
    """, (project_id, cycle, attempted, now))


@dataclass
class Totals:
    amounts: Amounts = ZERO
    orders: int = 0
    review_orders: int = 0

    def add(self, amounts: Amounts | None) -> None:
        self.orders += 1
        if amounts is None:
            self.review_orders += 1
        else:
            a, b, c, d = self.amounts
            self.amounts = a + amounts[0], b + amounts[1], c + amounts[2], d + amounts[3]

    def view(self, covered: bool) -> dict:
        known = self.orders > self.review_orders or (covered and not self.orders)
        return amount_view(self.amounts if known else None) | {"order_count": self.orders, "review_order_count": self.review_orders}


@dataclass
class Group:
    total: Totals = field(default_factory=Totals)
    categories: dict[str, Totals] = field(default_factory=lambda: defaultdict(Totals))

    def add(self, category: str, amounts: Amounts | None) -> None:
        self.total.add(amounts)
        self.categories[category].add(amounts)

    def view(self, covered: bool) -> dict:
        return self.total.view(covered) | {
            "by_category": {kind: self.categories[kind].view(covered) for kind in CATEGORIES},
        }


def _order_amounts(row: Row) -> Amounts | None:
    if row["cash_cents"] is None or row["created_at"] is None:
        return None
    return row["cash_cents"], row["order_cents"], row["coupon_cents"], row["credit_cents"]


def service_window(row: Row) -> tuple[int, int] | None:
    start, end = row["start_at"], row["end_at"]
    if row["order_type"] in SERVICE_ORDERS and start is not None and end is not None and start < end:
        return start, end
    return None


def compute_cash_filter(prefix: str = "") -> str:
    kinds = ",".join(f"'{kind}'" for kind in sorted(SERVICE_ORDERS))
    return f"""{prefix}category='compute' AND {prefix}instance_id IS NOT NULL
        AND {prefix}order_type IN ({kinds}) AND {prefix}cash_cents>=0
        AND {prefix}created_at IS NOT NULL AND {prefix}start_at<{prefix}end_at"""


def compute_cash_windows(
    conn: Connection, project_id: str, since: int, now: int, instance_id: str | None,
) -> Iterator[Row]:
    query = f"""SELECT instance_id,start_at,end_at,cash_cents FROM billing_orders
                WHERE project_id=? AND {compute_cash_filter()} AND start_at<? AND end_at>?"""
    params = (project_id, now, since)
    if instance_id is not None:
        query += " AND instance_id=?"
        params = (*params, instance_id)
    return iter(conn.execute(query + " ORDER BY instance_id,start_at,end_at", params))


def estimate_coverage(
    conn: Connection, project_id: str, since: int, now: int,
) -> dict[tuple[str, str], list[tuple[int, int]] | None]:
    coverage: dict[tuple[str, str], list[tuple[int, int]] | None] = {}
    for row in conn.execute("""
        SELECT category,resource_id,instance_id,order_type,charge_type,created_at,start_at,end_at
        FROM billing_orders WHERE project_id=? AND category IN ('compute','storage')
          AND ((start_at<? AND end_at>?) OR (created_at>=? AND created_at<=?))
        ORDER BY start_at,end_at
    """, (project_id, now, since, since, now)):
        resource = row["instance_id"] if row["category"] == "compute" else row["resource_id"]
        if resource is None:
            continue
        key = (row["category"], resource)
        window = service_window(row)
        if window is None and since <= (row["created_at"] or 0) <= now and (
            row["order_type"] == "OT_POSTPAID_PAYMENT" or (
                row["order_type"] in SERVICE_ORDERS and row["charge_type"] in {"Post", "Postpay", "Dynamic", "Used", "Spot"}
            )
        ):
            coverage[key] = None
        elif window is not None and coverage.get(key, []) is not None:
            coverage.setdefault(key, []).append(window)
    return coverage


def _shares(row: Row, basis: Basis, since: int, until: int) -> Iterator[tuple[str, Amounts | None]]:
    amounts = _order_amounts(row)
    window = service_window(row) if basis == "service_period" else None
    if window is not None:
        start, end = window
        left, right = max(since, start), min(until, end)
        while left < right:
            day = _local_date(left)
            finish = min(right, _midnight(day + timedelta(days=1)))
            share = tuple(apportioned(x, left - start, finish - start, end - start) for x in amounts) if amounts is not None else None
            yield day.isoformat(), share
            left = finish
    elif row["created_at"] is not None and since <= row["created_at"] < until:
        yield _local_date(row["created_at"]).isoformat(), amounts
    elif row["created_at"] is None:
        yield "", None


def _bounds(period: UsageRange, now: int) -> tuple[int, int]:
    today = _local_date(now)
    return _midnight(today - timedelta(days=PERIOD_DAYS[period] - 1)), _midnight(today + timedelta(days=1))


def _rows(
    conn: Connection, project_id: str, basis: Basis, since: int, until: int, instance_id: str | None = None,
    category: Category | None = None,
):
    base = """SELECT * FROM billing_orders WHERE project_id=? AND (
                (created_at>=? AND created_at<?) OR (created_at IS NULL AND cycle BETWEEN ? AND ?)"""
    params = [project_id, since, until, _local_date(since).isoformat()[:7], _local_date(until - 1).isoformat()[:7]]
    if basis == "service_period":
        base += " OR (start_at<? AND end_at>?)"
        params.extend((until, since))
    base += ")"
    if instance_id is not None:
        base += " AND instance_id=?"
        params.append(instance_id)
    if category is not None:
        base += " AND category=?"
        params.append(category)
    return conn.execute(base + " ORDER BY created_at DESC,order_no DESC", params)


def _coverage(conn: Connection, project_id: str, basis: Basis, since: int, now: int, review: int) -> dict:
    records = {row["cycle"]: row for row in conn.execute("SELECT * FROM billing_sync WHERE project_id=?", (project_id,))}
    today = _local_date(now)
    expected = cycles(today) if basis == "service_period" else [
        month for month in cycles(today, 3) if month >= _local_date(since).isoformat()[:7]
    ]
    published = {month for month, row in records.items() if row["synced_at"] is not None}
    restore = restored_at(conn)
    pending = [month for month in expected if month not in published or records[month]["synced_at"] < restore]
    failed = [{"cycle": month, "error": records[month]["error"]} for month in expected
              if month in records and records[month]["error"]]
    if not published.intersection(expected):
        status = "pending"
    else:
        status = "partial" if pending or failed or review else "ready"
    return {
        "status": status, "synced_at": max((records[month]["synced_at"] for month in published), default=None),
        "first_synced_cycle": min(published, default=None), "last_synced_cycle": max(published, default=None),
        "expected_since_cycle": expected[-1], "pending_cycles": pending, "failed_cycles": failed,
        "open_cycle": expected[0], "has_data": status != "pending", "published_cycles": sorted(published),
    }


def _report_rows(
    conn: Connection, project_id: str, period: UsageRange, basis: Basis, now: int,
    instance_id: str | None = None, category: Category | None = None,
) -> Iterator[tuple[Row, list[tuple[str, Amounts | None]], Amounts | None]]:
    since, until = _bounds(period, now)
    for row in _rows(conn, project_id, basis, since, until, instance_id, category):
        shares = list(_shares(row, basis, since, until))
        if shares:
            totals = Totals()
            for _, part in shares:
                totals.add(part)
            selected = totals.amounts if not totals.review_orders else None
            yield row, shares, selected


def summary(conn: Connection, project_id: str, period: UsageRange, basis: Basis, now: int, instance_id: str | None) -> dict:
    total, project_only = Group(), Group()
    days, instances = defaultdict(Group), defaultdict(Group)
    instance_names = {}
    latest = None
    for row, shares, selected in _report_rows(conn, project_id, period, basis, now, instance_id):
        category, owner = row["category"], row["instance_id"]
        total.add(category, selected)
        (instances[owner] if owner else project_only).add(category, selected)
        if owner and category == "compute" and row["name"]:
            instance_names.setdefault(owner, row["name"])
        latest = max(latest or 0, row["created_at"]) if row["created_at"] is not None else latest
        for day, amount in shares:
            if day:
                days[day].add(category, amount)

    since, _ = _bounds(period, now)
    cover = _coverage(conn, project_id, basis, since, now, total.total.review_orders)
    latest_project_order = conn.execute("SELECT MAX(created_at) FROM billing_orders WHERE project_id=?",
                                        (project_id,)).fetchone()[0]
    latest_metered_end = conn.execute("""
        SELECT MAX(end_at) FROM billing_orders WHERE project_id=? AND order_type='OT_POSTPAID_PAYMENT'
          AND start_at<end_at AND end_at<=?
    """, (project_id, now)).fetchone()[0]
    dates = [_local_date(since) + timedelta(days=offset) for offset in range(PERIOD_DAYS[period])]
    today = dates[-1]
    last_metered_day = _local_date(latest_metered_end - 1) if latest_metered_end else today - timedelta(days=1)
    provisional_start = min(last_metered_day + timedelta(days=1), today - timedelta(days=1))
    uncertain_cycles = set(cover["pending_cycles"]) | {item["cycle"] for item in cover["failed_cycles"]}
    return {
        "basis": basis, "coverage": cover, "latest_order_at": latest,
        "project_latest_order_at": latest_project_order,
        "provisional_days": [day.isoformat() for day in dates[:-1]
                             if day >= provisional_start or day.isoformat()[:7] in uncertain_cycles],
        "totals": total.view(cover["has_data"]), "project_only": project_only.view(cover["has_data"]),
        "days": {
            day: days[day].view(cover["has_data"] if basis == "service_period" else day[:7] in cover["published_cycles"])
            for day in (date.isoformat() for date in dates)
        },
        "instances": {key: value.view(cover["has_data"]) for key, value in instances.items()},
        "instance_names": instance_names,
    }


def orders(
    conn: Connection, project_id: str, period: UsageRange, basis: Basis, now: int, instance_id: str | None,
    category: Category | None, limit: int, offset: int,
) -> dict:
    data = []
    total = 0
    for row, _, selected in _report_rows(conn, project_id, period, basis, now, instance_id, category):
        total += 1
        if not offset < total <= offset + limit:
            continue
        fields = ("order_no", "order_type", "resource_id", "resource_type", "name", "charge_type", "category",
                  "instance_id", "created_at", "start_at", "end_at")
        data.append({key: row[key] for key in fields} | {
            "amounts": amount_view(_order_amounts(row)), "selected_amounts": amount_view(selected),
            "allocation": "review" if selected is None else
            "service_period" if basis == "service_period" and service_window(row) else "order_date",
        })
    return {"data": data, "total": total}


def storage_details(conn: Connection, project_id: str, period: UsageRange, basis: Basis, now: int, instance_id: str | None) -> dict:
    resources, project_only = {}, Totals()
    review = 0
    for row, _, selected in _report_rows(conn, project_id, period, basis, now, instance_id, "storage"):
        key = (row["resource_type"], row["resource_id"])
        item = resources.setdefault(key, (row["name"] or row["resource_id"], Totals(), defaultdict(Totals)))
        item[1].add(selected)
        item[2][row["instance_id"]].add(selected)
        if row["instance_id"] is None:
            project_only.add(selected)
        review += selected is None
    cover = _coverage(conn, project_id, basis, _bounds(period, now)[0], now, review)
    return {
        "coverage": cover, "project_only": project_only.view(cover["has_data"]),
        "data": [
            {"resource_type": kind, "resource_id": resource_id, "name": name, "totals": total.view(False),
             "allocations": [{"instance_id": key, "totals": value.view(False)} for key, value in by_instance.items()]}
            for (kind, resource_id), (name, total, by_instance) in resources.items()
        ],
    }
