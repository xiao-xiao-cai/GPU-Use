"""Create, inspect and pause instance automation rules."""

from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gpu_console import storage
from gpu_console.api.auth import require_session
from gpu_console.api.resources import Channel, Db, Id, Name
from gpu_console.application import automation_rules

router = APIRouter(prefix="/api/automation-rules", dependencies=[Depends(require_session)])
HourMinute = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
Timestamp = Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]
Threshold = Annotated[float, Field(strict=True, gt=0, le=100, allow_inf_nan=False)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Window(Input):
    weekday: int = Field(strict=True, ge=1, le=7)
    start: HourMinute
    end: HourMinute

    @model_validator(mode="after")
    def check_duration(self) -> Self:
        if self.start == self.end:
            raise ValueError("时段开始和结束时间必须不同")
        return self


class Weekly(Input):
    mode: Literal["weekly"]
    windows: list[Window] = Field(min_length=1, max_length=70)


class Once(Input):
    mode: Literal["once"]
    start_at: Timestamp
    end_at: Timestamp

    @model_validator(mode="after")
    def check_duration(self) -> Self:
        if self.start_at >= self.end_at:
            raise ValueError("结束时间必须晚于开始时间")
        return self


class RuleConfig(Input):
    cpu_threshold: Threshold = 10
    gpu_threshold: Threshold = 10
    vram_threshold: Threshold = 10
    duration_minutes: int = Field(default=60, strict=True, ge=1, le=10080)
    schedule: Annotated[Weekly | Once | None, Field(discriminator="mode")] = None
    email: str | None = Field(default=None, min_length=1, max_length=254)


class Rule(Input):
    project_id: Id
    instance_id: Id
    name: Name
    kind: Literal["idle_stop", "runtime_stop", "cardless_idle_stop"]
    config: RuleConfig
    channels: list[Channel] = Field(default_factory=lambda: ["site"], max_length=3)

    @model_validator(mode="after")
    def check_kind(self) -> Self:
        if self.kind != "runtime_stop":
            if self.config.schedule is None or self.config.duration_minutes > 1440:
                raise ValueError("空闲停机需指定时段，空闲时间为 1 至 1440 分钟")
        elif self.config.schedule is not None or self.config.duration_minutes < 5:
            raise ValueError("运行上限为 5 至 10080 分钟")
        return self


def _save(body: Rule, conn: Db, rule_id: str | None = None) -> dict:
    try:
        with storage.transaction(conn):
            return automation_rules.save_bound_rule(conn, body.model_dump(exclude_none=True), storage.now(), rule_id)
    except automation_rules.RuleTargetUnavailable as error:
        raise HTTPException(503, str(error)) from error


@router.get("")
def list_rules(conn: Db, project_id: Id | None = None, instance_id: Id | None = None) -> list[dict]:
    if instance_id is not None and project_id is None:
        raise HTTPException(422, "按实例查询需要明确项目 ID")
    return automation_rules.list_rules(conn, project_id, instance_id, storage.now())


@router.post("", status_code=201)
def create_rule(body: Rule, conn: Db) -> dict:
    return _save(body, conn)


@router.put("/{rule_id}")
def update_rule(rule_id: Id, body: Rule, conn: Db) -> dict:
    return _save(body, conn, rule_id)


@router.post("/{rule_id}/{action}")
def control_rule(rule_id: Id, action: Literal["pause", "resume"], conn: Db) -> dict:
    with storage.transaction(conn):
        return automation_rules.control_rule(conn, rule_id, action, storage.now())
