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
import sys
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
    monkeypatch.setenv("TICLI_TEST_SESSION", "session")
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


def test_start_play_pause_seek_two_tuis_and_closing_the_last_stops(home):
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
    assert _wait(lambda: _status(b)["playing"]), "one TUI left: it keeps playing"
    assert _pid() == pid and _mpv_running(home)

    b.close()
    assert _wait(lambda: _exited(pid)), "the last TUI closed: the music stops and it leaves"
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


def test_offline_start_reconnects_only_on_an_action(home, monkeypatch):
    monkeypatch.setenv("TICLI_TEST_SESSION", "offline_session")
    log = home / "fake-tidal" / "requests.log"

    def attempts():
        return log.read_text().split() if log.exists() else []

    a, status = ipc.connect_or_start()
    assert status is None and a is not None, status
    assert a.request("subscribe", timeout=5)["ok"]
    assert a.held[0]["state"]["connectivity"] == "offline"
    assert _status(a)["connectivity"] == "offline"
    assert attempts() == ["sessions"]
    time.sleep(1.5)  # three monitor ticks while idle
    assert attempts() == ["sessions"], "idle offline: no timer, probe or heartbeat"

    reply = a.request("search", {"query": "x"}, caller="agent", timeout=10)
    assert reply["ok"] and reply["result"]["source"] == "local", reply
    assert reply["state"]["connectivity"] == "offline"
    assert attempts() == ["sessions", "sessions"], "the action reconnected once, then answered locally"
    refused = a.request("like", {"track_ids": [1]}, caller="agent", timeout=10)
    assert refused["code"] == "offline"
    a.close()


def _saved_paused_track(home) -> None:
    """A saved, paused track on disk and no player running, the way a quit leaves it."""
    a, status = ipc.connect_or_start()
    assert status is None and a is not None, status
    pid = _pid()
    assert a.request("play.track", {"track_ids": [1]}, timeout=10)["ok"]
    assert _wait(lambda: _status(a)["playing"] and _status(a)["position"] > 0.3)
    assert a.request("seek", {"position": 20}, timeout=5)["ok"]
    assert a.request("pause", timeout=5)["ok"]
    os.kill(pid, signal.SIGTERM)
    assert _wait(lambda: _exited(pid))
    assert _wait(lambda: not _mpv_running(home), 5)
    a.close()
    saved = json.loads((throttle.STATE_DIR / "player_state.json").read_text())
    assert saved["track_ids"] == [1] and saved["position"] > 0


def _human_cli(home, *words, timeout=20.0):
    """`ticli WORDS` as the human from a shell: stdin, stdout and stderr all one terminal."""
    import pty
    import select
    import sys
    primary, secondary = pty.openpty()
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    started = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-m", "ticli.cli", *words], stdin=secondary,
                            stdout=secondary, stderr=secondary, env=env, cwd=str(home),
                            start_new_session=True)
    os.close(secondary)
    out = b""
    try:
        deadline = started + timeout
        while proc.poll() is None and time.monotonic() < deadline:
            if select.select([primary], [], [], 0.05)[0]:
                try:
                    out += os.read(primary, 4096)
                except OSError:
                    break
        if proc.poll() is None:
            proc.kill()
            proc.wait()
            raise AssertionError(f"`ticli {' '.join(words)}` hung: {out!r}")
        while select.select([primary], [], [], 0.05)[0]:
            try:
                chunk = os.read(primary, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    finally:
        os.close(primary)
    return proc.returncode, out.decode(errors="replace").replace("\r\n", "\n"), \
        time.monotonic() - started


def test_human_resume_starts_the_player_says_so_and_answers_at_once(home):
    _saved_paused_track(home)
    code, out, took = _human_cli(home, "resume")
    assert code == 0 and "resumed" in out, out
    assert "starting the player" in out, "a start is never a silent wait"
    assert took < 5, took
    assert _wait(lambda: _mpv_running(home), 5), "the saved track plays"


def test_human_resume_gives_up_readably_on_a_player_that_does_not_answer(home):
    a, status = ipc.connect_or_start()
    assert status is None and a is not None, status
    assert a.request("play.track", {"track_ids": [1]}, timeout=10)["ok"]
    assert a.request("pause", timeout=5)["ok"]
    pid = _pid()
    os.kill(pid, signal.SIGSTOP)  # its socket still takes connections; nothing answers
    try:
        code, out, took = _human_cli(home, "resume", timeout=30)
    finally:
        os.kill(pid, signal.SIGCONT)
    assert code == 1 and "has not answered" in out and "ticli status" in out, out
    assert took < 10, took
    a.close()


def test_queue_add_starts_the_player_queues_without_playing_and_is_saved(home):
    a, status = ipc.connect_or_start()
    assert status is None and a is not None, status
    pid = _pid()
    reply = a.request("queue.add", {"track_ids": [2, 3]}, caller="agent", timeout=10)
    assert reply["ok"] and reply["result"]["added"] == 2, reply
    assert reply["result"]["playing"] is False and reply["state"]["queue"] == {"len": 2, "index": 0}
    assert not _status(a)["playing"] and not _mpv_running(home)
    a.close()
    assert _wait(lambda: _exited(pid)), "nothing playing and nobody left: it leaves"
    saved = json.loads((throttle.STATE_DIR / "player_state.json").read_text())
    assert saved["track_ids"] == [2, 3] and saved["queue_index"] == 0

    b, status = ipc.connect_or_start()
    assert status is None and b is not None, status
    assert _status(b)["queue"] == {"length": 2, "index": 0}
    reply = b.request("queue.add", {"track_ids": [1], "position": "next"}, caller="agent", timeout=10)
    assert reply["ok"] and reply["result"]["index"] == 1, reply
    assert b.request("resume", timeout=5)["ok"]
    assert _wait(lambda: _status(b)["playing"])
    assert _status(b)["track"]["id"] == 2
    b.close()


def test_agent_resume_starts_the_player_and_plays_the_saved_track(home):
    _saved_paused_track(home)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    done = subprocess.run([sys.executable, "-m", "ticli.cli", "agent", "resume"], env=env,
                          cwd=str(home), stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=30)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["ok"] is True, done.stdout
    conn = ipc.connect()
    assert _wait(lambda: _status(conn)["playing"]), "the saved track plays"
    assert _status(conn)["track"]["id"] == 1
    conn.close()


def test_agent_queue_move_and_clear_reach_a_watching_tui_with_a_notice(home):
    tui, status = ipc.connect_or_start()
    assert status is None and tui is not None, status
    assert tui.request("subscribe", timeout=5)["ok"]
    assert tui.request("queue.add", {"track_ids": [1, 2, 3]}, timeout=10)["ok"]
    assert tui.request("resume", timeout=5)["ok"]
    assert _wait(lambda: _status(tui)["playing"])
    agent = ipc.connect()

    def pushed(key):
        tui.wait_for(-1, timeout=0.05)
        return [m["state"][key] for m in tui.held if m.get("event") == "state" and key in m["state"]]

    reply = agent.request("queue.move", {"index": 2, "to": 0, "track_id": 3}, caller="agent", timeout=5)
    assert reply["ok"] and reply["cost"]["requests"] == 0, reply
    assert _wait(lambda: any(t[0] == "agent: moved queue entry 3 to 1" for t in pushed("toast")))
    assert _wait(lambda: [t.id for t in pushed("queue")[-1]] == [3, 1, 2])
    assert _wait(lambda: pushed("queue_index") and pushed("queue_index")[-1] == 1)
    assert _status(tui)["track"]["id"] == 1 and _status(tui)["playing"], "playback untouched"

    stale = agent.request("queue.move", {"index": 0, "to": 2, "track_id": 1}, caller="agent", timeout=5)
    assert stale["code"] == "stale"

    reply = agent.request("queue.clear", caller="agent", timeout=5)
    assert reply["ok"] and reply["result"]["removed"] == 2, reply
    assert _wait(lambda: any(t[0] == "agent: cleared the queue (2 tracks)" for t in pushed("toast")))
    assert _wait(lambda: [t.id for t in pushed("queue")[-1]] == [1])
    assert _status(tui)["playing"]
    agent.close()
    tui.close()
