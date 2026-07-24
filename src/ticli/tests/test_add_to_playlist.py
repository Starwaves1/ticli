"""Tests for the add-to-playlist picker (y key)."""

import time
import types

from ticli.player import HeadlessTidalPlayer, KEY_DOWN, KEY_ENTER, KEY_ESC, KEY_UP


def _fake_track(tid, name=None):
    return types.SimpleNamespace(id=tid, name=name or f"Track {tid}", duration=200, artists=[])


def _fake_playlist(name, added_result=None, fail=False):
    def add(ids):
        if fail:
            raise RuntimeError("network down")
        return added_result if added_result is not None else [1]
    return types.SimpleNamespace(name=name, num_tracks=5, add=add)


def _wait_for(cond, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _make_player():
    p = HeadlessTidalPlayer()
    # Pretend the playlist cache is fresh so opening the picker doesn't hit
    # the network
    p._editable_playlists = [_fake_playlist("Mix A"), _fake_playlist("Mix B")]
    p._editable_playlists_time = time.time()
    return p


class TestTargetTrack:
    def test_player_mode_targets_current_track(self):
        p = _make_player()
        p._current_track = _fake_track(1)
        assert p._target_track_for_picker().id == 1

    def test_queue_mode_targets_cursor(self):
        p = _make_player()
        p._mode = p.MODE_QUEUE
        p._queue = [_fake_track(1), _fake_track(2)]
        p._queue_cursor = 1
        assert p._target_track_for_picker().id == 2

    def test_browse_mode_targets_cursor(self):
        p = _make_player()
        p._mode = p.MODE_BROWSE
        p._browse_tracks = [_fake_track(1), _fake_track(2)]
        p._browse_cursor = 0
        assert p._target_track_for_picker().id == 1

    def test_browse_play_all_row_falls_back_to_current(self):
        p = _make_player()
        p._mode = p.MODE_BROWSE
        p._browse_tracks = [_fake_track(1)]
        p._browse_cursor = -1  # "Play All" row
        p._current_track = _fake_track(9)
        assert p._target_track_for_picker().id == 9


class TestPicker:
    def test_open_with_no_track_shows_toast_and_stays(self):
        p = _make_player()
        p._open_playlist_picker()
        assert p._mode == p.MODE_PLAYER
        assert p._toast == "No track selected"
        assert time.time() < p._toast_until

    def test_open_and_cancel_returns_to_origin(self):
        p = _make_player()
        p._mode = p.MODE_QUEUE
        p._queue = [_fake_track(1)]
        p._queue_cursor = 0
        p._open_playlist_picker()
        assert p._mode == p.MODE_ADD_TO_PLAYLIST
        assert p._picker_track.id == 1
        p._handle_add_to_playlist_key(KEY_ESC)
        assert p._mode == p.MODE_QUEUE

    def test_cursor_navigation_clamps(self):
        p = _make_player()
        p._current_track = _fake_track(1)
        p._open_playlist_picker()
        p._handle_add_to_playlist_key(KEY_UP)
        assert p._picker_cursor == 0
        p._handle_add_to_playlist_key(KEY_DOWN)
        p._handle_add_to_playlist_key(KEY_DOWN)
        assert p._picker_cursor == 1  # clamped at last playlist

    def test_enter_adds_and_toasts_success(self):
        p = _make_player()
        p._current_track = _fake_track(1)
        p._open_playlist_picker()
        p._handle_add_to_playlist_key(KEY_ENTER)
        assert p._mode == p.MODE_PLAYER  # picker closes immediately
        assert _wait_for(lambda: p._toast == 'Added to "Mix A"')

    def test_duplicate_toasts_already_in(self):
        p = _make_player()
        p._editable_playlists = [_fake_playlist("Mix A", added_result=[])]
        p._current_track = _fake_track(1)
        p._open_playlist_picker()
        p._handle_add_to_playlist_key(KEY_ENTER)
        assert _wait_for(lambda: p._toast == 'Already in "Mix A"')

    def test_failure_toasts_error(self):
        p = _make_player()
        p._editable_playlists = [_fake_playlist("Mix A", fail=True)]
        p._current_track = _fake_track(1)
        p._open_playlist_picker()
        p._handle_add_to_playlist_key(KEY_ENTER)
        assert _wait_for(lambda: p._toast == "Failed to add to playlist")

    def test_remove_from_own_playlist(self):
        p = _make_player()
        p._mode = p.MODE_BROWSE
        removed = []
        p._browse_playlist = types.SimpleNamespace(
            name="Mix A", remove_by_index=lambda i: (removed.append(i), True)[-1])
        p._browse_tracks = [_fake_track(1), _fake_track(2), _fake_track(3)]
        p._browse_cursor = 1

        p._remove_from_browse_playlist()
        assert _wait_for(lambda: len(p._browse_tracks) == 2)
        assert removed == [1]
        assert [t.id for t in p._browse_tracks] == [1, 3]
        assert p._toast == 'Removed "Track 2" from Mix A'

    def test_remove_last_track_clamps_cursor(self):
        p = _make_player()
        p._mode = p.MODE_BROWSE
        p._browse_playlist = types.SimpleNamespace(
            name="Mix A", remove_by_index=lambda i: True)
        p._browse_tracks = [_fake_track(1)]
        p._browse_cursor = 0

        p._remove_from_browse_playlist()
        assert _wait_for(lambda: len(p._browse_tracks) == 0)
        assert p._browse_cursor == -1  # lands on the "Play All" row

    def test_remove_noop_outside_own_playlist(self):
        """Browsing an album (no playlist context) — x must do nothing."""
        p = _make_player()
        p._mode = p.MODE_BROWSE
        p._browse_playlist = None
        p._browse_tracks = [_fake_track(1)]
        p._browse_cursor = 0

        p._remove_from_browse_playlist()
        time.sleep(0.1)
        assert len(p._browse_tracks) == 1

    def test_remove_failure_keeps_tracks_and_toasts(self):
        p = _make_player()
        p._mode = p.MODE_BROWSE
        p._browse_playlist = types.SimpleNamespace(
            name="Mix A", remove_by_index=lambda i: False)
        p._browse_tracks = [_fake_track(1), _fake_track(2)]
        p._browse_cursor = 0

        p._remove_from_browse_playlist()
        assert _wait_for(lambda: p._toast == "Failed to remove from playlist")
        assert len(p._browse_tracks) == 2

    def test_busy_guard_prevents_double_add(self):
        p = _make_player()
        p._current_track = _fake_track(1)
        calls = []
        p._editable_playlists = [types.SimpleNamespace(
            name="Mix A", num_tracks=5,
            add=lambda ids: (calls.append(1), time.sleep(0.2), [1])[-1],
        )]
        p._open_playlist_picker()
        p._handle_add_to_playlist_key(KEY_ENTER)
        p._picker_track = p._current_track  # simulate reopening instantly
        p._picker_add_to(p._editable_playlists[0])  # busy → ignored
        assert _wait_for(lambda: not p._picker_busy)
        assert len(calls) == 1
