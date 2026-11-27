import base64
import json
import logging
from stat import S_IMODE

import pytest
from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from gpu_console import storage
from gpu_console.adapters.compshare import CloudError
from gpu_console.api import create_app
from gpu_console.application.ssh_password import SOCKET_NAME, serve_ssh_passwords
from gpu_console.config import Config
from gpu_console.domain.instance_access import safe_instance_url
from test_compshare import PROJECT, ZONE, Wire, client, instance


def test_instance_projects_connection_details_without_credentials(caplog):
    caplog.set_level(logging.INFO)
    wire = Wire({
        "TotalCount": 1,
        "UHostSet": [instance(
            ChargeType="Postpay",
            CpuArch="x86_64",
            OsName="Ubuntu 22.04",
            OsType="Linux",
            LoginMode="Password",
            GraphicsMemory={"Value": 24},
            UsageSeconds=0,
            AutoRenew="No",
            InstancePrice=2.05,
            DiskSet=[{
                "DiskId": "disk-test", "DiskType": "CLOUD_SSD", "Size": 100,
                "IsBoot": True, "Drive": "/", "Encrypted": "false", "Token": "disk-token-test",
            }],
            SshLoginCommand="ssh root@192.0.2.5 -p 23",
            IPSet=[{"Type": "Private", "IP": "192.0.2.5", "Default": "true", "Password": "network-token-test"}],
            Softwares=[
                {"Name": "JupyterLab", "URL": "http://192.0.2.5:8888/lab/workspaces/auto-D?token=jupyter-token-test"},
                {"Name": "FileBrowser", "URL": "https://files.example.test/files#file-token-test"},
                {"Name": "Invalid", "URL": "http://user:application-password-test@192.0.2.5:8888/"},
            ],
        )],
    })
    [row] = client(wire).instances(PROJECT)
    assert row["cpu_arch"] == "x86_64" and row["os_name"] == "Ubuntu 22.04"
    assert row["graphics_memory_gb"] == 24
    assert row["usage_seconds"] == 0 and row["auto_renew"] is False
    assert row["instance_price_per_hour"] == 2.05
    assert row["disks"] == [{
        "id": "disk-test", "type": "CLOUD_SSD", "size_gb": 100,
        "purpose": "system", "drive": "/", "encrypted": False,
    }]
    assert row["ip_addresses"][0]["is_default"] is True
    assert row["ssh"] == {"user": "root", "host": "192.0.2.5", "port": 23, "command": "ssh -p 23 root@192.0.2.5"}
    assert row["software_links"] == [
        {"name": "JupyterLab", "url": "http://192.0.2.5:8888/lab/workspaces/auto-D"},
        {"name": "FileBrowser", "url": "https://files.example.test/files"},
    ]
    exposed = json.dumps(row) + caplog.text
    assert all(secret not in exposed for secret in (
        "example-cloud-password", "browser-secret", "disk-token-test", "network-token-test",
        "jupyter-token-test", "file-token-test", "application-password-test",
    ))


def test_invalid_connection_fields_do_not_break_inventory_or_expose_price_unit():
    wire = Wire({
        "TotalCount": 1,
        "UHostSet": [instance(
            ChargeType="Month", InstancePrice=200,
            SshLoginCommand="sshpass -p fake-ssh-password ssh root@192.0.2.5",
            IPSet=[{"IP": "user:password@192.0.2.5"}], Softwares={"URL": "https://example.test"},
        )],
    })
    [row] = client(wire).instances(PROJECT)
    assert row["instance_price_per_hour"] is None
    assert row["ssh"] is None and row["ip_addresses"] == [] and row["software_links"] is None


@pytest.mark.parametrize("value", [
    "javascript:alert(1)", "//example.test/lab", "https:///lab",
    "https://user:pass@example.test/lab", "https://example.test:0/lab",
    "https://example.test:65536/lab", "https://example.test:/lab",
    "https://example.test\\@elsewhere.test/lab", "https://example.test/lab path",
    "https://example.test/\npath", "https://[::1]extra/lab", "https://[v1.test]/lab",
    "http://0177.0.0.1/lab", "https://%65xample.test/lab",
    "https://example.test/lab?token=secret", "https://example.test/lab#secret",
])
def test_custom_jupyter_link_rejects_unsafe_or_malformed_url(value):
    assert safe_instance_url(value) is None


def test_custom_jupyter_link_preserves_hostname_port_and_workspace():
    url = "https://[2001:db8::1]:8888/lab/workspaces/auto-D"
    assert safe_instance_url(url) == url


def test_instance_detail_and_jupyter_override_are_authenticated_and_project_scoped(tmp_path):
    config = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, True)
    project_a = "/api/instances/same?project_id=proj-a"
    project_b = "/api/instances/same?project_id=proj-b"
    list_a = "/api/instances?project_id=proj-a"
    list_b = "/api/instances?project_id=proj-b"
    override_a = "/api/instances/same/jupyter?project_id=proj-a"
    override_b = "/api/instances/same/jupyter?project_id=proj-b"
    automatic = {"url": "https://auto.example.test:8888/lab", "source": "platform"}
    fallback = {"url": None, "source": None}
    custom = {"url": "http://jupyter.example.test:8888/lab/workspaces/auto-D", "source": "custom"}
    assert safe_instance_url(automatic["url"]) == automatic["url"]

    with TestClient(create_app(config)) as web:
        assert web.get(project_a).status_code == 401
        assert web.get(list_a).status_code == 401
        password = "stub-password-12345"
        with storage.connect(config.db) as conn:
            conn.execute("INSERT INTO administrators VALUES (1,?,?)", ("ops", PasswordHash.recommended().hash(password)))
            rows = [
                ("proj-a", [{"id": "same", "project_id": "proj-a", "software_links": [
                    {"name": " JupyterLab ", "url": automatic["url"]},
                ]}], 42, "等待下次同步"),
                ("proj-b", [{"id": "same", "project_id": "proj-b"}], 43, None),
                ("proj-c", [], 43, None),
            ]
            conn.executemany(
                "INSERT INTO snapshots VALUES ('instances',?,?,?,?)",
                [(project, json.dumps(data), success, error) for project, data, success, error in rows],
            )
            conn.executemany("INSERT INTO metrics VALUES (?,?,?,?,?,?)", [
                ("proj-a", "same", "cpu", "", 100, 0),
                ("proj-a", "same", "cpu", "", 200, 1),
                ("proj-b", "same", "cpu", "", 999, 2),
            ])
        login = web.post("/api/login", json={"username": "ops", "password": password})
        assert login.status_code == 200
        headers = {"x-csrf-token": login.json()["csrf_token"]}

        initial = web.get(project_a)
        assert initial.headers["cache-control"] == "no-store"
        assert initial.json() == {
            "data": {"id": "same", "project_id": "proj-a", "software_links": [
                {"name": " JupyterLab ", "url": automatic["url"]},
            ]},
            "success_at": 42, "error": "等待下次同步", "last_sample_at": 200, "jupyter": automatic,
            "idle_snooze": {"paused": False, "until": None},
        }
        assert web.get(project_b).json()["jupyter"] == fallback
        assert web.get(list_a).json()["data"][0]["jupyter"] == automatic
        assert web.get(list_b).json()["data"][0]["jupyter"] == fallback
        assert web.get(project_b).json()["last_sample_at"] == 999
        assert web.get("/api/instances/same?project_id=proj-c").status_code == 404
        assert web.get("/api/instances/same?project_id=proj-d").status_code == 503

        body = {"url": custom["url"]}
        assert web.put(override_a, json=body).status_code == 403
        assert web.put(override_a, headers=headers, json={"url": custom["url"] + "?token=bad"}).status_code == 422
        assert web.put(override_a, headers=headers, json=body).json() == custom
        assert web.get(project_a).json()["jupyter"] == custom
        assert web.get(project_b).json()["jupyter"] == fallback
        assert web.get(list_a).json()["data"][0]["jupyter"] == custom
        assert web.get(list_b).json()["data"][0]["jupyter"] == fallback
        assert web.delete(override_b, headers=headers).json() == fallback
        assert web.get(project_a).json()["jupyter"] == custom
        assert web.delete(override_a, headers=headers).json() == automatic
        assert web.get(project_a).json()["jupyter"] == automatic
        assert web.get(list_a).json()["data"][0]["jupyter"] == automatic


@pytest.mark.parametrize("row", [
    instance(1),
    instance(ProjectId="wrong-project"),
    instance(Region="wrong-region"),
    instance(Zone="wrong-zone"),
])
def test_ssh_password_rejects_scope_mismatch(row):
    wire = Wire({"UHostSet": [row]})
    with pytest.raises(CloudError, match="平台响应格式无法核实"):
        client(wire).ssh_password(PROJECT, ZONE["region"], ZONE["zone"], "uhost-test-0")


@pytest.mark.parametrize("password", [None, "", False, "not-base64", "AA==", "//8="])
def test_ssh_password_ignores_unusable_provider_value(password):
    wire = Wire({"UHostSet": [instance(Password=password)]})
    assert client(wire).ssh_password(PROJECT, ZONE["region"], ZONE["zone"], "uhost-test-0") is None


def test_ssh_password_requires_explicit_authenticated_request(tmp_path, caplog):
    config = Config(tmp_path / "data", tmp_path / "secrets", tmp_path / "static", False, True)
    instance_id = "uhost-test-0"
    reveal_path = f"/api/instances/{instance_id}/ssh-password"
    reveal = f"{reveal_path}?project_id={PROJECT}"
    snapshot_row = {"id": instance_id, "project_id": PROJECT, **ZONE}
    admin_password = "stub-password-12345"
    fake_ssh_password = "Ssh-Test-Only5+"
    encoded = base64.b64encode(fake_ssh_password.encode()).decode()
    wire = Wire({"UHostSet": [instance(Password=encoded)]}, {"UHostSet": [instance(Password=None)]})
    caplog.set_level(logging.INFO)

    with TestClient(create_app(config)) as web:
        with storage.connect(config.db) as conn:
            conn.execute(
                "INSERT INTO administrators VALUES (1,?,?)",
                ("ops", PasswordHash.recommended().hash(admin_password)),
            )
            conn.executemany(
                "INSERT INTO snapshots VALUES ('instances',?,?,?,NULL)",
                [
                    (PROJECT, json.dumps([snapshot_row]), 42),
                    ("wrong", json.dumps([snapshot_row]), 42),
                    ("key-project", json.dumps([snapshot_row | {"project_id": "key-project", "login_mode": "SSHKey"}]), 42),
                ],
            )
        socket_path = config.data_dir / SOCKET_NAME
        with serve_ssh_passwords(config.data_dir, client(wire).ssh_password):
            assert socket_path.is_socket() and S_IMODE(socket_path.stat().st_mode) == 0o600
            assert web.post(reveal).status_code == 401
            login = web.post("/api/login", json={"username": "ops", "password": admin_password})
            assert login.status_code == 200
            headers = {"x-csrf-token": login.json()["csrf_token"]}
            assert web.post(reveal).status_code == 403
            assert web.get(reveal).status_code == 405
            assert web.post(f"{reveal_path}?project_id=wrong", headers=headers).status_code == 404
            assert web.post(f"{reveal_path}?project_id=key-project", headers=headers).status_code == 404
            assert wire.sent == []
            response = web.post(reveal, headers=headers)
            assert response.status_code == 200 and response.json() == {"password": fake_ssh_password}
            assert response.headers["cache-control"] == "no-store"
            [(params, retries)] = wire.sent
            assert retries == 0
            assert {key: params[key] for key in ("Action", "ProjectId", "Region", "Zone", "UHostIds.0")} == {
                "Action": "DescribeCompShareInstance", "ProjectId": PROJECT, "Region": ZONE["region"],
                "Zone": ZONE["zone"], "UHostIds.0": instance_id,
            }
            assert web.post(reveal, headers=headers).status_code == 404
        assert not socket_path.exists()
        assert web.post(reveal, headers=headers).status_code == 503
        list_body = web.get(f"/api/instances?project_id={PROJECT}").text
        detail_body = web.get(f"/api/instances/{instance_id}?project_id={PROJECT}").text
        with storage.connect(config.db) as conn:
            persisted = conn.execute("SELECT GROUP_CONCAT(data) FROM snapshots").fetchone()[0]
        output = caplog.text + list_body + detail_body + persisted
        assert fake_ssh_password not in output and encoded not in output


def test_instance_price_lookup_sends_explicit_ids_and_projects_only_rates():
    compute = instance(1, ChargeType="Postpay")
    other = instance(2, ChargeType="Postpay")
    wire = Wire(
        {"TotalCount": 2, "UHostSet": [other, compute]},
        {"TotalCount": 2, "UHostSet": [compute | {"InstancePrice": 8.2, "DiskPrice": 0},
                                      other | {"GPU": 2, "InstancePrice": 3.79}]},
    )
    cloud = client(wire)
    rows = cloud.instances(PROJECT)
    assert all(row["instance_price_per_hour"] is None for row in rows)
    assert cloud.instance_prices(PROJECT, rows) == {
        "uhost-test-1": {"instance_price_per_hour": 8.2, "disk_price_per_hour": 0.0, "image_price_per_hour": None},
    }
    request, retries = wire.sent[-1]
    assert request["Action"] == "DescribeCompShareInstance" and request["ProjectId"] == PROJECT
    assert [request["UHostIds.0"], request["UHostIds.1"]] == ["uhost-test-1", "uhost-test-2"]
    assert request["Limit"] == "2" and retries == 0


def test_instance_price_lookup_rejects_duplicate_or_unrequested_instances():
    raw = instance(1, ChargeType="Postpay", InstancePrice=1)
    wire = Wire({"TotalCount": 1, "UHostSet": [raw]},
                {"TotalCount": 2, "UHostSet": [raw, raw]},
                {"TotalCount": 1, "UHostSet": [raw | {"UHostId": "other"}]})
    cloud = client(wire)
    rows = cloud.instances(PROJECT)
    for _ in range(2):
        with pytest.raises(CloudError) as failure:
            cloud.instance_prices(PROJECT, rows)
        assert failure.value.code == "invalid_response"
