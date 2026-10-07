"""The command layer and its permission gate, through `execute` only."""

import json
import types

import pytest

from ticli import commands
from ticli import player as player_mod
from ticli.commands import AGENT, HUMAN, Commands, cli_caller, offline_read
from ticli.player import HeadlessTidalPlayer
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod
from ticli.utils.config import ai_key_matches, hash_ai_key, load_config


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", path)
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    return path


class _NoNetwork:
    """A session that records every attribute asked of it and answers none."""

    is_pkce = False

    def __init__(self):
        self.asked = []

    def __getattr__(self, name):
        self.asked.append(name)
        raise AssertionError(f"request attempted: {name}")


def _track(tid):
    return types.SimpleNamespace(id=tid, name=f"Track {tid}", duration=200,
                                 artists=[types.SimpleNamespace(name="Artist")], album=None)


class _Audio:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


def _player(**config):
    p = HeadlessTidalPlayer()
    p.config.update(config)
    p.session = _NoNetwork()
    p.audio = _Audio()
    p._queue = [_track(1), _track(2), _track(3)]
    p._queue_index = 1
    p._current_track = p._queue[1]
    p._plays = []
    p._play_track = lambda track, seek=0: p._plays.append(track.id)
    p._wake = lambda: None
    p.slept = []
    p.commands = Commands(p, sleep=p.slept.append)
    return p


def _agent(p, name, ai_key=None, **args):
    return p.commands.execute(name, args, caller=AGENT, key=ai_key)


def _human(p, name, **args):
    return p.commands.execute(name, args, caller=HUMAN)


def _assert_refusal(result, code):
    assert result["ok"] is False
    assert result["code"] == code
    assert result["reason"]
    assert "Ask your human" in result["fix"] or "ask your human" in result["fix"].lower()
    assert "never edit config.json" in result["fix"]


class TestRegistry:
    def test_every_command_is_classified(self):
        for cmd in commands.COMMANDS.values():
            assert isinstance(cmd.read, bool) and isinstance(cmd.tidal, bool)

    def test_the_dangerous_ones_are_exactly_the_destructive_ones(self):
        dangerous = {name for name, cmd in commands.COMMANDS.items() if cmd.dangerous is True}
        assert dangerous == {"playlist.remove", "playlist.delete", "download.delete", "cache.clear",
                             "login.pkce", "logout", "refetch"}

    def test_unknown_command(self):
        result = _human(_player(), "frobnicate")
        assert result["ok"] is False and result["code"] == "unknown_command"

    def test_results_are_plain_json(self):
        p = _player()
        for name in ("status", "queue.list", "settings.get"):
            json.dumps(_human(p, name))


class TestActions:
    def test_next_plays_the_following_entry(self):
        p = _player()
        assert _human(p, "next")["ok"]
        assert p._plays == [3] and p._queue_index == 2

    def test_queue_remove_before_the_current_entry_keeps_it_playing(self):
        p = _player()
        _human(p, "queue.remove", index=0)
        assert [t.id for t in p._queue] == [2, 3]
        assert p._queue_index == 0 and p._plays == []

    def test_queue_remove_of_the_current_entry_plays_the_next(self):
        p = _player()
        _human(p, "queue.remove", index=1)
        assert [t.id for t in p._queue] == [1, 3]
        assert p._plays == [3]

    def test_queue_remove_of_the_last_entry_stops(self):
        p = _player()
        p._queue, p._queue_index = [p._queue[1]], 0
        _human(p, "queue.remove", index=0)
        assert p._current_track is None and p.audio.stopped == 1

    @pytest.mark.parametrize("name", ["queue.play", "queue.remove"])
    def test_a_stale_queue_index_is_refused_with_the_queue_as_it_is(self, name):
        p = _player()
        result = _human(p, name, index=2, track_id=2)
        assert result["ok"] is False and result["code"] == "stale"
        assert result["queue"] == {"index": 1, "length": 3, "track_ids": [1, 2, 3]}
        assert len(p._queue) == 3 and p._plays == []

    def test_a_matching_track_id_goes_through(self):
        p = _player()
        assert _human(p, "queue.play", index=2, track_id=3)["ok"] and p._plays == [3]

    def test_play_album_from_the_open_list_costs_nothing(self):
        p = _player()
        p._browse_source = ("album", "77")
        p._browse_tracks = [_track(10), _track(11)]
        assert _human(p, "play.album", id="77", index=1)["ok"]
        assert p._plays == [11] and [t.id for t in p._queue] == [10, 11]
        assert p.session.asked == []

    def test_play_track_prefers_the_search_row(self):
        p = _player()
        row = _track(9)
        p._search_results = [{"type": "track", "obj": row}]
        _human(p, "play.track", track_ids=[9])
        assert p._queue == [row] and p._plays == [9]

    def test_seek_by_position(self):
        p = _player()
        sought = []
        p._seek_by = sought.append
        p._get_position = lambda: 10.0
        _human(p, "seek", position=40)
        assert sought == [30.0]

    def test_cache_clear_reports_and_toasts(self):
        p = _player()
        p._cache.clear_audio = lambda: (3, 0)
        assert _human(p, "cache.clear")["result"] == {"removed": 3, "kept": 0}
        assert p._toast == "Cleared 3 songs"

    def test_settings_set_applies_and_saves(self, config_file):
        p = _player()
        assert _human(p, "settings.set", key="page_size", value=20)["result"]["changed"]
        assert p._page_size == 20
        assert json.loads(config_file.read_text())["page_size"] == 20


class TestReadsOwnTheirLists:
    """Browsing reads run in the player and leave what they fetched there, so the
    next command by id costs nothing whichever client sent it."""

    def test_album_tracks_then_play_album_is_one_request(self):
        p = _player()
        asked = []
        album = types.SimpleNamespace(id=9, name="A", tracks=lambda: asked.append(1) or [_track(4), _track(5)])
        p._remember("album", [album])
        result = _human(p, "album.tracks", id=9)
        assert [t.id for t in result["result"]["tracks"]] == [4, 5]
        assert _human(p, "play.album", id=9, index=1)["ok"]
        assert p._plays == [5] and asked == [1]

    def test_artist_section_then_play_artist_costs_nothing_more(self):
        p = _player()
        artist = types.SimpleNamespace(id=3, name="X", get_top_tracks=lambda limit: [_track(6), _track(7)])
        p._remember("artist", [artist])
        rows = _human(p, "artist.section", id=3, section="tracks")["result"]["items"]
        assert [r["obj"].id for r in rows] == [6, 7]
        p._artist_sections = {}
        assert _human(p, "play.artist", id=3, section="tracks", index=1)["ok"]
        assert p._plays == [7]

    def test_play_track_by_an_id_from_the_metadata_index_asks_tidal_nothing(self):
        p = _player()
        p._cache.put_playlist_tracks("p", [_track(42)])
        assert _human(p, "play.track", track_ids=[42])["ok"]
        assert p._plays == [42]

    def test_playlist_remove_needs_the_rows_loaded(self):
        p = _player()
        result = _human(p, "playlist.remove", id="nope", index=0)
        assert result["code"] == "not_loaded"

    def test_playlist_remove_refuses_a_stale_row(self):
        p = _player()
        playlist = types.SimpleNamespace(id="p", name="P", remove_by_index=lambda i: True)
        p._remember("playlist", [playlist])
        p._lists[("playlist", "p")] = [_track(1), _track(2)]
        assert _human(p, "playlist.remove", id="p", index=0, track_id=2)["code"] == "stale"

    def test_library_playlists_refreshes_the_editable_list(self):
        p = _player()
        mine = player_mod.tidalapi.UserPlaylist.__new__(player_mod.tidalapi.UserPlaylist)
        mine.id, mine.name, mine.num_tracks = "m", "Mine", 1
        theirs = types.SimpleNamespace(id="t", name="Theirs", num_tracks=2, creator=None)
        p.session = types.SimpleNamespace(user=types.SimpleNamespace(playlists=lambda: [mine, theirs]))
        result = _human(p, "library.playlists")
        assert [pl.id for pl in result["result"]["playlists"]] == ["m", "t"]
        assert p._editable_playlists == [mine]

    def test_stop_stops_and_keeps_the_position(self):
        p = _player()
        p.audio.get_time_pos = lambda: None
        p._playing = True
        p._play_offset = 50.0
        p._play_start_time = None
        assert _human(p, "stop")["ok"]
        assert p._playing is False and p.audio.stopped == 1
        assert p._current_track is not None


class TestHardening:
    def test_search_uses_the_query_argument_not_the_input_box(self):
        p = _player()
        p._search_query = "typed"
        asked = []
        p.session = types.SimpleNamespace(search=lambda q, **kw: asked.append(q) or {"tracks": [_track(7)]})
        result = p.commands.execute("search", {"query": "x"}, caller=HUMAN)
        assert result["ok"] and asked == ["x"]
        assert [t.id for t in result["result"]["tracks"]] == [7]

    def test_a_raising_danger_predicate_is_a_structured_error(self, monkeypatch):
        p = _player()
        def boom(player, args):
            raise ValueError("nope")
        monkeypatch.setitem(commands.COMMANDS, "next",
                            commands.COMMANDS["next"].__class__(
                                **{**vars(commands.COMMANDS["next"]), "dangerous": boom}))
        result = _agent(p, "next")
        assert result["ok"] is False and "ValueError" in result["reason"]

    def test_a_failing_disk_read_is_a_structured_error(self, monkeypatch):
        def boom(self):
            raise RuntimeError("corrupt")
        monkeypatch.setattr(commands.MetadataCache, "get_playlists", boom)
        result = offline_read("library.playlists", {}, {})
        assert result["ok"] is False and result["code"] == "local_read_failed"
        assert result["fix"]


class TestProtectedSettings:
    @pytest.mark.parametrize("key,value", [
        ("allow_ai_control", False), ("allow_dangerous_commands", True),
        ("ai_control_key", {"salt": "00", "hash": "00"})])
    @pytest.mark.parametrize("caller", [HUMAN, AGENT])
    def test_no_command_can_write_them(self, key, value, caller, config_file):
        p = _player(allow_dangerous_commands=True)
        before = dict(p.config)
        result = p.commands.execute("settings.set", {"key": key, "value": value}, caller=caller)
        assert result["ok"] is False and result["code"] == "protected_setting"
        assert "never edit config.json" in result["fix"]
        assert p.config == before and not config_file.exists()

    def test_settings_get_never_shows_the_key(self):
        p = _player(ai_control_key=hash_ai_key("hunter2"))
        got = _human(p, "settings.get")["result"]
        assert got["ai_control_key"] is True
        assert "hunter2" not in json.dumps(got) and p.config["ai_control_key"]["hash"] not in json.dumps(got)


class TestAIControlSwitch:
    def test_off_refuses_agent_actions(self):
        p = _player(allow_ai_control=False)
        _assert_refusal(_agent(p, "next"), "ai_control_off")
        assert p._plays == []

    def test_off_still_answers_status(self):
        p = _player(allow_ai_control=False)
        result = _agent(p, "status")
        assert result["ok"] and result["result"]["switches"]["allow_ai_control"] is False

    def test_off_answers_reads_from_disk_with_no_requests(self):
        player_mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
        player_mod.STATE_FILE.write_text(json.dumps({
            "tracks": [{"id": 5, "name": "Saved", "artists": ["A"]}], "queue_index": 0}))
        p = _player(allow_ai_control=False)
        result = _agent(p, "queue.list")
        assert result["ok"] and result["result"]["source"] == "disk"
        assert [t["title"] for t in result["result"]["tracks"]] == ["Saved"]
        assert _agent(p, "search", query="saved")["result"]["source"] == "disk"
        assert p.session.asked == []

    def test_on_lets_agents_act(self):
        p = _player()
        assert _agent(p, "next")["ok"] and p._plays == [3]

    def test_humans_are_never_gated(self):
        p = _player(allow_ai_control=False, ai_control_key=hash_ai_key("k"))
        assert _human(p, "next")["ok"]
        assert p.slept == []


class TestUnreadableConfig:
    @pytest.fixture(autouse=True)
    def corrupt(self, config_file):
        config_file.write_text('{"allow_ai_control": true, "allow_dangerous')

    def test_agents_are_refused_everything_but_status(self):
        p = _player()
        for name in ("next", "queue.list", "search", "cache.clear"):
            _assert_refusal(_agent(p, name, query="x"), "config_unreadable")
        assert p._plays == [] and p.session.asked == []
        switches = _agent(p, "status")["result"]["switches"]
        assert switches == {"allow_ai_control": False, "allow_dangerous_commands": False,
                            "key_required": True}

    def test_the_agent_cli_gate_fails_closed_too(self):
        refused = commands.gate("search", AGENT, None, load_config(), read=True)
        assert refused["code"] == "config_unreadable"

    def test_the_human_still_plays_but_cannot_overwrite_the_file(self, config_file):
        p = _player()
        assert _human(p, "next")["ok"]
        result = p.commands.execute("settings.set", {"key": "page_size", "value": 20}, caller=HUMAN)
        assert result["code"] == "config_unreadable"
        assert config_file.read_text().startswith('{"allow_ai_control": true, "allow_dangerous')

    def test_the_tui_says_so(self):
        assert "unreadable" in HeadlessTidalPlayer()._toast


class TestDangerousSwitch:
    def test_off_by_default_refuses_dangerous_agent_calls(self):
        p = _player()
        p._cache.clear_audio = lambda: (1, 0)
        _assert_refusal(_agent(p, "cache.clear"), "dangerous_off")
        _assert_refusal(_agent(p, "logout"), "dangerous_off")

    def test_on_allows_them(self):
        p = _player(allow_dangerous_commands=True)
        p._cache.clear_audio = lambda: (1, 0)
        assert _agent(p, "cache.clear")["ok"]

    def test_lowering_the_cache_budget_is_dangerous_raising_is_not(self):
        p = _player(cache_budget_gb=4)
        p._cache.enforce_budget = lambda: None
        _assert_refusal(_agent(p, "settings.set", key="cache_budget_gb", value=2),
                        "dangerous_off")
        assert _agent(p, "settings.set", key="cache_budget_gb", value=8)["ok"]

    def test_humans_are_never_gated(self):
        p = _player()
        p._cache.clear_audio = lambda: (1, 0)
        assert _human(p, "cache.clear")["ok"]


class TestKey:
    def test_status_needs_no_key_and_says_one_is_required(self):
        p = _player(ai_control_key=hash_ai_key("secret"))
        result = _agent(p, "status")
        assert result["ok"] and result["result"]["switches"]["key_required"] is True

    def test_missing_key_is_refused(self):
        p = _player(ai_control_key=hash_ai_key("secret"))
        _assert_refusal(_agent(p, "next"), "key_required")
        _assert_refusal(_agent(p, "queue.list"), "key_required")
        assert p.slept == [] and p._plays == []

    def test_wrong_key_costs_a_second(self):
        p = _player(ai_control_key=hash_ai_key("secret"))
        _assert_refusal(_agent(p, "next", ai_key="guess"), "wrong_key")
        _assert_refusal(_agent(p, "next", ai_key="guess2"), "wrong_key")
        assert p.slept == [1.0, 1.0] and p._plays == []

    def test_right_key_passes(self):
        p = _player(ai_control_key=hash_ai_key("secret"))
        assert _agent(p, "next", ai_key="secret")["ok"]
        assert p.slept == []

    def test_key_is_stored_salted_never_plain(self):
        a, b = hash_ai_key("same"), hash_ai_key("same")
        assert a["salt"] != b["salt"] and a["hash"] != b["hash"]
        assert "same" not in json.dumps(a)
        assert ai_key_matches(a, "same") and not ai_key_matches(a, "Same")
        assert hash_ai_key("") is None


class TestAgentVisibility:
    def test_agent_action_toasts(self):
        p = _player()
        _agent(p, "next")
        assert p._toast == "agent: next track"

    def test_agent_read_does_not_toast(self):
        p = _player()
        _agent(p, "status")
        assert p._toast == ""

    def test_human_action_does_not_toast_as_agent(self):
        p = _player()
        _human(p, "next")
        assert not p._toast.startswith("agent:")


class TestOfflineRead:
    def test_needs_no_player(self):
        player_mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
        player_mod.STATE_FILE.write_text(json.dumps({
            "tracks": [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}],
            "queue_index": 1, "position": 42.0}))
        result = offline_read("status")
        assert result["ok"]
        assert result["result"]["track"]["title"] == "Two"
        assert result["result"]["position"] == 42.0

    def test_refuses_actions(self):
        _assert_refusal(offline_read("next"), "ai_control_off")


class TestCliCaller:
    def test_a_terminal_is_the_human(self):
        assert cli_caller(types.SimpleNamespace(isatty=lambda: True)) == HUMAN

    def test_anything_else_is_an_agent(self):
        assert cli_caller(types.SimpleNamespace(isatty=lambda: False)) == AGENT
        assert cli_caller(object()) == AGENT


class TestConfigMigration:
    def test_old_config_gains_the_switches_and_keeps_its_values(self, config_file):
        config_file.write_text(json.dumps({"version": config_mod.CONFIG_VERSION,
                                           "quality": "MAX", "page_size": 9, "future": 1}))
        cfg = load_config()
        assert cfg["quality"] == "MAX" and cfg["page_size"] == 9 and cfg["future"] == 1
        assert cfg["allow_ai_control"] is True
        assert cfg["allow_dangerous_commands"] is False
        assert cfg["ai_control_key"] is None

    def test_a_corrupt_key_reads_as_none(self, config_file):
        config_file.write_text(json.dumps({"ai_control_key": "plaintext"}))
        assert load_config()["ai_control_key"] is None


class TestSettingsPage:
    def _at(self, p, key):
        p._mode = p.MODE_SETTINGS
        p._settings_cursor = [s["key"] for s in config_mod.SETTINGS_ROWS].index(key)

    def test_the_three_rows_are_on_the_page(self):
        text = _player()._build_settings_display().plain
        for label in ("Allow AI control", "Allow dangerous commands", "AI control key"):
            assert label in text

    def test_toggling_a_switch_saves_and_toasts(self, config_file):
        p = _player()
        self._at(p, "allow_dangerous_commands")
        p._handle_key(player_mod.KEY_RIGHT)
        assert json.loads(config_file.read_text())["allow_dangerous_commands"] is True
        assert p._toast == "Allow dangerous commands: On"

    def test_key_entry_is_masked_and_stored_hashed(self, config_file):
        p = _player()
        self._at(p, "ai_control_key")
        p._handle_key(player_mod.KEY_ENTER)
        for ch in "hv k":
            p._handle_key(ch)
        assert p._mode == p.MODE_SETTINGS and not p._volume_open
        assert "hv k" not in p._build_settings_display().plain
        assert "••••" in p._build_settings_display().plain
        p._handle_key(player_mod.KEY_ENTER)
        stored = json.loads(config_file.read_text())["ai_control_key"]
        assert ai_key_matches(stored, "hv k")
        assert "hv k" not in config_file.read_text()
        assert p._toast == "AI control key: Set"

    def test_an_empty_key_clears_it(self, config_file):
        p = _player(ai_control_key=hash_ai_key("old"))
        self._at(p, "ai_control_key")
        p._handle_key(player_mod.KEY_ENTER)
        p._handle_key(player_mod.KEY_ENTER)
        assert json.loads(config_file.read_text())["ai_control_key"] is None
        assert p._toast == "AI control key: Not set"

    def test_esc_cancels_key_entry(self, config_file):
        p = _player()
        self._at(p, "ai_control_key")
        p._handle_key(player_mod.KEY_ENTER)
        p._handle_key("x")
        p._handle_key(player_mod.KEY_ESC)
        assert p._settings_secret is None and not config_file.exists()

    def test_indicator_follows_the_switch(self):
        p = _player()
        assert "AI control" in str(p._build_display().title)
        p.config["allow_ai_control"] = False
        assert "AI control" not in str(p._build_display().title)



class TestQueueAdd:
    def test_end_appends_and_keeps_the_playing_entry(self):
        p = _player()
        p._playing = True
        p._known[("track", "9")] = _track(9)
        result = _agent(p, "queue.add", track_ids=[9])
        assert result["ok"], result
        assert [t.id for t in p._queue] == [1, 2, 3, 9] and p._queue_index == 1
        assert result["result"] == {"added": 1, "position": "end", "index": 3, "queue_length": 4,
                                    "tracks": [commands._track_json(p._queue[3])]}
        assert p._plays == [] and p.session.asked == []

    def test_next_goes_after_the_current_entry(self):
        p = _player()
        p._playing = True
        p._known[("track", "8")], p._known[("track", "9")] = _track(8), _track(9)
        result = _agent(p, "queue.add", track_ids=[8, 9], next=True)
        assert [t.id for t in p._queue] == [1, 2, 8, 9, 3]
        assert result["result"]["index"] == 2 and result["result"]["position"] == "next"
        assert _human(p, "next")["ok"] and p._plays == [8]

    def test_an_unknown_id_is_one_lookup(self):
        p = _player()
        p.session = types.SimpleNamespace(track=lambda tid: _track(int(tid)), is_pkce=False)
        p._reconnect = lambda recheck=False: commands.ONLINE
        assert _human(p, "queue.add", track_ids=["44"])["ok"]
        assert p._queue[-1].id == 44

    def test_an_id_tidal_does_not_know_queues_nothing(self):
        class NotFound(Exception):
            pass
        NotFound.__name__ = "ObjectNotFound"

        def track(tid):
            raise NotFound(tid)
        p = _player()
        p._known[("track", "9")] = _track(9)
        p.session = types.SimpleNamespace(track=track, is_pkce=False)
        p._reconnect = lambda recheck=False: commands.ONLINE
        for caller in (_agent, _human):
            result = caller(p, "queue.add", track_ids=[9, 404])
            assert result["ok"] is False and result["code"] == "not_found", result
            assert "404" in result["reason"] and len(p._queue) == 3

    def test_duplicates_are_kept(self):
        p = _player()
        _human(p, "queue.add", track_ids=[2])
        assert [t.id for t in p._queue] == [1, 2, 3, 2]

    def test_nothing_loaded_loads_the_first_without_playing(self):
        p = _player()
        p._queue, p._queue_index, p._current_track = [], -1, None
        p._known[("track", "9")] = _track(9)
        result = _human(p, "queue.add", track_ids=[9])["result"]
        assert p._current_track.id == 9 and p._queue_index == 0 and not p._playing
        assert p._plays == [] and result["playing"] is False and "`resume`" in result["note"]

    def test_paused_says_how_to_play_it(self):
        p = _player()
        result = _human(p, "queue.add", track_ids=[1])["result"]
        assert result["note"].endswith("`queue play 3` plays it.")

    def test_an_album_from_the_cache_costs_nothing(self):
        p = _player()
        p._cache.put_items("album:77", [_track(10), _track(11)])
        result = _agent(p, "queue.add", album="77")
        assert result["ok"] and result["result"]["added"] == 2
        assert [t.id for t in p._queue][-2:] == [10, 11] and p.session.asked == []

    def test_a_playlist_open_in_the_player_costs_nothing(self):
        p = _player()
        p._lists[("playlist", "pl")] = [_track(20)]
        assert _agent(p, "queue.add", playlist="pl", position="next")["ok"]
        assert [t.id for t in p._queue] == [1, 2, 20, 3]

    @pytest.mark.parametrize("args", [{}, {"track_ids": []}, {"track_ids": [1], "album": "7"},
                                      {"track_ids": [1], "position": "middle"}])
    def test_bad_args(self, args):
        p = _player()
        assert _agent(p, "queue.add", **args)["code"] == "bad_args"
        assert len(p._queue) == 3

    def test_an_empty_list_is_empty(self):
        p = _player()
        p._lists[("playlist", "pl")] = []
        p._cache.put_playlist_tracks("pl", [])
        p.session = types.SimpleNamespace(
            playlist=lambda pid: types.SimpleNamespace(tracks=lambda: []), is_pkce=False)
        p._reconnect = lambda recheck=False: commands.ONLINE
        assert _agent(p, "queue.add", playlist="pl")["code"] == "empty"

    def test_ai_control_off_refuses(self):
        p = _player(allow_ai_control=False)
        _assert_refusal(_agent(p, "queue.add", track_ids=[1]), "ai_control_off")
        assert len(p._queue) == 3

    def test_cost_counts_only_what_is_not_known(self):
        p = _player()
        p._known[("track", "9")] = _track(9)
        assert commands.queue_add_cost(p, {"track_ids": [1, 9]}) == 0
        assert commands.queue_add_cost(p, {"track_ids": [1, 55, 66]}) == 2
        assert commands.queue_add_cost(p, {"album": "77"}) == 2
        p._cache.put_items("album:77", [_track(10)])
        assert commands.queue_add_cost(p, {"album": "77"}) == 0
