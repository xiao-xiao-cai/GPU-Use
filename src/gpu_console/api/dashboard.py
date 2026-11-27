"""Dashboard reads and opt-in rental window reminders."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gpu_console import storage
from gpu_console.api.auth import require_session
from gpu_console.api.resources import Channel, Db, Id, feishu_status
from gpu_console.application import availability, rental_plan
from gpu_console.domain.billing import Basis, Category
from gpu_console.storage import billing, catalog, dashboard_cost, dashboard_usage

router = APIRouter(prefix="/api/dashboard", dependencies=[Depends(require_session)])
Filter = Annotated[str, Field(min_length=1, max_length=100)]


class AvailabilityQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["catalog", "watch", "pool"] = "pool"
    project_id: Id | None = None
    key: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    gpu_model: Filter | None = None
    region: Filter | None = None
    zone: Filter | None = None
    instance_type: Literal["uhost", "spot"] | None = None
    limit: int = Field(50, ge=1, le=100)
    offset: int = Field(0, ge=0, le=2**31 - 1)
    card_threshold: availability.CardThreshold = 1

    @field_validator("card_threshold", mode="before")
    @classmethod
    def parse_card_threshold(cls, value: object) -> object:
        return int(value) if isinstance(value, str) and value in {"1", "2", "4", "8"} else value


class RentalReminder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["off", "advance", "when_available"]
    channels: list[Channel] = Field(default_factory=lambda: ["site"], max_length=3)


@router.get("/usage")
def usage(project_id: Id, conn: Db, range: Literal["today", "7d", "30d"] = "7d") -> dict:
    if not catalog.project_known(conn, project_id):
        raise HTTPException(404, "当前项目不存在，请先同步项目列表")
    return dashboard_usage.summary(conn, project_id, range, storage.now())


@router.get("/costs")
def costs(
    project_id: Id, conn: Db, range: dashboard_usage.UsageRange = "7d", instance_id: Id | None = None,
    include_details: bool = True, basis: Basis = "order_date",
) -> dict:
    if not catalog.project_known(conn, project_id):
        raise HTTPException(404, "当前项目不存在，请先同步项目列表")
    return dashboard_cost.summary(conn, project_id, range, storage.now(), instance_id,
                                  include_details=include_details, basis=basis)


@router.get("/costs/details")
def cost_details(project_id: Id, conn: Db, range: dashboard_usage.UsageRange = "7d", instance_id: Id | None = None) -> dict:
    if not catalog.project_known(conn, project_id):
        raise HTTPException(404, "当前项目不存在，请先同步项目列表")
    return dashboard_cost.details(conn, project_id, range, storage.now(), instance_id)


@router.get("/costs/storage")
def storage_costs(
    project_id: Id, conn: Db, range: dashboard_usage.UsageRange = "7d",
    basis: Basis = "order_date", instance_id: Id | None = None,
) -> dict:
    if not catalog.project_known(conn, project_id):
        raise HTTPException(404, "当前项目不存在，请先同步项目列表")
    with storage.transaction(conn, immediate=False):
        return billing.storage_details(conn, project_id, range, basis, storage.now(), instance_id)


@router.get("/costs/orders")
def bill_orders(
    project_id: Id, conn: Db, range: dashboard_usage.UsageRange = "7d", basis: Basis = "order_date",
    instance_id: Id | None = None, category: Category | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50, offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    if not catalog.project_known(conn, project_id):
        raise HTTPException(404, "当前项目不存在，请先同步项目列表")
    with storage.transaction(conn, immediate=False):
        return billing.orders(conn, project_id, range, basis, storage.now(), instance_id, category, limit, offset)


@router.put("/costs/rates/{instance_id}")
def save_rate(instance_id: Id, project_id: Id, body: dashboard_cost.ManualRate, conn: Db) -> dict:
    if not catalog.project_known(conn, project_id):
        raise HTTPException(404, "当前项目不存在，请先同步项目列表")
    raise HTTPException(410, "历史成本已切换到平台账单")


@router.post("/rental-plan")
def plan_rental(project_id: Id, body: rental_plan.RentalRequest, conn: Db) -> dict:
    if not catalog.project_known(conn, project_id):
        raise HTTPException(404, "当前项目不存在，请先同步项目列表")
    return rental_plan.plan(conn, project_id, body, storage.now())


@router.get("/availability")
def trends(filters: Annotated[AvailabilityQuery, Query()], conn: Db) -> dict:
    return availability.query(conn, now=storage.now(), **filters.model_dump())


@router.put("/watches/{watch_id}/reminder")
def update_reminder(watch_id: Id, project_id: Id, body: RentalReminder, request: Request, conn: Db) -> dict:
    channels = list(dict.fromkeys(body.channels))
    with storage.transaction(conn):
        if body.mode != "off":
            if not channels:
                raise HTTPException(422, "请为租卡时段提醒选择至少一个渠道")
            if "feishu" in channels and not feishu_status(request, conn)[1]:
                raise HTTPException(409, "请先配置飞书通知渠道")
            if "email" in channels:
                status = conn.execute("SELECT error FROM system_status WHERE component='channel_email'").fetchone()
                if status is None or status["error"] != "configured":
                    raise HTTPException(409, "请先配置邮件通知渠道")
                if not storage.get_settings(conn)["notification_rules"]["email_recipient"]:
                    raise HTTPException(409, "请先在通知设置中填写邮件收件地址")
        return availability.set_reminder(conn, project_id=project_id, watch_id=watch_id, mode=body.mode, channels=channels)
