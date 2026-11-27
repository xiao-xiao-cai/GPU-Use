"""Serve a production build with fictional UI-001 API responses on loopback."""

import argparse
import json
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from pathlib import Path
from statistics import median
from urllib.parse import parse_qs, urlparse


PROJECTS = [{"id": "sample", "name": "实验项目"}, {"id": "sample-b", "name": "备用项目"}]
CHANNELS = {"site": True, "feishu": True, "email": True}
SETTINGS = {
    "project_id": "sample", "read_only": False, "restore_gate": False, "status": {},
    "collection_seconds": 60, "catalog_seconds": 1800, "stock_seconds": 120,
    "task_stock_seconds": 120, "retention_days": 30, "idle_minutes": 30,
    "idle_threshold": 10, "idle_warmup_minutes": 10, "idle_channels": ["site"],
    "idle_email_recipient": None, "channels": CHANNELS,
}


def now():
    return int(time.time())


def snapshot(data, *, freshness=5):
    return {"data": data, "success_at": now() - freshness, "error": None}


def sample_instances(project, scenario="normal"):
    if project != "sample":
        return [{"id": "other", "project_id": project, "name": "另一项目", "state": "Stopped"}]
    rows = [
        {
            "id": instance_id, "project_id": project, "name": name, "state": state,
            "region": "cn-east", "zone": "cn-east-01", "gpu_model": "A100", "gpu": gpu,
            "gpu_mode": "with_gpu" if state == "Running" else None, "cpu": 16, "memory_gb": 64, "charge_type": "Postpay",
            "login_mode": "Password", "scheduler_status": "known", "support_without_gpu_start": True,
            "cardless_profiles": [
                {"spec": "B", "cpu": 8, "memory_gb": 16, "standard_hourly_cny": "0.60"},
                {"spec": "C", "cpu": 4, "memory_gb": 8, "standard_hourly_cny": "0.30"},
            ] if instance_id == "spare-c" else [],
            "jupyter": {"url": f"https://{instance_id}.example.invalid/lab" if url else None,
                        "source": "platform" if url else None},
            "ssh": {"user": "root", "host": "192.0.2.10", "port": 22, "command": "ssh root@192.0.2.10"},
            "disks": [], "updated_at": now(),
        }
        for instance_id, name, state, gpu, url in [
            ("train-a", "训练环境 A", "Running", 2, True),
            ("dev-b", "开发环境 B", "Running", 1, False),
            ("spare-c", "备用实例 C", "Stopped", 1, True),
        ]
    ]
    if scenario != "shutdown-stock":
        return rows
    stopped = rows[-1]
    stock = {"pool": "exclusive", "required_cards": 1, "state": "known", "remaining_cards": 25,
             "provider_updated_at": now(), "spec_available": True, "spec_checked_at": now()}
    return [*rows[:-1], stopped | {"stock": stock}, *[
        stopped | {"id": instance_id, "name": name, "gpu_model": model, "gpu": gpu,
                   "stock": stock | stock_fields, "cardless_profiles": []}
        for instance_id, name, model, gpu, stock_fields in [
            ("pool-only", "待确认开机的 4090", "4090", 0, {"required_cards": None, "spec_available": None, "spec_checked_at": None}),
            ("pool-empty", "没有库存的 5090", "5090", 1, {"remaining_cards": 0, "spec_available": False}),
            ("pool-blocked", "有卡但暂不能开机", "A100", 1, {"spec_available": False}),
            ("pool-expired", "库存过期的实例", "A100", 1, {"state": "stale", "remaining_cards": None}),
        ]
    ], stopped | {"id": "pool-starting", "name": "正在开机的实例", "stock": stock,
                  "active_operation": {"kind": "start", "state": "reconciling", "capacity_wait": False}}]


def scheduled_task():
    return {
        "id": "sample-task", "project_id": "sample", "name": "每日巡检", "kind": "inspection",
        "version": 1, "status": "active", "next_at": now() + 1800,
        "schedule": {"type": "daily", "at": now() + 1800}, "window_seconds": 7200,
        "config": {}, "channels": ["site"],
    }


def review_run():
    return {
        "id": "sample-run", "project_id": "sample", "name": "库存检测", "kind": "acquire",
        "state": "manual_review", "phase": "verify_create", "reason": "云端回执待核对",
        "scheduled_at": now() - 600, "deadline_at": now() + 1800, "updated_at": now() - 60,
        "result": {"reviewable": True}, "task_id": "sample-purchase",
        "operations": [{"action": "create", "state": "reserved"}], "audit": [],
    }


CANCEL_REVIEW_SAMPLE_AT = now()


def cancel_review_runs(scenario):
    appointment_offsets = {
        "cancel-review-old": -600,
        "cancel-review-future": 1800,
        "cancel-review-unknown": None,
        "cancel-review-conflict": -600,
        "cancel-review-snapshot": -600,
        "cancel-review-mixed": -600,
        "cancel-review-failure": -600,
    }
    if scenario not in appointment_offsets:
        return None

    timestamp = CANCEL_REVIEW_SAMPLE_AT
    offset = appointment_offsets[scenario]
    appointment = timestamp + offset if offset is not None else None
    config = {"instance_id": "train-a", "expected_stop_at": appointment}
    result = {
        "instance_id": "train-a",
        "cloud_shutdown_at": timestamp + 3600 if scenario == "cancel-review-conflict" else None,
    }
    if scenario != "cancel-review-snapshot":
        result["shutdown_at"] = appointment
    run = {
        "id": "sample-cancel-review", "project_id": "sample", "name": "取消关机预约",
        "kind": "cancel_shutdown", "state": "manual_review", "phase": "verify_cancel_shutdown",
        "reason": "撤销待确认，请核对原预约及当前云端时间",
        "scheduled_at": timestamp - 1200, "deadline_at": timestamp + 600, "updated_at": timestamp - 120,
        "snapshot": {"config": config}, "result": result,
        "operations": [{"action": "cancel_shutdown", "state": "accepted"}], "audit": [],
    }
    runs = {run["id"]: run}
    if scenario == "cancel-review-mixed":
        unrelated = review_run() | {"updated_at": timestamp - 300}
        runs[unrelated["id"]] = unrelated
    return runs


CATALOG = [
    {
        "region": "cn-east", "zone": "cn-east-01", "zone_name": "华东一区", "gpu_model": "A100",
        "status": "available", "instance_type": "on_demand", "cpu_platforms": ["Intel"],
        "specs": [{"gpu": 1, "cpu": 8, "memory_gb": 32}, {"gpu": 2, "cpu": 16, "memory_gb": 64}],
    },
    {
        "region": "cn-north", "zone": "cn-north-01", "zone_name": "华北一区", "gpu_model": "L40S",
        "status": "available", "instance_type": "spot", "cpu_platforms": ["AMD"],
        "specs": [{"gpu": 1, "cpu": 16, "memory_gb": 64}],
    },
    {
        "region": "cn-wlcb", "zone": "cn-wlcb-01", "zone_name": "华北二 A", "gpu_model": "5090",
        "status": "Normal", "instance_type": "uhost", "cpu_platforms": ["Intel"],
        "specs": [{"gpu": 1, "cpu": 16, "memory_gb": 96}, {"gpu": 2, "cpu": 32, "memory_gb": 192}],
    },
]

CREATE_CATALOG = [*CATALOG, {
    "region": "cn-sh2", "zone": "cn-sh2-01", "zone_name": "上海二 A", "gpu_model": "5090",
    "status": "Normal", "instance_type": "uhost", "cpu_platforms": ["Intel"],
    "specs": [{"gpu": 1, "cpu": 16, "memory_gb": 96}],
}]
CREATE_LOCATIONS = [
    {"region": "cn-wlcb", "zone": "cn-wlcb-01", "region_name": "华北二", "zone_name": "华北二 A", "is_pod": False},
    {"region": "cn-sh2", "zone": "cn-sh2-01", "region_name": "上海二", "zone_name": "上海二 A", "is_pod": True},
]
CREATE_DATA_DISK = {"purpose": "data", "type": "CLOUD_SSD", "min_size_gb": 10, "max_size_gb": 1500}


def create_options(zone):
    has_data = zone == "cn-wlcb-01"
    return {
        "system_disk": {"purpose": "system", "type": "CLOUD_SSD", "size_gb": 100 if has_data else 50},
        "system_disk_backing": "cloud" if has_data else "local",
        "data_disks": [CREATE_DATA_DISK] if has_data else [], "storage": "us3" if has_data else "cfs",
        "cfs": None, "cfs_known": not has_data, "reason": None, "can_mount": True,
    }


def pool_stock():
    timestamp = now()
    return [
        {"region": "cn-wlcb", "zone": "cn-wlcb-01", "gpu_model": model, "pool": pool,
         "remaining_cards": count, "state": state, "provider_updated_at": timestamp - age, "observed_at": timestamp - 5}
        for model, pool, count, state, age in (
            ("5090", "exclusive", 2, "known", 60), ("5090", "spot", 0, "known", 60),
            ("A6000", "exclusive", None, "stale", 700), ("A6000", "spot", None, "unsupported", 60),
        )
    ]


def inventory_overview(listings, include_spot):
    def key(row, pool):
        return row["region"], row["zone"], row["gpu_model"], pool

    stock = {key(row, row["pool"]): row for row in pool_stock()}
    directory = {key(row, "spot" if row["instance_type"] == "spot" else "exclusive"): row for row in listings}
    result = []
    for identity in sorted(stock.keys() | directory.keys()):
        region, zone, model, pool = identity
        if pool == "spot" and not include_spot:
            continue
        listing = directory.get(identity, {})
        observed = stock.get(identity, {"state": "unknown", "remaining_cards": None, "provider_updated_at": None})
        remaining = observed["remaining_cards"]
        specs = [
            spec | {"catalog_spec_id": f"{zone}-{model}-{pool}-{index}", "hourly_rate": None, "quote_failed": False,
                    "available": None if remaining is None else remaining >= spec["gpu"]}
            for index, spec in enumerate(listing.get("specs", []))
        ]
        result.append(listing | observed | {
            "region": region, "zone": zone, "gpu_model": model, "pool": pool, "listed": bool(listing),
            "instance_type": "spot" if pool == "spot" else "uhost", "graphics_memory_gb": listing.get("graphics_memory_gb"),
            "specs": specs, "card_counts": sorted({spec["gpu"] for spec in specs}),
            "hourly_range": {"quoted": 0, "total": len(specs), "failed": 0, "minimum": None, "maximum": None,
                             "source": None, "stale": False},
        })
    return snapshot(result)


def pool_heatmap_samples(row, timestamp):
    # Beijing 08:00 aligns with UTC midnight on the same calendar date.
    yesterday_at_eight = (timestamp + 8 * 3600) // 86400 * 86400 - 86400
    if row["gpu_model"] != "5090":
        return {}
    if row["pool"] == "spot":
        return {yesterday_at_eight: (0,)}
    return {yesterday_at_eight: (0,), yesterday_at_eight + 3600: (1, 2), yesterday_at_eight + 3 * 3600: (12,)}


def pool_sample_counts(samples, threshold):
    return {"median_cards": median(samples) if samples else None, "known_count": len(samples),
            "available_count": sum(cards >= threshold for cards in samples)}


def pool_trends(parameters, scenario="normal"):
    def get(field):
        return parameters.get(field, [None])[0]

    timestamp = now()
    since = timestamp - 7 * 86400
    threshold = int(get("card_threshold") or 1)
    items = []
    for row in pool_stock() if get("source") == "pool" else []:
        instance_type = "spot" if row["pool"] == "spot" else "uhost"
        identity = [row[field] for field in ("region", "zone", "gpu_model", "pool")]
        key = "pool-" + json.dumps(identity, separators=(",", ":"))
        count = row["remaining_cards"]
        available = count >= threshold if count is not None else None
        item = row | {"key": key, "name": row["gpu_model"], "instance_type": instance_type, "watch_id": None,
                      "spec": None, "available": available, "median_cards": count, "available_count": int(available is True),
                      "known_count": int(available is not None), "expected_count": 2017}
        if scenario == "pool-heatmap":
            samples = pool_heatmap_samples(row, timestamp)
            item.update(pool_sample_counts([cards for hour in samples.values() for cards in hour], threshold))
        if all(get(field) in (None, item[field]) for field in ("key", "region", "zone", "gpu_model", "instance_type")):
            items.append(item)
    detail = None
    if get("key") and items:
        detail = {"item": items[0], "recommendation_status": "insufficient", "recommended": [], "tight": [], "reminder": None,
                  "hours": [{field: items[0][field] for field in ("median_cards", "available_count", "known_count")}
                            | {"start": timestamp // 3600 * 3600, "expected_count": 12}]}
        if scenario == "pool-heatmap":
            samples = pool_heatmap_samples(items[0], timestamp)
            first_slot = since // 300 * 300
            last_slot = timestamp // 300 * 300
            detail["hours"] = [
                pool_sample_counts(samples.get(start, ()), threshold)
                | {"start": start, "expected_count": (min(last_slot, start + 3300) - max(first_slot, start)) // 300 + 1}
                for start in range(first_slot // 3600 * 3600, last_slot + 1, 3600)
            ]
    return {"as_of": timestamp, "since": since, "source": get("source"), "card_threshold": threshold,
            "items": items, "total": len(items), "detail": detail}


def purchase_task():
    return scheduled_task() | {
        "id": "sample-purchase", "name": "库存检测", "kind": "acquire", "next_at": now() + 3600,
        "config": {"max_price": "8", "purchase_limit": 1, "skip_if_match": True},
    }


SPEC = {
    "region": "cn-east", "zone": "cn-east-01", "gpu_model": "A100", "gpu": 1, "cpu": 8,
    "memory_gb": 32, "cpu_platform": "Intel", "image_id": "fixture-image",
    "disks": [{"purpose": "system", "type": "CLOUD_SSD", "size_gb": 50}], "charge_type": "Postpay",
}
REPAIR_TASK = purchase_task() | {
    "id": "sample-budget-task", "project_id": "sample-b", "name": "备用项目采购", "status": "completed", "next_at": None,
    "schedule": {"type": "once", "at": now() - 3703}, "template_id": "sample-budget-template", "template_version": 1,
    "config": SPEC | {"zones": [{"region": SPEC["region"], "zone": SPEC["zone"]}], "max_price": "8",
                      "purchase_limit": 1, "skip_if_match": True},
}
REPAIR_TEMPLATE = {
    "id": REPAIR_TASK["template_id"], "project_id": "sample-b", "name": "备用项目规格", "version": 2,
    "config": REPAIR_TASK["config"] | {"charge_type": "Day", "quantity": 1},
}


def actionable_runs():
    timestamp = now()
    return {
        "sample-readback": review_run() | {
            "id": "sample-readback", "project_id": "sample-b", "name": "备用实例关机核对", "kind": "stop",
            "phase": "verify_stop", "reason": "已到自动回读截止时间", "task_id": None,
            "snapshot": {"config": {"instance_id": "other"}},
            "result": {"instance_id": "other"},
            "operations": [{"action": "stop", "state": "accepted"}],
        },
        "sample-missed-shutdown": review_run() | {
            "id": "sample-missed-shutdown", "name": "训练环境预约关机", "kind": "shutdown",
            "phase": None, "state": "succeeded", "reason": "云端已确认关机预约", "task_id": None,
            "snapshot": {"config": {"instance_id": "train-a", "shutdown_at": timestamp - 600}},
            "result": {"instance_id": "train-a", "shutdown_at": timestamp - 600,
                       "cloud_shutdown_at": timestamp - 600, "shutdown_outcome": "missed"},
            "operations": [], "shutdown_reviewable": True,
        },
        "sample-budget-review": review_run() | {
            "id": "sample-budget-review", "project_id": "sample-b", "name": REPAIR_TASK["name"],
            "phase": None, "reason": "模板的计费方式或购买时长已改变，请重新预览并保存预算",
            "task_id": REPAIR_TASK["id"], "result": {"task_config_issue": True}, "operations": [],
        },
    }


def task_preview(body, scheduled_at):
    deadline = None
    if scheduled_at is not None:
        deadline = scheduled_at - 300 if body["kind"] == "shutdown" else scheduled_at + body["window_seconds"]
    config = body["config"]
    matching = [row for row in sample_instances(body["project_id"])
                if row["id"] == config.get("instance_id") or row.get("gpu_model") == config.get("gpu_model")]
    return {
        "scheduled_at": scheduled_at, "deadline_at": deadline, "matching_instances": matching,
        "max_price": config.get("max_price"), "purchase_limit": config.get("purchase_limit"),
        "snapshot_at": now(), "template_version": body.get("template_version"),
        **({"boot_mode": config.get("boot_mode", "with_gpu"), "without_gpu_spec": config.get("without_gpu_spec"),
            "shutdown_after_seconds": config.get("shutdown_after_seconds")} if body["kind"] == "start" else {}),
    }


DETAIL_APPOINTMENT = now() + 3 * 3600
ACTION_NAMES = {
    "start": "有卡开机", "stop": "立即关机", "reboot": "重启", "rename": "重命名",
    "shutdown": "设置关机预约", "cancel_shutdown": "取消关机预约",
}


def sample_detail(project, instance_id, scenario):
    instance = next((item for item in sample_instances(project, scenario) if item["id"] == instance_id), None)
    if instance is None:
        return None
    if instance_id == "train-a":
        instance["start_time"] = now() - 5400
        instance["software_links"] = [{"name": "TensorBoard", "url": "https://train-a.example.invalid/tensorboard"}]
        instance["ip_addresses"] = [{"address": "192.0.2.10", "type": "Private", "bandwidth_mbps": 0}]
        if scenario.startswith("detail") or scenario == "actionable":
            instance["scheduler_stop_time"] = DETAIL_APPOINTMENT
        if scenario == "detail-key":
            instance["login_mode"] = "KeyPair"
        if scenario == "detail-spot":
            instance["charge_type"] = "Spot"
    return instance


def sample_metrics(instance_id, scenario, since, until):
    if instance_id != "train-a":
        return []
    anchor = (now() // 60) * 60
    return [
        {"metric": "cloudwatch_gpu_util", "label": card, "sampled_at": anchor - offset, "value": value}
        for card, offsets in (
            ("GPU 0", [(240, 41), (180, 13), (120, 11), (60, 0), (0, 0)]),
            ("GPU 1", [(240, 60), (180, 54)] if scenario == "detail-gap" else [(240, 60), (180, 54), (60, 32), (0, 31)]),
        )
        for offset, value in offsets
        if since <= anchor - offset <= until
    ]


def notification(id_, event_id, channel, *, title="模拟巡检回执", state="pending", error=None,
                 event_type="result", project_id="sample", **payload):
    return {
        "id": id_, "event_id": event_id, "channel": channel, "payload": {"title": title} | payload,
        "event_type": event_type, "project_id": project_id,
        "state": state, "error": error, "attempts": 1 if state != "pending" else 0,
        "created_at": now() - 120, "next_at": None, "read_at": None,
        "delivered_at": now() - 90 if state == "delivered" else None,
    }


def system_status(scenario):
    names = ("worker", "projects", "instances", "metrics", "stock", "catalog", "cloud", "notifications")
    rows = {name: {"component": name, "success_at": now() - 40, "error": None} for name in names}
    if scenario.startswith("settings"):
        rows["worker"]["success_at"] = now() - 120
        rows["stock"]["error"] = "模拟规格检测超时"
    return rows


class FixtureHandler(SimpleHTTPRequestHandler):
    links = {}
    instance_runs = {}
    tasks = {"sample-task": scheduled_task(), "sample-purchase": purchase_task()}
    task_runs = {"sample-run": review_run()}
    templates = {}
    watches = {}
    sequence = count(1)
    preview_failures = set()
    warning_dismissals = {}
    warning_failures = set()
    shutdown_reviews = {}
    instance_rechecks = {}
    repair_tasks = {REPAIR_TASK["id"]: REPAIR_TASK.copy()}
    action_runs = actionable_runs()
    previews = {}
    creation_receipts = {}
    creation_runs = {}
    refreshes = {}
    project_refreshes = count(1)
    settings_changes = {}
    notifications = {
        101: notification(101, "sample-event", "site", state="delivered"),
        102: notification(102, "sample-event", "feishu", state="failed", error="模拟投递超时"),
        103: notification(103, "sample-event", "email", state="delivered"),
    }
    action_notifications = {
        201: notification(201, "stop-review", "site", title="备用实例关机待核对", event_type="review", project_id="sample-b",
                          state="delivered", run_id="sample-readback", message="原始回执仍待后台核对"),
        202: notification(202, "budget-review", "site", title="备用项目采购预算待更新", event_type="review", project_id="sample-b",
                          state="delivered", run_id="sample-budget-review"),
        203: notification(203, "missed-shutdown", "site", title="训练环境关机未按期确认", event_type="shutdown-outcome",
                          state="delivered", run_id="sample-missed-shutdown"),
        204: notification(204, "watch-stock", "site", title="备用项目关注规格到货", event_type="stock",
                          project_id="sample-b", state="delivered", watch_id="sample-stock-watch"),
        205: notification(205, "exact-spec", "site", title="可添加关注的到货规格", event_type="stock",
                          project_id="sample-b", state="delivered", spec=SPEC | {"gpu": 2, "cpu": 16, "memory_gb": 64}),
        206: notification(206, "idle-sample", "site", title="训练环境持续空闲", event_type="idle",
                          state="delivered", instance_id="train-a"),
        **{id_: notification(id_, f"legacy-{id_}", "site", title=f"历史通知 {id_}", event_type="legacy",
                             project_id=None, state="delivered") for id_ in range(120, 140)},
    }
    action_watches = {
        watch_id: {"id": watch_id, "project_id": "sample-b", "name": name, "spec": spec,
                   "available": True, "success_at": now() - 10, "error": None, "history": []}
        for watch_id, name, spec in [
            ("sample-stock-watch", "已通知的 A100 规格", SPEC),
            ("sample-other-watch", "另一个 A100 规格", SPEC | {"gpu": 4, "cpu": 32, "memory_gb": 128}),
        ]
    }

    def log_message(self, *_args):
        pass

    def send_json(self, data, status=200, cookie=None):
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie + "; Path=/; SameSite=Strict; HttpOnly")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path.startswith("/api/"):
            self.handle_api()
        else:
            super().do_GET()

    def do_POST(self):
        self.handle_api()

    def do_PATCH(self):
        self.handle_api()

    def do_PUT(self):
        self.handle_api()

    def do_DELETE(self):
        self.handle_api()

    def handle_api(self):
        url = urlparse(self.path)
        parameters = parse_qs(url.query)
        path = url.path.removeprefix("/api")
        project = parameters.get("project_id", ["sample"])[0]
        scenario = parse_qs(urlparse(self.headers.get("Referer", "")).query).get("scenario", ["normal"])[0]
        cookie = self.headers.get("Cookie", "")
        authenticated = "fixture_auth=yes" in cookie or (
            "fixture_auth=no" not in cookie and scenario != "login"
        )
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        if path == "/session":
            if scenario == "unreachable":
                return self.send_json({"detail": "模拟服务暂不可用"}, 503)
            initialized = scenario != "uninitialized"
            return self.send_json({
                "initialized": initialized,
                "authenticated": initialized and authenticated,
                "csrf_token": "fixture",
            })
        if path == "/login":
            if body != {"username": "demo", "password": "demo"}:
                return self.send_json({"detail": "账号或密码错误"}, 401)
            return self.send_json(
                {"initialized": True, "authenticated": True, "csrf_token": "fixture"},
                cookie="fixture_auth=yes",
            )
        if path == "/logout":
            return self.send_json({}, cookie="fixture_auth=no")
        if not authenticated or scenario == "expired":
            return self.send_json({"detail": "模拟会话已过期"}, 401)
        if scenario == "instance-create" and (
            path in {"/images", "/instance-create-options", "/instance-quotes", "/instance-purchase-requests/preview",
                     "/instance-purchase-requests"} or path.startswith("/instance-quotes/")
            or path == "/instances" and self.command == "POST"
            or path.startswith("/runs/") and path.rsplit("/", 1)[-1] in self.creation_runs
        ):
            return self.handle_instance_creation(path, parameters, body)
        if path == "/projects":
            projects = snapshot(PROJECTS)
            if scenario == "actionable-refresh" and not any(
                batch["scenario"] == scenario and batch["kind"] == "projects" and not batch["errors"]
                and time.monotonic() >= batch["ready_at"] for batch in tuple(self.refreshes.values())
            ):
                projects["error"] = "模拟上次项目同步失败"
            return self.send_json(projects)
        if path == "/instances":
            return self.send_json(snapshot(sample_instances(project, scenario), freshness=600 if scenario == "stale" else 5))
        if path.startswith("/instances/"):
            return self.handle_instance(path, project, scenario, parameters, body)
        if path == "/actions" and self.command == "POST":
            return self.handle_action(project, scenario, body)
        if path == "/runs" and "instance_id" in parameters:
            return self.handle_instance_runs(project, scenario, parameters["instance_id"][0])
        if path == "/runs" and scenario == "instance-create":
            return self.send_json([run for run in self.creation_runs.values() if run["project_id"] == project])
        if path == "/settings":
            if self.command == "PATCH":
                if scenario == "settings-error":
                    return self.send_json({"detail": "模拟设置暂时无法保存"}, 422)
                self.settings_changes.update(body)
            return self.send_json(SETTINGS | self.settings_changes | {
                "read_only": scenario == "detail-readonly",
                "restore_gate": scenario in {"detail-restore", "actionable-recovery"},
                "recovery": [{"backup_at": now() - 3600, "restored_at": now() - 600, "reviewed_at": None}]
                if scenario == "actionable-recovery" else [],
                "status": system_status(scenario),
            })
        if path == "/notifications" or path.startswith("/notifications/"):
            return self.handle_notifications(path, scenario, parameters, body)
        if any(path == prefix or path.startswith(prefix + "/") for prefix in ("/tasks", "/runs")):
            return self.handle_tasks(path, project, scenario, parameters, body)
        if path == "/inventory/pools":
            return self.send_json(snapshot(pool_stock()))
        if path == "/inventory/overview":
            listings = CREATE_CATALOG if scenario == "instance-create" else CATALOG
            return self.send_json(inventory_overview(listings, parameters.get("include_spot") == ["true"]))
        if path == "/dashboard/availability":
            return self.send_json(pool_trends(parameters, scenario))
        if path == "/locations":
            if scenario == "instance-create":
                return self.send_json(snapshot(CREATE_LOCATIONS))
            return self.send_json(snapshot([{"region": "cn-wlcb", "zone": "cn-wlcb-01", "is_pod": False,
                                             "region_name": "华北二", "zone_name": "华北二 A"}]))
        if any(path == prefix or path.startswith(prefix + "/") for prefix in (
            "/catalog", "/stock", "/watches", "/templates",
        )):
            return self.handle_inventory(path, project, scenario, body)
        if path == "/refresh" or path.startswith("/refresh/"):
            return self.handle_refresh(path, scenario, body)
        return self.send_json({"detail": "模拟接口未配置"}, 404)

    def handle_instance_creation(self, path, parameters, body):
        if path == "/images":
            images = {kind: snapshot([]) for kind in ("system", "app", "custom", "shared")}
            images["app"] = snapshot([{"id": "fixture-image", "name": "模拟 PyTorch", "kind": "app", "runtime": "container"}])
            return self.send_json(images | {"refresh_id": None})
        if path == "/instance-create-options":
            zone = parameters.get("zone", [""])[0]
            if (zone not in {item["zone"] for item in CREATE_LOCATIONS} or parameters.get("gpu_model") != ["5090"]
                    or parameters.get("image_id") != ["fixture-image"] or parameters.get("instance_type") != ["uhost"]):
                return self.send_json({"detail": "模拟创建选项参数无效"}, 422)
            return self.send_json(create_options(zone))
        if path in {"/instance-quotes", "/instance-purchase-requests/preview"}:
            spec = body["spec"]
            options = create_options(spec["zone"])
            boot = [disk for disk in spec["disks"] if disk["purpose"] == "system"]
            data = [disk for disk in spec["disks"] if disk["purpose"] == "data"]
            if boot != [options["system_disk"]] or any(not any(
                row["type"] == disk["type"] and row["min_size_gb"] <= disk["size_gb"] <= row["max_size_gb"]
                for row in options["data_disks"]
            ) for disk in data):
                return self.send_json({"detail": "报价所用磁盘与模拟平台选项不符"}, 422)
            key = f"create-{next(self.sequence)}"
            receipt = {"project_id": body["project_id"], "spec": spec, "expires_at": now() + 300}
            if path == "/instance-quotes":
                receipt.update(id=key, state="ready", price="1.25", quoted_at=now(), reason=None, run_id=None)
            else:
                receipt.update(preview_id=key, retry=body["retry"], window_seconds=body.get("window_seconds", 300),
                               max_price=body["max_price"], price_unit="CNY/hour", channels=body["channels"], email=body.get("email"))
            self.creation_receipts[key] = receipt
            return self.send_json(receipt, 202 if path == "/instance-quotes" else 200)
        if path in {"/instances", "/instance-purchase-requests"}:
            receipt = self.creation_receipts.get(body.get("quote_id") or body.get("preview_id"))
            if receipt is None:
                return self.send_json({"detail": "模拟报价已失效"}, 409)
            timestamp = now()
            run = {
                "id": body["request_id"], "project_id": receipt["project_id"], "name": "模拟创建实例", "kind": "acquire",
                "scheduled_at": timestamp, "deadline_at": timestamp + receipt.get("window_seconds", 300),
                "state": "waiting" if receipt.get("retry") else "succeeded", "updated_at": timestamp,
                "reason": "等待模拟库存" if receipt.get("retry") else "模拟实例已创建", "result": {},
            }
            if receipt.get("retry"):
                run["next_check"] = timestamp + 30
            elif (shared := receipt["spec"].get("shared_storage", {})).get("mode") == "cfs_create":
                run["reason"] = "模拟实例已创建，共享存储挂载尚未核实"
                run["result"] = {"instance_id": "train-a", "storage_state": "attachment_unverified",
                                 "storage_size_gib": shared["size_gib"], "storage_quote": "6.25"}
            self.creation_runs[run["id"]] = run
            return self.send_json(run, 202)
        key = path.rsplit("/", 1)[-1]
        return self.send_json(self.creation_receipts[key] if path.startswith("/instance-quotes/") else self.creation_runs[key])

    def handle_instance(self, path, project, scenario, parameters, body):
        _, _, instance_id, *route = path.split("/")
        instance = sample_detail(project, instance_id, scenario)
        if instance is None:
            return self.send_json({"detail": "项目快照中找不到该实例"}, 404)
        scope = (scenario, project, instance_id)
        platform_link = instance.get("jupyter", {"url": None, "source": None})
        link = self.links.get(scope, platform_link)
        if not route:
            # The detail response may arrive a second after the project snapshot.
            return self.send_json(snapshot(instance, freshness=600 if scenario == "stale" else 5) | {
                "jupyter": link, "last_sample_at": now() - 25 if instance_id == "train-a" else None,
            })
        if route == ["metrics"]:
            since = int(parameters.get("since", [0])[0])
            until = int(parameters.get("until", [now()])[0])
            return self.send_json(snapshot(sample_metrics(instance_id, scenario, since, until)))
        if route == ["ssh-password"] and self.command == "POST":
            if instance.get("login_mode") == "KeyPair":
                return self.send_json({"detail": "使用密钥登录"}, 409)
            time.sleep(2)
            return self.send_json({"password": "fixture-ssh-password"})
        if route == ["jupyter"]:
            if self.command == "PUT":
                link = {"url": body["url"], "source": "custom"}
                self.links[scope] = link
            elif self.command == "DELETE":
                self.links.pop(scope, None)
                link = platform_link
            return self.send_json(link)
        return self.send_json({"detail": "模拟实例接口未配置"}, 404)

    def handle_action(self, project, scenario, body):
        project_id = body.get("project_id", project)
        instance_id, kind = body.get("instance_id"), body.get("kind")
        if kind not in ACTION_NAMES or not sample_detail(project_id, instance_id, scenario):
            return self.send_json({"detail": "模拟实例操作参数无效"}, 422)
        timestamp = now()
        run = {
            "id": body["request_id"], "project_id": project_id,
            "name": "无卡开机" if kind == "start" and body.get("boot_mode") == "without_gpu" else ACTION_NAMES[kind],
            "kind": kind, "scheduled_at": timestamp, "deadline_at": timestamp + 1800,
            "state": "queued", "snapshot": {"config": body},
            "result": {"instance_id": instance_id}, "updated_at": timestamp,
        }
        self.instance_runs.setdefault((scenario, project_id, instance_id), []).append(run)
        return self.send_json(run)

    def handle_instance_runs(self, project, scenario, instance_id):
        runs = self.instance_runs.get((scenario, project, instance_id), [])
        if scenario == "actionable":
            runs = runs + [self.with_run_status(run, scenario) for run in self.action_runs.values()
                           if run["project_id"] == project and run["result"].get("instance_id") == instance_id]
        if scenario == "detail-inflight":
            return self.send_json([{
                "id": "detail-pending-run", "project_id": project, "kind": "reboot", "name": "实例重启",
                "scheduled_at": now(), "deadline_at": now() + 1800, "state": "waiting",
                "result": {"instance_id": instance_id}, "updated_at": now(),
            }])
        return self.send_json([
            run | {"state": "succeeded"} if run["state"] == "queued" and now() - run["updated_at"] > 2 else run
            for run in reversed(runs)
        ])


    def with_run_status(self, run, scenario):
        key = (scenario, run["id"])
        response = run | {"warning_dismissed": self.warning_dismissals.get(key, False)}
        if key in self.instance_rechecks:
            ready_at, checked_at = self.instance_rechecks[key]
            if time.monotonic() < ready_at:
                return response | {"state": "reconciling", "reason": "正在核对实例操作结果",
                                   "result": run["result"] | {"instance_recheck_pending": True}}
            evidence = {"checked_at": checked_at, "state": "Stopped", "detail": "云端已确认关机"}
            return response | {"state": "succeeded", "phase": None, "reason": evidence["detail"],
                               "updated_at": checked_at, "result": run["result"] | {"instance_review": evidence}}
        review = self.shutdown_reviews.get(key)
        if review is None:
            return response
        ready_at, checked_at, keep_at = review
        if run["kind"] == "shutdown":
            if time.monotonic() < ready_at:
                return response | {"phase": "review_shutdown", "shutdown_reviewable": False}
            outcome = "kept" if keep_at == DETAIL_APPOINTMENT else "different"
            evidence = {"checked_at": checked_at, "outcome": outcome, "schedule_known": True,
                        "instance_state": "Running", "resolved": outcome == "kept",
                        "original_shutdown_at": run["result"]["shutdown_at"], "cloud_shutdown_at": DETAIL_APPOINTMENT,
                        "detail": "已确认保留当前云端预约" if outcome == "kept" else "云端有不同的有效预约，请确认保留或到实例详情调整"}
            return response | {"updated_at": checked_at, "shutdown_reviewable": outcome != "kept",
                               "result": run["result"] | {"shutdown_review": evidence}}
        if time.monotonic() < ready_at:
            return response | {
                "state": "reconciling", "phase": "review_cancel_shutdown", "reason": "正在重新核对云端预约",
            }
        outcome, reason = {
            "cancel-review-future": ("still_present", "原预约仍存在，请在实例详情核对"),
            "cancel-review-unknown": ("unknown", "仍无法确认云端预约"),
            "cancel-review-conflict": ("changed", "云端已有其他时间的关机预约"),
        }.get(scenario, ("absent", "已核对，云端无关机预约"))
        result = run["result"]
        original = result.get("shutdown_at", run["snapshot"]["config"].get("expected_stop_at"))
        observed = {"still_present": original, "changed": result.get("cloud_shutdown_at")}.get(outcome)
        return response | {
            "state": "reviewed" if outcome in {"absent", "changed"} else "manual_review",
            "phase": None, "reason": reason, "updated_at": checked_at,
            "result": result | {"shutdown_review": {
                "checked_at": checked_at, "outcome": outcome,
                "cloud_shutdown_at": observed, "original_shutdown_at": original,
            }},
        }

    def business_run(self, run, scenario):
        run = self.with_run_status(run, scenario)
        result = run["result"]
        review = result.get("shutdown_review", {})
        destination = {"page": "tasks", "project_id": run["project_id"], "run_id": run["id"]}
        if result.get("task_config_issue") and run["state"] == "manual_review":
            destination = {"page": "tasks", "project_id": run["project_id"], "task_id": run["task_id"]}
        if run.get("phase") in {"review_shutdown", "review_cancel_shutdown"} or result.get("instance_recheck_pending"):
            status, label, attention = "in_progress", "正在核对", True
        elif run["state"] == "reviewed" or review.get("resolved") or result.get("purchase_review"):
            status, label, attention = "resolved", "已核对", False
        elif result.get("shutdown_outcome") == "missed":
            status, label, attention = "needs_attention", "关机结果待处理", True
        elif run["state"] == "manual_review" or run["state"] == "reconciling" and result.get("reviewable"):
            status, label, attention = "needs_attention", "待人工核对", True
        else:
            status, label, attention = "completed", {"queued": "已排队", "succeeded": "已完成"}.get(run["state"], run["state"]), False
        return run | {"business": {
            "status": status, "label": label, "attention": attention, "destination": destination,
            "detail": review.get("detail") or run.get("reason"), "observed_at": review.get("checked_at", run["updated_at"]),
        }}

    def handle_tasks(self, path, project, scenario, parameters, body):
        method = self.command
        tasks = self.tasks | self.repair_tasks if scenario == "actionable" else self.tasks
        if path == "/tasks/preview" and method == "POST":
            if scenario == "preview-race" and str(body["config"].get("max_price")) == "5":
                time.sleep(2)
            if scenario == "preview-failure" and body["project_id"] not in self.preview_failures:
                self.preview_failures.add(body["project_id"])
                return self.send_json({"detail": "模拟预览暂时无法读取"}, 503)
            scheduled_at = body["schedule"]["at"]
            task = tasks.get(body.get("task_id"))
            if task is None:
                return self.send_json(task_preview(body, scheduled_at))
            if task["project_id"] != body["project_id"] or task["version"] != body.get("task_version"):
                return self.send_json({"detail": "任务版本已更新，请重新打开任务编辑"}, 422)
            if body["schedule"] == task["schedule"]:
                scheduled_at = task["next_at"]
            token = f"preview-{next(self.sequence)}"
            definition = {key: value for key, value in body.items() if key not in {"task_id", "task_version"}}
            self.previews[task["id"]] = (token, task["version"], definition)
            return self.send_json(task_preview(body, scheduled_at) | {"preview_token": token})
        if path == "/tasks" and method == "GET":
            return self.send_json([task for task in tasks.values() if task["project_id"] == project])
        fixture_runs = cancel_review_runs(scenario)
        task_runs = self.task_runs if fixture_runs is None else fixture_runs
        if scenario == "actionable":
            task_runs = task_runs | self.action_runs
        if path == "/runs" and method == "GET":
            task_id = parameters.get("task_id", [None])[0]
            matching = [run for run in task_runs.values()
                        if run["project_id"] == project and (not task_id or run.get("task_id") == task_id)]
            if parameters.get("attention") == ["true"]:
                runs = [self.business_run(run, scenario) for run in matching]
                return self.send_json([run for run in runs if run["business"]["attention"]])
            return self.send_json([self.with_run_status(run, scenario) for run in matching])
        if path.startswith("/runs/"):
            _, _, run_id, *route = path.split("/")
            run = task_runs.get(run_id)
            if not run:
                run = next((row for rows in self.instance_runs.values() for row in rows if row["id"] == run_id), None)
            if not run:
                return self.send_json({"detail": "运行不存在"}, 404)
            if route == ["warning"] and method == "PATCH":
                if not isinstance(body, dict) or not isinstance(body.get("dismissed"), bool):
                    return self.send_json({"detail": "dismissed 必须为布尔值"}, 422)
                key = (scenario, run_id)
                if scenario == "cancel-review-failure" and key not in self.warning_failures:
                    self.warning_failures.add(key)
                    return self.send_json({"detail": "模拟提醒状态暂时无法保存"}, 503)
                self.warning_dismissals[key] = body["dismissed"]
                return self.send_json({"id": run_id, "warning_dismissed": body["dismissed"]})
            if route == ["review-shutdown"] and method == "POST":
                current = self.with_run_status(run, scenario)
                if run["kind"] == "shutdown" and run.get("shutdown_reviewable"):
                    keep_at = body.get("keep_stop_at")
                    evidence = current["result"].get("shutdown_review", {})
                    if keep_at is not None and (keep_at != evidence.get("cloud_shutdown_at") or evidence.get("outcome") != "different"):
                        return self.send_json({"detail": "请先核对当前云端预约"}, 422)
                    self.shutdown_reviews[(scenario, run_id)] = (time.monotonic() + 1, now() + 1, keep_at)
                    return self.send_json(self.with_run_status(run, scenario), 202)
                if run["kind"] == "cancel_shutdown" and (
                    current.get("phase") == "review_cancel_shutdown" or current["state"] == "reviewed"
                ):
                    return self.send_json(current, 202)
                if run["kind"] != "cancel_shutdown" or current["state"] != "manual_review":
                    return self.send_json({"detail": "此运行无法重新核对云端预约"}, 422)
                self.shutdown_reviews[(scenario, run_id)] = (time.monotonic() + 1, now() + 1, None)
                return self.send_json(self.with_run_status(run, scenario), 202)
            if route == ["recheck"] and method == "POST" and run_id == "sample-readback":
                self.instance_rechecks[(scenario, run_id)] = (time.monotonic() + 1, now() + 1)
                return self.send_json(self.with_run_status(run, scenario), 202)
            if route == ["reconcile"] and method == "POST":
                if body["action"] == "bind":
                    run.update(phase="manual_bind", state="reconciling", reason="正在回读手工指定的实例")
                    run["result"]["manual_instance_id"] = body["instance_id"]
                else:
                    if body.get("confirmed_no_order") is not True or not str(body.get("note") or "").strip():
                        return self.send_json({"detail": "请确认平台订单并填写核对说明"}, 422)
                    run.update(phase=None, state="failed", reason="已按订单记录确认未成交并释放预留")
                    run["result"]["purchase_review"] = {"outcome": "not_purchased", "checked_at": now()}
                run["audit"].append(body)
            response = self.business_run(run, scenario)
            return self.send_json(response if not route else {
                key: value for key, value in response.items() if key not in {"audit", "operations"}
            })
        if path.startswith("/tasks/"):
            _, _, task_id, *route = path.split("/")
            task = tasks.get(task_id)
            if not task:
                return self.send_json({"detail": "任务不存在"}, 404)
            if not route and method == "GET":
                return self.send_json(task)
            if route and method == "POST":
                action = route[0]
                if action == "trigger":
                    run = review_run() | {"id": f"run-{next(self.sequence)}", "name": task["name"],
                                          "task_id": task_id, "state": "queued", "reason": None,
                                          "project_id": task["project_id"], "kind": task["kind"], "phase": None}
                    self.task_runs[run["id"]] = run
                    return self.send_json(run)
                task["status"] = {"pause": "paused", "resume": "active", "cancel": "canceled"}[action]
                for run in self.task_runs.values():
                    if run.get("task_id") == task_id:
                        run["stopped"] = int(action != "resume")
                return self.send_json(task)
        if (path == "/tasks" and method == "POST") or (path.startswith("/tasks/") and method == "PUT"):
            task_id = path.split("/")[-1] if method == "PUT" else f"task-{next(self.sequence)}"
            token = body.pop("preview_token", None)
            existing = tasks.get(task_id, {})
            same_schedule = existing.get("schedule") == body["schedule"]
            task = body | {"id": task_id, "next_at": existing.get("next_at") if same_schedule else body["schedule"]["at"],
                           "status": existing["status"] if same_schedule else "active", "version": existing.get("version", 0) + 1}
            if task_id == REPAIR_TASK["id"]:
                if self.previews.get(task_id) != (token, existing["version"], body):
                    return self.send_json({"detail": "任务配置或版本已变化，请重新预览后保存"}, 422)
                self.repair_tasks[task_id] = task
                run = self.action_runs["sample-budget-review"]
                run.update(state="reviewed", reason="已保存经预览的新任务配置", updated_at=now())
                run["result"]["task_config_resolution"] = {"task_version": task["version"], "checked_at": now()}
                return self.send_json(task)
            self.tasks[task_id] = task
            return self.send_json(task)
        return self.send_json({"detail": "模拟任务接口未配置"}, 404)

    def handle_inventory(self, path, project, scenario, body):
        method = self.command
        if path == "/catalog" and method == "GET":
            return self.send_json(snapshot(CREATE_CATALOG if scenario == "instance-create" else CATALOG))
        if path == "/stock" and method == "GET":
            watches = self.watches | self.action_watches if scenario == "actionable" else self.watches
            rows = [watch for watch in watches.values() if watch["project_id"] == project]
            return self.send_json(snapshot(rows))
        if path == "/watches" and method == "POST":
            watch_id = f"watch-{next(self.sequence)}"
            watch = body | {"id": watch_id, "available": None, "success_at": None, "error": None, "history": []}
            self.watches[watch_id] = watch
            return self.send_json(watch)
        if path.startswith("/watches/") and method == "DELETE":
            self.watches.pop(path.rsplit("/", 1)[1], None)
            if scenario == "actionable":
                self.action_watches.pop(path.rsplit("/", 1)[1], None)
            return self.send_json({})
        if path == "/templates" and method == "GET":
            templates = list(self.templates.values())
            if scenario == "actionable":
                templates.append(REPAIR_TEMPLATE)
            return self.send_json([item for item in templates if item["project_id"] == project])
        if (path == "/templates" and method == "POST") or (path.startswith("/templates/") and method == "PUT"):
            template_id = path.rsplit("/", 1)[1] if method == "PUT" else f"template-{next(self.sequence)}"
            template = body | {"id": template_id, "version": self.templates.get(template_id, {}).get("version", 0) + 1}
            self.templates[template_id] = template
            return self.send_json(template)
        return self.send_json({"detail": "模拟库存接口未配置"}, 404)


    def site_business(self, row, scenario):
        project = row["project_id"]
        payload = row["payload"]
        if scenario == "actionable":
            run = self.action_runs.get(payload.get("run_id"))
            if run and run["project_id"] == project:
                return self.business_run(run, scenario)["business"]
            if row["event_type"] == "stock":
                destination = {"page": "inventory", "project_id": project}
                watch = self.action_watches.get(payload.get("watch_id"))
                if watch:
                    destination["watch_id"] = watch["id"]
                else:
                    destination["spec"] = payload.get("spec", SPEC)
                return {"status": "completed", "label": "已发现库存", "observed_at": now() - 10,
                        "destination": destination}
            if row["event_type"] == "idle":
                return {"status": "unknown", "label": "空闲状态待核实", "detail": "模拟采样有缺口",
                        "destination": {"page": "instances", "project_id": project, "instance_id": payload["instance_id"]}}
        return {"status": "history", "label": "测试通知" if row["event_type"] == "test" else "历史记录"}

    def handle_notifications(self, path, scenario, parameters, body):
        notices = self.notifications | self.action_notifications if scenario == "actionable" else self.notifications
        if path == "/notifications" and self.command == "GET":
            channel = parameters.get("channel", [None])[0]
            limit = int(parameters.get("limit", [100])[0])
            cursor = int(parameters["cursor"][0]) if "cursor" in parameters else None
            fields = {name: parameters[name][0] for name in ("project_id", "event_type", "event_id") if name in parameters}
            sorted_rows = (notices[id_] for id_ in sorted(notices, reverse=True))
            records = [
                row for row in sorted_rows if (cursor is None or row["id"] < cursor)
                and (channel is None or row["channel"] == channel or (channel == "external" and row["channel"] != "site"))
                and all(row[name] == value for name, value in fields.items())
            ]
            items = [row | {"business": self.site_business(row, scenario)} if row["channel"] == "site" else row
                     for row in records[:limit]]
            if parameters.get("paged") == ["true"] or channel or fields or cursor is not None:
                return self.send_json({"items": items, "next_cursor": items[-1]["id"] if len(records) > limit else None})
            return self.send_json(items)
        if path == "/notifications/test" and self.command == "POST":
            id_ = next(self.sequence) + 1000
            event_id = f"test-{id_}"
            channel = body["channel"]
            self.notifications[id_] = notification(
                id_, event_id, channel, title="模拟测试通知",
                event_type="test", project_id=None, state="delivered" if channel == "site" else "pending",
            )
            return self.send_json({"event_id": event_id}, 202)
        if self.command == "POST":
            _, _, id_text, action = path.split("/")
            row = notices.get(int(id_text))
            if row and action == "retry" and row["channel"] != "site" and row["state"] == "failed":
                row.update(state="pending", next_at=now(), error=None, attempts=0)
                return self.send_json({"queued": True}, 202)
            if row and action == "read" and row["channel"] == "site":
                row["read_at"] = row["read_at"] or now()
                return self.send_json({"read": True})
        return self.send_json({"detail": "模拟通知接口未配置"}, 404)

    def handle_refresh(self, path, scenario, body):
        if path == "/refresh" and self.command == "POST":
            kind = body.get("kind")
            project = body.get("project_id")
            errors = {}
            if kind == "projects":
                scopes = [""]
                if scenario == "actionable-refresh" and next(self.project_refreshes) == 1:
                    errors[""] = "模拟本次项目同步失败"
            elif kind in {"catalog", "pool"}:
                scopes = [""]
            elif kind == "instances" and project:
                scopes = [project]
            elif kind == "stock" and project:
                watch_id = body.get("watch_id")
                watches = self.watches | self.action_watches if scenario == "actionable" else self.watches
                scopes = [
                    id_ for id_, watch in watches.items()
                    if watch["project_id"] == project and (watch_id is None or watch_id == id_)
                ]
                if watch_id and not scopes:
                    return self.send_json({"detail": "此项目下没有该关注项"}, 404)
                if "sample-other-watch" in scopes:
                    errors["sample-other-watch"] = "模拟该规格采集超时"
            else:
                return self.send_json({"detail": "同步请求参数无效"}, 422)
            id_ = f"refresh-{next(self.sequence)}"
            self.refreshes[id_] = {"scenario": scenario, "kind": kind, "project_id": project,
                                   "scopes": scopes, "errors": errors, "ready_at": time.monotonic() + 1,
                                   "requested_at": now()}
            return self.send_json({"queued": True, "id": id_}, 202)
        id_ = path.rsplit("/", 1)[-1]
        batch = self.refreshes.get(id_)
        if self.command == "GET" and batch and batch["scenario"] == scenario:
            ready = time.monotonic() >= batch["ready_at"] or not batch["scopes"]
            outcomes = [
                {"scope": scope, "finished_at": batch["requested_at"] + 1 if ready else None,
                 "error": batch["errors"].get(scope) if ready else None} for scope in batch["scopes"]
            ]
            state = "queued" if not ready else ("failed" if batch["errors"] else "succeeded")
            return self.send_json({"id": id_, "state": state, "kind": batch["kind"], "project_id": batch["project_id"],
                                   "requested_at": batch["requested_at"], "outcomes": outcomes})
        return self.send_json({"detail": "同步请求不存在或已经过期"}, 404)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path, help="Vite production output")
    parser.add_argument("--port", type=int, default=4178)
    options = parser.parse_args()
    if not (options.dist / "index.html").is_file():
        parser.error("dist/index.html does not exist")
    with ThreadingHTTPServer(("127.0.0.1", options.port), partial(FixtureHandler, directory=options.dist)) as server:
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
