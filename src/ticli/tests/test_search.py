"""Tests for search: page sizing, the type filters, paging on scroll, and
searching the user's own playlists out of the local index.

No TIDAL session or network anywhere: the session is a fake that records
every request it is given, and the cache directory is redirected to tmp_path.
"""

import json
import threading
import time
import types

import pytest

import ticli.player as player_mod
from ticli.player import HeadlessTidalPlayer
from ticli.utils import cache as cache_mod
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


def _fake_search_playlist(i):
    """A community playlist as session.search() hands one back — a plain
    tidalapi.Playlist, not a UserPlaylist, because it is not yours."""
    return types.SimpleNamespace(
        id=f"pl-{i}", name=f"Playlist {i}", num_tracks=30,
        creator=types.SimpleNamespace(name=f"Curator {i}"),
        tracks=lambda: [_fake_track(1)],
    )


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
            "playlists": [_fake_search_playlist(i) for i in range(n("playlists"))],
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
            assert sum(HeadlessTidalPlayer._search_split(page)) == page

    def test_tracks_are_the_majority(self, config_file):
        for page in (5, 15, 25, 40):
            tracks, albums, artists, playlists = HeadlessTidalPlayer._search_split(page)
            assert tracks > albums >= artists
            assert artists == playlists  # the two 15%s, and equal at every size

    def test_no_category_vanishes(self, config_file):
        """Even the 5-row minimum page must still show every category."""
        assert all(n >= 1 for n in HeadlessTidalPlayer._search_split(5))


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
        assert kinds == {"track", "album", "artist", "playlist"}

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
        p, _ = _search(page_size=15, caps={"albums": 1, "artists": 1, "playlists": 1})
        kinds = [item["type"] for item in p._search_results]
        assert len(p._search_results) == 15
        assert kinds.count("album") == 1
        assert kinds.count("artist") == 1
        assert kinds.count("playlist") == 1
        assert kinds.count("track") == 12

    def test_fewer_results_than_a_page_renders(self, config_file):
        p, _ = _search(page_size=20,
                       caps={"tracks": 2, "albums": 1, "artists": 0, "playlists": 0})
        assert len(p._search_results) == 3
        text = p._build_search_display().plain
        assert "Track 0" in text
        assert "Page" not in text  # a single short page needs no pager

    def test_no_results_at_all(self, config_file):
        p, _ = _search(page_size=15,
                       caps={"tracks": 0, "albums": 0, "artists": 0, "playlists": 0})
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
        assert _wait_for(lambda: p._artist_rows())
        assert seen["limit"] >= 40
        assert len(p._artist_rows()) >= 40

    def test_small_page_still_gets_a_useful_list(self, config_file):
        """Shrinking the page must not shrink what the artist page offers."""
        seen = {}

        def get_top_tracks(limit):
            seen["limit"] = limit
            return [_fake_track(i) for i in range(limit)]

        p = HeadlessTidalPlayer()
        p._page_size = 5
        p._open_artist(types.SimpleNamespace(name="X", get_top_tracks=get_top_tracks))
        assert _wait_for(lambda: p._artist_rows())
        assert seen["limit"] == 20


# ── type filters ──


def _filtered(filter_name, page_size=10, caps=None, query="miles davis"):
    p = HeadlessTidalPlayer()
    p._page_size = page_size
    p.session = _FakeSession(caps)
    p._search_filter = filter_name
    p._search_query = query
    p._do_search()
    assert _wait_for(lambda: not p._search_loading), "search never finished"
    return p


class TestTypeFilters:
    def test_tab_cycles_through_every_scope(self, config_file):
        p = HeadlessTidalPlayer()
        seen = [p._search_filter]
        for _ in range(len(HeadlessTidalPlayer.SEARCH_FILTERS) - 1):
            p._handle_search_key(player_mod.KEY_TAB)
            seen.append(p._search_filter)
        assert seen == list(HeadlessTidalPlayer.SEARCH_FILTERS)
        p._handle_search_key(player_mod.KEY_TAB)
        assert p._search_filter == "all"  # wraps

    def test_shift_tab_cycles_backwards(self, config_file):
        p = HeadlessTidalPlayer()
        p._handle_search_key(player_mod.KEY_SHIFT_TAB)
        assert p._search_filter == HeadlessTidalPlayer.SEARCH_FILTERS[-1]

    def test_tab_is_not_typed_into_the_query(self, config_file):
        p = HeadlessTidalPlayer()
        p.session = _FakeSession()
        p._search_query = "monk"
        p._handle_search_key(player_mod.KEY_TAB)
        assert p._search_query == "monk"

    def test_tab_applies_the_scope_without_a_second_keystroke(self, config_file):
        """Tab is the whole gesture: no Enter, and the rows are there when the
        key comes back up."""
        p, session = _search(page_size=10)
        p._handle_search_key(player_mod.KEY_TAB)
        assert p._search_filter == "tracks"
        assert {i["type"] for i in p._search_results} == {"track"}
        assert len(p._search_results) == 10

    def test_tab_does_not_fetch_by_itself(self, config_file):
        """Applying a scope instantly and never fanning out are the same
        requirement: one request buys the query, and every scope is served out
        of it."""
        p, session = _search(page_size=10)
        for _ in range(4):
            p._handle_search_key(player_mod.KEY_TAB)
        assert len(session.calls) == 1

    def test_one_request_covers_every_scope(self, config_file):
        """The measured cost of walking the whole scope row and back."""
        p, session = _search(page_size=10)
        assert len(session.calls) == 1
        for _ in range(len(HeadlessTidalPlayer.SEARCH_FILTERS) * 2):
            p._handle_search_key(player_mod.KEY_TAB)
        assert p._search_filter == "all"  # two whole laps
        assert len(session.calls) == 1

    def test_a_fetch_asks_for_every_category(self, config_file):
        """Why the laps are free: `limit` is per type, so one request at the
        page size brings back a page of each — the same single request a
        scoped search used to make, with the other scopes paid for."""
        import tidalapi
        p = _filtered("tracks", page_size=12)
        assert len(p.session.calls) == 1
        assert p.session.calls[0]["models"] == [tidalapi.Track, tidalapi.Album,
                                                tidalapi.Artist, tidalapi.Playlist]

    def test_tracks_filter_shows_only_tracks(self, config_file):
        p = _filtered("tracks", page_size=12)
        assert {i["type"] for i in p._search_results} == {"track"}
        assert len(p._search_results) == 12  # a whole page of one type

    def test_albums_filter_shows_only_albums(self, config_file):
        p = _filtered("albums", page_size=12)
        assert {i["type"] for i in p._search_results} == {"album"}
        assert len(p._search_results) == 12

    def test_artists_filter_shows_only_artists(self, config_file):
        p = _filtered("artists", page_size=12)
        assert {i["type"] for i in p._search_results} == {"artist"}
        assert len(p._search_results) == 12

    def test_all_filter_still_splits(self, config_file):
        p = _filtered("all", page_size=12)
        assert {i["type"] for i in p._search_results} == {
            "track", "album", "artist", "playlist"}
        assert len(p._search_results) == 12

    def test_filter_row_is_on_screen(self, config_file):
        p = HeadlessTidalPlayer()
        p._search_filter = "albums"
        text = p._build_search_display().plain
        for label in HeadlessTidalPlayer.SEARCH_FILTER_LABELS.values():
            assert label in text
        assert "[Tab]" in text

    def test_entering_search_starts_in_all(self, config_file):
        p = HeadlessTidalPlayer()
        p._search_filter = "playlists"
        p._handle_player_key("s")
        assert p._search_filter == "all"


# ── the session's scope cache ──


class TestScopeCache:
    """Tab applies a scope at once, and every scope of one query is answered
    out of one reservoir of rows kept for the session."""

    def test_revisiting_a_scope_costs_nothing_and_keeps_its_rows(self, config_file):
        p, session = _search(page_size=10)
        p._handle_search_key(player_mod.KEY_TAB)  # tracks
        first = [i["name"] for i in p._search_results]
        for _ in range(len(HeadlessTidalPlayer.SEARCH_FILTERS)):
            p._handle_search_key(player_mod.KEY_TAB)  # all the way round
        assert p._search_filter == "tracks"
        assert [i["name"] for i in p._search_results] == first
        assert len(session.calls) == 1

    def test_the_cursor_is_remembered_per_scope(self, config_file):
        """Coming back to a scope comes back to where you were in it; a scope
        you have never opened starts at the top."""
        p, _ = _search(page_size=10)
        p._search_cursor = 4
        p._handle_search_key(player_mod.KEY_TAB)  # tracks
        assert p._search_cursor == 0
        p._search_cursor = 7
        p._handle_search_key(player_mod.KEY_SHIFT_TAB)  # back to all
        assert p._search_cursor == 4
        p._handle_search_key(player_mod.KEY_TAB)
        assert p._search_cursor == 7

    def test_a_scope_round_trip_keeps_the_pages_already_fetched(self, config_file):
        """Depth survives the trip: five pages deep in Tracks is still five
        pages deep after a lap of the scope row, and costs nothing to get back."""
        p = _paged(filter_name="tracks", page_size=10)
        for expected in (20, 30, 40):
            p._search_last_fetch = 0
            _scroll_to_bottom(p)
            assert _wait_for(lambda e=expected: len(p._search_results) == e)
        deep, cursor = len(p._search_results), p._search_cursor
        calls = len(p.session.calls)
        for _ in range(len(HeadlessTidalPlayer.SEARCH_FILTERS)):
            p._handle_search_key(player_mod.KEY_TAB)
        assert p._search_filter == "tracks"
        assert len(p._search_results) == deep == 40
        assert p._search_cursor == cursor  # and where you were in them
        assert len(p.session.calls) == calls

    def test_paging_still_works_after_a_round_trip(self, config_file):
        p = _paged(filter_name="tracks", page_size=10)
        for _ in range(len(HeadlessTidalPlayer.SEARCH_FILTERS)):
            p._handle_search_key(player_mod.KEY_TAB)
        p._search_last_fetch = 0
        _scroll_to_bottom(p)
        assert _wait_for(lambda: len(p._search_results) == 20)
        assert p.session.calls[-1]["offset"] == 10

    def test_a_scope_deepened_by_one_pays_for_the_others_too(self, config_file):
        """A page fetched under Tracks lands in the reservoir, so Albums gets
        two pages out of it without a request of its own."""
        p = _paged(filter_name="tracks", page_size=10)
        p._search_last_fetch = 0
        _scroll_to_bottom(p)
        assert _wait_for(lambda: len(p._search_results) == 20)
        calls = len(p.session.calls)
        p._handle_search_key(player_mod.KEY_TAB)  # albums
        p._handle_search_key(player_mod.KEY_DOWN)
        _scroll_to_bottom(p)
        assert len(p._search_results) == 20
        assert len(p.session.calls) == calls

    def test_held_tab_from_cold_is_one_request(self, config_file):
        """Nothing fetched yet and the key held down: the first Tab starts the
        one request, and every scope behind it waits on the same one."""
        p = HeadlessTidalPlayer()
        p._page_size = 10
        p.session = _FakeSession()
        p._search_query = "miles davis"
        release = threading.Event()
        started = []
        real_search = p.session.search

        def slow_search(*a, **kw):
            started.append(1)  # counted on entry: the request is out by now
            release.wait(2.0)
            return real_search(*a, **kw)

        p.session.search = slow_search
        for _ in range(40):
            p._handle_search_key(player_mod.KEY_TAB)
        assert _wait_for(lambda: started)
        assert len(started) == 1
        assert p._search_loading or p._search_filter == "playlists"
        release.set()
        assert _wait_for(lambda: not p._search_fetching)
        assert len(started) == 1
        assert len(p.session.calls) == 1
        # every scope the key passed through is answered by that one page
        for scope in ("all", "tracks", "albums", "artists"):
            assert p._search_views[scope]["results"], scope

    def test_typing_drops_everything_filed_under_the_old_query(self, config_file):
        p, session = _search(page_size=10)
        p._handle_search_key(player_mod.KEY_TAB)
        assert p._search_views
        p._handle_search_key("x")
        assert p._search_views == {}
        assert p._search_results == []
        assert p._search_offset == 0
        assert len(session.calls) == 1  # typing never searches

    def test_a_new_query_is_not_answered_by_the_old_one(self, config_file):
        p, session = _search(page_size=10)
        p._handle_search_key(player_mod.KEY_BACKSPACE)
        p._handle_search_key(player_mod.KEY_TAB)  # tracks, on the new query
        assert _wait_for(lambda: not p._search_loading)
        assert len(session.calls) == 2
        assert session.calls[-1]["query"] == p._search_query.strip()

    def test_enter_is_the_refresh(self, config_file):
        """The cache is per session, so Enter has to be the way past it — and
        it clears every scope, not just the one on screen."""
        p, session = _search(page_size=10)
        p._handle_search_key(player_mod.KEY_TAB)
        assert len(session.calls) == 1
        p._do_search()
        assert _wait_for(lambda: not p._search_loading)
        assert len(session.calls) == 2
        assert set(p._search_views) == {"tracks"}  # the others start over too

    def test_going_back_from_a_result_asks_for_nothing(self, config_file):
        p, session = _search(page_size=10)
        p._mode = p.MODE_SEARCH
        p._handle_search_key(player_mod.KEY_TAB)  # tracks
        p._search_cursor = 3
        p._push_nav()
        p._mode = p.MODE_BROWSE
        p._go_back()
        assert p._mode == p.MODE_SEARCH
        assert p._search_filter == "tracks"
        assert p._search_cursor == 3
        assert len(p._search_results) == 10
        assert len(session.calls) == 1

    def test_an_empty_category_is_not_asked_for_twice(self, config_file):
        """A scope TIDAL had nothing for says so, and stays said."""
        p, session = _search(page_size=10, caps={"albums": 0})
        p._handle_search_key(player_mod.KEY_TAB)
        p._handle_search_key(player_mod.KEY_TAB)  # albums
        assert p._search_filter == "albums"
        assert p._search_results == []
        assert p._search_message == "No results found"
        assert len(session.calls) == 1

    def test_a_failed_query_is_reported_on_every_scope(self, config_file):
        p = HeadlessTidalPlayer()
        p._page_size = 10

        def boom(*a, **kw):
            raise RuntimeError("network down")

        p.session = types.SimpleNamespace(search=boom, audio_quality=None)
        p._search_query = "x"
        p._do_search()
        assert _wait_for(lambda: not p._search_loading)
        assert "Search failed" in p._search_message
        p._handle_search_key(player_mod.KEY_TAB)
        assert "Search failed" in p._search_message  # and no second attempt

    def test_loading_empty_and_cached_read_differently(self, config_file):
        p, _ = _search(page_size=10)
        p._put_search_view("all", loading=True, results=[])
        assert "Searching..." in p._build_search_display().plain
        p._put_search_view("all", loading=False, results=[], message="No results found")
        assert "No results found" in p._build_search_display().plain
        assert "Searching..." not in p._build_search_display().plain
        p._handle_search_key(player_mod.KEY_TAB)  # tracks, straight from the pool
        text = p._build_search_display().plain
        assert "Already loaded this session" in text
        assert "Searching..." not in text

    def test_a_scope_paying_for_its_own_page_does_not_claim_to_be_free(self, config_file):
        p = _paged(filter_name="tracks", page_size=10)
        assert "Already loaded this session" not in p._build_search_display().plain

    def test_my_playlists_costs_nothing_and_keeps_its_messages(self, config_file, cache_dir):
        """The local scan is reached by Tab like any other scope, and the two
        things it has to say about an index it cannot use still get said."""
        _stocked(cache_dir)
        p = HeadlessTidalPlayer()
        p.session = _FakeSession()
        p._search_query = "so what"
        for _ in range(5):
            p._handle_search_key(player_mod.KEY_TAB)
        assert p._search_filter == "playlists"
        assert [r["name"] for r in p._search_results] == ["So What"]
        # One request for the four network scopes, and none for this one
        before = len(p.session.calls)
        p._handle_search_key(player_mod.KEY_TAB)
        p._handle_search_key(player_mod.KEY_SHIFT_TAB)
        assert len(p.session.calls) == before

    def test_tabbing_onto_an_empty_index_still_says_so(self, config_file, cache_dir):
        p = HeadlessTidalPlayer()
        p.session = _FakeSession()
        p._search_query = "anything"
        p._search_filter = "tidal_playlists"
        p._handle_search_key(player_mod.KEY_TAB)
        assert p._search_filter == "playlists"
        assert "Nothing cached" in p._search_message

    def test_tabbing_onto_a_disabled_cache_still_says_so(self, config_file, cache_dir):
        _stocked(cache_dir)
        save_config({**config_mod.DEFAULTS, "cache_metadata": False})
        p = HeadlessTidalPlayer()
        p.session = _FakeSession()
        p._search_query = "so what"
        p._search_filter = "tidal_playlists"
        p._handle_search_key(player_mod.KEY_TAB)
        assert "metadata cache" in p._search_message


# ── paging on scroll ──


class _PagedSession:
    """A session with a finite catalogue behind the query, so offsets matter."""

    def __init__(self, totals=None):
        self.calls = []
        self.totals = totals or {"tracks": 200, "albums": 200,
                                 "artists": 200, "playlists": 200}
        self.audio_quality = None

    def search(self, query, models=None, limit=50, offset=0):
        self.calls.append({"query": query, "models": models, "limit": limit, "offset": offset})
        def rows(kind, make):
            end = min(offset + limit, self.totals.get(kind, 0))
            return [make(i) for i in range(min(offset, end), end)]
        return {
            "tracks": rows("tracks", _fake_track),
            "albums": rows("albums", _fake_album),
            "artists": rows("artists", _fake_artist),
            "playlists": rows("playlists", _fake_search_playlist),
        }


def _paged(filter_name="tracks", page_size=10, totals=None):
    p = HeadlessTidalPlayer()
    p._page_size = page_size
    p.session = _PagedSession(totals)
    p._search_filter = filter_name
    p._search_query = "miles davis"
    p._do_search()
    assert _wait_for(lambda: not p._search_loading), "search never finished"
    return p


def _scroll_to_bottom(p):
    for _ in range(len(p._search_results) + 1):
        p._handle_search_key(player_mod.KEY_DOWN)


class TestFetchMoreOnScroll:
    def test_down_at_the_bottom_asks_for_the_next_page(self, config_file):
        p = _paged(page_size=10)
        assert len(p._search_results) == 10
        p._search_last_fetch = 0  # the gate is time, not this test's subject
        _scroll_to_bottom(p)
        assert _wait_for(lambda: len(p._search_results) > 10)
        assert p.session.calls[-1]["offset"] == 10
        assert len(p._search_results) == 20

    def test_results_are_appended_not_replaced(self, config_file):
        p = _paged(page_size=10)
        first = [i["name"] for i in p._search_results]
        p._search_last_fetch = 0
        _scroll_to_bottom(p)
        assert _wait_for(lambda: len(p._search_results) > 10)
        assert [i["name"] for i in p._search_results][:10] == first

    def test_cursor_stays_put_across_the_append(self, config_file):
        p = _paged(page_size=10)
        for _ in range(9):
            p._handle_search_key(player_mod.KEY_DOWN)
        assert p._search_cursor == 9  # the last row
        p._search_last_fetch = 0
        p._handle_search_key(player_mod.KEY_DOWN)  # one press: fetch, no move
        assert p._search_cursor == 9
        assert _wait_for(lambda: len(p._search_results) > 10)
        assert p._search_cursor == 9
        p._handle_search_key(player_mod.KEY_DOWN)
        assert p._search_cursor == 10  # and the next press walks into the new rows

    def test_surplus_rows_are_served_without_a_request(self, config_file):
        """"All" fetches a page of each category and shows a page in total, so
        the next screen or two is already in hand — scrolling must spend that
        before it asks TIDAL for anything."""
        p = _paged(filter_name="all", page_size=10)
        assert len(p.session.calls) == 1
        _scroll_to_bottom(p)
        # Page two came entirely out of rows already in hand. It is 8 rows, not
        # 10: one fetch brings 10 of each category and an "All" page spends 6
        # of its 10 on tracks, so tracks are the first thing to run short and
        # the shortfall is not padded out of the other categories.
        assert len(p._search_results) == 18
        assert len(p.session.calls) == 1  # no network for page two

    def test_end_of_results_stops_fetching(self, config_file):
        p = _paged(page_size=10, totals={"tracks": 14, "albums": 0, "artists": 0})
        assert len(p._search_results) == 10
        p._search_last_fetch = 0
        _scroll_to_bottom(p)
        assert _wait_for(lambda: len(p._search_results) == 14)
        assert p._search_done
        calls = len(p.session.calls)
        for _ in range(30):
            p._handle_search_key(player_mod.KEY_DOWN)
        assert len(p.session.calls) == calls  # never again, however hard you scroll
        assert p._search_cursor == 13

    def test_short_first_page_never_asks_for_a_second(self, config_file):
        p = _paged(page_size=10, totals={"tracks": 4, "albums": 0, "artists": 0})
        p._search_last_fetch = 0
        for _ in range(20):
            p._handle_search_key(player_mod.KEY_DOWN)
        assert len(p.session.calls) == 1
        assert p._search_done

    def test_held_down_arrow_does_not_fan_out(self, config_file):
        """A burst of repeats while a fetch is in flight is one fetch, not a
        request per key event — the owner's IP depends on it."""
        p = _paged(page_size=10)
        release = threading.Event()
        real_search = p.session.search

        def slow_search(*a, **kw):
            release.wait(2.0)
            return real_search(*a, **kw)

        p.session.search = slow_search
        p._search_last_fetch = 0
        for _ in range(60):  # ~a second of key repeat held at the bottom
            p._handle_search_key(player_mod.KEY_DOWN)
        assert p._search_fetching
        assert len(p.session.calls) == 1  # the first page only; the second is blocked
        release.set()
        assert _wait_for(lambda: not p._search_fetching)
        assert len(p.session.calls) == 2

    def test_back_to_back_fetches_are_rate_gated(self, config_file):
        """Even with nothing in flight, two network pages can't land inside the
        minimum interval."""
        p = _paged(page_size=10)
        _scroll_to_bottom(p)  # _do_search stamped the clock a moment ago
        time.sleep(0.05)
        assert len(p.session.calls) == 1

    def test_the_last_page_says_it_is_the_last(self, config_file):
        p = _paged(page_size=10, totals={"tracks": 14, "albums": 0, "artists": 0})
        p._search_last_fetch = 0
        _scroll_to_bottom(p)
        assert _wait_for(lambda: len(p._search_results) == 14)
        assert "end of results" in p._build_search_display().plain
        p._search_cursor = 0  # page one, where the list is still going
        assert "end of results" not in p._build_search_display().plain

    def test_a_fetch_in_flight_is_visible(self, config_file):
        p = _paged(page_size=10)
        p._put_search_view(p._search_filter, loading=True)
        assert "Loading more" in p._build_search_display().plain

    def test_the_offset_ceiling_is_respected(self, config_file):
        p = _paged(page_size=10)
        p._search_offset = player_mod.SEARCH_MAX_OFFSET
        p._search_last_fetch = 0
        _scroll_to_bottom(p)
        assert len(p.session.calls) == 1  # TIDAL has nothing past 300
        assert p._search_done


# ── searching your own playlists ──


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Redirect the metadata cache away from the real one."""
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    return tmp_path / "cache"


def _index(cache_dir, playlists, tracks_by_playlist):
    """Write an index the way the cache itself would have."""
    now = time.time()
    entries = {"playlists": {"fetched": now, "used": now, "data": playlists}}
    for pid, records in tracks_by_playlist.items():
        entries[f"playlist:{pid}"] = {"fetched": now, "used": now, "data": records}
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_mod.index_file().write_text(json.dumps({"version": cache_mod.CACHE_VERSION,
                                                  "entries": entries}))


def _rec(tid, name, artist, album="Kind of Blue"):
    return {"id": tid, "name": name, "duration": 200, "artists": [artist], "album": album}


def _stocked(cache_dir):
    _index(cache_dir, [
        {"id": "p1", "name": "Jazz", "num_tracks": 2, "creator": "Garrett", "editable": True},
        {"id": "p2", "name": "Late Night", "num_tracks": 1, "creator": "Garrett", "editable": True},
    ], {
        "p1": [_rec(1, "So What", "Miles Davis"), _rec(2, "Blue in Green", "Miles Davis")],
        "p2": [_rec(3, "Naima", "John Coltrane", album="Giant Steps")],
    })


def _local_search(p, query):
    p._search_filter = "playlists"
    p._search_query = query
    p._do_search()
    return p._search_results


class TestPlaylistSearch:
    def test_matches_are_found_without_any_request(self, config_file, cache_dir):
        _stocked(cache_dir)
        p = HeadlessTidalPlayer()
        p.session = _FakeSession()
        results = _local_search(p, "so what")
        assert [r["name"] for r in results] == ["So What"]
        assert p.session.calls == []  # local index only, never the network

    def test_results_say_which_playlist_they_came_from(self, config_file, cache_dir):
        _stocked(cache_dir)
        p = HeadlessTidalPlayer()
        results = _local_search(p, "naima")
        assert results[0]["playlist"] == "Late Night"
        p._search_cursor = 0
        assert "in Late Night" in p._build_search_display().plain

    def test_matching_is_case_insensitive_and_substring(self, config_file, cache_dir):
        _stocked(cache_dir)
        p = HeadlessTidalPlayer()
        assert len(_local_search(p, "BLUE IN")) == 1

    def test_artist_and_album_match_too_but_titles_lead(self, config_file, cache_dir):
        _index(cache_dir, [{"id": "p1", "name": "Jazz", "num_tracks": 2}], {
            "p1": [_rec(1, "Naima", "Miles Davis"), _rec(2, "Miles Ahead", "Bill Evans")],
        })
        p = HeadlessTidalPlayer()
        results = _local_search(p, "miles")
        assert [r["name"] for r in results] == ["Miles Ahead", "Naima"]

    def test_the_playlist_name_finds_its_tracks(self, config_file, cache_dir):
        """Typing a playlist's own name surfaces what is in it — the whole
        point of the field being indexed as plain text."""
        _stocked(cache_dir)
        p = HeadlessTidalPlayer()
        p.session = _FakeSession()
        results = _local_search(p, "late night")
        assert [r["name"] for r in results] == ["Naima"]
        assert p.session.calls == []  # still local, still free

    def test_the_playlist_name_matches_case_insensitively_too(self, config_file, cache_dir):
        _stocked(cache_dir)
        p = HeadlessTidalPlayer()
        assert len(_local_search(p, "JAZZ")) == 2  # both tracks in "Jazz"

    def test_every_field_has_its_place_in_the_order(self, config_file, cache_dir):
        """Title, then artist, then album, then playlist name. The playlist
        name is one string standing in for every track under it, so it ranks
        last: a common word must not bury the track actually called that."""
        _index(cache_dir, [{"id": "p1", "name": "Blue Mood", "num_tracks": 4}], {
            "p1": [
                _rec(1, "Naima", "John Coltrane", album="Giant Steps"),
                _rec(2, "Kind of Blue", "Miles Davis", album="Milestones"),
                _rec(3, "Solar", "Blue Mitchell", album="Milestones"),
                _rec(4, "Freddie Freeloader", "Miles Davis", album="Blue Train"),
            ],
        })
        p = HeadlessTidalPlayer()
        results = _local_search(p, "blue")
        assert [r["name"] for r in results] == [
            "Kind of Blue",        # title
            "Solar",               # artist
            "Freddie Freeloader",  # album
            "Naima",               # only the playlist it is in is called that
        ]

    def test_a_playlist_name_match_still_says_where_it_came_from(self, config_file, cache_dir):
        _stocked(cache_dir)
        p = HeadlessTidalPlayer()
        results = _local_search(p, "late")
        assert results[0]["playlist"] == "Late Night"

    def test_other_playlists_are_not_dragged_in(self, config_file, cache_dir):
        """A playlist name matches its own tracks and nobody else's."""
        _stocked(cache_dir)
        p = HeadlessTidalPlayer()
        results = _local_search(p, "night")
        assert {r["playlist"] for r in results} == {"Late Night"}

    def test_hits_resolve_to_a_real_track_before_playing(self, config_file, cache_dir):
        _stocked(cache_dir)
        p = HeadlessTidalPlayer()
        real = _fake_track(1)
        asked = []

        def track(tid):
            asked.append(tid)
            return real

        p.session = types.SimpleNamespace(search=None, track=track, audio_quality=None)
        played = []
        p._play_track = lambda t, seek=0: played.append(t)
        _local_search(p, "so what")
        p._search_cursor = 0
        p._select_search_result()
        # The row itself is a cached record; playback resolves it through the
        # session (_play_track does that on its own thread, so check the seam)
        assert played and getattr(played[0], "cached", False)
        assert p._resolve_track(played[0]) is real
        assert asked == [1]

    def test_no_match_says_so(self, config_file, cache_dir):
        _stocked(cache_dir)
        p = HeadlessTidalPlayer()
        assert _local_search(p, "stockhausen") == []
        assert p._search_message == "No results in your playlists"

    def test_empty_cache_says_so(self, config_file, cache_dir):
        p = HeadlessTidalPlayer()
        assert _local_search(p, "anything") == []
        assert "Nothing cached" in p._search_message

    def test_caching_disabled_says_so(self, config_file, cache_dir):
        _stocked(cache_dir)
        save_config({**config_mod.DEFAULTS, "cache_metadata": False})
        p = HeadlessTidalPlayer()
        assert _local_search(p, "so what") == []
        assert "metadata cache" in p._search_message
        assert "settings" in p._search_message

    def test_scrolling_a_local_result_list_never_fetches(self, config_file, cache_dir):
        _stocked(cache_dir)
        p = HeadlessTidalPlayer()
        p.session = _FakeSession()
        _local_search(p, "miles")
        for _ in range(20):
            p._handle_search_key(player_mod.KEY_DOWN)
        assert p.session.calls == []


# ── the community (TIDAL) playlists scope ──


class TestCommunityPlaylistScope:
    """A fifth server-side scope over the same one request. "Playlists" is
    TIDAL's; "My Playlists" is still the local index, still last, still free."""

    def test_both_playlist_scopes_are_on_the_scope_row_and_distinct(self, config_file):
        p = HeadlessTidalPlayer()
        p._search_filter = "tidal_playlists"
        text = p._build_search_display().plain
        assert "Playlists" in text and "My Playlists" in text
        assert text.count("Playlists") == 2  # two rows, two different things
        labels = list(HeadlessTidalPlayer.SEARCH_FILTER_LABELS.values())
        assert labels[-1] == "My Playlists"      # the local one stays last
        assert labels[-2] == "Playlists"         # the new one joins the network scopes

    def test_tab_reaches_it_before_my_playlists(self, config_file):
        p, _ = _search(page_size=10)
        for _ in range(4):
            p._handle_search_key(player_mod.KEY_TAB)
        assert p._search_filter == "tidal_playlists"
        p._handle_search_key(player_mod.KEY_TAB)
        assert p._search_filter == "playlists"

    def test_the_scope_shows_only_playlists(self, config_file):
        p = _filtered("tidal_playlists", page_size=12)
        assert {i["type"] for i in p._search_results} == {"playlist"}
        assert len(p._search_results) == 12

    def test_rows_say_whose_playlist_it_is(self, config_file):
        p = _filtered("tidal_playlists", page_size=10)
        assert p._search_results[0]["artist"] == "Curator 0"
        text = p._build_search_display().plain
        assert "[PLAYLIST] Playlist 0" in text
        assert "Curator 0" in text

    def test_all_scope_includes_playlists(self, config_file):
        p = _filtered("all", page_size=20)
        kinds = [i["type"] for i in p._search_results]
        assert kinds.count("playlist") == 3      # 15% of 20
        assert kinds.count("track") == 9         # tracks still the plurality

    def test_cycling_every_scope_costs_one_request(self, config_file, cache_dir):
        """Six scopes now — five over one reservoir and one over the local
        index — and a whole lap still buys nothing."""
        p = HeadlessTidalPlayer()
        p.session = _FakeSession()
        p._search_query = "miles davis"
        p._do_search()
        assert _wait_for(lambda: not p._search_loading)
        assert len(p.session.calls) == 1
        for _ in range(len(HeadlessTidalPlayer.SEARCH_FILTERS) * 2):
            p._handle_search_key(player_mod.KEY_TAB)
        assert p._search_filter == "all"
        assert len(p.session.calls) == 1

    def test_a_held_down_tab_is_still_one_request(self, config_file, cache_dir):
        p = HeadlessTidalPlayer()
        p.session = _FakeSession()
        p._search_query = "miles davis"
        release = threading.Event()
        started = []
        real_search = p.session.search

        def slow_search(*a, **kw):
            started.append(1)
            release.wait(2.0)
            return real_search(*a, **kw)

        p.session.search = slow_search
        for _ in range(40):
            p._handle_search_key(player_mod.KEY_TAB)
        assert _wait_for(lambda: started)
        release.set()
        assert _wait_for(lambda: not p._search_fetching)
        assert len(started) == 1
        assert p._search_views["tidal_playlists"]["results"]

    def test_paging_on_scroll_spends_the_surplus_then_asks(self, config_file):
        p = _paged(filter_name="tidal_playlists", page_size=10)
        assert len(p._search_results) == 10
        p._search_last_fetch = 0
        _scroll_to_bottom(p)
        assert _wait_for(lambda: len(p._search_results) > 10)
        assert p.session.calls[-1]["offset"] == 10
        assert len(p._search_results) == 20
        assert {i["type"] for i in p._search_results} == {"playlist"}


class TestOpeningACommunityPlaylist:
    def _opened(self):
        p = _paged(filter_name="tidal_playlists", page_size=10)
        p._mode = p.MODE_SEARCH
        p._search_cursor = 1
        p._select_search_result()
        return p

    def test_it_opens_through_browse(self, config_file, cache_dir):
        p = self._opened()
        assert p._mode == p.MODE_BROWSE
        assert p._browse_title == "Playlist 1"
        assert _wait_for(lambda: len(p._browse_tracks) == 1)

    def test_it_is_not_editable_so_x_removes_nothing(self, config_file, cache_dir):
        """A community playlist is not yours. The removal gate is 'is this a
        UserPlaylist', and a searched-for one never is."""
        p = self._opened()
        assert _wait_for(lambda: len(p._browse_tracks) == 1)
        assert p._browse_playlist is None
        p._browse_cursor = 0
        p._remove_from_browse_playlist()
        time.sleep(0.1)
        assert len(p._browse_tracks) == 1

    def test_going_back_returns_to_the_same_scope_for_free(self, config_file, cache_dir):
        p = self._opened()
        assert _wait_for(lambda: len(p._browse_tracks) == 1)
        before = len(p.session.calls)
        rows_before = [r["name"] for r in p._search_views["tidal_playlists"]["results"]]
        p._go_back()
        assert p._mode == p.MODE_SEARCH
        assert p._search_filter == "tidal_playlists"
        assert [r["name"] for r in p._search_results] == rows_before
        assert p._search_cursor == 1
        assert len(p.session.calls) == before  # TIDAL asked for nothing


class TestSearchHistoryRecall:
    """The blank search pane shows recent queries, ↓ walks into them, and the
    first printable key replaces them with a live query."""

    def _blank(self, history=("miles davis", "coltrane", "monk"), page_size=10):
        """A player sitting on the empty search screen with history in hand."""
        p = HeadlessTidalPlayer()
        p._page_size = page_size
        p.session = _FakeSession()
        p._search_history = list(history)
        p._mode = p.MODE_SEARCH
        return p

    def test_empty_query_shows_recent_searches(self, config_file):
        p = self._blank()
        text = p._build_search_display().plain
        assert "Recent searches" in text
        assert "miles davis" in text
        assert "coltrane" in text

    def test_no_history_leaves_the_pane_blank(self, config_file):
        p = self._blank(history=())
        assert "Recent searches" not in p._build_search_display().plain

    def test_a_live_query_hides_the_list(self, config_file):
        p = self._blank()
        p._handle_search_key("x")
        text = p._build_search_display().plain
        assert "Recent searches" not in text
        assert "Press Enter to search" in text
        # ...and backspacing back to empty brings it back, focus in the box
        p._handle_search_key(player_mod.KEY_BACKSPACE)
        assert "Recent searches" in p._build_search_display().plain
        assert p._search_history_cursor is None

    def test_the_window_follows_the_cursor_through_a_long_history(self, config_file):
        p = self._blank(history=[f"query {i}" for i in range(15)], page_size=10)
        text = p._build_search_display().plain
        assert "query 9" in text
        assert "query 10" not in text  # the first page, until the cursor leaves it
        for _ in range(11):
            p._handle_search_key(player_mod.KEY_DOWN)
        assert p._search_history_cursor == 10  # past the page: the display turns it
        text = p._build_search_display().plain
        assert "query 10" in text
        assert "query 9" not in text
        for _ in range(20):
            p._handle_search_key(player_mod.KEY_DOWN)
        assert p._search_history_cursor == 14  # the cursor stops where the list does

    def test_history_keeps_two_hundred_searches(self, config_file):
        p = self._blank(history=())
        for i in range(205):
            p._add_to_history(f"query {i}")
        assert len(p._search_history) == 200
        assert p._search_history[0] == "query 204"  # newest first, oldest aged out
        assert "query 4" not in p._search_history

    def test_down_enters_the_list_and_clamps_at_the_end(self, config_file):
        p = self._blank()
        assert p._search_history_cursor is None
        p._handle_search_key(player_mod.KEY_DOWN)
        assert p._search_history_cursor == 0
        for _ in range(5):
            p._handle_search_key(player_mod.KEY_DOWN)
        assert p._search_history_cursor == 2

    def test_up_from_row_zero_returns_to_the_query_box_then_the_player(self, config_file):
        p = self._blank()
        p._current_track = _fake_track(1)  # _focus_player is a no-op without one
        p._handle_search_key(player_mod.KEY_DOWN)
        p._handle_search_key(player_mod.KEY_DOWN)
        p._handle_search_key(player_mod.KEY_UP)
        assert p._search_history_cursor == 0
        p._handle_search_key(player_mod.KEY_UP)
        assert p._search_history_cursor is None
        assert not p._player_focus  # the box, not the player, gets this press
        p._handle_search_key(player_mod.KEY_UP)
        assert p._player_focus  # the existing hand-off, one press later

    def test_enter_on_a_row_runs_the_search(self, config_file):
        p = self._blank()
        p._handle_search_key(player_mod.KEY_DOWN)
        p._handle_search_key(player_mod.KEY_DOWN)  # coltrane
        p._handle_search_key(player_mod.KEY_ENTER)
        assert p._search_query == "coltrane"
        assert p._search_history_cursor is None
        assert _wait_for(lambda: not p._search_loading)
        assert p.session.calls[0]["query"] == "coltrane"
        assert p._search_results
        assert p._search_history[0] == "coltrane"  # re-running bumps it up

    def test_enter_with_an_empty_query_still_does_nothing(self, config_file):
        p = self._blank()
        p._handle_search_key(player_mod.KEY_ENTER)  # cursor unset: query box
        assert p.session.calls == []
        assert p._search_history == ["miles davis", "coltrane", "monk"]

    def test_backspace_deletes_the_row_under_the_cursor(self, config_file):
        p = self._blank()
        p._handle_search_key(player_mod.KEY_DOWN)
        p._handle_search_key(player_mod.KEY_DOWN)  # coltrane
        p._handle_search_key(player_mod.KEY_BACKSPACE)
        assert p._search_history == ["miles davis", "monk"]
        assert p._search_history_cursor == 1  # the row that slid up
        p._handle_search_key(player_mod.KEY_BACKSPACE)  # monk, the last row
        assert p._search_history == ["miles davis"]
        assert p._search_history_cursor == 0  # clamped, not past the end

    def test_deleting_the_last_entry_returns_focus_to_the_query_box(self, config_file):
        p = self._blank(history=("miles davis",))
        p._handle_search_key(player_mod.KEY_DOWN)
        p._handle_search_key(player_mod.KEY_BACKSPACE)
        assert p._search_history == []
        assert p._search_history_cursor is None
        assert "Recent searches" not in p._build_search_display().plain
        # ...and with focus back in the box, Backspace edits the query again
        p._handle_search_key("x")
        p._handle_search_key(player_mod.KEY_BACKSPACE)
        assert p._search_query == ""

    def test_typing_dismisses_the_list_and_starts_a_live_query(self, config_file):
        p = self._blank()
        p._handle_search_key(player_mod.KEY_DOWN)
        assert p._search_history_cursor == 0
        p._handle_search_key("m")
        assert p._search_history_cursor is None
        assert p._search_query == "m"
        assert "Recent searches" not in p._build_search_display().plain
        assert p.session.calls == []  # typing never searches
