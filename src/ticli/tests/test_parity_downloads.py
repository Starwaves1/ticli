"""Whole-list downloads (`download.album`, `download.playlist`) and the paging and
playlist-create arguments the legacy agent verbs and human verbs pass through."""

import json
import time

import pytest
from click.testing import CliRunner

from ticli import agent as agent_mod
from ticli import agentq, humancli, ipc
from ticli import player as player_mod
from ticli.cli import cli
from ticli.tests.agent_harness import GYM, Harness
from ticli.tests.fakes import fake_album, fake_track
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod
from ticli.utils import throttle


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ipc, "spawn_player", lambda *a, **kw: "error: no player in tests")


@pytest.fixture
def player():
    made = []

    def _make(**kw):
        made.append(Harness(**kw))
        return made[-1]

    yield _make
    for h in made:
        h.stop()


@pytest.fixture
def jobs(player, monkeypatch):
    monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.01)
    h = player()
    acquired = []
    monkeypatch.setattr(throttle, "acquire", lambda *a, **k: acquired.append(1))
    h.core.planned = planned = []
    h.core._download_plan = lambda track, tier: planned.append(track.id) or {"track_id": track.id, "title": "t"}
    h.core._download_deliver = lambda plan, abandoned=None, progress=None, record=True: (None, "", 1, False)
    h.core._track_estimate = lambda track, tier: 1
    return h, acquired


def _settle(fn, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not fn():
        assert time.monotonic() < deadline, "never settled"
        time.sleep(0.01)


def _done(h):
    _settle(lambda: (h.core._download_job or {}).get("state") == "done")


def _album(h, aid="777", name="Night Album", n=3):
    tracks = [fake_track(100 + i) for i in range(n)]
    h.core._known[("album", aid)] = fake_album(aid, name, tracks=tracks)
    h.session.album_tracks[aid] = tracks
    return tracks


def _queued(h, cmd, args):
    """An agent's TIDAL command is answered at once; what came of it is the last `done` row."""
    reply = h.agent(cmd, args)
    assert reply["ok"], reply
    if "queued" not in reply["result"]:
        return {"ok": True, **reply["result"]}
    h.idle()
    return h.agent("status")["result"]["done"][-1]


class TestDownloadAlbum:
    def test_a_known_album_downloads_all_its_tracks_paced_and_labelled(self, jobs):
        h, acquired = jobs
        _album(h)
        assert _queued(h, "download.album", {"id": "777"})["ok"]
        _done(h)
        assert sorted(h.core.planned) == [100, 101, 102] and len(acquired) >= 3 and h.core._download_job["tracks"] == 3
        assert h.core._download_job["labels"] == ("Night Album",)

    def test_the_result_names_the_album_and_the_tier(self, jobs):
        h, _acquired = jobs
        _album(h)
        reply = h.human("download.album", {"id": "777", "tier": "high"})
        assert reply["ok"], reply
        assert reply["result"] == {"accepted": True, "tracks": 3, "tier": "HIGH",
                                   "label": "Night Album", "id": "777"}

    def test_an_unknown_album_is_labelled_by_its_first_track(self, jobs):
        h, _acquired = jobs
        h.session.album_tracks["555"] = [fake_track(7), fake_track(8)]
        assert _queued(h, "download.album", {"id": "555", "tier": "high"})["ok"]
        _done(h)
        assert h.core._download_job["labels"] == ("Album 7",) and h.core._download_job["tracks"] == 2
        assert h.core._download_job["tier"] == "HIGH"

    def test_the_estimate_is_two_until_the_tracks_are_known(self, player):
        h = player()
        assert agentq.estimate(h.core, "download.album", {"id": "777"}) == 2
        h.core._lists[("album", "777")] = [fake_track(1)]
        assert agentq.estimate(h.core, "download.album", {"id": "777"}) == 0

    def test_a_listed_album_costs_no_request(self, jobs):
        h, _acquired = jobs
        h.core._lists[("album", "777")] = [fake_track(1), fake_track(2)]
        before = len(h.session.requests)
        assert _queued(h, "download.album", {"id": "777"})["ok"]
        assert h.session.requests[before:] == []

    def test_the_agent_toast_says_what_started(self, jobs):
        h, _acquired = jobs
        _album(h)
        assert h.agent("download.album", {"id": "777"})["ok"]
        h.idle()
        assert h.core._toast.startswith('agent: downloading album "')
        assert 'downloading album "Night Album" (3 tracks)' in h.core._toast

    def test_a_bad_tier_is_refused(self, jobs):
        h, _acquired = jobs
        _album(h)
        assert _queued(h, "download.album", {"id": "777", "tier": "ultra"})["code"] == "bad_args"
        reply = h.human("download.album", {"id": "777", "tier": "ultra"})
        assert reply["code"] == "bad_args" and "LOW, MEDIUM, HIGH or MAX" in reply["reason"]

    def test_an_empty_album_is_refused(self, jobs):
        h, _acquired = jobs
        h.session.album_tracks["404"] = []
        assert _queued(h, "download.album", {"id": "404"})["code"] == "empty"

    def test_an_id_is_required(self, jobs):
        h, _acquired = jobs
        reply = h.human("download.album", {"id": "  "})
        assert reply["code"] == "bad_args" and "needs an id" in reply["reason"]


class TestDownloadPlaylist:
    def test_a_playlist_downloads_its_tracks_labelled_by_name(self, jobs):
        h, acquired = jobs
        h.gym.items = [fake_track(11), fake_track(12)]
        assert _queued(h, "download.playlist", {"id": GYM})["ok"]
        _done(h)
        assert sorted(h.core.planned) == [11, 12] and len(acquired) >= 2 and h.core._download_job["labels"] == ("Gym",)

    def test_a_listed_playlist_costs_no_request_for_its_tracks(self, jobs):
        h, _acquired = jobs
        h.core._lists[("playlist", GYM)] = [fake_track(11), fake_track(12), fake_track(13)]
        assert agentq.estimate(h.core, "download.playlist", {"id": GYM}) == 0
        before = len(h.session.requests)
        reply = h.human("download.playlist", {"id": GYM})
        assert reply["ok"] and reply["result"]["tracks"] == 3 and reply["result"]["label"] == "Gym"
        assert h.session.requests[before:] == []

    def test_a_bad_tier_and_an_empty_playlist_are_refused(self, jobs):
        h, _acquired = jobs
        assert h.human("download.playlist", {"id": GYM, "tier": "best"})["code"] == "bad_args"
        reply = h.human("download.playlist", {"id": GYM})
        assert reply["code"] == "empty" and "playlist has no tracks" in reply["reason"], reply


def test_the_human_line_for_a_whole_list_download():
    reply = {"ok": True, "result": {"tracks": 3, "label": "X", "tier": "HIGH"}}
    assert humancli.line("download.album", reply) == 'downloading 3 tracks of "X" at HIGH'
    assert humancli.NAMED["download.album"] == "album"


class TestSearchPaging:
    def test_the_agent_verb_sends_the_offset_and_reports_it(self, monkeypatch):
        sent = []
        monkeypatch.setattr(agent_mod, "call", lambda cmd, args: sent.append((cmd, args)) or
                            {"ok": True, "result": {"tracks": []}})
        result = CliRunner().invoke(cli, ["agent", "search", "q", "--offset", "10"])
        assert result.exit_code == 0, result.output
        assert sent == [("search", {"query": "q", "types": ["tracks"], "limit": 10, "offset": 10})]
        assert json.loads(result.output)["offset"] == 10

    def test_the_human_verb_passes_the_offset(self, monkeypatch):
        sent = []
        monkeypatch.setattr(humancli, "run", lambda cmd, args, *a, **kw: sent.append((cmd, args)))
        result = CliRunner().invoke(cli, ["search", "q", "--offset", "5"])
        assert result.exit_code == 0, result.output
        assert len(sent) == 1 and sent[0][0] == "search" and sent[0][1]["offset"] == 5

    def test_a_later_page_goes_to_tidal_and_leaves_the_search_cache_alone(self, player):
        h = player(session=None)
        seen = []
        real = h.session.search
        h.session.search = lambda query, models=None, limit=50, offset=0: (
            seen.append(offset), real(query, models=models, limit=limit, offset=offset))[1]
        reply = h.agent("search", {"query": "paged", "offset": 10})
        assert reply["ok"], reply
        assert seen == [10] and h.core._cache.get_items("search:paged") is None
        assert h.agent("search", {"query": "first"})["ok"]
        assert seen == [10, 0] and h.core._cache.get_items("search:first") is not None


class TestLegacyPlaylistCreate:
    def _create(self, monkeypatch, *words):
        sent = []
        monkeypatch.setattr(agent_mod, "call", lambda cmd, args: sent.append((cmd, dict(args))) or
                            {"ok": True, "result": {"playlist": {"id": "p", "name": "Mix"}, "added": 2}})
        result = CliRunner().invoke(cli, ["agent", "playlist", "create", *words])
        assert result.exit_code == 0, result.output
        return sent, json.loads(result.output)

    def test_ids_and_description_are_sent_and_added_comes_back(self, monkeypatch):
        sent, out = self._create(monkeypatch, "Mix", "1", "2", "--description", "D")
        assert sent == [("playlist.create", {"name": "Mix", "description": "D",
                                             "track_ids": ["1", "2"]})]
        assert out["playlist"] == {"id": "p", "name": "Mix"} and out["added"] == 2

    def test_no_ids_sends_no_track_ids(self, monkeypatch):
        sent, _out = self._create(monkeypatch, "Mix")
        assert sent == [("playlist.create", {"name": "Mix", "description": ""})]
