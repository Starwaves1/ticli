"""The real `python -m ticli.playerd`, started the way `ticli` starts it, against
the fake TIDAL in `fake_tidal.py` through the hooks in `utils/testhooks.py`.

Everything the child touches lives under a throwaway HOME; mpv runs through a
wrapper that adds `--ao=null`, so nothing is ever audible. Skipped without mpv;
`-m real_player` runs just these.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from ticli import ipc
from ticli.utils import throttle

MPV = shutil.which("mpv")

pytestmark = [pytest.mark.real_player,
              pytest.mark.skipif(MPV is None, reason="needs mpv (run with --ao=null)")]


def _wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _pid() -> int:
    return int((throttle.STATE_DIR / ipc.LOCK_NAME).read_text().strip())


def _exited(pid) -> bool:
    for child in ipc._children:
        if child.pid == pid:
            return child.poll() is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _mpv_running(home) -> bool:
    found = subprocess.run(["pgrep", "-f", str(home / "fake-tidal")], capture_output=True)
    return found.returncode == 0


def _status(conn) -> dict:
    return conn.request("status", timeout=5)["result"]


@pytest.fixture
def home(monkeypatch):
    # Under /tmp: macOS caps a socket path at 104 bytes.
    home = Path(tempfile.mkdtemp(prefix="ticli-real-", dir="/tmp"))
    bin_dir = home / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "mpv"
    wrapper.write_text(f'#!/bin/sh\necho "$@" >> "{home}/mpv-calls.log"\n'
                       f'exec "{MPV}" --ao=null "$@"\n')
    wrapper.chmod(0o755)
    state = home / ".config" / "ticli"
    state.mkdir(parents=True)
    (state / "session.json").write_text(json.dumps(
        {"token_type": "Bearer", "access_token": "fake", "is_pkce": False}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("TICLI_TEST_HOOKS", "1")
    monkeypatch.setenv("TICLI_TEST_SESSION", "ticli.tests.fake_tidal:session")
    for name in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_MUSIC_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(throttle, "STATE_DIR", state)
    monkeypatch.setattr(ipc, "socket_path", lambda: state / ipc.SOCKET_NAME)
    yield home
    try:
        pid = _pid()
        if not _exited(pid):
            os.kill(pid, signal.SIGTERM)
            _wait(lambda: _exited(pid))
    except (OSError, ValueError):
        pass
    shutil.rmtree(home, ignore_errors=True)


def test_start_play_pause_seek_two_tuis_detach_and_stop(home):
    a, status = ipc.connect_or_start()
    assert status is None and a is not None, status
    assert os.stat(ipc.socket_path()).st_mode & 0o777 == 0o600
    pid = _pid()
    b = ipc.connect()
    for conn in (a, b):
        assert conn.request("subscribe", timeout=5)["ok"]
        assert conn.held[0]["full"] is True

    assert a.request("play.track", {"track_ids": [1]}, timeout=10)["ok"]
    assert _wait(lambda: _status(a)["playing"] and _status(a)["position"] > 0.3)
    assert (home / "mpv-calls.log").exists(), "mpv ran through the --ao=null wrapper"

    def playing_clocks(conn):
        conn.wait_for(-1, timeout=0.05)
        return [m["state"]["clock"] for m in conn.held
                if m.get("event") == "state" and m["state"].get("clock", [False])[0]]

    assert _wait(lambda: playing_clocks(b) and playing_clocks(a))
    assert playing_clocks(a)[0] == playing_clocks(b)[0], "both TUIs got the same clock"

    assert a.request("pause", timeout=5)["ok"]
    paused = _status(a)
    time.sleep(0.5)
    assert not paused["playing"] and _status(a)["position"] == paused["position"]
    assert a.request("resume", timeout=5)["ok"]
    assert _wait(lambda: _status(a)["position"] > paused["position"])
    assert a.request("seek", {"position": 20}, timeout=5)["ok"]
    assert _wait(lambda: 19 <= _status(a)["position"] <= 25)

    a.close()
    b.close()
    time.sleep(0.5)
    c = ipc.connect()
    assert c is not None and _status(c)["playing"], "detached, it keeps playing"
    assert _pid() == pid and _mpv_running(home)

    assert c.request("stop", timeout=5)["ok"]
    c.close()
    assert _wait(lambda: _exited(pid)), "stopped and nobody attached: it leaves"
    assert not ipc.socket_path().exists()
    assert _wait(lambda: not _mpv_running(home), 5)


def test_a_stale_socket_is_replaced_and_sigterm_saves_and_leaves(home):
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(ipc.socket_path()))
    stale.close()
    a, status = ipc.connect_or_start()
    assert status is None and a is not None, status
    pid = _pid()
    assert a.request("play.track", {"track_ids": [1]}, timeout=10)["ok"]
    assert _wait(lambda: _status(a)["playing"] and _status(a)["position"] > 0.3)

    os.kill(pid, signal.SIGTERM)
    assert _wait(lambda: _exited(pid)), "a signal ends it even with a client attached"
    assert a.wait_for(-1, timeout=2) is None and a.closed
    assert not ipc.socket_path().exists()
    assert _wait(lambda: not _mpv_running(home), 5)
    saved = json.loads((throttle.STATE_DIR / "player_state.json").read_text())
    assert saved["track_ids"] == [1] and saved["position"] > 0
