"""Tests for resume-last-session behavior.

Reopening the app should restore the last track paused at its saved position,
and space should resume playback from that position — never autoplay.
"""

import json
import time
import types

from ticli import player as player_mod
from ticli.player import HeadlessTidalPlayer


def _fake_track(tid, duration=200):
    return types.SimpleNamespace(id=tid, name=f"Track {tid}", duration=duration, artists=[])


class _FakeSession:
    def __init__(self, tracks):
        self._tracks = {t.id: t for t in tracks}

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
