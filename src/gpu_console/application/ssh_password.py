"""On-demand password exchange between the local web and worker processes."""

import json
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import BoundedSemaphore, Event, Thread

SOCKET_NAME = ".ssh.sock"
MAX_MESSAGE = 16384
CLIENT_TIMEOUT = 12
LOOKUP_SLOT = BoundedSemaphore(1)
PasswordLookup = Callable[[str, str, str, str], str | None]


class PasswordServiceUnavailable(RuntimeError):
    pass


class PasswordServiceBusy(RuntimeError):
    pass


def receive_local_message(peer: socket.socket, limit: int = MAX_MESSAGE) -> object:
    with peer.makefile("rb") as stream:
        line = stream.readline(limit + 1)
    if len(line) > limit or not line.endswith(b"\n"):
        raise ValueError("invalid password service message")
    return json.loads(line)


def send_local_message(peer: socket.socket, message: object) -> None:
    peer.sendall(json.dumps(message, ensure_ascii=False).encode() + b"\n")


def fetch_ssh_password(
    directory: Path, project_id: str, region: str, zone: str, instance_id: str,
) -> str | None:
    if not LOOKUP_SLOT.acquire(blocking=False):
        raise PasswordServiceBusy
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(CLIENT_TIMEOUT)
            peer.connect(str(directory / SOCKET_NAME))
            send_local_message(peer, [project_id, region, zone, instance_id])
            result = receive_local_message(peer)
        if isinstance(result, dict) and set(result) == {"password"}:
            password = result["password"]
            if password is None or isinstance(password, str) and password:
                return password
    except (OSError, ValueError, RecursionError):
        raise PasswordServiceUnavailable from None
    finally:
        LOOKUP_SLOT.release()
    raise PasswordServiceUnavailable


def _handle(peer: socket.socket, lookup: PasswordLookup) -> None:
    peer.settimeout(2)
    try:
        scope = receive_local_message(peer)
        if not isinstance(scope, list) or len(scope) != 4 or not all(
            isinstance(value, str) and 1 <= len(value) <= 128 for value in scope
        ):
            raise ValueError("invalid instance scope")
        reply = {"password": lookup(*scope)}
    except Exception:
        # Avoid exposing provider diagnostics or credential-bearing replies.
        reply = {"unavailable": True}
    try:
        send_local_message(peer, reply)
    except OSError:
        pass


def _serve(listener: socket.socket, shutdown: Event, lookup: PasswordLookup) -> None:
    while not shutdown.is_set():
        try:
            peer, _ = listener.accept()
        except TimeoutError:
            continue
        except OSError:
            return
        with peer:
            _handle(peer, lookup)


@contextmanager
def serve_ssh_passwords(directory: Path, lookup: PasswordLookup | None) -> Iterator[None]:
    """Run a single lookup at a time, under the worker process lock."""
    path = directory / SOCKET_NAME
    if path.is_socket():
        path.unlink()
    if lookup is None:
        yield
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        try:
            path.chmod(0o600)
            listener.listen(1)
            listener.settimeout(0.25)
            shutdown = Event()
            server = Thread(target=_serve, args=(listener, shutdown, lookup), daemon=True)
            server.start()
            try:
                yield
            finally:
                shutdown.set()
                server.join(timeout=0.5)
        finally:
            path.unlink(missing_ok=True)
