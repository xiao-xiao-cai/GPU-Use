"""Private SSH control messages between the MCP web process and worker."""

import socket

from gpu_console.application.ssh_password import receive_local_message, send_local_message
from gpu_console.config import Config

CONTROL_SOCKET = ".ssh-control.sock"
MAX_REPLY = 256 * 1024
RPC_TIMEOUT = 23


def ssh_request(config: Config, credential_id: str, operation: str, **arguments) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(RPC_TIMEOUT)
            peer.connect(str(config.data_dir / CONTROL_SOCKET))
            send_local_message(peer, {"credential_id": credential_id, "operation": operation, **arguments})
            reply = receive_local_message(peer, MAX_REPLY)
    except (OSError, ValueError, RecursionError):
        raise ValueError("SSH 后台响应中断，请先核对实例状态和操作结果") from None
    if isinstance(reply, dict):
        if isinstance(reply.get("error"), str):
            raise ValueError(reply["error"])
        if isinstance(reply.get("result"), dict):
            return reply["result"]
    raise ValueError("SSH 后台响应不可用，请核对操作结果")
