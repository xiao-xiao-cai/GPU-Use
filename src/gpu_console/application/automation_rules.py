"""Evaluate instance stop rules and preserve evidence for guarded dispatch."""

import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from sqlite3 import Connection, Row

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError, CompShare
from gpu_console.application.idle_notices import MAX_SAMPLE_GAP, SAMPLE_FRESHNESS, complete_cards
from gpu_console.application.task_config import SHANGHAI, channel_list, validate_config
from gpu_console.application.tasks import encode, insert_run, require_row, termination_pending
from gpu_console.storage import cardless

METRICS = {
    "uhost_cpu_used": "cpu_threshold",
    "cloudwatch_gpu_util": "gpu_threshold",
    "cloudwatch_gpu_memory_usage": "vram_threshold",
}
RUNTIME_SAMPLE_SECONDS = 300
MISSING = "缺少连续、完整且新鲜的 CPU、GPU 或显存采样"
BUSY = "CPU、GPU 或显存负载达到阈值，空闲证据已中断"
CPU_MISSING = "缺少连续、完整且新鲜的 CPU 采样"
CPU_BUSY = "CPU 负载达到阈值，空闲证据已中断"
CARDLESS_ONLY = "仅适用于已确认无卡且档位已核实的运行，未执行停机"
GPU_RULE_UNAVAILABLE = "无卡运行不适用需要 GPU 采样的规则"
PAUSED = "自动化规则已暂停或更新"
MONITOR_UNAVAILABLE = "执行前监控读取失败，已取消此次自动停机"


class RuleTargetUnavailable(ValueError):
    """The bound instance cannot be verified from the current snapshot."""


def initialize(conn: Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS automation_rules (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            region TEXT NOT NULL, zone TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
            config TEXT NOT NULL, channels TEXT NOT NULL, status TEXT NOT NULL,
            version INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            armed_at INTEGER NOT NULL, evidence_after INTEGER NOT NULL, skip_due_before INTEGER NOT NULL,
            progress TEXT NOT NULL, evidence TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS automation_rules_target ON automation_rules(project_id,instance_id,status);
        CREATE INDEX IF NOT EXISTS task_runs_automation_rule
            ON task_runs(json_extract(snapshot,'$.trigger.automation_rule_id'))
            WHERE json_type(snapshot,'$.trigger.automation_rule_id')='text';
    """)


def _progress(state: str, reason: str, *, started_at: int | None = None, since_at: int | None = None,
              sampled_at: int | None = None, due_at: int | None = None) -> dict:
    return dict(state=state, reason=reason, started_at=started_at, since_at=since_at,
                sampled_at=sampled_at, due_at=due_at)


def _ready() -> dict:
    return _progress("waiting", "等待实例运行与完整监控采样")


def _get(conn: Connection, rule_id: str) -> Row:
    return require_row(conn.execute("SELECT * FROM automation_rules WHERE id=?", (rule_id,)).fetchone(), "自动化规则不存在")


def _view(row: Row, instance: dict | None, now: int) -> dict:
    result = {key: row[key] for key in (
        "id", "project_id", "instance_id", "region", "zone", "name", "kind", "status", "version",
        "created_at", "updated_at",
    )}
    result.update(config=json.loads(row["config"]), channels=json.loads(row["channels"]),
                  progress=json.loads(row["progress"]), warning=None)
    if (
        row["kind"] == "runtime_stop" and instance and instance.get("state") == "Running"
        and instance.get("gpu_mode") != "without_gpu"
        and result["progress"]["state"] in {"waiting", "counting", "unavailable"}
    ):
        started_at = instance.get("start_time")
        if (result["progress"]["state"] not in {"queued", "reminded", "handled"} and type(started_at) is int
                and 0 < started_at <= now - result["config"]["duration_minutes"] * 60):
            result["warning"] = "实例已达到运行时长上限，启用后会在下一次采集时检查本次启动"
    return result


def list_rules(
    conn: Connection, project_id: str | None, instance_id: str | None, now: int, *, include_archived: bool = False,
) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM automation_rules WHERE (? IS NULL OR project_id=?) AND (? IS NULL OR instance_id=?)
           AND (? OR status<>'archived') ORDER BY created_at DESC,id""",
        (project_id, project_id, instance_id, instance_id, include_archived),
    ).fetchall()
    instances = {
        key: {item["id"]: item for item in storage.snapshot(conn, "instances", key)["data"]}
        for key in {row["project_id"] for row in rows}
    }
    return [_view(row, instances[row["project_id"]].get(row["instance_id"]), now) for row in rows]


def get_rule(conn: Connection, project_id: str, rule_id: str, now: int) -> dict:
    row = _get(conn, rule_id)
    if row["project_id"] != project_id:
        raise KeyError("自动化规则不存在")
    snapshot = storage.snapshot(conn, "instances", project_id)
    instance = next((item for item in snapshot["data"] if _matches(row, item)), None)
    return _view(row, instance, now)


def _cancel_unsent(conn: Connection, rule_id: str, now: int, reason: str = PAUSED) -> None:
    conn.execute(
        """UPDATE task_runs SET state='canceled',stopped=1,reason=?,updated_at=?
           WHERE json_extract(snapshot,'$.trigger.automation_rule_id')=?
             AND json_type(snapshot,'$.trigger.automation_rule_id')='text'
             AND state IN ('queued','waiting') AND phase IS NULL""", (reason, now, rule_id),
    )


def save_bound_rule(conn: Connection, rule: dict, now: int, rule_id: str | None = None) -> dict:
    snapshot = storage.snapshot(conn, "instances", rule["project_id"])
    instance = next((
        item for item in snapshot["data"] if item.get("id") == rule["instance_id"]
        and item.get("project_id") == rule["project_id"]
    ), None)
    if instance is None and snapshot["success_at"] is not None and not snapshot["error"]:
        raise KeyError("项目快照中找不到该实例")
    if not fresh_instance_snapshot(conn, snapshot, now):
        raise RuleTargetUnavailable("实例清单待同步，请刷新实例后重试")
    if not instance.get("region") or not instance.get("zone"):
        raise ValueError("需要指定地域和可用区明确的实例")
    if termination_pending(conn, rule["project_id"], rule["instance_id"]):
        raise ValueError("实例存在待核对的释放操作")
    rule_config = rule["config"]
    scope = validate_config("stop", {
        "instance_id": rule["instance_id"], "region": instance["region"], "zone": instance["zone"],
        "email": rule_config.get("email"),
    })
    if "email" in scope:
        rule_config = rule_config | {"email": scope["email"]}
    rule = rule | {"config": rule_config, "channels": channel_list(rule["channels"], scope)}
    return save_rule(conn, rule, instance, now, rule_id)


def save_rule(conn: Connection, rule: dict, instance: dict, now: int, rule_id: str | None = None) -> dict:
    if rule_id is None:
        rule_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO automation_rules (id,project_id,instance_id,region,zone,name,kind,config,channels,status,
               version,created_at,updated_at,armed_at,evidence_after,skip_due_before,progress,evidence)
               VALUES (?,?,?,?,?,?,?,?,?,'active',1,?,?,?,0,0,?,'{}')""",
            (rule_id, rule["project_id"], rule["instance_id"], instance["region"], instance["zone"], rule["name"],
             rule["kind"], encode(rule["config"]), json.dumps(rule["channels"]), now, now, now, encode(_ready())),
        )
    else:
        existing = _get(conn, rule_id)
        if existing["status"] == "archived":
            raise ValueError("已删除的自动化规则无法修改")
        if any(existing[key] != rule[key] for key in ("project_id", "instance_id", "kind")) or any(
            existing[key] != instance[key] for key in ("region", "zone")
        ):
            raise ValueError("编辑规则不能改变规则类型或目标实例范围")
        conn.execute(
            """UPDATE automation_rules SET name=?,config=?,channels=?,version=version+1,updated_at=?,armed_at=?,
               evidence_after=0,progress=?,evidence='{}' WHERE id=?""",
            (rule["name"], encode(rule["config"]), json.dumps(rule["channels"]), now, now, encode(_ready()), rule_id),
        )
        _cancel_unsent(conn, rule_id, now)
    return _view(_get(conn, rule_id), instance, now)


def control_rule(conn: Connection, rule_id: str, action: str, now: int) -> dict:
    rule = _get(conn, rule_id)
    status = {"resume": "active", "pause": "paused", "archive": "archived"}[action]
    if rule["status"] == "archived" and status != "archived":
        raise ValueError("已删除的自动化规则无法恢复或暂停")
    snapshot = storage.snapshot(conn, "instances", rule["project_id"])
    instance = next((item for item in snapshot["data"] if _matches(rule, item)), None)
    if rule["status"] != status:
        if status == "active" and (not instance or not fresh_instance_snapshot(conn, snapshot, now)):
            raise ValueError("暂时无法核对原实例，请刷新后重试")
        if status == "active" and termination_pending(conn, rule["project_id"], rule["instance_id"]):
            raise ValueError("实例存在待核对的释放操作")
        reason = "规则已删除" if status == "archived" else "规则已暂停"
        progress = _ready() if status == "active" else _progress("waiting", reason)
        conn.execute(
            """UPDATE automation_rules SET status=?,version=version+1,updated_at=?,armed_at=?,evidence_after=0,
               progress=?,evidence='{}' WHERE id=?""", (status, now, now, encode(progress), rule_id),
        )
        _cancel_unsent(conn, rule_id, now, reason if status == "archived" else PAUSED)
    return _view(_get(conn, rule_id), instance, now)


def _matches(rule: Row, instance: dict) -> bool:
    return (instance.get("id") == rule["instance_id"] and all(
        instance.get(key) == rule[key] for key in ("project_id", "region", "zone")
    ))


def fresh_instance_snapshot(conn: Connection, snapshot: dict, now: int) -> bool:
    collected_at = snapshot["success_at"]
    freshness = max(SAMPLE_FRESHNESS, 2 * storage.get_settings(conn)["collection_seconds"])
    return snapshot["error"] is None and collected_at is not None and 0 <= now - collected_at <= freshness


def restore_boundary(conn: Connection, now: int) -> None:
    conn.execute(
        """UPDATE automation_rules SET evidence_after=?,skip_due_before=?,progress=?,evidence='{}'""",
        (now + 1, now, encode(_progress("waiting", "备份恢复后等待新的采样"))),
    )


def collection_failed(conn: Connection, project_id: str, now: int) -> None:
    conn.execute(
        """UPDATE automation_rules SET evidence_after=?,progress=?,evidence='{}'
           WHERE project_id=? AND status='active'""",
        (now + 1, encode(_progress("unavailable", "实例清单读取失败，已中断空闲计时")), project_id),
    )


def _interval(schedule: dict, now: int) -> tuple[int, int] | None:
    if schedule["mode"] == "once":
        return (schedule["start_at"], schedule["end_at"]) if schedule["start_at"] <= now < schedule["end_at"] else None
    today = datetime.fromtimestamp(now, SHANGHAI).date()
    active = []
    for date in (today - timedelta(days=1), today):
        for entry in schedule["windows"]:
            if entry["weekday"] != date.isoweekday():
                continue
            start = datetime.fromisoformat(f"{date}T{entry['start']}").replace(tzinfo=SHANGHAI)
            end = datetime.fromisoformat(f"{date}T{entry['end']}").replace(tzinfo=SHANGHAI)
            if end < start:
                end += timedelta(days=1)
            if start.timestamp() <= now < end.timestamp():
                active.append((int(start.timestamp()), int(end.timestamp())))
    return min(active, key=lambda interval: (interval[0], -interval[1])) if active else None


def _rule_metrics(kind: str) -> dict[str, str]:
    return {"uhost_cpu_used": "cpu_threshold"} if kind == "cardless_idle_stop" else METRICS


def _samples(instance: dict, points: list[dict], floor: int, now: int, metrics: dict) -> dict:
    result = {metric: defaultdict(dict) for metric in metrics}
    for point in points:
        metric, sampled_at = point["metric"], point["sampled_at"]
        if (metric in result and floor <= sampled_at <= now and point["project_id"] == instance["project_id"]
                and point["instance_id"] == instance["id"]):
            result[metric][sampled_at][point["label"]] = point["value"]
    return result


def _advance(instance: dict, points: list[dict], config: dict, previous: dict, floor: int, now: int,
             kind: str) -> tuple[dict, str | None]:
    metrics = _rule_metrics(kind)
    missing, busy = (CPU_MISSING, CPU_BUSY) if kind == "cardless_idle_stop" else (MISSING, BUSY)
    evidence = {}
    problem = None
    for metric, groups in _samples(instance, points, floor, now, metrics).items():
        saved = previous.get(metric, {})
        last_at = saved.get("sampled_at", 0)
        reset_at = saved.get("reset_at", 0)
        labels = saved.get("labels", [])
        since = saved.get("since_at")
        if since is not None and since < floor:
            since = None
        latest = max(groups, default=0)
        unavailable = not groups or now - latest > SAMPLE_FRESHNESS
        if unavailable or latest < max(last_at, reset_at):
            evidence[metric] = dict(sampled_at=last_at, labels=[], since_at=None, reset_at=now if unavailable else reset_at)
            problem = missing
            continue
        gpu = METRICS[metric] != "cpu_threshold"
        for sampled_at, values in sorted(groups.items()):
            if sampled_at < reset_at:
                continue
            signature = sorted(values)
            valid = (complete_cards(values, instance.get("gpu") or 0) if gpu else
                     signature == [""] and 0 <= values[""] <= 100)
            low = valid and max(values.values()) < config[metrics[metric]]
            if sampled_at < last_at:
                if since is not None and sampled_at >= since and (not low or signature != labels):
                    last_at, labels, since = sampled_at, signature, None
                    problem = busy if valid and not low else missing
                continue
            if not low:
                since = None
                problem = missing if not valid else busy
            elif since is None or sampled_at - last_at > MAX_SAMPLE_GAP or signature != labels:
                if sampled_at == last_at and since is None:
                    continue
                since = sampled_at
            last_at, labels = sampled_at, signature
        evidence[metric] = dict(sampled_at=last_at, labels=labels, since_at=since)
    return evidence, problem


def _overlap(evidence: dict, now: int, kind: str) -> tuple[int, int] | None:
    if evidence.keys() != _rule_metrics(kind).keys() or any(
        item["since_at"] is None or not 0 <= now - item["sampled_at"] <= SAMPLE_FRESHNESS
        for item in evidence.values()
    ):
        return None
    start = max(item["since_at"] for item in evidence.values())
    end = min(item["sampled_at"] for item in evidence.values())
    return (start, end) if start <= end else None


def _store_progress(conn: Connection, rule_id: str, progress: dict, evidence: dict) -> None:
    conn.execute("UPDATE automation_rules SET progress=?,evidence=? WHERE id=?",
                 (encode(progress), encode(evidence), rule_id))


def _runtime_notified(conn: Connection, run: dict, now: int, reason: str) -> None:
    snapshot = run["snapshot"]
    scope = snapshot["config"]
    conn.execute("UPDATE task_runs SET result=json_patch(result,?) WHERE id=?", (encode({
        "instance_id": scope["instance_id"], "region": scope["region"], "zone": scope["zone"],
        "automation_reminded": True,
    }), run["id"]))
    storage.emit(conn, run["id"] + ":result", {
        "title": snapshot["name"], "message": reason, "task_name": snapshot["name"], "run_id": run["id"],
        "instance_id": scope["instance_id"], "zone": scope["zone"], "project_id": run["project_id"],
        "confirmed_at": now, "recipient": scope.get("email"),
    }, snapshot["channels"])


def note_blocked_stop(conn: Connection, run: dict, state: str, reason: str, now: int) -> bool:
    """Invalidate contradicted evidence and record an unattempted runtime rule's reminder."""
    trigger = run["snapshot"].get("trigger", {})
    if state != "expired" and reason not in {MISSING, BUSY, CPU_MISSING, CPU_BUSY, CARDLESS_ONLY,
                                              GPU_RULE_UNAVAILABLE, MONITOR_UNAVAILABLE}:
        return False
    row = conn.execute("SELECT * FROM automation_rules WHERE id=?", (trigger["automation_rule_id"],)).fetchone()
    if not row or row["status"] != "active" or row["version"] != trigger["rule_version"]:
        return False
    progress = json.loads(row["progress"])
    if trigger["kind"] != "runtime_stop":
        if state == "canceled" and progress["started_at"] == trigger["started_at"]:
            conn.execute("UPDATE automation_rules SET evidence_after=?,progress=?,evidence='{}' WHERE id=?", (
                now + 1, encode(_progress("unavailable", reason, started_at=trigger["started_at"])), row["id"],
            ))
        return False
    if reason == GPU_RULE_UNAVAILABLE:
        _store_progress(conn, row["id"], _progress("unavailable", reason, started_at=trigger["started_at"]), {})
        return False
    if conn.execute("SELECT 1 FROM task_operations WHERE run_id=? AND state IN ('reserved','accepted')", (run["id"],)).fetchone():
        return False
    reason = f"达到运行时长上限，{reason if state != 'expired' else MISSING}，本次启动仅提醒"
    conn.execute("UPDATE task_runs SET state='missed',reason=? WHERE id=?", (reason, run["id"]))
    _runtime_notified(conn, run, now, reason)
    if progress["started_at"] == trigger["started_at"]:
        progress.update(state="reminded", reason=reason)
        _store_progress(conn, row["id"], progress, json.loads(row["evidence"]))
    return True


def _enqueue(conn: Connection, rule: Row, config: dict, progress: dict, interval: tuple[int, int] | None,
             request_id: str, now: int, *, reminder: str | None = None) -> None:
    trigger = {key: progress[key] for key in ("started_at", "since_at", "sampled_at", "due_at")}
    trigger.update(automation_rule_id=rule["id"], kind=rule["kind"], rule_version=rule["version"])
    deadline = (progress["sampled_at"] or now) + SAMPLE_FRESHNESS
    if interval:
        trigger.update(window_start_at=interval[0], window_end_at=interval[1])
        deadline = interval[1] - 1
    snapshot = {
        "name": rule["name"], "kind": "stop", "project_id": rule["project_id"],
        "config": {key: rule[key] for key in ("instance_id", "region", "zone")} | {"email": config.get("email")},
        "channels": json.loads(rule["channels"]), "window_seconds": max(0, deadline - now), "trigger": trigger,
    }
    run = insert_run(conn, snapshot, now, now, request_id=request_id, state="missed" if reminder else "queued",
                     reason=reminder or progress["reason"])
    if reminder:
        _runtime_notified(conn, run, now, reminder)


def _existing_progress(conn: Connection, request_id: str, progress: dict) -> bool:
    existing = conn.execute("SELECT state,reason,result FROM task_runs WHERE request_id=?", (request_id,)).fetchone()
    if existing is None:
        return False
    if json.loads(existing["result"]).get("automation_reminded"):
        state = "reminded"
    else:
        state = "queued" if existing["state"] in {"queued", "waiting", "reconciling"} else "handled"
    progress.update(state=state, reason=existing["reason"])
    return True


def observe(conn: Connection, project_id: str, rows: list[dict], points_by_id: dict, now: int, *, enabled: bool) -> None:
    rules = conn.execute("SELECT * FROM automation_rules WHERE project_id=? AND status='active'", (project_id,)).fetchall()
    if not rules:
        return
    if not enabled:
        conn.execute(
            """UPDATE automation_rules SET evidence_after=?,skip_due_before=?,progress=?,evidence='{}'
               WHERE project_id=? AND status='active'""",
            (now + 1, now, encode(_progress("unavailable", "后台写操作已暂停")), project_id),
        )
        return
    instances = {row["id"]: row for row in rows}
    for rule in rules:
        instance = instances.get(rule["instance_id"])
        if not instance or not _matches(rule, instance):
            _store_progress(conn, rule["id"], _progress("unavailable", "未找到规则所绑定的实例"), {})
        elif instance["state"] != "Running" or type(instance.get("start_time")) is not int or not 0 < instance["start_time"] <= now:
            _store_progress(conn, rule["id"], _progress("waiting", "等待实例运行并确认启动时间"), {})
        else:
            _observe_instance(conn, rule, instance, points_by_id.get(instance["id"], []), now)


def _observe_instance(conn: Connection, rule: Row, instance: dict, points: list[dict], now: int) -> None:
    config = json.loads(rule["config"])
    started_at = instance["start_time"]
    kind = rule["kind"]
    mode_problem = _mode_problem(conn, kind, instance)
    if mode_problem:
        conn.execute("UPDATE automation_rules SET evidence_after=?,progress=?,evidence='{}' WHERE id=?", (
            max(rule["evidence_after"], now + 1), encode(_progress("unavailable", mode_problem, started_at=started_at)), rule["id"],
        ))
        return
    duration = config["duration_minutes"] * 60
    idle = kind != "runtime_stop"
    interval = _interval(config["schedule"], now) if idle else None
    if idle and interval is None:
        _store_progress(conn, rule["id"], _progress("outside_window", "当前不在允许停机时段", started_at=started_at), {})
        return
    floor = max(started_at, rule["evidence_after"], rule["armed_at"] if idle else 0, interval[0] if interval else 0)
    previous = json.loads(rule["progress"])
    saved = json.loads(rule["evidence"]) if previous["started_at"] == started_at else {}
    evidence, problem = _advance(instance, points, config, saved, floor, now, kind)
    intersection = _overlap(evidence, now, kind)
    since, sampled = intersection or (None, None)
    due_at = since + duration if idle and since is not None else None if idle else started_at + duration
    missing = CPU_MISSING if kind == "cardless_idle_stop" else MISSING
    progress = _progress("counting" if intersection else "unavailable", "正在累计空闲采样" if intersection else problem or missing,
                         started_at=started_at, since_at=since, sampled_at=sampled, due_at=due_at)
    if idle and intersection and sampled >= due_at:
        request_id = f"automation:{rule['id']}:{rule['version']}:{started_at}:{since}"
        if not _existing_progress(conn, request_id, progress):
            progress.update(state="queued", reason="当前时段内已达到连续空闲时间，等待执行停机")
            _enqueue(conn, rule, config, progress, interval, request_id, now)
    elif not idle and now >= due_at:
        _runtime_decision(conn, rule, config, progress, intersection, problem, now)
    elif not idle:
        progress.update(state="counting", reason="等待实例达到运行时长上限")
    _store_progress(conn, rule["id"], progress, evidence)


def _runtime_decision(conn: Connection, rule: Row, config: dict, progress: dict,
                      intersection: tuple[int, int] | None, problem: str | None, now: int) -> None:
    due_at = progress["due_at"]
    request_id = f"automation:{rule['id']}:runtime:{progress['started_at']}"
    if _existing_progress(conn, request_id, progress):
        return
    if due_at <= rule["skip_due_before"]:
        progress.update(state="outside_window", reason="已跳过暂停期间达到上限的启动记录")
        return
    check_at = max(due_at, rule["armed_at"])
    if (intersection and now - check_at <= SAMPLE_FRESHNESS and intersection[1] - intersection[0] >= RUNTIME_SAMPLE_SECONDS
            and intersection[0] <= check_at - RUNTIME_SAMPLE_SECONDS
            and intersection[1] >= check_at - MAX_SAMPLE_GAP):
        progress.update(state="queued", reason="达到运行时长上限且最近 5 分钟空闲，等待执行停机")
        _enqueue(conn, rule, config, progress, None, request_id, now)
    else:
        reason = f"达到运行时长上限，{BUSY if problem == BUSY else MISSING}，本次启动仅提醒"
        progress.update(state="reminded", reason=reason)
        _enqueue(conn, rule, config, progress, None, request_id, now, reminder=reason)


def _stop_evidence(conn: Connection, run: dict, now: int) -> tuple[Row | None, dict, str | None]:
    trigger = run["snapshot"]["trigger"]
    row = conn.execute("SELECT * FROM automation_rules WHERE id=?", (trigger["automation_rule_id"],)).fetchone()
    if row is None or row["status"] != "active" or row["version"] != trigger["rule_version"]:
        return row, {}, PAUSED
    scope = run["snapshot"]["config"]
    if row["project_id"] != run["project_id"] or any(row[key] != scope[key] for key in ("instance_id", "region", "zone")):
        return row, {}, "规则目标实例已发生变化"
    if storage.get_settings(conn)["restore_gate"]:
        return row, {}, "后台写操作已暂停"
    if trigger.get("window_end_at") is not None and now >= trigger["window_end_at"]:
        return row, {}, "允许停机时段已结束"
    if trigger["kind"] == "runtime_stop" and not 0 <= now - trigger["sampled_at"] <= SAMPLE_FRESHNESS:
        return row, {}, MISSING
    progress, evidence = json.loads(row["progress"]), json.loads(row["evidence"])
    if progress["started_at"] != trigger["started_at"]:
        return row, {}, "实例启动状态已变化"
    intersection = _overlap(evidence, now, row["kind"])
    if not intersection or intersection[0] > trigger["since_at"] or intersection[1] < trigger["sampled_at"]:
        return row, {}, CPU_MISSING if row["kind"] == "cardless_idle_stop" else MISSING
    return row, evidence, None


def stop_block(conn: Connection, run: dict, now: int) -> str | None:
    """Recheck the persisted rule and evidence during claim and before dispatch."""
    return _stop_evidence(conn, run, now)[2]


def _mode_problem(conn: Connection, kind: str, instance: dict) -> str | None:
    no_gpu = instance.get("gpu_mode") == "without_gpu"
    if kind == "cardless_idle_stop":
        options = cardless.options_for(instance, cardless.profiles(conn)) if no_gpu else []
        return None if cardless.match_profile(instance, options) else CARDLESS_ONLY
    return GPU_RULE_UNAVAILABLE if no_gpu else None


def check_stop(path: Path, run: dict, instance: dict, cloud: CompShare, now: int) -> str | None:
    """Before reserving the write, refetch the target's latest monitor readings."""
    trigger = run["snapshot"]["trigger"]
    with storage.connect(path) as conn:
        row, evidence, problem = _stop_evidence(conn, run, now)
        if problem:
            return problem
        if not _matches(row, instance) or instance.get("start_time") != trigger["started_at"]:
            return "目标实例或启动时间已发生变化"
        if problem := _mode_problem(conn, row["kind"], instance):
            return problem
    try:
        points = cloud.monitor(run["project_id"], instance["region"], [instance["id"]])
    except CloudError:
        return MONITOR_UNAVAILABLE
    config = json.loads(row["config"])
    checked_at = max(now, storage.now())
    floor = max(instance["start_time"], trigger.get("window_start_at", 0))
    updated, problem = _advance(instance, points, config, evidence, floor, checked_at, row["kind"])
    intersection = _overlap(updated, checked_at, row["kind"])
    if intersection and intersection[0] <= trigger["since_at"]:
        return None
    return problem or (CPU_MISSING if row["kind"] == "cardless_idle_stop" else MISSING)
