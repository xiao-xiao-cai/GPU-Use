"""Store the web-managed Feishu bot credentials outside the console database."""

import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from gpu_console.storage.maintenance import process_lock

SETTINGS_DIRECTORY = "feishu-settings"
SETTINGS_FILE = "config.json"
WEBHOOK = re.compile(r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9_-]+")
APP_ID = re.compile(r"cli_[A-Za-z0-9]+")
RECEIVE_IDS = {
    "email": re.compile(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+"),
    "open_id": re.compile(r"ou_[A-Za-z0-9]+"),
    "chat_id": re.compile(r"oc_[A-Za-z0-9]+"),
}
FeishuReceiveIdType = Literal["email", "open_id", "chat_id"]


class FeishuSettingsUnavailable(Exception):
    """A managed configuration exists but cannot be used."""


@dataclass(frozen=True)
class FeishuSettings:
    webhook_url: str = field(repr=False)
    signing_secret: str | None = field(default=None, repr=False)
    keyword: str = ""


@dataclass(frozen=True)
class FeishuAppSettings:
    app_id: str
    app_secret: str = field(repr=False)
    receive_id_type: FeishuReceiveIdType
    receive_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.app_id, str) or len(self.app_id) > 128 or not APP_ID.fullmatch(self.app_id):
            raise ValueError("请输入以 cli_ 开头的飞书 App ID")
        if not isinstance(self.app_secret, str) or not self.app_secret.strip() or len(self.app_secret) > 256:
            raise ValueError("请填写飞书 App Secret")
        pattern = RECEIVE_IDS.get(self.receive_id_type) if isinstance(self.receive_id_type, str) else None
        if (
            pattern is None
            or not isinstance(self.receive_id, str)
            or len(self.receive_id) > 254
            or not pattern.fullmatch(self.receive_id)
        ):
            raise ValueError("请填写与接收方式匹配的飞书接收人标识")


def valid_webhook_url(value: object) -> bool:
    return isinstance(value, str) and WEBHOOK.fullmatch(value) is not None


def read_feishu_settings(secrets_dir: Path) -> FeishuSettings | FeishuAppSettings | None:
    try:
        path = secrets_dir / SETTINGS_DIRECTORY / SETTINGS_FILE
        fields = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(fields, dict):
            raise ValueError
        mode = fields.pop("mode", "webhook")
        if mode == "app":
            return FeishuAppSettings(**fields)
        if mode != "webhook":
            raise ValueError
        config = FeishuSettings(**fields)
        if (
            not valid_webhook_url(config.webhook_url)
            or config.signing_secret is not None and not isinstance(config.signing_secret, str)
            or not isinstance(config.keyword, str)
        ):
            raise ValueError
        return config
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        raise FeishuSettingsUnavailable("无法读取飞书机器人配置，请重新填写并保存") from None


def save_feishu_settings(secrets_dir: Path, changes: dict[str, str | None]) -> None:
    fields = changes.copy()
    mode = fields.pop("mode", "webhook")
    directory = secrets_dir / SETTINGS_DIRECTORY
    with process_lock(directory, ".settings.lock", blocking=True):
        try:
            previous = read_feishu_settings(secrets_dir)
        except FeishuSettingsUnavailable:
            previous = None
        if mode == "app":
            if (
                "app_secret" not in fields
                and isinstance(previous, FeishuAppSettings)
                and fields["app_id"] == previous.app_id
            ):
                fields["app_secret"] = previous.app_secret
            if not fields.get("app_secret"):
                raise ValueError("请填写飞书 App Secret")
            contents = asdict(FeishuAppSettings(**fields)) | {"mode": "app"}
        else:
            webhook = previous if isinstance(previous, FeishuSettings) else FeishuSettings("")
            config = replace(webhook, **fields)
            if not config.webhook_url:
                raise ValueError("请填写飞书群机器人 Webhook 地址")
            if not valid_webhook_url(config.webhook_url):
                raise ValueError("请输入有效的飞书群机器人 Webhook 地址")
            contents = asdict(config)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as pending:
            try:
                json.dump(contents, pending)
                pending.flush()
                os.fsync(pending.fileno())
                os.replace(pending.name, directory / SETTINGS_FILE)
            finally:
                Path(pending.name).unlink(missing_ok=True)
