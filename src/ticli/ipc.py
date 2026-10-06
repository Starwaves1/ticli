"""The player socket: JSON lines over a 0600 Unix socket in the state dir (ADR-0008).

Request `{"id", "cmd", "args", "caller", "key"?}` gets `{"id", "ok", ...}` back;
a `subscribe` request then receives `{"event": "state", "full": true, "state"}`
once and deltas after it. TIDAL objects cross as flat records tagged `_k` and
come out as plain shims; the player keeps the real objects by id.
Kept light: `ticli <verb>` imports this without the TUI or tidalapi.
"""

import json
import os
import select
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ticli.utils import throttle
from ticli.utils.cache import (  # noqa: F401
    SHIMS, CachedAlbum, CachedArtist, CachedPlaylist, CachedTrack, kind_of, record_of,
)

SOCKET_NAME = "player.sock"
LOG_NAME = "player.log"
READY_TIMEOUT = 90.0
RACE_POLL_SECONDS = 0.05
REAP_SECONDS = 5.0
LOCK_NAME = "instance.lock"
CALLERS = ("tui", "human", "agent")
PLAYER_MODULE = "ticli.playerd"


def socket_path() -> Path:
    return throttle.STATE_DIR / SOCKET_NAME


def log_path() -> Path:
    return throttle.STATE_DIR / LOG_NAME


# ── TIDAL objects on the wire ──


# The shims live in utils/cache.py: the metadata index stores the same flat records.
RemoteAlbum, RemoteArtist = CachedAlbum, CachedArtist


def _object_hook(data: dict):
    kind = data.get("_k")
    shim = SHIMS.get(kind) if isinstance(kind, str) else None
    return shim(data) if shim else data


def encoder(remember=None):
    """A json `default` that flattens TIDAL objects, telling `remember(kind, obj)` each one."""
    def default(obj):
        if isinstance(obj, (set, frozenset, tuple)):
            return list(obj)
        kind = kind_of(obj)
        if kind is None:
            raise TypeError(f"{type(obj).__name__} is not wire data")
        if remember is not None:
            remember(kind, obj)
        return {"_k": kind, **record_of(kind, obj)}
    return default


def dumps(message: dict, default=None) -> bytes:
    return (json.dumps(message, default=default or encoder(), separators=(",", ":"))
            + "\n").encode()


def loads(line: bytes):
    return json.loads(line, object_hook=_object_hook)


# ── connections ──


class Connection:
    """One client end. Blocking sends; `read_messages` after select says readable."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buf = b""
        self._next_id = 0
        self.closed = False
        self.held: list = []

    def fileno(self) -> int:
        return self.sock.fileno()

    def send(self, cmd: str, args: Optional[dict] = None, caller: str = "tui", key=None) -> int:
        self._next_id += 1
        message = {"id": self._next_id, "cmd": cmd, "args": args or {}, "caller": caller}
        if key:
            message["key"] = key
        try:
            self.sock.sendall(dumps(message))
        except OSError:
            self.closed = True
        return self._next_id

    def read_messages(self) -> list:
        try:
            data = self.sock.recv(1 << 16)
        except BlockingIOError:
            return []
        except OSError:
            data = b""
        if not data:
            self.closed = True
            return []
        self._buf += data
        *lines, self._buf = self._buf.split(b"\n")
        messages = []
        for line in lines:
            try:
                messages.append(loads(line))
            except ValueError:
                continue
        return messages

    def wait_for(self, rid: int, timeout: Optional[float] = None) -> Optional[dict]:
        """Block until the response to `rid`; events that arrive first are kept in `held`."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.closed:
            for i, message in enumerate(self.held):
                if message.get("id") == rid:
                    return self.held.pop(i)
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return None
            if select.select([self.sock], [], [], left)[0]:
                self.held.extend(self.read_messages())
        return None

    def request(self, cmd: str, args: Optional[dict] = None, caller: str = "tui", key=None,
                timeout: Optional[float] = None) -> Optional[dict]:
        return self.wait_for(self.send(cmd, args, caller, key), timeout)

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


def connect(path: Optional[Path] = None) -> Optional[Connection]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path or socket_path()))
    except OSError:
        sock.close()
        return None
    return Connection(sock)


def spawn_player(quality: Optional[str] = None, login_flow: Optional[str] = None,
                 timeout: float = READY_TIMEOUT) -> str:
    """Start the background player detached and wait for its one-line status:
    "ready", "running" (another player holds the lock), "login" (no usable
    saved session) or "error: ...". A dead child reads as an error, never a hang."""
    throttle.STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    ready_r, ready_w = os.pipe()
    cmd = [sys.executable, "-m", PLAYER_MODULE, "--ready-fd", str(ready_w)]
    if quality:
        cmd += ["--quality", quality]
    if login_flow:
        cmd += ["--login-flow", login_flow]
    env = dict(os.environ)
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (package_root, env.get("PYTHONPATH")) if p)
    log_fd = os.open(log_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=log_fd, pass_fds=(ready_w,), start_new_session=True,
                                 close_fds=True, cwd="/", env=env)
    finally:
        os.close(log_fd)
        os.close(ready_w)
    try:
        status = read_status(ready_r, timeout)
    finally:
        os.close(ready_r)
    if status != "ready":
        try:
            child.wait(REAP_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    if child.poll() is None:
        _children.append(child)
    return status


# Players this process started, reaped once they exit so none lingers as a zombie.
_children: list = []


def _reap() -> None:
    _children[:] = [child for child in _children if child.poll() is None]


def _lock_holder_alive() -> bool:
    try:
        pid = int((throttle.STATE_DIR / LOCK_NAME).read_text().strip() or 0)
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    for child in _children:
        if child.pid == pid:
            return child.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass
    return True


def read_status(fd: int, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    data = b""
    while b"\n" not in data:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            return "error: the player did not start in time"
        chunk = os.read(fd, 256)
        if not chunk:
            return "error: the player exited while starting; see " + str(log_path())
        data += chunk
    return data.split(b"\n", 1)[0].decode(errors="replace").strip()


def connect_or_start(quality: Optional[str] = None, login_flow: Optional[str] = None,
                     timeout: float = READY_TIMEOUT) -> tuple:
    """(connection, None), or (None, status) when the player could not be reached.

    "running" means another client's player holds the lock: wait for its socket,
    and start one again if it leaves first (its starter may have quit at once)."""
    _reap()
    deadline = time.monotonic() + timeout
    while True:
        conn = connect()
        if conn is not None:
            return conn, None
        left = deadline - time.monotonic()
        if left <= 0:
            return None, "error: the player did not start in time; try again in a moment"
        status = spawn_player(quality, login_flow, left)
        if status not in ("ready", "running"):
            return None, status
        while time.monotonic() < deadline:
            conn = connect()
            if conn is not None:
                return conn, None
            if status == "ready" or not _lock_holder_alive():
                break
            time.sleep(RACE_POLL_SECONDS)
