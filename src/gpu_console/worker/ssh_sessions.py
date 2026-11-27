"""Own authenticated SSH connections independently of the stateless MCP transport."""

import hashlib
import os
import secrets
import socketserver
import sqlite3
from _thread import LockType
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore, Event, Lock, Thread
from time import monotonic

from gpu_console import storage
from gpu_console.adapters.openssh import IDLE_SECONDS, Master, tmux_read, tmux_send
from gpu_console.application.ssh_access import pinned_key, resolve_target
from gpu_console.application.ssh_password import receive_local_message, send_local_message
from gpu_console.application.ssh_rpc import CONTROL_SOCKET
from gpu_console.config import Config
from gpu_console.mcp.auth import is_current

MAX_SESSIONS = 4
MAX_RECEIPTS = 256
BUSY = "SSH 会话正在处理另一个请求，请稍后重试"
GONE = "SSH 会话已结束，请重新建立连接"
REVOKED = "MCP 令牌已失效，请更新客户端凭据"


@dataclass
class Session:
    owner: str
    master: Master
    last_used: float = field(default_factory=monotonic)
    lock: LockType = field(default_factory=Lock)
    receipts: dict[str, tuple[bytes, dict]] = field(default_factory=dict)


class Sessions:
    def __init__(self, config: Config, directory: Path) -> None:
        self.config = config
        self.directory = directory
        self.sessions: dict[str, Session] = {}
        self.lock = Lock()
        self.stopped = False

    def _current(self, owner: str) -> bool:
        with storage.connect(self.config.db) as conn:
            return is_current(conn, owner, ssh=True)

    def _drop(self, session_id: str, session: Session) -> None:
        with self.lock:
            if self.sessions.get(session_id) is session:
                del self.sessions[session_id]
            else:
                return
        session.master.close()

    def reap(self, *, shutdown: bool = False) -> None:
        with self.lock:
            if shutdown:
                self.stopped = True
            entries = tuple(self.sessions.items())
        active = {session.owner: self._current(session.owner) for _, session in entries} if not shutdown else {}
        for session_id, session in entries:
            if not active.get(session.owner):
                self._drop(session_id, session)
            elif session.lock.acquire(blocking=False):
                try:
                    if monotonic() - session.last_used >= IDLE_SECONDS:
                        self._drop(session_id, session)
                finally:
                    session.lock.release()

    def _open(self, owner: str, project_id: str, instance_id: str) -> dict:
        proxy = self.config.ssh_proxy
        if not proxy:
            raise ValueError("SSH 会话需要显式配置 GPU_SSH_PROXY")
        self.reap()
        target = resolve_target(self.config, project_id, instance_id)
        with self.lock:
            if self.stopped:
                raise ValueError("SSH 后台正在退出，请稍后重试")
            if not self._current(owner):
                raise ValueError(REVOKED)
            matching = [
                (session_id, session) for session_id, session in self.sessions.items()
                if session.owner == owner and session.master.target.scope == target.scope
            ]
            for session_id, session in matching:
                if not session.lock.acquire(blocking=False):
                    raise ValueError(BUSY)
                try:
                    if (
                        session.master.target == target
                        and monotonic() - session.last_used < IDLE_SECONDS and session.master.alive()
                    ):
                        session.last_used = monotonic()
                        return {"session_id": session_id, "idle_timeout_seconds": IDLE_SECONDS}
                    session.master.close()
                    del self.sessions[session_id]
                finally:
                    session.lock.release()

            if len(self.sessions) >= MAX_SESSIONS:
                raise ValueError("SSH 会话已达到上限，请关闭一个会话再试")
            session_id = secrets.token_urlsafe(24)
            session = Session(owner, Master(target, self.directory / f"{session_id}.sock"))
            session.lock.acquire()
            self.sessions[session_id] = session

        try:
            session.master.open(self.config.data_dir, proxy)
            if not self._current(owner):
                raise ValueError(REVOKED)
            with self.lock:
                if self.sessions.get(session_id) is not session:
                    raise ValueError(GONE)
                session.last_used = monotonic()
        except BaseException:
            self._drop(session_id, session)
            # A revoked session can be removed while its SSH authentication is still in progress.
            session.master.close()
            raise
        finally:
            session.lock.release()
        return {"session_id": session_id, "idle_timeout_seconds": IDLE_SECONDS}

    def _run(self, session: Session, operation: str, command: str, request_id: str | None) -> dict:
        fingerprint = hashlib.sha256(f"{operation}\0{command}".encode()).digest()
        if request_id is not None and request_id in session.receipts:
            previous, result = session.receipts[request_id]
            if fingerprint != previous:
                raise ValueError("request_id 已关联另一项 SSH 操作，请使用新的标识")
            return result
        if not session.master.alive():
            raise ValueError(GONE)
        if request_id is not None and len(session.receipts) >= MAX_RECEIPTS:
            raise ValueError("此 SSH 会话的操作记录已满，请关闭会话后重新连接")
        # Reserve the request result before sending input so a post-send failure cannot replay it.
        unknown = {"state": "unknown", "exit_code": None, "stdout": "", "stderr": "", "truncated": False}
        if request_id is not None:
            session.receipts[request_id] = fingerprint, unknown
        try:
            result = session.master.run(command)
        except OSError:
            result = unknown
        if request_id is not None:
            session.receipts[request_id] = fingerprint, result
        return result

    def dispatch(
        self, credential_id: str, operation: str, *, project_id: str = "", instance_id: str = "",
        session_id: str = "", command: str = "", request_id: str | None = None,
        line: str | None = None, key: str | None = None, lines: int = 100,
    ) -> dict:
        if not self._current(credential_id):
            raise ValueError(REVOKED)
        if operation == "open":
            return self._open(credential_id, project_id, instance_id)
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None or session.owner != credential_id:
                if operation == "close":
                    return {"closed": True}
                raise ValueError(GONE)
            if not session.lock.acquire(blocking=False):
                raise ValueError(BUSY)
        try:
            if operation == "close":
                self._drop(session_id, session)
                return {"closed": True}
            if monotonic() - session.last_used >= IDLE_SECONDS:
                self._drop(session_id, session)
                raise ValueError(GONE)
            target = session.master.target
            try:
                if pinned_key(self.config, *target.scope) != target.host_key:
                    raise ValueError("SSH 主机公钥登记已更新，请重新建立连接")
            except ValueError:
                self._drop(session_id, session)
                raise
            if operation == "terminal_send":
                command = tmux_send(line=line, key=key)
            elif operation == "terminal_read":
                command = tmux_read(lines)
            elif operation != "exec":
                raise ValueError("未知 SSH 操作")
            if not self._current(credential_id):
                raise ValueError(REVOKED)
            return self._run(session, operation, command, request_id)
        finally:
            session.last_used = monotonic()
            session.lock.release()


class ControlServer(socketserver.ThreadingUnixStreamServer):
    def __init__(self, path: Path, sessions: Sessions) -> None:
        self.sessions = sessions
        self.slots = BoundedSemaphore(MAX_SESSIONS + 1)
        super().__init__(str(path), ControlHandler)

    def process_request(self, request, address) -> None:
        if self.slots.acquire(blocking=False):
            try:
                super().process_request(request, address)
            except BaseException:
                self.slots.release()
                raise
        else:
            self.shutdown_request(request)

    def process_request_thread(self, request, address) -> None:
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class ControlHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(2)
        try:
            message = receive_local_message(self.request)
        except (OSError, ValueError, RecursionError):
            return
        try:
            if not isinstance(message, dict):
                raise ValueError("无效的 SSH 请求")
            reply = {"result": self.server.sessions.dispatch(**message)}
        except ValueError as error:
            reply = {"error": str(error)}
        except Exception:
            # Request bodies and remote output can contain secrets.
            reply = {"error": "SSH 后台操作失败，请核对操作结果"}
        try:
            send_local_message(self.request, reply)
        except OSError:
            pass


@contextmanager
def serve_ssh_sessions(config: Config) -> Iterator[None]:
    path = config.data_dir / CONTROL_SOCKET
    if path.is_socket():
        path.unlink()
    with TemporaryDirectory(prefix="gssh-") as directory:
        sessions = Sessions(config, Path(directory))
        with ControlServer(path, sessions) as server:
            try:
                path.chmod(0o600)
                stopping = Event()

                def reap() -> None:
                    while not stopping.wait(2):
                        try:
                            sessions.reap()
                        except (OSError, ValueError, sqlite3.Error):
                            continue

                listener = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
                janitor = Thread(target=reap, daemon=True)
                listener.start()
                janitor.start()
                try:
                    yield
                finally:
                    stopping.set()
                    server.shutdown()
                    listener.join()
                    janitor.join(timeout=2)
                    sessions.reap(shutdown=True)
            finally:
                os.unlink(path)
