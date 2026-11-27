"""Check usage accounting and receipt semantics independently of cloud access."""

import json
from datetime import datetime, timedelta

import pytest

from gpu_console import storage
from gpu_console.application.task_config import SHANGHAI
from gpu_console.storage import dashboard_usage as usage
from gpu_console.worker.collection import GPU_UTIL, Collector

PROJECT = "org-test"
MIDNIGHT = int(datetime(2026, 11, 22, tzinfo=SHANGHAI).timestamp())


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "console.sqlite3"
    storage.initialize(path)
    with storage.connect(path) as db:
        yield db


def instance(instance_id="uhost-observed", *, boot=MIDNIGHT - 600, state="Running", model="4090"):
    return {
        "id": instance_id, "project_id": PROJECT, "name": "训练机", "state": state,
        "gpu_model": model, "gpu": 2, "start_time": boot,
        "gpu_mode": "with_gpu" if state == "Running" else None,
    }


def observe(conn, timestamp, rows, readings=None, *, interval=60):
    if readings is None:
        readings = {row["id"]: {timestamp: {"GPU-1": 0.0, "GPU-2": 0.0}} for row in rows}
    with storage.transaction(conn):
        usage.observe(conn, PROJECT, rows, readings, timestamp, interval)


def test_midnight_split_duplicate_and_released_instance(conn):
    row = instance()
    observe(conn, MIDNIGHT - 30, [row])
    observe(conn, MIDNIGHT + 30, [row], {row["id"]: {MIDNIGHT + 30: {"GPU-1": 20.0, "GPU-2": 60.0}}})
    observe(conn, MIDNIGHT + 30, [row])
    observe(conn, MIDNIGHT + 20, [row])
    observe(conn, MIDNIGHT + 90, [])

    result = usage.summary(conn, PROJECT, "7d", MIDNIGHT + 90)
    assert result["recording_since"] == MIDNIGHT - 30
    previous, today = result["days"][-2:]
    assert (previous["date"], today["date"]) == ("2026-11-21", "2026-11-22")
    assert previous["gpu_hours"] == today["gpu_hours"] == pytest.approx(1 / 60)
    assert previous["average_utilization"] == today["average_utilization"] == pytest.approx(20)
    assert today["coverage"] == pytest.approx(1 / 3)
    assert result["gpu_hours"] == pytest.approx(1 / 30)
    assert result["instances_used"] == 1
    assert result["instances"][0]["instance_id"] == row["id"]
    assert not result["instances"][0]["current"]


def test_zero_usage_is_distinct_from_unknown_and_zero_percent(conn):
    result = usage.summary(conn, PROJECT, "today", MIDNIGHT + 60)
    assert result["gpu_hours"] is None
    assert result["days"][0]["coverage"] == 0
    assert result["recording_since"] is None

    observe(conn, MIDNIGHT + 60, [instance(state="Stopped")])
    observe(conn, MIDNIGHT + 120, [instance(state="Stopped")])
    result = usage.summary(conn, PROJECT, "today", MIDNIGHT + 120)
    assert result["gpu_hours"] == 0
    assert result["average_utilization"] is None
    assert result["days"][0]["coverage"] == pytest.approx(0.5)
    assert (result["days"][0]["covered_seconds"], result["days"][0]["elapsed_seconds"]) == (60, 120)
    midnight = usage.summary(conn, PROJECT, "today", MIDNIGHT)
    assert (midnight["days"][0]["covered_seconds"], midnight["days"][0]["elapsed_seconds"]) == (0, 0)

    row = instance(boot=MIDNIGHT + 130)
    observe(conn, MIDNIGHT + 180, [row])
    observe(conn, MIDNIGHT + 240, [row])
    conn.execute(
        "INSERT INTO snapshots(kind,scope,data,success_at) VALUES ('instances',?,?,?)",
        (PROJECT, json.dumps([row]), MIDNIGHT + 240),
    )
    result = usage.summary(conn, PROJECT, "today", MIDNIGHT + 240)
    assert result["gpu_hours"] == pytest.approx(1 / 30)
    assert result["average_utilization"] == 0
    assert result["instances"][0]["current"]


def test_gaps_failed_polls_restore_and_reboots_do_not_extend_usage(conn):
    row = instance()
    observe(conn, MIDNIGHT, [row], interval=30)
    observe(conn, MIDNIGHT + 61, [row], interval=30)
    usage.collection_failed(conn, PROJECT, MIDNIGHT + 80)
    observe(conn, MIDNIGHT + 90, [row], interval=30)
    usage.restore_boundary(conn, MIDNIGHT + 100)
    observe(conn, MIDNIGHT + 120, [row], interval=30)
    rebooted = instance(boot=MIDNIGHT + 140)
    observe(conn, MIDNIGHT + 150, [rebooted], interval=30)
    observe(conn, MIDNIGHT + 180, [rebooted], {row["id"]: {MIDNIGHT + 180: {"GPU-1": 0.0}}}, interval=30)
    assert usage.summary(conn, PROJECT, "today", MIDNIGHT + 180)["gpu_hours"] is None

    observe(conn, MIDNIGHT + 210, [rebooted], interval=30)
    observe(conn, MIDNIGHT + 240, [rebooted], interval=30)
    assert usage.summary(conn, PROJECT, "today", MIDNIGHT + 240)["gpu_hours"] == pytest.approx(1 / 60)


def test_partial_project_and_independent_daily_retention(conn):
    good, unknown = instance(), instance("uhost-unknown")
    for offset in (0, 60):
        timestamp = MIDNIGHT + offset
        observe(conn, timestamp, [good, unknown], {good["id"]: {timestamp: {"GPU-1": 0.0, "GPU-2": 100.0}}})
    day = usage.summary(conn, PROJECT, "today", MIDNIGHT + 60)["days"][0]
    assert day["gpu_hours"] == pytest.approx(1 / 30)
    assert day["average_utilization"] == 50
    assert day["coverage"] == 0
    assert usage.summary(conn, "another-project", "today", MIDNIGHT + 60)["gpu_hours"] is None

    boundary = int((datetime.fromtimestamp(MIDNIGHT, SHANGHAI) + timedelta(days=30)).timestamp())
    usage.trim(conn, boundary)
    assert conn.execute("SELECT COUNT(*) FROM dashboard_usage_instance_days").fetchone()[0] == 0


def test_worker_collection_records_usage_from_monitor_evidence(tmp_path, monkeypatch):
    path = tmp_path / "worker.sqlite3"
    storage.initialize(path)
    timestamp = MIDNIGHT + 5
    monkeypatch.setattr(storage, "now", lambda: timestamp)
    row = instance() | {"region": "cn-test", "zone": "cn-test-01", "gpu_mode": None}

    class Cloud:
        def projects(self):
            return [{"id": PROJECT}]

        def zones(self):
            return []

        def catalog(self, *, zones):
            return []

        def instances(self, _project_id):
            return [row.copy()]

        def monitor(self, project_id, _region, instance_ids):
            return [
                {"project_id": project_id, "instance_id": instance_ids[0], "metric": GPU_UTIL,
                 "label": label, "sampled_at": timestamp, "value": 60.0}
                for label in ("GPU-1", "GPU-2")
            ]

    worker = Collector(path, Cloud())
    worker.tick()
    timestamp += 60
    worker.tick()
    with storage.connect(path) as db:
        result = usage.summary(db, PROJECT, "today", timestamp)
    assert result["gpu_hours"] == pytest.approx(1 / 30)
    assert result["average_utilization"] == 60
    assert result["instances"][0]["current"]


def test_usage_receipts_distinguish_actions_reminders_and_resource_waits(conn):
    conn.execute(
        """INSERT INTO tasks(id,project_id,status,version,definition,next_at,created_at,updated_at)
           VALUES ('scheduled',?,'active',1,'{}',NULL,?,?)""", (PROJECT, MIDNIGHT, MIDNIGHT),
    )

    def run(run_id, kind, state, offset, result, *, scheduled=False, automated=False, project=PROJECT):
        snapshot = {"name": run_id, "kind": kind}
        if automated:
            snapshot["trigger"] = {"automation_rule_id": "rule"}
        when = MIDNIGHT + offset
        conn.execute(
            """INSERT INTO task_runs(id,task_id,project_id,scheduled_at,deadline_at,state,snapshot,result,
                                     next_check,created_at,updated_at,request_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, "scheduled" if scheduled else None, project, when, when + 3600, state,
             json.dumps(snapshot), json.dumps(result), when, when, when, "request-" + run_id),
        )

    proof = {"disposition": "stopped", "instance_review": {"checked_at": MIDNIGHT + 60}}
    run("confirmed", "stop", "succeeded", 60, proof, automated=True)
    conn.execute(
        """INSERT INTO task_operations(run_id,project_id,action,state,data,created_at,updated_at)
           VALUES ('confirmed',?,'stop','accepted','{}',?,?)""", (PROJECT, MIDNIGHT + 50, MIDNIGHT + 55),
    )
    run("already-stopped", "stop", "succeeded", 60, {"disposition": "stopped"}, automated=True)
    run("reminder", "stop", "missed", 65, {"automation_reminded": True}, automated=True)
    run("review", "stop", "manual_review", 70, {}, automated=True)
    run("another-project", "stop", "manual_review", 70, {}, automated=True, project="another-project")
    run("waiting", "start", "waiting", 95, {"capacity_wait_since": MIDNIGHT + 80}, scheduled=True)
    run("started", "start", "succeeded", 96, {"capacity_wait_since": MIDNIGHT + 20, "disposition": "started",
                                               "instance_review": {"checked_at": MIDNIGHT + 90}}, scheduled=True)
    run("reused", "start", "succeeded", 95, {"capacity_wait_since": MIDNIGHT + 45, "disposition": "reused"}, scheduled=True)
    run("direct-start", "start", "succeeded", 97, {"capacity_wait_since": MIDNIGHT + 45, "disposition": "started"})

    automation = usage.summary(conn, PROJECT, "today", MIDNIGHT + 100)["automation"]
    assert len(automation["recent"]) == 6
    assert {row["run_id"]: row["outcome"] for row in automation["recent"]} == {
        "confirmed": "confirmed_shutdown", "reminder": "reminded", "review": "needs_review",
        "waiting": "capacity_waiting", "started": "capacity_completed", "reused": "capacity_ended",
    }
    assert automation | {"recent": None} == {
        "recent": None, "confirmed_shutdowns": 1, "reminded": 1, "needs_review": 1, "capacity_waits": 3,
        "capacity_completed": 1, "average_wait_seconds": 70,
    }
