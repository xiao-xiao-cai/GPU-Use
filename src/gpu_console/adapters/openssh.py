"""OpenSSH password authentication and multiplexed commands on Linux."""

import hashlib
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic

from gpu_console.application.ssh_access import ID, Target
from gpu_console.application.ssh_password import PasswordServiceBusy, PasswordServiceUnavailable, fetch_ssh_password
from gpu_console.config import Config
from gpu_console.domain.instance_access import valid_host

IDLE_SECONDS = 900
COMMAND_SECONDS = 15
OUTPUT_LIMIT = 32 * 1024
TMUX_SESSION = "gpu-console"
TMUX_PANE = f"={TMUX_SESSION}:"
ASKPASS_SCOPE = "GPU_CONSOLE_ASKPASS_SCOPE"
CONNECT_TIMEOUT = 19
SOCKET_PATH_LIMIT = 103
TMUX_KEYS = {"Enter", "C-c", "C-d", "Escape", "Up", "Down", "Left", "Right", "Tab"}


def askpass() -> None:
    """Called by ssh through a private pipe after it verifies the server host key."""
    scope = os.environ.get(ASKPASS_SCOPE, "").split()
    if os.isatty(1) or len(scope) != 4 or not all(ID.fullmatch(value) for value in scope):
        raise SystemExit(1)
    try:
        password = fetch_ssh_password(Config.from_env().data_dir, *scope)
    except (PasswordServiceBusy, PasswordServiceUnavailable, ValueError):
        raise SystemExit(1) from None
    if password is None:
        raise SystemExit(1)
    sys.stdout.buffer.write(password.encode("utf-8") + b"\n")


def scope_key(project_id: str, instance_id: str) -> str:
    return hashlib.sha256(f"{project_id}/{instance_id}".encode()).hexdigest()[:16]


def runtime_dir(directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = directory.lstat()
    if directory.is_symlink() or not directory.is_dir() or details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise ValueError("SSH 运行目录须由当前账号独占，权限为 0700")
    return directory.resolve()


def _ssh() -> list[str]:
    if shutil.which("ssh") is None:
        raise ValueError("请安装 OpenSSH 客户端后重试")
    return ["ssh", "-F", "/dev/null", "-o", "LogLevel=ERROR"]


def _proxy(proxy: str) -> list[str]:
    host, _, port = proxy.rpartition(":")
    if not host.isascii() or not valid_host(host) or not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ValueError("SSH 代理地址须配置为 host:port")
    address = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    nc = shutil.which("nc")
    if nc is None:
        raise ValueError("SSH 代理需要 netcat-openbsd")
    return ["-o", f"ProxyCommand={shlex.quote(nc)} -X connect -x {address} %h %p"]


def _control(path: Path, operation: str) -> bool:
    try:
        result = subprocess.run(
            [*_ssh(), "-S", str(path), "-O", operation, "--", "gpu-console"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def close_master(path: Path) -> bool:
    return path.is_socket() and _control(path, "exit")


def _connect_error(stderr: bytes) -> ValueError:
    if b"Host key verification failed" in stderr or b"REMOTE HOST IDENTIFICATION HAS CHANGED" in stderr:
        return ValueError("SSH 主机公钥校验失败，请核对登记信息")
    if b"Permission denied" in stderr:
        return ValueError("SSH 密码认证失败，请核对实例登录方式、平台密码和后台密码服务")
    return ValueError("SSH 连接失败，请检查目标 SSH 端口和网络连通性")


@dataclass(frozen=True)
class Master:
    target: Target
    path: Path

    @classmethod
    def for_target(cls, target: Target, directory: Path) -> "Master":
        key = scope_key(*target.scope)
        identity = hashlib.sha256(repr(target).encode()).hexdigest()[:12]
        path = directory / f"{key}-{identity}.sock"
        # OpenSSH temporarily appends a random suffix while publishing the control socket.
        if len(os.fsencode(path)) + 17 > SOCKET_PATH_LIMIT:
            raise ValueError("SSH 控制套接字路径过长")
        return cls(target, path)

    def alive(self) -> bool:
        return self.path.is_socket() and _control(self.path, "check")

    def open(self, data_dir: Path, proxy: str | None = None) -> None:
        if self.alive():
            return
        self.path.unlink(missing_ok=True)
        target = self.target
        askpass_exe = Path(sys.executable).with_name("gpu-ssh-askpass")
        if not os.access(askpass_exe, os.X_OK):
            raise ValueError("SSH 密码程序不可用，请同步项目运行环境")
        env = os.environ | {
            "SSH_ASKPASS": str(askpass_exe), "SSH_ASKPASS_REQUIRE": "force",
            "GPU_DATA_DIR": str(data_dir.resolve()),
            ASKPASS_SCOPE: " ".join((target.project_id, target.region, target.zone, target.instance_id)),
            "LC_ALL": "C",
        }
        with NamedTemporaryFile() as known_hosts, NamedTemporaryFile() as diagnostics:
            known_hosts.write(f"gpu-console {target.host_key}\n".encode())
            known_hosts.flush()
            args = [
                *_ssh(), "-fMN", "-S", str(self.path), "-p", str(target.port), "-l", target.user,
                "-o", f"ControlPersist={IDLE_SECONDS}", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=3", "-o", "ClearAllForwardings=yes",
                "-o", "HostKeyAlias=gpu-console", "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={known_hosts.name}", "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "UpdateHostKeys=no", "-o", "PreferredAuthentications=password",
                "-o", "NumberOfPasswordPrompts=1", "-o", "ConnectTimeout=7",
            ]
            if proxy is not None:
                args.extend(_proxy(proxy))
            with subprocess.Popen(
                [*args, "--", target.host], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=diagnostics, env=env, start_new_session=True,
            ) as process:
                try:
                    status = process.wait(timeout=CONNECT_TIMEOUT)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.close()
                    raise ValueError("SSH 登录超时，请检查平台密码服务和网络") from None
            if status or not self.alive():
                self.close()
                diagnostics.seek(0)
                raise _connect_error(diagnostics.read(8192))

    def close(self) -> bool:
        return close_master(self.path)

    def command(self, command: str, *, tty: bool = False) -> list[str]:
        return [
            *_ssh(), "-tt" if tty else "-T", "-e", "none", "-S", str(self.path),
            "-o", "ProxyCommand=/bin/false", "-o", "BatchMode=yes",
            "--", self.target.host, command,
        ]

    def run(self, command: str) -> dict:
        deadline = monotonic() + COMMAND_SECONDS
        collected = {"stdout": bytearray(), "stderr": bytearray()}
        remaining = OUTPUT_LIMIT
        truncated = False
        status = None
        with subprocess.Popen(
            self.command(command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as process, selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            try:
                while selector.get_map() and monotonic() < deadline:
                    for key, _ in selector.select(max(0, deadline - monotonic())):
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        kept = chunk[:remaining]
                        collected[key.data].extend(kept)
                        remaining -= len(kept)
                        truncated |= len(kept) < len(chunk)
                status = process.wait(timeout=max(0, deadline - monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
            finally:
                if process.poll() is None:
                    process.kill()
        finished = status is not None and 0 <= status < 255
        return {
            "state": "finished" if finished else "unknown", "exit_code": status if finished else None,
            "stdout": collected["stdout"].decode("utf-8", "replace"),
            "stderr": collected["stderr"].decode("utf-8", "replace"), "truncated": truncated,
        }


def tmux_send(*, line: str | None, key: str | None) -> str:
    if (line is None) == (key is None):
        raise ValueError("请提供一行终端输入或一个按键")
    if line is not None:
        if not line or any(not char.isprintable() and char != "\t" for char in line):
            raise ValueError("终端输入须为单行文本")
        send = f"tmux send-keys -t {TMUX_PANE} -l -- {shlex.quote(line)} && tmux send-keys -t {TMUX_PANE} Enter"
    else:
        if key not in TMUX_KEYS:
            raise ValueError("不支持该终端按键")
        send = f"tmux send-keys -t {TMUX_PANE} -- {key}"
    return (
        f"(tmux has-session -t ={TMUX_SESSION} 2>/dev/null || tmux new-session -d -s {TMUX_SESSION} "
        f"|| tmux has-session -t ={TMUX_SESSION}) && {send}"
    )


def tmux_read(lines: int) -> str:
    return (
        f'output=$(tmux capture-pane -p -t {TMUX_PANE} -S -{lines}) '
        f'&& printf "%s\\n" "$output" | tail -n {lines}'
    )
