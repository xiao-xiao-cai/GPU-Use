"""Resolve a console instance and its administrator-pinned SSH host key."""

import base64
import json
import re
from dataclasses import dataclass

from gpu_console import storage
from gpu_console.config import Config
from gpu_console.domain.instance_access import valid_host

TARGETS_FILE = "gpu-ssh-targets.json"
ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
HOST_KEY_TYPES = {"ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa"}


@dataclass(frozen=True)
class Target:
    project_id: str
    instance_id: str
    region: str
    zone: str
    user: str
    host: str
    port: int
    host_key: str

    @property
    def scope(self) -> tuple[str, str]:
        return self.project_id, self.instance_id


def _snapshot(config: Config, project_id: str, instance_id: str) -> tuple[dict, dict, int]:
    with storage.connect(config.db) as conn:
        snapshot = storage.snapshot(conn, "instances", project_id)
        maximum_age = max(180, 3 * storage.get_settings(conn)["collection_seconds"])
    for instance in snapshot["data"]:
        if instance.get("id") == instance_id and instance.get("project_id", project_id) == project_id:
            return instance, snapshot, maximum_age
    if snapshot["success_at"] is None or snapshot["error"]:
        raise ValueError("实例清单待同步，请先刷新该项目实例")
    raise ValueError("该项目下找不到指定实例")


def _address(instance: dict) -> dict:
    ssh = instance.get("ssh")
    if isinstance(ssh, dict):
        host, port, user = ssh.get("host"), ssh.get("port"), ssh.get("user")
        if (
            isinstance(host, str) and host.isascii() and valid_host(host)
            and type(port) is int and 1 <= port <= 65535
            and isinstance(user, str) and USER.fullmatch(user)
        ):
            return {"host": host, "port": port, "user": user}
    raise ValueError("当前实例没有可用的 SSH 地址，请刷新实例清单")


def connection_info(config: Config, project_id: str, instance_id: str) -> dict:
    instance, snapshot, _ = _snapshot(config, project_id, instance_id)
    return {
        "project_id": project_id, "instance_id": instance_id, **_address(instance),
        "login_mode": instance.get("login_mode"), "instance_state": instance.get("state"),
        "sampled_at": snapshot["success_at"],
    }


def _key(value: object) -> str:
    parts = value.split(maxsplit=2) if isinstance(value, str) else []
    if len(parts) < 2 or parts[0] not in HOST_KEY_TYPES or len(parts[1]) > 4096:
        raise ValueError
    algorithm = parts[0].encode("ascii")
    data = base64.b64decode(parts[1], validate=True)
    if not data.startswith(len(algorithm).to_bytes(4, "big") + algorithm):
        raise ValueError
    return " ".join(parts[:2])


def pinned_key(config: Config, project_id: str, instance_id: str) -> str:
    try:
        with (config.secrets_dir / TARGETS_FILE).open("rb") as file:
            raw = file.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError
        entries = json.loads(raw)
        if not isinstance(entries, list):
            raise ValueError
        keys = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"project_id", "instance_id", "host_key"}:
                raise ValueError
            scope = entry["project_id"], entry["instance_id"]
            if not all(isinstance(value, str) and ID.fullmatch(value) for value in scope) or scope in keys:
                raise ValueError
            keys[scope] = _key(entry["host_key"])
    except FileNotFoundError:
        keys = {}
    except (OSError, ValueError, RecursionError):
        raise ValueError("SSH 目标配置无效或无法读取，请检查管理台主机上的 SSH 注册文件") from None
    key = keys.get((project_id, instance_id))
    if key is None:
        raise ValueError("该实例尚未启用 SSH，请在管理台主机登记实例及其 SSH 主机公钥")
    return key


def resolve_target(config: Config, project_id: str, instance_id: str) -> Target:
    key = pinned_key(config, project_id, instance_id)
    instance, snapshot, maximum_age = _snapshot(config, project_id, instance_id)
    sampled_at = snapshot["success_at"]
    if sampled_at is None or snapshot["error"] or storage.now() - sampled_at > maximum_age:
        raise ValueError("实例连接信息待更新，请先刷新该项目实例")
    if instance.get("state") != "Running":
        raise ValueError("实例需处于运行状态才可连接 SSH")
    mode = instance.get("login_mode")
    if isinstance(mode, str) and mode.lower() in {"keypair", "sshkey"}:
        raise ValueError("该实例使用 SSH 密钥登录，当前 SSH 接入支持平台密码登录")
    region, zone = instance.get("region"), instance.get("zone")
    if not all(isinstance(value, str) and ID.fullmatch(value) for value in (region, zone)):
        raise ValueError("实例地域或可用区待同步，请刷新实例清单")
    return Target(project_id, instance_id, region, zone, **_address(instance), host_key=key)
