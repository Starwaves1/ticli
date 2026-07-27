"""Tests for the artist page: its four sections, the one-request-per-section
rule, the three states a section can be in, and what Enter does to each kind of
row.

No TIDAL session or network anywhere: the artist is a fake that counts every
endpoint it is asked for, and the config directory is redirected to tmp_path.
"""

import time
import types

import pytest
import tidalapi

from ticli.player import (
    KEY_DOWN,
    KEY_ENTER,
    KEY_ESC,
    KEY_LEFT,
    KEY_RIGHT,
    KEY_SHIFT_TAB,
    KEY_TAB,
    KEY_UP,
    HeadlessTidalPlayer,
)
from ticli.utils import config as config_mod


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


def _track(i):
    return types.SimpleNamespace(
        id=i, name=f"Track {i}", duration=200,
        artists=[types.SimpleNamespace(name="Miles Davis")],
    )


def _album(i):
    return types.SimpleNamespace(
        id=i, name=f"Album {i}", year=1959 + i,
        artist=types.SimpleNamespace(name="Miles Davis"),
    )


def _playlist(i):
    """A real Playlist instance — the page filter is an isinstance check."""
    pl = tidalapi.Playlist.__new__(tidalapi.Playlist)
    pl.id = f"pl-{i}"
    pl.name = f"Playlist {i}"
    pl.num_tracks = 10 + i
    pl.creator = types.SimpleNamespace(name="TIDAL")
    return pl


def _similar(i):
    return types.SimpleNamespace(id=100 + i, name=f"Similar {i}")


class _FakeArtist:
    """Counts every endpoint. `fails` names the ones that raise instead, and
    `empty` the ones that answer with nothing."""

    def __init__(self, name="Miles Davis", artist_id=1, fails=(), empty=(),
                 playlist_rows=None):
        self.id = artist_id
        self.name = name
        self.calls = []
        self.fails = set(fails)
        self.empty = set(empty)
        self._playlist_rows = playlist_rows

    def _answer(self, endpoint, rows):
        self.calls.append(endpoint)
        if endpoint in self.fails:
            raise RuntimeError(endpoint)
        return [] if endpoint in self.empty else rows

    def get_top_tracks(self, limit=None, offset=0):
        return self._answer("toptracks", [_track(i) for i in range(limit or 20)])

    def get_albums(self, limit=None, offset=0):
        return self._answer("albums", [_album(i) for i in range(3)])

    def get_radio(self, limit=None):
        return self._answer("radio", [_track(500 + i) for i in range(2)])

    def get_similar(self):
        return self._answer("similar", [_similar(i) for i in range(2)])

    def page(self):
        self.calls.append("page")
        if "page" in self.fails:
            raise RuntimeError("page")
        rows = self._playlist_rows
        if rows is None:
            rows = [] if "page" in self.empty else [_playlist(i) for i in range(2)]
        return types.SimpleNamespace(categories=[
            # An artist page row of the wrong kind, which must be ignored
            types.SimpleNamespace(items=[_album(9)]),
            types.SimpleNamespace(items=rows),
            # A category with nothing in it at all
            types.SimpleNamespace(items=None),
        ])


def _open(artist=None, page_size=10):
    p = HeadlessTidalPlayer()
    p._page_size = page_size
    artist = artist if artist is not None else _FakeArtist()
    p._open_artist(artist)
    assert _wait_for(lambda: p._artist_record()["state"] != "loading")
    return p, artist


def _tab_to(p, section):
    """Tab round to a section and wait for whatever it fetches."""
    while p._artist_section != section:
        p._handle_key(KEY_TAB)
    assert _wait_for(lambda: p._artist_record()["state"] != "loading")


# ── lazy loading ──


class TestLazyLoad:
    def test_opening_the_page_costs_one_request(self, config_file):
        p, artist = _open()
        assert artist.calls == ["toptracks"]
        assert p._mode == HeadlessTidalPlayer.MODE_ARTIST
        assert p._artist_section == "tracks"

    def test_each_section_is_fetched_on_first_visit(self, config_file):
        p, artist = _open()
        _tab_to(p, "albums")
        assert artist.calls == ["toptracks", "albums"]
        _tab_to(p, "playlists")
        assert artist.calls == ["toptracks", "albums", "page"]
        _tab_to(p, "suggestions")
        assert artist.calls == ["toptracks", "albums", "page", "radio", "similar"]

    def test_browsing_every_section_costs_five_requests(self, config_file):
        """Four tabs, five endpoints — suggestions is two — and no more."""
        p, artist = _open()
        for _ in range(len(HeadlessTidalPlayer.ARTIST_SECTIONS)):
            p._handle_key(KEY_TAB)
            assert _wait_for(lambda: p._artist_record()["state"] != "loading")
        assert len(artist.calls) == 5

    def test_a_held_down_tab_does_not_fan_out(self, config_file):
        p, artist = _open()
        for _ in range(40):
            p._handle_key(KEY_TAB)
        assert _wait_for(lambda: all(
            r["state"] != "loading" for r in p._artist_sections.values()))
        assert len(artist.calls) == 5


class TestSessionCache:
    def test_returning_to_a_section_makes_no_request(self, config_file):
        p, artist = _open()
        _tab_to(p, "albums")
        _tab_to(p, "tracks")  # all the way round, so every section is warm
        _tab_to(p, "albums")
        assert artist.calls.count("albums") == 1
        assert len(p._artist_rows()) == 3

    def test_going_back_from_an_album_refetches_nothing(self, config_file):
        p, artist = _open()
        _tab_to(p, "albums")
        p._handle_key(KEY_ENTER)  # opens the album
        assert p._mode == HeadlessTidalPlayer.MODE_BROWSE
        p._handle_key(KEY_LEFT)
        assert p._mode == HeadlessTidalPlayer.MODE_ARTIST
        assert p._artist_section == "albums"
        assert artist.calls == ["toptracks", "albums"]

    def test_two_artists_do_not_share_a_section(self, config_file):
        p, first = _open()
        second = _FakeArtist(name="Bill Evans", artist_id=2)
        p._open_artist(second)
        assert _wait_for(lambda: p._artist_record()["state"] == "ready")
        assert second.calls == ["toptracks"]
        p._handle_key(KEY_LEFT)
        assert p._artist is first
        assert first.calls == ["toptracks"]

    def test_the_cursor_is_remembered_per_section(self, config_file):
        p, _ = _open()
        p._handle_key(KEY_DOWN)
        p._handle_key(KEY_DOWN)
        assert p._artist_cursor == 2
        _tab_to(p, "albums")
        assert p._artist_cursor == 0
        _tab_to(p, "tracks")
        assert p._artist_cursor == 2


# ── the three states ──


class TestStates:
    def test_loading_says_so(self, config_file):
        p = HeadlessTidalPlayer()
        blocked = _FakeArtist()
        blocked.get_top_tracks = lambda limit=None, offset=0: time.sleep(5)
        p._open_artist(blocked)
        text = p._build_artist_display().plain
        assert "Loading top tracks" in text

    def test_empty_names_what_is_missing(self, config_file):
        p, _ = _open(_FakeArtist(empty=("albums",)))
        _tab_to(p, "albums")
        assert p._artist_record()["state"] == "ready"
        assert "No albums for this artist" in p._build_artist_display().plain

    def test_failed_is_not_empty(self, config_file):
        p, _ = _open(_FakeArtist(fails=("albums",)))
        _tab_to(p, "albums")
        assert p._artist_record()["state"] == "failed"
        text = p._build_artist_display().plain
        assert "Failed to load albums" in text
        assert "No albums" not in text
        assert "retry" in text

    def test_empty_and_failed_do_not_look_alike(self, config_file):
        """Different words and a different colour, not two blank screens."""
        empty, _ = _open(_FakeArtist(empty=("albums",)))
        _tab_to(empty, "albums")
        failed, _ = _open(_FakeArtist(fails=("albums",)))
        _tab_to(failed, "albums")
        assert empty._build_artist_display().plain != failed._build_artist_display().plain
        styles = {str(s.style) for s in failed._build_artist_display().spans}
        assert any("red" in s for s in styles)

    def test_enter_retries_a_failed_section(self, config_file):
        artist = _FakeArtist(fails=("albums",))
        p, _ = _open(artist)
        _tab_to(p, "albums")
        artist.fails = set()
        p._handle_key(KEY_ENTER)
        assert _wait_for(lambda: p._artist_record()["state"] == "ready")
        assert artist.calls == ["toptracks", "albums", "albums"]
        assert len(p._artist_rows()) == 3

    def test_a_failed_section_does_not_retry_itself(self, config_file):
        p, artist = _open(_FakeArtist(fails=("albums",)))
        _tab_to(p, "albums")
        _tab_to(p, "tracks")
        _tab_to(p, "albums")
        assert artist.calls.count("albums") == 1

    def test_half_of_suggestions_is_still_suggestions(self, config_file):
        p, _ = _open(_FakeArtist(fails=("radio",)))
        _tab_to(p, "suggestions")
        assert p._artist_record()["state"] == "ready"
        assert [r["type"] for r in p._artist_rows()] == ["artist", "artist"]

    def test_losing_both_halves_is_a_failure(self, config_file):
        p, _ = _open(_FakeArtist(fails=("radio", "similar")))
        _tab_to(p, "suggestions")
        assert p._artist_record()["state"] == "failed"
        assert "Failed to load suggestions" in p._build_artist_display().plain


# ── sections and switching ──


class TestSections:
    def test_the_tab_row_is_always_on_screen(self, config_file):
        p, _ = _open()
        text = p._build_artist_display().plain
        for label in HeadlessTidalPlayer.ARTIST_SECTION_LABELS.values():
            assert label in text
        assert "[Tab]" in text
        assert "Miles Davis" in text

    def test_the_active_section_is_the_only_bold_one(self, config_file):
        p, _ = _open()
        _tab_to(p, "albums")
        display = p._build_artist_display()
        bold = {display.plain[s.start:s.end] for s in display.spans
                if "bold cyan" in str(s.style)}
        assert "Albums" in bold
        assert "Top Tracks" not in bold

    def test_tab_wraps_and_shift_tab_goes_back(self, config_file):
        p, _ = _open()
        order = HeadlessTidalPlayer.ARTIST_SECTIONS
        for expected in order[1:] + order[:1]:
            p._handle_key(KEY_TAB)
            assert p._artist_section == expected
        p._handle_key(KEY_SHIFT_TAB)
        assert p._artist_section == order[-1]

    def test_tab_works_while_a_section_is_loading(self, config_file):
        p = HeadlessTidalPlayer()
        slow = _FakeArtist()
        slow.get_top_tracks = lambda limit=None, offset=0: time.sleep(5)
        p._open_artist(slow)
        p._handle_key(KEY_TAB)
        assert p._artist_section == "albums"

    def test_sections_honour_page_size(self, config_file):
        p, artist = _open(page_size=40)
        assert len(p._artist_rows()) >= 40
        text = p._build_artist_display().plain
        assert text.count("[TRACK]") == 40

    def test_a_small_page_shows_a_page(self, config_file):
        p, _ = _open(page_size=5)
        text = p._build_artist_display().plain
        assert text.count("[TRACK]") == 5
        assert "Page 1/4" in text

    def test_a_downloaded_track_is_marked_here_too(self, config_file, monkeypatch):
        """The same gutter browse, queue and search use. A marker on three
        list screens out of four is its own kind of jank."""
        p, _ = _open(page_size=5)
        first = p._artist_rows()[0]["obj"]
        monkeypatch.setattr(p, "_downloaded",
                            lambda tid: tid == getattr(first, "id", None))
        lines = [l for l in p._build_artist_display().plain.splitlines()
                 if "[TRACK]" in l]
        # After the cursor gutter, before the badge
        assert 0 < lines[0].index("↓") < lines[0].index("[TRACK]"), lines[0]
        assert "↓" not in lines[1], lines[1]
        # Reserved, not inserted: an undownloaded row's badge sits in the same
        # column as a downloaded one's, or the list would look ragged
        assert (lines[0].index("[TRACK]") == lines[1].index("[TRACK]")), \
            "the marker shifted the badge column"

    def test_a_row_that_is_not_a_track_reserves_the_gutter_anyway(self, config_file):
        """Only a track can be downloaded, but an albums section still has to
        line its badges up with a tracks section's."""
        p, _ = _open(page_size=5)
        _tab_to(p, "albums")
        lines = [l for l in p._build_artist_display().plain.splitlines()
                 if "[ALBUM]" in l]
        assert lines, "no album rows to check"
        assert "↓" not in lines[0]
        p2, _ = _open(page_size=5)
        track_line = [l for l in p2._build_artist_display().plain.splitlines()
                      if "[TRACK]" in l][0]
        assert lines[0].index("[ALBUM]") == track_line.index("[TRACK]")

    def test_playlist_rows_are_the_only_ones_kept_from_the_page(self, config_file):
        p, _ = _open()
        _tab_to(p, "playlists")
        assert [r["type"] for r in p._artist_rows()] == ["playlist", "playlist"]
        text = p._build_artist_display().plain
        assert "Playlist 0" in text
        assert "Album 9" not in text

    def test_duplicate_playlists_across_rows_appear_once(self, config_file):
        dupe = _playlist(1)
        p, _ = _open(_FakeArtist(playlist_rows=[dupe, dupe]))
        _tab_to(p, "playlists")
        assert len(p._artist_rows()) == 1


# ── selecting a row ──


class TestSelection:
    def test_enter_on_a_track_plays_it(self, config_file):
        p, _ = _open()
        played = []
        p._play_track = lambda t: played.append(t)
        p._handle_key(KEY_DOWN)
        p._handle_key(KEY_ENTER)
        assert played and played[0].name == "Track 1"
        assert p._queue_index == 1
        assert len(p._queue) == len(p._artist_rows())

    def test_right_on_an_album_opens_it(self, config_file):
        p, _ = _open()
        _tab_to(p, "albums")
        opened = []
        p._open_album = lambda a: opened.append(a)
        p._handle_key(KEY_RIGHT)
        assert opened and opened[0].name == "Album 0"

    def test_enter_on_a_playlist_opens_it(self, config_file):
        p, _ = _open()
        _tab_to(p, "playlists")
        opened = []
        p._open_playlist = lambda pl: opened.append(pl)
        p._handle_key(KEY_ENTER)
        assert opened and opened[0].name == "Playlist 0"

    def test_enter_on_a_similar_artist_opens_that_artist(self, config_file):
        p, _ = _open()
        _tab_to(p, "suggestions")
        # radio tracks first, then the similar artists
        for _ in range(2):
            p._handle_key(KEY_DOWN)
        opened = []
        p._open_artist = lambda a: opened.append(a)
        p._handle_key(KEY_ENTER)
        assert opened and opened[0].name == "Similar 0"

    def test_a_mixed_section_queues_only_its_tracks(self, config_file):
        p, _ = _open()
        _tab_to(p, "suggestions")
        played = []
        p._play_track = lambda t: played.append(t)
        p._handle_key(KEY_DOWN)
        p._handle_key(KEY_ENTER)
        assert p._queue_index == 1
        assert len(p._queue) == 2  # the two radio tracks, not the artists

    def test_play_all_plays_the_section(self, config_file):
        p, _ = _open(page_size=5)
        started = []
        p._play_queue_index = lambda i: started.append(i)
        p._handle_key("a")
        assert started == [0]
        assert len(p._queue) == 20

    def test_play_all_on_an_empty_section_does_nothing(self, config_file):
        p, _ = _open(_FakeArtist(empty=("albums",)))
        _tab_to(p, "albums")
        p._handle_key("a")
        assert p._queue == []

    def test_add_to_playlist_targets_the_row_under_the_cursor(self, config_file):
        p, _ = _open()
        p.session = types.SimpleNamespace(user=types.SimpleNamespace(playlists=list))
        p._handle_key(KEY_DOWN)
        p._handle_key("y")
        assert p._mode == HeadlessTidalPlayer.MODE_ADD_TO_PLAYLIST
        assert p._picker_track.name == "Track 1"


# ── never stuck ──


class TestNoDeadEnds:
    def test_up_from_the_top_hands_the_arrows_to_the_player(self, config_file):
        p, _ = _open()
        p._current_track = _track(0)
        p._handle_key(KEY_UP)
        assert p._player_focus

    def test_scrubbing_still_works_from_the_artist_page(self, config_file):
        p, _ = _open()
        p._current_track = _track(0)
        p._handle_key(KEY_UP)
        seeks = []
        p._seek_by = lambda d: seeks.append(d)
        p._handle_key(KEY_RIGHT)
        p._handle_key(KEY_LEFT)
        assert len(seeks) == 2 and seeks[0] > 0 > seeks[1]
        p._handle_key(KEY_DOWN)
        assert not p._player_focus
        # And the list has its arrows back
        p._handle_key(KEY_DOWN)
        assert p._artist_cursor == 1

    def test_up_within_the_list_does_not_steal_focus(self, config_file):
        p, _ = _open()
        p._current_track = _track(0)
        p._handle_key(KEY_DOWN)
        p._handle_key(KEY_UP)
        assert not p._player_focus
        assert p._artist_cursor == 0

    def test_esc_leaves_a_loading_section(self, config_file):
        p = HeadlessTidalPlayer()
        slow = _FakeArtist()
        slow.get_top_tracks = lambda limit=None, offset=0: time.sleep(5)
        p._open_artist(slow)
        p._handle_key(KEY_ESC)
        assert p._mode == HeadlessTidalPlayer.MODE_PLAYER

    def test_enter_on_an_empty_section_does_nothing(self, config_file):
        p, _ = _open(_FakeArtist(empty=("albums",)))
        _tab_to(p, "albums")
        p._handle_key(KEY_ENTER)
        assert p._mode == HeadlessTidalPlayer.MODE_ARTIST

    def test_the_footer_names_the_section_key(self, config_file):
        p, _ = _open()
        panel = p._build_display()
        assert "[Tab]" in panel.renderable.plain
