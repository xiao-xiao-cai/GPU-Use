"""Replay inventory windows for an upcoming training deadline."""

from bisect import bisect_left, bisect_right
from decimal import Decimal
from sqlite3 import Connection
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gpu_console import storage
from gpu_console.application import availability, watches
from gpu_console.storage import availability as observations
from gpu_console.storage import capacity
from gpu_console.storage.dashboard_cost import money

DAY = 86400
SLOT = observations.SLOT_SECONDS
MIN_DAYS = 3
MIN_COVERAGE = 0.8
MIN_HIT_RATE = 0.8
Id = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
Rate = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=6)]


class RentalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watch_id: Id | None = None
    region: Id | None = None
    zone: Id | None = None
    pool: Literal["exclusive", "spot"] | None = None
    hourly_rate: Rate | None = Field(default=None, description="算力费，元/实例小时，不含存储和镜像")

    @model_validator(mode="after")
    def validate_scope(self):
        scope = (self.region, self.zone, self.pool)
        if (self.watch_id is not None and any(scope)) or (self.watch_id is None and not all(scope)):
            raise ValueError("请选择关注规格，或提供完整可用区和资源池")
        return self


class RentalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gpu_model: str = Field(min_length=1, max_length=100)
    cards: Literal[1, 2, 4, 8]
    target_start: int = Field(gt=0, strict=True)
    latest_start: int = Field(gt=0, strict=True)
    training_minutes: int = Field(gt=0, le=60 * 24 * 30, strict=True)
    preparation_minutes: int = Field(default=15, ge=0, le=60 * 24, strict=True)
    candidates: list[RentalCandidate] = Field(min_length=1, max_length=8)


def _scope(conn: Connection, project_id: str, request: RentalRequest, candidate: RentalCandidate,
           now: int, pools: dict[tuple, dict], combos: dict, profiles: dict) -> tuple[tuple, str]:
    if candidate.watch_id is None:
        return (candidate.region, candidate.zone, request.gpu_model, candidate.pool), capacity.card_key(request.cards)
    row = conn.execute("SELECT * FROM watches WHERE project_id=? AND id=?", (project_id, candidate.watch_id)).fetchone()
    if row is None:
        raise KeyError("该项目下找不到关注规格")
    item = watches.view(conn, row, now, pools=pools, combos=combos, profiles=profiles)
    spec = item["spec"]
    if item["binding_status"] != "bound" or (spec.get("gpu_model"), spec.get("gpu")) != (request.gpu_model, request.cards):
        raise ValueError("关注规格需绑定目录，且机型和卡数须与本次试算一致")
    return watches.pool_key(spec), item["catalog_spec_id"]


def _replay(known: list[int], available: list[int], start: int, end: int, now: int) -> dict:
    hit, missed, unknown, observed_days, seen, expected = 0, 0, 0, 0, 0, 0
    slots = (observations.slot_start(end) - observations.slot_start(start)) // SLOT + 1
    for day in range(1, 8):
        left, right = start - DAY * day, end - DAY * day
        if not known or right >= now or right < known[0]:
            continue
        count = bisect_right(known, right) - bisect_left(known, left)
        observed_days += bool(count)
        seen += count
        expected += slots
        if bisect_right(available, right) > bisect_left(available, left):
            hit += 1
        elif count == slots:
            missed += 1
        else:
            unknown += 1
    days = hit + missed + unknown
    return {
        "hit_days": hit, "miss_days": missed, "unknown_days": unknown, "days": days, "observed_days": observed_days,
        "coverage": seen / expected if expected else None, "hit_rate": hit / days if days else None,
    }


def _option(request: RentalRequest, rate: Decimal | None, start: int, cutoff: int, replay: dict) -> dict:
    prep = request.preparation_minutes * 60
    wait = max(request.target_start - (start + prep), 0)
    billable = wait + prep + request.training_minutes * 60
    cost = rate * billable / 3600 if rate is not None else None
    eligible = (
        replay["observed_days"] >= MIN_DAYS and (replay["coverage"] or 0) >= MIN_COVERAGE
        and (replay["hit_rate"] or 0) >= MIN_HIT_RATE
    )
    return {
        "start_search_at": start, "latest_search_at": cutoff,
        "training_start_if_acquired_immediately": start + prep + wait,
        "advance_hold_seconds": wait, "estimated_cost_cny": money(cost) if cost is not None else None,
        "eligible": eligible, "history": replay,
    }


def _order(option: dict) -> tuple:
    cost = option["estimated_cost_cny"]
    return (0 if cost is not None else 1, Decimal(cost) if cost is not None else Decimal(0),
            -(option["history"]["hit_rate"] or 0), -option["start_search_at"])


def _savings(option: dict, baseline: dict, rate: Decimal | None) -> str | None:
    return money(rate * (baseline["advance_hold_seconds"] - option["advance_hold_seconds"]) / 3600) if rate is not None else None


def plan(conn: Connection, project_id: str, request: RentalRequest, now: int) -> dict:
    if not now <= request.target_start <= request.latest_start <= now + DAY:
        raise ValueError("计划开训和最迟开训时间须按先后顺序位于未来 24 小时内")
    cutoff = request.latest_start - request.preparation_minutes * 60
    with storage.transaction(conn, immediate=False):
        pools = {(row["region"], row["zone"], row["gpu_model"], row["pool"]): row
                 for row in availability.pool_snapshot(conn, now)["data"]}
        combos = watches.current_catalog(conn)
        profiles = capacity.profiles(conn, project_id, combos)
        rows, qualified, seen = [], [], set()
        sufficient_history = False
        for candidate in request.candidates:
            key, evidence_key = _scope(conn, project_id, request, candidate, now, pools, combos, profiles)
            if (key, evidence_key) in seen:
                raise ValueError("同一资源池或关注规格只需选择一次")
            seen.add((key, evidence_key))
            stock = pools.get(key)
            if stock is None:
                raise KeyError("库存目录中找不到该机型、可用区和资源池")
            kind = availability.INSTANCE_TYPE_BY_POOL[key[-1]]
            profile = profiles.get((*key[:3], kind))
            samples = capacity.history(conn, capacity.series(profile, evidence_key), now - observations.WINDOW_SECONDS,
                                       now, known_only=True)
            known = [sample["observed_at"] for sample in samples]
            available = [sample["observed_at"] for sample in samples if sample["available"]]
            current, checked_at = capacity.current(profile, evidence_key, now)
            rate = candidate.hourly_rate
            baseline, later = None, None
            starts = [now] + list(range(observations.slot_start(now) + SLOT, cutoff + 1, SLOT)) if now <= cutoff else []
            for start in starts:
                replay = _replay(known, available, start, cutoff, now)
                sufficient_history |= replay["observed_days"] >= MIN_DAYS and (replay["coverage"] or 0) >= MIN_COVERAGE
                option = _option(request, rate, start, cutoff, replay)
                if start == now:
                    baseline = option
                elif option["eligible"] and stock["state"] != "unsupported" and (later is None or _order(option) < _order(later)):
                    later = option
            possible = [item for item in (baseline, later) if item is not None and item["eligible"]
                        and stock["state"] != "unsupported"]
            for option in (baseline, later):
                if option is not None:
                    option["savings_vs_now_cny"] = _savings(option, baseline, rate)
            if possible:
                qualified.append((len(rows), min(possible, key=_order)))
            count = stock["remaining_cards"]
            rows.append({
                **{field: stock[field] for field in ("region", "zone", "pool", "state", "provider_updated_at")},
                "watch_id": candidate.watch_id, "remaining_cards": count, "available": current,
                "capacity_checked_at": checked_at, "can_try_now": now <= cutoff and current is True,
                "hourly_rate_cny": str(rate) if rate is not None else None, "price_source": "manual" if rate is not None else None,
                "known_samples": len(known), "available_samples": len(available), "now": baseline, "later": later,
            })

    recommendation = None
    if now > cutoff:
        status = "deadline_too_close"
    elif qualified:
        index, best = min(qualified, key=lambda item: _order(item[1]))
        status = "ready" if best["estimated_cost_cny"] is not None else "availability_only"
        recommendation = {"candidate_index": index, **best}
    else:
        status = "no_window" if sufficient_history else "insufficient"
    return {
        "as_of": now, "gpu_model": request.gpu_model, "cards": request.cards, "target_start": request.target_start,
        "latest_start": request.latest_start, "preparation_minutes": request.preparation_minutes,
        "training_minutes": request.training_minutes, "latest_search_at": cutoff, "status": status,
        "incomplete_prices": any(option["estimated_cost_cny"] is None for _, option in qualified),
        "recommendation": recommendation, "candidates": rows,
    }
