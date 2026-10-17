"""Shuffle, repeat, move, playlist edits and the search bar's focus: commands and keys."""

import time
import types

import pytest

from ticli import commands
from ticli.commands import AGENT, HUMAN, Commands
from ticli.player import (
    HeadlessTidalPlayer, KEY_DOWN, KEY_ENTER, KEY_ESC, KEY_UP,
)


class _NoNetwork:
    is_pkce = False

    def __init__(self):
        self.asked = []

    def __getattr__(self, name):
        self.asked.append(name)
        raise AssertionError(f"request attempted: {name}")


def _track(tid):
    return types.SimpleNamespace(id=tid, name=f"Track {tid}", duration=200,
                                 artists=[types.SimpleNamespace(name="Artist", id=70)],
                                 album=types.SimpleNamespace(name="Album", id=90))


class _Audio:
    player_cmd = "mpv"

    def stop(self):
        pass

    def volume_ceiling(self):
        return 100


def _player(queue=(1, 2, 3, 4, 5, 6), index=1, **config):
    p = HeadlessTidalPlayer()
    p.config.update(config)
    p.session = _NoNetwork()
    p.audio = _Audio()
    p._queue = [_track(i) for i in queue]
    p._queue_index = index
    p._current_track = p._queue[index] if p._queue else None
    p._plays = []
    p._play_track = lambda track, seek=0, automatic=False: (
        p._plays.append(track.id), setattr(p, "_current_track", track))
    p._wake = lambda: None
    p.commands = Commands(p, sleep=lambda s: None)
    return p


def _human(p, name, **args):
    return p.commands.execute(name, args, caller=HUMAN)


def _ids(rows):
    return [t.id for t in rows]


def _wait_for(cond, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def reversing(monkeypatch):
    """A shuffle that is checkable: it reverses."""
    monkeypatch.setattr(commands.random, "shuffle", lambda rows: rows.reverse())
    from ticli import player as player_mod
    monkeypatch.setattr(player_mod.random, "shuffle", lambda rows: rows.reverse())


class TestShuffleMode:
    def test_on_shuffles_only_what_follows_and_off_restores_it(self, reversing):
        p = _player()
        playing = p._current_track
        assert _human(p, "shuffle", mode="on")["result"]["shuffle"] is True
        assert _ids(p._queue) == [1, 2, 6, 5, 4, 3]
        assert p._queue[p._queue_index] is playing and p._plays == []
        assert _human(p, "shuffle", mode="off")["result"]["shuffle"] is False
        assert _ids(p._queue) == [1, 2, 3, 4, 5, 6]
        assert p._queue[p._queue_index] is playing

    def test_off_restores_after_playback_moved_on(self, reversing):
        p = _player()
        _human(p, "shuffle")
        _human(p, "next")  # 6 now plays
        assert p._current_track.id == 6
        _human(p, "shuffle")
        assert _ids(p._queue) == [1, 2, 6, 3, 4, 5]
        assert p._current_track.id == 6 and p._queue_index == 2

    def test_queue_add_while_shuffled_lands_in_both_orders(self, reversing):
        p = _player()
        _human(p, "shuffle", mode="on")
        p._lists[("album", "a")] = [_track(7), _track(8)]
        p._remember("track", [_track(9)])
        _human(p, "queue.add", album="a")
        _human(p, "queue.add", track_ids=[9], position="next")
        assert _ids(p._queue) == [1, 2, 9, 6, 5, 4, 3, 7, 8]
        _human(p, "shuffle", mode="off")
        assert _ids(p._queue) == [1, 2, 9, 3, 4, 5, 6, 7, 8]

    def test_removals_and_moves_stay_consistent(self, reversing):
        p = _player()
        _human(p, "shuffle", mode="on")
        _human(p, "queue.remove", index=3)  # 5
        _human(p, "queue.move", index=4, to=2)  # 3 up front
        assert _ids(p._queue) == [1, 2, 3, 6, 4]
        _human(p, "shuffle", mode="off")
        assert _ids(p._queue) == [1, 2, 3, 4, 6]

    def test_queue_clear_leaves_nothing_to_restore(self, reversing):
        p = _player()
        _human(p, "shuffle", mode="on")
        _human(p, "queue.clear")
        _human(p, "shuffle", mode="off")
        assert _ids(p._queue) == [2]

    def test_a_list_played_while_on_starts_at_the_choice_then_shuffles(self, reversing):
        p = _player()
        _human(p, "shuffle", mode="on")
        p._lists[("album", "a")] = [_track(i) for i in (11, 12, 13, 14)]
        result = _human(p, "play.album", id="a", index=1)["result"]
        assert result["shuffled"] is True and result["index"] == 0
        assert _ids(p._queue) == [12, 14, 13, 11] and p._plays == [12]
        _human(p, "shuffle", mode="off")
        assert _ids(p._queue) == [12, 11, 13, 14]

    def test_status_reports_both_modes(self):
        p = _player()
        status = _human(p, "status")["result"]
        assert status["shuffle"] is False and status["repeat"] == "off"
        _human(p, "shuffle")
        _human(p, "repeat", mode="one")
        status = _human(p, "status")["result"]
        assert status["shuffle"] is True and status["repeat"] == "one"
        snap = p.snapshot()
        assert snap["shuffle"] is True and snap["repeat"] == "one"

    @pytest.mark.parametrize("name,mode", [("shuffle", "sideways"), ("repeat", "twice")])
    def test_a_bad_mode_is_refused(self, name, mode):
        assert _human(_player(), name, mode=mode)["code"] == "bad_args"

    def test_an_agent_toggle_is_local_and_toasts(self):
        p = _player()
        result = p.commands.execute("shuffle", {"mode": "on"}, caller=AGENT)
        assert result["ok"] and p.session.asked == []
        assert p._toast == "agent: shuffle on"


class TestShufflePlay:
    def test_plays_the_whole_list_shuffled_from_its_first_entry(self, reversing):
        p = _player()
        p._lists[("playlist", "pl")] = [_track(i) for i in (21, 22, 23)]
        result = _human(p, "play.playlist", id="pl", index=2, shuffle=True)["result"]
        assert result == {"queue_length": 3, "index": 0, "shuffled": True}
        assert _ids(p._queue) == [23, 22, 21] and p._plays == [23]
        assert p._shuffle is False and p._unshuffled is None

    def test_a_cli_string_is_understood(self, reversing):
        p = _player()
        p._lists[("album", "a")] = [_track(31), _track(32)]
        assert _human(p, "play.album", id="a", shuffle="true")["result"]["shuffled"]
        assert _human(p, "play.album", id="a", shuffle="maybe")["code"] == "bad_args"

    def test_s_in_a_list_shuffle_plays_it(self, reversing):
        p = _player()
        p._mode = p.MODE_BROWSE
        p._browse_source = ("album", "a")
        p._browse_tracks = [_track(41), _track(42), _track(43)]
        p._lists[("album", "a")] = list(p._browse_tracks)
        p._handle_key("s")
        assert _ids(p._queue) == [43, 42, 41] and p._plays == [43]


class TestRepeat:
    def test_cycles_off_all_one(self):
        p = _player()
        assert [_human(p, "repeat")["result"]["repeat"] for _ in range(3)] == ["all", "one", "off"]

    def test_off_stops_at_the_end(self):
        p = _player(index=5)
        assert p._advance(1, automatic=True) is False

    def test_all_wraps_to_the_first_entry(self):
        p = _player(index=5)
        _human(p, "repeat", mode="all")
        assert p._advance(1, automatic=True) is True
        assert p._queue_index == 0 and p._plays == [1]

    def test_one_replays_at_the_natural_end_but_next_still_moves(self):
        p = _player(index=2)
        _human(p, "repeat", mode="one")
        assert p._advance(1, automatic=True) is True
        assert p._queue_index == 2 and p._plays == [3]
        _human(p, "next")
        assert p._queue_index == 3 and p._plays == [3, 4]

    def test_keys_on_the_player_and_queue_screens(self):
        p = _player()
        p._handle_key("R")
        p._handle_key("S")
        assert p._repeat == "all" and p._shuffle is True
        p._mode = p.MODE_QUEUE
        p._handle_key("r")
        p._handle_key("s")
        assert p._repeat == "one" and p._shuffle is False

    def test_footers_show_the_state(self):
        p = _player()
        _human(p, "shuffle")
        _human(p, "repeat", mode="all")
        p._mode = p.MODE_QUEUE
        labels = {h.key: h.label for h in p._screen_hints()}
        assert labels["s"] == "shuffle: on" and labels["r"] == "repeat: all"
        p._mode = p.MODE_PLAYER
        p._show_more = True
        labels = {h.key: h.label for h in p._screen_hints()}
        assert labels["S"] == "shuffle: on" and labels["R"] == "repeat: all"


class _MovablePlaylist:
    def __init__(self, pid="pl", ok=True, fail=False):
        self.id, self.name, self.num_tracks = pid, "Mix", 4
        self.moves = []
        self.ok, self.fail = ok, fail

    def move_by_index(self, index, position):
        self.moves.append((index, position))
        if self.fail:
            raise RuntimeError("network down")
        return self.ok

    def remove_by_index(self, index):
        return True


def _with_playlist(pl, ids=(1, 2, 3, 4)):
    p = _player()
    p._remember("playlist", [pl])
    p._lists[("playlist", pl.id)] = [_track(i) for i in ids]
    return p


class TestPlaylistMove:
    @pytest.mark.parametrize("index,to,sent,order", [
        (0, 2, (0, 3), [2, 3, 1, 4]),
        (3, 0, (3, 0), [4, 1, 2, 3]),
        (1, 3, (1, 4), [1, 3, 4, 2]),
    ])
    def test_one_write_and_the_new_order(self, index, to, sent, order):
        pl = _MovablePlaylist()
        p = _with_playlist(pl)
        result = _human(p, "playlist.move", id="pl", index=index, to=to)
        assert result["ok"] and result["result"]["accepted"]
        assert _wait_for(lambda: not p._browse_remove_busy)
        assert pl.moves == [sent]
        assert _ids(p._lists[("playlist", "pl")]) == order
        assert _ids(p._cache.get_playlist_tracks("pl")) == order
        assert p._toast.startswith("Moved ")

    def test_same_place_writes_nothing(self):
        pl = _MovablePlaylist()
        p = _with_playlist(pl)
        assert _human(p, "playlist.move", id="pl", index=1, to=1)["ok"]
        assert pl.moves == []

    def test_a_failed_write_keeps_the_old_order(self):
        pl = _MovablePlaylist(fail=True)
        p = _with_playlist(pl)
        _human(p, "playlist.move", id="pl", index=0, to=3)
        assert _wait_for(lambda: not p._browse_remove_busy)
        assert _ids(p._lists[("playlist", "pl")]) == [1, 2, 3, 4]
        assert "Failed to move" in p._toast

    def test_not_yours_is_refused(self):
        theirs = types.SimpleNamespace(id="pl", name="Theirs")
        p = _with_playlist(theirs)
        assert _human(p, "playlist.move", id="pl", index=0, to=1)["code"] == "not_yours"

    def test_refused_while_another_edit_is_in_flight(self):
        pl = _MovablePlaylist()
        p = _with_playlist(pl)
        p._browse_remove_busy = True
        assert _human(p, "playlist.move", id="pl", index=0, to=1)["code"] == "busy"
        p._browse_remove_busy, p._picker_busy = False, True
        assert _human(p, "playlist.move", id="pl", index=0, to=1)["code"] == "busy"
        assert pl.moves == []

    def test_stale_and_bad_destinations_are_refused(self):
        pl = _MovablePlaylist()
        p = _with_playlist(pl)
        assert _human(p, "playlist.move", id="pl", index=0, to=1, track_id=2)["code"] == "stale"
        assert _human(p, "playlist.move", id="pl", index=0, to=4)["code"] == "bad_args"
        assert _human(p, "playlist.move", id="nope", index=0, to=1)["code"] == "not_loaded"
        assert pl.moves == []

    def test_dangerous_for_agents(self):
        pl = _MovablePlaylist()
        p = _with_playlist(pl)
        result = p.commands.execute("playlist.move", {"id": "pl", "index": 0, "to": 1}, caller=AGENT)
        assert result["code"] == "dangerous_off" and pl.moves == []


def _browsing(pl, ids=(1, 2, 3, 4)):
    p = _with_playlist(pl, ids)
    p._mode = p.MODE_BROWSE
    p._browse_source = ("playlist", pl.id)
    p._browse_playlist = pl
    p._browse_tracks = [_track(i) for i in ids]
    p._browse_cursor = 0
    return p


class TestMoveKeys:
    def test_pick_up_move_and_cancel_touches_nothing(self):
        pl = _MovablePlaylist()
        p = _browsing(pl)
        p._handle_key("m")
        p._handle_key(KEY_DOWN)
        p._handle_key(KEY_DOWN)
        assert _ids(p._carried_rows(p.MODE_BROWSE)) == [2, 3, 1, 4]
        assert p._browse_cursor == 2
        assert "↕" in p._build_browse_display().plain
        assert [h.label for h in p._screen_hints()] == ["move", "drop", "cancel"]
        p._handle_key("x")  # inert while carrying
        p._handle_key("v")
        assert not p._volume_open
        p._handle_key(KEY_ESC)
        assert p._carry is None and p._browse_cursor == 0
        assert _ids(p._browse_tracks) == [1, 2, 3, 4] and pl.moves == []

    def test_drop_is_one_write(self):
        pl = _MovablePlaylist()
        p = _browsing(pl)
        p._browse_cursor = 3
        p._handle_key("m")
        p._handle_key(KEY_UP)
        p._handle_key(KEY_UP)
        p._handle_key(KEY_ENTER)
        assert _wait_for(lambda: _ids(p._browse_tracks) == [1, 4, 2, 3])
        assert pl.moves == [(3, 1)] and p._browse_cursor == 1

    def test_a_refused_drop_restores_the_order(self):
        pl = _MovablePlaylist()
        p = _browsing(pl)
        p._browse_remove_busy = False
        p._handle_key("m")
        p._handle_key(KEY_DOWN)
        p._picker_busy = True
        p._handle_key(KEY_ENTER)
        assert _ids(p._browse_tracks) == [1, 2, 3, 4] and p._browse_cursor == 0
        assert "in flight" in p._toast and pl.moves == []

    def test_a_failed_write_restores_the_order(self):
        pl = _MovablePlaylist(ok=False)
        p = _browsing(pl)
        p._handle_key("m")
        p._handle_key(KEY_DOWN)
        p._handle_key(KEY_ENTER)
        assert _wait_for(lambda: "Failed to move" in p._toast)
        assert _ids(p._browse_tracks) == [1, 2, 3, 4]

    def test_not_in_an_album(self):
        p = _browsing(_MovablePlaylist())
        p._browse_playlist = None
        p._handle_key("m")
        assert p._carry is None and "own playlists" in p._toast

    def test_queue_move_uses_the_queue(self):
        p = _player()
        p._mode = p.MODE_QUEUE
        p._queue_cursor = 0
        p._handle_key("m")
        for _ in range(3):
            p._handle_key(KEY_DOWN)
        assert _ids(p._queue) == [1, 2, 3, 4, 5, 6]  # nothing moves until the drop
        p._handle_key(KEY_ENTER)
        assert _ids(p._queue) == [2, 3, 4, 1, 5, 6]
        assert p._queue_index == 0 and p._queue_cursor == 3 and p._plays == []


class TestQueueClearKey:
    def test_confirm_clears_keeping_the_current(self):
        p = _player()
        p._mode = p.MODE_QUEUE
        p._handle_key("C")
        assert "Clear 5 tracks" in p._build_queue_clear_confirm().plain
        p._handle_key("y")
        assert _ids(p._queue) == [2]

    def test_any_other_key_cancels(self):
        p = _player()
        p._mode = p.MODE_QUEUE
        p._handle_key("C")
        p._handle_key("n")
        assert len(p._queue) == 6 and not p._queue_clear_pending


class _EditablePlaylist(_MovablePlaylist):
    def __init__(self, pid="pl", name="Mix", description="old words"):
        super().__init__(pid)
        self.name, self.description = name, description
        self.edits, self.deleted = [], False

    def edit(self, title=None, description=None):
        self.edits.append((title, description))
        return True

    def delete(self):
        self.deleted = True
        return True

    def add(self, ids):
        return list(ids)


def _listing(*playlists):
    p = _player()
    p._mode = p.MODE_PLAYLISTS
    p._playlists = list(playlists)
    p._editable_playlists = list(playlists)
    p._owns = lambda pl: hasattr(pl, "edit")
    for pl in playlists:
        p._remember("playlist", [pl])
    return p


class TestPlaylistEditKeys:
    def test_rename_from_the_list(self):
        pl = _EditablePlaylist()
        p = _listing(pl)
        p._handle_key("R")
        assert p._text_edit["value"] == "Mix"
        assert "Rename: ‹ Mix" in p._build_playlists_display().plain
        assert not p._can_open_volume() and not p._can_exit_menu_to_player()
        for key in ("v", "h", "!"):
            p._handle_key(key)
        assert p._text_edit["value"] == "Mixvh!" and not p._volume_open
        p._handle_key(KEY_ENTER)
        assert _wait_for(lambda: pl.edits == [("Mixvh!", None)])
        assert p._text_edit is None and p._mode == p.MODE_PLAYLISTS

    def test_describe_is_prefilled_and_esc_cancels(self):
        pl = _EditablePlaylist()
        p = _listing(pl)
        p._handle_key("E")
        assert p._text_edit["value"] == "old words"
        p._handle_key("x")
        p._handle_key(KEY_ESC)
        assert p._text_edit is None and pl.edits == []

    def test_an_empty_name_is_not_sent(self):
        pl = _EditablePlaylist(name="A")
        p = _listing(pl)
        p._handle_key("R")
        p._handle_key("\x7f")
        p._handle_key(KEY_ENTER)
        assert p._text_edit is not None and "empty" in p._toast and pl.edits == []

    def test_not_yours_is_refused_locally(self):
        theirs = types.SimpleNamespace(id="t", name="Theirs")
        p = _listing(theirs)
        for key in ("R", "E", "X"):
            p._handle_key(key)
        assert p._text_edit is None and p._playlist_delete is None
        assert "own playlists" in p._toast

    def test_delete_from_the_list_names_it_and_needs_y(self):
        pl, other = _EditablePlaylist(), _EditablePlaylist(pid="p2", name="Other")
        p = _listing(pl, other)
        p._handle_key("X")
        assert 'Delete playlist "Mix"' in p._build_playlist_delete_confirm().plain
        p._handle_key("n")
        assert not pl.deleted and len(p._playlists) == 2
        p._handle_key("X")
        p._handle_key("y")
        assert _wait_for(lambda: pl.deleted)
        assert p._playlists == [other]

    def test_delete_from_the_detail_goes_back_to_the_list(self):
        pl = _EditablePlaylist()
        p = _listing(pl)
        p._open_playlist = lambda playlist: (p._push_nav(), setattr(p, "_mode", p.MODE_BROWSE),
                                             setattr(p, "_browse_playlist", playlist))
        p._handle_key(KEY_ENTER)
        assert p._mode == p.MODE_BROWSE
        p._handle_key("X")
        p._handle_key("y")
        assert p._mode == p.MODE_PLAYLISTS and p._playlists == []
        assert _wait_for(lambda: pl.deleted)

    def test_rename_from_the_detail_retitles_it(self):
        pl = _EditablePlaylist()
        p = _browsing(pl)
        p._browse_title = "Mix"
        p._handle_key("R")
        for _ in range(3):
            p._handle_key("\x7f")
        for key in "Jams":
            p._handle_key(key)
        p._handle_key(KEY_ENTER)
        assert p._browse_title == "Jams"
        assert _wait_for(lambda: pl.edits == [("Jams", None)])

    def test_album_detail_has_no_edits(self):
        p = _browsing(_EditablePlaylist())
        p._browse_playlist = None
        p._handle_key("X")
        assert p._playlist_delete is None
        keys = {h.key for h in p._screen_hints()}
        assert {"m", "R", "E", "X"}.isdisjoint(keys) and "s" in keys


def _searching(*items):
    p = _player()
    p._mode = p.MODE_SEARCH
    p._search_query = "miles"
    p._search_results = list(items)
    p._search_cursor = 0
    return p


def _row(kind, obj, name):
    return {"type": kind, "name": name, "obj": obj}


class TestSearchFocus:
    def test_down_then_q_queues_and_leaves_the_query(self):
        song = _track(50)
        p = _searching(_row("track", song, "So What"))
        assert not p._search_in_results()
        p._handle_key(KEY_DOWN)
        assert p._search_in_results()
        p._handle_key("q")
        assert p._queue[-1] is song
        assert p._search_query == "miles" and p._search_results
        assert p._toast == 'Queued "So What"'

    def test_letters_in_the_results_never_reach_the_query(self):
        p = _searching(_row("track", _track(50), "So What"), _row("track", _track(51), "Blue"))
        p._handle_key(KEY_DOWN)
        for key in "abcxzQ1":
            p._handle_key(key)
        assert p._search_query == "miles" and len(p._search_results) == 2
        assert p._search_in_results()

    def test_an_album_queues_as_a_list(self):
        album = types.SimpleNamespace(id="77", name="Kind of Blue")
        p = _searching(_row("album", album, "Kind of Blue"))
        p._lists[("album", "77")] = [_track(60), _track(61)]
        p._handle_key(KEY_DOWN)
        p._handle_key("q")
        assert _ids(p._queue[-2:]) == [60, 61]

    def test_slash_and_up_from_the_top_return_to_the_bar(self):
        p = _searching(_row("track", _track(50), "A"), _row("track", _track(51), "B"))
        p._handle_key(KEY_DOWN)
        p._handle_key(KEY_DOWN)
        assert p._search_cursor == 1
        p._handle_key("/")
        assert not p._search_in_results()
        p._handle_key("s")
        assert p._search_query == "miless" and not p._search_results
        p = _searching(_row("track", _track(50), "A"), _row("track", _track(51), "B"))
        p._handle_key(KEY_DOWN)
        p._handle_key(KEY_DOWN)
        p._handle_key(KEY_UP)
        assert p._search_in_results() and p._search_cursor == 0
        p._handle_key(KEY_UP)
        assert not p._search_in_results() and not p._player_focus

    def test_the_caret_and_the_highlight_follow_the_focus(self):
        p = _searching(_row("track", _track(50), "A"))
        assert "█" in p._build_search_display().plain
        assert "▸   [TRACK]" not in p._build_search_display().plain
        p._handle_key(KEY_DOWN)
        shown = p._build_search_display().plain
        assert "█" not in shown and "▸   [TRACK]" in shown
        keys = {h.key: h.label for h in p._screen_hints()}
        assert keys["q"] == "queue" and keys["/"] == "edit search"

    def test_y_opens_the_picker_for_that_track(self):
        song = _track(50)
        p = _searching(_row("track", song, "A"))
        p._editable_playlists = [types.SimpleNamespace(id="x", name="X", add=lambda ids: [1])]
        p._editable_playlists_time = time.time()
        p._handle_key(KEY_DOWN)
        p._handle_key("y")
        assert p._mode == p.MODE_ADD_TO_PLAYLIST and p._picker_track is song
        p._handle_key(KEY_ESC)
        assert p._mode == p.MODE_SEARCH and p._search_in_results()

    def test_d_opens_the_download_box_for_that_track(self):
        song = _track(50)
        p = _searching(_row("track", song, "A"))
        p._handle_key(KEY_DOWN)
        p._handle_key("d")
        assert p._download_open and p._download_track is song


class TestCliForms:
    def test_the_verbs_parse_like_the_others(self):
        from ticli import agent
        assert agent.form_args("shuffle", ["on"]) == {"mode": "on"}
        assert agent.form_args("repeat", []) == {}
        assert agent.form_args("playlist.move", ["Mix", "3", "0"]) == {"id": "Mix", "index": 3, "to": 0}
        assert agent.form_args("play.album", ["123", "shuffle=true"]) == {"id": 123, "shuffle": True}

    def test_human_lines(self):
        from ticli import humancli
        assert humancli.line("shuffle", {"ok": True, "result": {"shuffle": True}}) == "shuffle on"
        assert humancli.line("repeat", {"ok": True, "result": {"repeat": "one"}}) == "repeat one"
        assert humancli.NAMED["playlist.move"] == "playlist"
