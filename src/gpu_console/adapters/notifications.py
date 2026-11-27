"""Notification channel protocols, with no provider payloads in error messages."""

import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import httpx

from gpu_console.storage.feishu import (
    FeishuAppSettings, FeishuSettings, FeishuSettingsUnavailable, read_feishu_settings, valid_webhook_url,
)

FEISHU_TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
FEISHU_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
FEISHU_TOKEN_REJECTED = {99991663, 99991665}
FEISHU_APP_RETRYABLE = {20050, 230020, 230049, 99991400} | FEISHU_TOKEN_REJECTED
FEISHU_APP_ERROR_HINTS = {
    20002: "请核对 App ID 和 App Secret",
    20009: "请确认应用已发布并安装",
    20048: "请核对 App ID",
    20069: "请确认应用已发布并启用",
    20130: "请核对 App ID 和 App Secret，以及应用的认证方式",
    20140: "请检查应用启用的客户端认证方式",
    230002: "请将应用机器人添加到接收消息的群",
    230006: "请在应用中启用机器人能力并发布",
    230013: "请将接收人加入应用可用范围并发布",
    230018: "请确认机器人在群内有发言权限",
    230027: "请确认应用已获得以应用身份发消息的权限",
    230034: "请核对接收者类型和账号标识",
    230035: "请检查群发言权限或接收人的消息设置",
    230053: "请在飞书中恢复接收该机器人的单聊消息",
    99991401: "请检查应用的 IP 白名单设置",
    99991662: "请确认应用已发布并启用",
    99991672: "请申请以应用身份发消息的权限并发布",
    99992351: "请核对接收人的 Open ID 是否来自当前应用",
    99992356: "请核对群 ID 和机器人的群成员身份",
}


class DeliveryError(Exception):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def read_values(directory: Path, *names: str) -> tuple[str, ...] | None:
    try:
        values = tuple((directory / name).read_text().strip() for name in names)
    except (OSError, UnicodeError):
        return None
    return values if all(values) else None


class Channels:
    def __init__(self, directory: Path, *, console_origin: str | None = None):
        self.directory = directory
        self.console_origin = console_origin
        legacy = read_values(directory, "feishu_webhook_url", "feishu_sign_secret")
        keyword, = read_values(directory, "feishu_keyword") or ("",)
        self.legacy_feishu = FeishuSettings(*legacy, keyword=keyword) if legacy else None
        self.email = read_values(directory, "aliyun_access_key_id", "aliyun_access_key_secret", "aliyun_mail_sender")
        self._app_token: tuple[tuple[str, str], str, float] | None = None

    def configured(self, channel: str, *, legacy_only: bool = False) -> bool:
        if channel != "feishu":
            return channel == "email" and bool(self.email)
        try:
            config = self.legacy_feishu if legacy_only else self._feishu_settings()
        except FeishuSettingsUnavailable:
            return False
        return isinstance(config, FeishuAppSettings) or (
            config is not None and valid_webhook_url(config.webhook_url)
        )

    def _feishu_settings(self) -> FeishuAppSettings | FeishuSettings | None:
        return read_feishu_settings(self.directory) or self.legacy_feishu

    def send(self, channel: str, payload: dict, *, event_id: str | None = None) -> None:
        classified = channel == "feishu" and bool(payload.get("policy_event"))
        title = str((payload.get("feishu_title") if classified else None) or payload.get("title")
                    or "GPU 实验管理台通知")[:160]
        context = "｜".join(
            str(payload[key])
            for key in ("task_name", "instance_id", "gpu_model", "zone")
            if payload.get(key)
        )
        details = str(payload.get("message") or payload.get("reason") or "")
        confirmed_at = payload.get("confirmed_at")
        timestamp = datetime.fromtimestamp(confirmed_at, ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds") \
            if type(confirmed_at) is int else ""
        project = payload.get("project_id") if classified else None
        run_id = payload.get("run_id") if classified else None
        receipt = None
        if run_id:
            if self.console_origin and project:
                query = urlencode({"project": project, "run": run_id})
                receipt = f"任务回执 {self.console_origin}/#tasks?{query}"
            else:
                receipt = f"任务编号 {run_id}"
        message = "\n".join(part for part in (
            title, f"项目 {project}" if project else None, context, details[:2000] if classified else details,
            timestamp, receipt,
        ) if part)[:4000]
        if channel == "feishu":
            try:
                config = self._feishu_settings()
            except FeishuSettingsUnavailable as error:
                raise DeliveryError(str(error), retryable=False) from None
            if config is None:
                raise DeliveryError("通知渠道未配置", retryable=False)
            if isinstance(config, FeishuAppSettings):
                self._send_feishu_app(config, message, event_id)
            else:
                self._app_token = None
                if config.keyword and config.keyword not in message:
                    message = f"{config.keyword}\n{message}"[:4000]
                self._send_feishu(config, message)
        elif channel == "email" and self.email:
            recipient = payload.get("recipient")
            if not isinstance(recipient, str) or "@" not in recipient or any(char in recipient for char in ",;\n\r"):
                raise DeliveryError("缺少有效的邮件收件地址", retryable=False)
            self._send_email(recipient, title, message)
        else:
            raise DeliveryError("通知渠道未配置", retryable=False)

    def _send_feishu(self, config: FeishuSettings, message: str) -> None:
        if not valid_webhook_url(config.webhook_url):
            raise DeliveryError("飞书机器人地址格式有误", retryable=False)
        body = {"msg_type": "text", "content": {"text": message}}
        if config.signing_secret:
            timestamp = str(int(time.time()))
            signing_key = f"{timestamp}\n{config.signing_secret}".encode()
            signature = base64.b64encode(hmac.new(signing_key, b"", hashlib.sha256).digest()).decode()
            body.update(timestamp=timestamp, sign=signature)
        try:
            response = httpx.post(config.webhook_url, json=body, timeout=8)
            if not response.is_success:
                raise DeliveryError(
                    f"飞书 HTTP {response.status_code}",
                    retryable=response.status_code in (408, 429) or response.status_code >= 500,
                )
            result = response.json()
        except (httpx.HTTPError, ValueError):
            raise DeliveryError("飞书请求失败或回执格式异常") from None
        code = result.get("code", result.get("StatusCode")) if isinstance(result, dict) else None
        if type(code) is not int:
            raise DeliveryError("飞书未返回有效业务回执")
        if code != 0:
            raise DeliveryError(f"飞书业务错误 {code}", retryable=code in (11232, 9499, 99991400))

    def _feishu_app_request(
        self, url: str, operation: str, body: dict[str, str], *,
        params: dict[str, str] | None = None, headers: dict[str, str] | None = None,
    ) -> dict:
        is_token_request = url == FEISHU_TOKEN_URL
        try:
            try:
                response = httpx.post(url, json=body, params=params, headers=headers, timeout=8)
            except httpx.TransportError:
                if not is_token_request:
                    raise
                response = httpx.post(url, json=body, params=params, headers=headers, timeout=8)
        except httpx.HTTPError as error:
            reason = "请求超时" if is_token_request and isinstance(error, httpx.TimeoutException) else "连接失败"
            raise DeliveryError(f"飞书{operation}{reason}") from None
        try:
            result = response.json()
        except ValueError:
            result = None

        code = result.get("code") if isinstance(result, dict) else None
        http_retryable = response.status_code in (408, 429) or response.is_server_error
        if type(code) is int and code != 0:
            if code in FEISHU_TOKEN_REJECTED:
                self._app_token = None
            hint = FEISHU_APP_ERROR_HINTS.get(code)
            detail = f"飞书{operation}失败（{code}）"
            raise DeliveryError(
                f"{detail}，{hint}" if hint else detail,
                retryable=http_retryable or code in FEISHU_APP_RETRYABLE,
            )
        if not response.is_success:
            raise DeliveryError(f"飞书{operation} HTTP {response.status_code}", retryable=http_retryable)
        if type(code) is not int:
            raise DeliveryError(f"飞书{operation}未返回有效业务回执")
        return result

    def _feishu_app_access_token(self, config: FeishuAppSettings) -> str:
        credentials = (config.app_id, config.app_secret)
        timestamp = time.monotonic()
        if self._app_token is not None:
            saved_credentials, token, valid_until = self._app_token
            if saved_credentials == credentials and timestamp < valid_until:
                return token
        self._app_token = None
        result = self._feishu_app_request(FEISHU_TOKEN_URL, "获取访问凭证", {
            "grant_type": "client_credentials", "client_id": config.app_id, "client_secret": config.app_secret,
        })
        token, expires_in = result.get("access_token"), result.get("expires_in")
        if (
            not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/\-]+=*", token)
            or type(expires_in) is not int or not 0 < expires_in <= 7200
        ):
            raise DeliveryError("飞书未返回有效访问凭证")
        self._app_token = (credentials, token, timestamp + expires_in - min(60, expires_in / 10))
        return token

    def _send_feishu_app(self, config: FeishuAppSettings, message: str, event_id: str | None) -> None:
        token = self._feishu_app_access_token(config)
        body = {
            "receive_id": config.receive_id, "msg_type": "text",
            "content": json.dumps({"text": message}, ensure_ascii=False),
        }
        if event_id:
            # Feishu deduplicates retries using the same UUID within one hour.
            key = json.dumps([event_id, config.app_id, config.receive_id_type, config.receive_id])
            body["uuid"] = str(uuid5(NAMESPACE_URL, f"gpu-console:feishu:{key}"))
        result = self._feishu_app_request(
            FEISHU_MESSAGE_URL, "发送消息", body,
            params={"receive_id_type": config.receive_id_type}, headers={"Authorization": f"Bearer {token}"},
        )
        data = result.get("data")
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if not isinstance(message_id, str) or not message_id.strip():
            raise DeliveryError("飞书未返回有效消息回执")

    def _send_email(self, recipient: str, title: str, message: str) -> None:
        from alibabacloud_dm20151123.client import Client
        from alibabacloud_dm20151123.models import SingleSendMailRequest
        from alibabacloud_tea_openapi.models import Config

        access_key, secret, sender = self.email
        try:
            client = Client(Config(
                access_key_id=access_key, access_key_secret=secret, region_id="cn-hangzhou",
                endpoint="dm.aliyuncs.com", protocol="HTTPS", read_timeout=8000, connect_timeout=5000,
            ))
            response = client.single_send_mail(SingleSendMailRequest(
                account_name=sender, address_type=1, reply_to_address=False,
                to_address=recipient, subject=title, text_body=message,
            ))
        except Exception as exc:
            code = getattr(exc, "statusCode", None)
            status = f" HTTP {code}" if isinstance(code, int) else ""
            raise DeliveryError(f"阿里云邮件请求失败{status}") from None
        if response.status_code != 200 or not response.body or not response.body.request_id:
            raise DeliveryError("阿里云邮件未返回有效受理回执")
