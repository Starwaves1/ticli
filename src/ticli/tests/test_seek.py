"""Tests for scrubbing within a track.

Covers the interaction (↑ hands the arrows to the player, ←/→ then move inside
the song), the bookkeeping the progress bar and the resume position read, the
clamps at both ends, the coalescing that keeps a held-down arrow from becoming
one respawn per key repeat, and both backends underneath — mpv over IPC and
ffplay killed and started again at the offset. No TIDAL session, no real
player process.
"""

import subprocess
import time
import types

import pytest

from ticli import player as player_mod
from ticli.player import AudioPlayer, HeadlessTidalPlayer
from ticli.utils import config as config_mod

KEY_UP = player_mod.KEY_UP
KEY_DOWN = player_mod.KEY_DOWN
KEY_LEFT = player_mod.KEY_LEFT
KEY_RIGHT = player_mod.KEY_RIGHT
KEY_ESC = player_mod.KEY_ESC
STEP = player_mod.SEEK_STEP_SECONDS


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    """Keep every player built here off the real ~/.config/ticli."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")


def _fake_track(tid=1, duration=200):
    return types.SimpleNamespace(id=tid, name=f"Track {tid}", duration=duration,
                                 artists=[], album=None)


def _wait_for(cond, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return False


class _FakeAudio:
    """Stands in for AudioPlayer: records absolute seeks, never a process."""

    def __init__(self, ok=True):
        self.ok = ok
        self.seeks = []

    def seek_to(self, position):
        self.seeks.append(position)
        return self.ok

    def seek_to_start(self):
        return True


def _make_player(position=100.0, playing=True, duration=200, seek_ok=True):
    p = HeadlessTidalPlayer()
    p._queue = [_fake_track(1, duration), _fake_track(2, duration)]
    p._queue_index = 0
    p._current_track = p._queue[0]
    p._playing = playing
    p._play_offset = position
    p._play_start_time = time.time() if playing else None
    p.audio = _FakeAudio(seek_ok)
    p._plays = []
    p._play_track = lambda track, seek=0: p._plays.append((track.id, seek))
    return p


def _settled(p):
    """Wait for the seek worker thread to have finished the one it took.

    Not the same as "nothing is pending": a burst deliberately leaves its last
    position waiting for the coalescing interval, which is the monitor's tick
    to deliver.
    """
    assert _wait_for(lambda: not p._seek_applying)


class TestSeekKeys:
    """←/→ scrub, but only once the player has the arrows."""

    def test_arrows_still_skip_tracks_until_the_player_is_focused(self):
        p = _make_player()
        skipped = []
        p._next_track = lambda: skipped.append("next")
        p._prev_track = lambda: skipped.append("prev")
        p._handle_key(KEY_RIGHT)
        p._handle_key(KEY_LEFT)
        assert skipped == ["next", "prev"]
        assert p.audio.seeks == []

    def test_up_on_the_player_screen_focuses_it(self):
        p = _make_player()
        p._handle_key(KEY_UP)
        assert p._player_focus is True

    def test_focused_arrows_seek_instead_of_skipping(self):
        p = _make_player(position=100.0)
        skipped = []
        p._next_track = lambda: skipped.append("next")
        p._handle_key(KEY_UP)
        p._handle_key(KEY_RIGHT)
        _settled(p)
        assert skipped == []
        assert p.audio.seeks == [pytest.approx(110, abs=0.5)]

    def test_left_seeks_backwards(self):
        p = _make_player(position=100.0)
        p._handle_key(KEY_UP)
        p._handle_key(KEY_LEFT)
        _settled(p)
        assert p.audio.seeks == [pytest.approx(90, abs=0.5)]

    def test_down_hands_the_arrows_back(self):
        p = _make_player()
        p._handle_key(KEY_UP)
        p._handle_key(KEY_DOWN)
        assert p._player_focus is False
        skipped = []
        p._next_track = lambda: skipped.append("next")
        p._handle_key(KEY_RIGHT)
        assert skipped == ["next"]

    def test_escape_unfocuses_before_it_quits(self):
        p = _make_player()
        p._handle_key(KEY_UP)
        p._handle_key(KEY_ESC)
        assert p._player_focus is False
        assert p._quit_pending is False
        p._handle_key(KEY_ESC)
        assert p._quit_pending is True

    def test_any_other_key_drops_focus_and_still_does_its_job(self):
        p = _make_player()
        p._handle_key(KEY_UP)
        p._handle_key("t")  # tiny player
        assert p._player_focus is False
        assert p._mini_player is True

    def test_space_still_toggles_while_focused(self):
        p = _make_player()
        toggled = []
        p._toggle_play = lambda: toggled.append(True)
        p._handle_key(KEY_UP)
        p._handle_key(" ")
        assert toggled == [True]
        assert p._player_focus is True  # play/pause is not "done scrubbing"

    def test_nothing_playing_means_nothing_to_focus(self):
        p = _make_player()
        p._current_track = None
        p._handle_key(KEY_UP)
        assert p._player_focus is False


class TestFocusFromLists:
    """↑ at the top of a list is where the player takes the arrows over."""

    def _search_player(self):
        p = _make_player()
        p._mode = p.MODE_SEARCH
        p._search_results = [{"type": "track", "name": "A", "obj": None},
                             {"type": "track", "name": "B", "obj": None}]
        p._search_cursor = 1
        return p

    def test_up_inside_the_list_only_moves_the_cursor(self):
        p = self._search_player()
        p._handle_key(KEY_UP)
        assert p._search_cursor == 0
        assert p._player_focus is False

    def test_up_at_the_top_focuses_the_player(self):
        p = self._search_player()
        p._search_cursor = 0
        p._handle_key(KEY_UP)
        assert p._player_focus is True

    def test_focused_arrows_do_not_leave_the_search_screen(self):
        p = self._search_player()
        p._search_cursor = 0
        p._handle_key(KEY_UP)
        p._handle_key(KEY_LEFT)  # ← is "back" in search when unfocused
        _settled(p)
        assert p._mode == p.MODE_SEARCH
        assert p.audio.seeks == [pytest.approx(90, abs=0.5)]

    def test_typing_returns_to_the_query(self):
        p = self._search_player()
        p._search_cursor = 0
        p._handle_key(KEY_UP)
        p._handle_key("x")
        assert p._player_focus is False
        assert p._search_query == "x"

    def test_down_returns_to_the_list_where_it_was(self):
        p = self._search_player()
        p._search_cursor = 0
        p._handle_key(KEY_UP)
        p._handle_key(KEY_DOWN)
        assert p._player_focus is False
        assert p._search_cursor == 0  # the list did not move under the user

    def test_queue_and_playlists_focus_the_same_way(self):
        p = _make_player()
        p._mode = p.MODE_QUEUE
        p._queue_cursor = 0
        p._handle_key(KEY_UP)
        assert p._player_focus is True
        p._player_focus = False
        p._mode = p.MODE_PLAYLISTS
        p._playlists = [types.SimpleNamespace(id="p1", name="Jazz")]
        p._playlists_cursor = 0
        p._handle_key(KEY_UP)
        assert p._player_focus is True

    def test_an_empty_list_focuses_on_the_first_press(self):
        p = _make_player()
        p._mode = p.MODE_BROWSE
        p._browse_tracks = []
        p._handle_key(KEY_UP)
        assert p._player_focus is True


class TestPositionBookkeeping:
    """The clock has to be right the instant the key lands, and stay right."""

    def test_the_position_moves_before_the_backend_is_touched(self):
        p = _make_player(position=100.0)
        p.audio.seek_to = lambda pos: (_ for _ in ()).throw(AssertionError("too early"))
        p._player_focus = True
        p._seek_applying = True  # pretend a flush is already in flight
        p._handle_key(KEY_RIGHT)
        assert p._get_position() == pytest.approx(110, abs=0.5)

    def test_the_bar_paints_the_new_position(self):
        p = _make_player(position=100.0, duration=200)
        p._player_focus = True
        p._handle_key(KEY_RIGHT)
        assert "1:50" in p._build_player_display().plain

    def test_the_clock_runs_on_from_the_new_position(self):
        p = _make_player(position=100.0)
        p._seek_by(STEP)
        _settled(p)
        time.sleep(0.05)
        assert p._get_position() > 110

    def test_a_paused_seek_does_not_start_the_clock(self):
        p = _make_player(position=100.0, playing=False)
        p._seek_by(STEP)
        _settled(p)
        assert p._play_start_time is None
        assert p._get_position() == pytest.approx(110)

    def test_the_saved_position_follows_the_seek(self, tmp_path, monkeypatch):
        monkeypatch.setattr(player_mod, "STATE_FILE", tmp_path / "state.json")
        p = _make_player(position=100.0)
        p._seek_by(STEP)
        _settled(p)
        p._save_state()
        import json
        saved = json.loads((tmp_path / "state.json").read_text())
        assert saved["position"] == pytest.approx(110, abs=1)

    def test_a_track_change_abandons_a_pending_seek(self):
        p = _make_player(position=100.0)
        p._play_track = HeadlessTidalPlayer._play_track.__get__(p)
        p._seek_target = 150.0
        p._seek_applied = None
        p._playing = False
        p._play_track(_fake_track(2), seek=0)  # no session: the thread bails out
        assert p._seek_target is None


class TestClamping:
    """Neither end of the track is a way out of it."""

    def test_seeking_past_the_start_lands_on_zero(self):
        p = _make_player(position=4.0)
        p._seek_by(-STEP)
        _settled(p)
        assert p._get_position() == pytest.approx(0, abs=0.1)
        assert p.audio.seeks == [0]
        assert p._queue_index == 0  # not the previous track

    def test_seeking_past_the_end_stops_short_of_it(self):
        p = _make_player(position=195.0, duration=200)
        p._seek_by(STEP)
        _settled(p)
        assert p.audio.seeks == [pytest.approx(198, abs=0.5)]
        assert p._plays == []  # never the next track

    def test_the_end_clamp_is_the_same_however_often_you_press(self):
        p = _make_player(position=195.0, duration=200)
        for _ in range(5):
            p._seek_by(STEP)
        _settled(p)
        assert p._get_position() == pytest.approx(198, abs=0.5)

    def test_an_unknown_duration_only_clamps_the_start(self):
        p = _make_player(position=5.0, duration=0)
        p._seek_by(STEP)
        _settled(p)
        assert p._get_position() == pytest.approx(15, abs=0.5)

    def test_seeking_with_no_track_does_nothing(self):
        p = _make_player()
        p._current_track = None
        p._seek_by(STEP)
        assert p.audio.seeks == []


class TestCoalescing:
    """A held-down arrow is one seek every so often, not one per repeat."""

    def test_a_burst_of_presses_reaches_the_backend_once(self):
        p = _make_player(position=10.0, duration=10000)
        p._player_focus = True
        for _ in range(30):
            p._handle_key(KEY_RIGHT)
        _settled(p)
        assert len(p.audio.seeks) == 1
        # ...but every one of them counted towards where we ended up
        assert p._get_position() == pytest.approx(10 + 30 * STEP, abs=1)

    def test_the_position_the_burst_ended_on_is_the_one_delivered(self):
        p = _make_player(position=10.0, duration=10000)
        for _ in range(5):
            p._seek_by(STEP)
        _settled(p)
        # The first press went straight out; the monitor's tick delivers the
        # rest, and what it delivers is where the burst finished
        p._last_seek_apply = 0
        p._flush_seek()
        _settled(p)
        assert p.audio.seeks[-1] == pytest.approx(60, abs=1)

    def test_a_pending_seek_is_visible_to_the_monitor(self):
        p = _make_player(position=10.0)
        p._seek_by(STEP)
        _settled(p)
        p._seek_by(STEP)          # too soon: left pending
        assert p._seek_pending()
        p._last_seek_apply = 0    # the interval has passed
        p._flush_seek()
        _settled(p)
        assert not p._seek_pending()
        assert p.audio.seeks == [pytest.approx(20, abs=1), pytest.approx(30, abs=1)]

    def test_nothing_is_sent_twice(self):
        p = _make_player(position=10.0)
        p._seek_by(STEP)
        _settled(p)
        p._last_seek_apply = 0
        p._flush_seek()
        _settled(p)
        assert len(p.audio.seeks) == 1

    def test_a_backend_that_cannot_seek_is_restarted_once(self):
        p = _make_player(position=10.0, seek_ok=False)
        for _ in range(20):
            p._seek_by(STEP)
        _settled(p)
        assert len(p._plays) == 1
        assert p._plays[0][0] == 1  # the same track, at the new position


# ── the backends themselves ──


class _FakeProcess:
    def __init__(self, alive=True):
        self._alive = alive
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


@pytest.fixture
def spawned(monkeypatch):
    """Record every process the audio player tries to start."""
    calls = []

    def _popen(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProcess()

    monkeypatch.setattr(player_mod.subprocess, "Popen", _popen)
    monkeypatch.setattr(AudioPlayer, "_open_stderr", lambda self: subprocess.DEVNULL)
    return calls


class TestMpvSeek:
    def test_a_live_mpv_is_seeked_over_ipc(self):
        audio = AudioPlayer("mpv")
        audio._ipc_path = "/tmp/nope.sock"
        audio._process = _FakeProcess()
        sent = []
        audio._mpv_command = lambda cmd: sent.append(cmd) or True
        assert audio.seek_to(42.5) is True
        assert sent == [{"command": ["seek", 42.5, "absolute"]}]
        assert audio._seek_offset == 42.5

    def test_a_paused_mpv_seeks_without_starting_the_clock(self):
        audio = AudioPlayer("mpv")
        audio._ipc_path = "/tmp/nope.sock"
        audio._process = _FakeProcess()
        audio._paused = True
        audio._mpv_command = lambda cmd: True
        assert audio.seek_to(30) is True
        assert audio._play_start is None

    def test_an_unacknowledged_seek_is_a_failure(self):
        audio = AudioPlayer("mpv")
        audio._ipc_path = "/tmp/nope.sock"
        audio._process = _FakeProcess()
        audio._mpv_command = lambda cmd: False
        assert audio.seek_to(42) is False

    def test_a_dead_mpv_is_a_failure(self):
        audio = AudioPlayer("mpv")
        audio._ipc_path = "/tmp/nope.sock"
        audio._process = _FakeProcess(alive=False)
        assert audio.seek_to(42) is False


class TestFfplaySeek:
    def test_playing_ffplay_is_killed_and_started_at_the_offset(self, spawned):
        audio = AudioPlayer("ffplay")
        audio._current_url = "http://cdn/track.m4a"
        old = _FakeProcess()
        audio._process = old
        assert audio.seek_to(42.5) is True
        assert old.terminated
        assert spawned == [["ffplay", "-nodisp", "-autoexit", "-loglevel", "error",
                            "-volume", "100", "-ss", "42.5", "http://cdn/track.m4a"]]
        assert audio._seek_offset == 42.5

    def test_it_seeks_in_the_cached_copy_when_there_is_one(self, spawned, tmp_path):
        cached = tmp_path / "1.m4a"
        cached.write_bytes(b"audio")
        audio = AudioPlayer("ffplay")
        audio._current_url = "http://cdn/track.m4a"
        audio._cache_file = str(cached)
        audio._process = _FakeProcess()
        assert audio.seek_to(10) is True
        assert spawned[0][-1] == str(cached)

    def test_a_segmented_stream_keeps_its_flags(self, spawned):
        audio = AudioPlayer("ffplay")
        audio._current_url = "/tmp/ticli/1.m3u8"
        audio._process = _FakeProcess()
        audio.seek_to(10)
        assert "-f" in spawned[0] and "hls" in spawned[0]

    def test_the_download_in_flight_is_not_abandoned(self, spawned):
        """A scrub must not bump the download generation — that is what tells
        the copy being fetched for this very track to delete itself."""
        audio = AudioPlayer("ffplay")
        audio._current_url = "http://cdn/track.m4a"
        audio._process = _FakeProcess()
        before = audio._download_gen
        audio.seek_to(10)
        assert audio._download_gen == before

    def test_a_paused_ffplay_only_moves_the_resume_point(self, spawned):
        audio = AudioPlayer("ffplay")
        audio._paused = True
        audio._current_url = "http://cdn/track.m4a"
        assert audio.seek_to(75) is True
        assert audio._seek_offset == 75
        assert spawned == []  # nothing is started until resume

    def test_a_stopped_ffplay_is_a_failure(self, spawned):
        audio = AudioPlayer("ffplay")
        audio._current_url = "http://cdn/track.m4a"
        assert audio.seek_to(75) is False
        assert spawned == []

    def test_with_no_source_at_all_it_fails(self, spawned):
        audio = AudioPlayer("ffplay")
        audio._process = _FakeProcess()
        assert audio.seek_to(75) is False
