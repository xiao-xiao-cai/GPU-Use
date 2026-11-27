"""Allocate paid compute orders to measured GPU holding and utilization."""

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field, fields
from datetime import date, timedelta
from decimal import Decimal
from itertools import groupby
from sqlite3 import Connection, Row

from gpu_console.storage import billing, dashboard_cost
from gpu_console.storage.dashboard_usage import _local_date, _midnight

CashWindow = tuple[int, int, Decimal]


@dataclass
class Totals:
    matched_seconds: int = 0
    monitored_seconds: int = 0
    weighted_seconds: Decimal = field(default_factory=Decimal)
    matched_cash: Decimal = field(default_factory=Decimal)
    monitored_cash: Decimal = field(default_factory=Decimal)
    low_load_cash: Decimal = field(default_factory=Decimal)
    idle_cash: Decimal = field(default_factory=Decimal)

    def add(self, row: Row, seconds: int, cash: Decimal, low_cards: int, idle: bool) -> None:
        self.matched_seconds += row["gpu"] * seconds
        self.matched_cash += cash
        if row["load"] is None:
            return
        self.monitored_seconds += row["gpu"] * seconds
        self.weighted_seconds += Decimal(str(row["load"])) * seconds
        self.monitored_cash += cash
        if low_cards:
            self.low_load_cash += cash * low_cards / row["gpu"]
        if idle:
            self.idle_cash += cash

    def merge(self, other: "Totals") -> None:
        for descriptor in fields(self):
            key = descriptor.name
            setattr(self, key, getattr(self, key) + getattr(other, key))

    def view(self) -> dict:
        money = dashboard_cost.money
        return {
            "matched_compute_cost_cny": money(self.matched_cash) if self.matched_seconds else None,
            "matched_gpu_hours": self.matched_seconds / 3600,
            "matched_monitored_gpu_hours": self.monitored_seconds / 3600,
            "evaluable_compute_cost_cny": money(self.monitored_cash) if self.monitored_seconds else None,
            "low_load_cost_cny": money(self.low_load_cash) if self.monitored_seconds else None,
            "low_load_cost_ratio": float(self.low_load_cash / self.matched_cash)
            if self.monitored_seconds and self.matched_cash > 0 else None,
            "idle_cost_cny": money(self.idle_cash) if self.monitored_seconds else None,
            "cost_per_weighted_gpu_hour_cny": money(self.monitored_cash * 3600 / self.weighted_seconds)
            if self.weighted_seconds else None,
            "priced_weighted_gpu_hours": float(self.weighted_seconds / 3600),
        }


def _cash_windows(orders: Iterator[Row], since: int, now: int) -> Iterator[CashWindow]:
    changes = defaultdict(lambda: (0, Decimal(0)))
    for row in orders:
        start, end = row["start_at"], row["end_at"]
        rate = Decimal(row["cash_cents"]) / (100 * (end - start))
        for at, sign in ((max(since, start), 1), (min(now, end), -1)):
            count, amount = changes[at]
            changes[at] = (count + sign, amount + sign * rate)
    active, rate, previous = 0, Decimal(0), since
    for at, (count, change) in sorted(changes.items()):
        if active and at > previous:
            yield previous, at, rate
        active += count
        rate = rate + change if active else Decimal(0)
        previous = at


def _matched_fragments(
    intervals: list[Row], windows: Iterator[CashWindow], since: int,
) -> Iterator[tuple[Row, date, int, Decimal, int, bool]]:
    low_counts, whole_idle = dashboard_cost._load_counts(intervals)
    current = next(windows, None)
    previous_end = since
    for row, low, idle in zip(intervals, low_counts, whole_idle):
        start, end = max(previous_end, row["start"]), row["end"]
        previous_end = max(previous_end, end)
        while current is not None and current[0] < end:
            left, right, rate = current
            clip = max(left, start)
            finish = min(right, end)
            while clip < finish:
                day = _local_date(clip)
                boundary = min(finish, _midnight(day + timedelta(days=1)))
                seconds = boundary - clip
                yield row, day, seconds, rate * seconds, low, idle
                clip = boundary
            if right > end:
                break
            current = next(windows, None)
        if current is None:
            break


def calculate(
    conn: Connection, project_id: str, since: int, until: int, now: int, instance_id: str | None = None,
) -> dict[tuple[date, str], Totals]:
    """Match cash within the requested dates, using nearby samples to classify sustained load."""
    if since >= until:
        return {}
    windows = {
        key: list(_cash_windows(orders, since, until))
        for key, orders in groupby(billing.compute_cash_windows(conn, project_id, since, until, instance_id),
                                   lambda row: row["instance_id"])
    }
    daily = defaultdict(Totals)
    if windows:
        through = min(now, until + dashboard_cost.SUSTAINED_SECONDS + dashboard_cost.SAMPLE_FRESHNESS)
        for key, intervals in dashboard_cost._interval_groups(conn, project_id, since, through, instance_id):
            if key not in windows:
                continue
            for row, day, seconds, cash, low, idle in _matched_fragments(intervals, iter(windows[key]), since):
                daily[day, key].add(row, seconds, cash, low, idle)
    return daily


def apply(conn: Connection, project_id: str, now: int, instance_id: str | None, report: dict) -> None:
    from gpu_console.storage import dashboard_billed_cost_rollups

    since, today = report["since"], _midnight(_local_date(now))
    daily = dashboard_billed_cost_rollups.read(conn, project_id, since, today, now, instance_id)
    total = Totals()
    days, instances = defaultdict(Totals), defaultdict(Totals)
    for day, key in sorted(daily, key=lambda pair: (pair[1], pair[0])):
        values = daily[day, key]
        days[day.isoformat()].merge(values)
        instances[key].merge(values)
        total.merge(values)

    report.update(total.view())
    report["cost_metrics_basis"] = "paid_compute_service_period"
    for day in report["days"]:
        day.update(days[day["date"]].view())
    for row in report["instances"]:
        row.update(instances[row["instance_id"]].view())
