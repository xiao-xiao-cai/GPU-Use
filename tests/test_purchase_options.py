import json

import pytest

from gpu_console import storage
from gpu_console.application.purchase_options import (
    cfs_scope, create_options, normalize_purchase_config, normalize_purchase_spec, record_boot_disk, verify_purchase_spec,
)
from gpu_console.application.task_config import validate_spec

PROJECT = "project-test"
CLOUD_ZONE = {"region": "cn-wlcb", "zone": "cn-wlcb-01"}
POD_ZONE = {"region": "cn-sh2", "zone": "cn-sh2-01"}
SYSTEM = {"purpose": "system", "type": "CLOUD_SSD", "size_gb": 100}
DATA = {"purpose": "data", "type": "CLOUD_SSD", "size_gb": 120}
SPEC = {
    **CLOUD_ZONE, "gpu_model": "5090", "gpu": 1, "cpu": 16, "memory_gb": 96,
    "cpu_platform": "Intel/EmeraldRapids", "image_id": "image-test", "charge_type": "Postpay", "disks": [],
}


def put(conn, kind, data, scope=""):
    conn.execute("INSERT OR REPLACE INTO snapshots(kind,scope,data,success_at) VALUES (?,?,?,?)",
                 (kind, scope, json.dumps(data), storage.now()))


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state.db"
    storage.initialize(path)
    with storage.connect(path) as conn:
        put(conn, "projects", [{"id": PROJECT}])
        disks = [
            {"purpose": "system", "type": "CLOUD_SSD", "max_size_gb": 1000},
            {"purpose": "data", "type": "CLOUD_SSD", "min_size_gb": 20, "max_size_gb": 1500},
        ]
        put(conn, "catalog", [
            CLOUD_ZONE | {"gpu_model": "5090", "instance_type": "uhost", "disks": disks},
            POD_ZONE | {"gpu_model": "5090", "instance_type": "uhost", "disks": disks},
        ])
        put(conn, "locations", [CLOUD_ZONE | {"is_pod": False}, POD_ZONE | {"is_pod": True}])
        put(conn, "images", [{"id": "image-test", "runtime": "container"}],
            "system:" + cfs_scope(PROJECT, **POD_ZONE))
        yield conn


def confirm_boot_default(db, location):
    record_boot_disk(db, location | SYSTEM | {
        "project_id": PROJECT, "gpu_model": SPEC["gpu_model"], "instance_type": "uhost",
        "image_id": SPEC["image_id"], "source": "https://www.compshare.cn/console",
    })


def test_upper_bound_does_not_supply_a_boot_default(db):
    location = {"region": "future-region", "zone": "future-zone-01"}
    spec = SPEC | location
    put(db, "catalog", [location | {"gpu_model": "5090", "instance_type": "uhost", "disks": [
        {"purpose": "system", "type": "CLOUD_SSD", "max_size_gb": 1000},
    ]}])
    with pytest.raises(ValueError, match="默认系统盘待核实"):
        normalize_purchase_spec(db, PROJECT, spec)
    confirm_boot_default(db, location)
    assert normalize_purchase_spec(db, PROJECT, spec)["disks"] == [SYSTEM]
    with pytest.raises(ValueError, match="默认系统盘待核实"):
        normalize_purchase_spec(db, PROJECT, spec | {"image_id": "different-image"})


def test_confirmed_regional_default_is_used_without_an_explicit_catalog_default(db):
    config = normalize_purchase_spec(db, PROJECT, SPEC | {"disks": [DATA]})
    assert config["disks"] == [SYSTEM, DATA]
    assert config["zones"][0]["resolved_disks"] == [SYSTEM, DATA]
    with pytest.raises(ValueError, match="与已核实"):
        normalize_purchase_spec(db, PROJECT, SPEC | {"disks": [SYSTEM | {"size_gb": 99}]})
    with pytest.raises(ValueError, match="旧|重新预览"):
        verify_purchase_spec(db, PROJECT, SPEC)


def test_each_candidate_zone_has_its_own_confirmed_disk_configuration(db):
    with pytest.raises(ValueError, match="数据盘类型"):
        normalize_purchase_spec(db, PROJECT, SPEC | POD_ZONE | {"disks": [DATA]})
    config = validate_spec(SPEC | {"zones": [CLOUD_ZONE, POD_ZONE]}, purchasing=False) | {
        "zone_storage": [CLOUD_ZONE | {"disks": [DATA]}, POD_ZONE | {"disks": []}],
    }
    resolved = normalize_purchase_config(db, PROJECT, config)
    assert [zone["resolved_disks"] for zone in resolved["zones"]] == [
        [SYSTEM, DATA], [SYSTEM | {"size_gb": 50}],
    ]
    # A server-normalized configuration keeps the explicitly approved disks on revalidation.
    assert normalize_purchase_config(db, PROJECT, resolved) == resolved


def test_regional_default_requires_the_catalog_sku_and_capacity(db):
    row = CLOUD_ZONE | {"gpu_model": "5090", "instance_type": "uhost"}
    alternative = {"purpose": "system", "type": "CLOUD_RSSD", "max_size_gb": 1000}
    standard = {"purpose": "system", "type": "CLOUD_SSD", "max_size_gb": 1000}
    put(db, "catalog", [row | {"disks": [alternative, standard]}])
    assert normalize_purchase_spec(db, PROJECT, SPEC)["disks"] == [SYSTEM]
    put(db, "catalog", [row | {"disks": [alternative, standard | {"max_size_gb": 80}]}])
    with pytest.raises(ValueError, match="默认系统盘待核实"):
        normalize_purchase_spec(db, PROJECT, SPEC)
    put(db, "catalog", [row | {"disks": [standard | {"default_size_gb": 1100}]}])
    with pytest.raises(ValueError, match="默认系统盘待核实"):
        normalize_purchase_spec(db, PROJECT, SPEC)
    put(db, "catalog", [row | {"disks": [alternative | {"default_size_gb": 120}, standard]}])
    assert normalize_purchase_spec(db, PROJECT, SPEC)["disks"] == [
        {"purpose": "system", "type": "CLOUD_RSSD", "size_gb": 120},
    ]


def test_cfs_unknown_is_distinct_from_known_absence_and_paid_storage_needs_one_off_confirmation(db):
    scope = cfs_scope(PROJECT, **POD_ZONE)
    args = (PROJECT, POD_ZONE["region"], POD_ZONE["zone"], SPEC["gpu_model"], "uhost", SPEC["image_id"])
    assert create_options(db, *args, queue_cfs=True)["cfs_known"] is False
    assert db.execute("SELECT scope FROM refresh_requests WHERE kind='cfs'").fetchone()["scope"] == scope
    put(db, "cfs", {"items": [], "checked_at": storage.now()}, scope)
    assert create_options(db, *args)["cfs_known"] is True
    request = SPEC | POD_ZONE | {"shared_storage": {
        "mode": "cfs_create", "name": "training", "size_gib": 100,
        "charge_type": "Month", "quantity": 1, "max_price": "12.50",
    }}
    with pytest.raises(ValueError, match="一次性确认"):
        normalize_purchase_spec(db, PROJECT, request, paid=False)
    config = normalize_purchase_spec(db, PROJECT, request)
    assert config["zones"][0]["shared_storage"]["max_price"] == "12.5"
    put(db, "cfs", {"items": [{"id": "other", "size_gib": 150}], "checked_at": storage.now()}, scope)
    with pytest.raises(ValueError, match="已有"):
        normalize_purchase_spec(db, PROJECT, request)


def test_documented_container_zone_stays_local_and_disallows_vm_and_spot(db):
    args = (PROJECT, POD_ZONE["region"], POD_ZONE["zone"], SPEC["gpu_model"])
    choices = create_options(db, *args, "uhost", SPEC["image_id"])
    assert choices["system_disk_backing"] == "local"
    assert choices["data_disks"] == [] and choices["storage"] == "cfs"
    spot = create_options(db, *args, "spot", SPEC["image_id"])
    assert spot["system_disk"] is None and "抢占" in spot["reason"]
    put(db, "images", [{"id": "vm-test", "runtime": "vm"}], "system:" + cfs_scope(PROJECT, **POD_ZONE))
    vm = create_options(db, *args, "uhost", "vm-test")
    assert vm["system_disk"] is None and "容器镜像" in vm["reason"]
