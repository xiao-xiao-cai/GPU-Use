"""Compose the catalog, pool stock and cached hourly prices for inventory readers."""

from decimal import Decimal, InvalidOperation
from sqlite3 import Connection, Row

from gpu_console.application import availability, watches
from gpu_console.storage import catalog


def _quote(row: Row | None, now: int) -> dict | None:
    if row is None or row["state"] != "ready" or row["source"] not in {"account", "catalog"}:
        return None
    try:
        amount = Decimal(row["amount"])
    except (TypeError, InvalidOperation):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return {
        "amount": str(amount), "source": row["source"], "observed_at": row["observed_at"],
        "stale": row["error"] is not None or row["next_at"] <= now,
    }


def _range(specs: list[dict]) -> dict:
    quoted = [spec["hourly_rate"] for spec in specs if spec["hourly_rate"] is not None]
    amounts = [Decimal(row["amount"]) for row in quoted]
    complete = bool(specs) and len(quoted) == len(specs)
    sources = {row["source"] for row in quoted}
    return {
        "quoted": len(quoted), "total": len(specs), "failed": sum(spec["quote_failed"] for spec in specs),
        "minimum": str(min(amounts)) if complete else None,
        "maximum": str(max(amounts)) if complete else None,
        "source": (next(iter(sources)) if len(sources) == 1 else "mixed") if complete else None,
        "stale": any(row["stale"] for row in quoted),
    }


def overview(conn: Connection, project_id: str | None, now: int, *, include_spot: bool) -> dict:
    if project_id is not None and not catalog.project_known(conn, project_id):
        raise KeyError("找不到该项目，请先同步项目")
    listing = watches.catalog_view(conn, now, project_id)
    stock = availability.pool_snapshot(conn, now)
    prices = {(row["spec_id"], row["charge_type"]): row for row in conn.execute(
        "SELECT * FROM inventory_prices WHERE project_id=? AND charge_type IN ('Postpay','Spot')", (project_id,),
    )}
    details = {watches.pool_key(row): row for row in listing["data"]
               if (row.get("instance_type") or "uhost") in catalog.POOL_BY_INSTANCE_TYPE}
    entries = []
    for pool in stock["data"]:
        if pool["pool"] == "spot" and not include_spot:
            continue
        key = tuple(pool[field] for field in ("region", "zone", "gpu_model", "pool"))
        row = details.get(key, {})
        charge = "Spot" if pool["pool"] == "spot" else "Postpay"
        specs = []
        for spec in sorted(row.get("specs", []), key=lambda r: (r["gpu"], r["cpu"], r["memory_gb"])):
            price = prices.get((spec["catalog_spec_id"], charge))
            specs.append(spec | {
                "hourly_rate": _quote(price, now),
                "quote_failed": price is not None and price["state"] == "failed",
            })
        entries.append(pool | {
            "listed": bool(row), "zone_name": row.get("zone_name") or pool["zone"],
            "graphics_memory_gb": row.get("graphics_memory_gb"),
            "instance_type": catalog.instance_type(charge), "status": row.get("status") or "Unknown",
            "specs": specs, "card_counts": sorted({spec["gpu"] for spec in specs}),
            "hourly_range": _range(specs),
        })
    return stock | {"data": entries, "catalog_success_at": listing["success_at"], "catalog_error": listing["error"]}
