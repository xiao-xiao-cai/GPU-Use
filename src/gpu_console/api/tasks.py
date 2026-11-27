"""Manage task definitions, local requests and execution results."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from gpu_console import storage
from gpu_console.api.auth import require_session
from gpu_console.api.resources import Db, Id, configured_feishu
from gpu_console.application import instance_quotes, instance_resize
from gpu_console.application.task_config import BootMode, CardlessSpec
from gpu_console.application.tasks import InstanceActionUnavailable, TaskListView, TaskStore

router = APIRouter(prefix="/api", dependencies=[Depends(require_session)])


def task_store(request: Request) -> TaskStore:
    config = request.app.state.config
    return TaskStore(config.db, feishu_configured=lambda conn: configured_feishu(config, conn)[1])


Store = Annotated[TaskStore, Depends(task_store)]


class Trigger(BaseModel):
    request_id: Id


class TaskTrashChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: Id
    expected_version: int = Field(strict=True, ge=1)


class Reconciliation(BaseModel):
    action: Literal["bind", "release"]
    instance_id: Id | None = None
    note: str | None = Field(default=None, max_length=500)
    confirmed_no_order: StrictBool = False


class RunWarning(BaseModel):
    dismissed: StrictBool


class ShutdownReviewRequest(BaseModel):
    keep_stop_at: int | None = Field(default=None, strict=True, gt=0, le=2_147_483_647)


class InstanceAction(Trigger):
    project_id: Id
    instance_id: Id
    kind: Literal["start", "stop", "shutdown", "cancel_shutdown", "reboot", "rename"]
    boot_mode: BootMode | None = None
    without_gpu_spec: CardlessSpec | None = None
    stop_at: int | None = Field(default=None, gt=0, le=2_147_483_647)
    expected_stop_at: int | None = Field(default=None, gt=0, le=2_147_483_647)
    conflict: Literal["keep", "replace"] = "keep"
    name: str | None = Field(default=None, min_length=1, max_length=63)
    expected_name: str | None = Field(default=None, min_length=1, max_length=200)


class InstanceQuote(BaseModel):
    project_id: Id
    spec: dict


class InstancePurchase(Trigger):
    quote_id: Id
    channels: list[Literal["site", "feishu", "email"]] | None = Field(default=None, max_length=3)
    email: str | None = Field(default=None, max_length=254)


class InstancePurchaseRetryPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: Id
    spec: dict
    max_price: str = Field(min_length=1, max_length=24)
    window_seconds: int | None = Field(default=None, strict=True, ge=60, le=604800)
    retry: StrictBool = True
    channels: list[Literal["site", "feishu", "email"]] = Field(default_factory=lambda: ["site"], max_length=3)
    email: str | None = Field(default=None, max_length=254)


class InstancePurchaseRetry(Trigger):
    model_config = ConfigDict(extra="forbid")

    preview_id: Id


@router.post("/instance-purchase-requests/preview")
def preview_purchase_retry(body: InstancePurchaseRetryPreview, store: Store) -> dict:
    return store.preview_purchase_retry(body.model_dump())


@router.post("/instance-purchase-requests", status_code=202)
def submit_purchase_retry(body: InstancePurchaseRetry, store: Store, request: Request) -> dict:
    try:
        return store.submit_purchase_retry(
            body.preview_id, body.request_id, read_only=request.app.state.config.read_only,
        )
    except InstanceActionUnavailable as error:
        raise HTTPException(409, str(error)) from error


@router.post("/instance-quotes", status_code=202)
def create_instance_quote(body: InstanceQuote, conn: Db) -> dict:
    with storage.transaction(conn):
        return instance_quotes.create(conn, body.project_id, body.spec, storage.now())


@router.get("/instance-quotes/{quote_id}")
def get_instance_quote(quote_id: Id, conn: Db) -> dict:
    return instance_quotes.view(instance_quotes.get(conn, quote_id), storage.now())


@router.post("/instances", status_code=202)
def purchase_instance(body: InstancePurchase, store: Store, request: Request) -> dict:
    try:
        return store.submit_purchase(
            body.quote_id, body.request_id, read_only=request.app.state.config.read_only,
            channels=body.channels, email=body.email,
        )
    except InstanceActionUnavailable as error:
        raise HTTPException(409, str(error)) from error


@router.get("/tasks")
def list_tasks(store: Store, project_id: Id | None = None, view: TaskListView | None = None) -> list:
    if view is not None and project_id is None:
        raise HTTPException(422, "按视图查询任务需要明确项目 ID")
    return store.list_tasks(project_id, view=view)


@router.post("/tasks", status_code=201)
def create_task(body: dict, store: Store) -> dict:
    return store.create_task(body)


@router.post("/tasks/preview")
def preview_task(body: dict, store: Store) -> dict:
    project_id = body.get("project_id")
    if not isinstance(project_id, str):
        raise HTTPException(422, "需要明确的项目 ID")
    with storage.connect(store.path) as conn:
        data = storage.snapshot(conn, "instances", project_id)
    return store.preview(body, data["data"]) | {"snapshot_at": data["success_at"], "snapshot_error": data["error"]}


@router.get("/tasks/{task_id}")
def get_task(task_id: Id, store: Store, project_id: Id | None = None) -> dict:
    return store.get_task(task_id, project_id)


@router.put("/tasks/{task_id}")
def update_task(task_id: Id, body: dict, store: Store) -> dict:
    return store.update_task(task_id, body)


@router.post("/tasks/{task_id}/trigger", status_code=202)
def trigger_task(task_id: Id, body: Trigger, store: Store) -> dict:
    return store.trigger_task(task_id, body.request_id)


@router.post("/tasks/{task_id}/trash")
def trash_task(task_id: Id, body: TaskTrashChange, store: Store) -> dict:
    return store.change_trash(body.project_id, task_id, body.expected_version, "trash")


@router.post("/tasks/{task_id}/trash/{action}")
def change_task_trash(task_id: Id, action: Literal["restore", "purge"], body: TaskTrashChange, store: Store) -> dict:
    return store.change_trash(body.project_id, task_id, body.expected_version, action)


@router.post("/tasks/{task_id}/{action}")
def control_task(task_id: Id, action: Literal["pause", "resume", "cancel"], store: Store) -> dict:
    return store.control_task(task_id, action)


@router.get("/runs")
def list_runs(
    store: Store, project_id: Id | None = None, task_id: Id | None = None, instance_id: Id | None = None,
    attention: bool = False, automation_rule_id: Id | None = None,
) -> list:
    if instance_id is not None and project_id is None:
        raise HTTPException(422, "按实例查询需要明确项目 ID")
    if automation_rule_id is not None and project_id is None:
        raise HTTPException(422, "按自动化规则查询需要明确项目 ID")
    if attention and project_id is None:
        raise HTTPException(422, "待处理事项需要明确项目 ID")
    return store.list_runs(project_id, task_id, instance_id, attention=attention, automation_rule_id=automation_rule_id)


@router.get("/runs/{run_id}")
def get_run(run_id: Id, store: Store) -> dict:
    return store.get_run(run_id)


@router.patch("/runs/{run_id}/warning")
def update_run_warning(run_id: Id, body: RunWarning, store: Store) -> dict[str, str | bool]:
    store.set_run_warning_dismissed(run_id, body.dismissed)
    return {"id": run_id, "warning_dismissed": body.dismissed}


@router.post("/runs/{run_id}/review-shutdown", status_code=202)
def review_shutdown(run_id: Id, store: Store, body: ShutdownReviewRequest | None = None) -> dict:
    return store.queue_shutdown_review(run_id, body.keep_stop_at if body else None)


@router.post("/runs/{run_id}/cancel")
def cancel_action(run_id: Id, store: Store) -> dict:
    return store.cancel_action(run_id)


@router.post("/runs/{run_id}/recheck", status_code=202)
def recheck_instance_operation(run_id: Id, store: Store) -> dict:
    return store.queue_instance_recheck(run_id)


@router.post("/runs/{run_id}/reconcile")
def reconcile_run(run_id: Id, body: Reconciliation, store: Store) -> dict:
    return store.reconcile_run(
        run_id, body.action, body.instance_id, body.note, confirmed_no_order=body.confirmed_no_order,
    )


@router.post("/actions", status_code=202)
def submit_action(body: InstanceAction, store: Store, request: Request) -> dict:
    try:
        return store.submit_action(
            {
                "project_id": body.project_id,
                "kind": body.kind,
                "config": {
                    "instance_id": body.instance_id,
                    "boot_mode": body.boot_mode,
                    "without_gpu_spec": body.without_gpu_spec,
                    "shutdown_at": body.stop_at,
                    "expected_stop_at": body.expected_stop_at,
                    "replace": body.conflict == "replace",
                    "name": body.name,
                    "expected_name": body.expected_name,
                },
            },
            body.request_id,
            read_only=request.app.state.config.read_only,
            use_snapshot_scope=True,
        )
    except InstanceActionUnavailable as error:
        raise HTTPException(409, str(error)) from error


class ResizeQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ResizeSubmit(Trigger):
    quote_id: Id


@router.get("/instances/{instance_id}/resize-options")
def resize_options(instance_id: Id, project_id: Id, conn: Db) -> dict:
    return instance_resize.options(conn, project_id, instance_id, storage.now())


@router.post("/instances/{instance_id}/resize-quotes", status_code=202)
def enqueue_resize_quote(instance_id: Id, project_id: Id, body: ResizeQuoteRequest, conn: Db) -> dict:
    with storage.transaction(conn):
        return instance_resize.create_quote(conn, project_id, instance_id, body.spec_id, storage.now())


@router.get("/instances/{instance_id}/resize-quotes/{quote_id}")
def read_resize_quote(instance_id: Id, project_id: Id, quote_id: Id, conn: Db) -> dict:
    return instance_resize.quote_view(instance_resize.get(conn, project_id, instance_id, quote_id), storage.now())


@router.post("/instances/{instance_id}/resize", status_code=202)
def resize_instance(instance_id: Id, project_id: Id, body: ResizeSubmit, conn: Db, request: Request) -> dict:
    with storage.transaction(conn):
        return instance_resize.submit(conn, project_id, instance_id, body.quote_id, body.request_id,
                                      storage.now(), read_only=request.app.state.config.read_only)
