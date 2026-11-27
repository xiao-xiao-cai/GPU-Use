"""Estimate today's observed usage that has no matching paid service window."""

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from sqlite3 import Connection

from gpu_console import storage
from gpu_console.storage import billing, dashboard_cost as cost, instance_history, storage_cost
from gpu_console.storage.dashboard_usage import _local_date, _midnight


def _uncovered(start: int, end: int, windows: list[tuple[int, int]]) -> Iterator[tuple[int, int]]:
    for left, right in windows:
        if right <= start:
            continue
        if left >= end:
            break
        if start < left:
            yield start, left
        start = max(start, right)
        if start >= end:
            return
    if start < end:
        yield start, end


@dataclass
class Estimate:
    compute: cost.Totals = field(default_factory=cost.Totals)
    storage: storage_cost.Estimate = field(default_factory=storage_cost.Estimate)
    compute_seen: bool = False
    storage_seen: bool = False
    cardless: storage_cost.Estimate = field(default_factory=storage_cost.Estimate)
    cardless_seen: bool = False
    cardless_review: bool = False
    cardless_sources: set[str] = field(default_factory=set)
    review: set[tuple[str, str]] = field(default_factory=set)

    def merge(self, other: "Estimate") -> None:
        self.compute.merge(other.compute)
        self.storage.cost += other.storage.cost
        self.storage.priced_seconds += other.storage.priced_seconds
        self.storage.missing_seconds += other.storage.missing_seconds
        self.compute_seen |= other.compute_seen
        self.storage_seen |= other.storage_seen
        self.cardless.cost += other.cardless.cost
        self.cardless.priced_seconds += other.cardless.priced_seconds
        self.cardless.missing_seconds += other.cardless.missing_seconds
        self.cardless_seen |= other.cardless_seen
        self.cardless_review |= other.cardless_review
        self.cardless_sources.update(other.cardless_sources)
        self.review.update(other.review)

    def view(self, compute_complete: bool, storage_complete: bool) -> dict:
        compute, disks = self.compute, self.storage
        compute_review = any(kind == "compute" for kind, _ in self.review)
        cardless_missing = bool(self.cardless.missing_seconds or self.cardless_review)
        compute_missing = bool(compute.unpriced_seconds or compute_review or cardless_missing)
        storage_missing = bool(disks.missing_seconds or any(kind == "storage" for kind, _ in self.review))
        cardless_known = self.cardless.priced_seconds or (self.cardless_seen and not cardless_missing)
        compute_known = (compute.platform_seconds or compute.manual_seconds or cardless_known
                         or ((compute_complete or self.compute_seen) and not compute_missing))
        storage_known = disks.priced_seconds or ((storage_complete or self.storage_seen) and not storage_missing)
        metrics = compute.view()
        return {
            "compute_cny": cost.money(compute.cost + self.cardless.cost) if compute_known else None,
            "cardless_compute_cny": cost.money(self.cardless.cost) if cardless_known else None,
            "cardless_unpriced_hours": self.cardless.missing_seconds / 3600,
            "cardless_rate_sources": sorted(self.cardless_sources),
            "storage_cny": cost.money(disks.cost) if storage_known else None,
            "total_cny": cost.money(compute.cost + self.cardless.cost + disks.cost) if compute_known or storage_known else None,
            "low_load_cost_cny": metrics["low_load_cost_cny"],
            "low_load_cost_ratio": metrics["low_load_cost_ratio"],
            "idle_cost_cny": metrics["idle_cost_cny"],
            "cost_per_weighted_gpu_hour_cny": metrics["cost_per_weighted_gpu_hour_cny"],
            "unpriced_instance_hours": (compute.unpriced_seconds + self.cardless.missing_seconds) / 3600,
            "unpriced_storage_hours": disks.missing_seconds / 3600,
            "review_resource_count": len(self.review),
            "status": ("review" if self.review else "ready" if (
                compute_complete and storage_complete and not compute_missing and not storage_missing
            ) else "partial" if compute_known or storage_known else "pending"),
        }


def read(conn: Connection, project_id: str, now: int, instance_id: str | None) -> dict:
    date = _local_date(now)
    since = _midnight(date)
    windows = billing.estimate_coverage(conn, project_id, since, now)
    estimates: dict[str, Estimate] = defaultdict(Estimate)
    rates = cost._rates(conn, project_id, instance_id)
    compute_through = None
    for key, intervals in cost._interval_groups(conn, project_id, since, now, instance_id):
        low_counts, idle_flags = cost._load_counts(intervals)
        applicable_rates = rates.get(key, [])
        rate_times = [at for at, _ in applicable_rates]
        coverage_key = ("compute", key)
        paid = windows.get(coverage_key, [])
        previous_end = since
        for row, low, idle in zip(intervals, low_counts, idle_flags):
            start, end = max(previous_end, row["start"]), row["end"]
            if start >= end or row["charge_type"] not in cost.HOURLY:
                continue
            previous_end = end
            compute_through = max(compute_through or 0, end)
            estimate = estimates[key]
            estimate.compute_seen = True
            if paid is None:
                estimate.review.add(coverage_key)
                continue
            for left, right in _uncovered(start, end, paid):
                for seconds, rate, source in cost._price_segments(row, left, right, applicable_rates, rate_times):
                    estimate.compute.add(row, seconds, low, idle, rate, source, False)

    for row in conn.execute("""SELECT * FROM dashboard_cardless_intervals
        WHERE project_id=? AND end>? AND start<? AND (? IS NULL OR instance_id=?)
        ORDER BY instance_id,start""", (project_id, since, now, instance_id, instance_id)):
        key = row["instance_id"]
        estimate = estimates[key]
        estimate.cardless_seen = True
        end = min(row["end"], now)
        compute_through = max(compute_through or 0, end)
        coverage_key = ("compute", key)
        paid = windows.get(coverage_key, [])
        if paid is None:
            estimate.review.add(coverage_key)
            estimate.cardless_review = True
            continue
        for left, right in _uncovered(max(since, row["start"]), end, paid):
            seconds = right - left
            if row["rate"] is None:
                estimate.cardless.missing_seconds += seconds
            else:
                estimate.cardless.priced_seconds += seconds
                estimate.cardless.cost += Decimal(row["rate"]) * seconds / 3600
                estimate.cardless_sources.add(row["rate_source"])

    progress = cost._progress(conn, project_id)
    observed = conn.execute("SELECT observed_seconds FROM dashboard_usage_days WHERE project_id=? AND day=?",
                            (project_id, since)).fetchone()
    location = None
    known_instance = instance_id is None or instance_id in estimates
    if instance_id is not None:
        current = storage.snapshot(conn, "instances", project_id)["data"]
        selected = next((row for row in current if row["id"] == instance_id), None)
        selected = selected or instance_history.get(conn, project_id, instance_id) or {}
        known_instance = known_instance or bool(selected)
        if selected.get("region") and selected.get("zone"):
            location = selected["region"], selected["zone"]

    compute_complete = bool(known_instance and progress and observed and since < progress["sampled_at"] <= now
                            and now - progress["sampled_at"] <= cost.SAMPLE_FRESHNESS
                            and observed["observed_seconds"] == progress["sampled_at"] - since)
    if compute_complete:
        compute_through = progress["sampled_at"]

    segments, storage_through, storage_complete = storage_cost.metered_segments(conn, project_id, since, now, location)
    storage_complete = known_instance and storage_complete
    for resource, segment in segments:
        owner = segment.instance_id
        if instance_id is not None and owner != instance_id:
            continue
        estimate = estimates[owner]
        estimate.storage_seen = True
        coverage_key = ("storage", resource)
        paid = windows.get(coverage_key, [])
        if paid is None:
            estimate.review.add(coverage_key)
            continue
        for left, right in _uncovered(segment.start, segment.end, paid):
            seconds = right - left
            if segment.price is None:
                estimate.storage.missing_seconds += seconds
            else:
                estimate.storage.priced_seconds += seconds
                estimate.storage.cost += Decimal(segment.price["amount"]) * seconds / 3600

    total = Estimate()
    for estimate in estimates.values():
        total.merge(estimate)
    unassigned = estimates.pop("", Estimate())
    return total.view(compute_complete, storage_complete) | {
        "date": date.isoformat(), "compute_data_through": compute_through, "storage_data_through": storage_through,
        "project_only_storage_cny": unassigned.view(False, storage_complete)["storage_cny"] if instance_id is None else None,
        "instances": {key: estimate.view(compute_complete, storage_complete) for key, estimate in sorted(estimates.items())},
    }
