"""Serial execution of durable task runs using the CompShare adapter."""

import json
from collections.abc import Collection
from decimal import Decimal
from typing import Any

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError, create_instance_name
from gpu_console.application import automation_rules, feishu_notices, instance_release, instance_resize, watches
from gpu_console.application.purchase_options import platform_candidates, verify_purchase_spec
from gpu_console.application.task_config import (
    SHUTDOWN_MINIMUM, SPEC_FIELDS, disk_key, includes_event, match_spec, run_window_expired, shutdown_registration_closed,
)
from gpu_console.application.tasks import (
    INSTANCE_RECHECK_PHASES,
    INSTANCE_RECHECK_SQL,
    PURCHASE_RECHECK_PHASES,
    TERMINAL_RUNS,
    TaskStore,
    conflicts_with_create_receipt,
    encode,
    run_view,
    write_audit,
)
from gpu_console.storage import capacity, cardless, instance_history
from gpu_console.worker import shared_storage, shutdown


INSTANCE_CHANGE_PHASES = {"verify_reboot", "verify_rename", "verify_terminate", "verify_resize"}
INSTANCE_READBACK_SECONDS = 600
PURCHASE_RETRY_INTERVAL = 60
SHUTDOWN_CUTOFF = "已超过最晚开机时间，距离兜底关机不足 5 分钟"


def _single_purchase(run: dict) -> bool:
    return "instance_quote_id" in run["snapshot"] or run["snapshot"].get("purchase_retry") is False


def _expired(run: dict, now: int) -> bool:
    if shutdown_registration_closed(run, now):
        return True
    if "purchase_retry" in run["snapshot"]:
        return now >= run["deadline_at"]
    return run_window_expired(run, now)


class TaskRunner:
    def __init__(
        self, store: TaskStore, cloud: Any, *, read_only: bool = False,
        release_charge_types: Collection[str] = instance_release.DEFAULT_CHARGE_TYPES,
    ):
        self.store = store
        self.cloud = cloud
        self.read_only = read_only
        self.release_charge_types = release_charge_types

    def tick(self, limit: int = 25) -> int:
        self.store.enqueue_due()
        shutdown.observe_due_shutdowns(self)
        with storage.connect(self.store.path) as conn:
            runs = [run_view(row) for row in conn.execute(
                """SELECT * FROM task_runs WHERE (state IN ('queued','waiting','reconciling') OR phase='review_shutdown')
                     AND next_check<=? ORDER BY next_check,
                     CASE WHEN json_extract(snapshot,'$.kind')='stop' THEN 0 ELSE 1 END,
                     scheduled_at,created_at,id LIMIT ?""",
                (self.store.now(), limit),
            )]
        for run in runs:
            self._run(run)
        return len(runs)

    def _run(self, run: dict) -> None:
        single_purchase = _single_purchase(run)
        if run["phase"]:
            action = self._reconcile
        elif run["stopped"]:
            self._transition(run, "canceled", "任务已停止后续尝试")
            return
        elif _expired(run, self.store.now()):
            if run["kind"] == "shutdown":
                shutdown.expire_shutdown(self, run)
            elif run["kind"] == "cancel_shutdown":
                self._transition(run, "manual_review", "撤销待确认，执行窗口已结束")
            elif shutdown_registration_closed(run, self.store.now()):
                self._transition(run, "expired", SHUTDOWN_CUTOFF, {"shutdown_cutoff_reached": True})
            elif "instance_quote_id" in run["snapshot"]:
                self._transition(run, "expired", "确认的报价已过期，请重新询价")
            elif run["snapshot"].get("trigger", {}).get("kind") == "idle_stop":
                self._transition(run, "expired", "允许停机时段已结束")
            else:
                self._transition(run, "expired", run["reason"] or "执行窗口已结束")
            return
        elif self.cloud is None:
            if single_purchase:
                self._transition(run, "failed", "优云智算凭据未配置，无法创建实例")
            else:
                self._transition(run, "waiting", "等待配置优云智算凭据")
            return
        elif self._writes_disabled() and run["kind"] not in {"stock", "inspection"}:
            if single_purchase:
                self._transition(run, "failed", "云端写操作暂停，恢复后请重新询价")
            else:
                self._transition(run, "waiting", "云端写操作暂停，等待恢复核对或部署开放")
            return
        else:
            action = {
                "stock": self._stock,
                "acquire": self._acquire,
                "start": self._start,
                "stop": self._stop,
                "reboot": self._reboot,
                "rename": self._rename,
                "terminate": self._terminate,
                "resize": self._resize,
                "inspection": self._inspection,
                "shutdown": lambda item: shutdown.set_shutdown(self, item),
                "cancel_shutdown": lambda item: shutdown.cancel_shutdown(self, item),
            }[run["kind"]]

        local_review_phases = PURCHASE_RECHECK_PHASES | {"review_cancel_shutdown", "review_shutdown"}
        if self.cloud is None and run["phase"] not in INSTANCE_RECHECK_PHASES | local_review_phases:
            self._transition(run, "reconciling", "等待平台凭据以回读已提交的操作", phase=run["phase"])
            return
        try:
            action(run)
        except CloudError as error:
            result = {"cloud_code": error.code, "cloud_request_id": error.request_id}
            if run["phase"]:
                self._transition(run, "reconciling", str(error), result, phase=run["phase"])
            else:
                state = "waiting" if error.retryable and not single_purchase else "failed"
                self._transition(run, state, str(error), result)

    def _writes_disabled(self) -> bool:
        with storage.connect(self.store.path) as conn:
            return self.read_only or storage.get_settings(conn)["restore_gate"]

    def _delay(self, conn, result: dict, phase: str | None) -> int:
        base = storage.get_settings(conn)["task_stock_seconds"]
        if result.get("cloud_code") == "429":
            return max(60, base * 4)
        return min(base, 5) if phase == "verify_reboot" else base

    def _current(self, conn, run: dict) -> dict | None:
        row = conn.execute("SELECT * FROM task_runs WHERE id=?", (run["id"],)).fetchone()
        current = run_view(row) if row is not None else None
        if current is None or any(current[key] != run[key] for key in (
            "state", "phase", "result", "next_check",
        )):
            return None
        return current

    def _transition(
        self, run: dict, state: str, reason: str, result: dict | None = None, *,
        phase: str | None = None, policy_event: feishu_notices.FeishuEvent | None = None,
    ) -> None:
        with storage.connect(self.store.path) as conn, storage.transaction(conn):
            current = self._current(conn, run)
            if current is not None:
                self._write_transition(conn, current, state, reason, result or {}, phase=phase, policy_event=policy_event)

    def _write_transition(
        self, conn, run: dict, state: str, reason: str, result: dict, *,
        phase: str | None = None, notify: bool = True, policy_event: feishu_notices.FeishuEvent | None = None,
    ) -> None:
        now = self.store.now()
        merged = run["result"] | result
        if run["stopped"] and state == "waiting" and phase is None:
            state, reason = "canceled", "任务已停止后续尝试"
        if state == "waiting" and "capacity_attempts" in result:
            next_check = now + run["snapshot"]["config"]["capacity_retry"]["interval_seconds"]
        else:
            next_check = now + self._delay(conn, merged, phase)
        if state == "waiting" and phase is None and "purchase_retry" in run["snapshot"]:
            if run["snapshot"]["purchase_retry"]:
                next_check = max(next_check, now + PURCHASE_RETRY_INTERVAL)
            next_check = min(next_check, run["deadline_at"])
        target = run["snapshot"]["config"].get("shutdown_at")
        if state == "waiting" and phase is None and run["kind"] in {"start", "acquire"} and target is not None:
            next_check = min(next_check, target - SHUTDOWN_MINIMUM + 1)
        if state == "reconciling" and phase in INSTANCE_RECHECK_PHASES and not merged.get("instance_recheck_pending"):
            intent = conn.execute(
                "SELECT created_at FROM task_operations WHERE run_id=? ORDER BY id DESC LIMIT 1", (run["id"],),
            ).fetchone()
            if intent:
                next_check = min(next_check, intent["created_at"] + INSTANCE_READBACK_SECONDS)
        if state == "reconciling" and phase in {"verify_create", "manual_bind"}:
            deadline = merged.get("creation_readback_deadline_at")
            if type(deadline) is int and deadline > now:
                next_check = min(next_check, deadline)
        if run["result"].get("instance_recheck_pending") and result.get("instance_recheck_pending") is False:
            write_audit(conn, run["id"], "instance_recheck_result", result["instance_review"], now)
        conn.execute(
            """UPDATE task_runs SET state=?,phase=?,reason=?,result=?,next_check=?,updated_at=?
               WHERE id=?""",
            (state, phase, reason, encode(merged), next_check, now, run["id"]),
        )
        if (
            state in {"canceled", "expired"} and run["snapshot"].get("trigger", {}).get("automation_rule_id")
            and automation_rules.note_blocked_stop(conn, run, state, reason, now)
        ):
            return
        if notify and (state in TERMINAL_RUNS - {"canceled"} or state == "manual_review"):
            category = "task_result"
            if run["kind"] in {"stop", "shutdown", "cancel_shutdown"}:
                category = "shutdown_result"
            elif run["kind"] in {"stock", "start", "acquire"}:
                category = {
                    "succeeded": "acquisition_succeeded", "expired": "acquisition_expired",
                    "missed": "acquisition_expired",
                }.get(state, category)
                if merged.get("disposition") == "mode_conflict":
                    category = "task_result"
            if policy_event is None and run["kind"] not in {"stock", "inspection"}:
                if state in {"failed", "expired", "missed", "manual_review"}:
                    policy_event = "shutdown_attention" if run["kind"] == "shutdown" else (
                        "operation_review" if state == "manual_review" else "operation_failed"
                    )
                elif run["kind"] == "shutdown" and merged.get("shutdown_registration") == "managed":
                    policy_event = "shutdown_registered"
            self._emit(conn, run, reason, merged, "result" if state != "manual_review" else "review", category,
                       policy_event=policy_event)

    def _emit(
        self, conn, run: dict, reason: str, result: dict, event: str, category: str = "shutdown_result", *,
        policy_event: feishu_notices.FeishuEvent | None = None, legacy: bool = True,
    ) -> None:
        snapshot = run["snapshot"]
        channels = snapshot["channels"] if legacy and includes_event(snapshot, category) else []
        if not channels and policy_event is None:
            return
        title = snapshot["name"]
        payload = {
            "title": title, "message": reason, "task_name": title, "run_id": run["id"],
            "instance_id": result.get("instance_id") or snapshot["config"].get("instance_id"),
            "gpu_model": result.get("gpu_model"),
            "zone": result.get("zone"), "project_id": run["project_id"],
            "confirmed_at": self.store.now(), "recipient": snapshot["config"].get("email"),
        }
        details = [value for value in (payload["instance_id"], payload["gpu_model"], payload["zone"]) if value]
        if details:
            payload["message"] += "｜" + "｜".join(details)
        feishu_notices.emit(conn, run["id"] + ":" + event, payload, channels, policy_event=policy_event,
                           feishu_configured=policy_event is not None and self.store.feishu_configured(conn))

    def _stock(self, run: dict) -> None:
        config = run["snapshot"]["config"]
        if config.get("stock_pool"):
            self._stock_pool(run)
            return
        unknown = False
        for spec in self._specs(run):
            available = self.cloud.capacity(spec)
            if available is True:
                self._transition(run, "succeeded", "检测到目标规格可开", {
                    "region": spec["region"], "zone": spec["zone"], "gpu_model": config["gpu_model"],
                    "spec": spec, "checked_at": self.store.now(),
                })
                return
            unknown = unknown or available is None
        self._transition(run, "waiting", "部分目标规格库存未知" if unknown else "目标规格当前缺货")

    def _stock_pool(self, run: dict) -> None:
        now = self.store.now()
        unknown = False
        with storage.connect(self.store.path) as conn, storage.transaction(conn, immediate=False):
            combos, pools = watches.current_catalog(conn), watches.pool_rows(conn, now)
            profiles = capacity.profiles(conn, run["project_id"], combos)
            for spec in self._specs(run):
                key = watches.catalog_spec_id(spec)
                if key not in combos:
                    unknown = True
                    continue
                stock = watches.spec_stock(spec, pools, profiles, now)
                if stock["available"] is True:
                    result = stock | {key: spec[key] for key in ("region", "zone", "gpu_model")}
                    result.update(spec=spec, catalog_spec_id=key, checked_at=stock["capacity_checked_at"])
                    break
                unknown |= stock["available"] is None
            else:
                result = None
        if result is not None:
            self._transition(run, "succeeded", "平台确认规格可租", result)
        else:
            self._transition(run, "waiting", "部分目标规格探测结果未知" if unknown else "目标规格当前不可租")

    def _inspection(self, run: dict) -> None:
        config = run["snapshot"]["config"]
        if config.get("instance_id"):
            instance = self._target(run)
            if instance is None:
                self._transition(run, "failed", "指定实例未在项目和可用区中找到")
                return
            self._transition(run, "succeeded", "已核对实例状态", {
                "instance_id": instance["id"], "state": instance["state"], "zone": instance.get("zone"),
            })
        else:
            instances = self.cloud.instances(run["project_id"])
            self._transition(run, "succeeded", f"已核对 {len(instances)} 台实例", {"instance_count": len(instances)})

    def _specs(self, run: dict):
        config = run["snapshot"]["config"]
        fields = (*SPEC_FIELDS, "disks", "quantity", "shared_storage", "cpu_auto", "cpu_locked")
        spec = {key: config[key] for key in fields if key in config}
        pinned = shared_storage.pinned_storage_scope(run)
        for zone in config["zones"]:
            if pinned is not None and pinned != (zone["region"], zone["zone"]):
                continue
            resolved = {
                "region": zone["region"], "zone": zone["zone"], "project_id": run["project_id"],
                "disks": zone.get("resolved_disks", spec.get("disks", [])),
            }
            if "shared_storage" in zone:
                resolved["shared_storage"] = zone["shared_storage"]
            yield spec | resolved

    def _target(self, run: dict, target: dict | None = None) -> dict | None:
        target = target or run["snapshot"]["config"]
        scope = {key: target[key] for key in ("region", "zone")}
        instances = self.cloud.instances(run["project_id"], [scope])
        return next((
            item for item in instances if item["id"] == target["instance_id"]
            and item.get("project_id") == run["project_id"]
            and all(item.get(key) == value for key, value in scope.items())
        ), None)

    def _has_gpu(self, instance: dict, minimum_time: int | None = None) -> bool:
        if instance.get("state") != "Running" or not instance.get("gpu"):
            return False
        if instance.get("gpu_mode") == "with_gpu":
            return True
        if instance.get("gpu_mode") == "without_gpu":
            return False
        start_time = instance.get("start_time")
        if not start_time and not minimum_time:
            return False
        with storage.connect(self.store.path) as conn:
            maximum_age = max(180, storage.get_settings(conn)["collection_seconds"] * 3)
        earliest = max(start_time or 0, minimum_time or 0, self.store.now() - maximum_age)
        try:
            points = self.cloud.monitor(instance["project_id"], instance["region"], [instance["id"]])
        except CloudError:
            return False
        samples: dict[int, set[str]] = {}
        for point in points:
            if (
                point["metric"] == "cloudwatch_gpu_util" and point.get("label")
                and earliest <= point["sampled_at"] <= self.store.now()
            ):
                samples.setdefault(point["sampled_at"], set()).add(point["label"])
        return bool(samples) and len(samples[max(samples)]) == instance["gpu"]

    def _instance_result(self, instance: dict, disposition: str) -> dict:
        return {
            "instance_id": instance["id"], "zone": instance.get("zone"),
            "region": instance.get("region"), "gpu_model": instance.get("gpu_model"),
            "disposition": disposition,
        }

    def _complete_instance(
        self, run: dict, instance: dict, disposition: str, *, track_shutdown: bool = False,
        readback: dict | None = None,
    ) -> None:
        result = self._instance_result(instance, disposition) | (readback or {})
        config = run["snapshot"]["config"]
        cardless_boot = config.get("boot_mode") == "without_gpu"
        mode_label = "无卡" if cardless_boot else "有卡"
        if run["kind"] == "start":
            result["boot_mode"] = "without_gpu" if cardless_boot else "with_gpu"
            if cardless_boot:
                result["without_gpu_spec"] = config["without_gpu_spec"]
        reason = {
            "reused": f"实例已{mode_label}运行，已保留原安排",
            "started": f"已回读到实例{mode_label}运行",
            "created": "已回读到新建实例有卡运行",
            "observed": f"已回读到实例{mode_label}运行，开机请求的归属待核对",
        }[disposition]
        if result.get("storage_state") == "attachment_unverified":
            reason += "，共享存储挂载尚未核实"
        with storage.connect(self.store.path) as conn, storage.transaction(conn):
            current = self._current(conn, run)
            if current is not None:
                target = shutdown.linked_target(current, instance)
                if current["cancel_shutdown_on_confirmation"] and target:
                    result.update(shutdown_confirmation="canceled", shutdown_at=target)
                elif track_shutdown and not (disposition == "reused" and (cardless_boot or config.get("shutdown_after_seconds"))):
                    shutdown.linked_shutdown(self, conn, current, instance, result)
                elif target and (disposition == "observed" or run["result"].get("instance_recheck_pending")):
                    result.update(shutdown_confirmation="unconfirmed", shutdown_at=target)
                    self._emit(conn, current, "已回读到实例运行，关联关机预约待处理",
                               result, "shutdown-pending", policy_event="shutdown_attention")
                if run["task_id"] and not cardless_boot and disposition in {"started", "created"} and instance.get("start_time"):
                    conn.execute(
                        """INSERT INTO idle_notification_runs VALUES (?,?,?,?)
                           ON CONFLICT(project_id,instance_id) DO UPDATE SET
                           start_time=excluded.start_time, run_id=excluded.run_id""",
                        (run["project_id"], instance["id"], instance["start_time"], run["id"]),
                    )
                policy_event = {"started": "instance_started", "created": "instance_created",
                                "observed": "operation_review"}.get(disposition)
                self._write_transition(conn, current, "succeeded", reason, result, policy_event=policy_event)

    def _start(self, run: dict) -> None:
        target = self._target(run)
        if target is None:
            self._transition(run, "failed", "指定实例未在项目和可用区中找到")
        else:
            self._start_instance(run, target)

    def _start_instance(self, run: dict, target: dict) -> None:
        result = self._instance_result(target, "pending")
        config = run["snapshot"]["config"]
        spec = config.get("without_gpu_spec")
        profile = None
        if spec is not None:
            result.update(boot_mode="without_gpu", without_gpu_spec=spec)
            try:
                with storage.connect(self.store.path) as conn:
                    profile = cardless.require_profile(conn, target, spec)
            except ValueError as error:
                self._transition(run, "failed", str(error), result)
                return
        if config.get("shutdown_after_seconds") and target.get("charge_type") not in {"Postpay", "Day", "Month"}:
            self._transition(run, "failed", "此实例的计费方式无法核实为支持关机预约", result)
            return
        state = target["state"]
        if state == "Running":
            no_gpu = target.get("gpu_mode") == "without_gpu"
            matches_gpu = not no_gpu and self._has_gpu(target)
            matches_cardless = profile is not None and cardless.match_profile(target, [profile]) is not None
            if matches_cardless or (profile is None and matches_gpu):
                self._complete_instance(run, target, "reused", track_shutdown=True)
            elif run["kind"] == "start" and (no_gpu or matches_gpu):
                observed = "without_gpu" if no_gpu else "with_gpu"
                with storage.connect(self.store.path) as conn:
                    options = cardless.profiles(conn).get((target["region"], target["zone"]), []) if no_gpu else []
                actual_profile = cardless.match_profile(target, options)
                result.update(disposition="mode_conflict", boot_mode="without_gpu" if profile else "with_gpu",
                              observed_boot_mode=observed, observed_without_gpu_spec=(actual_profile or {}).get("spec"))
                current_mode = f"无卡档位 {actual_profile['spec']}" if actual_profile else "无卡（档位待核对）" if no_gpu else "有卡"
                desired_mode = f"无卡档位 {spec}" if profile else "有卡"
                self._transition(run, "missed", f"实例已按{current_mode}运行，跳过本轮{desired_mode}开机，保留原运行和预约", result)
            else:
                self._transition(run, "waiting", "实例已运行，启动模式待确认", result)
        elif state == "Stopped":
            self._start_stopped(run, target, result, profile=profile)
        else:
            self._transition(run, "waiting", "实例操作状态待核对", result)

    def _start_stopped(self, run: dict, instance: dict, result: dict, *, profile: dict | None = None) -> None:
        now = self.store.now()
        with storage.connect(self.store.path) as conn, storage.transaction(conn):
            current = self._current(conn, run)
            if current is None or current["stopped"]:
                return
            if shutdown.settle_early(conn, instance, now, inspect_inflight=True):
                self._write_transition(conn, current, "waiting", "旧预约清理待确认", result)
                return
            if conn.execute(
                """SELECT 1 FROM task_runs AS r WHERE r.project_id=? AND r.id!=?
                    AND r.state IN ('queued','waiting','reconciling') AND r.stopped=0
                    AND r.scheduled_at<=? AND (r.phase IS NOT NULL OR r.deadline_at>=?)
                    AND json_extract(r.snapshot,'$.kind')='stop'
                    AND json_extract(r.snapshot,'$.config.instance_id')=?
                    AND json_extract(r.snapshot,'$.config.region')=? AND json_extract(r.snapshot,'$.config.zone')=?""",
                (run["project_id"], run["id"], now, now, instance["id"], instance["region"], instance["zone"]),
            ).fetchone():
                self._write_transition(conn, current, "waiting", "等待本次关机结果", result)
                return
        if profile is not None or run["snapshot"]["config"].get("shutdown_after_seconds"):
            result["previous_start_time"] = instance.get("start_time")
        if profile is None:
            self._perform(run, "start", result, lambda: self.cloud.start(instance), "verify_start")
        else:
            result.update(expected_cpu=profile["cpu"], expected_memory_gb=profile["memory_gb"])
            self._perform(run, "start", result, lambda: self.cloud.start_without_gpu(instance, profile["spec"]), "verify_start")

    def _complete_stop(
        self, run: dict, instance: dict, reason: str, result: dict, *, dispatch: bool,
        accepted: bool = False, decision_at: int | None = None,
    ) -> None:
        with storage.connect(self.store.path) as conn, storage.transaction(conn):
            current = self._current(conn, run)
            if current is None:
                return
            now = self.store.now()
            shutdown.settle_early(conn, instance, now, stop_run=current if accepted else None, inspect_inflight=True)
            decision_at = now if decision_at is None else decision_at
            conn.execute(
                """UPDATE task_runs AS q SET state='canceled',stopped=1,updated_at=:now,
                       reason='本轮已关机，跳过已到期的开机尝试',
                       result=json_set(q.result,'$.stop_run_id',:stop_run_id)
                   WHERE q.project_id=:project_id AND q.phase IS NULL AND q.state IN ('queued','waiting')
                     AND q.scheduled_at<=:cutoff
                     AND (q.task_id IS NOT NULL OR q.created_at<:cutoff OR
                         (q.created_at=:cutoff AND q.rowid<(SELECT rowid FROM task_runs WHERE id=:stop_run_id)))
                     AND (json_extract(q.snapshot,'$.kind')='start'
                         OR (json_extract(q.snapshot,'$.kind')='acquire' AND
                             json_extract(q.result,'$.pinned_instance_id')=:instance_id))
                     AND COALESCE(json_extract(q.snapshot,'$.config.instance_id'),
                                  json_extract(q.result,'$.pinned_instance_id'))=:instance_id
                     AND COALESCE(json_extract(q.snapshot,'$.config.region'),json_extract(q.result,'$.region'))=:region
                     AND COALESCE(json_extract(q.snapshot,'$.config.zone'),json_extract(q.result,'$.zone'))=:zone""",
                {"now": now, "cutoff": decision_at, "project_id": run["project_id"], "stop_run_id": run["id"],
                 "instance_id": instance["id"], "region": instance["region"], "zone": instance["zone"]},
            )
            self._write_transition(run=current, conn=conn, state="succeeded", reason=reason, result=result,
                                   notify=dispatch, policy_event="instance_stopped" if accepted else "operation_review")

    def _stop(self, run: dict) -> None:
        instance = self._target(run)
        if instance is None:
            self._transition(run, "failed", "指定实例未在项目和可用区中找到")
            return
        result = self._instance_result(instance, "stopped")
        if instance["state"] == "Stopped":
            self._complete_stop(run, instance, "实例已关机", result | {"disposition": "already_stopped"}, dispatch=False)
        elif instance["state"] == "Running":
            if run["snapshot"].get("trigger", {}).get("automation_rule_id"):
                reason = automation_rules.check_stop(self.store.path, run, instance, self.cloud, self.store.now())
                if reason:
                    self._transition(run, "canceled", reason, result)
                    return
            self._perform(run, "stop", result | {"previous_start_time": instance.get("start_time")},
                          lambda: self.cloud.stop(instance), "verify_stop")
        else:
            self._transition(run, "waiting", "实例操作状态待核对", result)

    def _reboot(self, run: dict) -> None:
        instance = self._target(run)
        if instance is None:
            self._transition(run, "failed", "指定实例未在项目和可用区中找到")
        elif instance["state"] != "Running":
            self._transition(run, "failed", "仅运行中的实例可以重启", self._instance_result(instance, "pending"))
        else:
            result = self._instance_result(instance, "pending") | {"previous_start_time": instance.get("start_time")}
            self._perform(run, "reboot", result, lambda: self.cloud.reboot(instance), "verify_reboot")

    def _rename(self, run: dict) -> None:
        instance = self._target(run)
        if instance is None:
            self._transition(run, "failed", "指定实例未在项目和可用区中找到")
            return
        config = run["snapshot"]["config"]
        name = config["name"]
        current_name = instance.get("name")
        result = self._instance_result(instance, "pending") | {"previous_name": current_name, "name": name}
        if current_name == name:
            self._transition(run, "succeeded", "实例已经使用此名称", result | {"disposition": "unchanged"})
        elif current_name != config["expected_name"]:
            self._transition(run, "failed", "实例名称已变化，请刷新后重新确认", result)
        else:
            self._perform(run, "rename", result, lambda: self.cloud.rename(instance, name), "verify_rename")

    def _terminate(self, run: dict) -> None:
        instance = self._target(run)
        if instance is None:
            self._transition(run, "failed", "指定实例未在项目和可用区中找到")
            return
        result = self._instance_result(instance, "pending")
        try:
            instance_release.check_target(
                instance, self.release_charge_types, run["snapshot"]["config"]["expected_instance"],
            )
        except ValueError as error:
            self._transition(run, "failed", str(error), result)
            return
        result["last_instance"] = instance_history.projection(instance)
        self._perform(run, "terminate", result, lambda: self.cloud.terminate(instance), "verify_terminate")

    def _resize(self, run: dict) -> None:
        instance = self._target(run)
        config = run["snapshot"]["config"]
        target = config["target"]
        try:
            if instance is None or instance.get("state") != "Stopped":
                raise ValueError("实例需处于关机状态，请重新询价")
            if instance_resize.baseline(instance | {"project_id": run["project_id"]}) != config["expected"]:
                raise ValueError("实例配置已变化，请重新询价")
            price = self.cloud.resize_quote(instance, target)
            if price != Decimal(config["amount"]):
                raise ValueError("改配报价已变化，请重新询价并确认")
            if self.cloud.capacity(config["expected"] | target) is not True:
                raise ValueError("目标规格库存待确认，请重新询价")
        except (ValueError, CloudError) as error:
            self._transition(run, "failed", str(error))
            return
        result = self._instance_result(instance, "pending") | {
            "resize_target": target, "resize_before": {key: instance[key] for key in instance_resize.COMPUTE},
            "resize_price": config["amount"],
        }
        self._perform(run, "resize", result, lambda: self.cloud.resize_instance(instance, target), "verify_resize")

    def _acquire(self, run: dict) -> None:
        config = run["snapshot"]["config"]
        quoted_purchase = "instance_quote_id" in run["snapshot"]
        single_purchase = _single_purchase(run)
        unavailable = "failed" if single_purchase else "waiting"
        specs = list(self._specs(run))
        if config["skip_if_match"] and specs and self._find_existing(run, specs):
            return
        unknown = False
        checked = False
        invalid_reason = None
        quote_issue = None
        if config.get("cpu_auto"):
            candidates = []
            with storage.connect(self.store.path) as conn:
                for spec in specs:
                    try:
                        candidates.extend(platform_candidates(conn, spec))
                    except ValueError as error:
                        invalid_reason = str(error)
            specs = candidates
        for spec in specs:
            try:
                with storage.connect(self.store.path) as conn:
                    verify_purchase_spec(conn, run["project_id"], spec)
            except ValueError as error:
                invalid_reason = str(error)
                continue
            checked = True
            capacity = self.cloud.capacity(spec)
            if capacity is not True:
                unknown = unknown or capacity is None
                continue
            try:
                quoted = self.cloud.quote(spec)
            except CloudError as error:
                if error.code != "quote_unverified":
                    raise
                reason = "报价无法核实，请重新询价" if quoted_purchase else "报价无法核实，暂不提交采购"
                if spec.get("cpu_auto") and not spec.get("cpu_locked"):
                    quote_issue = (reason, {"cloud_code": error.code})
                    continue
                self._transition(run, unavailable, reason, {"cloud_code": error.code})
                return
            quote = {"quote": str(quoted), "quoted_at": self.store.now(), "quote_request_id": getattr(self.cloud, "last_request_id", None)}
            if not quoted.is_finite() or quoted < 0 or quoted > Decimal(config["max_price"]):
                reason = ("报价高于已确认金额或无法核实，请重新询价" if quoted_purchase
                          else "报价超出预算或无法核实，暂不提交采购")
                if spec.get("cpu_auto") and not spec.get("cpu_locked"):
                    quote_issue = (reason, quote)
                    continue
                self._transition(run, unavailable, reason, quote)
                return
            rechecked = self.cloud.capacity(spec)
            if rechecked is not True:
                unknown = unknown or rechecked is None
                continue
            if spec.get("shared_storage") and not shared_storage.ensure_storage(self, run, spec):
                return
            decision_at = self.store.now()
            result = quote | {
                "spec": spec, "region": spec["region"], "zone": spec["zone"], "gpu_model": spec["gpu_model"],
                "remark": run["id"], "submitted_name": create_instance_name(run["id"]),
                "decision_at": decision_at, "creation_readback_deadline_at": decision_at + INSTANCE_READBACK_SECONDS,
            }
            status = self._perform(
                run, "create", result, lambda: self.cloud.create(spec, run["id"]), "verify_create"
            )
            if status == "226604" and not single_purchase and "purchase_retry" not in run["snapshot"]:
                # A documented rejection released the reservation. Check the remaining allowed zones.
                run = self.store.get_run(run["id"])
                if run["state"] in {"queued", "waiting"}:
                    continue
            return
        if quote_issue is not None:
            self._transition(run, unavailable, *quote_issue)
            return
        if not checked:
            state = "manual_review" if run["task_id"] is not None else "failed"
            self._transition(run, state, invalid_reason or "没有已核实的可用区，请重新预览", {"task_config_issue": True})
            return
        reason = "目标规格当前缺货"
        if unknown:
            reason = "无法核实目标规格的库存" if single_purchase else "部分目标规格库存未知"
        self._transition(run, unavailable, reason)

    def _find_existing(self, run: dict, specs: list[dict]) -> bool:
        zones = [{key: spec[key] for key in ("region", "zone")} for spec in specs]
        allowed = {(zone["region"], zone["zone"]): index for index, zone in enumerate(zones)}
        instances = self.cloud.instances(run["project_id"], zones)
        if any((item.get("region"), item.get("zone")) not in allowed for item in instances):
            self._transition(run, "waiting", "实例清单存在缺失或不一致的可用区，等待重新核对")
            return True
        matches = []
        for item in instances:
            spec = specs[allowed[item["region"], item["zone"]]]
            match = match_spec(item, spec)
            attachment = spec.get("shared_storage")
            if match is True and attachment:
                match = (shared_storage.attachment_status(self, item, spec, run)
                         if attachment["mode"] in {"cfs_attach", "us3"} else False)
            matches.append((item, match))
        pinned = run["result"].get("pinned_instance_id")
        if pinned:
            candidate = next((item for item, status in matches if item["id"] == pinned and status is True), None)
            if candidate is None:
                self._transition(run, "waiting", "已选复用实例待重新核对，暂缓采购")
            else:
                self._start_instance(run, candidate)
            return True
        exact = sorted(
            (item for item, match in matches if match is True),
            key=lambda item: (allowed[item["region"], item["zone"]], item["id"]),
        )
        for item in exact:
            if self._has_gpu(item):
                self._complete_instance(run, item, "reused", track_shutdown=True)
                return True
        unconfirmed = next((item for item in exact if item["state"] in {"Running", "Starting"}), None)
        if unconfirmed:
            self._transition(run, "waiting", "匹配实例正在启动或有卡待确认", self._instance_result(unconfirmed, "pending"))
            return True
        if any(match is None for _, match in matches) or any(item["state"] != "Stopped" for item in exact):
            self._transition(run, "waiting", "可能匹配的实例规格或操作状态待核对，暂缓采购")
            return True
        if exact:
            candidate = exact[0]
            result = self._instance_result(candidate, "pending") | {"pinned_instance_id": candidate["id"]}
            self._start_stopped(run, candidate, result)
            return True
        return False

    def _perform(self, run: dict, action: str, result: dict, call, phase: str) -> str | None:
        claim = self._claim(run, action, result, phase)
        if claim is None:
            return None
        run, operation_id = claim
        with storage.connect(self.store.path) as conn:
            latest = self._current(conn, run)
            paused = storage.get_settings(conn)["restore_gate"]
            stop_block = (
                self._automation_stop_block(conn, latest, self.store.now()) if latest and action == "stop" else None
            )
            release_block = bool(latest and action == "terminate" and any(instance_release.blockers(
                conn, run["project_id"], result["instance_id"], exclude_run_id=run["id"],
            ).values()))
            invalid_purchase = None
            if latest is not None and action == "create":
                try:
                    verify_purchase_spec(conn, run["project_id"], result["spec"])
                except ValueError as error:
                    invalid_purchase = str(error)
        if latest is None:
            return None
        single_purchase = _single_purchase(run)
        blocked = None
        if latest["stopped"]:
            blocked = ("canceled", "发送前确认任务已停止")
        elif paused:
            blocked = ("failed" if single_purchase else "waiting", "发送前确认云端写操作已暂停")
        elif _expired(latest, self.store.now()):
            blocked = ("expired", "确认的报价已过期，请重新询价" if "instance_quote_id" in run["snapshot"]
                       else "发送前确认执行窗口已结束")
            if shutdown_registration_closed(latest, self.store.now()):
                blocked = ("expired", SHUTDOWN_CUTOFF)
        elif invalid_purchase:
            blocked = ("failed", invalid_purchase)
        elif stop_block:
            blocked = ("canceled", stop_block)
        elif release_block:
            blocked = ("failed", "存在关联的启用任务、规则或待核对操作，请重新预览释放")
        if blocked:
            state, reason = blocked
            self._operation_result(run, operation_id, "not_dispatched", state, reason,
                                   {"shutdown_cutoff_reached": True} if reason == SHUTDOWN_CUTOFF else {}, None)
            return None
        try:
            response = call()
            if action in {"terminate", "resize"} and response != result["instance_id"]:
                raise CloudError("invalid_response", uncertain=True, request_id=getattr(self.cloud, "last_request_id", None))
        except CloudError as error:
            lock_conflict = error.code == "InstanceOperationInProgress"
            details = {"cloud_code": error.code, "cloud_request_id": error.request_id}
            if error.uncertain or (lock_conflict and phase not in INSTANCE_CHANGE_PHASES):
                self._operation_result(run, operation_id, "reserved", "reconciling", str(error), details, phase)
            else:
                can_retry = error.retryable and not (lock_conflict or single_purchase or action in {"terminate", "resize"})
                state = "waiting" if can_retry else "failed"
                reason = str(error)
                if (
                    run["kind"] == "start" and error.code == "226604"
                    and "capacity_retry" in run["snapshot"]["config"]
                ):
                    state = "waiting"
                    details.update(
                        capacity_wait_since=run["result"].get("capacity_wait_since", self.store.now()),
                        capacity_attempts=run["result"].get("capacity_attempts", 0) + 1,
                    )
                elif action == "cancel_shutdown":
                    state = "waiting" if error.retryable else "manual_review"
                    reason = "撤销待确认，" + reason
                self._operation_result(run, operation_id, "rejected", state, reason, details, None)
                if action == "set_shutdown":
                    shutdown.report_set_error(self, run["id"], reason)
            return error.code
        result = {"cloud_request_id": getattr(self.cloud, "last_request_id", None), "cloud_code": None}
        if action == "create":
            result["submitted_ids"] = response
        elif action in {"terminate", "resize"}:
            result["acknowledged_instance_id"] = response
        self._operation_result(run, operation_id, "accepted", "reconciling", "云端已受理，等待回读", result, phase)
        return None

    @staticmethod
    def _automation_stop_block(conn, run: dict, now: int) -> str | None:
        if not run["snapshot"].get("trigger", {}).get("automation_rule_id"):
            return None
        return automation_rules.stop_block(conn, run, now)

    def _claim(self, run: dict, action: str, result: dict, phase: str) -> tuple[dict, int] | None:
        now = self.store.now()
        with storage.connect(self.store.path) as conn, storage.transaction(conn):
            current = self._current(conn, run)
            if current is None or current["stopped"]:
                return None
            if self.read_only or storage.get_settings(conn)["restore_gate"]:
                return None
            if _expired(current, now):
                if shutdown_registration_closed(current, now):
                    self._write_transition(conn, current, "expired", SHUTDOWN_CUTOFF, {"shutdown_cutoff_reached": True})
                return None
            if action == "stop" and (reason := self._automation_stop_block(conn, current, now)):
                self._write_transition(conn, current, "canceled", reason, result)
                return None
            fingerprint = None
            if action == "create":
                single_purchase = _single_purchase(current)
                spec = result["spec"]
                fingerprint = encode({
                    **{key: value.casefold() if isinstance(value, str) else value
                       for key, value in spec.items() if key in SPEC_FIELDS and key != "cpu_platform"},
                    "disks": disk_key(spec["disks"]),
                })
                blocked = conn.execute(
                    """SELECT 1 FROM task_operations o JOIN task_runs r ON r.id=o.run_id
                       WHERE o.project_id=? AND o.fingerprint=? AND o.run_id!=?
                         AND (o.state='reserved' OR (o.state='accepted' AND r.phase IN ('verify_create','manual_bind')))""",
                    (current["project_id"], fingerprint, current["id"]),
                ).fetchone()
                if blocked:
                    state = "failed" if single_purchase else "waiting"
                    reason = "同项目同规格有未决采购，请先核对" if single_purchase else "同项目同规格有未决采购，等待核对"
                    self._write_transition(conn, current, state, reason, {})
                    return None
                if self._purchase_limit_reached(conn, current):
                    reason = "此申请已经尝试创建，请核对运行记录" if single_purchase else "已达到任务总购买次数上限"
                    self._write_transition(conn, current, "failed", reason, {})
                    return None
            else:
                if action == "terminate" and any(instance_release.blockers(
                    conn, current["project_id"], result["instance_id"], exclude_run_id=current["id"],
                ).values()):
                    self._write_transition(conn, current, "failed", "存在关联的启用任务、规则或待核对操作，请重新预览释放", result)
                    return None
                if action != "terminate" and instance_release.has_pending_termination(conn, current["project_id"], result["instance_id"]):
                    self._write_transition(conn, current, "failed", "此实例有待完成的释放操作", result)
                    return None
                if action != "resize" and "instance_id" in result and instance_resize.pending(
                    conn, current["project_id"], result["instance_id"],
                ):
                    self._write_transition(conn, current, "waiting", "等待实例改配回读", {})
                    return None
                same_instance = conn.execute(
                    f"""SELECT 1 FROM task_operations o JOIN task_runs r ON r.id=o.run_id
                       WHERE o.project_id=:project_id AND o.run_id!=:run_id AND o.state IN ('reserved','accepted')
                         AND ((r.state='reconciling' AND r.phase IS NOT NULL
                               AND (:action!='stop' OR r.phase NOT IN ('verify_set_shutdown','verify_cancel_shutdown')))
                              OR (r.state='manual_review' AND r.phase IN {INSTANCE_RECHECK_SQL}))
                         AND (json_extract(o.data,'$.instance_id')=:instance_id OR
                            (json_type(o.data,'$.submitted_ids')='array' AND EXISTS (
                                SELECT 1 FROM json_each(o.data,'$.submitted_ids') AS submitted
                                WHERE submitted.type='text' AND submitted.value=:instance_id
                            )))
                       LIMIT 1""",
                    {"project_id": current["project_id"], "run_id": current["id"], "instance_id": result["instance_id"],
                     "action": action},
                ).fetchone()
                if same_instance:
                    self._write_transition(conn, current, "waiting", "此实例仍有待回读的操作", {})
                    return None
                if action == "rename" and conn.execute(
                    """SELECT 1 FROM task_operations o JOIN task_runs r ON r.id=o.run_id
                       WHERE o.project_id=? AND o.action='create' AND o.state IN ('reserved','accepted')
                         AND r.state IN ('reconciling','manual_review') AND r.phase IN ('verify_create','manual_bind')
                         AND json_extract(o.data,'$.submitted_name') IN (?,?) LIMIT 1""",
                    (current["project_id"], result["previous_name"], result["name"]),
                ).fetchone():
                    self._write_transition(conn, current, "failed", "此名称关联待核对的采购申请，请先核对采购结果", {})
                    return None
            cursor = conn.execute(
                """INSERT INTO task_operations(run_id,project_id,action,state,fingerprint,data,created_at,updated_at)
                     VALUES (?,?,?,'reserved',?,?,?,?)""",
                (current["id"], current["project_id"], action, fingerprint, encode(result), now, now),
            )
            self._write_transition(conn, current, "reconciling", "已保存云端操作意图", result, phase=phase)
            updated = conn.execute("SELECT * FROM task_runs WHERE id=?", (current["id"],)).fetchone()
            return run_view(updated), cursor.lastrowid

    def _purchase_limit_reached(self, conn, run: dict) -> bool:
        if _single_purchase(run):
            return conn.execute(
                "SELECT 1 FROM task_operations WHERE run_id=? AND action='create'", (run["id"],),
            ).fetchone() is not None
        if run["snapshot"].get("purchase_retry") is True:
            return conn.execute(
                """SELECT 1 FROM task_operations WHERE run_id=? AND action='create'
                   AND state IN ('reserved','accepted')""", (run["id"],),
            ).fetchone() is not None
        claimed = conn.execute(
            """SELECT count(*) FROM task_operations o JOIN task_runs r ON r.id=o.run_id
               WHERE r.task_id=? AND o.action='create' AND o.state IN ('reserved','accepted')""",
            (run["task_id"],),
        ).fetchone()[0]
        adjustment = conn.execute(
            "SELECT adjustment FROM recovery_purchases WHERE task_id=?", (run["task_id"],),
        ).fetchone()
        return claimed + (adjustment["adjustment"] if adjustment else 0) >= run["snapshot"]["config"]["purchase_limit"]

    def _operation_result(
        self, run: dict, operation_id: int, op_state: str, state: str, reason: str,
        result: dict, phase: str | None,
    ) -> None:
        with storage.connect(self.store.path) as conn, storage.transaction(conn):
            op = conn.execute("SELECT * FROM task_operations WHERE id=?", (operation_id,)).fetchone()
            current = self._current(conn, run)
            if current is None or op["state"] != "reserved":
                return
            conn.execute(
                """UPDATE task_operations SET state=?,request_id=?,cloud_code=?,data=?,updated_at=? WHERE id=?""",
                (op_state, result.get("cloud_request_id"), result.get("cloud_code"),
                 encode(json.loads(op["data"]) | result), self.store.now(), operation_id),
            )
            self._write_transition(conn, current, state, reason, result, phase=phase)

    def _last_operation(self, run: dict) -> dict:
        with storage.connect(self.store.path) as conn:
            row = conn.execute(
                "SELECT * FROM task_operations WHERE run_id=? ORDER BY id DESC LIMIT 1", (run["id"],)
            ).fetchone()
        return dict(row) | {"data": json.loads(row["data"])}

    def _reconcile(self, run: dict) -> None:
        if run["phase"] in {"verify_create", "manual_bind"}:
            self._verify_create(run)
        elif run["phase"] == "verify_cfs":
            shared_storage.reconcile_storage(self, run)
        elif run["phase"] in INSTANCE_RECHECK_PHASES:
            self._verify_instance(run)
        elif run["phase"] in {"verify_set_shutdown", "verify_cancel_shutdown"}:
            shutdown.verify_shutdown(self, run)
        elif run["phase"] == "review_cancel_shutdown":
            shutdown.review_cancel_shutdown(self, run)
        elif run["phase"] == "review_shutdown":
            shutdown.review_shutdown(self, run)

    def _verify_create(self, run: dict) -> None:
        op = self._last_operation(run)
        manual = run["result"].get("instance_recheck_pending") is True

        def reviewed(detail: str) -> dict:
            return {"instance_recheck_pending": False, "instance_review": {
                "checked_at": self.store.now(), "detail": detail,
            }} if manual else {}

        def transition(state: str, reason: str, result: dict | None = None) -> None:
            self._transition(run, state, reason, (result or {}) | reviewed(reason), phase=run["phase"])

        spec = op["data"].get("spec")
        if not isinstance(spec, dict):
            transition("manual_review", "缺少已提交的采购规格，请核对", {"reviewable": True})
            return
        deadline = op["data"].get("creation_readback_deadline_at")
        expired = type(deadline) is int and self.store.now() >= deadline
        pending_state = "manual_review" if expired or manual else "reconciling"
        manual_id = run["result"].get("manual_instance_id") if run["phase"] == "manual_bind" else None
        bound_id = op["data"].get("instance_id")
        if (manual_id and conflicts_with_create_receipt(op["data"], manual_id)) or (
            bound_id and conflicts_with_create_receipt(op["data"], bound_id)
        ):
            transition("manual_review", "绑定实例与采购回执不一致，请核对")
            return
        known_ids = op["data"].get("submitted_ids") or ([bound_id] if bound_id else [])
        if self.cloud is None:
            transition(pending_state, "等待平台凭据以回读已提交的采购", {"reviewable": True})
            return
        try:
            instances = self.cloud.instances(run["project_id"])
        except CloudError as error:
            if pending_state == "reconciling":
                raise
            transition("manual_review", "当前无法回读实例，需核对已提交的采购", {
                "reviewable": True, "cloud_code": error.code, "cloud_request_id": error.request_id,
            })
            return
        if manual_id:
            matches = [item for item in instances if item["id"] == manual_id
                       and self._matches_purchase(run, op, item, spec, known_ids)]
            if not matches:
                transition("manual_review", "手工绑定的实例缺少本次采购归属证据")
                return
        else:
            submitted_name = op["data"].get("submitted_name")
            markers = [
                item for item in instances if item.get("remark") == run["id"]
                or (submitted_name and item.get("name") == submitted_name)
            ]
            matches = [item for item in instances if item["id"] in known_ids] if known_ids else markers
            if len(markers) > 1 or (matches and markers and matches[0]["id"] != markers[0]["id"]):
                transition("manual_review", "运行编号对应多个或冲突的云端实例")
                return
            if matches and not known_ids:
                matches = [item for item in matches if self._matches_purchase(run, op, item, spec, known_ids)]

        if len(matches) != 1:
            state = "manual_review" if len(matches) > 1 else pending_state
            reason = "实例回读窗口已结束，采购额度继续预留" if expired else "申请结果待核对，采购额度继续预留"
            transition(state, reason, {"reviewable": True})
            return
        instance = matches[0]
        if not self._bind_create(run, op, instance):
            return
        if self._has_gpu(instance, op["created_at"]):
            readback = reviewed("已回读到新建实例有卡运行")
            if spec.get("shared_storage"):
                auto_wait = pending_state == "reconciling"
                attached = shared_storage.verify_storage(self, run, spec, instance["id"], instance=instance,
                                                        report_pending=auto_wait)
                if not attached and auto_wait:
                    return
                readback["storage_state"] = "attached" if attached else "attachment_unverified"
            self._complete_instance(run, instance, "created", track_shutdown=True, readback=readback)
        else:
            reason = "已确认购买实例，回读窗口已结束，有卡仍待核对" if expired else "已确认购买实例，有卡待确认"
            transition(pending_state, reason, self._instance_result(instance, "created") | {"reviewable": True})

    def _matches_purchase(self, run: dict, op: dict, item: dict, spec: dict, known_ids: list[str]) -> bool:
        if (item.get("project_id"), item.get("region"), item.get("zone")) != (
            run["project_id"], spec["region"], spec["zone"],
        ) or match_spec(item, spec) is not True:
            return False
        if known_ids:
            return item["id"] in known_ids
        created = item.get("create_time")
        if item.get("remark") == run["id"]:
            return created is None or type(created) is int and op["created_at"] <= created <= self.store.now()
        submitted_name = op["data"].get("submitted_name")
        return (
            bool(submitted_name) and not item.get("remark") and item.get("name") == submitted_name
            and type(created) is int and op["created_at"] <= created <= self.store.now()
        )

    def _bind_create(self, run: dict, op: dict, instance: dict) -> bool:
        with storage.connect(self.store.path) as conn, storage.transaction(conn):
            current = self._current(conn, run)
            if current is None:
                return False
            if conn.execute(
                """SELECT 1 FROM task_operations o WHERE o.project_id=:project_id
                     AND o.action='create' AND o.state='accepted' AND o.run_id!=:run_id
                     AND (json_extract(o.data,'$.instance_id')=:instance_id OR EXISTS (
                          SELECT 1 FROM json_each(o.data,'$.submitted_ids') WHERE value=:instance_id))
                     LIMIT 1""",
                {"project_id": run["project_id"], "run_id": run["id"], "instance_id": instance["id"]},
            ).fetchone():
                self._write_transition(conn, current, "manual_review", "实例已归属另一笔采购，继续保留待核对记录",
                                       {"instance_recheck_pending": False, "instance_review": {
                                           "checked_at": self.store.now(), "detail": "实例已归属另一笔采购",
                                       }}, phase=run["phase"])
                return False
            data = op["data"] | {"instance_id": instance["id"]}
            conn.execute(
                "UPDATE task_operations SET state='accepted',data=?,updated_at=? WHERE id=?",
                (encode(data), self.store.now(), op["id"]),
            )
            return True

    def _verify_instance(self, run: dict) -> None:
        op = self._last_operation(run)
        phase = run["phase"]
        manual = bool(run["result"].get("instance_recheck_pending"))
        label = {"verify_start": "开机", "verify_stop": "关机", "verify_reboot": "重启", "verify_rename": "重命名",
                 "verify_terminate": "释放", "verify_resize": "改配"}[phase]
        deadline = op["created_at"] + INSTANCE_READBACK_SECONDS
        if not manual and self.store.now() >= deadline:
            self._transition(run, "manual_review", f"{label}结果待核对，已到自动回读截止时间", phase=phase)
            return

        detail = "未找到原项目与可用区中的实例" if self.cloud is not None else "平台凭据未配置"
        inventory_complete = False
        try:
            instance = self._target(run, op["data"]) if self.cloud is not None else None
            inventory_complete = self.cloud is not None
        except CloudError as error:
            instance = None
            detail = str(error)

        now = self.store.now()
        evidence = {"checked_at": now}
        result = {"instance_review": evidence, "instance_recheck_pending": False}
        if phase == "verify_terminate" and inventory_complete and instance is None:
            if op["state"] == "accepted" and op["data"].get("acknowledged_instance_id") == op["data"]["instance_id"]:
                evidence["detail"] = "平台已受理释放，完整回读确认实例已从活动清单移除"
                with storage.connect(self.store.path) as conn, storage.transaction(conn):
                    current = self._current(conn, run)
                    if current is not None:
                        target = op["data"]
                        record = target.get("last_instance") or current["snapshot"]["config"]["expected_instance"]
                        instance_history.save(conn, record | {"id": target["instance_id"], "project_id": run["project_id"]},
                                              "released", now, run["id"])
                        conn.execute("INSERT OR IGNORE INTO refresh_requests VALUES ('instances',?,?)",
                                     (run["project_id"], now))
                        conn.execute("INSERT OR IGNORE INTO refresh_requests VALUES ('storage',?,?)",
                                     (run["project_id"], now))
                        shutdown.retire_appointments(conn, run["project_id"], target, "released", now)
                        self._write_transition(conn, current, "succeeded", evidence["detail"],
                                               result | {"disposition": "released"}, policy_event="instance_released")
                return
            detail = "实例未列在完整清单中，缺少确认释放的云端回执"
        if instance is not None:
            state = instance["state"]
            start_time = instance.get("start_time")
            evidence.update(state=state, name=instance.get("name"), start_time=start_time, stop_time=instance.get("stop_time"))
            if phase == "verify_stop" and state == "Stopped":
                accepted = op["state"] == "accepted"
                evidence["detail"] = "云端已确认关机" if accepted else "已回读到实例关机，关机请求的归属待核对"
                self._complete_stop(run, instance, evidence["detail"], result | self._instance_result(instance, "stopped"),
                                    dispatch=True, accepted=accepted, decision_at=op["created_at"])
                return
            cardless_boot = op["data"].get("boot_mode") == "without_gpu"
            verify_new_boot = cardless_boot or run["snapshot"]["config"].get("shutdown_after_seconds") is not None
            previous_start = op["data"].get("previous_start_time")
            new_boot = (type(start_time) is int and op["created_at"] <= start_time <= now
                        and (type(previous_start) is not int or start_time > previous_start))
            if phase == "verify_start":
                if cardless_boot:
                    matched_mode = (state == "Running" and instance.get("gpu_mode") == "without_gpu" and
                                    (instance.get("cpu"), instance.get("memory_gb")) ==
                                    (op["data"]["expected_cpu"], op["data"]["expected_memory_gb"]))
                else:
                    matched_mode = self._has_gpu(instance, op["created_at"])
                evidence["boot_mode"] = instance.get("gpu_mode")
            if phase == "verify_start" and matched_mode and (new_boot or not verify_new_boot):
                accepted = op["state"] == "accepted"
                disposition = "started" if accepted else (
                    "reused" if op["cloud_code"] == "InstanceOperationInProgress" else "observed"
                )
                evidence["detail"] = "已回读到实例无卡运行且实配与目标档位一致" if cardless_boot else "已回读到实例有卡运行"
                self._complete_instance(run, instance, disposition, track_shutdown=accepted and not manual, readback=result)
                return
            if phase == "verify_resize" and state == "Stopped" and all(
                instance.get(key) == value for key, value in op["data"]["resize_target"].items()
            ):
                accepted = op["state"] == "accepted"
                evidence["detail"] = "云端已回读到目标规格" if accepted else "已回读到目标规格，改配请求归属待核对"
                with storage.connect(self.store.path) as conn, storage.transaction(conn):
                    current = self._current(conn, run)
                    if current is not None:
                        conn.execute("INSERT OR IGNORE INTO refresh_requests VALUES ('instances',?,?)",
                                     (run["project_id"], now))
                        self._write_transition(conn, current, "succeeded", evidence["detail"],
                                               result | {"disposition": "resized" if accepted else "resize_observed"})
                return
            if phase == "verify_rename" and instance.get("name") == op["data"]["name"]:
                evidence["detail"] = "已回读到新的实例名称"
                self._transition(run, "succeeded", evidence["detail"], result | {"disposition": "renamed"})
                return
            if phase == "verify_reboot":
                previous_start = op["data"].get("previous_start_time")
                updated_start = (
                    type(start_time) is int and op["created_at"] <= start_time <= now
                    and (start_time > previous_start if type(previous_start) is int else start_time > op["created_at"])
                )
                if state == "Running" and (run["result"].get("observed_rebooting") or updated_start):
                    evidence["detail"] = "已回读到实例重启完成"
                    self._transition(run, "succeeded", evidence["detail"], result | {
                        "disposition": "rebooted", "start_time": start_time,
                    }, policy_event="instance_rebooted" if op["state"] == "accepted" else None)
                    return
                if state == "Rebooting":
                    result["observed_rebooting"] = True
            if op["cloud_code"] == "InstanceOperationInProgress" and (
                (phase == "verify_start" and state == "Stopped") or (phase == "verify_stop" and state == "Running")
            ):
                # The cloud explicitly rejected the operation and its original state is stable again.
                evidence["detail"] = "云端拒绝了该操作，实例已回到操作前的状态"
                with storage.connect(self.store.path) as conn, storage.transaction(conn):
                    current = self._current(conn, run)
                    if current is not None:
                        conn.execute("UPDATE task_operations SET state='rejected' WHERE id=?", (op["id"],))
                        self._write_transition(conn, current, "failed", evidence["detail"], result)
                return
            detail = ("实例已运行，目标启动模式或本次启动时间待确认" if phase == "verify_start" and state == "Running"
                      else "当前实例状态尚未确认操作完成")

        evidence["detail"] = detail
        state = "manual_review" if manual or self.store.now() >= deadline else "reconciling"
        reason = f"{label}结果待核对，{detail}" if state == "manual_review" else detail
        self._transition(run, state, reason, result, phase=phase)
