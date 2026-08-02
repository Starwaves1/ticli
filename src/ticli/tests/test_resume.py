"""Tests for resume-last-session behavior.

Reopening the app should restore the last track paused at its saved position,
and space should resume playback from that position — never autoplay.

Two restore paths, both real. A state file written by this build carries
flattened track records ("tracks") and restores with zero network requests;
a pre-upgrade file carries only "track_ids" and still fetches each one on a
daemon thread — paced, and stopped dead on any sign of a rate limit. The
fixtures here that write id-only files are exercising that legacy path on
purpose.
"""

import json
import threading
import time
import types

import pytest

from ticli import player as player_mod
from ticli.player import HeadlessTidalPlayer
from ticli.utils import cache as cache_mod


@pytest.fixture(autouse=True)
def no_real_pacing_waits(monkeypatch):
    """The legacy restore keeps its fetches REFETCH_MIN_INTERVAL apart, and a
    real two-second wait has no place in the suite — so the pacing point is a
    no-op here. The pacing test replaces it with a recorder instead, which is
    the point of it being one module function."""
    monkeypatch.setattr(player_mod, "_restore_sleep", lambda seconds: None)


def _fake_track(tid, duration=200):
    return types.SimpleNamespace(id=tid, name=f"Track {tid}", duration=duration, artists=[])


class _FakeSession:
    def __init__(self, tracks):
        self._tracks = {t.id: t for t in tracks}
        self.is_pkce = False

    def track(self, tid):
        return self._tracks[tid]


def _make_player():
    p = HeadlessTidalPlayer()
    p.session = _FakeSession([_fake_track(1), _fake_track(2), _fake_track(3)])
    return p


def _wait_for(cond, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _write_state(path, **overrides):
    state = {
        "track_ids": [1, 2, 3],
        "queue_index": 1,
        "position": 42.5,
        "search_history": [],
    }
    state.update(overrides)
    path.write_text(json.dumps(state))


class TestRestoreState:
    def test_restores_track_and_position_paused(self, tmp_path, monkeypatch):
        state_file = tmp_path / "player_state.json"
        _write_state(state_file)
        monkeypatch.setattr(player_mod, "STATE_FILE", state_file)

        p = _make_player()
        p._restore_state()

        assert _wait_for(lambda: len(p._queue) == 3)
        assert p._queue_index == 1
        assert p._current_track.id == 2
        assert p._play_offset == 42.5
        assert p._get_position() == 42.5
        assert p._playing is False  # never autoplay on launch

    def test_discards_position_near_track_end(self, tmp_path, monkeypatch):
        state_file = tmp_path / "player_state.json"
        _write_state(state_file, track_ids=[1], queue_index=0, position=199.5)
        monkeypatch.setattr(player_mod, "STATE_FILE", state_file)

        p = _make_player()
        p._restore_state()

        assert _wait_for(lambda: len(p._queue) == 1)
        assert p._play_offset == 0

    def test_does_not_clobber_user_playback(self, tmp_path, monkeypatch):
        state_file = tmp_path / "player_state.json"
        _write_state(state_file)
        monkeypatch.setattr(player_mod, "STATE_FILE", state_file)

        p = _make_player()
        # User already started playing something before restore finished
        user_track = _fake_track(99)
        p._current_track = user_track
        p._playing = True
        p._queue = [user_track]
        p._queue_index = 0

        p._restore_state()
        time.sleep(0.3)  # give the restore thread time to run

        assert p._current_track is user_track
        assert p._queue == [user_track]
        assert p._play_offset == 0


class TestSaveState:
    def _patch_state_paths(self, monkeypatch, tmp_path):
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path)
        state_file = tmp_path / "player_state.json"
        monkeypatch.setattr(player_mod, "STATE_FILE", state_file)
        return state_file

    def test_quit_during_restore_does_not_wipe_state(self, tmp_path, monkeypatch):
        """Regression: quitting before the restore thread finished used to
        overwrite the saved state with an empty queue."""
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        _write_state(state_file)

        p = _make_player()

        # Simulate a slow restore: track fetches block until we release them
        release = time.time() + 0.5
        real_track = p.session.track
        p.session.track = lambda tid: (_wait_for(lambda: time.time() > release), real_track(tid))[1]

        p._restore_state()
        p._save_state()  # user quits immediately — must not clobber the file

        saved = json.loads(state_file.read_text())
        assert saved["track_ids"] == [1, 2, 3]
        assert saved["position"] == 42.5

        # After restore completes, saving works normally again
        assert _wait_for(lambda: len(p._queue) == 3)
        p._save_state()
        saved = json.loads(state_file.read_text())
        assert saved["track_ids"] == [1, 2, 3]
        assert saved["queue_index"] == 1

    def test_saves_current_track_when_queue_empty(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)

        p = _make_player()
        p._current_track = _fake_track(7)
        p._play_offset = 12.0
        p._save_state()

        saved = json.loads(state_file.read_text())
        assert saved["track_ids"] == [7]
        assert saved["queue_index"] == 0
        assert saved["position"] == 12.0
        # The fallback mirrors the record format too, so the next launch
        # restores this track without a request either
        assert [r["id"] for r in saved["tracks"]] == [7]

    def test_playing_restored_track_still_attaches_queue(self, tmp_path, monkeypatch):
        """Pressing play on the restored track while the queue is still
        loading must not prevent the queue from attaching."""
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        _write_state(state_file)

        p = _make_player()
        p._restore_state()
        assert _wait_for(lambda: p._current_track is not None)
        p._playing = True  # user hits play on the restored track

        assert _wait_for(lambda: len(p._queue) == 3)
        assert p._queue_index == 1


class TestQuitOrder:
    """Quitting used to save state first and stop the audio afterwards — and
    saving asks mpv for its position over IPC, so the music kept playing for
    the whole write. Silence has to come first, without costing accuracy."""

    def _audio(self, log, position=61.5, stop_delay=0.0):
        def _stop():
            time.sleep(stop_delay)
            log.append("stop")

        def _get_time_pos():
            log.append("time-pos")
            return position

        return types.SimpleNamespace(get_time_pos=_get_time_pos, stop=_stop)

    def test_audio_stops_before_the_state_file_is_written(self, tmp_path, monkeypatch):
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path)
        monkeypatch.setattr(player_mod, "STATE_FILE", tmp_path / "player_state.json")
        log = []
        p = _make_player()
        p._current_track = _fake_track(2)
        p._queue = [_fake_track(2)]
        p.audio = self._audio(log)
        real_write = p._write_state_file
        p._write_state_file = lambda data: (log.append("write"), real_write(data))[1]

        p._shutdown()

        assert log == ["time-pos", "stop", "write"], log

    def test_the_saved_position_still_comes_from_the_player(self, tmp_path, monkeypatch):
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path)
        state_file = tmp_path / "player_state.json"
        monkeypatch.setattr(player_mod, "STATE_FILE", state_file)
        p = _make_player()
        p._current_track = _fake_track(2)
        p._queue = [_fake_track(2)]
        p._playing = True
        p._play_offset = 0  # wall clock says 0; only mpv knows the truth
        p.audio = self._audio([], position=61.5)

        p._shutdown()

        saved = json.loads(state_file.read_text())
        assert 61.5 <= saved["position"] < 62.0

    def test_a_backend_that_cannot_report_position_still_saves(self, tmp_path, monkeypatch):
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path)
        state_file = tmp_path / "player_state.json"
        monkeypatch.setattr(player_mod, "STATE_FILE", state_file)
        p = _make_player()
        p._current_track = _fake_track(2)
        p._queue = [_fake_track(2)]
        p._play_offset = 30.0
        p.audio = self._audio([], position=None)  # ffplay never answers this

        p._shutdown()

        assert json.loads(state_file.read_text())["position"] == 30.0

    def test_shutdown_without_a_player_is_harmless(self, tmp_path, monkeypatch):
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path)
        monkeypatch.setattr(player_mod, "STATE_FILE", tmp_path / "player_state.json")
        p = _make_player()
        p.audio = None
        p._shutdown()  # must not raise


class TestTogglePlayResume:
    def _player_with_track(self, offset):
        p = _make_player()
        p._current_track = _fake_track(1, duration=200)
        p._play_offset = offset
        p.audio = types.SimpleNamespace(is_paused=False)
        p._seeks = []
        p._play_track = lambda track, seek=0: p._seeks.append(seek)
        return p

    def test_resumes_from_restored_position(self):
        p = self._player_with_track(offset=42.5)
        p._toggle_play()
        assert p._seeks == [42.5]

    def test_restarts_when_position_at_end(self):
        p = self._player_with_track(offset=199.0)
        p._toggle_play()
        assert p._seeks == [0]

    def test_starts_from_zero_when_no_position(self):
        p = self._player_with_track(offset=0)
        p._toggle_play()
        assert p._seeks == [0]

    def test_restarts_from_position_when_paused_player_died(self):
        """If the player process died while paused, resume() fails — space
        should restart the track from the last position, not skip ahead."""
        p = self._player_with_track(offset=42.5)
        p.audio = types.SimpleNamespace(is_paused=True, resume=lambda: False)
        p._toggle_play()
        assert p._seeks == [42.5]

    def test_clamps_seek_when_duration_unknown(self):
        p = self._player_with_track(offset=500.0)
        p._current_track = _fake_track(1, duration=0)
        p._toggle_play()
        assert p._seeks == [0]


class _CountingSession(_FakeSession):
    """Fake session that counts every track() fetch."""

    def __init__(self, tracks):
        super().__init__(tracks)
        self.track_calls = 0

    def track(self, tid):
        self.track_calls += 1
        return super().track(tid)


class TestNonDictStateFile:
    """A state file that is valid JSON but not a dict used to crash startup:
    _restore_state only caught JSONDecodeError/OSError around json.loads, then
    called .get() on whatever parsed. The contract everywhere else is
    'missing or corrupt -> defaults, never raises' — this held it to that."""

    def _patch_state_paths(self, monkeypatch, tmp_path):
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path)
        state_file = tmp_path / "player_state.json"
        monkeypatch.setattr(player_mod, "STATE_FILE", state_file)
        return state_file

    def _player_with_counting_session(self):
        p = HeadlessTidalPlayer()
        p.session = _CountingSession([_fake_track(1), _fake_track(2), _fake_track(3)])
        return p

    def test_a_list_state_file_neither_crashes_restore_nor_spends_requests(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        state_file.write_text("[]")

        p = self._player_with_counting_session()
        p._restore_state()  # must not raise
        time.sleep(0.2)  # a wrongly-spawned fetch thread would land in here

        assert p._queue == []
        assert p._current_track is None
        assert p.session.track_calls == 0

    def test_a_bare_string_state_file_reads_the_same_as_a_missing_one(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        state_file.write_text('"not a dict"')

        p = self._player_with_counting_session()
        p._restore_state()  # must not raise
        time.sleep(0.2)

        assert p._queue == []
        assert p._current_track is None
        assert p.session.track_calls == 0

    def test_a_save_after_a_bad_file_writes_a_dict_a_second_player_restores(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        state_file.write_text("[]")

        p = self._player_with_counting_session()
        p._restore_state()
        p._current_track = _fake_track(2)
        p._play_offset = 12.0
        p._save_state()

        saved = json.loads(state_file.read_text())
        assert isinstance(saved, dict)
        assert saved["track_ids"] == [2]
        assert saved["position"] == 12.0

        fresh = _make_player()  # next run, same (healed) state file
        fresh._restore_state()
        assert _wait_for(lambda: fresh._current_track is not None)
        assert fresh._current_track.id == 2
        assert fresh._play_offset == 12.0
        assert fresh._playing is False

    def test_pinning_a_playlist_over_a_non_dict_file_lands_and_heals_it(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        state_file.write_text("[]")

        p = _make_player()
        p._remember_last_playlist(types.SimpleNamespace(id="uuid-Mix B"))

        saved = json.loads(state_file.read_text())
        assert isinstance(saved, dict)
        assert saved["last_playlist_id"] == "uuid-Mix B"
        assert p._last_playlist_id == "uuid-Mix B"

    def test_position_merge_over_a_non_dict_file_writes_nothing(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        state_file.write_text("[]")

        p = _make_player()
        p._current_track = _fake_track(2)
        p._play_offset = 77.0
        p._merge_position_into_saved_state()  # no usable track_ids -> no-op

        assert state_file.read_text() == "[]"  # byte-for-byte untouched

    def test_position_merge_with_no_file_still_writes_nothing(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)

        p = _make_player()
        p._current_track = _fake_track(2)
        p._play_offset = 77.0
        p._merge_position_into_saved_state()

        assert not state_file.exists()


class TestSaveStateHardening:
    def _patch_state_paths(self, monkeypatch, tmp_path):
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path)
        state_file = tmp_path / "player_state.json"
        monkeypatch.setattr(player_mod, "STATE_FILE", state_file)
        return state_file

    def test_failed_restore_never_shrinks_state_file(self, tmp_path, monkeypatch):
        """Regression: after a failed restore, the 10s autosave used to
        collapse the saved queue to a single track."""
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        _write_state(state_file)

        p = _make_player()
        p.session = types.SimpleNamespace(track=lambda tid: (_ for _ in ()).throw(RuntimeError("network down")))
        p._restore_state()
        time.sleep(0.3)  # let the restore thread fail

        assert p._restore_pending is True  # latch stays set on failure
        p._save_state()  # simulates the periodic autosave
        saved = json.loads(state_file.read_text())
        assert saved["track_ids"] == [1, 2, 3]

    def test_position_merges_during_pending_restore(self, tmp_path, monkeypatch):
        """Quitting mid-restore after playing the restored track should still
        update the saved position (but nothing else)."""
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        _write_state(state_file)

        p = _make_player()
        p._restore_pending = True
        p._current_track = _fake_track(2)  # matches ids[queue_index=1]
        p._play_offset = 77.0
        p._save_state()

        saved = json.loads(state_file.read_text())
        assert saved["position"] == 77.0
        assert saved["track_ids"] == [1, 2, 3]
        assert saved["queue_index"] == 1

    def test_no_merge_when_track_differs(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        _write_state(state_file)

        p = _make_player()
        p._restore_pending = True
        p._current_track = _fake_track(99)  # not the saved current track
        p._play_offset = 77.0
        p._save_state()

        saved = json.loads(state_file.read_text())
        assert saved["position"] == 42.5  # untouched

    def test_write_is_atomic_no_temp_leftover(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)

        p = _make_player()
        p._queue = [_fake_track(1)]
        p._queue_index = 0
        p._save_state()

        assert json.loads(state_file.read_text())["track_ids"] == [1]
        assert not (tmp_path / "player_state.tmp").exists()


class TestRecordRestore:
    """A state file written by this build carries flattened track records next
    to the ids, and restoring from records is synchronous and costs **zero**
    requests: the queue is CachedTrack shims, resolved lazily — one request,
    when a track is actually played. Restoring a saved playlist used to spend
    one session.track() per id at every launch, unpaced."""

    def _patch_state_paths(self, monkeypatch, tmp_path):
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path)
        state_file = tmp_path / "player_state.json"
        monkeypatch.setattr(player_mod, "STATE_FILE", state_file)
        return state_file

    def _record(self, tid, duration=200):
        return {"id": tid, "name": f"Track {tid}", "duration": duration,
                "artists": [f"Artist {tid}"], "album": f"Album {tid}"}

    def _write_record_state(self, path, ids=(1, 2, 3), **overrides):
        state = {
            "track_ids": list(ids),
            "tracks": [self._record(i) for i in ids],
            "queue_index": 1,
            "position": 42.5,
            "search_history": [],
        }
        state.update(overrides)
        path.write_text(json.dumps(state))

    def _player(self):
        p = HeadlessTidalPlayer()
        p.session = _CountingSession([_fake_track(1), _fake_track(2), _fake_track(3)])
        return p

    def test_a_record_file_restores_the_whole_queue_with_zero_requests(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        self._write_record_state(state_file)

        p = self._player()
        p._restore_state()

        # No thread and no waiting: the attach happened before this line
        assert [t.id for t in p._queue] == [1, 2, 3]
        assert p._queue_index == 1
        assert p._current_track.name == "Track 2"
        assert p._current_track.duration == 200
        assert p._current_track.artists[0].name == "Artist 2"
        assert p._play_offset == 42.5
        assert p._get_position() == 42.5
        assert p._playing is False  # never autoplay on launch
        assert p.session.track_calls == 0
        assert p._restore_pending is False  # attach done: full saves enabled

    def test_a_record_position_near_the_track_end_is_discarded(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        self._write_record_state(state_file, ids=(1,), queue_index=0,
                                 position=199.5)

        p = self._player()
        p._restore_state()

        assert len(p._queue) == 1
        assert p._play_offset == 0
        assert p.session.track_calls == 0

    def test_corrupt_fields_in_a_record_file_read_as_defaults_not_a_crash(self, tmp_path, monkeypatch):
        """The record path runs before the UI loop starts, with no thread and
        no catch-all around it, so a corrupt machine-written field must read
        as its default — the contract the non-dict state file fix set. "Not a
        crash" has to mean the whole journey, not just this call: a corrupt
        value that merely rides along on the shim crashes the first frame
        instead, and the corrupt file persists, so it crashes every launch.
        The shim must carry the typed default, the real display builder must
        render, and the next save must write the sanitized values back —
        healing the file rather than re-reading the corruption forever."""
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        self._write_record_state(
            state_file, queue_index="two", position="half way",
            tracks=[{"id": 1, "name": "Track 1", "duration": "long"}])

        p = self._player()
        p._restore_state()  # must not raise

        assert [t.id for t in p._queue] == [1]
        assert p._queue_index == 0
        assert p._play_offset == 0
        assert p.session.track_calls == 0
        # The shim carries the typed default, not the corruption
        assert p._current_track.duration == 0
        assert p._current_track.name == "Track 1"
        assert p._current_track.artists == []

        # The first frame renders — this is what the old "no raise on
        # restore" assertion missed: duration "long" crashed
        # _build_progress_line's `duration > 0` on every paint
        frame = p._build_player_display()
        assert "Track 1" in frame.plain
        assert "--:--" in frame.plain  # an unknown duration, shown honestly

        # And the next autosave heals the file: sanitized bytes on disk, so
        # the next launch reads clean values instead of crashing again
        p._save_state()
        saved = json.loads(state_file.read_text())
        assert saved["tracks"] == [{"id": 1, "name": "Track 1", "duration": 0,
                                    "artists": [], "album": None}]
        assert saved["track_ids"] == [1]

    def test_a_corrupt_artists_field_neither_crashes_the_restore_nor_the_frame(self, tmp_path, monkeypatch):
        """artists=5 crashed _restore_state itself — a TypeError iterating an
        int inside CachedTrack, synchronously inside run() with nothing to
        catch it, at every launch for as long as the file stayed on disk."""
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        self._write_record_state(
            state_file, ids=(1,), queue_index=0,
            tracks=[{"id": 1, "duration": 200, "artists": 5}])

        p = self._player()
        p._restore_state()  # must not raise

        assert [t.id for t in p._queue] == [1]
        assert p.session.track_calls == 0
        assert p._current_track.name == "?"  # absent name, honest placeholder
        assert p._current_track.duration == 200  # the good field survives
        assert p._current_track.artists == []
        assert p._play_offset == 42.5  # a real duration still clamps normally

        frame = p._build_player_display()
        assert "?" in frame.plain
        assert "3:20" in frame.plain  # the intact duration still renders

        p._save_state()
        saved = json.loads(state_file.read_text())
        assert saved["tracks"] == [{"id": 1, "name": "?", "duration": 200,
                                    "artists": [], "album": None}]

    def test_a_record_file_with_a_rowless_dict_falls_back_to_the_ids(self, tmp_path, monkeypatch):
        """A "tracks" entry that is not usable (a row without an id) must not
        half-restore: the ids are still there, and the legacy fetch still
        works from them."""
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        self._write_record_state(state_file, tracks=[{"name": "no id at all"}])

        p = self._player()
        p._restore_state()

        assert _wait_for(lambda: len(p._queue) == 3)
        assert p.session.track_calls == 3  # the legacy path, fetched for real
        assert p._queue_index == 1

    def test_pressing_play_then_resolves_the_shim_exactly_once(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        self._write_record_state(state_file)

        p = self._player()
        p._restore_state()
        assert p.session.track_calls == 0
        p.audio = types.SimpleNamespace(is_paused=False)

        p._toggle_play()  # space on the restored track

        # The existing lazy path: one resolve, and the queue's copy swapped
        # for the real track so a replay never pays a second one
        assert _wait_for(lambda: not getattr(p._queue[1], "cached", False))
        assert p.session.track_calls == 1
        assert p._current_track.id == 2
        assert not getattr(p._current_track, "cached", False)

        # Play again: the real track passes _resolve_track through untouched
        assert _wait_for(lambda: not p._track_changing)
        p._playing = False
        p._toggle_play()
        assert _wait_for(lambda: not p._track_changing)
        assert p.session.track_calls == 1


class TestRecordRoundTrip:
    """_save_state from a live queue → bytes on disk → a fresh player restores
    identically, spending nothing. The file still carries "track_ids", which
    is what every pre-record build reads — downgrade compat, pinned here in
    the bytes on disk."""

    def _patch_state_paths(self, monkeypatch, tmp_path):
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path)
        state_file = tmp_path / "player_state.json"
        monkeypatch.setattr(player_mod, "STATE_FILE", state_file)
        return state_file

    def _rich_track(self, tid):
        return types.SimpleNamespace(
            id=tid, name=f"Track {tid}", duration=200 + tid,
            artists=[types.SimpleNamespace(name=f"Artist {tid}")],
            album=types.SimpleNamespace(name=f"Album {tid}"),
        )

    def _player(self):
        p = HeadlessTidalPlayer()
        p.session = _CountingSession([_fake_track(1), _fake_track(2), _fake_track(3)])
        return p

    def test_a_live_queue_round_trips_through_the_file_with_zero_requests(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        p = self._player()
        p._queue = [self._rich_track(i) for i in (1, 2, 3)]
        p._queue_index = 2
        p._play_offset = 42.5
        p._save_state()

        on_disk = json.loads(state_file.read_text())
        assert on_disk["track_ids"] == [1, 2, 3]  # the legacy build's format
        assert on_disk["tracks"][2] == {
            "id": 3, "name": "Track 3", "duration": 203,
            "artists": ["Artist 3"], "album": "Album 3",
        }

        fresh = self._player()
        fresh._restore_state()

        assert fresh.session.track_calls == 0
        assert [t.id for t in fresh._queue] == [1, 2, 3]
        assert fresh._queue_index == 2
        assert fresh._current_track.name == "Track 3"
        assert fresh._current_track.duration == 203
        assert fresh._current_track.artists[0].name == "Artist 3"
        assert fresh._current_track.album.name == "Album 3"
        assert fresh._play_offset == 42.5
        assert fresh._playing is False

    def test_a_save_straight_after_a_record_restore_is_lossless(self, tmp_path, monkeypatch):
        """The restored shims flatten back through track_record cleanly, so
        save → restore → save leaves the file's queue byte-identical."""
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        p = self._player()
        p._queue = [self._rich_track(i) for i in (1, 2, 3)]
        p._queue_index = 0
        p._save_state()
        first = json.loads(state_file.read_text())

        fresh = self._player()
        fresh._restore_state()
        fresh._save_state()
        second = json.loads(state_file.read_text())

        assert second["tracks"] == first["tracks"]
        assert second["track_ids"] == first["track_ids"]
        assert second["queue_index"] == first["queue_index"]
        assert fresh.session.track_calls == 0


class TestCorruptSearchHistory:
    """search_history is machine-written JSON with the same exposure as the
    records: _restore_state sliced it ([:200]) before checking its shape, so
    a history that was not a list crashed startup — synchronously, at every
    launch, for as long as the corrupt file stayed on disk. The contract is
    the same as everywhere else: corrupt reads as its default, and non-str
    elements are dropped rather than riding into the search screen. The
    write side is untouched — it slices what memory holds, which this
    guarantees is a list of strings."""

    def _patch_state_paths(self, monkeypatch, tmp_path):
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path)
        state_file = tmp_path / "player_state.json"
        monkeypatch.setattr(player_mod, "STATE_FILE", state_file)
        return state_file

    def _restore_over(self, state_file, history):
        state_file.write_text(json.dumps(
            {"search_history": history, "track_ids": []}))
        p = HeadlessTidalPlayer()
        p.session = _CountingSession([_fake_track(1)])
        p._restore_state()  # must not raise
        assert p.session.track_calls == 0
        return p

    def test_a_dict_history_reads_as_empty_not_a_crash(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        # Used to raise TypeError: unhashable type 'slice' — {}[:200]
        p = self._restore_over(state_file, {"not": "alist"})
        assert p._search_history == []

    def test_an_int_history_reads_as_empty_not_a_crash(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        p = self._restore_over(state_file, 5)
        assert p._search_history == []

    def test_non_string_elements_are_dropped_the_strings_kept(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        p = self._restore_over(
            state_file, [5, "abba", None, ["x"], "b side", {"q": 1}])
        assert p._search_history == ["abba", "b side"]

    def test_the_two_hundred_entry_cap_still_holds(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        p = self._restore_over(state_file, [f"query {i}" for i in range(250)])
        assert p._search_history == [f"query {i}" for i in range(200)]


class TestLegacyRestoreIsFrugal:
    """A pre-upgrade state file has only ids, so restoring it still costs one
    request per track. What is pinned here: those requests are paced to the
    re-fetch floor, stop dead on any evidence of a rate limit, and stand down
    when the user has moved on."""

    def _patch_state_paths(self, monkeypatch, tmp_path):
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path)
        state_file = tmp_path / "player_state.json"
        monkeypatch.setattr(player_mod, "STATE_FILE", state_file)
        return state_file

    def _player(self):
        p = HeadlessTidalPlayer()
        p.session = _CountingSession([_fake_track(1), _fake_track(2), _fake_track(3)])
        return p

    def test_legacy_fetches_ask_for_the_refetch_floor_between_starts(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        _write_state(state_file)  # id-only: the legacy format

        naps = []
        monkeypatch.setattr(player_mod, "_restore_sleep", naps.append)
        p = self._player()
        p._restore_state()

        assert _wait_for(lambda: len(p._queue) == 3)
        assert p.session.track_calls == 3
        # Three request starts, two gaps, each asked to wait out the whole
        # floor (the fake fetches return instantly)
        assert len(naps) == 2
        for nap in naps:
            assert nap == pytest.approx(player_mod.REFETCH_MIN_INTERVAL,
                                        rel=0.05)

    def test_a_rate_limited_legacy_restore_stops_dead_and_says_so(self, tmp_path, monkeypatch):
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        _write_state(state_file, queue_index=0)

        calls = []

        def _track(tid):
            calls.append(tid)
            if len(calls) >= 2:
                raise Exception("429 Client Error: Too Many Requests for url")
            return _fake_track(tid)

        p = HeadlessTidalPlayer()
        p.session = types.SimpleNamespace(track=_track, is_pkce=False)
        p._restore_state()

        assert _wait_for(lambda: "rate-limiting" in p._toast)
        time.sleep(0.1)  # a request made after the block would land in here
        assert calls == [1, 2]  # the block ended the run: nothing after it
        assert p._queue == []  # no attach
        assert p._restore_pending is True  # latch stays set on failure
        assert p._toast == ("TIDAL is rate-limiting — restore stopped. "
                            "Nothing will be retried.")

        p._save_state()  # the suppressed save must not shrink the good file
        assert json.loads(state_file.read_text())["track_ids"] == [1, 2, 3]

    def test_a_restore_superseded_during_the_pacing_wait_stops_quietly(self, tmp_path, monkeypatch):
        """Deterministic: the supersede lands inside the pacing wait itself —
        the widest window in the loop — and the next fetch must then never
        start."""
        state_file = self._patch_state_paths(monkeypatch, tmp_path)
        _write_state(state_file, queue_index=0)
        p = self._player()

        def _supersede(seconds):
            p._restore_pending = False  # radio claimed the queue mid-wait

        monkeypatch.setattr(player_mod, "_restore_sleep", _supersede)
        p._restore_state()

        assert _wait_for(lambda: p._current_track is not None)
        time.sleep(0.1)  # a second fetch would land in here
        assert p.session.track_calls == 1  # only the current track, ever
        assert p._queue == []  # nothing attached over the user's new queue
        assert p._toast == ""  # stood down quietly — nothing went wrong


def _run_together(jobs, timeout=30.0):
    """Start every job on its own thread, wait for all of them, re-raise.

    Same shape as test_cache.py's: a failure inside a thread is otherwise a
    printed traceback and a green test, and a deadlock — the failure mode a
    non-reentrant lock invites — is otherwise a wedged suite instead of an
    assertion.
    """
    errors = []

    def wrap(job):
        def run():
            try:
                job()
            except BaseException as e:  # noqa: BLE001 - re-raised below
                errors.append(e)
        return run

    threads = [threading.Thread(target=wrap(job), daemon=True) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    alive = [t for t in threads if t.is_alive()]
    assert not alive, \
        f"{len(alive)} state writer(s) never finished — a deadlock, not a race"
    if errors:
        raise errors[0]


def _widen_the_window(p, pause=0.25):
    """Hold every state-file read open for a moment, so the race is certain.

    The losing interleave is a thread switch between `_read_state_dict` and
    `_write_state_file` inside one writer's cycle — a window microseconds
    wide, which a test cannot land on by throwing threads at it
    (test_cache.py's lesson: with the lock taken out, the naive version
    passes every run). Sleeping inside the read makes the scheduler's worst
    case happen every time: another writer's whole cycle fits inside the
    pause. With the lock, the pause only makes the test slower — the second
    writer waits at the door.
    """
    original = p._read_state_dict

    def slow_read():
        data = original()
        time.sleep(pause)
        return data

    p._read_state_dict = slow_read
    return p


class TestStateWritesAreSerialized:
    """One player, one player_state.json, and every writer on its own thread.

    `_save_state` runs on the monitor thread every 10 seconds and again on the
    main thread at shutdown; `_merge_position_into_saved_state` runs under it
    while a restore is pending; `_remember_last_playlist` runs on the
    add-to-playlist background thread. Each is a load-modify-save over the
    same file, so unlocked they erase each other's cycles wholesale: an
    autosave landing inside the pin's read-write gap is clobbered by the
    stale read (fresh queue and position lost), the reverse loses the pin.
    `_state_lock` holds each whole cycle apart — the sibling of the cache
    tracker's `_tracker_lock` and of test_cache.py's
    TestTrackerWritesAreSerialized.

    Timing tests, so written to fail rather than flake: assertions are on the
    final bytes of the real file, and the windows are widened so the losing
    interleave happens every run without the lock.
    """

    def _patch(self, monkeypatch, tmp_path):
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path)
        state_file = tmp_path / "player_state.json"
        monkeypatch.setattr(player_mod, "STATE_FILE", state_file)
        return state_file

    def test_a_playlist_pin_beside_an_autosave_loses_neither(self, tmp_path, monkeypatch):
        """The pin's cycle reads the file, sleeps, writes — and the autosave's
        whole full save runs inside that gap. Unlocked, the pin writes back
        its stale copy and the session's fresh queue and position vanish from
        disk; the event makes that ordering certain rather than probable."""
        state_file = self._patch(monkeypatch, tmp_path)
        # A previous session's file: what the pin's stale read would resurrect
        _write_state(state_file, track_ids=[9], queue_index=0, position=1.0)
        p = _make_player()
        p._queue = [_fake_track(1), _fake_track(2)]
        p._queue_index = 1
        p._current_track = p._queue[1]
        p._play_offset = 55.0

        pin_has_read = threading.Event()
        original = p._read_state_dict

        def slow_read():
            data = original()
            pin_has_read.set()
            time.sleep(0.25)  # the autosave's whole cycle fits in here
            return data

        p._read_state_dict = slow_read

        def pin():
            p._remember_last_playlist(types.SimpleNamespace(id="pl-9"))

        def autosave():
            assert pin_has_read.wait(5.0), "the pin never read the file"
            p._save_state()

        _run_together([pin, autosave])

        saved = json.loads(state_file.read_text())
        assert saved["track_ids"] == [1, 2], \
            "the pin's stale read erased the fresh queue"
        assert saved["queue_index"] == 1
        assert saved["position"] == 55.0
        assert saved["last_playlist_id"] == "pl-9", "the autosave erased the pin"

    def test_a_position_merge_beside_a_playlist_pin_keeps_both(self, tmp_path, monkeypatch):
        """Both writers read the same file; both reads are held open, so both
        happen before either write. Unlocked, whichever write lands second
        undoes the first — the position rolls back or the pin vanishes."""
        state_file = self._patch(monkeypatch, tmp_path)
        _write_state(state_file)  # track_ids [1,2,3], index 1, position 42.5
        p = _make_player()
        p._current_track = _fake_track(2)  # matches ids[1], so the merge writes
        p._play_offset = 77.0
        _widen_the_window(p)

        def merge():
            p._merge_position_into_saved_state()

        def pin():
            p._remember_last_playlist(types.SimpleNamespace(id="pl-9"))

        _run_together([merge, pin])

        saved = json.loads(state_file.read_text())
        assert saved["position"] == 77.0, \
            "the pin's stale read rolled the position back"
        assert saved["last_playlist_id"] == "pl-9", "the merge erased the pin"
        assert saved["track_ids"] == [1, 2, 3]

    def test_shutdown_beside_a_monitor_save_neither_deadlocks_nor_tears(self, tmp_path, monkeypatch):
        """[q] runs _shutdown → _save_state on the main thread while the
        monitor thread's 10-second autosave may be mid-cycle. With a restore
        still pending, both saves reach the merge *inside* their locked cycle
        — the branch where a reentrant acquire would deadlock, which
        _run_together's join timeout turns into a failure instead of a wedged
        suite. The file must end well-formed, whole, and carrying one write's
        whole truth."""
        state_file = self._patch(monkeypatch, tmp_path)
        _write_state(state_file)
        p = _make_player()
        p._restore_pending = True
        p._queue = []
        p._current_track = _fake_track(2)
        p._play_offset = 33.0
        p.audio = types.SimpleNamespace(get_time_pos=lambda: 61.5,
                                        stop=lambda: None)
        _widen_the_window(p)

        _run_together([p._save_state, p._shutdown], timeout=10.0)

        saved = json.loads(state_file.read_text())
        assert saved["track_ids"] == [1, 2, 3], "the merge shrank the file"
        assert saved["queue_index"] == 1
        # 33.0 is the monitor's read, 61.5 the position mpv answered at [q];
        # either whole answer is fine, a mixture or a crash is not
        assert saved["position"] in (33.0, 61.5)

    # ── the lock is taken exactly once per cycle ──

    def test_every_state_write_happens_under_the_lock(self, tmp_path, monkeypatch):
        """The lock is only worth anything if nothing writes around it: every
        path that replaces the file — the full save, the merge, the pin, and
        the merge reached through _save_state's restore-pending branch — must
        already hold `_state_lock` when it reaches `_write_state_file`."""
        self._patch(monkeypatch, tmp_path)
        p = _make_player()
        p._queue = [_fake_track(2)]
        p._queue_index = 0
        p._current_track = p._queue[0]

        held = []
        real_write = p._write_state_file

        def checked(state):
            held.append(p._state_lock.locked())
            real_write(state)

        p._write_state_file = checked

        p._save_state()  # the full save
        p._merge_position_into_saved_state()  # the position merge
        p._remember_last_playlist(types.SimpleNamespace(id="pl-9"))  # the pin
        p._restore_pending = True  # and the merge reached through
        p._queue = []  # _save_state's restore-pending branch
        p._save_state()

        assert len(held) == 4, "a writer skipped the file, or wrote it twice"
        assert all(held), "a state write happened outside _state_lock"
