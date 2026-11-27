"""MCP projections of cached GPU data and background receipts."""

import json
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from gpu_console import storage
from gpu_console.adapters.compshare import METRICS
from gpu_console.application.watches import catalog_view
from gpu_console.application import availability, rental_plan
from gpu_console.application.tasks import TaskStore, shutdown_cleanup_pending
from gpu_console.config import Config
from gpu_console.domain.billing import Basis
from gpu_console.mcp.common import mutate, page
from gpu_console.mcp.server import ConsoleMCPServer
from gpu_console.storage import cardless, catalog, dashboard_cost, dashboard_usage, refresh

Id = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
Limit = Annotated[int, Field(ge=1, le=100)]
Offset = Annotated[int, Field(ge=0, le=2**31 - 1)]
Timestamp = Annotated[int, Field(gt=0, le=2**31 - 1)]
AvailabilityKey = Annotated[str, Field(min_length=1, max_length=512)]
GpuModel = Annotated[str, Field(min_length=1, max_length=100)]
DirectoryKey = tuple[Id, Id, GpuModel, Literal["uhost", "spot"]]
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
QUEUE_READ = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
UPDATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

INSTANCE_FIELDS = (
    "id", "name", "project_id", "region", "zone", "state", "machine_type", "instance_type", "gpu_mode",
    "gpu", "gpu_model", "graphics_memory_gb", "cpu", "memory_gb", "cpu_platform", "os_name", "image_id",
    "image_name", "charge_type", "instance_price_per_hour", "disk_price_per_hour", "image_price_per_hour",
    "create_time", "start_time", "stop_time", "updated_at", "expire_time", "release_time", "scheduler_stop_time",
    "scheduler_status",
    "support_without_gpu_start", "cardless_profiles", "without_gpu_spec", "cardless_hourly_cny", "cardless_rate_source",
)
RUN_FIELDS = (
    "id", "name", "kind", "task_id", "source_task_id", "project_id", "state", "phase", "reason", "stopped", "scheduled_at",
    "deadline_at", "next_check", "updated_at", "created_at", "start_attempts",
)
RESULT_FIELDS = (
    "instance_id", "region", "zone", "gpu_model", "disposition", "cloud_code", "cloud_request_id", "capacity_wait_since",
    "capacity_attempts", "shutdown_at", "cloud_shutdown_at", "shutdown_confirmation", "shutdown_outcome",
    "actual_stop_at", "shutdown_cause", "stop_run_id", "shutdown_cleanup_pending", "shutdown_cutoff_reached",
    "purchase_binding_conflict",
    "release_confirmation", "released_at", "automation_reminded", "instance_recheck_pending",
    "boot_mode", "without_gpu_spec", "observed_boot_mode", "observed_without_gpu_spec",
)


def _select(item: dict, fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: item[field] for field in fields if field in item}


def _metadata(snapshot: dict) -> dict[str, Any]:
    return _select(snapshot, ("success_at", "error"))


def _instance(item: dict) -> dict[str, Any]:
    data = _select(item, INSTANCE_FIELDS)
    if isinstance(item.get("disks"), list):
        data["disks"] = [_select(disk, ("purpose", "type", "size_gb")) for disk in item["disks"] if isinstance(disk, dict)]
    if isinstance(item.get("utilization"), dict):
        usage = item["utilization"]
        data["utilization"] = _select(usage, ("cpu", "memory")) | {
            "gpus": [_select(card, ("utilization", "memory")) for card in usage.get("gpus", []) if isinstance(card, dict)],
        }
    return data


def _target(conn, project_id: str, instance_id: str) -> tuple[dict, dict]:
    snapshot = storage.snapshot(conn, "instances", project_id)
    for item in snapshot["data"]:
        if item.get("id") == instance_id and item.get("project_id", project_id) == project_id:
            return item, snapshot
    if snapshot["success_at"] is None or snapshot["error"]:
        raise ValueError("实例清单待同步，请先刷新该项目实例")
    raise ValueError("该项目下找不到指定实例")


def _project(conn, project_id: str) -> None:
    if not catalog.project_known(conn, project_id):
        raise ValueError("项目目录中没有该项目，请检查项目 ID 或刷新项目列表")


def _image_scope(conn, project_id: str, region: str, zone: str) -> None:
    _project(conn, project_id)
    if not catalog.location_known(conn, region, zone):
        raise ValueError("可用区目录中没有该地域和可用区，请先刷新库存目录")


def public_run(run: dict) -> dict[str, Any]:
    data = _select(run, RUN_FIELDS)
    snapshot = run.get("snapshot", {})
    data["target"] = _select(snapshot.get("config", {}), (
        "instance_id", "region", "zone", "boot_mode", "without_gpu_spec", "shutdown_after_seconds",
    ))
    if snapshot.get("trigger"):
        data["trigger"] = _select(snapshot["trigger"], ("automation_rule_id", "kind", "sampled_at", "due_at"))
    data["result"] = _select(run.get("result", {}), RESULT_FIELDS)
    if "operations" in run:
        data["recent_operations"] = [
            _select(operation, ("action", "state", "request_id", "cloud_code", "created_at", "updated_at"))
            for operation in run["operations"][-20:]
        ]
    return data


def register(server: ConsoleMCPServer, config: Config) -> None:
    store = TaskStore(config.db)

    @server.scoped_tool("read", name="gpu_list_projects", annotations=READ)
    def list_projects(limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """List CompShare projects using the most recent successful project snapshot."""
        with storage.connect(config.db) as conn:
            snapshot = storage.snapshot(conn, "projects")
        return page([_select(item, ("id", "name")) for item in snapshot["data"]], limit, offset) | _metadata(snapshot)

    @server.scoped_tool("read", name="gpu_list_instances", annotations=READ)
    def list_instances(
        project_id: Id, state: str | None = None, region: Id | None = None,
        zone: Id | None = None, limit: Limit = 50, offset: Offset = 0,
    ) -> dict[str, Any]:
        """List GPU instances in a project and report snapshot age and collection errors."""
        with storage.connect(config.db) as conn:
            _project(conn, project_id)
            snapshot = storage.snapshot(conn, "instances", project_id)
            profiles = cardless.profiles(conn)
        filters = {"state": state, "region": region, "zone": zone}
        items = [_instance(item | {"cardless_profiles": cardless.options_for(item, profiles)})
                 for item in snapshot["data"] if item.get("project_id", project_id) == project_id
                 and all(value is None or item.get(field) == value for field, value in filters.items())]
        return page(items, limit, offset) | _metadata(snapshot)

    @server.scoped_tool("read", name="gpu_get_instance", annotations=READ)
    def get_instance(project_id: Id, instance_id: Id) -> dict[str, Any]:
        """Get a project's instance state, specification, costs, and scheduler details."""
        with storage.connect(config.db) as conn:
            item, snapshot = _target(conn, project_id, instance_id)
            item = item | {"cardless_profiles": cardless.options_for(item, cardless.profiles(conn))}
            sampled = conn.execute(
                "SELECT MAX(sampled_at) FROM metrics WHERE project_id=? AND instance_id=?", (project_id, instance_id),
            ).fetchone()[0]
            cleanup = shutdown_cleanup_pending(conn, project_id, instance_id)
        result = {"instance": _instance(item), "last_sample_at": sampled} | _metadata(snapshot)
        if cleanup:
            result["shutdown_cleanup_pending"] = True
        return result

    @server.scoped_tool("read", name="gpu_get_metrics", annotations=READ)
    def get_metrics(
        project_id: Id, instance_id: Id, metric: str | None = None,
        since: Timestamp | None = None, until: Timestamp | None = None, limit: Limit = 50, offset: Offset = 0,
    ) -> dict[str, Any]:
        """Get paged monitoring samples. Times are Unix seconds; without dates the latest hour is used."""
        if metric is not None and metric not in METRICS:
            raise ValueError("未知监控指标，可用值为 " + "、".join(sorted(METRICS)))
        until = until if until is not None else storage.now()
        since = since if since is not None else max(1, until - 3600)
        if until < since:
            raise ValueError("监控结束时间不能早于开始时间")
        with storage.connect(config.db) as conn:
            _target(conn, project_id, instance_id)
            samples = """FROM metrics WHERE project_id=? AND instance_id=? AND sampled_at BETWEEN ? AND ?
                         AND (? IS NULL OR metric=?)"""
            parameters = (project_id, instance_id, since, until, metric, metric)
            rows = conn.execute(
                "SELECT metric,label,sampled_at,value " + samples
                + " ORDER BY sampled_at DESC,metric,label LIMIT ? OFFSET ?", (*parameters, limit, offset),
            )
            items = [dict(row) for row in rows]
            total = conn.execute("SELECT COUNT(*) " + samples, parameters).fetchone()[0]
            snapshot = storage.snapshot(conn, "metrics", project_id)
        next_offset = offset + len(items)
        return {"items": items, "count": len(items), "total": total, "since": since, "until": until,
                "has_more": next_offset < total, "next_offset": next_offset if next_offset < total else None} | _metadata(snapshot)

    @server.scoped_tool("read", name="gpu_list_locations", annotations=READ)
    def list_locations(region: Id | None = None, limit: Limit = 50, offset: Offset = 0) -> dict[str, Any]:
        """List catalog region and zone identifiers, optionally filtered by region."""
        with storage.connect(config.db) as conn:
            snapshot = catalog.locations(conn)
        items = [item for item in snapshot["data"] if region is None or item.get("region") == region]
        return page(items, limit, offset) | _metadata(snapshot)

    @server.scoped_tool("read", name="gpu_get_usage_summary", annotations=READ)
    def usage_summary(project_id: Id, range: Literal["today", "7d", "30d"] = "7d") -> dict[str, Any]:
        """Summarize measured GPU-hours, utilization, coverage and automation receipts in Beijing time."""
        with storage.connect(config.db) as conn:
            _project(conn, project_id)
            return dashboard_usage.summary(conn, project_id, range, storage.now())

    @server.scoped_tool("read", name="gpu_get_cost_summary", annotations=READ)
    def cost_summary(
        project_id: Id, range: dashboard_usage.UsageRange = "7d", instance_id: Id | None = None,
        include_details: bool = True, basis: Basis = "order_date",
    ) -> dict[str, Any]:
        """Summarize paid CompShare bills, today's unbilled estimates, and observed GPU hours in Beijing time.

        Set include_details=false for totals only. Estimates are separate from paid amounts.
        """
        with storage.connect(config.db) as conn:
            _project(conn, project_id)
            return dashboard_cost.summary(conn, project_id, range, storage.now(), instance_id,
                                          include_details=include_details, basis=basis)

    @server.scoped_tool("read", name="gpu_plan_gpu_rental", annotations=READ)
    def plan_gpu_rental(project_id: Id, plan: rental_plan.RentalRequest) -> dict[str, Any]:
        """Compare when to seek GPU capacity before a training deadline. Times are Unix seconds in the next 24 hours.

        Each candidate selects a project watch or a region, zone and pool. Optional compute rates are CNY per
        instance-hour. Inventory replay is historical evidence. This tool never creates or starts an instance.
        """
        with storage.connect(config.db) as conn:
            _project(conn, project_id)
            return rental_plan.plan(conn, project_id, plan, storage.now())

    @server.scoped_tool("read", name="gpu_get_availability_trends", annotations=READ)
    def availability_trends(
        source: Literal["catalog", "watch", "pool"] = "pool", project_id: Id | None = None,
        key: AvailabilityKey | DirectoryKey | None = None,
        gpu_model: GpuModel | None = None,
        region: Id | None = None, zone: Id | None = None, instance_type: Literal["uhost", "spot"] | None = None,
        limit: Limit = 20, offset: Offset = 0, include_hourly: bool = False,
        card_threshold: availability.CardThreshold = 1,
    ) -> dict[str, Any]:
        """Compare seven days of actual platform scheduling checks and GPU pool counts.

        Pool trends list only pools with catalog configurations for the exact card_threshold. One
        schedulable configuration for that count is sufficient. Watches match the saved GPU, CPU and memory combination.
        Pass project_id for project-specific evidence; watches require it. Use an item key for detail
        and include_hourly for dated hourly buckets.
        The legacy catalog source reports only the directory listing state.
        """
        if isinstance(key, tuple):
            if source != "catalog":
                raise ValueError("请使用当前库存来源返回的键")
            key = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
        with storage.connect(config.db) as conn:
            result = availability.query(
                conn, source=source, now=storage.now(), project_id=project_id, key=key,
                gpu_model=gpu_model, region=region, zone=zone, instance_type=instance_type, limit=limit, offset=offset,
                card_threshold=card_threshold,
            )
        if result["detail"] is not None and not include_hourly:
            result["detail"].pop("hours", None)
        next_offset = offset + len(result["items"])
        return result | {"next_offset": next_offset if next_offset < result["total"] else None}

    @server.scoped_tool("read", name="gpu_list_inventory", annotations=READ)
    def list_inventory(
        region: Id | None = None, zone: Id | None = None, gpu_model: str | None = None,
        limit: Limit = 20, offset: Offset = 0, source: Literal["catalog", "pool"] = "catalog",
        project_id: Id | None = None,
    ) -> dict[str, Any]:
        """Use source=pool for official remaining card counts and update times for pools listed in the catalog.

        The catalog source includes the latest scheduling check for each configuration in the selected project.
        """
        with storage.connect(config.db) as conn, storage.transaction(conn, immediate=False):
            now = storage.now()
            snapshot = availability.pool_snapshot(conn, now) if source == "pool" else catalog_view(conn, now, project_id)
        items = []
        for item in snapshot["data"]:
            if (region and item.get("region") != region) or (zone and item.get("zone") != zone):
                continue
            if gpu_model and gpu_model.casefold() not in str(item.get("gpu_model") or "").casefold():
                continue
            items.append(item if source == "pool" else _select(
                item, ("region", "zone", "zone_name", "gpu_model", "instance_type", "status", "specs",
                       "disks", "graphics_memory_gb"),
            ))
        return page(items, limit, offset) | _metadata(snapshot)

    @server.scoped_tool("read", name="gpu_list_images", annotations=READ)
    def list_images(
        project_id: Id, region: Id, zone: Id,
        kind: Literal["system", "app", "custom", "shared"] | None = None, limit: Limit = 50, offset: Offset = 0,
    ) -> dict[str, Any]:
        """List images in a project and location, with independent freshness per catalog category."""
        with storage.connect(config.db) as conn:
            _image_scope(conn, project_id, region, zone)
            snapshots = catalog.image_snapshots(conn, project_id, region, zone)
        kinds = (kind,) if kind else catalog.IMAGE_KINDS
        fields = ("id", "name", "kind", "status", "size_gb", "gpu_source_type", "paid", "runtime", "framework",
                  "framework_version", "os_version", "python_version", "cuda_version", "software")
        items = [_select(item, fields) for image_kind in kinds for item in snapshots[image_kind]["data"]]
        return page(items, limit, offset) | {"sources": {image_kind: _metadata(snapshots[image_kind]) for image_kind in kinds},
                                             "refresh_id": snapshots["refresh_id"]}

    @server.scoped_tool("manage", name="gpu_refresh_data", annotations=QUEUE_READ)
    def refresh_data(
        kind: Literal["projects", "catalog", "instances", "stock", "images", "pool"], request_id: Id,
        project_id: Id | None = None, region: Id | None = None, zone: Id | None = None, watch_id: Id | None = None,
    ) -> dict[str, Any]:
        """Queue a data collection request and return its refresh ID for follow-up."""
        from gpu_console.api.resources import Refresh, refresh as queue_refresh

        def execute(conn) -> dict[str, Any]:
            if kind == "images":
                if project_id is None or region is None or zone is None or watch_id is not None:
                    raise ValueError("刷新镜像须提供项目、地域和可用区")
                _image_scope(conn, project_id, region, zone)
                receipt = {"id": catalog.queue_images(conn, project_id, region, zone), "queued": True}
            else:
                if region is not None or zone is not None or (kind in {"projects", "catalog", "pool"} and project_id is not None):
                    raise ValueError("刷新类型和范围参数不匹配")
                if project_id is not None:
                    _project(conn, project_id)
                receipt = queue_refresh(Refresh(kind=kind, project_id=project_id, watch_id=watch_id), conn)
            return receipt | {"project_id": project_id}

        return mutate(config, "refresh_data", request_id,
                      {"kind": kind, "project_id": project_id, "region": region, "zone": zone, "watch_id": watch_id},
                      execute, allow_read_only=True)

    @server.scoped_tool("read", name="gpu_get_refresh", annotations=READ)
    def get_refresh(refresh_id: Id, project_id: Id | None = None) -> dict[str, Any]:
        """Get the result of a previously queued data refresh in its original scope."""
        with storage.connect(config.db) as conn:
            receipt = refresh.get(conn, refresh_id)
        if receipt is None or receipt["project_id"] != project_id:
            raise ValueError("此范围内的同步回执不存在或已过期")
        return receipt

    @server.scoped_tool("read", name="gpu_list_runs", annotations=READ)
    def list_runs(
        project_id: Id, task_id: Id | None = None, instance_id: Id | None = None,
        automation_rule_id: Id | None = None, limit: Limit = 50, offset: Offset = 0,
    ) -> dict[str, Any]:
        """List historical or active operation receipts in one project using a task, rule, or instance filter."""
        runs = store.list_runs(project_id, task_id, instance_id, automation_rule_id=automation_rule_id,
                              limit=limit + 1, offset=offset)
        items = [public_run(run) for run in runs[:limit]]
        has_more = len(runs) > limit
        return {"items": items, "count": len(items), "has_more": has_more,
                "next_offset": offset + len(items) if has_more else None}

    @server.scoped_tool("read", name="gpu_get_run", annotations=READ)
    def get_run(project_id: Id, run_id: Id) -> dict[str, Any]:
        """Read current status, next retry, and recent cloud receipts for one operation."""
        run = store.get_run(run_id)
        if run["project_id"] != project_id:
            raise ValueError("此项目下没有该运行记录")
        return public_run(run)

    def control_run(project_id: str, run_id: str, request_id: str, *, recheck: bool = False) -> dict[str, Any]:
        def execute(conn) -> dict[str, Any]:
            if not conn.execute("SELECT 1 FROM task_runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone():
                raise ValueError("此项目下没有该运行记录")
            run = store.queue_instance_recheck(run_id) if recheck else store.cancel_action(run_id)
            return public_run(run)

        return mutate(config, "recheck_run" if recheck else "cancel_run", request_id,
                      {"project_id": project_id, "run_id": run_id}, execute, allow_read_only=recheck)

    @server.scoped_tool("manage", name="gpu_cancel_run", annotations=UPDATE)
    def cancel_run(project_id: Id, run_id: Id, request_id: Id) -> dict[str, Any]:
        """Stop future attempts for an eligible instance operation and preserve cloud readback."""
        return control_run(project_id, run_id, request_id)

    @server.scoped_tool("manage", name="gpu_recheck_run", annotations=QUEUE_READ)
    def recheck_run(project_id: Id, run_id: Id, request_id: Id) -> dict[str, Any]:
        """Queue another readback for a pending instance review without sending a new cloud operation."""
        return control_run(project_id, run_id, request_id, recheck=True)
