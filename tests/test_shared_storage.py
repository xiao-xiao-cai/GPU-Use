from decimal import Decimal

import pytest

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.application.tasks import TaskStore, insert_run
from gpu_console.worker import tasks
from test_compshare import SPEC, Wire, client
from test_task_runner import NOW, Clock, FakeCloud


SCOPE = {key: SPEC[key] for key in ("project_id", "region", "zone")} | {"zone_id": 5001}
CREATE = {"mode": "cfs_create", "name": "training", "size_gib": 50,
          "charge_type": "Month", "quantity": 2, "max_price": "8"}
RESIZE = {"mode": "cfs_resize", "cfs_id": "cfs-test", "size_gib": 70, "max_price": "8"}
RECORD = {"CfsId": "cfs-test", "Name": "training", "Size": 50, "ZoneId": 5001,
          "ChargeType": "Month", "CreateTime": NOW, "Found": True, "MountedUHostIds": ["cpod-one"]}


def test_cfs_adapter_scopes_quotes_receipts_and_preserves_paid_operation_contract():
    wire = Wire(
        {"Found": False, "TotalCount": 1, "CFSSet": [RECORD]},
        {"Found": True, "CFSSet": [RECORD], **RECORD},
        {"PriceDetails": [{"ChargeType": "Year", "Disks": 40},
                          {"ChargeType": "Month", "Disks": "4.25", "Quantity": 2}]},
        {"CfsId": "cfs-test", "ZoneId": 5001, "Size": 50, "Name": "training"},
        {"Price": 6},
        {"CfsId": "cfs-test", "OldSize": 50, "NewSize": 70},
    )
    cloud = client(wire)
    (row,) = cloud.describe_cfs(SCOPE)
    assert row == {"id": "cfs-test", "name": "training", "size_gib": 50, "zone_id": 5001,
                   "charge_type": "Month", "created_at": NOW, "mounted_instance_ids": ["cpod-one"]}
    assert cloud.describe_cfs(SCOPE, "cfs-test") == [row]
    assert cloud.quote_cfs(SCOPE, CREATE) == Decimal("4.25")
    assert cloud.create_cfs(SCOPE, CREATE, "run-one") == "cfs-test"
    assert cloud.quote_cfs_resize(SCOPE, RESIZE) == Decimal(6)
    assert cloud.resize_cfs(SCOPE, RESIZE, "run-one") == "cfs-test"
    actions = [request["Action"] for request, _ in wire.sent]
    assert actions == ["DescribeCFS", "DescribeCFS", "GetCompShareCFSPrice", "CreateCFS",
                       "GetCompShareCFSUpgradePrice", "ResizeCFS"]
    for request, retries in wire.sent:
        assert retries == 0
        assert {key: request[key] for key in ("ProjectId", "Region", "Zone")} == {
            "ProjectId": SCOPE["project_id"], "Region": SCOPE["region"], "Zone": SCOPE["zone"],
        }
    for index in (2, 3):
        request = wire.sent[index][0]
        assert {key: request[key] for key in ("Size", "ChargeType", "Quantity")} == {
            "Size": "50", "ChargeType": "Month", "Quantity": "2",
        }
        assert ("Name" in request) == (index == 3)
    for index in (4, 5):
        request = wire.sent[index][0]
        assert request["CfsId"] == "cfs-test" and request["Size"] == "70"
        assert "ChargeType" not in request and "Quantity" not in request


@pytest.mark.parametrize("record", [RECORD | {"ZoneId": 5002}, RECORD | {"CfsId": "cfs-other"},
                                    RECORD | {"ProjectId": "org-other"}])
def test_cfs_detail_rejects_conflicting_scope(record):
    cloud = client(Wire({"Found": True, "CFSSet": [record]}))
    with pytest.raises(CloudError, match="项目、可用区"):
        cloud.describe_cfs(SCOPE, "cfs-test")


@pytest.mark.parametrize("price", [None, True, -1, "NaN", "Infinity"])
def test_cfs_price_requires_explicit_nonnegative_amount(price):
    cloud = client(Wire({"PriceDetails": [{"ChargeType": "Month", "Disks": price}]}))
    with pytest.raises(CloudError) as error:
        cloud.quote_cfs(SCOPE, CREATE)
    assert error.value.code == "quote_unverified"


@pytest.mark.parametrize(("intent", "key"), [({"mode": "us3"}, "EnableUS3"), (CREATE, "EnableCfs"),
                                               (RESIZE, "EnableCfs")])
def test_instance_creation_mount_flags_are_exact(intent, key):
    wire = Wire({"UHostIds": ["cpod-one"]})
    client(wire).create(SPEC | {"shared_storage": intent}, "run-mount")
    request = wire.sent[0][0]
    assert request[key] == "true"
    assert not ({"EnableUS3", "EnableCfs"} - {key}) & request.keys()
    assert "CfsId" not in request and "Size" not in request


class CfsCloud(FakeCloud):
    def __init__(self):
        super().__init__()
        self.record = None
        self.cfs_error = None
        self.cfs_price = Decimal("4")

    def describe_cfs(self, spec, cfs_id=None):
        self.reads.append(("cfs_describe", spec["project_id"], cfs_id))
        if self.record is None or cfs_id and self.record["id"] != cfs_id:
            return []
        return [self.record]

    def quote_cfs(self, spec, intent):
        self.reads.append(("cfs_quote", intent))
        return self.cfs_price

    def create_cfs(self, spec, intent, run_id):
        self.writes.append(("cfs_create", spec["zone"], run_id))
        if self.cfs_error:
            raise self.cfs_error
        return "cfs-test"

    def quote_cfs_resize(self, spec, intent):
        return self.cfs_price

    def resize_cfs(self, spec, intent, run_id):
        self.writes.append(("cfs_resize", spec["zone"], run_id))
        if self.cfs_error:
            raise self.cfs_error
        return "cfs-test"


@pytest.fixture
def worker(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "state.db", Clock())
    storage.initialize(store.path)
    with storage.connect(store.path) as conn, storage.transaction(conn):
        storage.set_settings(conn, {"task_stock_seconds": 1})
    monkeypatch.setattr(tasks, "verify_purchase_spec", lambda *_: None)
    return store, CfsCloud()


def _run(store, *, intent=CREATE, retry=True, project_id=SPEC["project_id"], window=1200, reuse=False):
    config = SPEC | {
        "zones": [{key: SCOPE[key] for key in ("region", "zone", "zone_id")}],
        "max_price": "5", "skip_if_match": reuse, "purchase_limit": 1, "shared_storage": intent,
    }
    snapshot = {"name": "测试购置", "kind": "acquire", "project_id": project_id, "config": config,
                "window_seconds": window, "channels": [], "purchase_retry": retry}
    with storage.connect(store.path) as conn, storage.transaction(conn):
        return insert_run(conn, snapshot, NOW, NOW, request_id="action:" + project_id)


def _record(**changes):
    return {"id": "cfs-test", "name": "training", "size_gib": 50, "zone_id": 5001,
            "charge_type": "Month", "created_at": NOW, "mounted_instance_ids": []} | changes


@pytest.mark.parametrize("mounted,expected", [(["matched"], "succeeded"), (None, "waiting")])
def test_reuse_checks_existing_cfs_mount_before_buying(worker, mounted, expected):
    store, cloud = worker
    run = _run(store, intent={"mode": "cfs_attach", "cfs_id": "cfs-test"}, reuse=True)
    cloud.instances_by_region = [SPEC | {"id": "matched", "state": "Running", "gpu_mode": "with_gpu"}]
    cloud.record = _record(mounted_instance_ids=mounted)
    tasks.TaskRunner(store, cloud).tick()
    current = store.get_run(run["id"])
    assert current["state"] == expected
    if expected == "succeeded":
        assert current["result"]["disposition"] == "reused"
    assert ("cfs_describe", SPEC["project_id"], "cfs-test") in cloud.reads
    assert cloud.writes == []


def test_existing_mount_does_not_skip_the_requested_resize(worker):
    store, cloud = worker
    run = _run(store, intent=RESIZE, reuse=True)
    cloud.instances_by_region = [SPEC | {"id": "matched", "state": "Running", "gpu_mode": "with_gpu"}]
    cloud.record = _record(mounted_instance_ids=["matched"])
    tasks.TaskRunner(store, cloud).tick()
    assert store.get_run(run["id"])["phase"] == "verify_cfs"
    assert [write[0] for write in cloud.writes] == ["cfs_resize"]


def test_uncertain_cfs_create_blocks_other_projects_and_resumes_from_readback(worker):
    store, cloud = worker
    run = _run(store)
    cloud.cfs_error = CloudError("transport_error", uncertain=True)
    runner = tasks.TaskRunner(store, cloud)
    runner.tick()
    current = store.get_run(run["id"])
    assert current["phase"] == "verify_cfs"
    assert current["operations"][0]["state"] == "reserved"
    assert [write[0] for write in cloud.writes] == ["cfs_create"]

    other = _run(store, project_id="org-other")
    # Restarting the runner does not grant permission to repeat the order.
    runner = tasks.TaskRunner(store, cloud)
    store.clock.advance()
    runner.tick()
    assert store.get_run(other["id"])["state"] == "waiting"
    assert [write[0] for write in cloud.writes] == ["cfs_create"]

    cloud.record = _record()
    store.clock.advance()
    runner.tick()
    assert store.get_run(run["id"])["result"]["cfs_id"] == "cfs-test"
    runner.tick()
    assert [write[0] for write in cloud.writes] == ["cfs_create", "create"]
    cloud.instances_by_region = [SPEC | {"id": "created1", "state": "Running", "gpu_mode": "with_gpu"}]
    store.clock.advance()
    runner.tick()
    current = store.get_run(run["id"])
    assert current["state"] == "reconciling" and current["result"]["storage_state"] == "attachment_pending"
    cloud.record = _record(mounted_instance_ids=["created1"])
    store.clock.advance()
    runner.tick()
    current = store.get_run(run["id"])
    assert current["state"] == "succeeded" and current["result"]["storage_state"] == "attached"
    assert [write[0] for write in cloud.writes] == ["cfs_create", "create"]


@pytest.mark.parametrize(("gpu_price", "cfs_price", "stock"), [
    (Decimal(6), Decimal(4), True), (Decimal(4), Decimal(9), True), (Decimal(4), Decimal(4), False),
])
def test_shared_storage_requires_gpu_capacity_and_both_budgets(worker, gpu_price, cfs_price, stock):
    store, cloud = worker
    run = _run(store, retry=False)
    cloud.quoted, cloud.cfs_price, cloud.stock = gpu_price, cfs_price, stock
    tasks.TaskRunner(store, cloud).tick()
    assert store.get_run(run["id"])["state"] == "failed"
    assert cloud.writes == []


def test_readback_finishing_at_deadline_does_not_place_gpu_order(worker):
    store, cloud = worker
    run = _run(store, intent=RESIZE, window=16)
    cloud.record = _record()
    runner = tasks.TaskRunner(store, cloud)
    runner.tick()
    assert [write[0] for write in cloud.writes] == ["cfs_resize"]
    cloud.record = _record(size_gib=70)
    store.clock.advance()
    runner.tick()
    current = store.get_run(run["id"])
    assert current["state"] == "expired" and current["result"]["storage_size_gib"] == 70
    assert [write[0] for write in cloud.writes] == ["cfs_resize"]


def test_unacknowledged_cfs_send_times_out_to_review_and_keeps_account_zone_reserved(worker):
    store, cloud = worker
    run = _run(store)
    cloud.cfs_error = RuntimeError("process left before the receipt was recorded")
    with pytest.raises(RuntimeError):
        tasks.TaskRunner(store, cloud).tick()
    runner = tasks.TaskRunner(store, cloud)
    store.clock.advance(600)
    runner.tick()
    current = store.get_run(run["id"])
    assert current["state"] == "manual_review" and current["phase"] == "verify_cfs"
    assert current["result"]["storage_state"] == "unverified"
    assert current["operations"][0]["state"] == "reserved"

    another = _run(store, project_id="org-other", retry=False)
    runner.tick()
    assert store.get_run(another["id"])["state"] == "failed"
    assert [write[0] for write in cloud.writes] == ["cfs_create"]


def test_explicit_cfs_review_reads_once_and_records_success_without_expired_gpu_order(worker):
    store, cloud = worker
    run = _run(store, window=120)
    cloud.cfs_error = CloudError("transport_error", uncertain=True)
    runner = tasks.TaskRunner(store, cloud)
    runner.tick()
    store.clock.advance(600)
    runner.tick()

    cloud.reads.clear()
    store.queue_instance_recheck(run["id"])
    runner.tick()
    current = store.get_run(run["id"])
    assert current["state"] == "manual_review" and current["phase"] == "verify_cfs"
    assert current["result"]["instance_recheck_pending"] is False
    assert cloud.reads == [("cfs_describe", SPEC["project_id"], None)]

    cloud.record = _record()
    store.queue_instance_recheck(run["id"])
    runner.tick()
    current = store.get_run(run["id"])
    assert current["state"] == "expired" and current["phase"] is None
    assert current["result"]["instance_recheck_pending"] is False
    assert current["result"]["cfs_id"] == cloud.record["id"]
    assert current["operations"][0]["state"] == "verified"
    assert cloud.reads == [("cfs_describe", SPEC["project_id"], None)] * 2
    assert [write[0] for write in cloud.writes] == ["cfs_create"]
