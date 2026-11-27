"""Process configuration shared by the web and worker entry points."""

import os
from dataclasses import dataclass
from pathlib import Path


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(value for part in os.getenv(name, default).split(",") if (value := part.strip()))


@dataclass(frozen=True)
class Config:
    data_dir: Path
    secrets_dir: Path
    static_dir: Path
    cookie_secure: bool
    read_only: bool
    scheduler_omits_empty: bool = False
    mcp_bind_ip: str = "127.0.0.1"
    mcp_bind_port: int = 18081
    mcp_trusted_hosts: tuple[str, ...] = ()
    mcp_trusted_origins: tuple[str, ...] = ()
    mcp_release_charge_types: frozenset[str] = frozenset({"Postpay"})
    ssh_proxy: str | None = None
    console_bind_port: int = 18080
    mcp_public_url: str | None = None
    mcp_health_url: str = "http://mcp:8080/healthz"

    @classmethod
    def from_env(cls) -> "Config":
        port = int(os.getenv("GPU_MCP_BIND_PORT") or "18081")
        console_port = int(os.getenv("GPU_BIND_PORT") or "18080")
        if not 1 <= port <= 65535:
            raise ValueError("GPU_MCP_BIND_PORT 必须在 1 到 65535 之间")
        if not 1 <= console_port <= 65535:
            raise ValueError("GPU_BIND_PORT 必须在 1 到 65535 之间")
        return cls(
            data_dir=Path(os.getenv("GPU_DATA_DIR", "data")),
            secrets_dir=Path(os.getenv("GPU_SECRETS_DIR", "secrets")),
            static_dir=Path(os.getenv("GPU_STATIC_DIR", "frontend/dist")),
            cookie_secure=os.getenv("GPU_COOKIE_SECURE") == "1",
            read_only=os.getenv("GPU_READ_ONLY", "0") != "0",
            scheduler_omits_empty=os.getenv("GPU_SCHEDULER_OMITS_EMPTY", "0") == "1",
            mcp_bind_ip=os.getenv("GPU_BIND_IP") or "127.0.0.1",
            mcp_bind_port=port,
            mcp_trusted_hosts=_csv("GPU_MCP_TRUSTED_HOSTS"),
            mcp_trusted_origins=_csv("GPU_MCP_TRUSTED_ORIGINS"),
            mcp_release_charge_types=frozenset(_csv("GPU_MCP_RELEASE_CHARGE_TYPES", "Postpay")),
            ssh_proxy=(os.getenv("GPU_SSH_PROXY") or "").strip() or None,
            console_bind_port=console_port,
            mcp_public_url=os.getenv("GPU_MCP_PUBLIC_URL") or None,
        )

    @property
    def db(self) -> Path:
        return self.data_dir / "console.sqlite3"
