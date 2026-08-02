"""Tests for track radio ([r] on the player screen).

The load-bearing property is that starting a radio is a *queue* change and
nothing else: the song playing keeps playing, out of the same process and the
same stream URL, and only what comes after it changes. Everything here is
asserted against a fake audio backend and a fake track — no TIDAL session and
no player process.
"""

import threading
import time
import types

import pytest

from ticli import player as player_mod
from ticli.player import HeadlessTidalPlayer
from ticli.utils import config as config_mod
from ticli.utils.cache import CachedTrack


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    """Keep every player built here off the real ~/.config/ticli."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")


def _wait_for(cond, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return False


class _FakeAudio:
    """Stands in for AudioPlayer and records anything that would disturb the
    sound: a new process, a stop, a seek."""

    def __init__(self):
        self.calls = []
        self.is_paused = False
        self.is_playing = True

    def play_url(self, url, seek=0, title="", cache_key=None,
                 local=None, quality=None):
        self.calls.append(("play_url", url, seek))

    def stop(self):
        self.calls.append(("stop",))

    def pause(self):
        self.calls.append(("pause",))

    def resume(self):
        self.calls.append(("resume",))
        return True

    def seek_to(self, position):
        self.calls.append(("seek_to", position))
        return True

    def seek_to_start(self):
        self.calls.append(("seek_to_start",))
        return True

    def get_time_pos(self):
        return None


class _FakeTrack:
    """A track that can answer with a radio mix — or refuse to."""

    def __init__(self, tid=1, duration=200, radio=None, fail=False, delay=0.0):
        self.id = tid
        self.name = f"Track {tid}"
        self.duration = duration
        self.artists = []
        self.album = None
        self._radio = radio if radio is not None else []
        self._fail = fail
        self._delay = delay
        self.radio_calls = 0
        self.limits = []

    def get_track_radio(self, limit=100):
        self.radio_calls += 1
        self.limits.append(limit)
        if self._delay:
            time.sleep(self._delay)
        if self._fail:
            raise RuntimeError("no radio for this track")
        return list(self._radio)


def _mix(n=3, start=100):
    return [_FakeTrack(start + i) for i in range(n)]


def _make_player(current=None, queue=None, position=42.0, playing=True):
    p = HeadlessTidalPlayer()
    current = current if current is not None else _FakeTrack(1)
    p._queue = list(queue) if queue is not None else [current, _FakeTrack(2)]
    p._queue_index = p._queue.index(current) if current in p._queue else 0
    p._current_track = current
    p._playing = playing
    p._play_offset = position
    p._play_start_time = None  # a still clock, so positions compare exactly
    p.audio = _FakeAudio()
    p._plays = []
    p._real_play_track = p._play_track
    p._play_track = lambda track, seek=0: p._plays.append((track.id, seek))
    return p


def _settled(p):
    """Wait for the radio worker to have finished."""
    _wait_for(lambda: not p._radio_fetching, timeout=2.0)
    # The queue swap happens just after the flag drops; give it the same wait
    time.sleep(0.02)


class TestRadioDoesNotRestartTheSong:
    """The reported bug: [r] replayed the song you were listening to."""

    def test_playback_is_untouched(self):
        mix = _mix()
        current = _FakeTrack(1, radio=mix)
        p = _make_player(current)
        gen_before = p._play_gen

        p._handle_player_key("r")
        _settled(p)

        assert p.audio.calls == []          # no stop, no respawn, no seek
        assert p._plays == []               # _play_track never ran
        assert p._play_gen == gen_before    # nothing superseded the start
        assert p._play_offset == 42.0       # still where the song was
        assert p._playing is True
        assert p._current_track is current

    def test_one_stream_url_fetch_and_no_backend_call(self, monkeypatch):
        """The radio path asks TIDAL for the mix and for nothing else — no
        second get_stream() for a song already playing."""
        streams = []
        current = _FakeTrack(1, radio=_mix())
        p = _make_player(current)
        monkeypatch.setattr(p, "_stream_url", lambda t: streams.append(t.id))

        p._handle_player_key("r")
        _settled(p)

        assert streams == []
        assert current.radio_calls == 1


class TestRadioFromARestoredShim:
    def test_radio_on_a_record_restored_track_resolves_it_first(self):
        """A session restored from saved records leaves the current track as
        a cached shim until it is played, and a shim has no get_track_radio —
        [r] must resolve it through the session, not die on it."""
        real = _FakeTrack(1, radio=_mix())
        shim = CachedTrack({"id": 1, "name": "Track 1", "duration": 200,
                            "artists": []})
        p = _make_player(current=shim, queue=[shim, _FakeTrack(2)],
                         playing=False)
        p.session = types.SimpleNamespace(track=lambda tid: real)

        p._start_track_radio()
        _settled(p)

        assert real.radio_calls == 1
        assert [t.id for t in p._queue] == [1, 100, 101, 102]
        assert p._queue[0] is shim  # the restored row itself is untouched


class TestQueueAfterRadio:
    def test_current_track_heads_the_queue(self):
        mix = _mix(3)
        current = _FakeTrack(1, radio=mix)
        p = _make_player(current)

        p._start_track_radio()
        _settled(p)

        assert p._queue[0] is current
        assert [t.id for t in p._queue] == [1, 100, 101, 102]
        assert p._queue_index == 0

    def test_up_next_is_the_first_radio_track(self):
        mix = _mix(2)
        p = _make_player(_FakeTrack(1, radio=mix))

        p._start_track_radio()
        _settled(p)

        # What the player screen shows as "up next" is queue[index + 1]
        assert p._queue[p._queue_index + 1] is mix[0]

    def test_auto_advance_lands_on_the_radio(self):
        mix = _mix(2)
        p = _make_player(_FakeTrack(1, radio=mix))
        p._start_track_radio()
        _settled(p)

        p._next_track()

        assert p._plays == [(100, 0)]

    def test_seed_track_is_not_queued_twice(self):
        """TIDAL's mix often opens with the song it was seeded from."""
        current = _FakeTrack(1)
        current._radio = [_FakeTrack(1), _FakeTrack(100)]
        p = _make_player(current)

        p._start_track_radio()
        _settled(p)

        assert [t.id for t in p._queue] == [1, 100]

    def test_prefetch_is_rearmed(self):
        """A URL fetched for whatever used to be next is not next any more."""
        p = _make_player(_FakeTrack(1, radio=_mix()))
        p._prefetch = (2, "http://old", time.time())
        p._prefetch_id = 2

        p._start_track_radio()
        _settled(p)

        assert p._prefetch is None
        assert p._prefetch_id is None

    def test_saved_state_matches_the_new_queue(self, tmp_path, monkeypatch):
        import json
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path)
        monkeypatch.setattr(player_mod, "STATE_FILE", tmp_path / "state.json")
        p = _make_player(_FakeTrack(1, radio=_mix(2)))

        p._start_track_radio()
        _settled(p)
        p._save_state()

        state = json.loads((tmp_path / "state.json").read_text())
        assert state["track_ids"] == [1, 100, 101]
        assert state["queue_index"] == 0


class TestOneRequest:
    def test_rapid_presses_cost_one_fetch(self):
        current = _FakeTrack(1, radio=_mix(), delay=0.05)
        p = _make_player(current)

        for _ in range(40):
            p._handle_player_key("r")
        _settled(p)

        assert current.radio_calls == 1

    def test_presses_after_the_reply_still_wait_out_the_interval(self):
        """Single-flight alone would let a held key fan out between two fast
        replies; the interval is the floor between them."""
        current = _FakeTrack(1, radio=_mix())
        p = _make_player(current)

        p._start_track_radio()
        _settled(p)
        for _ in range(10):
            p._start_track_radio()
        _settled(p)

        assert current.radio_calls == 1

    def test_a_deliberate_later_press_is_answered(self, monkeypatch):
        monkeypatch.setattr(player_mod, "RADIO_FETCH_MIN_INTERVAL", 0.0)
        current = _FakeTrack(1, radio=_mix())
        p = _make_player(current)

        p._start_track_radio()
        _settled(p)
        p._start_track_radio()
        _settled(p)

        assert current.radio_calls == 2

    def test_fetch_runs_off_the_ui_thread(self):
        seen = []
        current = _FakeTrack(1, radio=_mix())
        original = current.get_track_radio

        def _record(limit=100):
            seen.append(threading.current_thread() is threading.main_thread())
            return original(limit=limit)

        current.get_track_radio = _record
        p = _make_player(current)

        p._start_track_radio()
        _settled(p)

        assert seen == [False]


class TestRadioFailure:
    def test_failed_fetch_keeps_playing_and_says_so(self):
        current = _FakeTrack(1, fail=True)
        queue = [current, _FakeTrack(2)]
        p = _make_player(current, queue=queue)

        p._start_track_radio()
        _settled(p)

        assert p._playing is True
        assert p.audio.calls == []
        assert p._plays == []
        assert [t.id for t in p._queue] == [1, 2]      # queue untouched
        assert "Radio unavailable" in p._toast

    def test_empty_mix_is_a_failure_not_an_empty_queue(self):
        current = _FakeTrack(1, radio=[])
        p = _make_player(current)

        p._start_track_radio()
        _settled(p)

        assert [t.id for t in p._queue] == [1, 2]
        assert "Radio unavailable" in p._toast

    def test_a_mix_of_only_the_seed_is_a_failure(self):
        current = _FakeTrack(1)
        current._radio = [_FakeTrack(1)]
        p = _make_player(current)

        p._start_track_radio()
        _settled(p)

        assert [t.id for t in p._queue] == [1, 2]
        assert "Radio unavailable" in p._toast

    def test_no_current_track_is_a_no_op(self):
        p = _make_player()
        p._current_track = None

        p._start_track_radio()

        assert p._radio_fetching is False


class TestStaleMix:
    def test_a_mix_for_a_song_already_skipped_past_is_dropped(self):
        current = _FakeTrack(1, radio=_mix(), delay=0.1)
        queue = [current, _FakeTrack(2)]
        p = _make_player(current, queue=queue)

        p._start_track_radio()
        # The user skips on while the mix is in flight — every track change
        # bumps _play_gen, which is what the worker checks
        p._play_gen += 1
        p._current_track = queue[1]
        _settled(p)

        assert [t.id for t in p._queue] == [1, 2]

    def test_a_pending_restore_does_not_clobber_the_radio_queue(self):
        """At startup the restored track is current but its queue is still
        being fetched; radio leaves that track playing, so the restore's own
        "did anything change" check cannot see the swap."""
        current = _FakeTrack(1, radio=_mix(2))
        p = _make_player(current, queue=[current])
        p._restore_pending = True

        p._start_track_radio()
        _settled(p)

        assert p._restore_pending is False
        assert [t.id for t in p._queue] == [1, 100, 101]


class TestQueueScreen:
    def test_cursor_points_at_the_playing_track(self):
        p = _make_player(_FakeTrack(1, radio=_mix(2)))
        p._queue_cursor = 1

        p._start_track_radio()
        _settled(p)

        assert p._queue_cursor == 0

    def test_going_back_to_a_shorter_queue_clamps_the_cursor(self):
        p = _make_player(_FakeTrack(1, radio=_mix(1)))
        p._mode = p.MODE_QUEUE
        p._queue_cursor = 7
        p._push_nav()
        p._mode = p.MODE_PLAYER
        p._queue = [p._current_track]

        p._go_back()

        assert p._queue_cursor == 0
        assert p._mode == p.MODE_QUEUE

    def test_queue_screen_renders_the_radio_rows(self):
        p = _make_player(_FakeTrack(1, radio=_mix(2)))
        p._start_track_radio()
        _settled(p)
        p._mode = p.MODE_QUEUE

        text = p._build_queue_display().plain

        assert "Track 100" in text and "Track 101" in text
        assert "(3 tracks)" in text
