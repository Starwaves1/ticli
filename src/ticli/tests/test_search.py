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
        p._search_query = "monk"
        p._handle_search_key(player_mod.KEY_TAB)
        assert p._search_query == "monk"

    def test_tab_does_not_fetch_by_itself(self, config_file):
        """Changing scope is a key you press repeatedly — Enter is what costs
        a request, exactly as it does after typing."""
        p, session = _search(page_size=10)
        for _ in range(4):
            p._handle_search_key(player_mod.KEY_TAB)
        assert len(session.calls) == 1
        assert p._search_results == []

    def test_tracks_filter_asks_only_for_tracks(self, config_file):
        import tidalapi
        p = _filtered("tracks", page_size=12)
        assert p.session.calls[0]["models"] == [tidalapi.Track]
        assert {i["type"] for i in p._search_results} == {"track"}
        assert len(p._search_results) == 12  # a whole page of one type

    def test_albums_filter_asks_only_for_albums(self, config_file):
        import tidalapi
        p = _filtered("albums", page_size=12)
        assert p.session.calls[0]["models"] == [tidalapi.Album]
        assert {i["type"] for i in p._search_results} == {"album"}
        assert len(p._search_results) == 12

    def test_artists_filter_asks_only_for_artists(self, config_file):
        import tidalapi
        p = _filtered("artists", page_size=12)
        assert p.session.calls[0]["models"] == [tidalapi.Artist]
        assert {i["type"] for i in p._search_results} == {"artist"}
        assert len(p._search_results) == 12

    def test_all_filter_still_splits(self, config_file):
        p = _filtered("all", page_size=12)
        assert {i["type"] for i in p._search_results} == {"track", "album", "artist"}
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


# ── paging on scroll ──


class _PagedSession:
    """A session with a finite catalogue behind the query, so offsets matter."""

    def __init__(self, totals=None):
        self.calls = []
        self.totals = totals or {"tracks": 200, "albums": 200, "artists": 200}
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
        assert len(p._search_results) == 20
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
        p._search_fetching = True
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
