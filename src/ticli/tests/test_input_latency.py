"""Tests for the input and repaint path — the things that make the player
feel fast or feel laggy.

Covers: splitting a key-repeat burst into whole keys (an arrow used to eat the
next arrow's bytes and both were dropped), play/pause repeat suppression that
no longer swallows genuine quick presses, the repaint skipping identical
frames while idle, and track start-up not blocking the UI thread on the
network. No TIDAL session, no real player process, no network.
"""

import io
import os
import select
import sys
import time
import types

import pytest

from ticli.player import (
    KEY_DOWN,
    KEY_ESC,
    KEY_UP,
    HeadlessTidalPlayer,
    _incomplete_escape,
    _split_keys,
)
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="pty/termios input path is POSIX-only")


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    """Keep every player built here off the real ~/.config/ticli."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    # ...and off the real cache, which is where the download index lives
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")


def _fake_track(tid, duration=200):
    return types.SimpleNamespace(
        id=tid, name=f"Track {tid}", duration=duration,
        artists=[types.SimpleNamespace(name=f"Artist {tid}")],
        album=types.SimpleNamespace(name=f"Album {tid}", id=tid),
    )


class _FakeAudio:
    """Stands in for AudioPlayer: records play_url calls, no process."""

    def __init__(self):
        self.plays = []
        self.is_paused = False

    def play_url(self, url, seek=0, title="", cache_key=None, local=None,
                 quality=None):
        self.plays.append((url, seek, title))


def _make_player():
    p = HeadlessTidalPlayer()
    p._queue = [_fake_track(i) for i in range(5)]
    p._queue_index = 0
    p._current_track = p._queue[0]
    p.audio = _FakeAudio()
    return p


class TestKeySplitting:
    """One read can hold a whole burst of keys; each has to survive it."""

    def test_arrow_burst_is_not_swallowed(self):
        # Reading a fixed 8 bytes used to let the first arrow absorb the
        # second one's bytes, so both were dropped
        assert _split_keys(KEY_DOWN * 4) == [KEY_DOWN] * 4

    def test_mixed_burst(self):
        assert _split_keys(f"ab{KEY_UP}c{KEY_DOWN}") == ["a", "b", KEY_UP, "c", KEY_DOWN]

    def test_bare_escape_stays_escape(self):
        assert _split_keys(KEY_ESC) == [KEY_ESC]

    def test_modified_arrow_kept_whole(self):
        assert _split_keys("\x1b[1;5A") == ["\x1b[1;5A"]

    def test_escape_followed_by_typing(self):
        assert _split_keys("\x1bab") == [KEY_ESC, "a", "b"]

    @pytest.mark.parametrize("text,expected", [
        ("", False),
        ("abc", False),
        ("\x1b", True),
        ("\x1b[", True),
        ("\x1b[1;5", True),
        ("\x1b[A", False),
        (KEY_DOWN * 2, False),
        ("a\x1b[", True),
    ])
    def test_incomplete_escape(self, text, expected):
        assert _incomplete_escape(text) is expected


@POSIX_ONLY
class TestReadKeys:
    """_read_keys against a real pty."""

    @pytest.fixture
    def pty(self, monkeypatch):
        import tty

        master, slave = os.openpty()
        tty.setcbreak(slave)
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(fileno=lambda: slave))
        yield master
        os.close(master)
        os.close(slave)

    def test_whole_repeat_burst_is_returned_in_one_pass(self, pty):
        p = _make_player()
        os.write(pty, KEY_DOWN.encode() * 10)
        time.sleep(0.05)
        assert p._read_keys(select, timeout=0.5) == [KEY_DOWN] * 10

    def test_single_key(self, pty):
        p = _make_player()
        os.write(pty, b"j")
        assert p._read_keys(select, timeout=0.5) == ["j"]

    def test_timeout_returns_no_keys(self, pty):
        p = _make_player()
        assert p._read_keys(select, timeout=0.01) == []


class TestPlayPauseRepeat:
    """Key repeat must not toggle in a loop, but two real presses in quick
    succession must both land (the old held-flag ate the second one)."""

    def _counting_player(self):
        p = _make_player()
        p._toggles = 0
        p._toggle_play = lambda: setattr(p, "_toggles", p._toggles + 1)
        return p

    def test_two_quick_presses_both_toggle(self):
        p = self._counting_player()
        p._toggle_play_key()
        time.sleep(0.16)  # just past the repeat window — a deliberate second press
        p._toggle_play_key()
        assert p._toggles == 2

    def test_repeat_burst_toggles_once(self):
        p = self._counting_player()
        for _ in range(20):  # a held key, ~30ms apart
            p._toggle_play_key()
            time.sleep(0.005)
        assert p._toggles == 1

    def test_other_keys_do_not_block_a_later_space(self):
        p = self._counting_player()
        p._handle_player_key(" ")
        p._handle_player_key("m")
        time.sleep(0.16)
        p._handle_player_key(" ")
        assert p._toggles == 2

    def test_space_works_on_every_screen(self):
        for mode, handler in (
            ("MODE_QUEUE", "_handle_queue_key"),
            ("MODE_BROWSE", "_handle_browse_key"),
            ("MODE_PLAYLISTS", "_handle_playlists_key"),
            ("MODE_SETTINGS", "_handle_settings_key"),
        ):
            p = self._counting_player()
            p._mode = getattr(p, mode)
            getattr(p, handler)(" ")
            assert p._toggles == 1, mode


class _FakeLive:
    """Records every repaint that actually reached the terminal."""

    def __init__(self):
        self.updates = 0

    def update(self, renderable, refresh=False):
        self.updates += 1


class TestRepaint:
    """Repaint immediately on input, stay quiet when nothing moved."""

    def _player(self):
        from rich.console import Console

        p = _make_player()
        p.console = Console(file=io.StringIO(), force_terminal=True, width=100, height=40)
        p._playing = False
        return p

    def test_identical_frame_is_not_rewritten(self):
        p = self._player()
        live = _FakeLive()
        p._repaint(live, force=True)
        for _ in range(10):
            p._repaint(live)
        assert live.updates == 1

    def test_forced_repaint_always_writes(self):
        p = self._player()
        live = _FakeLive()
        p._repaint(live, force=True)
        p._repaint(live, force=True)
        assert live.updates == 2

    def test_changed_state_repaints_on_an_idle_tick(self):
        p = self._player()
        live = _FakeLive()
        p._repaint(live, force=True)
        p._mode = p.MODE_QUEUE  # e.g. a background thread changed something
        p._repaint(live)
        assert live.updates == 2


class TestPlayTrackOffUiThread:
    """The stream URL is a TIDAL round trip; the UI must not wait on it."""

    def _track(self, tid, delay=0.0, url="http://stream"):
        t = _fake_track(tid)

        def get_stream():
            time.sleep(delay)
            manifest = types.SimpleNamespace(is_bts=True, get_urls=lambda: [url])
            return types.SimpleNamespace(
                audio_quality="HIGH",
                get_stream_manifest=lambda: manifest,
            )

        t.get_stream = get_stream
        return t

    def test_ui_thread_returns_before_the_network_call_finishes(self):
        p = _make_player()
        track = self._track(9, delay=0.3)
        start = time.monotonic()
        p._play_track(track)
        elapsed = time.monotonic() - start
        assert elapsed < 0.1, f"_play_track blocked the UI thread for {elapsed:.2f}s"
        # The new track shows immediately, with its clock held until audio starts
        assert p._current_track is track
        assert p._playing is True
        assert p._play_start_time is None

    def test_playback_starts_once_the_url_arrives(self):
        p = _make_player()
        p._play_track(self._track(9, url="http://ok"))
        deadline = time.monotonic() + 2
        while not p.audio.plays and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [u for u, _, _ in p.audio.plays] == ["http://ok"]
        assert p._track_changing is False

    def test_a_stale_request_cannot_clobber_a_newer_one(self):
        p = _make_player()
        p._play_track(self._track(1, delay=0.3, url="http://slow"))
        time.sleep(0.02)
        p._play_track(self._track(2, url="http://fast"))
        time.sleep(0.5)
        assert [u for u, _, _ in p.audio.plays] == ["http://fast"]
        assert p._current_track.id == 2

    def test_pausing_during_the_fetch_cancels_the_start(self):
        # Space used to be impossible here because the UI was frozen; now it
        # lands mid-fetch and must not be overridden when the URL arrives
        p = _make_player()
        p._play_track(self._track(9, delay=0.2))
        p._playing = False  # what _toggle_play does on a pause
        time.sleep(0.4)
        assert p.audio.plays == []
        assert p._playing is False

    def test_a_failing_url_stops_playback_instead_of_raising(self):
        p = _make_player()
        track = _fake_track(9)
        def boom():
            raise RuntimeError("no stream")
        track.get_url = boom
        p._play_track(track)
        deadline = time.monotonic() + 2
        while p._playing and time.monotonic() < deadline:
            time.sleep(0.01)
        assert p._playing is False
        assert p._track_changing is False
