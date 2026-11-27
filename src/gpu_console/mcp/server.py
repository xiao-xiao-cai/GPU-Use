"""Serve the console tools from an independent Streamable HTTP application."""

from contextlib import asynccontextmanager
from ipaddress import ip_address
from typing import Any, Literal
from urllib.parse import urlsplit

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, InputRequiredResult, Tool
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gpu_console import storage
from gpu_console.config import Config
from gpu_console.mcp import auth

MCP_PATH = "/mcp"


class ConsoleMCPServer(MCPServer):
    def __init__(self) -> None:
        super().__init__("gpu_console_mcp", log_level="WARNING")
        self.scopes: dict[str, Literal["read", "manage", "ssh"]] = {}

    def scoped_tool(self, scope: Literal["read", "manage", "ssh"], *, name: str, **kwargs):
        self.scopes[name] = scope
        return self.tool(name=name, **kwargs)

    def _allowed(self, name: str, principal: auth.Principal) -> bool:
        scope = self.scopes.get(name)
        return scope == "read" or (scope == "manage" and principal.manage) or (scope == "ssh" and principal.ssh)

    async def list_tools(self) -> list[Tool]:
        principal = auth.require_principal()
        return [tool for tool in await super().list_tools() if self._allowed(tool.name, principal)]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context | None = None,
    ) -> CallToolResult | InputRequiredResult:
        if not self._allowed(name, auth.require_principal()):
            raise ToolError("此 MCP Key 无权使用该工具")
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as error:
            cause = error.__cause__
            if isinstance(cause, ValidationError):
                fields = sorted({".".join(map(str, item["loc"])) for item in cause.errors(include_input=False)})
                raise ToolError("请检查工具输入字段：" + "、".join(fields[:12])) from None
            if isinstance(cause, HTTPException) and isinstance(cause.detail, str):
                raise ToolError(cause.detail) from None
            if isinstance(cause, (ValueError, KeyError)):
                raise ToolError(str(cause.args[0]) if cause.args else "该操作无法执行") from None
            raise


class MCPBearerAuth:
    def __init__(self, app: ASGIApp, config: Config) -> None:
        self.app = app
        self.db = config.db

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        values = Headers(scope=scope).getlist("authorization")
        scheme, _, token = values[0].partition(" ") if len(values) == 1 else ("", "", "")
        principal = await run_in_threadpool(auth.authenticate, self.db, token) if scheme.lower() == "bearer" else None
        if principal is None:
            response = JSONResponse(
                {"detail": "需要有效的 MCP 令牌"}, status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        context = auth.current_principal.set(principal)
        try:
            async def no_cache(message: Message) -> None:
                if message["type"] == "http.response.start":
                    MutableHeaders(scope=message)["Cache-Control"] = "no-store"
                await send(message)

            await self.app(scope, receive, no_cache)
        finally:
            auth.current_principal.reset(context)


def _host_valid(host: str) -> bool:
    if not host or any(char.isspace() or char in "/*@\\#?" for char in host):
        return False
    try:
        parsed = urlsplit(f"//{host}")
        port = parsed.port
    except ValueError:
        return False
    return parsed.hostname is not None and not host.endswith(":") and (port is None or port > 0)


def _origin_valid(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"} and _host_valid(parsed.netloc)
        and origin == f"{parsed.scheme}://{parsed.netloc}"
    )


def _bind_url(config: Config) -> str:
    address = ip_address(config.mcp_bind_ip)
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"http://{host}:{config.mcp_bind_port}{MCP_PATH}"


def connection_urls(config: Config) -> tuple[str, str]:
    public = config.mcp_public_url or _bind_url(config)
    try:
        url = urlsplit(public)
        valid = (url.path == MCP_PATH and _origin_valid(f"{url.scheme}://{url.netloc}")
                 and public == f"{url.scheme}://{url.netloc}{MCP_PATH}")
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("GPU_MCP_PUBLIC_URL 需填写完整的 HTTP 或 HTTPS MCP 地址，路径为 /mcp")
    return public, f"http://127.0.0.1:{config.mcp_bind_port}{MCP_PATH}"


def _transport_security(config: Config) -> TransportSecuritySettings:
    public, local = connection_urls(config)
    urls = [urlsplit(url) for url in (public, local, _bind_url(config))]
    base_hosts = {url.netloc for url in urls}
    base_origins = {f"{url.scheme}://{url.netloc}" for url in urls}
    for url in urls:
        if url.port == (443 if url.scheme == "https" else 80):
            no_port = url.netloc.rsplit(":", 1)[0]
            base_hosts.add(no_port)
            base_origins.add(f"{url.scheme}://{no_port}")
    if not all(map(_host_valid, config.mcp_trusted_hosts)):
        raise ValueError("GPU_MCP_TRUSTED_HOSTS 需填写完整 Host 值")
    if not all(map(_origin_valid, config.mcp_trusted_origins)):
        raise ValueError("GPU_MCP_TRUSTED_ORIGINS 需填写完整 HTTP 或 HTTPS 源")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(base_hosts) + list(config.mcp_trusted_hosts),
        allowed_origins=sorted(base_origins) + list(config.mcp_trusted_origins),
    )


def register_tools(server: ConsoleMCPServer, config: Config) -> None:
    from gpu_console.mcp import instances, management, queries, ssh

    queries.register(server, config)
    instances.register(server, config)
    management.register(server, config)
    ssh.register(server, config)


def create_mcp_app(config: Config) -> Starlette:
    server = ConsoleMCPServer()
    register_tools(server, config)
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=_transport_security(config),
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        storage.initialize(config.db)
        async with server.session_manager.run():
            yield

    def health(_request):
        with storage.connect(config.db) as conn:
            conn.execute("SELECT key_id FROM mcp_credentials LIMIT 1").fetchone()
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    return Starlette(lifespan=lifespan, routes=[
        Route(MCP_PATH, MCPBearerAuth(app, config)),
        Route("/healthz", health),
    ])
