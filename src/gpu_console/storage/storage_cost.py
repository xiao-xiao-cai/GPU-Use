"""Record independent storage observations and their estimated daily costs."""

import calendar
import json
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from sqlite3 import Connection, Row

from gpu_console.storage import instance_assets as assets
from gpu_console.storage.dashboard_usage import _local_date, _midnight

RETENTION_DAYS = 30
MAX_GAP = 2 * assets.SYNC_SECONDS
UDISK_BILLABLE = {"Available", "InUse", "Attaching", "Detaching"}
SPEC_FIELDS = ("type", "size", "charge_type")
SCOPE_FILTER = "project_id=? AND region=? AND zone=? AND kind=?"


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS storage_cost_samples (
            project_id TEXT NOT NULL, region TEXT NOT NULL, zone TEXT NOT NULL, kind TEXT NOT NULL,
            sampled_at INTEGER NOT NULL, started_at INTEGER NOT NULL, resources TEXT,
            PRIMARY KEY(project_id,region,zone,kind,sampled_at)
        );
        CREATE INDEX IF NOT EXISTS storage_cost_samples_retention ON storage_cost_samples(sampled_at);
        CREATE TABLE IF NOT EXISTS storage_cost_daily (
            project_id TEXT NOT NULL, region TEXT NOT NULL, zone TEXT NOT NULL, kind TEXT NOT NULL,
            day INTEGER NOT NULL, resource_id TEXT NOT NULL, instance_id TEXT NOT NULL, source TEXT NOT NULL,
            name TEXT, charge_type TEXT, cost TEXT NOT NULL,
            start INTEGER NOT NULL, end INTEGER NOT NULL,
            covered_seconds INTEGER NOT NULL, priced_seconds INTEGER NOT NULL, missing_seconds INTEGER NOT NULL,
            PRIMARY KEY(project_id,region,zone,kind,day,resource_id,instance_id,source)
        );
        CREATE INDEX IF NOT EXISTS storage_cost_daily_project_day ON storage_cost_daily(project_id,day);
        CREATE INDEX IF NOT EXISTS storage_cost_daily_retention ON storage_cost_daily(day);
    """)


def _prices(conn: Connection, row: dict, sampled_at: int, cache: dict[str, list[dict]]) -> list[dict]:
    result = []
    actual = assets.actual_storage_price(row)
    quotes = assets.storage_quotes(conn, row, cache)
    for price in ([actual] + quotes if actual is not None else quotes):
        valid_until = assets.price_valid_until(price)
        if not price["observed_at"] or not price["observed_at"] <= sampled_at < valid_until:
            continue
        try:
            amount = Decimal(str(price["amount"]))
        except InvalidOperation:
            continue
        if amount.is_finite() and amount >= 0:
            result.append({
                "source": price["source"], "amount": str(amount),
                "observed_at": price["observed_at"], "valid_until": valid_until,
            })
    return result


def _resource(conn: Connection, row: dict, sampled_at: int, cache: dict[str, list[dict]]) -> dict:
    return {key: row.get(key) for key in (
        "name", *SPEC_FIELDS, "status", "expires_at", "mounted_instance_ids",
    )} | {"unit": assets.BILLING_UNITS.get(row.get("charge_type")),
          "prices": _prices(conn, row, sampled_at, cache)}


def _days(start: int, end: int) -> Iterator[tuple[date, int, int]]:
    while start < end:
        date = _local_date(start)
        finish = min(end, _midnight(date + timedelta(days=1)))
        yield date, start, finish
        start = finish


def _write_day(
    conn: Connection, scope: tuple[str, ...], day: int, start: int, end: int,
    *, resource_id: str = "", instance_id: str = "", source: str = "", resource: dict | None = None,
    cost: Decimal | None = None, covered: bool = False,
) -> None:
    key = (*scope, day, resource_id, instance_id, source)
    amount = cost
    if amount is not None:
        previous = conn.execute(
            "SELECT cost FROM storage_cost_daily WHERE " + SCOPE_FILTER
            + " AND day=? AND resource_id=? AND instance_id=? AND source=?", key,
        ).fetchone()
        if previous is not None:
            amount += Decimal(previous["cost"])
    seconds = end - start
    conn.execute("""
        INSERT INTO storage_cost_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(project_id,region,zone,kind,day,resource_id,instance_id,source) DO UPDATE SET
            name=excluded.name,charge_type=excluded.charge_type,cost=excluded.cost,
            start=MIN(storage_cost_daily.start,excluded.start),end=MAX(storage_cost_daily.end,excluded.end),
            covered_seconds=storage_cost_daily.covered_seconds+excluded.covered_seconds,
            priced_seconds=storage_cost_daily.priced_seconds+excluded.priced_seconds,
            missing_seconds=storage_cost_daily.missing_seconds+excluded.missing_seconds
    """, (*key, resource.get("name") if resource else None, resource.get("charge_type") if resource else None,
          str(amount) if amount is not None else "0", start, end, seconds if covered else 0,
          seconds if cost is not None else 0, seconds if not covered and cost is None else 0))


def _exclusive_mount(before: dict, after: dict) -> str:
    mounts = before["mounted_instance_ids"]
    if (isinstance(mounts, list) and len(mounts) == 1 and mounts == after["mounted_instance_ids"]
            and {before["status"], after["status"]}.isdisjoint({"Attaching", "Detaching"})):
        return mounts[0]
    return ""


def _cycle_seconds(unit: str, date: date) -> int:
    if unit == "hour":
        return 3600
    if unit == "month":
        return calendar.monthrange(date.year, date.month)[1] * 86400
    if unit == "year":
        return (366 if calendar.isleap(date.year) else 365) * 86400
    return 86400


@dataclass(frozen=True)
class ResourceSegment:
    start: int
    end: int
    instance_id: str
    price: dict | None


def _resource_segments(
    kind: str, before: dict | None, after: dict | None, start: int, end: int,
) -> Iterator[ResourceSegment]:
    resource = after if after is not None else before
    stable = before is not None and after is not None and all(before[k] == after[k] for k in SPEC_FIELDS)
    billable = stable and (kind == "cfs" or {before["status"], after["status"]} <= UDISK_BILLABLE)
    instance = _exclusive_mount(before, after) if billable else ""
    prices = [item for item in before["prices"] if item["source"] != "actual" or instance] if billable else []
    priced_until = end
    if stable and resource["charge_type"] != "Postpay" and before["expires_at"]:
        priced_until = min(end, before["expires_at"])
    for _, left, right in _days(start, end):
        while left < min(right, priced_until) and resource["unit"]:
            price = next((item for item in prices if item["valid_until"] > left), None)
            if price is None:
                break
            finish = min(right, priced_until, price["valid_until"])
            yield ResourceSegment(left, finish, instance, price)
            left = finish
        if left < right:
            yield ResourceSegment(left, right, instance, None)


def _resource_interval(
    conn: Connection, scope: tuple[str, ...], resource_id: str, before: dict | None,
    after: dict | None, start: int, end: int,
) -> None:
    resource = after if after is not None else before
    for segment in _resource_segments(scope[-1], before, after, start, end):
        date = _local_date(segment.start)
        price = segment.price
        cost = (Decimal(price["amount"]) * (segment.end - segment.start) / _cycle_seconds(resource["unit"], date)
                if price is not None else None)
        _write_day(conn, scope, _midnight(date), segment.start, segment.end, resource_id=resource_id,
                   instance_id=segment.instance_id, source=price["source"] if price else "", resource=resource, cost=cost)


def observe(
    conn: Connection, project_id: str, observations: list[tuple[str, str, str, int, int, list[dict] | None]],
    *, estimate_costs: bool = True,
) -> None:
    """Append fresh scope reads and optional daily rollups in the caller's transaction."""
    restored_at = conn.execute("SELECT MAX(restored_at) FROM recovery_log").fetchone()[0] or 0
    cache: dict[str, list[dict]] = {}
    for region, zone, kind, started_at, sampled_at, rows in observations:
        scope = (project_id, region, zone, kind)
        previous = conn.execute(
            "SELECT sampled_at,started_at,resources FROM storage_cost_samples WHERE " + SCOPE_FILTER
            + " ORDER BY sampled_at DESC LIMIT 1", scope,
        ).fetchone()
        if started_at < restored_at or (previous and (
            started_at <= previous["started_at"] or sampled_at <= previous["sampled_at"]
        )):
            continue
        current = None if rows is None else {row["id"]: _resource(conn, row, sampled_at, cache) for row in rows}
        conn.execute("INSERT INTO storage_cost_samples VALUES (?,?,?,?,?,?,?)",
                     (*scope, sampled_at, started_at, json.dumps(current) if current is not None else None))
        if not estimate_costs:
            continue
        if previous is None:
            # Include newly discovered or failed scopes in the coverage report before they have a pair of samples.
            _write_day(conn, scope, _midnight(_local_date(sampled_at)), sampled_at, sampled_at)
            continue
        prior_at = previous["sampled_at"]
        continuous = (previous["resources"] is not None and current is not None
                      and prior_at >= restored_at and sampled_at - prior_at <= MAX_GAP)
        start = max(prior_at, _cutoff(sampled_at))
        for date, left, right in _days(start, sampled_at):
            _write_day(conn, scope, _midnight(date), left, right,
                       source="observed" if continuous else "", covered=continuous)
        if continuous:
            before = json.loads(previous["resources"])
            for resource_id in before.keys() | current.keys():
                _resource_interval(conn, scope, resource_id, before.get(resource_id), current.get(resource_id),
                                   start, sampled_at)


def metered_segments(
    conn: Connection, project_id: str, since: int, now: int, location: tuple[str, str] | None,
) -> tuple[list[tuple[str, ResourceSegment]], int | None, bool]:
    restored_at = conn.execute("SELECT MAX(restored_at) FROM recovery_log").fetchone()[0] or 0
    segments = []
    previous: dict[tuple[str, str, str], tuple[int, dict | None]] = {}
    covered: dict[tuple[str, str, str], int] = defaultdict(int)
    through: dict[tuple[str, str, str], int] = {}
    for row in conn.execute("""
        SELECT region,zone,kind,sampled_at,resources FROM storage_cost_samples
        WHERE project_id=? AND sampled_at BETWEEN ? AND ? ORDER BY sampled_at
    """, (project_id, max(since - MAX_GAP, restored_at), now)):
        scope = row["region"], row["zone"], row["kind"]
        end = row["sampled_at"]
        current = json.loads(row["resources"]) if row["resources"] is not None else None
        prior_at, prior = previous.get(scope, (end, None))
        previous[scope] = end, current
        start = max(prior_at, since)
        if prior is None or current is None or end - prior_at > MAX_GAP or start >= end:
            continue
        covered[scope] += end - start
        through[scope] = end
        for resource_id in prior.keys() | current.keys():
            before, after = prior.get(resource_id), current.get(resource_id)
            if not any(item and item["unit"] == "hour" for item in (before, after)):
                continue
            segments.extend((resource_id, segment) for segment in
                            _resource_segments(scope[-1], before, after, start, end))
    expected_scopes = {scope: sample for scope, sample in previous.items() if location is None or scope[:2] == location}
    complete = bool(expected_scopes) and all(
        current is not None and through.get(scope) == end and now - end <= MAX_GAP
        and covered[scope] == end - since for scope, (end, current) in expected_scopes.items()
    )
    return segments, max((through[scope] for scope in expected_scopes if scope in through), default=None), complete


def _cutoff(now: int) -> int:
    return _midnight(_local_date(now) - timedelta(days=RETENTION_DAYS - 1))


def trim(conn: Connection, now: int) -> None:
    cutoff = _cutoff(now)
    conn.execute("DELETE FROM storage_cost_samples WHERE sampled_at<?", (cutoff - MAX_GAP,))
    conn.execute("DELETE FROM storage_cost_daily WHERE day<?", (cutoff,))


@dataclass
class Estimate:
    cost: Decimal = field(default_factory=Decimal)
    priced_seconds: int = 0
    missing_seconds: int = 0

    def add(self, row: Row) -> None:
        self.cost += Decimal(row["cost"])
        self.priced_seconds += row["priced_seconds"]
        self.missing_seconds += row["missing_seconds"]

    def amount(self, empty_known: bool = False) -> Decimal | None:
        return self.cost if self.priced_seconds or (empty_known and not self.missing_seconds) else None


@dataclass
class Coverage:
    since: int | None = None
    through: int | None = None
    gap_seconds: int = 0

    def add(self, row: Row) -> None:
        if row["covered_seconds"]:
            self.since = min(self.since if self.since is not None else row["start"], row["start"])
            self.through = max(self.through or 0, row["end"])
        self.gap_seconds += row["missing_seconds"]

    def complete(self, since: int, until: int) -> bool:
        return (self.since is not None and self.since <= since and not self.gap_seconds
                and self.through is not None and self.through >= until)


@dataclass
class ResourceCost:
    totals: Estimate = field(default_factory=Estimate)
    name: str = ""
    charge_types: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    instances: dict[str, Estimate] = field(default_factory=lambda: defaultdict(Estimate))


@dataclass
class Report:
    total: Estimate = field(default_factory=Estimate)
    project_only: Estimate = field(default_factory=Estimate)
    days: dict[int, Estimate] = field(default_factory=lambda: defaultdict(Estimate))
    instances: dict[str, Estimate] = field(default_factory=lambda: defaultdict(Estimate))
    resources: dict[tuple[str, str, str, str], ResourceCost] = field(default_factory=dict)
    data_through: int | None = None
    recording_since: int | None = None
    empty_known_days: set[int] = field(default_factory=set)
    status: str = "unobserved"
    gap_seconds: int = 0


def read(
    conn: Connection, project_id: str, since: int, now: int, instance_id: str | None = None,
    *, include_resources: bool = False, instance_location: tuple[str, str] | None = None,
) -> Report:
    report = Report()
    scopes: dict[tuple[str, str, str], Coverage] = defaultdict(Coverage)
    daily_coverage: dict[int, dict[tuple[str, str, str], Coverage]] = defaultdict(lambda: defaultdict(Coverage))
    instance_locations = {instance_location} if instance_location is not None else set()
    current_day = _midnight(_local_date(now))
    rows = conn.execute(
        """SELECT * FROM storage_cost_daily WHERE project_id=? AND day BETWEEN ? AND ?
           ORDER BY day,end""", (project_id, since, current_day),
    )
    for row in rows:
        scope = (row["region"], row["zone"], row["kind"])
        if not row["resource_id"]:
            scopes[scope].add(row)
            daily_coverage[row["day"]][scope].add(row)
            continue
        owner = row["instance_id"]
        if instance_id is not None and instance_id != owner:
            continue
        if instance_id is not None:
            instance_locations.add((row["region"], row["zone"]))
        report.total.add(row)
        report.days[row["day"]].add(row)
        if owner:
            report.instances[owner].add(row)
        else:
            report.project_only.add(row)
        if include_resources:
            identity = (*scope, row["resource_id"])
            resource = report.resources.setdefault(identity, ResourceCost())
            resource.name = row["name"] or row["resource_id"]
            resource.totals.add(row)
            resource.instances[owner].add(row)
            if row["charge_type"]:
                resource.charge_types.add(row["charge_type"])
            if row["source"]:
                resource.sources.add(row["source"])
    if instance_locations:
        scopes = {scope: coverage for scope, coverage in scopes.items() if scope[:2] in instance_locations}
    report.gap_seconds = sum(item.gap_seconds for item in scopes.values())
    starts = [item.since for item in scopes.values() if item.since is not None]
    if starts:
        report.recording_since = min(starts)
        report.data_through = max(item.through for item in scopes.values() if item.through is not None)
        restored_at = conn.execute("SELECT MAX(restored_at) FROM recovery_log").fetchone()[0] or 0
        report.status = "partial" if report.total.missing_seconds or any(
            not item.complete(since, max(restored_at, now - MAX_GAP)) for item in scopes.values()
        ) else "complete"
    for day, coverage in daily_coverage.items():
        end = now - MAX_GAP if day == current_day else day + 86400
        if scopes and all(scope in coverage and coverage[scope].complete(day, end) for scope in scopes):
            report.empty_known_days.add(day)
    return report
