"""Order and reconcile selected CFS storage before placing the GPU order."""

from contextlib import ExitStack
from decimal import Decimal, DecimalException
from hashlib import sha256
from typing import TYPE_CHECKING

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.application import shared_storage as orders
from gpu_console.application.tasks import run_view
from gpu_console.storage.maintenance import process_lock

if TYPE_CHECKING:
    from gpu_console.worker.tasks import TaskRunner

CFS_READBACK_SECONDS = 600


def pinned_storage_scope(run: dict) -> tuple[str, str] | None:
    scope = run["result"].get("storage_scope")
    return (scope["region"], scope["zone"]) if scope else None


def _scope(spec: dict) -> dict:
    return {key: spec[key] for key in ("project_id", "region", "zone", "zone_id", "is_pod") if key in spec}


def _scope_lock(stack: ExitStack, runner: "TaskRunner", scope: dict) -> bool:
    scope_id = sha256(orders.fingerprint(scope).encode()).hexdigest()
    try:
        stack.enter_context(process_lock(runner.store.path.parent, f".cfs-{scope_id}.lock"))
    except ValueError:
        return False
    return True


def _single(runner: "TaskRunner", spec: dict, identity: str | None = None) -> dict | None:
    rows = runner.cloud.describe_cfs(spec, cfs_id=identity)
    if len(rows) > 1 or (rows and identity and rows[0]["id"] != identity):
        raise CloudError("cfs_scope_unverified")
    return rows[0] if rows else None


def _matches_order(current: dict | None, intent: dict) -> bool:
    return bool(current and current["size_gib"] == intent["size_gib"] and (
        intent["mode"] == "cfs_resize"
        or (current["name"] == intent["name"] and current["charge_type"] == intent["charge_type"])
    ))


def _waiting_state(run: dict) -> str:
    from gpu_console.worker.tasks import _single_purchase

    return "failed" if _single_purchase(run) else "waiting"


def preview_storage(cloud, spec: dict) -> Decimal | None:
    """Return a verified paid storage quote for exactly the selected GPU scope."""
    intent = spec.get("shared_storage") or {}
    if intent.get("mode") not in orders.PAID_MODES:
        return None
    price = cloud.quote_cfs(spec, intent) if intent["mode"] == "cfs_create" else cloud.quote_cfs_resize(spec, intent)
    try:
        limit = Decimal(intent["max_price"])
    except (DecimalException, KeyError, TypeError):
        raise CloudError("quote_unverified") from None
    if not isinstance(price, Decimal) or not price.is_finite() or price < 0 or not limit.is_finite() or limit < 0:
        raise CloudError("quote_unverified")
    return price


def ensure_storage(runner: "TaskRunner", run: dict, spec: dict) -> bool:
    intent = spec.get("shared_storage") or {}
    mode = intent.get("mode")
    if not mode or mode == "us3":
        return True
    scope = _scope(spec)
    if mode == "cfs_attach":
        if _single(runner, scope, intent["cfs_id"]):
            return True
        runner._transition(run, _waiting_state(run), "所选云存储 Pro 未通过项目和可用区核对")
        return False
    if mode not in orders.PAID_MODES:
        raise CloudError("cfs_scope_unverified")
    with ExitStack() as lock:
        if not _scope_lock(lock, runner, scope):
            runner._transition(run, _waiting_state(run), "同一账号与可用区的云存储 Pro 操作正在进行")
            return False
        return _ensure_paid(runner, run, scope, intent)


def _ensure_paid(runner: "TaskRunner", run: dict, scope: dict, intent: dict) -> bool:
    with storage.connect(runner.store.path) as conn:
        op = orders.operation(conn, run["id"])
        blocked = orders.blocked(conn, run["id"], scope)
    if op:
        data = op["data"]
        if orders.fingerprint(data["scope"]) != orders.fingerprint(scope) or data["intent"] != intent:
            runner._transition(run, "manual_review", "本次运行已有不同范围的云存储 Pro 记录，请核对")
            return False
        if op["state"] == "verified":
            current = _single(runner, scope, data["cfs_id"])
            if _matches_order(current, intent):
                return True
            runner._transition(run, "manual_review", "已准备的云存储 Pro 当前配置已改变，请核对")
            return False
        if op["state"] in orders.PENDING_STATES:
            runner._transition(run, "reconciling", "继续核对已提交的云存储 Pro 操作", phase=orders.VERIFY_PHASE)
            return False
        runner._transition(run, "failed", "本次运行已记录云存储 Pro 操作结果，请核对后重新发起")
        return False
    if blocked:
        runner._transition(run, _waiting_state(run), "同一账号与可用区有待核对的云存储 Pro 操作")
        return False
    current = _single(runner, scope, intent.get("cfs_id"))
    if intent["mode"] == "cfs_create" and current is not None:
        runner._transition(run, "failed", "目标可用区已有云存储 Pro，请改为挂载已有存储或扩容")
        return False
    if intent["mode"] == "cfs_resize" and (current is None or current["size_gib"] >= intent["size_gib"]):
        runner._transition(run, "failed", "目标云存储 Pro 不在当前范围或扩容容量已改变，请重新核对")
        return False
    price = preview_storage(runner.cloud, scope | {"shared_storage": intent})
    details = {
        "scope": scope, "intent": intent, "price": str(price),
        "quote_request_id": getattr(runner.cloud, "last_request_id", None),
    }
    if price > Decimal(intent["max_price"]):
        runner._transition(run, _waiting_state(run), "云存储 Pro 报价超过单独确认的预算", {"storage_quote": str(price)})
        return False
    if current is not None:
        details["previous_size_gib"] = current["size_gib"]
    claim = _claim(runner, run, details)
    if claim is not None:
        _send(runner, *claim, details)
    return False


def _claim(runner: "TaskRunner", run: dict, details: dict) -> tuple[dict, int] | None:
    from gpu_console.worker.tasks import _expired

    now = runner.store.now()
    with storage.connect(runner.store.path) as conn, storage.transaction(conn):
        current = runner._current(conn, run)
        if (current is None or current["stopped"] or current["phase"] or runner.read_only
                or _expired(current, now) or storage.get_settings(conn)["restore_gate"]):
            return None
        if runner._purchase_limit_reached(conn, current):
            runner._write_transition(conn, current, "failed", "达到 GPU 购买次数上限，暂停云存储 Pro 操作", {})
            return None
        if orders.operation(conn, current["id"]) or orders.blocked(conn, current["id"], details["scope"]):
            return None
        operation_id = orders.reserve(conn, current, details, now)
        runner._write_transition(conn, current, "reconciling", "已保存云存储 Pro 操作意图", {
            "storage_scope": details["scope"], "storage_quote": details["price"],
            "storage_state": "reserved",
        }, phase=orders.VERIFY_PHASE)
        conn.execute("UPDATE task_runs SET next_check=min(next_check,?) WHERE id=?",
                     (now + CFS_READBACK_SECONDS, current["id"]))
        row = conn.execute("SELECT * FROM task_runs WHERE id=?", (current["id"],)).fetchone()
        return run_view(row), operation_id


def _send(runner: "TaskRunner", run: dict, operation_id: int, details: dict) -> None:
    from gpu_console.application.task_config import shutdown_registration_closed
    from gpu_console.worker.tasks import SHUTDOWN_CUTOFF, _expired

    with storage.connect(runner.store.path) as conn:
        latest = runner._current(conn, run)
        paused = runner.read_only or storage.get_settings(conn)["restore_gate"]
    if latest is None:
        return
    if latest["stopped"] or paused or _expired(latest, runner.store.now()):
        state = "canceled" if latest["stopped"] else "expired" if not paused else "failed"
        cutoff = state == "expired" and shutdown_registration_closed(latest, runner.store.now())
        runner._operation_result(run, operation_id, "not_dispatched", state,
                                 SHUTDOWN_CUTOFF if cutoff else "发送前确认本次云存储 Pro 操作已停止、暂停或过期",
                                 {"storage_state": "not_dispatched", **({"shutdown_cutoff_reached": True} if cutoff else {})},
                                 None)
        return
    intent, scope = details["intent"], details["scope"]
    call = runner.cloud.create_cfs if intent["mode"] == "cfs_create" else runner.cloud.resize_cfs
    try:
        identity = call(scope, intent, run["id"])
    except CloudError as error:
        op_state = "reserved" if error.uncertain else "rejected"
        runner._operation_result(run, operation_id, op_state, "reconciling" if error.uncertain else "failed",
                                 str(error), {"cloud_code": error.code, "cloud_request_id": error.request_id,
                                              "storage_state": "uncertain" if error.uncertain else "rejected"},
                                 orders.VERIFY_PHASE if error.uncertain else None)
    else:
        runner._operation_result(run, operation_id, "accepted", "reconciling", "云存储 Pro 已受理，等待回读", {
            "receipt_cfs_id": identity, "storage_state": "accepted",
            "cloud_request_id": getattr(runner.cloud, "last_request_id", None), "cloud_code": None,
        }, orders.VERIFY_PHASE)
    latest = runner.store.get_run(run["id"])
    if latest["phase"] == orders.VERIFY_PHASE:
        _reconcile_locked(runner, latest)


def reconcile_storage(runner: "TaskRunner", run: dict) -> None:
    scope = run["result"].get("storage_scope")
    if not scope:
        _manual_review(runner, run, "缺少待核对的云存储 Pro 范围")
        return
    with ExitStack() as lock:
        if _scope_lock(lock, runner, scope):
            _reconcile_locked(runner, run)
        elif run["result"].get("instance_recheck_pending"):
            _manual_review(runner, run, "同一账号与可用区的云存储 Pro 操作正在进行")


def _review_result(run: dict, now: int, detail: str) -> dict:
    if run["result"].get("instance_recheck_pending"):
        return {"instance_recheck_pending": False, "instance_review": {"checked_at": now, "detail": detail}}
    return {}


def _manual_review(runner: "TaskRunner", run: dict, reason: str) -> None:
    runner._transition(run, "manual_review", reason, _review_result(run, runner.store.now(), reason),
                       phase=orders.VERIFY_PHASE)


def _defer_readback(runner: "TaskRunner", run: dict, op: dict, details: dict, reason: str) -> None:
    deadline = op["created_at"] + CFS_READBACK_SECONDS
    with storage.connect(runner.store.path) as conn, storage.transaction(conn):
        current = runner._current(conn, run)
        if current is None:
            return
        now = runner.store.now()
        if current["result"].get("instance_recheck_pending") or now >= deadline:
            review = _review_result(current, now, reason)
            runner._write_transition(conn, current, "manual_review", reason,
                                     details | review | {"storage_state": "unverified"}, phase=orders.VERIFY_PHASE)
        else:
            runner._write_transition(conn, current, "reconciling", reason, details, phase=orders.VERIFY_PHASE)
            conn.execute("UPDATE task_runs SET next_check=min(next_check,?) WHERE id=?", (deadline, run["id"]))


def _reconcile_locked(runner: "TaskRunner", run: dict) -> None:
    from gpu_console.worker.tasks import _expired

    with storage.connect(runner.store.path) as conn:
        op = orders.operation(conn, run["id"])
    if op is None or op["state"] not in orders.PENDING_STATES:
        _manual_review(runner, run, "缺少待核对的云存储 Pro 操作记录")
        return
    if (not run["result"].get("instance_recheck_pending")
            and runner.store.now() >= op["created_at"] + CFS_READBACK_SECONDS):
        _defer_readback(runner, run, op, {}, "云存储 Pro 操作结果待人工核对，继续保留操作占位")
        return
    if runner.cloud is None:
        _defer_readback(runner, run, op, {}, "未配置平台凭据，无法核对已提交的云存储 Pro 操作")
        return
    data = op["data"]
    intent = data["intent"]
    identity = data.get("receipt_cfs_id") or intent.get("cfs_id")
    try:
        current = _single(runner, data["scope"], identity)
    except CloudError as error:
        _defer_readback(runner, run, op, {
            "cloud_code": error.code, "cloud_request_id": error.request_id,
        }, "云存储 Pro 回读失败，保留已提交操作")
        return
    if not _matches_order(current, intent) or (
        not identity and (type(current["created_at"]) is not int
                          or not op["created_at"] <= current["created_at"] <= runner.store.now())
    ):
        _defer_readback(runner, run, op, {}, "云存储 Pro 结果尚未核实，保留已提交操作")
        return
    with storage.connect(runner.store.path) as conn, storage.transaction(conn):
        latest = runner._current(conn, run)
        if latest is None:
            return
        now = runner.store.now()
        orders.confirm(conn, op, current["id"], now)
        state, reason = "waiting", "已核对云存储 Pro，等待再次确认 GPU 规格与预算"
        if latest["stopped"]:
            state, reason = "canceled", "云存储 Pro 已就绪，本次 GPU 采购已停止"
        elif _expired(latest, now):
            state, reason = "expired", "云存储 Pro 已就绪，本次 GPU 采购窗口已结束"
        runner._write_transition(conn, latest, state, reason, {
            "storage_state": "ready", "cfs_id": current["id"], "storage_quote": data["price"],
            "storage_scope": data["scope"], "storage_size_gib": current["size_gib"], "cloud_code": None,
        } | _review_result(latest, now, "已核对云存储 Pro 的资源 ID 和容量"))
        if state == "waiting":
            conn.execute("UPDATE task_runs SET next_check=? WHERE id=?", (now, latest["id"]))


def attachment_status(runner: "TaskRunner", instance: dict, spec: dict, run: dict) -> bool | None:
    intent = spec["shared_storage"]
    if intent["mode"] == "us3":
        attached_at = instance.get("us3_attached_at")
        return attached_at > 0 if type(attached_at) is int else None
    cfs_id = intent.get("cfs_id") or run["result"].get("cfs_id")
    if not cfs_id:
        return None
    try:
        current = _single(runner, spec, cfs_id)
    except CloudError:
        return None
    mounted = current.get("mounted_instance_ids") if current else None
    return instance["id"] in mounted if mounted is not None else None


def verify_storage(
    runner: "TaskRunner", run: dict, spec: dict, instance_id: str, *,
    instance: dict | None = None, report_pending: bool = True,
) -> bool:
    intent = spec.get("shared_storage") or {}
    mode = intent.get("mode")
    if not mode:
        return True
    try:
        if instance is None and mode == "us3":
            instance = runner._target(run, spec | {"instance_id": instance_id})
        target = instance if instance is not None else {"id": instance_id}
        verified = attachment_status(runner, target, spec, run)
    except CloudError:
        verified = False
    if verified:
        return True
    if report_pending:
        runner._transition(run, "reconciling", "GPU 实例已回读，等待核实所选共享存储挂载", {
            "instance_id": instance_id, "storage_state": "attachment_pending",
        }, phase=run["phase"])
    return False
