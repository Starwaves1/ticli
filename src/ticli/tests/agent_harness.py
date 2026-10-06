"""An in-process player for agent tests: a real socket and player core in a
thread, TIDAL as `fakes.FakeTidal`, time as `fakes.FakeClock`."""

import io
import threading
import time

from rich.console import Console

from ticli import ipc, playerd
from ticli.player import HeadlessTidalPlayer
from ticli.tests.fakes import FakeClock, FakeTidal, fake_track
from ticli.utils import throttle


class _Audio:
    player_cmd = "mpv"
    is_paused = False
    is_playing = True

    def volume_ceiling(self):
        return 130

    def pause(self):
        self.is_paused = True

    def resume(self):
        self.is_paused = False
        return True

    def stop(self):
        pass

    def get_time_pos(self):
        return None

    def set_volume(self, value):
        pass


def make_core(session):
    core = HeadlessTidalPlayer()
    core.console = Console(file=io.StringIO())
    core.session = session
    core.audio = _Audio()
    core._queue = [fake_track(1), fake_track(2), fake_track(3)]
    core._queue_index = 0

    def _play(track, seek=0):
        core._current_track = track
        core._playing = True
        core._play_offset = seek
        core._play_start_time = time.time()
        core._wake()

    core._play_track = _play
    core._save_state = lambda: None
    core._remember_last_playlist = lambda playlist: None
    _play(core._queue[0])
    return core


class Harness:
    def __init__(self, session=None, clock=None):
        self.session = session or FakeTidal()
        self.clock = clock or FakeClock()
        self.core = make_core(self.session)
        self.road = self.session.add_playlist("road", "Road trip")
        self.gym = self.session.add_playlist("gym", "Gym")
        self.core._editable_playlists = [self.road, self.gym]
        self.server = playerd.PlayerServer(self.core, clock=self.clock, sleep=self.clock.sleep)
        self.server.listen()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.keeper = ipc.connect()  # a client stays, so the player doesn't leave between calls

    def _serve(self):
        try:
            self.server.serve()
        finally:
            self.server.close()

    def agent(self, cmd, args=None, key=None):
        conn = ipc.connect()
        try:
            return conn.request(cmd, args or {}, caller="agent", key=key, timeout=5)
        finally:
            conn.close()

    def human(self, cmd, args=None):
        conn = ipc.connect()
        try:
            return conn.request(cmd, args or {}, caller="human", timeout=5)
        finally:
            conn.close()

    def do(self, *items):
        return self.agent("agent.do", {"commands": [
            {"cmd": c, "args": a} for c, a in items]})

    def idle(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while self.server.agent_queue.busy():
            assert time.monotonic() < deadline, "the agent queue never drained"
            time.sleep(0.01)

    def hold(self):
        """Make the throttle busy and park the queue's sleeper, so commands wait."""
        throttle.acquire(now=self.clock, sleep=self.clock.sleep)
        self.clock.hold = threading.Event()

    def release(self):
        self.clock.hold.set()
        self.idle()

    def stop(self):
        self.keeper.close()
        self.core.running = False
        self.server.wake()
        self.thread.join(2)
