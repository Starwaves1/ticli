"""The background player and the TUI as its client (ADR-0008), over a real Unix
socket with the player core in a thread. No TIDAL, no audio backend."""

import io
import os
import select
import subprocess
import sys
import threading
import time
import types

import pytest
from rich.console import Console

from ticli import ipc, playerd
from ticli import player as player_mod
from ticli.player import HeadlessTidalPlayer
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod
from ticli.utils import throttle


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")


def _track(tid, duration=200):
    return types.SimpleNamespace(id=tid, name=f"Track {tid}", duration=duration,
                                 artists=[types.SimpleNamespace(name=f"Artist {tid}")],
                                 album=types.SimpleNamespace(name=f"Album {tid}", cover=None))


class _Audio:
    player_cmd = "mpv"
    is_paused = False
    is_playing = True

    def __init__(self):
        self.stopped = 0

    def volume_ceiling(self):
        return 130

    def pause(self):
        self.is_paused = True

    def resume(self):
        self.is_paused = False
        return True

    def stop(self):
        self.stopped += 1

    def get_time_pos(self):
        return None

    def set_volume(self, value):
        pass


class _NoNetwork:
    is_pkce = False

    def __getattr__(self, name):
        raise AssertionError(f"request attempted: {name}")


def _core(playing=True):
    core = HeadlessTidalPlayer()
    core.console = Console(file=io.StringIO())
    core.session = _NoNetwork()
    core.audio = _Audio()
    core._queue = [_track(1), _track(2), _track(3)]
    core._queue_index = 0
    core._current_track = core._queue[0]
    core._liked_ids = {2}

    def _play(track, seek=0):
        core._current_track = track
        core._playing = True
        core._play_offset = seek
        core._play_start_time = time.time()
        core._wake()

    core._play_track = _play
    if playing:
        _play(core._queue[0])
    core._save_state = lambda: None
    return core


class _Running:
    def __init__(self, core):
        self.core = core
        self.server = playerd.PlayerServer(core)
        self.server.listen()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            self.server.serve()
        finally:
            self.server.close()

    def stop(self):
        self.core.running = False
        try:
            self.server.wake()
        except Exception:
            pass
        self.thread.join(2)


@pytest.fixture
def running():
    started = []

    def _start(core=None):
        run = _Running(core or _core())
        started.append(run)
        return run

    yield _start
    for run in started:
        run.stop()


def _tui():
    conn = ipc.connect()
    assert conn is not None
    ui = HeadlessTidalPlayer(remote=conn)
    ui.console = Console(file=io.StringIO(), width=100, height=40)
    ui._wake = lambda: None
    assert conn.wait_for(conn.send("subscribe"), timeout=2)["ok"]
    held, conn.held = conn.held, []
    for message in held:
        ui._on_message(message)
    return ui


def _pump(ui, until, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not until():
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        if select.select([ui.remote], [], [], left)[0]:
            ui._drain_remote()
    return True


class TestProtocol:
    def test_a_request_gets_its_answer(self, running):
        running()
        conn = ipc.connect()
        response = conn.request("status", caller="human", timeout=2)
        assert response["ok"] is True
        assert response["result"]["track"]["title"] == "Track 1"
        assert response["result"]["playing"] is True

    def test_the_socket_is_private(self, running):
        run = running()
        assert (os.stat(run.server.path).st_mode & 0o777) == 0o600

    def test_garbage_gets_an_error_not_a_dead_player(self, running):
        running()
        conn = ipc.connect()
        conn.sock.sendall(b"not json\n")
        assert conn.wait_for(None, timeout=2)["code"] == "bad_request"
        assert conn.request("status", caller="human", timeout=2)["ok"]

    def test_unknown_commands_are_refused(self, running):
        running()
        assert ipc.connect().request("nope", caller="tui", timeout=2)["code"] == "unknown_command"


class TestPushedState:
    def test_subscribe_gets_one_snapshot_then_deltas(self, running):
        running()
        conn = ipc.connect()
        conn.wait_for(conn.send("subscribe"), timeout=2)
        snapshot = conn.held.pop(0)
        assert snapshot["event"] == "state" and snapshot["full"] is True
        assert [t.id for t in snapshot["state"]["queue"]] == [1, 2, 3]
        assert snapshot["state"]["liked"] == [2]

        conn.request("next", caller="tui", timeout=2)
        delta = next(m for m in conn.held if m.get("event") == "state")
        assert delta["full"] is False
        assert delta["state"]["queue_index"] == 1
        assert delta["state"]["track"].id == 2
        assert "queue" not in delta["state"], "an unchanged queue is not resent"

    def test_a_backend_resync_within_tolerance_is_not_pushed(self, running):
        run = running()
        conn = ipc.connect()
        conn.wait_for(conn.send("subscribe"), timeout=2)
        conn.held.clear()
        run.core._play_offset += 0.1
        conn.request("status", caller="tui", timeout=2)
        assert not [m for m in conn.held if m.get("event") == "state"], \
            "the monitor's twice-a-second resync must not wake every TUI"
        run.core._play_offset += 1.0
        conn.request("status", caller="tui", timeout=2)
        assert [m for m in conn.held if "clock" in m.get("state", {})]

    def test_the_tui_mirrors_the_player(self, running):
        running()
        ui = _tui()
        assert ui._current_track.name == "Track 1"
        assert [t.id for t in ui._queue] == [1, 2, 3]
        assert ui._playing is True
        assert 0 <= ui._get_position() < 2

    def test_two_tuis_see_the_same_state(self, running):
        run = running()
        first, second = _tui(), _tui()
        first._handle_key("n")
        assert _pump(first, lambda: first._queue_index == 1)
        assert _pump(second, lambda: second._queue_index == 1)
        assert second._current_track.id == 2
        assert run.core._queue_index == 1

    def test_a_pause_reaches_every_tui(self, running):
        running()
        first, second = _tui(), _tui()
        first._handle_key(" ")
        assert _pump(second, lambda: second._playing is False)
        frozen = second._get_position()
        time.sleep(0.05)
        assert second._get_position() == frozen

    def test_a_player_toast_reaches_the_tui(self, running):
        run = running()
        ui = _tui()
        run.core._set_toast("Downloaded to somewhere")
        run.server.wake()
        assert _pump(ui, lambda: ui._toast == "Downloaded to somewhere")


class TestBrowsingThroughThePlayer:
    def test_opening_an_album_asks_the_player_and_plays_from_its_list(self, running):
        run = running()
        album_tracks = [_track(10), _track(11)]
        album = types.SimpleNamespace(id=5, name="An Album", num_tracks=2,
                                      artist=types.SimpleNamespace(name="Someone"),
                                      tracks=lambda: album_tracks)
        run.core._remember("album", [album])
        ui = _tui()
        ui._open_album(ipc.RemoteAlbum({"id": 5, "name": "An Album"}))
        assert _pump(ui, lambda: len(ui._browse_tracks) == 2)
        assert [t.id for t in ui._browse_tracks] == [10, 11]

        ui._handle_key(player_mod.KEY_DOWN)
        ui._handle_key(player_mod.KEY_DOWN)
        ui._handle_key(player_mod.KEY_ENTER)
        assert _pump(ui, lambda: ui._current_track is not None and ui._current_track.id == 11)
        assert run.core._current_track is album_tracks[1], "the player plays its own object"

    def test_removing_from_a_playlist_works_with_nothing_open_in_the_player(self, running):
        run = running()
        removed = []
        tracks = [_track(1), _track(2), _track(3)]
        playlist = player_mod.tidalapi.UserPlaylist.__new__(player_mod.tidalapi.UserPlaylist)
        playlist.id, playlist.name, playlist.num_tracks = "p1", "Mix", 3
        playlist.tracks = lambda: tracks
        playlist.remove_by_index = lambda i: (removed.append(i), True)[-1]
        run.core._remember("playlist", [playlist])
        run.core._cache.put_playlist_tracks = lambda *a: None
        ui = _tui()
        ui._open_playlist(ipc.CachedPlaylist({"id": "p1", "name": "Mix", "editable": True}))
        assert _pump(ui, lambda: len(ui._browse_tracks) == 3 and ui._browse_playlist is not None)
        ui._browse_cursor = 1
        ui._handle_key("x")
        assert _pump(ui, lambda: [t.id for t in ui._browse_tracks] == [1, 3])
        assert removed == [1]


class TestAgentGateOverTheSocket:
    def test_ai_control_off_refuses_agent_actions(self, running):
        core = _core()
        core.config["allow_ai_control"] = False
        running(core)
        response = ipc.connect().request("next", caller="agent", timeout=3)
        assert response["ok"] is False and response["code"] == "ai_control_off"
        assert core._queue_index == 0

    def test_dangerous_commands_are_refused_to_agents(self, running):
        core = _core()
        running(core)
        response = ipc.connect().request("cache.clear", caller="agent", timeout=3)
        assert response["code"] == "dangerous_off"

    def test_the_tui_is_never_gated(self, running):
        core = _core()
        core.config["allow_ai_control"] = False
        running(core)
        assert ipc.connect().request("next", caller="tui", timeout=2)["ok"] is True
        assert core._queue_index == 1

    def test_a_switch_changed_in_the_tui_reaches_the_player(self, running, config_file):
        core = _core()
        running(core)
        ui = _tui()
        ui._set_protected_setting(config_mod.get_spec("allow_ai_control"), False)
        assert _pump(ui, lambda: ui._mirror["switches"]["allow_ai_control"] is False)
        assert core.config["allow_ai_control"] is False
        response = ipc.connect().request("next", caller="agent", timeout=3)
        assert response["code"] == "ai_control_off"

    def test_the_key_hash_never_crosses_the_socket(self, running):
        core = _core()
        core.config["ai_control_key"] = config_mod.hash_ai_key("secret")
        running(core)
        conn = ipc.connect()
        conn.wait_for(conn.send("subscribe"), timeout=2)
        raw = repr(conn.held)
        assert core.config["ai_control_key"]["hash"] not in raw
        assert conn.held[0]["state"]["switches"]["ai_control_key"] is True


class TestLifecycle:
    def test_closing_the_tui_keeps_playing(self, running):
        run = running()
        ui = _tui()
        ui.remote.close()
        time.sleep(0.1)
        assert run.thread.is_alive()
        assert run.core._playing is True
        assert run.core.audio.stopped == 0

    def test_paused_and_nobody_left_means_exit(self, running):
        run = running(_core(playing=False))
        ui = _tui()
        ui.remote.close()
        run.thread.join(2)
        assert not run.thread.is_alive()

    def test_the_last_track_ending_unattended_means_exit(self, running):
        run = running()
        _tui().remote.close()
        time.sleep(0.05)
        assert run.thread.is_alive()
        run.core._playing = False
        run.server._tick()
        run.thread.join(2)
        assert not run.thread.is_alive()

    def test_a_running_download_keeps_it_alive(self, running):
        run = running(_core(playing=False))
        run.core._download_job = {"state": "running"}
        _tui().remote.close()
        time.sleep(0.1)
        assert run.thread.is_alive()
        run.core._download_job = {"state": "done"}
        run.server.wake()
        run.thread.join(2)
        assert not run.thread.is_alive()

    def test_quitting_the_tui_stops_playback(self, running):
        run = running()
        ui = _tui()
        ui._quitting = True
        ui.remote.request("stop", timeout=2)
        ui.remote.close()
        run.thread.join(2)
        assert run.core._playing is False
        assert run.core.audio.stopped == 1
        assert not run.thread.is_alive(), "stopped and nobody attached: the player leaves"


class TestSingleInstance:
    def test_a_second_player_refuses_and_says_so(self, monkeypatch):
        fd, _ = player_mod._take_instance_lock()
        try:
            ready_r, ready_w = os.pipe()
            monkeypatch.setattr(player_mod, "_find_audio_player",
                                lambda: pytest.fail("a refused player touched the backend"))
            assert playerd.main(["--ready-fd", str(ready_w)]) == 1
            assert ipc.read_status(ready_r, 2) == "running"
            os.close(ready_r)
        finally:
            os.close(fd)

    def test_no_saved_login_asks_the_tui_to_sign_in(self, monkeypatch):
        monkeypatch.setattr(player_mod, "_find_audio_player", lambda: "mpv")
        monkeypatch.setattr(player_mod, "load_tokens", lambda: None)
        monkeypatch.setattr(player_mod.AudioPlayer, "set_volume", lambda self, v: None)
        monkeypatch.setattr(player_mod.AudioPlayer, "volume_ceiling", lambda self: 130)
        ready_r, ready_w = os.pipe()
        assert playerd.main(["--ready-fd", str(ready_w)]) == 1
        assert ipc.read_status(ready_r, 2) == "login"
        os.close(ready_r)


FAKE_PLAYER = '''
import os, socket, sys
fd = int(sys.argv[sys.argv.index("--ready-fd") + 1])
path = os.environ["TICLI_TEST_SOCKET"]
s = socket.socket(socket.AF_UNIX)
s.bind(path)
s.listen(1)
os.write(fd, b"ready\\n")
os.close(fd)
c, _ = s.accept()
c.recv(4096)
c.sendall(b'{"id":1,"ok":true,"result":{"fake":true}}\\n')
c.close()
'''


class TestAutoStart:
    def test_the_first_client_starts_the_player_and_waits_for_it(self, tmp_path, monkeypatch,
                                                                  never_the_real_player_socket):
        (tmp_path / "fake_playerd.py").write_text(FAKE_PLAYER)
        monkeypatch.setenv("PYTHONPATH", str(tmp_path))
        monkeypatch.setenv("TICLI_TEST_SOCKET", str(ipc.socket_path()))
        monkeypatch.setattr(ipc, "PLAYER_MODULE", "fake_playerd")
        conn, status = ipc.connect_or_start()
        assert status is None and conn is not None
        assert conn.request("status", timeout=5)["result"] == {"fake": True}

    def test_an_existing_player_is_reused_not_restarted(self, running, monkeypatch):
        running()
        monkeypatch.setattr(ipc, "spawn_player", lambda *a, **k: pytest.fail("spawned a second"))
        conn, status = ipc.connect_or_start()
        assert conn is not None and status is None

    def test_a_player_that_dies_starting_is_an_error_not_a_hang(self, tmp_path, monkeypatch):
        (tmp_path / "dying_playerd.py").write_text("import sys; sys.exit(3)\n")
        monkeypatch.setenv("PYTHONPATH", str(tmp_path))
        monkeypatch.setattr(ipc, "PLAYER_MODULE", "dying_playerd")
        started = time.monotonic()
        conn, status = ipc.connect_or_start()
        assert conn is None and status.startswith("error:")
        assert time.monotonic() - started < 5


LOGIN_PLAYER = """
import os, sys
fd = int(sys.argv[sys.argv.index("--ready-fd") + 1])
os.write(fd, b"login\\n")
"""


class TestStartRace:
    def _hold_lock(self):
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        throttle.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (throttle.STATE_DIR / ipc.LOCK_NAME).write_text(f"{holder.pid}\n")
        return holder

    def test_a_starter_that_leaves_at_once_does_not_strand_the_next_client(self, running,
                                                                           monkeypatch):
        holder = self._hold_lock()
        spawns = []

        def spawn(*args, **kwargs):
            spawns.append(args)
            if len(spawns) == 1:
                # The other client's player holds the lock, then exits: its starter already left.
                threading.Timer(0.2, lambda: (holder.kill(), holder.wait())).start()
                return "running"
            running()
            return "ready"

        monkeypatch.setattr(ipc, "spawn_player", spawn)
        conn, status = ipc.connect_or_start(timeout=5)
        assert status is None and conn is not None
        assert len(spawns) == 2, "the client started a player of its own once the other left"
        assert conn.request("status", timeout=2)["ok"]

    def test_a_player_slower_than_the_deadline_is_an_error_not_a_hang(self, monkeypatch):
        holder = self._hold_lock()
        try:
            monkeypatch.setattr(ipc, "spawn_player", lambda *a, **k: "running")
            started = time.monotonic()
            conn, status = ipc.connect_or_start(timeout=0.5)
            assert conn is None and status.startswith("error:") and "in time" in status
            assert time.monotonic() - started < 2
        finally:
            holder.kill()
            holder.wait()

    def test_a_player_that_exits_at_once_is_reaped(self, tmp_path, monkeypatch):
        (tmp_path / "login_playerd.py").write_text(LOGIN_PLAYER)
        monkeypatch.setenv("PYTHONPATH", str(tmp_path))
        monkeypatch.setattr(ipc, "PLAYER_MODULE", "login_playerd")
        children = []
        real = subprocess.Popen

        def popen(*args, **kwargs):
            children.append(real(*args, **kwargs))
            return children[-1]

        monkeypatch.setattr(ipc.subprocess, "Popen", popen)
        assert ipc.connect_or_start() == (None, "login")
        assert children and children[0].returncode is not None, "no zombie left behind"


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class _Live:
    def __init__(self):
        self.updates = 0

    def update(self, display, refresh=False):
        self.updates += 1


class TestRepaintCadence:
    def _ui(self, monkeypatch, playing):
        clock = _Clock()
        monkeypatch.setattr(player_mod.time, "time", clock)
        ui = HeadlessTidalPlayer(remote=types.SimpleNamespace())
        ui.console = Console(file=io.StringIO(), width=100, height=40)
        ui._show_artwork = False
        ui._apply_state({"track": _track(1), "queue": [_track(1)], "queue_index": 0,
                         "clock": [playing, 12.3, clock.now if playing else None]})
        return ui, clock

    def _simulate(self, ui, clock, seconds):
        live = _Live()
        ui._repaint(live, force=True)
        live.updates = 0
        wakeups = 0
        end = clock.now + seconds
        while True:
            timeout = ui._wait_timeout()
            if timeout is None or clock.now + timeout > end:
                break
            clock.now += timeout
            wakeups += 1
            ui._repaint(live)
        return wakeups, live.updates

    def test_playing_repaints_once_per_displayed_second(self, monkeypatch):
        ui, clock = self._ui(monkeypatch, playing=True)
        wakeups, repaints = self._simulate(ui, clock, 10)
        assert wakeups <= 10
        assert repaints == wakeups, "every wake lands on a new second"

    def test_paused_never_wakes(self, monkeypatch):
        ui, clock = self._ui(monkeypatch, playing=False)
        assert ui._wait_timeout() is None
        assert self._simulate(ui, clock, 60) == (0, 0)

    def test_a_toast_wakes_once_more_to_clear_itself(self, monkeypatch):
        ui, clock = self._ui(monkeypatch, playing=False)
        ui._set_toast("hello", seconds=2.5)
        assert ui._wait_timeout() == pytest.approx(2.5 + player_mod.SECOND_EDGE)
        clock.now += 3
        assert ui._wait_timeout() is None

    def test_the_wake_lands_just_past_the_second_boundary(self, monkeypatch):
        ui, clock = self._ui(monkeypatch, playing=True)
        assert ui._wait_timeout() == pytest.approx(0.7 + player_mod.SECOND_EDGE)


class TestTheRealLoop:
    def test_sigterm_ends_a_paused_tui_and_leaves_the_player(self):
        """Paused, the loop waits with no timeout, and Python retries an interrupted
        select: without the self-pipe wake a closed terminal would leave it hanging."""
        import pty
        import signal

        pid, fd = pty.fork()
        if pid == 0:
            code = 1
            try:
                # pytest's capture objects stand in for these; the child needs the pty itself.
                sys.stdin = os.fdopen(0, "r")
                sys.stdout = os.fdopen(1, "w")
                run = _Running(_core(playing=False))
                ui = HeadlessTidalPlayer(remote=ipc.connect())
                ui.console = Console(file=sys.stdout, force_terminal=True, width=80, height=24)
                ui._show_artwork = False
                threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM)).start()
                ui.run()
                code = 0 if run.thread.is_alive() and run.core.audio.stopped == 0 else 2
            finally:
                os._exit(code)
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            try:
                if select.select([fd], [], [], 0.1)[0]:
                    os.read(fd, 65536)
            except OSError:
                pass
        else:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("SIGTERM did not end a paused TUI")
        os.close(fd)
        assert os.waitstatus_to_exitcode(status) == 0, "the player must outlive its TUI"
