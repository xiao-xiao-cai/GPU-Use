"""Remote shutdown timers, their ownership, and linked acquisition schedules."""

import json
from typing import TYPE_CHECKING

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.application.task_config import SHUTDOWN_MINIMUM
from gpu_console.application.tasks import (
    encode, enqueue_owner_cancel, insert_run, shutdown_review_target, task_snapshot, task_view, write_audit,
)

if TYPE_CHECKING:
    from gpu_console.worker.tasks import TaskRunner


SHUTDOWN_OBSERVATION_GRACE = 5 * 60


def linked_target(run: dict, instance: dict) -> int | None:
    config = run["snapshot"]["config"]
    started = instance.get("start_time")
    if duration := config.get("shutdown_after_seconds"):
        return started + duration if type(started) is int and started > 0 else None
    return config.get("shutdown_at")


def linked_shutdown(runner: "TaskRunner", conn, run: dict, instance: dict, result: dict) -> None:
    """Bind acquisition and its follow-up in the same storage transaction."""
    config = run["snapshot"]["config"]
    target = linked_target(run, instance)
    if target is None:
        return
    now = runner.store.now()
    problem = None
    if runner.read_only or storage.get_settings(conn)["restore_gate"]:
        problem = "实例已运行，后台写操作已暂停，关机预约待处理"
    elif instance.get("charge_type") == "Spot":
        problem = "实例已运行，抢占式实例无法登记定时关机"
    elif target - now < SHUTDOWN_MINIMUM:
        problem = "实例已运行，距离目标关机时间不足 5 分钟"
    if problem:
        result.update(shutdown_confirmation="unconfirmed", shutdown_at=target)
        runner._emit(conn, run, problem, result, "shutdown-pending", policy_event="shutdown_attention")
        return
    parent = run["snapshot"]
    snapshot = {
        "name": parent["name"] + " · 关机预约", "kind": "shutdown", "project_id": run["project_id"],
        "window_seconds": parent["window_seconds"], "channels": parent["channels"],
        "config": {
            "instance_id": instance["id"], "region": instance["region"], "zone": instance["zone"],
            "shutdown_at": target, "replace": False, "parent_run_id": run["id"],
            "instance_started_at": instance.get("start_time"),
            **({"require_same_boot": True} if config.get("shutdown_after_seconds") else {}),
            **({"email": config["email"]} if config.get("email") else {}),
        },
    }
    if "notification_events" in parent:
        snapshot["notification_events"] = parent["notification_events"]
    linked = insert_run(
        conn, snapshot, target, now, request_id="linked:" + run["id"] + ":" + instance["id"],
        source_task_id=run["source_task_id"] or run["task_id"],
    )
    result.update(shutdown_run_id=linked["id"], shutdown_confirmation="queued", shutdown_at=target)


def set_shutdown(runner: "TaskRunner", run: dict) -> None:
    config = run["snapshot"]["config"]
    target = config.get("shutdown_at", run["scheduled_at"])
    try:
        instance = runner._target(run)
    except CloudError as error:
        if not config.get("parent_run_id"):
            raise
        result = {
            "instance_id": config["instance_id"], "region": config["region"], "zone": config["zone"],
            "shutdown_at": target, "cloud_code": error.code, "cloud_request_id": error.request_id,
        }
        if error.retryable:
            _wait_for_readback(runner, run, f"云端关机预约未确认，{error}", result)
        else:
            _finish(runner, run, "failed", f"实例已运行，关机预约未确认，{error}", result)
        return
    if instance is None:
        _finish(runner, run, "failed", "指定实例未在项目和可用区中找到", {})
        return
    result = runner._instance_result(instance, "shutdown_scheduled") | {"shutdown_at": target}
    started_at = config.get("instance_started_at") or run["result"].get("shutdown_started_at")
    if started_at and instance.get("start_time") and instance["start_time"] != started_at:
        _finish(runner, run, "canceled", "原运行已结束，跳过旧关机预约", result | {"shutdown_outcome": "superseded"})
        return
    if instance["state"] == "Stopped" and (started_at or config.get("parent_run_id")):
        _finish(runner, run, "succeeded", "实例已提前关机，已结清预约", result | {
            "shutdown_outcome": "early", "actual_stop_at": _shutdown_time(instance.get("stop_time")) or runner.store.now(),
        })
        return
    if config.get("require_same_boot") and instance.get("start_time") != started_at:
        _wait_for_readback(runner, run, "实例本次启动时间待核对，暂停登记", result)
        return
    if instance.get("charge_type") == "Spot":
        _finish(runner, run, "failed", "抢占式实例不支持定时关机", result)
        return
    if instance.get("charge_type") not in {"Postpay", "Day", "Month"}:
        _wait_for_readback(runner, run, "实例计费方式待核对，暂停登记", result)
        return
    if instance.get("scheduler_status") != "known":
        _wait_for_readback(runner, run, "云端预约时间待核对，暂停登记", result)
        return
    if instance["state"] != "Running":
        runner._transition(run, "waiting", "等待实例进入运行状态再登记关机", result)
        return
    result["shutdown_started_at"] = instance.get("start_time")

    current = instance.get("scheduler_stop_time")
    result["previous_shutdown_at"] = current
    expected = config.get("expected_stop_at") if config.get("replace") else None
    owner = _owner(runner, run, instance["id"])
    if (
        owner and run["task_id"] and owner["task_id"] == run["task_id"]
        and owner["stop_at"] != target and expected is None
    ):
        runner._transition(run, "waiting", "等待前一次云端关机预约完成核对", result | {"shutdown_at": target})
    elif current == target:
        _finish(runner, run, "succeeded", "已回读到相同时间的云端预约，保留现有安排",
                result | {"cloud_shutdown_at": target, "shutdown_registration": "external"})
    elif expected is not None and current != expected:
        _finish(runner, run, "manual_review", "云端预约在确认后已变化，请重新核对", result)
    elif current is not None and expected is None and (
        config.get("require_same_boot") or not _previous_managed_time(runner, run, instance["id"], current)
    ):
        _finish(runner, run, "manual_review", "云端已有不同关机时间，请确认保留或替换", result)
    else:
        runner._perform(
            run, "set_shutdown", result, lambda: runner.cloud.set_stop(instance, target), "verify_set_shutdown"
        )


def cancel_shutdown(runner: "TaskRunner", run: dict) -> None:
    config = run["snapshot"]["config"]
    instance = runner._target(run)
    if instance is None:
        runner._transition(run, "manual_review", "撤销待确认，无法回读实例", {"instance_id": config["instance_id"]})
        return
    result = runner._instance_result(instance, "shutdown_canceled")
    owner = _owner(runner, run, instance["id"])
    owner_run_id = config.get("owner_run_id")
    current = instance.get("scheduler_stop_time")
    expected = config.get("expected_stop_at")
    if expected is None and owner:
        expected = owner["stop_at"]
    matched_owner = owner if owner and owner["stop_at"] == expected else None
    result.update(shutdown_at=expected, cloud_shutdown_at=current,
                  owner_run_id=owner_run_id or (matched_owner or {}).get("run_id"))
    if instance.get("scheduler_status") != "known":
        runner._transition(run, "waiting", "撤销待确认，云端预约时间待核对", result)
    elif owner_run_id and run["request_id"] == "cancel-owner:" + owner_run_id and (
        not matched_owner or matched_owner["run_id"] != owner_run_id
    ):
        _finish_cancel(runner, run, result, outcome="superseded")
    elif owner_run_id and (not matched_owner or matched_owner["run_id"] != owner_run_id):
        runner._transition(run, "manual_review", "撤销待确认，原预约归属已经变化", result)
    elif current is None:
        _finish_cancel(runner, run, result)
    elif current != expected:
        if matched_owner and _early_owner(runner, matched_owner["run_id"]):
            _finish_cancel(runner, run, result, outcome="superseded")
        else:
            runner._transition(run, "manual_review", "撤销待确认，云端时间与原预约不一致", result)
    else:
        runner._perform(
            run, "cancel_shutdown", result, lambda: runner.cloud.cancel_stop(instance), "verify_cancel_shutdown"
        )


def verify_shutdown(runner: "TaskRunner", run: dict) -> None:
    op = runner._last_operation(run)
    result = op["data"]
    read_error = ""
    try:
        instance = runner._target(run, result)
    except CloudError as error:
        instance = None
        result |= {"cloud_code": error.code, "cloud_request_id": error.request_id}
        read_error = f"，{error}"
    known = instance is not None and instance.get("scheduler_status") == "known"
    observed = instance["scheduler_stop_time"] if known else None
    if run["phase"] == "verify_set_shutdown":
        target = result["shutdown_at"]
        if known and observed == target:
            _finish_set(runner, run, op, instance, result)
        elif runner.store.now() > run["deadline_at"]:
            _finish(runner, run, "manual_review", "未回读确认云端关机预约" + read_error,
                    result | {"cloud_shutdown_at": observed}, phase=run["phase"])
        else:
            _wait_for_readback(runner, run, "云端关机预约尚未回读确认" + read_error,
                               result | {"cloud_shutdown_at": observed}, phase=run["phase"])
    elif known and observed is None:
        _finish_cancel(runner, run, result | {"cloud_shutdown_at": None})
    elif known and observed != result["shutdown_at"] and result.get("owner_run_id") and _early_owner(
        runner, result["owner_run_id"]
    ):
        _finish_cancel(runner, run, result | {"cloud_shutdown_at": observed}, outcome="superseded")
    elif (known and observed != result["shutdown_at"]) or runner.store.now() > op["created_at"] + 180:
        runner._transition(
            run, "manual_review", "撤销待确认，请核对原预约及当前云端时间" + read_error,
            result | {"cloud_shutdown_at": observed}, phase=run["phase"],
        )
    else:
        runner._transition(
            run, "reconciling", "撤销待确认，仍在回读云端预约" + read_error,
            result | {"cloud_shutdown_at": observed}, phase=run["phase"],
        )


def _shutdown_time(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _shutdown_review_request(conn, run_id: str):
    return conn.execute(
        """SELECT id,data FROM task_audit WHERE run_id=? AND action='review_shutdown_requested'
           ORDER BY id DESC LIMIT 1""",
        (run_id,),
    ).fetchone()


def review_shutdown(runner: "TaskRunner", run: dict) -> None:
    """Read the original instance without changing the cloud or replaying the run."""
    with storage.connect(runner.store.path) as conn:
        queued = _shutdown_review_request(conn, run["id"])
    if queued is None:
        return
    request = json.loads(queued["data"])
    target = shutdown_review_target(run)
    instance = None
    if target is None:
        detail = "原运行缺少完整的实例范围或预约时间，无法核对"
    elif runner.cloud is None:
        detail = "尚未配置平台凭据，无法核对云端关机预约"
    else:
        try:
            instance = runner._target(run, target[0])
        except CloudError as error:
            detail = f"核对云端关机预约失败，{error}"
        else:
            detail = "在原项目和可用区中找不到实例，无法核对云端关机预约"

    original = target[1] if target else None
    now = runner.store.now()
    outcome = "unknown"
    observed, stop_time, instance_state = None, None, None
    known = False
    if instance is not None:
        instance_state = instance.get("state")
        stop_time = _shutdown_time(instance.get("stop_time"))
        known = instance.get("scheduler_status") == "known" and "scheduler_stop_time" in instance
        if known and instance["scheduler_stop_time"] is not None:
            observed = _shutdown_time(instance["scheduler_stop_time"])
            known = observed is not None
        if instance_state == "Stopped":
            outcome, detail = "stopped", "已回读确认实例目前已关机"
        elif not known:
            detail = "云端预约状态未知，请稍后重新核对"
        elif observed is None:
            outcome, detail = "absent", "云端当前无预约，实例仍未确认关机"
        elif observed <= now:
            detail = "回读的云端预约时间已过，尚未取得关机证据"
        elif observed == original:
            outcome, detail = "matching", "已回读确认云端登记了目标预约"
        elif observed == request.get("keep_stop_at"):
            outcome, detail = "kept", "已确认保留当前云端预约"
        else:
            outcome, detail = "different", "云端有不同的有效预约，请确认保留或到实例详情调整"
    evidence = {
        "checked_at": now, "outcome": outcome, "detail": detail,
        "original_shutdown_at": original, "cloud_shutdown_at": observed,
        "instance_state": instance_state, "stop_time": stop_time, "schedule_known": known,
        "resolved": outcome in {"stopped", "matching", "kept"},
    }
    with storage.connect(runner.store.path) as conn, storage.transaction(conn):
        current = runner._current(conn, run)
        pending = _shutdown_review_request(conn, run["id"])
        if current is None or pending is None or pending["id"] != queued["id"]:
            return
        if outcome == "matching" and target is not None:
            _track_external_shutdown(conn, run["id"], target[0], target[1])
        write_audit(conn, run["id"], "review_shutdown_result", evidence, now)
        runner._write_transition(
            conn, current, current["state"], current["reason"], {"shutdown_review": evidence},
            notify=False, phase=request.get("original_phase"),
        )
        previous = current["result"]
        if (
            original is not None and outcome == "stopped" and previous.get("shutdown_outcome") == "missed"
            and previous.get("shutdown_observation", {}).get("managed") is True
            and original >= storage.get_settings(conn).get("feishu_policy_since", 0)
        ):
            runner._emit(conn, current, detail, previous, "shutdown-confirmed", policy_event="instance_stopped", legacy=False)


def review_cancel_shutdown(runner: "TaskRunner", run: dict) -> None:
    """Check the current schedule without replaying the original cancellation."""
    config = run["snapshot"]["config"]
    original = _shutdown_time(config.get("expected_stop_at")) or _shutdown_time(run["result"].get("shutdown_at"))
    observed = None
    outcome = "unknown"
    if runner.cloud is None:
        reason = "尚未配置平台凭据，无法核对云端关机预约"
    elif not all(isinstance(config.get(key), str) and config[key] for key in ("instance_id", "region", "zone")):
        reason = "原运行缺少实例、地域或可用区信息，无法核对云端关机预约"
    else:
        try:
            instance = runner._target(run)
        except CloudError as error:
            reason = f"核对云端关机预约失败，{error}"
        else:
            if instance is None:
                reason = "在原项目和可用区中找不到实例，无法核对云端关机预约"
            elif instance.get("scheduler_status") != "known" or "scheduler_stop_time" not in instance:
                reason = "云端关机预约状态未知，请稍后重新核对"
            elif instance["scheduler_stop_time"] is None:
                outcome, reason = "absent", "已核对，云端当前没有关机预约"
            else:
                observed = _shutdown_time(instance["scheduler_stop_time"])
                if observed is None:
                    reason = "云端关机预约时间无效，请稍后重新核对"
                elif original is None:
                    reason = "缺少原预约时间，请到实例详情核对当前预约"
                elif observed == original:
                    outcome, reason = "still_present", "原关机预约仍存在，请在实例详情处理后重新核对"
                else:
                    outcome, reason = "changed", "已核对，原关机预约已不存在，云端当前有其他关机预约"

    state = "reviewed" if outcome in {"absent", "changed"} else "manual_review"
    evidence = {
        "checked_at": runner.store.now(), "outcome": outcome,
        "original_shutdown_at": original, "cloud_shutdown_at": observed,
    }
    with storage.connect(runner.store.path) as conn, storage.transaction(conn):
        current = runner._current(conn, run)
        if current is None:
            return
        owner_run_id = config.get("owner_run_id") or current["result"].get("owner_run_id")
        if state == "reviewed" and original is not None:
            retire_appointments(conn, run["project_id"], config, "canceled" if outcome == "absent" else "superseded",
                                evidence["checked_at"], stop_at=original, owner_run_id=owner_run_id)
        write_audit(conn, run["id"], "review_cancel_shutdown_result", evidence, evidence["checked_at"])
        runner._write_transition(conn, current, state, reason, {"shutdown_review": evidence}, notify=False)


def expire_shutdown(runner: "TaskRunner", run: dict) -> None:
    target = run["snapshot"]["config"].get("shutdown_at", run["scheduled_at"])
    missed = run["task_id"] is not None and runner.store.now() >= target
    _finish(
        runner, run, "missed" if missed else "expired",
        "已错过本地关机计划" if missed else "未在最晚登记时间前确认云端预约",
        {"shutdown_at": target},
    )


def report_set_error(runner: "TaskRunner", run_id: str, reason: str) -> None:
    run = runner.store.get_run(run_id)
    if not run["snapshot"]["config"].get("parent_run_id"):
        return
    with storage.connect(runner.store.path) as conn, storage.transaction(conn):
        current = runner._current(conn, run)
        if current is not None:
            _update_linked(conn, run, "unconfirmed")
            if current["state"] in {"waiting", "reconciling"}:
                runner._emit(conn, current, "实例已运行，关机预约未确认，" + reason, run["result"], "shutdown-pending")


def _previous_managed_time(runner: "TaskRunner", run: dict, instance_id: str, previous: int) -> bool:
    if not run["task_id"] or previous > runner.store.now():
        return False
    with storage.connect(runner.store.path) as conn:
        row = conn.execute(
            """SELECT 1 FROM task_runs WHERE task_id=? AND state='succeeded'
               AND json_extract(result,'$.cloud_shutdown_at')=?
               AND json_extract(result,'$.instance_id')=?
               AND json_extract(result,'$.shutdown_registration') IS NOT 'external'
               AND json_extract(result,'$.shutdown_outcome') IS NOT NULL""",
            (run["task_id"], previous, instance_id),
        ).fetchone()
    return row is not None


def _owner(runner: "TaskRunner", run: dict, instance_id: str) -> dict | None:
    with storage.connect(runner.store.path) as conn:
        row = conn.execute(
            "SELECT * FROM shutdown_owners WHERE project_id=? AND instance_id=?",
            (run["project_id"], instance_id),
        ).fetchone()
        return dict(row) if row else None


def _early_owner(runner: "TaskRunner", run_id: str) -> bool:
    with storage.connect(runner.store.path) as conn:
        row = conn.execute("SELECT json_extract(result,'$.shutdown_outcome') FROM task_runs WHERE id=?", (run_id,)).fetchone()
        return row is not None and row[0] == "early"


def _appointments(conn, project_id: str, scope: dict):
    return conn.execute(
        """WITH tracked AS (
               SELECT run_id,instance_id,region,zone,stop_at,1 AS managed FROM shutdown_owners WHERE project_id=?1
               UNION ALL
               SELECT o.run_id,o.instance_id,o.region,o.zone,o.stop_at,0
               FROM external_shutdown_observations o JOIN task_runs r ON r.id=o.run_id WHERE r.project_id=?1
           )
           SELECT t.*,r.snapshot,r.result,r.created_at FROM tracked t JOIN task_runs r ON r.id=t.run_id
           WHERE t.instance_id=?2 AND t.region=?3 AND t.zone=?4 ORDER BY t.managed DESC""",
        (project_id, scope["instance_id"], scope["region"], scope["zone"]),
    ).fetchall()


def _record_outcome(
    conn, row, outcome: str, now: int, *, evidence: dict | None = None, cleanup_pending: bool = False,
) -> dict:
    result = json.loads(row["result"])
    if result.get("shutdown_outcome") != "early":
        result.update({"shutdown_outcome": outcome} | (evidence or {}))
    result["shutdown_cleanup_pending"] = cleanup_pending
    conn.execute("UPDATE task_runs SET result=?,updated_at=? WHERE id=?", (encode(result), now, row["run_id"]))
    parent_id = json.loads(row["snapshot"])["config"].get("parent_run_id")
    if parent_id:
        actual_outcome = result["shutdown_outcome"]
        status = {"early": "fulfilled", "confirmed": "confirmed", "missed": "confirmed"}.get(actual_outcome, actual_outcome)
        conn.execute(
            "UPDATE task_runs SET result=json_patch(result,?) WHERE id=?",
            (encode({
                "shutdown_confirmation": status, "shutdown_outcome": actual_outcome,
                "shutdown_cleanup_pending": cleanup_pending,
                **{key: result[key] for key in ("actual_stop_at", "shutdown_cause", "stop_run_id") if key in result},
            }), parent_id),
        )
    if not cleanup_pending:
        table = "shutdown_owners" if row["managed"] else "external_shutdown_observations"
        conn.execute(f"DELETE FROM {table} WHERE run_id=?", (row["run_id"],))
    return result


def retire_appointments(
    conn, project_id: str, scope: dict, outcome: str, now: int, *,
    stop_at: int | None = None, owner_run_id: str | None = None,
) -> None:
    for row in _appointments(conn, project_id, scope):
        if (stop_at is None or row["stop_at"] == stop_at) and (
            not row["managed"] or owner_run_id is None or row["run_id"] == owner_run_id
        ):
            _record_outcome(conn, row, outcome, now)


def settle_early(
    conn, instance: dict, now: int, *, stop_run: dict | None = None, inspect_inflight: bool = False,
    expected: dict | None = None,
) -> bool:
    """Use fresh power-state evidence and keep ownership until any stale timer is cleared."""
    if instance["state"] != "Stopped":
        return False
    scope = {"instance_id": instance["id"], "region": instance["region"], "zone": instance["zone"]}
    project_id = instance["project_id"]
    rows = _appointments(conn, project_id, scope)
    if rows and not (stop_run or inspect_inflight) and conn.execute(
        """SELECT 1 FROM task_runs r JOIN task_operations o ON o.run_id=r.id
           WHERE r.project_id=? AND r.state='reconciling' AND r.phase='verify_stop'
             AND o.action='stop' AND o.state IN ('accepted','reserved') AND json_extract(o.data,'$.instance_id')=?""",
        (project_id, instance["id"]),
    ).fetchone():
        return False
    known = instance.get("scheduler_status") == "known"
    actual = _shutdown_time(instance.get("stop_time")) or now
    cleanup_pending = False
    for row in rows:
        if expected is not None and (row["run_id"], row["stop_at"]) != (expected["run_id"], expected["stop_at"]):
            continue
        result = json.loads(row["result"])
        matches_cloud = not known or instance.get("scheduler_stop_time") == row["stop_at"]
        if result.get("shutdown_outcome") == "early":
            if matches_cloud:
                cleanup_pending |= bool(row["managed"])
            else:
                _record_outcome(conn, row, "early", now)
            continue
        started_at = result.get("shutdown_started_at")
        current_start = instance.get("start_time")
        if not (row["created_at"] <= actual <= now and actual < row["stop_at"]) or (
            started_at and current_start and current_start < started_at
        ):
            continue
        pending = bool(row["managed"] and matches_cloud)
        cause = stop_run["snapshot"].get("trigger", {}).get("kind") if stop_run else None
        if stop_run and not cause:
            cause = "manual" if (stop_run["request_id"] or "").startswith("action:") else "scheduled"
        _record_outcome(conn, row, "early", now, cleanup_pending=pending, evidence={
            "actual_stop_at": actual, "shutdown_cause": cause or "observed", "stop_run_id": stop_run["id"] if stop_run else None,
        })
        if pending:
            enqueue_owner_cancel(conn, json.loads(row["snapshot"]), dict(row), now)
            cleanup_pending = True
    return cleanup_pending


def _update_linked(conn, run: dict, status: str, outcome: dict | None = None) -> None:
    parent_run_id = run["snapshot"]["config"].get("parent_run_id")
    if parent_run_id:
        conn.execute(
            "UPDATE task_runs SET result=json_patch(result,?) WHERE id=?",
            (encode({"shutdown_confirmation": status} | (outcome or {})), parent_run_id),
        )


def _wait_for_readback(runner: "TaskRunner", run: dict, reason: str, result: dict, *, phase: str | None = None) -> None:
    with storage.connect(runner.store.path) as conn, storage.transaction(conn):
        current = runner._current(conn, run)
        if current is not None:
            if run["snapshot"]["config"].get("parent_run_id"):
                _update_linked(conn, run, "unconfirmed")
                runner._emit(conn, current, "实例已运行，" + reason, result, "shutdown-pending")
            runner._write_transition(conn, current, "reconciling" if phase else "waiting", reason, result, phase=phase)


def _finish(
    runner: "TaskRunner", run: dict, state: str, reason: str, result: dict, *, phase: str | None = None,
) -> None:
    with storage.connect(runner.store.path) as conn, storage.transaction(conn):
        current = runner._current(conn, run)
        if current is not None:
            if result.get("shutdown_registration") == "external":
                _track_external_shutdown(conn, run["id"], result, result["shutdown_at"])
            status = {"early": "fulfilled", "superseded": "superseded"}.get(result.get("shutdown_outcome"))
            outcome = {key: result[key] for key in ("shutdown_outcome", "actual_stop_at") if key in result}
            _update_linked(conn, run, status or ("confirmed" if state == "succeeded" else "unconfirmed"), outcome)
            runner._write_transition(conn, current, state, reason, result, phase=phase,
                                     notify=result.get("shutdown_outcome") != "early")


def _finish_set(runner: "TaskRunner", run: dict, op: dict, instance: dict, result: dict) -> None:
    now = runner.store.now()
    with storage.connect(runner.store.path) as conn, storage.transaction(conn):
        current = runner._current(conn, run)
        if current is None:
            return
        if op["cloud_code"] == "InstanceOperationInProgress":
            # The conflicting request could have been submitted outside the console.
            reason = "已回读到相同时间的云端预约，保留现有安排"
            _track_external_shutdown(conn, run["id"], result, result["shutdown_at"])
            result = result | {"shutdown_registration": "external"}
        else:
            previous = result.get("previous_shutdown_at")
            if previous is not None and previous != result["shutdown_at"]:
                retire_appointments(conn, run["project_id"], result, "superseded", now, stop_at=previous)
            conn.execute(
                """INSERT INTO shutdown_owners(project_id,instance_id,run_id,task_id,region,zone,stop_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(project_id,instance_id) DO UPDATE SET
                       run_id=excluded.run_id,task_id=excluded.task_id,region=excluded.region,
                       zone=excluded.zone,stop_at=excluded.stop_at""",
                (run["project_id"], instance["id"], run["id"], run["task_id"], result["region"],
                 result["zone"], result["shutdown_at"]),
            )
            conn.execute(
                "UPDATE task_operations SET state='accepted',updated_at=? WHERE id=?",
                (now, op["id"]),
            )
            reason = "云端已确认关机预约"
            result = result | {"shutdown_registration": "managed"}
            cancel_task_id = (current["source_task_id"] or current["task_id"]) if current["cancel_shutdown_on_confirmation"] else None
            if cancel_task_id and result["shutdown_at"] > now:
                task = task_view(conn.execute("SELECT * FROM tasks WHERE id=?", (cancel_task_id,)).fetchone())
                owner = {
                    "instance_id": instance["id"], "run_id": run["id"],
                    "region": result["region"], "zone": result["zone"], "stop_at": result["shutdown_at"],
                }
                enqueue_owner_cancel(conn, task_snapshot(task), owner, now)
        _update_linked(conn, run, "confirmed")
        runner._write_transition(conn, current, "succeeded", reason, result | {"cloud_shutdown_at": result["shutdown_at"]})
        settle_early(conn, instance, now, inspect_inflight=True)


def _finish_cancel(runner: "TaskRunner", run: dict, result: dict, *, outcome: str = "canceled") -> None:
    with storage.connect(runner.store.path) as conn, storage.transaction(conn):
        current = runner._current(conn, run)
        if current is not None:
            early = any(json.loads(row["result"]).get("shutdown_outcome") == "early"
                        for row in _appointments(conn, run["project_id"], result)
                        if row["run_id"] == result.get("owner_run_id"))
            if result.get("shutdown_at") is not None:
                retire_appointments(conn, run["project_id"], result, outcome, runner.store.now(),
                                    stop_at=result["shutdown_at"], owner_run_id=result.get("owner_run_id"))
            reason = "原关机预约已结清，已保留当前云端安排" if outcome == "superseded" else "已回读确认没有云端关机预约"
            runner._write_transition(conn, current, "succeeded", reason, result | {
                "cloud_shutdown_at": result.get("cloud_shutdown_at") if outcome == "superseded" else None,
            }, notify=not early and (not (run["request_id"] or "").startswith("cancel-owner:") or outcome == "canceled"))


def _track_external_shutdown(conn, run_id: str, scope: dict, stop_at: int) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO external_shutdown_observations (run_id,instance_id,region,zone,stop_at)
           VALUES (?,?,?,?,?)""",
        (run_id, scope["instance_id"], scope["region"], scope["zone"], stop_at),
    )


def observe_due_shutdowns(runner: "TaskRunner", limit: int = 5) -> None:
    """Check whether cloud-registered shutdowns reached their observed terminal state."""
    now = runner.store.now()
    with storage.connect(runner.store.path) as conn:
        rows = conn.execute(
            """SELECT o.run_id,o.instance_id,o.region,o.zone,o.stop_at,1 AS owned
               FROM shutdown_owners o JOIN task_runs r ON r.id=o.run_id
               WHERE o.stop_at<=?1 AND r.next_check<=?2 AND json_extract(r.result,'$.shutdown_outcome') IS NOT 'early'
               UNION ALL
               SELECT o.run_id,o.instance_id,o.region,o.zone,o.stop_at,0 AS owned
               FROM external_shutdown_observations o JOIN task_runs r ON r.id=o.run_id
               WHERE o.stop_at<=?1 AND r.next_check<=?2 AND ?4
               ORDER BY stop_at,owned DESC LIMIT ?3""",
            (now - 60, now, limit, runner.cloud is not None),
        ).fetchall()
    for row in rows:
        owner = dict(row)
        run = runner.store.get_run(owner["run_id"])
        instance = None
        try:
            if runner.cloud is not None:
                instance = runner._target(run, owner)
        except CloudError:
            pass
        now = runner.store.now()
        if instance is not None and instance["state"] == "Stopped":
            with storage.connect(runner.store.path) as conn, storage.transaction(conn):
                settle_early(conn, instance, now, inspect_inflight=True, expected=owner)
        instance_state = instance.get("state") if instance else None
        deadline = owner["stop_at"] + SHUTDOWN_OBSERVATION_GRACE
        stopped = instance_state == "Stopped"
        table = "shutdown_owners" if owner["owned"] else "external_shutdown_observations"
        scope = (run["id"], owner["instance_id"], owner["region"], owner["zone"], owner["stop_at"])
        with storage.connect(runner.store.path) as conn, storage.transaction(conn):
            current = runner._current(conn, run)
            if current is None:
                continue
            next_check = None
            if not owner["owned"] and instance is None:
                next_check = now + 60
            elif not stopped and now < deadline and (owner["owned"] or instance_state == "Stopping"):
                next_check = min(now + 30, deadline)
            if next_check is not None:
                conn.execute(
                    f"""UPDATE task_runs SET next_check=? WHERE id=? AND EXISTS (
                        SELECT 1 FROM {table} WHERE run_id=? AND instance_id=? AND region=? AND zone=? AND stop_at=?
                    )""",
                    (next_check, run["id"], *scope),
                )
                continue
            rows = [row for row in _appointments(conn, run["project_id"], owner) if row["stop_at"] == owner["stop_at"]]
            if not any(row["run_id"] == run["id"] for row in rows) or any(
                json.loads(row["result"]).get("shutdown_outcome") == "early" for row in rows
            ):
                continue
            reason = "预约到时回读确认实例已关机" if stopped else "已过预约时间，未回读确认实例关机"
            outcome = "confirmed" if stopped else "missed"
            result = current["result"]
            for appointment in rows:
                observation = {
                    "checked_at": now, "instance_state": instance_state, "managed": bool(appointment["managed"]),
                    "stop_time": _shutdown_time(instance.get("stop_time")) if instance else None,
                }
                recorded = _record_outcome(conn, appointment, outcome, now, evidence={"shutdown_observation": observation})
                if appointment["run_id"] == run["id"]:
                    result = recorded
            policy_event = None
            if owner["owned"] and owner["stop_at"] >= storage.get_settings(conn).get("feishu_policy_since", 0):
                policy_event = "instance_stopped" if stopped else "shutdown_attention"
            runner._emit(conn, current, reason, result, "shutdown-outcome", policy_event=policy_event)
