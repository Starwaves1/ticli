"""Handover: a client on newer code replaces an older player and playback resumes.

The fingerprint is faked with TICLI_TEST_CODE: the player takes the value in its
environment at spawn, the client reads it on every check. TICLI_TEST_LEGACY makes
the player one from before `hello`, missing the listed commands.
"""

import os
import select
import time

import pytest

from ticli import ipc
from ticli.tests import test_real_player
from ticli.tests.test_real_player import MPV, _exited, _human_cli, _pid, _status, _wait

home = test_real_player.home


def _old_player(monkeypatch, legacy=None, code="old@1"):
    monkeypatch.setenv("TICLI_TEST_CODE", code)
    if legacy is not None:
        monkeypatch.setenv("TICLI_TEST_LEGACY", legacy)
    conn, status = ipc.connect_or_start()
    assert status is None and conn is not None, status
    monkeypatch.setenv("TICLI_TEST_CODE", "new@2")
    monkeypatch.delenv("TICLI_TEST_LEGACY", raising=False)
    return conn, _pid()


def _playing_at(conn, seconds, pause=False):
    assert conn.request("play.track", {"track_ids": [1]}, timeout=10)["ok"]
    assert _wait(lambda: _status(conn)["playing"] and _status(conn)["position"] > 0.3)
    assert conn.request("seek", {"position": seconds}, timeout=5)["ok"]
    assert _wait(lambda: _status(conn)["position"] >= seconds)
    if pause:
        assert conn.request("pause", timeout=5)["ok"]


# ── the code check, no player process ──


def test_the_fingerprint_follows_source_edits_but_not_tests_or_bytecode(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    for skipped in ("tests", "__pycache__"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "b.py").write_text("")
    before = ipc._fingerprint(tmp_path)
    (tmp_path / "tests" / "b.py").write_text("changed")
    assert ipc._fingerprint(tmp_path) == before
    (tmp_path / "a.py").write_text("x = 22\n")
    os.utime(tmp_path / "a.py", (time.time() + 5, time.time() + 5))
    after = ipc._fingerprint(tmp_path)
    assert after["id"] != before["id"] and after["at"] > before["at"]


def test_only_a_newer_client_counts_a_player_as_outdated(monkeypatch):
    monkeypatch.setenv("TICLI_TEST_HOOKS", "1")
    monkeypatch.setenv("TICLI_TEST_CODE", "mine@5")
    assert not ipc.outdated({"code": {"id": "mine", "at": 5}})
    assert ipc.outdated({"code": {"id": "theirs", "at": 4}})
    assert not ipc.outdated({"code": {"id": "theirs", "at": 6}}), "never take turns with a newer build"
    assert ipc.outdated({"code": None}), "a player from before hello is older"


class _Conn:
    def __init__(self):
        self.stale = False
        self.closed = False

    def close(self):
        self.closed = True


@pytest.mark.parametrize("info, replaced", [
    ({"code": None, "pid": 7, "loaded": False, "playing": False, "busy": []}, True),
    ({"code": {"id": "x", "at": 1}, "pid": 7, "loaded": True, "playing": True, "busy": []}, True),
    ({"code": {"id": "x", "at": 1}, "pid": 7, "loaded": True, "playing": True, "busy": ["download"]}, False),
    ({"code": {"id": "x", "at": 1}, "pid": 7, "loaded": False, "playing": False, "busy": ["agent queue"]}, False),
    ({"code": None, "pid": 7, "loaded": True, "playing": True, "busy": []}, False),
    ({"code": None, "pid": 7, "loaded": False, "playing": False, "busy": None}, False),
])
def test_connect_current_replaces_only_what_it_can_hand_over_safely(monkeypatch, info, replaced):
    monkeypatch.setenv("TICLI_TEST_HOOKS", "1")
    monkeypatch.setenv("TICLI_TEST_CODE", "new@2")
    old, new, calls, said = _Conn(), _Conn(), [], []
    monkeypatch.setattr(ipc, "connect", lambda path=None: old)
    monkeypatch.setattr(ipc, "player_info", lambda conn: info)
    monkeypatch.setattr(ipc, "replace_player", lambda pid, *a: calls.append(pid) or (new, None))
    conn, status = ipc.connect_current(start=False, say=said.append)
    assert status is None
    if replaced:
        assert conn is new and calls == [7] and old.closed and len(said) == 1
    else:
        assert conn is old and old.stale and not calls and not said


def test_restart_refuses_while_a_job_runs_unless_forced(monkeypatch):
    info = {"code": {"id": "x", "at": 1}, "pid": 7, "loaded": False, "playing": False,
            "busy": ["download"]}
    monkeypatch.setattr(ipc, "connect", lambda path=None: _Conn())
    monkeypatch.setattr(ipc, "player_info", lambda conn: info)
    monkeypatch.setattr(ipc, "replace_player", lambda pid, *a: (_Conn(), None))
    refused = ipc.restart()
    assert refused["code"] == "busy" and "download" in refused["reason"]
    assert ipc.restart(force=True)["ok"]


def test_a_stale_unknown_command_says_what_to_run():
    reply = ipc.stale_reply({"ok": False, "code": "unknown_command",
                             "reason": "No command named 'cache.status'."}, "ticli restart")
    assert reply["reason"].startswith("The background player is running older code")
    assert "`ticli restart`" in reply["fix"] and "resumes" in reply["fix"]
    other = {"ok": False, "code": "bad_args", "reason": "x"}
    assert ipc.stale_reply(other, "ticli restart") is other


def test_a_handover_note_is_taken_once_and_only_while_fresh(monkeypatch):
    ipc.write_handover(True)
    assert ipc.handover_pending()
    assert ipc.take_handover()["playing"] is True
    assert ipc.take_handover() is None and ipc.handover_pending(), "a TUI still sees it"
    ipc.write_handover(False)
    monkeypatch.setattr(ipc, "HANDOVER_FRESH_SECONDS", -1)
    assert ipc.take_handover() is None


# ── real players ──


@pytest.mark.real_player
@pytest.mark.skipif(MPV is None, reason="needs mpv")
def test_an_idle_older_player_is_replaced_with_one_line(home, monkeypatch):
    old, pid = _old_player(monkeypatch)
    said = []
    conn, status = ipc.connect_current(say=said.append)
    assert status is None and conn is not None and not conn.stale, status
    info = ipc.player_info(conn)
    assert info["pid"] != pid and info["code"]["id"] == "new"
    assert said == ["ticli: restarted the background player on the updated code"]
    assert _wait(lambda: _exited(pid))
    old.close()
    conn.close()


@pytest.mark.real_player
@pytest.mark.skipif(MPV is None, reason="needs mpv")
def test_a_playing_older_player_hands_over_queue_and_position(home, monkeypatch):
    old, pid = _old_player(monkeypatch)
    assert old.request("subscribe", timeout=5)["ok"]
    assert old.request("queue.add", {"track_ids": [2, 1, 3]}, timeout=10)["ok"]
    assert old.request("queue.play", {"index": 1, "track_id": 1}, timeout=5)["ok"]
    assert _wait(lambda: _status(old)["playing"] and _status(old)["track"]["id"] == 1
                 and _status(old)["position"] > 0.3)
    assert old.request("seek", {"position": 20}, timeout=5)["ok"]
    assert _wait(lambda: _status(old)["position"] >= 20 and _status(old)["track"]["id"] == 1)
    started = time.monotonic()
    conn, status = ipc.connect_current(say=lambda line: None)
    assert status is None and conn is not None, status
    assert _wait(lambda: _status(conn)["playing"] and _status(conn)["position"] > 20.5)
    gap = time.monotonic() - started
    now = _status(conn)
    assert ipc.player_info(conn)["pid"] != pid
    assert now["track"]["id"] == 1 and now["queue"] == {"length": 3, "index": 1}
    assert 20 <= now["position"] < 24, now
    print(f"handover gap {gap:.2f} s")
    assert gap < 5, gap
    assert old.wait_for(-1, timeout=5) is None and old.closed, "the old player left"
    conn.close()


@pytest.mark.real_player
@pytest.mark.skipif(MPV is None, reason="needs mpv")
def test_a_paused_player_stays_paused_at_its_place(home, monkeypatch):
    old, pid = _old_player(monkeypatch)
    _playing_at(old, 12, pause=True)
    at = _status(old)["position"]
    assert ipc.restart()["result"] == {"restarted": True, "running": True, "loaded": True,
                                       "playing": False}
    assert _wait(lambda: ipc.lock_holder() is None), "paused and alone, the new player leaves"
    conn, _ = ipc.connect_or_start()
    now = _status(conn)
    assert not now["playing"] and now["track"]["id"] == 1 and abs(now["position"] - at) < 1
    old.close()
    conn.close()


@pytest.mark.real_player
@pytest.mark.skipif(MPV is None, reason="needs mpv")
def test_a_handover_moves_a_watching_tui_to_the_new_player(home, monkeypatch):
    from ticli.player import HeadlessTidalPlayer
    old, pid = _old_player(monkeypatch)
    _playing_at(old, 10)
    tui = HeadlessTidalPlayer(remote=ipc.connect())
    assert tui.remote.request("subscribe", timeout=5)["ok"]
    tui.running = True

    def drained():
        # As the TUI's loop does: read only what select says is there.
        if select.select([tui.remote], [], [], 0.05)[0]:
            tui._drain_remote()
        return tui.remote

    assert ipc.restart()["result"]["restarted"]
    assert _wait(lambda: not drained().closed and ipc.player_info(tui.remote)["pid"] != pid)
    assert tui.running
    assert _wait(lambda: drained() and tui._playing and tui._current_track.id == 1)
    tui.remote.close()
    old.close()


@pytest.mark.real_player
@pytest.mark.skipif(MPV is None, reason="needs mpv")
def test_todays_player_without_hello_hints_then_ticli_restart_resumes_it(home, monkeypatch):
    old, pid = _old_player(monkeypatch, legacy="cache.status")
    _playing_at(old, 15)
    code, out, _ = _human_cli(home, "cache", "status")
    assert code == 1 and "running older code" in out and "ticli restart" in out, out
    assert ipc.player_info(old)["pid"] == pid, "playing with unknown jobs: kept"
    assert _status(old)["playing"]

    code, out, _ = _human_cli(home, "restart")
    assert code == 0 and "restarted the player; playback resumed" in out, out
    conn = ipc.connect()
    assert _wait(lambda: _status(conn)["playing"])
    assert _status(conn)["position"] >= 15 and ipc.player_info(conn)["pid"] != pid
    code, out, _ = _human_cli(home, "cache", "status")
    assert code == 0, out
    old.close()
    conn.close()


@pytest.mark.real_player
@pytest.mark.skipif(MPV is None, reason="needs mpv")
def test_an_idle_player_without_hello_is_replaced_after_a_safe_look_at_its_jobs(home, monkeypatch):
    old, pid = _old_player(monkeypatch, legacy="")
    info = ipc.player_info(old)
    assert info["code"] is None and info["busy"] == [] and info["pid"] == pid
    code, out, _ = _human_cli(home, "status")
    assert code == 0 and "restarted the background player" in out, out
    assert _wait(lambda: _exited(pid))
    old.close()


@pytest.mark.real_player
@pytest.mark.skipif(MPV is None, reason="needs mpv")
def test_agent_restart_answers_json_and_a_newer_player_is_left_alone(home, monkeypatch):
    import json
    import subprocess
    import sys
    from pathlib import Path
    old, pid = _old_player(monkeypatch, code="newest@9")
    conn, _ = ipc.connect_current()
    assert ipc.player_info(conn)["pid"] == pid and not conn.stale, "an older client never replaces"
    conn.close()
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    done = subprocess.run([sys.executable, "-m", "ticli.cli", "agent", "restart"], env=env,
                          cwd=str(home), stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=30)
    reply = json.loads(done.stdout)
    assert done.returncode == 0 and reply["ok"] and reply["result"]["restarted"], done.stdout
    assert _wait(lambda: _exited(pid))
    old.close()
