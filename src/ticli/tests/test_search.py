"""Tests for search sizing — the "Songs per page" setting has to reach search
and the browse lists it opens, not just the queue and playlist pages.

No TIDAL session or network: the session is a fake that records the single
request it is given.
"""

import json
import time
import types

import pytest

from ticli.player import HeadlessTidalPlayer
from ticli.utils import config as config_mod
from ticli.utils.config import save_config


def _fake_track(i):
    return types.SimpleNamespace(
        id=i, name=f"Track {i}", duration=200,
        artists=[types.SimpleNamespace(name=f"Artist {i}")],
    )


def _fake_album(i):
    return types.SimpleNamespace(id=i, name=f"Album {i}", artist=types.SimpleNamespace(name=f"Artist {i}"))


def _fake_artist(i):
    return types.SimpleNamespace(id=i, name=f"Artist {i}")


class _FakeSession:
    """Records every search call. Returns as many of each model as asked for,
    unless a cap says this category is thinner than that."""

    def __init__(self, caps=None):
        self.calls = []
        self.caps = caps or {}
        self.audio_quality = None
        self.is_pkce = False

    def search(self, query, models=None, limit=50, offset=0):
        self.calls.append({"query": query, "models": models, "limit": limit})
        def n(kind):
            return min(limit, self.caps.get(kind, limit))
        return {
            "tracks": [_fake_track(i) for i in range(n("tracks"))],
            "albums": [_fake_album(i) for i in range(n("albums"))],
            "artists": [_fake_artist(i) for i in range(n("artists"))],
        }


def _wait_for(cond, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Point the config module at a throwaway directory."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", path)
    return path


def _search(page_size=None, caps=None, query="miles davis"):
    """Run one search to completion and hand back the player and its session."""
    p = HeadlessTidalPlayer()
    if page_size is not None:
        p._page_size = page_size
    p.session = _FakeSession(caps)
    p._search_query = query
    p._do_search()
    assert _wait_for(lambda: not p._search_loading), "search never finished"
    return p, p.session


class TestSearchSplit:
    def test_split_fills_exactly_one_page(self, config_file):
        for page in range(5, 41):
            tracks, albums, artists = HeadlessTidalPlayer._search_split(page)
            assert tracks + albums + artists == page

    def test_tracks_are_the_majority(self, config_file):
        for page in (5, 15, 25, 40):
            tracks, albums, artists = HeadlessTidalPlayer._search_split(page)
            assert tracks > albums >= artists

    def test_albums_and_artists_never_vanish(self, config_file):
        """Even the 5-row minimum page must still show every category."""
        tracks, albums, artists = HeadlessTidalPlayer._search_split(5)
        assert albums >= 1 and artists >= 1 and tracks >= 1


class TestSearchHonoursPageSize:
    def test_one_request_at_the_page_size(self, config_file):
        p, session = _search(page_size=20)
        assert len(session.calls) == 1  # not one per model
        assert session.calls[0]["limit"] == 20

    def test_results_fill_a_page(self, config_file):
        p, _ = _search(page_size=20)
        assert len(p._search_results) == 20

    def test_bigger_page_means_more_results(self, config_file):
        small, _ = _search(page_size=5)
        big, _ = _search(page_size=40)
        assert len(big._search_results) == 40
        assert len(small._search_results) == 5

    def test_every_category_is_present(self, config_file):
        p, _ = _search(page_size=15)
        kinds = {item["type"] for item in p._search_results}
        assert kinds == {"track", "album", "artist"}

    def test_tracks_come_first(self, config_file):
        p, _ = _search(page_size=15)
        kinds = [item["type"] for item in p._search_results]
        assert kinds[0] == "track"
        assert kinds.index("album") < kinds.index("artist")

    def test_page_size_from_config_is_used(self, config_file):
        save_config({**config_mod.DEFAULTS, "page_size": 12})
        p = HeadlessTidalPlayer()
        p.session = _FakeSession()
        p._search_query = "coltrane"
        p._do_search()
        assert _wait_for(lambda: not p._search_loading)
        assert p.session.calls[0]["limit"] == 12
        assert len(p._search_results) == 12


class TestShortResults:
    def test_thin_categories_give_their_rows_to_tracks(self, config_file):
        """One matching album shouldn't leave three empty album rows."""
        p, _ = _search(page_size=15, caps={"albums": 1, "artists": 1})
        kinds = [item["type"] for item in p._search_results]
        assert len(p._search_results) == 15
        assert kinds.count("album") == 1
        assert kinds.count("artist") == 1
        assert kinds.count("track") == 13

    def test_fewer_results_than_a_page_renders(self, config_file):
        p, _ = _search(page_size=20, caps={"tracks": 2, "albums": 1, "artists": 0})
        assert len(p._search_results) == 3
        text = p._build_search_display().plain
        assert "Track 0" in text
        assert "Page" not in text  # a single short page needs no pager

    def test_no_results_at_all(self, config_file):
        p, _ = _search(page_size=15, caps={"tracks": 0, "albums": 0, "artists": 0})
        assert p._search_results == []
        assert p._search_message == "No results found"

    def test_failure_is_reported_not_raised(self, config_file):
        p = HeadlessTidalPlayer()
        def boom(*a, **kw):
            raise RuntimeError("network down")
        p.session = types.SimpleNamespace(search=boom, audio_quality=None)
        p._search_query = "x"
        p._do_search()
        assert _wait_for(lambda: not p._search_loading)
        assert "Search failed" in p._search_message


class TestPageSizeChangesTakeEffectNextSearch:
    def test_existing_results_are_not_refetched(self, config_file):
        p, session = _search(page_size=10)
        assert len(p._search_results) == 10
        p._page_size = 30  # user opens settings mid-results
        assert len(session.calls) == 1  # no background refetch
        assert len(p._search_results) == 10  # already-fetched page is left alone

    def test_next_search_uses_the_new_size(self, config_file):
        p, session = _search(page_size=10)
        p._page_size = 30
        p._search_query = "monk"
        p._do_search()
        assert _wait_for(lambda: not p._search_loading)
        assert session.calls[-1]["limit"] == 30
        assert len(p._search_results) == 30

    def test_setting_edit_reaches_search(self, config_file):
        """End to end: change the setting on the settings page, then search."""
        import ticli.player as player_mod

        p = HeadlessTidalPlayer()
        p._mode = p.MODE_SETTINGS
        p._settings_cursor = 1  # page size row
        p._handle_settings_key(player_mod.KEY_RIGHT)
        expected = json.loads(config_file.read_text())["page_size"]
        p.session = _FakeSession()
        p._search_query = "bill evans"
        p._do_search()
        assert _wait_for(lambda: not p._search_loading)
        assert p.session.calls[0]["limit"] == expected


class TestBrowseListsHonourPageSize:
    def test_artist_top_tracks_cover_a_full_page(self, config_file):
        seen = {}

        def get_top_tracks(limit):
            seen["limit"] = limit
            return [_fake_track(i) for i in range(limit)]

        p = HeadlessTidalPlayer()
        p._page_size = 40
        artist = types.SimpleNamespace(name="Miles Davis", get_top_tracks=get_top_tracks)
        p._open_artist(artist)
        assert _wait_for(lambda: not p._browse_loading)
        assert seen["limit"] >= 40
        assert len(p._browse_tracks) >= 40

    def test_small_page_still_gets_a_useful_list(self, config_file):
        """Shrinking the page must not shrink what the artist page offers."""
        seen = {}

        def get_top_tracks(limit):
            seen["limit"] = limit
            return [_fake_track(i) for i in range(limit)]

        p = HeadlessTidalPlayer()
        p._page_size = 5
        p._open_artist(types.SimpleNamespace(name="X", get_top_tracks=get_top_tracks))
        assert _wait_for(lambda: not p._browse_loading)
        assert seen["limit"] == 20
