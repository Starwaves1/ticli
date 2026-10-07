"""Playlist delete/rename/describe and favourites: the agent and command layers."""

import types

import pytest

from ticli import agent, agentq, commands, humancli
from ticli.tests.agent_harness import GYM, ROAD, Harness
from ticli.tests.fakes import fake_album
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture
def h():
    made = Harness()
    made.core.config["allow_dangerous_commands"] = True
    yield made
    made.stop()


def _seed_playlists(h):
    h.core._cache.put("playlists", [
        {"id": ROAD, "name": "Road trip", "num_tracks": 0, "creator": None, "editable": True},
        {"id": GYM, "name": "Gym", "num_tracks": 0, "creator": None, "editable": True}])


def _run(h, cmd, args):
    """The outcome: playlist edits wait for it; the rest are read back from `status`."""
    reply = h.agent(cmd, args)
    h.idle()
    done = h.agent("status")["result"]["done"][-1]
    if cmd in agentq.WAITS:
        assert done["ok"] == reply["ok"]
        return reply
    assert reply["ok"]
    return done


@pytest.fixture
def inline(monkeypatch):
    monkeypatch.setattr(commands._inline, "on", True, raising=False)


class TestDelete:
    def test_refused_while_dangerous_commands_are_off(self, h):
        h.core.config["allow_dangerous_commands"] = False
        reply = h.agent("playlist.delete", {"id": GYM})
        assert reply["code"] == "dangerous_off"
        assert GYM in h.session.playlists and h.session.requests == []

    def test_deletes_a_known_playlist_in_one_request(self, h):
        _seed_playlists(h)
        h.core._last_playlist_id = GYM
        row = _run(h, "playlist.delete", {"id": GYM})
        assert row["ok"] and row["result"]["deleted"] and row["result"]["name"] == "Gym"
        assert row["cost"]["requests"] == 1
        assert GYM not in h.session.playlists
        assert h.gym not in h.core._editable_playlists and h.road in h.core._editable_playlists
        assert h.session.requests == [f"DELETE playlists/{GYM}"]
        assert h.core._toast == 'agent: deleted playlist "Gym"'
        assert h.core._last_playlist_id is None
        assert [r["id"] for r in h.core._cache.get("playlists")] == [ROAD]
        assert h.core._cache.get("playlists")[0]["editable"] is True

    def test_a_playlist_without_delete_is_not_yours(self, h):
        h.core._editable_playlists = []
        h.core._known[("playlist", "other")] = types.SimpleNamespace(id="other", name="Theirs")
        h.session.playlists["other"] = types.SimpleNamespace(id="other", name="Theirs")
        assert _run(h, "playlist.delete", {"id": "other"})["code"] == "not_yours"
        assert "other" in h.session.playlists

    def test_the_cli_refuses_a_name_but_lets_an_id_through(self):
        _, _, refused = agent.with_ids("playlist.delete", {"id": "Gym"})
        assert refused["code"] == "bad_args"
        _, _, refused = agent.with_ids("playlist.delete", {"id": GYM})
        assert refused is None

    def test_confirmation_shows_the_resolved_name(self, monkeypatch):
        seen = []
        monkeypatch.setattr(humancli.click, "confirm", lambda text, default=False: seen.append(text) or True)
        assert humancli.confirmed("playlist.delete", {"id": GYM}, {"id": "Gym"}) is True
        assert '"Gym"' in seen[0] and GYM in seen[0]


class TestEdit:
    def test_rename_changes_the_object_and_the_cache(self, h, inline):
        _seed_playlists(h)
        out = commands._playlist_rename(h.core, {"id": GYM, "name": " Lifting "})
        assert out["was"] == "Gym" and out["name"] == "Lifting"
        assert h.gym.name == "Lifting" and h.gym.edits == [("Lifting", None)]
        assert {r["id"]: r["name"] for r in h.core._cache.get("playlists")} == {
            ROAD: "Road trip", GYM: "Lifting"}
        assert h.core._toast == 'Renamed "Gym" to "Lifting"'
        assert h.session.requests == [f"POST playlists/{GYM}"]

    def test_rename_needs_dangerous_commands(self, h):
        h.core.config["allow_dangerous_commands"] = False
        reply = h.agent("playlist.rename", {"id": GYM, "name": "Lifting"})
        assert reply["code"] == "dangerous_off"
        assert h.gym.name == "Gym" and h.gym.edits == []

    def test_empty_rename_and_description_are_bad_args(self, h):
        assert _run(h, "playlist.rename", {"id": GYM, "name": "  "})["code"] == "bad_args"
        assert _run(h, "playlist.describe", {"id": GYM, "description": ""})["code"] == "bad_args"
        assert h.gym.edits == []

    def test_describe_updates_the_description(self, h, inline):
        out = commands._playlist_describe(h.core, {"id": GYM, "description": " leg day "})
        assert out["description"] == "leg day"
        assert h.gym.description == "leg day" and h.gym.name == "Gym"
        assert h.gym.edits == [(None, "leg day")]
        assert h.core._toast == 'Changed the description of "Gym"'

    def test_a_playlist_without_edit_is_not_yours(self, h):
        h.core._editable_playlists = []
        h.session.playlists["other"] = types.SimpleNamespace(id="other", name="Theirs")
        assert _run(h, "playlist.rename", {"id": "other", "name": "Mine"})["code"] == "not_yours"


class TestFavorites:
    def test_favorite_album_prepends_the_known_album(self, h):
        old = fake_album("1", "Old")
        new = fake_album("2", "New")
        h.core._cache.put_items("favorites:albums", [old])
        h.core._known[("album", "2")] = new
        assert _run(h, "favorite.album", {"id": "2"})["ok"]
        assert h.session.user.favorites.albums == {"2"}
        assert h.session.requests == ["POST favorites/albums/2"]
        assert [str(i.id) for i in h.core._cache.get_items("favorites:albums")] == ["2", "1"]

    def test_unfavorite_artist_drops_the_row(self, h):
        a = types.SimpleNamespace(id="7", name="A")
        h.core._cache.put_items("favorites:artists", [a])
        h.session.user.favorites.artists.add("7")
        assert _run(h, "unfavorite.artist", {"id": "7"})["ok"]
        assert h.session.user.favorites.artists == set()
        assert h.session.requests == ["DELETE favorites/artists/7"]
        assert h.core._cache.get_items("favorites:artists") == []

    def test_favorite_playlist_records_the_id(self, h):
        assert _run(h, "favorite.playlist", {"id": GYM})["ok"]
        assert h.session.user.favorites.playlists == {GYM}
        assert h.session.requests == [f"POST favorites/playlists/{GYM}"]

    def test_missing_id_is_bad_args(self, h):
        assert _run(h, "favorite.album", {"id": " "})["code"] == "bad_args"
