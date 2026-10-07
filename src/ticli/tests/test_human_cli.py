"""The human CLI (`ticli <verb>`): one-line output, names and `<song>` forms, the
terminal check, y/N for dangerous verbs, and not starting a player to say "nothing
playing". Real socket and player core via `agent_harness`; TIDAL is `fakes.FakeTidal`,
so request counts are exact and nothing touches the network.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from ticli import commands, ipc
from ticli.cli import cli
from ticli.tests.agent_harness import GYM, ROAD, Harness
from ticli.tests.fakes import (
    FakeTidal, fake_album, fake_playlist, fake_track,
)
from ticli.utils import config as config_mod
from ticli.utils import downloads
from ticli.utils.cache import MetadataCache


@pytest.fixture(autouse=True)
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(ipc, "spawn_player", lambda *a, **kw: calls.append(a) or "error: no player")
    return calls


@pytest.fixture
def player():
    made = []

    def _make(**kw):
        made.append(Harness(**kw))
        MetadataCache().put_playlists([made[-1].road, made[-1].gym])
        return made[-1]

    yield _make
    for h in made:
        h.stop()


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(commands, "cli_caller", lambda stdin=None: commands.HUMAN)


def ticli(*args, **kw):
    return CliRunner().invoke(cli, list(args), **kw)


def settle(h, fn, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not fn():
        assert time.monotonic() < deadline, "never settled"
        time.sleep(0.01)


class TestTransport:
    def test_status_is_one_line(self, player, tty):
        player()
        result = ticli("status")
        assert result.exit_code == 0
        assert result.output.strip() == "playing: Artist 1 - Track 1 [0:00/3:20] (queue 1/3)"

    def test_pause_resume_next_prev_do_the_thing(self, player, tty):
        h = player()
        assert ticli("pause").output.strip() == "paused"
        assert h.core._playing is False
        assert ticli("status").output.startswith("paused: ")
        assert ticli("resume").output.strip() == "resumed"
        assert h.core._playing is True
        assert ticli("next").output.strip() == "next track"
        assert h.core._queue_index == 1
        assert ticli("prev").output.strip() == "previous track"
        assert h.core._queue_index == 0

    @pytest.mark.parametrize("verb,code", [("status", 0), ("pause", 1), ("resume", 1),
                                           ("next", 1), ("prev", 1)])
    def test_nothing_playing_never_starts_the_player(self, tty, spawned, verb, code):
        result = ticli(verb)
        assert result.output.strip() == "nothing playing" and result.exit_code == code
        assert spawned == []

    def test_status_track_says_duration_seconds_everywhere(self, player):
        h = player()
        track = h.agent("status")["state"]["track"]
        assert track["dur"] == 200
        assert h.human("status")["result"]["track"]["duration_seconds"] == 200
        assert h.human("queue.list")["result"]["tracks"][0]["duration_seconds"] == 200


class TestCallerGating:
    def test_without_a_terminal_ai_control_off_refuses_readably(self, player):
        player()
        config_mod.save_config({**config_mod.DEFAULTS, "allow_ai_control": False})
        result = ticli("pause")  # CliRunner's stdin is not a terminal: an agent
        assert result.exit_code == 1
        assert "(ai_control_off)" in result.output and "Ask your human" in result.output
        assert "never edit config.json" in result.output

    def test_a_terminal_ignores_the_switches(self, player, tty):
        h = player()
        config_mod.save_config({**config_mod.DEFAULTS, "allow_ai_control": False,
                                "ai_control_key": config_mod.hash_ai_key("sesame")})
        assert ticli("pause").exit_code == 0
        assert h.core._playing is False

    def test_the_key_comes_from_the_flag_or_the_environment(self, player):
        h = player()
        h.core.commands._sleep = lambda s: None
        config_mod.save_config({**config_mod.DEFAULTS, "ai_control_key": config_mod.hash_ai_key("sesame")})
        no_key = ticli("pause")
        assert no_key.exit_code == 1 and "key_required" in no_key.output
        assert "wrong_key" in ticli("--key", "nope", "pause").output
        assert ticli("--key", "sesame", "pause").exit_code == 0
        assert h.core._playing is False
        assert ticli("resume", env={"TICLI_AI_KEY": "sesame"}).exit_code == 0
        assert h.core._playing is True

    def test_dangerous_verbs_ask_the_terminal_default_no(self, player, tty, monkeypatch):
        removed = []
        monkeypatch.setattr(downloads, "remove", lambda tid: removed.append(tid) or True)
        player()
        declined = ticli("download", "delete", "5", input="\n")
        assert declined.exit_code == 1 and "cancelled" in declined.output and removed == []
        assert ticli("download", "delete", "5", input="n\n").exit_code == 1 and removed == []
        accepted = ticli("download", "delete", "5", input="y\n")
        assert accepted.exit_code == 0 and removed == ["5"]

    def test_a_script_gets_the_agent_refusal_not_a_prompt(self, player, monkeypatch):
        removed = []
        monkeypatch.setattr(downloads, "remove", lambda tid: removed.append(tid) or True)
        player()
        result = ticli("download", "delete", "5", input="y\n")
        assert result.exit_code == 1 and "(dangerous_off)" in result.output and removed == []

    def test_a_safe_verb_does_not_prompt(self, player, tty):
        player()
        assert "Continue?" not in ticli("pause").output


class TestPlaylistsAndLikes:
    def test_list_and_show(self, player, tty):
        h = player()
        h.road.items = [fake_track(5, "Song", ["Band"])]
        listed = ticli("playlist", "list").output
        assert f"{ROAD}  Road trip (1 tracks)" in listed and f"{GYM}  Gym" in listed
        shown = ticli("playlist", "show", "road trip").output
        assert "Road trip" in shown and "5  Band - Song" in shown

    def test_create_makes_a_playlist(self, player, tty):
        h = player()
        result = ticli("playlist", "create", "Morning Uplift")
        assert result.exit_code == 0
        settle(h, lambda: any(p.name == "Morning Uplift" for p in h.session.playlists.values()))

    def test_add_by_name_ignores_case_and_asks_tidal_nothing_to_find_it(self, player, tty):
        h = player()
        assert ticli("playlist", "add", "ROAD TRIP", "11", "22").exit_code == 0
        settle(h, lambda: h.road.adds)
        assert h.road.adds == [["11", "22"]]
        assert h.session.requests.count("GET search") == 0

    def test_like_defaults_to_the_playing_track(self, player, tty):
        h = player()
        assert ticli("like").output.strip() == "liked"
        settle(h, lambda: "POST favorites/tracks" in h.session.requests)

    def test_download_with_no_song_means_the_playing_track(self, player, tty, monkeypatch):
        h = player()
        started = []
        monkeypatch.setattr(h.core, "_start_download_job", lambda tier: started.append(
            h.core._download_track.id))
        assert ticli("download").exit_code == 0
        assert started == [1]

    def test_the_player_stays_for_a_write_the_cli_has_already_left(self):
        gate, order = __import__("threading").Event(), []
        commands.idle_hook = lambda: order.append(commands.in_flight())
        try:
            commands._background(lambda: gate.wait(2))
            assert commands.in_flight() == 1
            gate.set()
            deadline = time.monotonic() + 2
            while commands.in_flight() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert commands.in_flight() == 0 and order == [0]
        finally:
            commands.idle_hook = None


class TestStart:
    @pytest.fixture
    def tui(self, monkeypatch):
        opened = []
        from ticli import player as player_mod
        monkeypatch.setattr(player_mod, "run_tui", lambda **kw: opened.append(kw))
        return opened

    def test_plays_an_exact_name_then_opens_the_tui(self, player, tty, tui):
        h = player()
        h.road.items = [fake_track(5), fake_track(6)]
        result = ticli("start", "playlist", "road trip")
        assert result.exit_code == 0
        assert 'playing playlist "Road trip": 2 tracks' in result.output
        assert [t.id for t in h.core._queue] == [5, 6]
        assert len(tui) == 1
        assert h.session.requests.count("GET search") == 0

    def test_no_tui_only_plays(self, player, tty, tui):
        h = player()
        h.gym.items = [fake_track(8)]
        assert ticli("start", "playlist", "Gym", "--no-tui").exit_code == 0
        assert [t.id for t in h.core._queue] == [8] and tui == []

    def test_ambiguous_prints_a_numbered_top_five_and_a_number_picks(self, player, tty, tui):
        h = player()
        h.road.name = "EDM one"
        h.gym.name = "EDM two"
        MetadataCache().put_playlists([h.road, h.gym])
        h.gym.items = [fake_track(9)]
        first = ticli("start", "playlist", "edm")
        assert first.exit_code == 1 and tui == []
        assert "1. EDM one" in first.output and "2. EDM two" in first.output
        assert "ticli start playlist 2" in first.output
        assert h.core._queue[0].id == 1  # nothing started
        picked = ticli("start", "playlist", "2", "--no-tui")
        assert picked.exit_code == 0 and [t.id for t in h.core._queue] == [9]

    def test_agents_get_candidates_with_ids(self, player):
        h = player()
        h.road.name = "EDM one"
        h.gym.name = "EDM two"
        MetadataCache().put_playlists([h.road, h.gym])
        result = ticli("start", "playlist", "edm")
        out = json.loads(result.output)
        assert out["code"] == "ambiguous"
        assert [c["id"] for c in out["candidates"]] == [ROAD, GYM]

    def test_no_local_match_makes_exactly_one_tidal_request(self, player, tty, tui):
        pid = "8f1b2c3d-1111-2222-3333-444455556666"
        session = FakeTidal(search_playlists=[fake_playlist(pid, "EDM Mix")])
        h = player(session=session)
        session.add_playlist(pid, "EDM Mix", [fake_track(30)])
        before = len(session.requests)
        assert ticli("start", "playlist", "mix", "--no-tui").exit_code == 0
        assert session.requests[before:].count("GET search") == 1
        assert [t.id for t in h.core._queue] == [30]

    def test_nothing_anywhere_says_so(self, player, tty, tui):
        h = player()
        before = len(h.session.requests)
        result = ticli("start", "playlist", "zzz")
        assert result.exit_code == 1 and "not_found" in result.output
        assert h.session.requests[before:] == ["GET search"] and tui == []

    def test_albums_and_artists_go_to_tidal(self, player, tty, tui):
        album = fake_album(4242, "Discovery", "Daft Punk", [fake_track(61), fake_track(62)])
        h = player(session=FakeTidal(search_albums=[album]))
        assert ticli("start", "album", "discovery", "--no-tui").exit_code == 0
        assert [t.id for t in h.core._queue] == [61, 62]


def _save_paused_track():
    from ticli.utils import throttle
    throttle.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (throttle.STATE_DIR / "player_state.json").write_text(json.dumps(
        {"tracks": [{"id": 7, "name": "Saved", "duration": 200}], "queue_index": 0,
         "position": 30}))


class TestBarePlayAndStart:
    @pytest.fixture
    def tui(self, monkeypatch):
        opened = []
        from ticli import player as player_mod
        monkeypatch.setattr(player_mod, "run_tui", lambda **kw: opened.append(kw))
        return opened

    def test_bare_play_resumes_like_resume(self, player, tty):
        h = player()
        assert ticli("pause").exit_code == 0 and h.core._playing is False
        result = ticli("play")
        assert result.exit_code == 0 and result.output.strip() == "resumed"
        assert h.core._playing is True

    def test_bare_play_with_nothing_saved_says_so_and_starts_nothing(self, tty, spawned):
        result = ticli("play")
        assert result.exit_code == 1 and result.output.strip() == "nothing to play"
        assert spawned == []

    def test_bare_play_starts_the_player_for_a_saved_track(self, tty, spawned):
        _save_paused_track()
        result = ticli("play")
        assert len(spawned) == 1, "the player is started to resume the saved track"
        assert "starting the player" in result.stderr
        assert result.exit_code == 1 and "error: no player" in result.stderr

    def test_bare_play_with_a_running_player_and_no_track_has_nothing_to_play(self, player, tty):
        h = player()
        h.core._current_track = None
        h.core._playing = False
        result = ticli("play")
        assert result.exit_code == 1 and result.output.strip() == "nothing to play"

    def test_bare_start_resumes_then_opens_the_tui(self, player, tty, tui):
        h = player()
        ticli("pause")
        result = ticli("start")
        assert result.exit_code == 0 and result.output.strip() == "resumed"
        assert h.core._playing is True and len(tui) == 1

    def test_bare_start_with_nothing_saved_just_opens_the_tui(self, tty, spawned, tui):
        result = ticli("start")
        assert result.exit_code == 0 and result.output.strip() == ""
        assert len(tui) == 1 and spawned == []

    def test_bare_start_no_tui_only_resumes(self, player, tty, tui):
        h = player()
        ticli("pause")
        assert ticli("start", "--no-tui").exit_code == 0
        assert h.core._playing is True and tui == []

    def test_start_kind_without_a_name_is_a_usage_error(self, tty, tui):
        result = ticli("start", "playlist")
        assert result.exit_code == 2 and "NAME" in result.output and tui == []


class TestPlayAnything:
    def test_an_exact_album_name_plays_the_album_with_one_search(self, player, tty):
        album = fake_album(4242, "How to Listen to This Album", "Artist",
                           [fake_track(61), fake_track(62)])
        h = player(session=FakeTidal(search_albums=[album]))
        before = len(h.session.requests)
        result = ticli("play", "How", "to", "listen", "to", "this", "album")
        assert result.exit_code == 0, result.output
        assert 'playing album "How to Listen to This Album - Artist": 2 tracks' in result.output
        assert [t.id for t in h.core._queue] == [61, 62]
        assert h.session.requests[before:].count("GET search") == 1

    def test_your_playlist_by_exact_name_costs_no_request(self, player, tty):
        h = player()
        h.road.items = [fake_track(5), fake_track(6)]
        before = len(h.session.requests)
        assert ticli("play", "road", "trip").exit_code == 0
        assert [t.id for t in h.core._queue] == [5, 6]
        assert "GET search" not in h.session.requests[before:]

    def test_an_exact_track_title_plays_the_track(self, player, tty):
        h = player(session=FakeTidal(search_tracks=[fake_track(7, "One More Time", ["Daft Punk"])]))
        assert ticli("play", "one", "more", "time").exit_code == 0
        assert h.core._current_track.id == 7

    def test_several_loose_matches_number_them_and_a_number_picks(self, player, tty):
        a = fake_album(4242, "Discovery Live", "Daft Punk", [fake_track(61)])
        b = fake_album(4343, "Discovery Remixes", "Daft Punk", [fake_track(71)])
        h = player(session=FakeTidal(search_albums=[a, b]))
        first = ticli("play", "discovery")
        assert first.exit_code == 1
        assert "1. album: Discovery Live" in first.output and "ticli play 2" in first.output
        assert ticli("play", "2").exit_code == 0
        assert [t.id for t in h.core._queue] == [71]

    def test_a_bare_number_plays_that_track_without_searching(self, player, tty):
        h = player()
        before = len(h.session.requests)
        result = ticli("play", "307345598")
        assert result.exit_code == 0, result.output
        assert str(h.core._current_track.id) == "307345598"
        assert "GET search" not in h.session.requests[before:]

    def test_a_playlist_uuid_plays_that_playlist(self, player, tty):
        from ticli.tests.agent_harness import ROAD
        h = player()
        h.road.items = [fake_track(5)]
        assert ticli("play", ROAD).exit_code == 0
        assert [t.id for t in h.core._queue] == [5]

    def test_the_kinded_verbs_still_work(self, player, tty):
        album = fake_album(4242, "Discovery", "Daft Punk", [fake_track(61), fake_track(62)])
        h = player(session=FakeTidal(search_albums=[album]))
        assert ticli("play", "album", "discovery").exit_code == 0
        assert [t.id for t in h.core._queue] == [61, 62]

    def test_a_tidal_url_plays_without_searching(self, player, tty):
        album = fake_album(4242, "Discovery", "Daft Punk", [fake_track(61)])
        session = FakeTidal(search_albums=[album])
        session.album_tracks["4242"] = [fake_track(61)]
        h = player(session=session)
        before = len(session.requests)
        result = ticli("play", "https://tidal.com/browse/album/4242")
        assert result.exit_code == 0, result.output
        assert "GET search" not in session.requests[before:]
        assert [t.id for t in h.core._queue] == [61]


class TestSongForms:
    def add(self, *song):
        return ticli("playlist", "add", ROAD, *song)

    def test_a_track_id(self, player, tty):
        h = player()
        assert self.add("123456").exit_code == 0
        settle(h, lambda: h.road.adds)
        assert h.road.adds == [["123456"]]

    def test_track_urls_in_both_shapes(self, player, tty):
        h = player()
        assert self.add("https://tidal.com/browse/track/777", "https://listen.tidal.com/album/5/track/888").exit_code == 0
        settle(h, lambda: h.road.adds)
        assert h.road.adds == [["777", "888"]]

    def test_an_album_url_is_all_its_tracks(self, player, tty):
        session = FakeTidal()
        h = player(session=session)
        session.album_tracks["99"] = [fake_track(71), fake_track(72)]
        assert self.add("https://tidal.com/browse/album/99").exit_code == 0
        settle(h, lambda: h.road.adds)
        assert h.road.adds == [["71", "72"]]

    def test_a_playlist_url_is_all_its_tracks(self, player, tty):
        h = player()
        h.gym.items = [fake_track(81), fake_track(82)]
        assert self.add(f"https://tidal.com/browse/playlist/{GYM}").exit_code == 0
        settle(h, lambda: h.road.adds)
        assert h.road.adds == [["81", "82"]]

    def test_current(self, player, tty):
        h = player()
        assert self.add("current").exit_code == 0
        settle(h, lambda: h.road.adds)
        assert h.road.adds == [["1"]]

    def test_current_with_nothing_playing_does_not_start_the_player(self, tty, spawned):
        from ticli.tests.fakes import fake_playlist
        MetadataCache().put_playlists([fake_playlist(ROAD, "Road trip")])
        result = ticli("playlist", "add", ROAD, "current")
        assert result.exit_code == 1 and "no_track" in result.output and spawned == []

    def test_artist_dash_title_adds_only_when_confident(self, player, tty):
        session = FakeTidal(search_tracks=[fake_track(7, "One More Time", ["Daft Punk"])])
        h = player(session=session)
        assert self.add("daft punk - one more time").exit_code == 0
        settle(h, lambda: h.road.adds)
        assert h.road.adds == [["7"]]
        assert session.requests.count("GET search") == 1

    def test_a_remix_is_candidates_not_an_add(self, player, tty):
        session = FakeTidal(search_tracks=[fake_track(8, "One More Time (Remix)", ["Daft Punk"]),
                                           fake_track(9, "One More Time", ["Somebody Else"])])
        h = player(session=session)
        result = self.add("daft punk - one more time")
        assert result.exit_code == 1 and "not_confident" in result.output
        assert "8  Daft Punk - One More Time (Remix)" in result.output
        time.sleep(0.1)
        assert h.road.adds == []

    def test_agents_get_candidate_json(self, player):
        session = FakeTidal(search_tracks=[fake_track(8, "One More Time (Remix)", ["Daft Punk"])])
        player(session=session)
        out = json.loads(self.add("daft punk - one more time").output)
        assert out["code"] == "not_confident" and out["candidates"][0]["id"] == "8"

    def test_a_bare_word_is_not_a_song(self, player, tty):
        player()
        result = self.add("whatever")
        assert result.exit_code == 1 and "bad_args" in result.output


class TestShape:
    def test_help_is_instant_and_never_imports_the_player(self):
        code = ("import sys\nfrom click.testing import CliRunner\nfrom ticli.cli import cli\n"
                "r = CliRunner().invoke(cli, ['--help'])\nassert r.exit_code == 0\n"
                "print('ticli.player' in sys.modules or 'tidalapi' in sys.modules)")
        src = str(Path(__file__).resolve().parents[2])
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env={"PYTHONPATH": src, "PATH": "/usr/bin"}, timeout=30)
        assert out.stdout.strip() == "False", out.stderr

    def test_bare_ticli_still_opens_the_tui(self, monkeypatch):
        from ticli import player as player_mod
        opened = []
        monkeypatch.setattr(player_mod, "run_tui", lambda **kw: opened.append(kw))
        assert ticli().exit_code == 0 and len(opened) == 1

    def test_every_registry_command_has_a_human_verb(self):
        from ticli.cli import HUMAN_COVERED, HUMAN_HIDDEN
        from ticli.commands import COMMANDS
        out = ticli("--help").output
        for name in set(COMMANDS) - HUMAN_COVERED - HUMAN_HIDDEN:
            assert name.replace(".", " ") in out, name
        for hand in ("pause", "resume", "next", "prev", "status", "start", "search"):
            assert hand in out


class TestQueueOps:
    def test_queue_remove_pins_the_track_it_was_shown(self, player, tty):
        h = player()
        seen = []
        real = h.core.commands.execute

        def spy(name, args=None, *a, **kw):
            if name == "queue.remove":
                seen.append(dict(args or {}))
            return real(name, args, *a, **kw)
        h.core.commands.execute = spy
        assert ticli("queue", "remove", "2").exit_code == 0
        assert seen == [{"index": 2, "track_id": 3}]
        assert [t.id for t in h.core._queue] == [1, 2]

    def test_a_queue_that_moved_is_stale_not_another_track(self, player):
        h = player()
        reply = h.agent("queue.remove", {"index": 2, "track_id": 1})
        assert reply["code"] == "stale" and len(h.core._queue) == 3


class TestSavedQueueWithNoPlayer:
    @pytest.fixture
    def saved(self):
        from ticli.utils import throttle
        throttle.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (throttle.STATE_DIR / "player_state.json").write_text(json.dumps({
            "tracks": [{"id": 251380837, "name": "One More Time", "artists": ["Daft Punk"],
                        "duration": 320}], "queue_index": 0, "position": 61}))

    def test_status_reads_the_saved_queue(self, tty, spawned, saved):
        result = ticli("status")
        assert result.exit_code == 0 and spawned == []
        assert result.output.strip() == "paused (saved): Daft Punk - One More Time [1:01/5:20] (queue 1/1)"

    @pytest.mark.parametrize("verb", ["pause", "next", "prev"])
    def test_other_transport_still_does_not_start_it(self, tty, spawned, saved, verb):
        result = ticli(verb)
        assert result.output.strip() == "nothing playing" and spawned == []

    def test_resume_starts_the_player_and_plays(self, tty, saved, monkeypatch):
        made = []

        def spawn(*a, **kw):
            made.append(Harness())
            made[-1].core._playing = False
            return "ready"
        monkeypatch.setattr(ipc, "spawn_player", spawn)
        try:
            result = ticli("resume")
            assert result.exit_code == 0 and result.stdout.strip() == "resumed"
            assert result.stderr.strip() == "starting the player..."
            assert len(made) == 1 and made[0].core._playing is True
        finally:
            for h in made:
                h.stop()

    def test_resume_with_nothing_saved_says_so(self, tty, spawned):
        result = ticli("resume")
        assert result.output.strip() == "nothing playing" and spawned == []


class TestScriptReadsWithAiControlOff:
    def test_search_and_playlist_list_answer_from_disk(self, spawned):
        MetadataCache().put_playlists([fake_playlist(ROAD, "Foo Fighters mix")])
        config_mod.save_config({**config_mod.DEFAULTS, "allow_ai_control": False})
        searched = ticli("search", "foo")
        assert searched.exit_code == 0 and "Foo Fighters mix" in searched.output
        listed = ticli("playlist", "list")
        assert listed.exit_code == 0 and f"{ROAD}  Foo Fighters mix" in listed.output
        assert spawned == []
        assert ticli("pause").exit_code == 1


class TestLongLists:
    def _album(self, session, n):
        session.album_tracks["424242"] = [fake_track(251380000 + i) for i in range(n)]

    def test_an_album_url_pages_through_every_track(self, player):
        session = FakeTidal()
        h = player(session=session)
        self._album(session, 250)
        assert ticli("playlist", "add", ROAD, "https://tidal.com/browse/album/424242").exit_code == 0
        h.idle()
        assert [len(a) for a in h.road.adds] == [100, 100, 50]
        assert session.requests.count("GET albums/424242") == 1

    def test_a_human_is_asked_above_two_hundred(self, player, tty):
        session = FakeTidal()
        h = player(session=session)
        self._album(session, 250)
        declined = ticli("playlist", "add", ROAD, "https://tidal.com/browse/album/424242", input="\n")
        assert declined.exit_code == 1 and "250 tracks" in declined.output
        time.sleep(0.1)
        assert h.road.adds == []
        accepted = ticli("playlist", "add", ROAD, "https://tidal.com/browse/album/424242", input="y\n")
        assert accepted.exit_code == 0
        settle(h, lambda: sum(map(len, h.road.adds)) == 250)

    def test_a_list_that_comes_up_short_says_n_of_m(self, player, tty, monkeypatch):
        import types
        session = FakeTidal()
        h = player(session=session)
        tracks = [fake_track(251380000 + i) for i in range(10)]
        monkeypatch.setattr(session, "album", lambda aid: types.SimpleNamespace(
            id=aid, name="A", num_tracks=12,
            tracks=lambda limit=None, offset=0: tracks[offset:offset + (limit or 10)]))
        result = ticli("playlist", "add", ROAD, "https://tidal.com/browse/album/424242")
        assert result.exit_code == 0 and "got 10 of 12 tracks" in result.output
        settle(h, lambda: h.road.adds)


class TestPicks:
    @pytest.fixture
    def edm(self, player):
        h = player()
        h.road.name, h.gym.name = "EDM one", "EDM two"
        MetadataCache().put_playlists([h.road, h.gym])
        h.gym.items = [fake_track(9)]
        h.road.items = [fake_track(8)]
        return h

    @pytest.fixture
    def tui(self, monkeypatch):
        from ticli import player as player_mod
        monkeypatch.setattr(player_mod, "run_tui", lambda **kw: None)

    def test_a_pick_expires(self, edm, tty, tui):
        from ticli import humancli
        assert ticli("start", "playlist", "edm").exit_code == 1
        saved = json.loads(humancli._picks_file().read_text())
        saved["human"]["at"] -= humancli.PICK_TTL_SECONDS + 1
        humancli._picks_file().write_text(json.dumps(saved))
        assert ticli("start", "playlist", "2", "--no-tui").exit_code == 1
        assert edm.core._queue[0].id == 1

    def test_a_scripts_ambiguity_keeps_the_humans_numbering(self, edm, tty, tui, monkeypatch):
        assert ticli("start", "playlist", "edm").exit_code == 1  # 1. EDM one  2. EDM two
        three = fake_playlist("aaaaaaaa-0000-4000-8000-00000000000e", "EDM three")
        MetadataCache().put_playlists([three, edm.gym, edm.road])
        monkeypatch.setattr(commands, "cli_caller", lambda stdin=None: commands.AGENT)
        script = json.loads(ticli("start", "playlist", "edm").output)
        assert script["code"] == "ambiguous" and script["candidates"][0]["name"] == "EDM three"
        monkeypatch.setattr(commands, "cli_caller", lambda stdin=None: commands.HUMAN)
        assert ticli("start", "playlist", "1", "--no-tui").exit_code == 0
        assert [t.id for t in edm.core._queue] == [8]

    def test_an_exact_local_name_beats_a_stale_pick(self, edm, tty, tui):
        assert ticli("start", "playlist", "edm").exit_code == 1
        edm.road.name = "2"
        MetadataCache().put_playlists([edm.road, edm.gym])
        assert ticli("start", "playlist", "2", "--no-tui").exit_code == 0
        assert [t.id for t in edm.core._queue] == [8]

    def test_the_add_hint_keeps_the_song(self, edm, tty):
        result = ticli("playlist", "add", "edm", "251380837")
        assert result.exit_code == 1 and "ticli playlist add 2 251380837" in result.output

    def test_a_short_number_is_an_album_name(self, player, tty, tui):
        album = fake_album(424242, "1989", "Taylor Swift", [fake_track(61)])
        h = player(session=FakeTidal(search_albums=[album]))
        assert ticli("start", "album", "1989", "--no-tui").exit_code == 0
        assert h.session.requests.count("GET search") == 1
        assert [t.id for t in h.core._queue] == [61]


class TestQueue:
    def ids(self, h):
        return [t.id for t in h.core._queue]

    def test_a_song_by_name_goes_at_the_end_and_keeps_playing(self, player, tty):
        h = player(session=FakeTidal(search_tracks=[fake_track(7, "Reach Out", ["Marc Rebillet"])]))
        result = ticli("queue", "reach", "out")
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "queued Marc Rebillet - Reach Out at the end (#4 of 4)"
        assert self.ids(h) == [1, 2, 3, 7] and h.core._current_track.id == 1 and h.core._playing

    def test_next_and_artist_dash_title(self, player, tty):
        h = player(session=FakeTidal(search_tracks=[fake_track(7, "Reach Out", ["Marc Rebillet"])]))
        result = ticli("queue", "--next", "Marc", "Rebillet", "-", "Reach", "Out")
        assert result.exit_code == 0, result.output
        assert "next (#2 of 4)" in result.output and self.ids(h) == [1, 7, 2, 3]

    def test_your_playlist_by_name_costs_no_request(self, player, tty):
        h = player()
        h.road.items = [fake_track(5), fake_track(6)]
        before = len(h.session.requests)
        result = ticli("queue", "road", "trip")
        assert result.output.strip() == "queued 2 tracks at the end (#4 of 5)"
        assert self.ids(h) == [1, 2, 3, 5, 6]
        assert "GET search" not in h.session.requests[before:]

    def test_a_bare_number_is_a_track_without_searching(self, player, tty):
        h = player()
        before = len(h.session.requests)
        assert ticli("queue", "307345598").exit_code == 0
        assert str(self.ids(h)[-1]) == "307345598"
        assert "GET search" not in h.session.requests[before:]

    def test_ambiguous_numbers_them_and_a_number_picks(self, player, tty):
        a = fake_album(4242, "Discovery Live", "Daft Punk")
        b = fake_album(4343, "Discovery Remixes", "Daft Punk", [fake_track(71)])
        h = player(session=FakeTidal(search_albums=[a, b]))
        first = ticli("queue", "discovery")
        assert first.exit_code == 1 and "ticli queue 2" in first.output and len(h.core._queue) == 3
        picked = ticli("queue", "2")
        assert picked.exit_code == 0, picked.output
        assert self.ids(h) == [1, 2, 3, 71]

    def test_an_artist_is_refused(self, player, tty):
        from types import SimpleNamespace
        h = player(session=FakeTidal(search_artists=[SimpleNamespace(id=5, name="Marc Rebillet")]))
        result = ticli("queue", "marc", "rebillet")
        assert result.exit_code == 1 and "artist" in result.output and len(h.core._queue) == 3

    def test_bare_queue_lists_it(self, player, tty):
        player()
        assert ticli("queue").output.splitlines()[0] == "> 0  Artist 1 - Track 1"

    def test_the_plain_verbs_still_work(self, player, tty):
        h = player()
        assert ticli("queue", "add", "2", "--next").exit_code == 0
        assert self.ids(h) == [1, 2, 2, 3]
        assert ticli("queue", "remove", "1").exit_code == 0 and self.ids(h) == [1, 2, 3]

    def test_paused_queues_without_playing_and_says_how(self, player, tty):
        h = player()
        ticli("pause")
        result = ticli("queue", "307345598")
        assert "Not playing, so nothing started; `queue play 3` plays it." in result.output
        assert h.core._playing is False

    def test_no_player_starts_one_and_queues_without_playing(self, tty, monkeypatch):
        made = []

        def spawn(*a, **kw):
            made.append(Harness())
            h = made[-1]
            h.core._queue, h.core._queue_index, h.core._current_track = [], -1, None
            h.core._playing = False
            return "ready"
        monkeypatch.setattr(ipc, "spawn_player", spawn)
        try:
            result = ticli("queue", "307345598")
            assert result.exit_code == 0, result.output
            assert "starting the player..." in result.stderr
            assert "`resume` plays it" in result.stdout
            core = made[0].core
            assert [str(t.id) for t in core._queue] == ["307345598"] and core._queue_index == 0
            assert core._playing is False
        finally:
            for h in made:
                h.stop()

    def test_a_script_is_gated_like_an_agent(self, player):
        config_mod.save_config({**config_mod.DEFAULTS, "allow_ai_control": False})
        h = player()
        result = ticli("queue", "307345598")
        assert result.exit_code == 1 and "ai_control_off" in result.output
        assert len(h.core._queue) == 3
