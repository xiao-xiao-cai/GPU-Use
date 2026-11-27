"""CompShare order amounts and the console's two billing date bases."""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

Basis = Literal["order_date", "service_period"]
Category = Literal["compute", "storage", "image", "other"]
Amounts = tuple[int, int, int, int]  # Cash, order amount, coupon, and gifted credit, in CNY cents.
CATEGORIES: tuple[Category, ...] = ("compute", "storage", "image", "other")
AMOUNT_FIELDS = ("cash_cny", "order_amount_cny", "coupon_cny", "free_credit_cny")
SERVICE_ORDERS = {"OT_POSTPAID_PAYMENT", "OT_BUY", "OT_RENEW"}
HISTORY_MONTHS = 13


@dataclass(frozen=True)
class Order:
    billing_project_id: str
    project_name: str
    order_no: str
    order_type: str
    resource_id: str | None
    resource_type: str
    resource_name: str | None
    charge_type: str | None
    created_at: int | None
    start_at: int | None
    end_at: int | None
    amounts: Amounts | None


def cycles(day: date, count: int = HISTORY_MONTHS) -> list[str]:
    start = day.replace(day=1)
    result = []
    for _ in range(count):
        result.append(start.isoformat()[:7])
        start = (start - timedelta(days=1)).replace(day=1)
    return result


def amount_view(amounts: Amounts | None) -> dict:
    def format_cents(value: int) -> str:
        units, cents = divmod(abs(value), 100)
        return f"{'-' if value < 0 else ''}{units}.{cents:02}"

    return {key: format_cents(amounts[i]) if amounts is not None else None for i, key in enumerate(AMOUNT_FIELDS)}


def apportioned(value: int, before: int, through: int, duration: int) -> int:
    amount = abs(value)
    share = amount * through // duration - amount * before // duration
    return -share if value < 0 else share
