"""MCP tools for inspecting, opening and using a worker-owned SSH connection."""

from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from gpu_console.application.ssh_access import connection_info
from gpu_console.application.ssh_rpc import ssh_request
from gpu_console.config import Config
from gpu_console.mcp.auth import require_principal
from gpu_console.mcp.server import ConsoleMCPServer

Id = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
SessionId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{32}$")]
RequestId = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")]
ShellCommand = Annotated[str, Field(min_length=1, max_length=2048, pattern=r"^[^\x00]+$")]
TerminalLine = Annotated[str, Field(min_length=1, max_length=2048)]
TerminalKey = Literal["Enter", "C-c", "C-d", "Escape", "Up", "Down", "Left", "Right", "Tab"]
Lines = Annotated[int, Field(ge=1, le=200)]
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
CONNECTION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
TERMINAL_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def register(server: ConsoleMCPServer, config: Config) -> None:
    async def request(operation: str, **arguments) -> dict[str, Any]:
        owner = require_principal().credential_id
        return await run_in_threadpool(ssh_request, config, owner, operation, **arguments)

    @server.scoped_tool("ssh", name="gpu_get_ssh_connection", annotations=READ)
    def get_connection(project_id: Id, instance_id: Id) -> dict[str, Any]:
        """Get an instance's SSH host, port, username, login mode and snapshot time."""
        return connection_info(config, project_id, instance_id)

    @server.scoped_tool("ssh", name="gpu_ssh_open", annotations=CONNECTION)
    async def open_connection(project_id: Id, instance_id: Id) -> dict[str, Any]:
        """Open or reuse a password-authenticated SSH connection to an enrolled GPU instance."""
        return await request("open", project_id=project_id, instance_id=instance_id)

    @server.scoped_tool("ssh", name="gpu_ssh_exec", annotations=WRITE)
    async def execute(session_id: SessionId, command: ShellCommand, request_id: RequestId) -> dict[str, Any]:
        """Run a short SSH command in a new shell. Inspect the remote state before retrying an unknown outcome."""
        return await request("exec", session_id=session_id, command=command, request_id=request_id)

    @server.scoped_tool("ssh", name="gpu_ssh_terminal_send", annotations=WRITE)
    async def terminal_send(
        session_id: SessionId, request_id: RequestId, line: TerminalLine | None = None, key: TerminalKey | None = None,
    ) -> dict[str, Any]:
        """Send one literal line and Enter, or one key, to the persistent tmux terminal."""
        return await request("terminal_send", session_id=session_id, line=line, key=key, request_id=request_id)

    @server.scoped_tool("ssh", name="gpu_ssh_terminal_read", annotations=TERMINAL_READ)
    async def terminal_read(session_id: SessionId, lines: Lines = 100) -> dict[str, Any]:
        """Read recent lines from the persistent tmux terminal."""
        return await request("terminal_read", session_id=session_id, lines=lines)

    @server.scoped_tool("ssh", name="gpu_ssh_close", annotations=CONNECTION)
    async def close_connection(session_id: SessionId) -> dict[str, Any]:
        """Close the SSH connection and leave the remote tmux terminal available for reconnection."""
        return await request("close", session_id=session_id)
