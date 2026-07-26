"""Tests for the add-to-playlist picker (y key)."""

import json
import time
import types

import pytest

from ticli import player as player_mod
from ticli.player import (
    HeadlessTidalPlayer,
    KEY_BACKSPACE,
    KEY_DOWN,
    KEY_ENTER,
    KEY_ESC,
    KEY_LEFT,
    KEY_UP,
)


def _fake_track(tid, name=None):
    return types.SimpleNamespace(id=tid, name=name or f"Track {tid}", duration=200, artists=[])


def _fake_playlist(name, added_result=None, fail=False, pid=None):
    def add(ids):
        if fail:
            raise RuntimeError("network down")
        return added_result if added_result is not None else [1]
    return types.SimpleNamespace(
        id=pid or f"uuid-{name}", name=name, num_tracks=5, add=add)


def _wait_for(cond, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Adding to a playlist now writes the pin to the state file. Never the
    owner's — every test in this module gets its own."""
    monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(player_mod, "STATE_FILE", tmp_path / "state" / "player_state.json")


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
        # -1 is the "+ New playlist" row, above the list and clamped there
        assert p._picker_cursor == -1
        p._handle_add_to_playlist_key(KEY_UP)
        assert p._picker_cursor == -1
        p._handle_add_to_playlist_key(KEY_DOWN)
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


def _fake_session(created, fail=False, add_fail=False):
    """A session whose user.create_playlist records what it was asked for."""
    def create_playlist(title, description, parent_id="root"):
        created.append((title, description))
        if fail:
            raise RuntimeError("network down")
        return _fake_playlist(title, fail=add_fail, pid="uuid-new")
    return types.SimpleNamespace(
        user=types.SimpleNamespace(create_playlist=create_playlist))


def _type(p, text):
    for ch in text:
        p._handle_add_to_playlist_key(ch)


class TestNewPlaylistRow:
    def test_row_is_on_screen_above_the_list(self):
        p = _make_player()
        p._current_track = _fake_track(1)
        p._open_playlist_picker()
        screen = p._build_add_to_playlist_display().plain
        assert "+ New playlist" in screen
        assert screen.index("+ New playlist") < screen.index("Mix A")

    def test_up_from_the_top_lands_on_it_and_marks_it(self):
        p = _make_player()
        p._current_track = _fake_track(1)
        p._open_playlist_picker()
        p._handle_add_to_playlist_key(KEY_UP)
        screen = p._build_add_to_playlist_display().plain
        assert "▸ + New playlist" in screen

    def test_it_is_the_only_row_when_there_is_nothing_to_add_to(self):
        p = _make_player()
        p._editable_playlists = []
        p._editable_playlists_time = time.time()
        p._current_track = _fake_track(1)
        p._open_playlist_picker()
        assert p._picker_cursor == -1
        screen = p._build_add_to_playlist_display().plain
        assert "▸ + New playlist" in screen
        assert "No playlists you can edit" in screen

    def test_enter_opens_a_prompt_and_typing_shows_the_name(self):
        p = _make_player()
        p._current_track = _fake_track(1)
        p._open_playlist_picker()
        p._handle_add_to_playlist_key(KEY_UP)
        p._handle_add_to_playlist_key(KEY_ENTER)
        assert p._picker_new_name == ""
        _type(p, "Road trip")  # the space must type, not pause playback
        assert p._picker_new_name == "Road trip"
        screen = p._build_add_to_playlist_display().plain
        assert "New playlist: ‹ Road trip▏›" in screen
        # How to commit it is on the pane, in the footer with every other hint
        # rather than written a second time on the row — one copy, and one that
        # cannot be cut in half by a narrow window
        footer = "".join(line.plain for line in p._build_hints(p._fit))
        assert "[Enter] create" in footer
        assert "[Esc] cancel" in footer
        assert "[↑/↓]" not in footer, "the arrows do nothing while typing"

    def test_backspace_shortens_and_arrows_do_not_navigate(self):
        p = _make_player()
        p._current_track = _fake_track(1)
        p._open_playlist_picker()
        p._handle_add_to_playlist_key(KEY_UP)
        p._handle_add_to_playlist_key(KEY_ENTER)
        _type(p, "abc")
        p._handle_add_to_playlist_key(KEY_BACKSPACE)
        assert p._picker_new_name == "ab"
        p._handle_add_to_playlist_key(KEY_DOWN)
        p._handle_add_to_playlist_key(KEY_LEFT)
        assert p._picker_new_name == "ab"      # arrows swallowed by the textbox
        assert p._picker_cursor == -1
        assert p._mode == p.MODE_ADD_TO_PLAYLIST  # ← did not go back either


class TestCreatePlaylist:
    def _prompting(self, created, **kw):
        p = _make_player()
        p.session = _fake_session(created, **kw)
        p._current_track = _fake_track(7)
        p._open_playlist_picker()
        p._handle_add_to_playlist_key(KEY_UP)
        p._handle_add_to_playlist_key(KEY_ENTER)
        return p

    def test_creates_and_adds_in_one_gesture(self):
        created = []
        p = self._prompting(created)
        _type(p, "Road trip")
        p._handle_add_to_playlist_key(KEY_ENTER)
        assert p._mode == p.MODE_PLAYER          # picker closes at once
        assert p._picker_new_name is None
        assert _wait_for(lambda: p._toast == 'Created "Road trip" and added track')
        assert created == [("Road trip", "")]

    def test_new_playlist_joins_the_cached_list_pinned_first(self):
        created = []
        p = self._prompting(created)
        _type(p, "Road trip")
        p._handle_add_to_playlist_key(KEY_ENTER)
        assert _wait_for(lambda: len(p._editable_playlists) == 3)
        assert [pl.name for pl in p._picker_playlists()][0] == "Road trip"

    def test_escape_creates_nothing(self):
        created = []
        p = self._prompting(created)
        _type(p, "Road trip")
        p._handle_add_to_playlist_key(KEY_ESC)
        time.sleep(0.1)
        assert created == []
        assert p._picker_new_name is None
        assert p._mode == p.MODE_ADD_TO_PLAYLIST  # cancelled the prompt, not the picker
        assert p._toast == ""

    def test_empty_name_creates_nothing_and_keeps_the_prompt(self):
        created = []
        p = self._prompting(created)
        p._handle_add_to_playlist_key(KEY_ENTER)
        time.sleep(0.1)
        assert created == []
        assert p._picker_new_name == ""
        assert p._toast == "Playlist name can't be empty"

    def test_whitespace_only_name_creates_nothing(self):
        created = []
        p = self._prompting(created)
        _type(p, "   ")
        p._handle_add_to_playlist_key(KEY_ENTER)
        time.sleep(0.1)
        assert created == []

    def test_double_enter_creates_one_playlist(self):
        created = []
        p = self._prompting(created)
        _type(p, "Road trip")
        p._handle_add_to_playlist_key(KEY_ENTER)
        # The second Enter, delivered to the picker again as fast as a key
        # repeat could manage it
        p._picker_new_name = "Road trip"
        p._handle_add_to_playlist_key(KEY_ENTER)
        assert _wait_for(lambda: not p._picker_busy)
        time.sleep(0.1)
        assert created == [("Road trip", "")]

    def test_creation_failure_toasts(self):
        created = []
        p = self._prompting(created, fail=True)
        _type(p, "Road trip")
        p._handle_add_to_playlist_key(KEY_ENTER)
        assert _wait_for(lambda: p._toast == 'Failed to create "Road trip"')
        assert p._last_playlist_id is None
        assert not p._picker_busy

    def test_add_failure_after_a_successful_create_says_so(self):
        created = []
        p = self._prompting(created, add_fail=True)
        _type(p, "Road trip")
        p._handle_add_to_playlist_key(KEY_ENTER)
        assert _wait_for(
            lambda: p._toast == 'Created "Road trip", but failed to add track')
        assert p._last_playlist_id == "uuid-new"


class TestLastUsedPin:
    def test_adding_writes_the_pin_to_the_state_file(self):
        p = _make_player()
        p._current_track = _fake_track(1)
        p._open_playlist_picker()
        p._handle_add_to_playlist_key(KEY_DOWN)      # Mix B
        p._handle_add_to_playlist_key(KEY_ENTER)
        assert _wait_for(lambda: p._toast == 'Added to "Mix B"')
        written = json.loads(player_mod.STATE_FILE.read_text())
        assert written["last_playlist_id"] == "uuid-Mix B"

    def test_creating_writes_the_pin_to_the_state_file(self):
        created = []
        p = _make_player()
        p.session = _fake_session(created)
        p._current_track = _fake_track(1)
        p._open_playlist_picker()
        p._handle_add_to_playlist_key(KEY_UP)
        p._handle_add_to_playlist_key(KEY_ENTER)
        _type(p, "Road trip")
        p._handle_add_to_playlist_key(KEY_ENTER)
        assert _wait_for(lambda: p._toast.startswith('Created "Road trip"'))
        written = json.loads(player_mod.STATE_FILE.read_text())
        assert written["last_playlist_id"] == "uuid-new"

    def test_a_fresh_run_reads_it_back_and_pins_that_row(self):
        p = _make_player()
        p._current_track = _fake_track(1)
        p._open_playlist_picker()
        p._handle_add_to_playlist_key(KEY_DOWN)
        p._handle_add_to_playlist_key(KEY_ENTER)
        assert _wait_for(lambda: p._toast == 'Added to "Mix B"')

        fresh = _make_player()                    # next run, same state file
        fresh._restore_state()
        assert fresh._last_playlist_id == "uuid-Mix B"
        assert [pl.name for pl in fresh._picker_playlists()] == ["Mix B", "Mix A"]
        fresh._current_track = _fake_track(1)
        fresh._open_playlist_picker()
        screen = fresh._build_add_to_playlist_display().plain
        assert screen.index("Mix B") < screen.index("Mix A")
        assert "last used" in screen

    def test_the_pin_survives_a_full_state_save(self):
        p = _make_player()
        p._last_playlist_id = "uuid-Mix A"
        p._current_track = _fake_track(1)
        p._save_state()
        assert json.loads(
            player_mod.STATE_FILE.read_text())["last_playlist_id"] == "uuid-Mix A"

    def test_a_pin_naming_a_deleted_playlist_degrades_silently(self):
        """Pinned on TIDAL's website, then deleted there. No ghost, no error."""
        p = _make_player()
        p._last_playlist_id = "uuid-gone"
        assert [pl.name for pl in p._picker_playlists()] == ["Mix A", "Mix B"]
        p._current_track = _fake_track(1)
        p._open_playlist_picker()
        screen = p._build_add_to_playlist_display().plain
        assert "last used" not in screen
        assert screen.count("Mix A") == 1 and screen.count("Mix B") == 1
        assert screen.index("Mix A") < screen.index("Mix B")
        # and the cursor still selects what it points at
        p._handle_add_to_playlist_key(KEY_ENTER)
        assert _wait_for(lambda: p._toast == 'Added to "Mix A"')

    def test_no_pin_leaves_the_order_alone(self):
        p = _make_player()
        assert [pl.name for pl in p._picker_playlists()] == ["Mix A", "Mix B"]
