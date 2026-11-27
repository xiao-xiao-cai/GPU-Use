"""Record GPU holding intervals and estimate the cost of observed usage."""

import json
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field, fields
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from itertools import groupby
from sqlite3 import Connection, Row

from pydantic import BaseModel, ConfigDict, Field

from gpu_console import storage
from gpu_console.application.idle_notices import MAX_SAMPLE_GAP, SAMPLE_FRESHNESS, complete_cards
from gpu_console.domain.billing import Basis
from gpu_console.storage import billing, instance_history, storage_cost
from gpu_console.storage.dashboard_usage import PERIOD_DAYS, UsageRange, _local_date, _midnight

HOURLY = {"Postpay", "Spot"}
LOAD_THRESHOLD = 10
SUSTAINED_SECONDS = 15 * 60
RETENTION_DAYS = 30
GPU_UTIL = "cloudwatch_gpu_util"
CPU_UTIL = "uhost_cpu_used"
GPU_MEMORY = "cloudwatch_gpu_memory_usage"
INSERT_INTERVAL = "INSERT INTO dashboard_cost_intervals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"


class ManualRate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effective_at: int = Field(gt=0, strict=True)
    hourly_rate: Decimal = Field(ge=0, max_digits=12, decimal_places=6)


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dashboard_cost_progress (
            project_id TEXT PRIMARY KEY, first_at INTEGER, sampled_at INTEGER NOT NULL,
            gap_limit INTEGER NOT NULL, instances TEXT
        );
        CREATE TABLE IF NOT EXISTS dashboard_cost_intervals (
            project_id TEXT NOT NULL, instance_id TEXT NOT NULL, start INTEGER NOT NULL,
            end INTEGER NOT NULL, boot INTEGER NOT NULL, name TEXT NOT NULL, gpu_model TEXT,
            gpu INTEGER NOT NULL, charge_type TEXT, platform_rate TEXT, load REAL,
            low_cards TEXT, busy_cards INTEGER NOT NULL, active INTEGER NOT NULL, cpu_load INTEGER, memory_load INTEGER,
            PRIMARY KEY(project_id,instance_id,start)
        );
        CREATE INDEX IF NOT EXISTS dashboard_cost_interval_end ON dashboard_cost_intervals(end);
        CREATE TABLE IF NOT EXISTS dashboard_cardless_intervals (
            project_id TEXT NOT NULL, instance_id TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
            rate TEXT, rate_source TEXT, PRIMARY KEY(project_id,instance_id,start)
        );
        CREATE INDEX IF NOT EXISTS dashboard_cardless_interval_end ON dashboard_cardless_intervals(end);
        CREATE TABLE IF NOT EXISTS dashboard_cost_history_imports (project_id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS dashboard_cost_manual_rates (
            project_id TEXT NOT NULL, instance_id TEXT NOT NULL, effective_at INTEGER NOT NULL,
            hourly_rate TEXT NOT NULL, PRIMARY KEY(project_id,instance_id,effective_at)
        );
    """)


def _platform_rate(value: object) -> str | None:
    try:
        rate = Decimal(str(value))
    except InvalidOperation:
        return None
    return str(rate) if rate.is_finite() and rate >= 0 else None


def _load_hint(points: list[dict], metric: str, boot: int, timestamp: int, expected: int) -> bool | None:
    samples: dict[int, dict[str, float]] = defaultdict(dict)
    for point in points:
        sampled_at = point["sampled_at"]
        if point["metric"] == metric and max(boot, timestamp - SAMPLE_FRESHNESS) <= sampled_at <= timestamp:
            samples[sampled_at][point["label"]] = point["value"]
    return _hint(samples[max(samples)] if samples else {}, expected)


def _hint(cards: dict[str, float], expected: int) -> bool | None:
    if any(LOAD_THRESHOLD <= value <= 100 for value in cards.values()):
        return True
    return False if len(cards) == expected and all(0 <= value < LOAD_THRESHOLD for value in cards.values()) else None


def _observation(row: dict, readings: dict[int, dict[str, float]], points: list[dict], timestamp: int) -> dict | None:
    boot, gpu = row.get("start_time"), row.get("gpu")
    if row.get("state") != "Running" or type(boot) is not int or not 0 < boot <= timestamp:
        return None
    if row.get("gpu_mode") == "without_gpu" and row.get("charge_type") == "Postpay":
        return {"mode": "without_gpu", "boot": boot, "cpu_cores": row.get("cpu"), "memory_gb": row.get("memory_gb"),
                "gpu": 0, "gpu_model": row.get("gpu_model"), "charge_type": "Postpay",
                "rate": _platform_rate(row.get("cardless_hourly_cny")), "source": row.get("cardless_rate_source")}
    if type(gpu) is not int or gpu <= 0:
        return None
    sampled_at = max(readings, default=0)
    cards = readings.get(sampled_at, {})
    covered = boot <= sampled_at <= timestamp and timestamp - sampled_at <= SAMPLE_FRESHNESS and complete_cards(cards, gpu)
    charge = row.get("charge_type")
    return {
        "boot": boot, "name": row.get("name") or row["id"], "gpu_model": row.get("gpu_model"), "gpu": gpu,
        "mode": row.get("gpu_mode"), "charge_type": charge,
        "rate": _platform_rate(row.get("instance_price_per_hour")) if charge in HOURLY else None,
        "cards": cards if covered else {}, "card_ids": sorted(cards) if covered else [],
        "cpu": _load_hint(points, CPU_UTIL, boot, timestamp, 1),
        "memory": _load_hint(points, GPU_MEMORY, boot, timestamp, gpu),
    }


def _interval(project: str, instance: str, start: int, end: int, before: dict, after: dict) -> tuple | None:
    if "without_gpu" in (before["mode"], after["mode"]):
        return None
    if any(before[key] != after[key] for key in ("boot", "gpu", "gpu_model", "charge_type")):
        return None
    mode = {before["mode"], after["mode"]}
    if "with_gpu" not in mode or "without_gpu" in mode:
        return None
    left, right = before["cards"], after["cards"]
    if before["card_ids"] and after["card_ids"] and before["card_ids"] != after["card_ids"]:
        return None
    after["mode"] = "with_gpu"
    after["card_ids"] = after["card_ids"] or before["card_ids"]
    load, low, busy, active = None, None, 0, False
    if left and right:
        load = (sum(left.values()) + sum(right.values())) / 200
        low = json.dumps(sorted(card for card in left if max(left[card], right[card]) < LOAD_THRESHOLD))
        busy = sum(min(left[card], right[card]) >= LOAD_THRESHOLD for card in left)
        active = min(max(left.values()), max(right.values())) >= LOAD_THRESHOLD
    hints = [
        True if before[key] or after[key] else (False if before[key] is after[key] is False else None)
        for key in ("cpu", "memory")
    ]
    rate = after["rate"] if before["rate"] == after["rate"] else None
    return (
        project, instance, start, end, after["boot"], after["name"], after["gpu_model"], after["gpu"],
        after["charge_type"], rate, load, low, busy, active, *hints,
    )


def _cardless_interval(project: str, instance: str, start: int, end: int, before: dict, after: dict) -> tuple | None:
    if before["mode"] != "without_gpu" or after["mode"] != "without_gpu" or any(
        before.get(key) != after.get(key) for key in ("boot", "cpu_cores", "memory_gb")
    ):
        return None
    rate, source = after.get("rate"), after.get("source")
    if not rate or source not in {"official", "account"} or (rate, source) != (before.get("rate"), before.get("source")):
        rate = source = None
    return project, instance, start, end, rate, source


def _historical_intervals(
    conn: Connection, project: str, row: dict, since: int, snapshot_at: int, cutoff: int,
) -> Iterator[tuple]:
    """Recover observed intervals in the last identifiable boot, before regular cost collection."""
    boot, gpu = row.get("start_time"), row.get("gpu")
    if type(boot) is not int or boot <= 0 or type(gpu) is not int or gpu <= 0 or row.get("gpu_mode") == "without_gpu":
        return
    if row.get("state") == "Stopped":
        stop = row.get("stop_time")
        if type(stop) is not int or not boot < stop <= snapshot_at:
            return
        cutoff = min(cutoff, stop)
    elif row.get("state") != "Running":
        return
    start = max(since, boot)
    if start >= cutoff:
        return
    samples = conn.execute(
        """SELECT sampled_at,metric,label,value FROM metrics WHERE project_id=? AND instance_id=?
           AND metric IN (?,?,?) AND sampled_at BETWEEN ? AND ? ORDER BY sampled_at""",
        (project, row["id"], GPU_UTIL, CPU_UTIL, GPU_MEMORY, start, cutoff),
    )
    base = {
        "boot": boot, "name": row.get("name") or row["id"], "gpu_model": row.get("gpu_model"), "gpu": gpu,
        "mode": "with_gpu", "charge_type": row.get("charge_type"), "rate": None,
    }
    previous = None
    hints: dict[str, tuple[int, dict[str, float]]] = {}
    for at, points in groupby(samples, lambda sample: sample["sampled_at"]):
        metrics: dict[str, dict[str, float]] = defaultdict(dict)
        for point in points:
            metrics[point["metric"]][point["label"]] = point["value"]
        for metric in (CPU_UTIL, GPU_MEMORY):
            if metric in metrics:
                hints[metric] = at, metrics[metric]
        if GPU_UTIL not in metrics:
            continue
        cards = metrics[GPU_UTIL]
        if not complete_cards(cards, gpu):
            previous = None
            continue
        cpu_at, cpu = hints.get(CPU_UTIL, (0, {}))
        memory_at, memory = hints.get(GPU_MEMORY, (0, {}))
        observation = base | {
            "cards": cards, "card_ids": sorted(cards),
            "cpu": _hint(cpu, 1) if at - cpu_at <= SAMPLE_FRESHNESS else None,
            "memory": _hint(memory, gpu) if at - memory_at <= SAMPLE_FRESHNESS else None,
        }
        if previous and at - previous[0] <= MAX_SAMPLE_GAP:
            interval = _interval(project, row["id"], previous[0], at, previous[1], observation)
            if interval is not None:
                yield interval
        previous = at, observation


def _import_history(conn: Connection, project_id: str, rows: list[dict], timestamp: int, first_at: int) -> None:
    # Claims and imported intervals use the caller's collection transaction.
    if not conn.execute("INSERT OR IGNORE INTO dashboard_cost_history_imports VALUES (?)", (project_id,)).rowcount:
        return
    since = _midnight(_local_date(timestamp) - timedelta(days=RETENTION_DAYS - 1)) - SUSTAINED_SECONDS
    candidates = {}
    snapshot = storage.snapshot(conn, "instances", project_id)
    if snapshot["success_at"] and not snapshot["error"]:
        candidates.update({row["id"]: (row, snapshot["success_at"]) for row in snapshot["data"]})
    candidates.update({
        row["id"]: (row, timestamp) for row in rows if row.get("project_id") == project_id
        and row.get("state") in {"Running", "Stopped"}
        and type(row.get("start_time")) is int and 0 < row["start_time"] < first_at
    })
    for row, snapshot_at in candidates.values():
        conn.executemany(INSERT_INTERVAL, _historical_intervals(
            conn, project_id, row, since, snapshot_at, min(first_at, snapshot_at),
        ))


def observe(
    conn: Connection, project_id: str, rows: list[dict], readings: dict[str, dict[int, dict[str, float]]],
    points: dict[str, list[dict]], timestamp: int, collection_seconds: int,
) -> None:
    previous = conn.execute("SELECT * FROM dashboard_cost_progress WHERE project_id=?", (project_id,)).fetchone()
    if previous is not None and timestamp <= previous["sampled_at"]:
        return
    _import_history(conn, project_id, rows, timestamp, (previous["first_at"] if previous else None) or timestamp)
    current = {}
    for row in rows:
        if row.get("project_id") == project_id:
            key = row["id"]
            observation = _observation(row, readings.get(key, {}), points.get(key, []), timestamp)
            if observation is not None:
                current[key] = observation
    gap = min(2 * collection_seconds, SAMPLE_FRESHNESS)
    if previous and previous["instances"] is not None and timestamp - previous["sampled_at"] <= min(gap, previous["gap_limit"]):
        initial = json.loads(previous["instances"])
        intervals = (
            _interval(project_id, key, previous["sampled_at"], timestamp, initial[key], current[key])
            for key in initial.keys() & current.keys()
        )
        conn.executemany(
            INSERT_INTERVAL,
            (item for item in intervals if item is not None),
        )
        cardless = (
            _cardless_interval(project_id, key, previous["sampled_at"], timestamp, initial[key], current[key])
            for key in initial.keys() & current.keys()
        )
        conn.executemany("INSERT OR IGNORE INTO dashboard_cardless_intervals VALUES (?,?,?,?,?,?)",
                         (item for item in cardless if item is not None))
    conn.execute(
        """INSERT INTO dashboard_cost_progress VALUES (?,?,?,?,?)
           ON CONFLICT(project_id) DO UPDATE SET first_at=COALESCE(first_at,excluded.first_at),
               sampled_at=excluded.sampled_at,gap_limit=excluded.gap_limit,instances=excluded.instances""",
        (project_id, timestamp, timestamp, gap, json.dumps(current, allow_nan=False)),
    )


def collection_failed(conn: Connection, project_id: str, timestamp: int) -> None:
    conn.execute(
        """INSERT INTO dashboard_cost_progress VALUES (?,NULL,?,0,NULL)
           ON CONFLICT(project_id) DO UPDATE SET sampled_at=excluded.sampled_at,instances=NULL
           WHERE sampled_at<excluded.sampled_at""",
        (project_id, timestamp),
    )


def restore_boundary(conn: Connection, timestamp: int) -> None:
    conn.execute("UPDATE dashboard_cost_progress SET sampled_at=MAX(sampled_at,?),instances=NULL", (timestamp,))


def trim(conn: Connection, timestamp: int) -> None:
    cutoff = _midnight(_local_date(timestamp) - timedelta(days=RETENTION_DAYS - 1))
    conn.execute("DELETE FROM dashboard_cost_intervals WHERE end<=?", (cutoff - SUSTAINED_SECONDS,))
    conn.execute("DELETE FROM dashboard_cardless_intervals WHERE end<=?", (cutoff,))


def save_rate(conn: Connection, project_id: str, instance_id: str, rate: ManualRate, now: int) -> dict:
    if rate.effective_at > now:
        raise ValueError("费率生效时间不能晚于现在")
    instance = next((row for row in storage.snapshot(conn, "instances", project_id)["data"]
                     if row["id"] == instance_id), None)
    known = instance and instance.get("charge_type") in HOURLY
    if not known and conn.execute(
        """SELECT 1 FROM dashboard_cost_intervals WHERE project_id=? AND instance_id=?
           AND charge_type IN ('Postpay','Spot') LIMIT 1""", (project_id, instance_id),
    ).fetchone() is None:
        raise KeyError("该项目下找不到按时计费的实例")
    conn.execute(
        """INSERT INTO dashboard_cost_manual_rates VALUES (?,?,?,?)
           ON CONFLICT(project_id,instance_id,effective_at) DO UPDATE SET hourly_rate=excluded.hourly_rate""",
        (project_id, instance_id, rate.effective_at, str(rate.hourly_rate)),
    )
    return rate.model_dump(mode="json")


def money(value: Decimal) -> str:
    return format(value, ".4f")


@dataclass
class Totals:
    gpu_seconds: int = 0
    recovered_gpu_seconds: int = 0
    monitored_seconds: int = 0
    low_seconds: int = 0
    idle_seconds: int = 0
    unpriced_idle_seconds: int = 0
    weighted_seconds: float = 0.0
    priced_weighted_seconds: float = 0.0
    unpriced_seconds: int = 0
    unsupported_seconds: int = 0
    platform_seconds: int = 0
    manual_seconds: int = 0
    priced_monitored_seconds: int = 0
    cost: Decimal = field(default_factory=Decimal)
    monitored_cost: Decimal = field(default_factory=Decimal)
    idle_cost: Decimal = field(default_factory=Decimal)
    low_load_cost: Decimal = field(default_factory=Decimal)

    def merge(self, other: "Totals") -> None:
        for descriptor in fields(self):
            key = descriptor.name
            setattr(self, key, getattr(self, key) + getattr(other, key))

    def add(
        self, interval: Row, seconds: int, low_cards: int, idle: bool, rate: Decimal | None, source: str | None,
        recovered: bool,
    ) -> None:
        self.gpu_seconds += interval["gpu"] * seconds
        if recovered:
            self.recovered_gpu_seconds += interval["gpu"] * seconds
        if interval["load"] is not None:
            self.monitored_seconds += interval["gpu"] * seconds
            self.weighted_seconds += interval["load"] * seconds
        self.low_seconds += low_cards * seconds
        self.idle_seconds += seconds if idle else 0
        if interval["charge_type"] not in HOURLY:
            self.unsupported_seconds += seconds
        elif rate is None:
            self.unpriced_seconds += seconds
        elif source == "platform":
            self.platform_seconds += seconds
        else:
            self.manual_seconds += seconds
        if rate is None:
            self.unpriced_idle_seconds += seconds if idle else 0
            return
        cost = rate * seconds / 3600
        self.cost += cost
        if interval["load"] is not None:
            self.monitored_cost += cost
            self.priced_monitored_seconds += seconds
            self.priced_weighted_seconds += interval["load"] * seconds
            if low_cards:
                self.low_load_cost += cost * low_cards / interval["gpu"]
        if idle:
            self.idle_cost += cost

    def view(self, estimate_costs: bool = True) -> dict:
        usage = {
            "gpu_hours": self.gpu_seconds / 3600 if self.gpu_seconds else None,
            "recovered_gpu_hours": self.recovered_gpu_seconds / 3600,
            "weighted_gpu_hours": self.weighted_seconds / 3600 if self.monitored_seconds else None,
            "monitored_gpu_hours": self.monitored_seconds / 3600,
            "unmonitored_gpu_hours": (self.gpu_seconds - self.monitored_seconds) / 3600,
            "low_gpu_hours": self.low_seconds / 3600 if self.monitored_seconds else None,
            "idle_instance_hours": self.idle_seconds / 3600 if self.monitored_seconds else None,
        }
        if not estimate_costs:
            return usage | dict.fromkeys((
                "estimated_cost_cny", "estimated_total_cost_cny", "storage_cost_cny", "low_load_cost_cny",
                "evaluable_compute_cost_cny", "low_load_cost_ratio", "idle_cost_cny", "unpriced_idle_instance_hours",
                "cost_per_weighted_gpu_hour_cny", "priced_weighted_gpu_hours", "unpriced_instance_hours",
                "unsupported_instance_hours", "platform_priced_instance_hours", "manual_priced_instance_hours",
                "unpriced_storage_hours",
            ))
        return usage | {
            "estimated_cost_cny": money(self.cost) if self.platform_seconds or self.manual_seconds else None,
            "low_load_cost_cny": money(self.low_load_cost) if self.priced_monitored_seconds else None,
            "evaluable_compute_cost_cny": money(self.monitored_cost) if self.priced_monitored_seconds else None,
            "low_load_cost_ratio": float(self.low_load_cost / self.cost)
            if self.priced_monitored_seconds and self.cost > 0 else None,
            "idle_cost_cny": money(self.idle_cost) if (
                self.idle_seconds > self.unpriced_idle_seconds or self.idle_seconds == 0 and self.priced_monitored_seconds
            ) else None,
            "unpriced_idle_instance_hours": self.unpriced_idle_seconds / 3600,
            "cost_per_weighted_gpu_hour_cny": money(self.monitored_cost * 3600 / Decimal(str(self.priced_weighted_seconds)))
            if self.priced_weighted_seconds else None,
            "priced_weighted_gpu_hours": self.priced_weighted_seconds / 3600,
            "unpriced_instance_hours": self.unpriced_seconds / 3600,
            "unsupported_instance_hours": self.unsupported_seconds / 3600,
            "platform_priced_instance_hours": self.platform_seconds / 3600,
            "manual_priced_instance_hours": self.manual_seconds / 3600,
        }


def _contiguous(left: Row, right: Row) -> bool:
    return left["end"] == right["start"] and all(
        left[key] == right[key] for key in ("boot", "gpu", "gpu_model", "charge_type")
    )


def _streaks(intervals: list[Row], values: list[set[str]]) -> list[tuple[str, int, int]]:
    """Return sustained runs as half-open ranges of interval indexes."""
    starts = {}
    runs = []
    for end in range(len(intervals) + 1):
        current = values[end] if end < len(intervals) else set()
        carry = current if end and end < len(intervals) and _contiguous(intervals[end - 1], intervals[end]) else set()
        for label in starts.keys() - carry:
            start = starts.pop(label)
            if intervals[end - 1]["end"] - intervals[start]["start"] >= SUSTAINED_SECONDS:
                runs.append((label, start, end))
        for label in current:
            starts.setdefault(label, end)
    return runs


def _load_counts(intervals: list[Row]) -> tuple[list[int], list[bool]]:
    lows = [set(json.loads(row["low_cards"])) if row["low_cards"] is not None else set() for row in intervals]
    counts = [0] * len(intervals)
    for _, start, end in _streaks(intervals, lows):
        for index in range(start, end):
            counts[index] += 1
    idle = [False] * len(intervals)
    for _, start, end in _streaks(intervals, [{"idle"} if len(cards) == row["gpu"] else set()
                                            for row, cards in zip(intervals, lows)]):
        idle[start:end] = [True] * (end - start)
    return counts, idle


def _load_details(
    intervals: list[Row], counts: list[int], idle: list[bool], since: int, now: int,
) -> tuple[list[dict], list[dict]]:
    episodes = []
    for index, (row, cards, whole_idle) in enumerate(zip(intervals, counts, idle)):
        start, end = max(since, row["start"]), min(now, row["end"])
        if not cards or start >= end:
            continue
        previous = episodes[-1] if episodes else None
        if (previous and index and _contiguous(intervals[index - 1], row) and previous["end"] == start
            and (previous["low_cards"], previous["busy_cards"], previous["whole_idle"]) ==
                (cards, row["busy_cards"], whole_idle)):
            previous["end"] = end
            previous["cpu_load"] = _merge_hint(previous["cpu_load"], row["cpu_load"])
            previous["gpu_memory_load"] = _merge_hint(previous["gpu_memory_load"], row["memory_load"])
        else:
            episodes.append({"start": start, "end": end, "low_cards": cards, "busy_cards": row["busy_cards"], "gpu": row["gpu"],
                             "whole_idle": whole_idle, "cpu_load": _merge_hint(False, row["cpu_load"]),
                             "gpu_memory_load": _merge_hint(False, row["memory_load"])})

    first_active = {}
    for _, start, _ in _streaks(intervals, [{"active"} if row["active"] else set() for row in intervals]):
        row = intervals[start]
        first_active.setdefault(row["boot"], row["start"])
    boots = []
    for boot, rows in groupby(intervals, lambda row: row["boot"]):
        if not since <= boot < now:
            continue
        first = first_active.get(boot)
        boundary = boot
        for row in rows:
            if first is not None and row["start"] >= first:
                break
            if row["start"] != boundary or row["load"] is None:
                break
            boundary = row["end"]
        boots.append({"boot_at": boot, "first_load_at": first, "wait_seconds": first - boot if first is not None else None,
                      "complete_before_load": first is not None and first == boundary})
    return episodes, boots


def _merge_hint(left: bool | None, right: int | None) -> bool | None:
    if left or right:
        return True
    return False if left is not None and right is not None else None


def _price_segments(row: Row, start: int, end: int, rates: list[tuple[int, str]], times: list[int]):
    price = row["platform_rate"]
    while start < end:
        finish = end
        rate, source = None, None
        if row["charge_type"] in HOURLY:
            if price is not None:
                rate, source = Decimal(price), "platform"
            else:
                at = bisect_right(times, start)
                if at < len(times):
                    finish = min(finish, times[at])
                if at:
                    rate, source = Decimal(rates[at - 1][1]), "manual"
        yield finish - start, rate, source
        start = finish


def _segments(row: Row, start: int, end: int, rates: list[tuple[int, str]], times: list[int], estimate_costs: bool):
    while start < end:
        day = _local_date(start)
        finish = min(end, _midnight(day + timedelta(days=1)))
        pieces = _price_segments(row, start, finish, rates, times) if estimate_costs else [(finish - start, None, None)]
        for seconds, rate, rate_source in pieces:
            yield day.isoformat(), seconds, rate, rate_source
        start = finish


def summary(
    conn: Connection, project_id: str, period: UsageRange, now: int, instance_id: str | None = None,
    *, include_details: bool = True, basis: Basis | None = None,
) -> dict:
    from gpu_console.storage import dashboard_billed_cost, dashboard_cost_estimate, dashboard_cost_rollups

    with storage.transaction(conn, immediate=False):
        estimate_costs = basis is None
        report = dashboard_cost_rollups.read(conn, project_id, period, now, instance_id, estimate_costs=estimate_costs)
        if report is None:
            report = _summary(conn, project_id, period, now, instance_id, include_details, estimate_costs)
        elif include_details:
            report.update(_details(conn, project_id, period, now, instance_id))
        if basis is not None:
            report["billing"] = billing.summary(conn, project_id, period, basis, now, instance_id)
            report["today_estimate"] = dashboard_cost_estimate.read(conn, project_id, now, instance_id)
            known = {item["instance_id"] for item in report["instances"]}
            charged = report["billing"]["instances"].keys() | report["today_estimate"]["instances"].keys()
            for key in sorted(charged - known):
                instance = instance_history.get(conn, project_id, key) or {}
                report["instances"].append(Totals().view(False) | {
                    "instance_id": key, "name": instance.get("name") or report["billing"]["instance_names"].get(key) or key,
                    "gpu_model": instance.get("gpu_model"), "charge_type": instance.get("charge_type"),
                    "current": False, "current_hourly_rate_cny": None, "current_rate_observed_at": None, "manual_rates": [],
                })
            dashboard_billed_cost.apply(conn, project_id, now, instance_id, report)
        return report


def _rates(conn: Connection, project_id: str, instance_id: str | None) -> dict[str, list[tuple[int, str]]]:
    scope = " AND instance_id=?" if instance_id is not None else ""
    params = (project_id, instance_id) if instance_id is not None else (project_id,)
    rates: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for key, at, price in conn.execute(
        "SELECT instance_id,effective_at,hourly_rate FROM dashboard_cost_manual_rates WHERE project_id=?"
        + scope + " ORDER BY effective_at", params,
    ):
        rates[key].append((at, price))
    return rates


def _interval_groups(conn: Connection, project_id: str, since: int, until: int, instance_id: str | None):
    scope = " AND instance_id=?" if instance_id is not None else ""
    params = (project_id, instance_id) if instance_id is not None else (project_id,)
    data = conn.execute(
        "SELECT * FROM dashboard_cost_intervals WHERE project_id=?" + scope
        + " AND end>? AND end<=? ORDER BY instance_id,start",
        (*params, since - SUSTAINED_SECONDS, until),
    )
    for key, rows in groupby(data, lambda row: row["instance_id"]):
        intervals = list(rows)
        if intervals[-1]["end"] > since:
            yield key, intervals


def _first_day(period: UsageRange, now: int):
    return _local_date(now) - timedelta(days=PERIOD_DAYS[period] - 1)


def _day_totals(period: UsageRange, now: int) -> dict[str, Totals]:
    first_day = _first_day(period, now)
    return {(first_day + timedelta(days=offset)).isoformat(): Totals() for offset in range(PERIOD_DAYS[period])}


def _progress(conn: Connection, project_id: str) -> Row | None:
    return conn.execute(
        "SELECT first_at,sampled_at FROM dashboard_cost_progress WHERE project_id=?", (project_id,),
    ).fetchone()


def _summary(
    conn: Connection, project_id: str, period: UsageRange, now: int, instance_id: str | None = None,
    include_details: bool = True, estimate_costs: bool = True,
) -> dict:
    since = _midnight(_first_day(period, now))
    progress = _progress(conn, project_id)
    first_at = progress["first_at"] if progress else None
    rates = _rates(conn, project_id, instance_id) if estimate_costs else {}
    total = Totals()
    days = _day_totals(period, now)
    instances, per_instance, episodes, boots = {}, {}, [], []
    data_through = None
    for key, intervals in _interval_groups(conn, project_id, since, now, instance_id):
        item = Totals()
        instance_rates = rates.get(key, [])
        times = [at for at, _ in instance_rates]
        counts, idle = _load_counts(intervals)
        last = intervals[-1]
        data_through = max(data_through or 0, last["end"])
        identity = {"instance_id": key, "name": last["name"]}
        if include_details:
            windows, starts = _load_details(intervals, counts, idle, since, now)
            episodes.extend(identity | window for window in windows)
            boots.extend(identity | event for event in starts)
        for row, low, whole_idle in zip(intervals, counts, idle):
            recovered = first_at is not None and row["start"] < first_at
            for day, seconds, price, source in _segments(
                row, max(row["start"], since), row["end"], instance_rates, times, estimate_costs,
            ):
                for totals in (total, item, days[day]):
                    totals.add(row, seconds, low, whole_idle, price, source, recovered)
        per_instance[key] = item
        instances[key] = identity | {"gpu_model": last["gpu_model"], "charge_type": last["charge_type"]}
    report = _format_summary(conn, project_id, period, now, instance_id, progress, rates, total, days, instances, per_instance,
                             estimate_costs=estimate_costs)
    return report | _detail_view(episodes, boots) | {"data_through": data_through, "aggregation_pending": False}


def _storage_view(compute: Totals, estimate: storage_cost.Estimate, empty_known: bool) -> dict:
    amount = estimate.amount(empty_known)
    compute_amount = compute.cost if compute.platform_seconds or compute.manual_seconds else None
    total = (compute_amount or Decimal(0)) + (amount or Decimal(0))
    return {
        "storage_cost_cny": money(amount) if amount is not None else None,
        "estimated_total_cost_cny": money(total) if amount is not None or compute_amount is not None else None,
        "unpriced_storage_hours": estimate.missing_seconds / 3600,
    }


def _format_summary(
    conn: Connection, project_id: str, period: UsageRange, now: int, instance_id: str | None,
    progress: Row | None, rates: dict[str, list[tuple[int, str]]], total: Totals,
    days: dict[str, Totals], instances: dict[str, dict], per_instance: dict[str, Totals],
    *, estimate_costs: bool = True,
) -> dict:
    since = _midnight(_first_day(period, now))
    snapshot = storage.snapshot(conn, "instances", project_id)
    current = {row["id"]: row for row in snapshot["data"] if instance_id is None or row["id"] == instance_id}
    location = None
    if estimate_costs and instance_id is not None:
        selected = current.get(instance_id) or instance_history.get(conn, project_id, instance_id) or {}
        if selected.get("region") and selected.get("zone"):
            location = (selected["region"], selected["zone"])
    estimate = (storage_cost.read(conn, project_id, since, now, instance_id, instance_location=location)
                if estimate_costs else storage_cost.Report())
    empty_known = estimate.status == "complete"
    for key in current.keys() | estimate.instances.keys():
        if key in instances:
            continue
        row = current.get(key) or instance_history.get(conn, project_id, key) or {}
        instances[key] = {"instance_id": key, "name": row.get("name") or key, "gpu_model": row.get("gpu_model"),
                          "charge_type": row.get("charge_type")}
    for key, item in instances.items():
        latest = current.get(key, {})
        compute = per_instance.get(key, Totals())
        item.update(compute.view(estimate_costs))
        if estimate_costs:
            item.update(_storage_view(compute, estimate.instances[key], empty_known))
        current_rate = (_platform_rate(latest.get("instance_price_per_hour"))
                        if estimate_costs and latest.get("charge_type") in HOURLY else None)
        item.update(current=key in current, current_hourly_rate_cny=current_rate,
                    current_rate_observed_at=snapshot["success_at"] if current_rate is not None else None,
                    manual_rates=[{"effective_at": at, "hourly_rate": price} for at, price in rates.get(key, ())])
    daily = []
    for offset, (date, counts) in enumerate(days.items()):
        day = since + offset * 86400
        amount = _storage_view(counts, estimate.days[day], day in estimate.empty_known_days) if estimate_costs else {}
        daily.append({"date": date} | counts.view(estimate_costs) | amount)
    amount = _storage_view(total, estimate.total, empty_known) if estimate_costs else {}
    return total.view(estimate_costs) | amount | {
        "as_of": now, "since": since, "range": period,
        "recording_since": progress["first_at"] if progress else None,
        "last_collected_at": progress["sampled_at"] if progress else None,
        "storage_recording_since": estimate.recording_since,
        "storage_data_through": estimate.data_through,
        "storage_cost_status": estimate.status,
        "storage_scope_gap_hours": estimate.gap_seconds / 3600,
        "days": daily,
        "instances": sorted(instances.values(), key=lambda item: (-(item["gpu_hours"] or 0), item["instance_id"])),
    }


def storage_details(conn: Connection, project_id: str, period: UsageRange, now: int) -> dict:
    def amount(estimate: storage_cost.Estimate) -> str | None:
        value = estimate.amount()
        return money(value) if value is not None else None

    with storage.transaction(conn, immediate=False):
        since = _midnight(_first_day(period, now))
        report = storage_cost.read(conn, project_id, since, now, include_resources=True)
        current = storage.snapshot(conn, "instances", project_id)["data"]
        names = {row["id"]: row.get("name") for row in instance_history.list_project(conn, project_id) + current}
        resources = []
        for (region, zone, kind, resource_id), resource in sorted(report.resources.items()):
            resources.append({
                "region": region, "zone": zone, "kind": kind, "resource_id": resource_id, "name": resource.name,
                "storage_cost_cny": amount(resource.totals),
                "unpriced_storage_hours": resource.totals.missing_seconds / 3600,
                "charge_types": sorted(resource.charge_types), "price_sources": sorted(resource.sources),
                "allocations": [
                    {"instance_id": key or None, "name": (names.get(key) or key) if key else None,
                     "storage_cost_cny": amount(totals), "unpriced_storage_hours": totals.missing_seconds / 3600}
                    for key, totals in sorted(resource.instances.items())
                ],
            })
        return {"data": resources, "project_only_storage_cost_cny": amount(report.project_only),
                "storage_cost_status": report.status}


def _detail_view(episodes: list[dict], boots: list[dict]) -> dict:
    return {
        "low_load_windows": sorted(episodes, key=lambda row: (-row["end"], row["instance_id"]))[:20],
        "boot_waits": sorted(boots, key=lambda row: (-row["boot_at"], row["instance_id"]))[:20],
    }


def _details(conn: Connection, project_id: str, period: UsageRange, now: int, instance_id: str | None) -> dict:
    since = _midnight(_first_day(period, now))
    episodes, boots = [], []
    for key, intervals in _interval_groups(conn, project_id, since, now, instance_id):
        counts, idle = _load_counts(intervals)
        windows, starts = _load_details(intervals, counts, idle, since, now)
        identity = {"instance_id": key, "name": intervals[-1]["name"]}
        episodes.extend(identity | window for window in windows)
        boots.extend(identity | event for event in starts)
    return _detail_view(episodes, boots)


def details(conn: Connection, project_id: str, period: UsageRange, now: int, instance_id: str | None = None) -> dict:
    with storage.transaction(conn, immediate=False):
        return _details(conn, project_id, period, now, instance_id)
