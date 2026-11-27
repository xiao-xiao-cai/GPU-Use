"""Check live capacity and price without granting authority to place an order."""

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError, CompShare
from gpu_console.application import inventory_prices
from gpu_console.application.instance_quotes import QUOTE_SECONDS
from gpu_console.application.purchase_options import platform_candidates
from gpu_console.application.task_config import SPEC_FIELDS


class QuoteWorker:
    def __init__(self, path: Path, cloud: CompShare | None, *, clock: Callable[[], int] = storage.now):
        self.path = path
        self.cloud = cloud
        self.now = clock

    def tick(self) -> bool:
        started_at = self.now()
        with storage.connect(self.path) as conn:
            row = conn.execute(
                """SELECT id,project_id,config FROM instance_quotes WHERE state='queued' AND created_at>?
                   ORDER BY created_at,id LIMIT 1""",
                (started_at - QUOTE_SECONDS,),
            ).fetchone()
        if row is None:
            return False
        config = json.loads(row["config"])
        spec = {key: config[key] for key in (*SPEC_FIELDS, "quantity", "disks") if key in config}
        zone = config["zones"][0]
        spec.update({key: zone[key] for key in ("region", "zone", "shared_storage") if key in zone},
                    project_id=row["project_id"])
        if config.get("cpu_auto"):
            spec["cpu_auto"] = True
        price, reason = None, "无法核实目标规格的库存"
        try:
            with storage.connect(self.path) as conn:
                candidates = platform_candidates(conn, spec)
        except ValueError as error:
            candidates, reason = [], str(error)
        for candidate in candidates:
            price, reason = self._quote(candidate)
            if price is not None:
                if config.get("cpu_auto"):
                    config.update(cpu_platform=candidate["cpu_platform"], cpu_locked=True)
                break
        now = self.now()
        with storage.connect(self.path) as conn:
            conn.execute(
                """UPDATE instance_quotes SET state=?,price=?,reason=?,quoted_at=?,expires_at=?,config=?
                   WHERE id=? AND state='queued' AND created_at>?""",
                ("ready" if price is not None else "failed", price, reason,
                 now if price is not None else None, now + QUOTE_SECONDS if price is not None else None,
                 json.dumps(config, ensure_ascii=False),
                 row["id"], now - QUOTE_SECONDS),
            )
        return True

    def _quote(self, spec: dict) -> tuple[str | None, str | None]:
        if self.cloud is None:
            return None, "优云智算凭据未配置"
        try:
            capacity = self.cloud.capacity(spec)
            if capacity is not True:
                return None, "目标规格当前缺货" if capacity is False else "无法核实目标规格的库存"
            price = self.cloud.quote(spec)
        except CloudError as error:
            return None, str(error)
        if not isinstance(price, Decimal) or not price.is_finite() or price < 0:
            return None, "无法核实同一计费周期的报价"
        return str(price), None


class InventoryPriceWorker:
    """Look up one catalog price without checking capacity or placing an order."""

    def __init__(self, path: Path, cloud: CompShare | None, *, clock: Callable[[], int] = storage.now):
        self.path = path
        self.cloud = cloud
        self.now = clock

    def tick(self) -> bool:
        started_at = self.now()
        with storage.connect(self.path) as conn:
            row = conn.execute(
                """SELECT p.project_id,p.spec_id,p.charge_type,p.next_at,p.amount,p.source,p.observed_at
                   FROM inventory_prices AS p LEFT JOIN inventory_price_targets AS t
                     ON p.project_id=t.project_id AND p.spec_id=t.spec_id AND p.charge_type='Postpay'
                   WHERE p.next_at<=? AND (p.accessed_at>=? OR t.spec_id IS NOT NULL)
                   ORDER BY NOT (p.state='queued' AND p.accessed_at>0),p.next_at,p.project_id,p.spec_id LIMIT 1""",
                (started_at, started_at - inventory_prices.RETENTION_SECONDS),
            ).fetchone()
            if row is None:
                return False
            project, key, charge, next_at, previous_amount, previous_source, previous_observed_at = row
            try:
                spec = inventory_prices.resolve(conn, project, key, charge)
            except (KeyError, ValueError) as failure:
                spec, error = None, failure.args[0]

        amount, source = None, None
        if spec is not None:
            if self.cloud is None:
                error = "优云智算凭据未配置"
            else:
                try:
                    price, source = self.cloud.inventory_price(spec)
                    amount, error = str(price), None
                except CloudError as failure:
                    error = str(failure)
        now = self.now()
        if amount is not None:
            observed_at = now
            scheduled_at = now + inventory_prices.REFRESH_SECONDS
        else:
            amount, source, observed_at = previous_amount, previous_source, previous_observed_at
            scheduled_at = now + (
                inventory_prices.REFRESH_SECONDS if amount is not None else inventory_prices.INITIAL_RETRY_SECONDS
            )
        with storage.connect(self.path) as conn:
            conn.execute(
                """UPDATE inventory_prices SET state=?,amount=?,source=?,observed_at=?,error=?,next_at=?
                   WHERE project_id=? AND spec_id=? AND charge_type=? AND next_at=?""",
                ("ready" if amount is not None else "failed", amount, source, observed_at, error, scheduled_at,
                 project, key, charge, next_at),
            )
        return True
