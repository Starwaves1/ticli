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
from ticli.utils.cache import CachedPlaylist, CachedTrack, _Named, playlist_record, track_record

SOCKET_NAME = "player.sock"
LOG_NAME = "player.log"
READY_TIMEOUT = 90.0
CONNECT_RACE_SECONDS = 10.0
CALLERS = ("tui", "human", "agent")
PLAYER_MODULE = "ticli.playerd"


def socket_path() -> Path:
    return throttle.STATE_DIR / SOCKET_NAME


def log_path() -> Path:
    return throttle.STATE_DIR / LOG_NAME


# ── TIDAL objects on the wire ──


class RemoteAlbum:
    cached = True
    wire_kind = "album"
    __slots__ = ("id", "name", "artist", "cover", "num_tracks")

    def __init__(self, record: dict):
        self.id = record.get("id")
        self.name = record.get("name") or "?"
        self.artist = _Named(record["artist"]) if record.get("artist") else None
        self.cover = record.get("cover")
        self.num_tracks = record.get("num_tracks") or 0


class RemoteArtist:
    cached = True
    wire_kind = "artist"
    __slots__ = ("id", "name")

    def __init__(self, record: dict):
        self.id = record.get("id")
        self.name = record.get("name") or "?"


_KINDS_BY_CLASS = {"Track": "track", "Video": "track", "Album": "album", "Artist": "artist",
                   "Playlist": "playlist", "UserPlaylist": "playlist",
                   "CachedTrack": "track", "CachedPlaylist": "playlist"}


def kind_of(obj) -> Optional[str]:
    kind = getattr(obj, "wire_kind", None) or _KINDS_BY_CLASS.get(type(obj).__name__)
    if kind:
        return kind
    if hasattr(obj, "duration") and hasattr(obj, "artists"):
        return "track"
    if hasattr(obj, "num_tracks") and hasattr(obj, "artist"):
        return "album"
    if hasattr(obj, "num_tracks") or hasattr(obj, "creator"):
        return "playlist"
    if hasattr(obj, "id") and hasattr(obj, "name"):
        return "artist"
    return None


def _is_editable(obj) -> bool:
    return type(obj).__name__ == "UserPlaylist" or bool(getattr(obj, "editable", False))


def record_of(kind: str, obj) -> dict:
    if kind == "track":
        return track_record(obj)
    if kind == "playlist":
        return playlist_record(obj, _is_editable(obj))
    if kind == "album":
        artist = getattr(obj, "artist", None)
        return {"id": getattr(obj, "id", None), "name": getattr(obj, "name", None),
                "artist": getattr(artist, "name", None) if artist else None,
                "cover": getattr(obj, "cover", None) if isinstance(getattr(obj, "cover", None), str) else None,
                "num_tracks": getattr(obj, "num_tracks", None)}
    return {"id": getattr(obj, "id", None), "name": getattr(obj, "name", None)}


_SHIMS = {"track": CachedTrack, "playlist": CachedPlaylist, "album": RemoteAlbum,
          "artist": RemoteArtist}


def _object_hook(data: dict):
    kind = data.get("_k")
    shim = _SHIMS.get(kind) if isinstance(kind, str) else None
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
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log_fd,
                         pass_fds=(ready_w,), start_new_session=True, close_fds=True,
                         cwd="/", env=env)
    finally:
        os.close(log_fd)
        os.close(ready_w)
    try:
        return read_status(ready_r, timeout)
    finally:
        os.close(ready_r)


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


def connect_or_start(quality: Optional[str] = None, login_flow: Optional[str] = None) -> tuple:
    """(connection, None), or (None, status) when the player could not be reached."""
    conn = connect()
    if conn is not None:
        return conn, None
    status = spawn_player(quality, login_flow)
    if status == "ready":
        conn = connect()
    elif status == "running":
        # Rare: two clients started a player at once and the other one won the lock.
        deadline = time.monotonic() + CONNECT_RACE_SECONDS
        while conn is None and time.monotonic() < deadline:
            time.sleep(0.05)
            conn = connect()
    return (conn, None) if conn is not None else (None, status if status != "ready" else
                                                  "error: the player started but its socket is gone")
