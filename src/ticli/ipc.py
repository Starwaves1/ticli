"""The player socket: JSON lines over a 0600 Unix socket in the state dir (ADR-0008).

Request `{"id", "cmd", "args", "caller", "key"?}` gets `{"id", "ok", ...}` back;
a `subscribe` request then receives `{"event": "state", "full": true, "state"}`
once and deltas after it. TIDAL objects cross as flat records tagged `_k` and
come out as plain shims; the player keeps the real objects by id.
Kept light: `ticli <verb>` imports this without the TUI or tidalapi.
"""

import hashlib
import json
import os
import select
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ticli.utils import testhooks, throttle
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
HELLO_TIMEOUT = 2.0
HANDOVER_NAME = "handover.json"
HANDOVER_FRESH_SECONDS = 60.0
REPLACE_SECONDS = 10.0
STALE_REASON = "The background player is running older code"
STALE_FIX = ("Run `{}` to update it — playback resumes where it was.")


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
        self.stale = False  # an older player this client could not hand over: hint on unknown_command

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
                 timeout: float = READY_TIMEOUT, replace: Optional[int] = None) -> str:
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
    if replace:
        cmd += ["--replace", str(replace)]
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


# ── handover: a client on newer code replaces an older player (ADR-0008) ──


def _fingerprint(root: Path = Path(__file__).resolve().parent) -> dict:
    """The package's .py files by path, mtime and size: an editable install's edits
    change it. `at` is the newest mtime, so a client knows which side is newer."""
    newest, parts = 0.0, []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ("tests", "__pycache__"))
        for name in sorted(filenames):
            if name.endswith(".py"):
                path = os.path.join(dirpath, name)
                st = os.stat(path)
                newest = max(newest, st.st_mtime)
                parts.append(f"{os.path.relpath(path, root)}:{st.st_mtime_ns}:{st.st_size}")
    return {"id": hashlib.blake2b("\n".join(parts).encode(), digest_size=6).hexdigest(),
            "at": newest}


_CODE = _fingerprint()


def code() -> dict:
    return testhooks.code() or _CODE


def lock_holder() -> Optional[int]:
    """The pid holding the instance lock, read only while the lock is really held."""
    import fcntl
    try:
        fd = os.open(throttle.STATE_DIR / LOCK_NAME, os.O_RDONLY)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            return int(os.read(fd, 32).decode().strip() or 0) or None
        except (OSError, ValueError, UnicodeDecodeError):
            return None
    except OSError:
        return None
    finally:
        os.close(fd)
    return None


def _handover_path() -> Path:
    return throttle.STATE_DIR / HANDOVER_NAME


def write_handover(playing: bool, taken: bool = False) -> None:
    path = _handover_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"playing": playing, "taken": taken, "at": time.time()}))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def handover_pending() -> bool:
    """A handover happened just now: a TUI that lost its player attaches to the next."""
    try:
        return time.time() - _handover_path().stat().st_mtime < HANDOVER_FRESH_SECONDS
    except OSError:
        return False


def take_handover() -> Optional[dict]:
    """The replaced player's playing flag, once: whichever player starts next resumes it."""
    try:
        data = json.loads(_handover_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("taken") or not handover_pending():
        return None
    write_handover(bool(data.get("playing")), taken=True)
    return data


def _running(job) -> bool:
    return isinstance(job, dict) and job.get("state") == "running"


def player_info(conn: Connection) -> Optional[dict]:
    """{"code", "pid", "loaded", "playing", "busy"} for the player on `conn`; None if it
    did not answer. A player from before `hello` has code None, and busy None when
    its jobs can't be seen without risk."""
    reply = conn.request("hello", caller="human", timeout=HELLO_TIMEOUT)
    if reply is None:
        return None
    if reply.get("ok"):
        return reply.get("result") or {}
    status = conn.request("status", caller="human", timeout=HELLO_TIMEOUT)
    if not (status and status.get("ok")):
        return None
    result = status.get("result") or {}
    info = {"code": None, "pid": lock_holder(), "loaded": result.get("track") is not None,
            "playing": bool(result.get("playing")), "busy": None}
    if isinstance(result.get("jobs"), dict):
        info["busy"] = [name for name, job in result["jobs"].items() if _running(job)]
    elif not info["loaded"]:
        # Its own connection: a subscriber leaving while music plays stops it.
        probe = connect()
        if probe is not None:
            try:
                probe.request("subscribe", timeout=HELLO_TIMEOUT)
                full = next((m["state"] for m in probe.held if m.get("event") == "state"), None)
            finally:
                probe.close()
            if full is not None:
                info["busy"] = [name for name, on in (
                    ("download", _running(full.get("download_job")) or full.get("download_run")),
                    ("refetch", _running(full.get("refetch_job")))) if on]
    return info


def outdated(info: dict) -> bool:
    """Only a newer client replaces a player, so two builds never take turns."""
    theirs, mine = info.get("code"), code()
    if not isinstance(theirs, dict):
        return True
    return theirs.get("id") != mine["id"] and float(theirs.get("at") or 0) < mine["at"]


def replace_player(pid: int, quality: Optional[str] = None, login_flow: Optional[str] = None,
                   timeout: float = READY_TIMEOUT) -> tuple:
    """A fresh player that saves the old one's place, stops it and resumes there;
    (connection, None) or (None, status)."""
    _reap()
    status = spawn_player(quality, login_flow, timeout, replace=pid)
    if status not in ("ready", "running"):
        return None, status
    return connect_or_start(quality, login_flow, timeout)


def connect_current(quality: Optional[str] = None, login_flow: Optional[str] = None,
                    timeout: float = READY_TIMEOUT, start: bool = True, say=None) -> tuple:
    """`connect_or_start` (or just `connect`), then the code check: an older player that
    is idle, or only playing, is replaced (`say` gets one line); one running a job, or
    too old to tell, is kept and the connection marked `stale`."""
    conn, status = connect_or_start(quality, login_flow, timeout) if start else (connect(), None)
    if conn is None:
        return conn, status
    info = player_info(conn)
    if info is None or not outdated(info):
        return conn, None
    unknown = info.get("busy") is None or (info.get("code") is None and info.get("loaded"))
    if unknown or info["busy"] or not info.get("pid"):
        conn.stale = True
        return conn, None
    conn.close()
    conn, status = replace_player(info["pid"], quality, login_flow, timeout)
    if conn is not None and say is not None:
        say("ticli: restarted the background player on the updated code"
            + ("; playback resumed" if info.get("playing") else ""))
    return conn, status


def stale_reply(response: dict, restart: str) -> dict:
    """An unknown_command from a stale player says why and what to run."""
    if response.get("code") != "unknown_command":
        return response
    return {**response, "reason": f"{STALE_REASON}: {response.get('reason', '')}",
            "fix": STALE_FIX.format(restart)}


def restart(quality: Optional[str] = None, login_flow: Optional[str] = None, force: bool = False,
            timeout: float = READY_TIMEOUT) -> dict:
    """`ticli restart`: a handover on demand. Refused while a job runs unless `force`."""
    conn = connect()
    if conn is None:
        return {"ok": True, "result": {"restarted": False, "running": False}}
    try:
        info = player_info(conn)
    finally:
        conn.close()
    if info is None:
        return {"ok": False, "code": "player_slow", "reason": "The player did not answer.",
                "fix": "Try again in a moment; its log is " + str(log_path())}
    if info.get("busy") and not force:
        return {"ok": False, "code": "busy", "reason": f"The player is busy: {', '.join(info['busy'])}.",
                "fix": "Restart once that finishes; it would be cut short."}
    pid = info.get("pid") or lock_holder()
    if not pid:
        return {"ok": False, "code": "player_unavailable", "reason": "Could not tell which process is the player.",
                "fix": "Quit ticli and start it again."}
    conn, status = replace_player(pid, quality, login_flow, timeout)
    if conn is None:
        return {"ok": False, "code": "player_unavailable", "reason": status or "The player did not start.",
                "fix": "See the player's log: " + str(log_path())}
    try:
        after = player_info(conn) or {}
    finally:
        conn.close()
    return {"ok": True, "result": {"restarted": after.get("pid") != pid, "running": True,
                                   "loaded": bool(after.get("loaded")),
                                   "playing": bool(after.get("playing"))}}
