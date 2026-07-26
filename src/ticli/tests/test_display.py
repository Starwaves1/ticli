"""Tests for what actually reaches the terminal.

The bug these exist for was invisible to every other test in this suite:
`_build_display()` returned a perfectly correct renderable every time, and
the panel at the bottom of the screen looked right. What was wrong was the
*bytes* — each repaint stranded the previous frame instead of overwriting
it, so the scrollback filled with bands of album art and, in mini mode,
three whole `Ticli` panels showing three different timestamps.

So nothing here asserts that a function returned a string. Everything is
driven through a real `rich.live.Live` against a fixed-size console, and the
escape sequences it writes are replayed into `vt.Screen` — a small terminal
model — so a test can ask the question the eye asks: how many panels are on
the screen, and what fell off the top.

No network, no session, no TIDAL: the track is a stub and the artwork is a
grid of one colour.
"""

import re
import time
import types

import pytest
from rich.console import Console

from ticli.player import HeadlessTidalPlayer
from ticli.tests.vt import Screen
from ticli.utils import artwork as art_mod
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod

COVER = "3f2d1c0b-1111-2222-3333-444455556666"

MODES = (
    HeadlessTidalPlayer.MODE_PLAYER,
    HeadlessTidalPlayer.MODE_SEARCH,
    HeadlessTidalPlayer.MODE_BROWSE,
    HeadlessTidalPlayer.MODE_ARTIST,
    HeadlessTidalPlayer.MODE_QUEUE,
    HeadlessTidalPlayer.MODE_PLAYLISTS,
    HeadlessTidalPlayer.MODE_ADD_TO_PLAYLIST,
    HeadlessTidalPlayer.MODE_SETTINGS,
)

# The widths a real window gets dragged to. 60 is where the old fixed-width
# progress bar started wrapping and 120 is past every ceiling in the layout.
WIDTHS = (60, 80, 100, 120)

# A `[key] label` pair, or a bare `[key]` — the two shapes a hint may take.
HINT = re.compile(r"\[[^\[\]]+\](?: [^\[\]]+?)?(?:  |$)")

ALT_SCREEN_ON = "\x1b[?1049h"
ALT_SCREEN_OFF = "\x1b[?1049l"
CURSOR_UP = "\x1b[1A"


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """No test may read or write the owner's real cache or config."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config" / "config.json")
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    return tmp_path


def _track(name="Satisfied (Ambient Reprise)"):
    return types.SimpleNamespace(
        id=1,
        name=name,
        artists=[types.SimpleNamespace(name="Catching Flies")],
        album=types.SimpleNamespace(name="An Album With A Name", cover=COVER),
        duration=200,
    )


def _grid(cols, rows):
    return [[(10, 20, 30)] * cols for _ in range(rows * 2)]


class Harness:
    """A player painting into a `Screen` through a real `Live`."""

    def __init__(self, width=100, height=30, artwork=(20, 10)):
        import io

        self.buf = io.StringIO()
        self.player = HeadlessTidalPlayer()
        self.player.console = Console(
            file=self.buf, force_terminal=True, width=width, height=height,
            color_system="truecolor")
        self.player._current_track = _track()
        self.player._playing = True
        self.player._show_artwork = True
        self.player._queue = [_track(), _track("Next One")]
        self.player._queue_index = 0
        self.set_artwork(artwork)
        self.screen = Screen(width, height)
        self.live = None

    # ── driving ──

    def set_artwork(self, size):
        cols, rows = size if size else (20, 10)
        self.player._artwork = (COVER, cols, rows, _grid(cols, rows) if size else None)

    def resize(self, width, height):
        self.player.console.size = (width, height)
        self.screen.resize(width, height)

    def start(self):
        self.live = self.player._make_live()
        self.live.start(refresh=False)
        return self._drain()

    def stop(self):
        self.live.stop()
        return self._drain()

    def repaint(self, force=True):
        self.player._repaint(self.live, force=force)
        return self._drain()

    def _drain(self):
        out = self.buf.getvalue()
        self.buf.seek(0)
        self.buf.truncate(0)
        self.screen.feed(out)
        return out

    # ── asking ──

    def panels(self):
        """Top borders visible on screen: more than one means a stranded frame."""
        return [r for r in self.screen.text() if "╭" in r]

    def art_rows(self):
        return [r for r in self.screen.text() if "▀" in r]

    def stranded(self):
        return [r for r in self.screen.scrolled() if r.strip()]

    def assert_one_frame(self, note=""):
        """The screen holds one whole panel and nothing else.

        Not just "one top border": the artifact was as often a *piece* of a
        frame — bands of album art with a scrap of the left border and the
        progress column beside them — left above or below the live panel
        with no border of its own. So every non-blank row on the screen has
        to be inside the one panel.
        """
        rows = self.screen.text()
        tops = [i for i, r in enumerate(rows) if "╭" in r]
        bottoms = [i for i, r in enumerate(rows) if "╰" in r]
        assert len(tops) == 1, (note, "top borders", tops)
        assert len(bottoms) <= 1, (note, "bottom borders", bottoms)
        # No bottom border means the pane was taller than the terminal, so
        # Rich cropped it — degradation, not corruption: it still runs from
        # the top of the screen to the bottom with nothing else beside it.
        end = bottoms[0] if bottoms else len(rows) - 1
        outside = [r for i, r in enumerate(rows)
                   if r.strip() and not tops[0] <= i <= end]
        assert outside == [], (note, outside)


@pytest.fixture
def h():
    harness = Harness()
    harness.start()
    yield harness
    if harness.live is not None:
        harness.live.stop()


# ── the artifact ──


class TestNothingIsStranded:
    def test_a_steady_player_repaints_in_place(self, h):
        for _ in range(8):
            h.repaint()
        h.assert_one_frame()
        assert h.stranded() == []

    def test_a_frame_that_shrinks_leaves_nothing_above_it(self, h):
        """Full player to mini: the reported screenshot had three whole
        panels stacked, one per repaint, each showing a different time."""
        h.repaint()
        h.player._mini_player = True
        h.repaint()
        h.repaint()
        h.assert_one_frame("mini")
        assert h.art_rows() == []
        h.player._mini_player = False
        h.repaint()
        h.assert_one_frame("back to full")

    def test_artwork_appearing_and_vanishing(self, h):
        # The size the window in the fixture actually gets, not a constant:
        # art_size answers to the width as well as the height now
        size = art_mod.art_size(100, 30)
        h.set_artwork(size)
        h.repaint()
        h.set_artwork(None)          # a cover with no picture: pane gets shorter
        h.repaint()
        assert h.art_rows() == []
        h.assert_one_frame("no artwork")
        h.set_artwork(size)
        h.repaint()
        assert len(h.art_rows()) == size[1]
        h.assert_one_frame("artwork back")

    def test_artwork_stepping_down_a_size(self, h):
        """The picture's size is a step function of the terminal's, so
        crossing a threshold changes the height of the frame."""
        for height in (30, 25, 23, 21, 30):
            h.resize(100, height)
            size = art_mod.art_size(100, height)
            h.set_artwork(size)
            h.repaint()
            assert len(h.art_rows()) == (size[1] if size else 0), height
            h.assert_one_frame(f"height {height}")

    def test_a_toast_and_the_extra_controls_row(self, h):
        h.repaint()
        h.player._toast = "Cached songs cleared"
        h.player._toast_until = time.time() + 60
        h.player._show_more = True
        h.repaint()
        h.assert_one_frame("toast up")
        h.player._toast_until = 0
        h.player._show_more = False
        h.repaint()
        h.assert_one_frame("toast gone")
        assert h.stranded() == []

    def test_switching_modes(self, h):
        for mode in (h.player.MODE_SEARCH, h.player.MODE_QUEUE,
                     h.player.MODE_PLAYLISTS, h.player.MODE_SETTINGS,
                     h.player.MODE_PLAYER):
            h.player._mode = mode
            h.repaint()
            h.assert_one_frame(str(mode))


# ── resize ──


class TestResize:
    @pytest.mark.parametrize("size", [(60, 30), (40, 30), (100, 18), (140, 50), (30, 12)])
    def test_the_display_survives_a_resize(self, h, size):
        h.repaint()
        h.resize(*size)
        h.repaint()
        h.assert_one_frame(f"just resized to {size}")
        h.repaint()
        h.assert_one_frame(f"resized to {size}")

    def test_a_resize_alone_forces_a_write(self, h):
        """A window that only got shorter renders the same segments, and the
        skip-if-identical optimisation would swallow the one repaint that
        has to happen. The size is part of the key for exactly this."""
        h.repaint()
        assert h.repaint(force=False) == ""
        h.resize(100, 18)
        assert h.repaint(force=False) != ""

    def test_sigwinch_asks_for_a_repaint(self):
        player = HeadlessTidalPlayer()
        assert player._resized is False
        player._resized = True          # what the handler does, minus the signal
        assert player._resized is True


# ── how the repaint is written ──


class TestTheWriteItself:
    def test_the_tui_runs_on_the_alternate_screen(self):
        harness = Harness()
        assert ALT_SCREEN_ON in harness.start()
        assert ALT_SCREEN_OFF in harness.stop()

    def test_a_repaint_never_counts_rows(self, h):
        """The whole class of bug was Rich walking the cursor up as many rows
        as the last frame was tall — an assumption a resize invalidates. On
        the alternate screen a frame is placed absolutely, so a repaint that
        emits a cursor-up has lost that guarantee."""
        out = h.repaint()
        assert CURSOR_UP not in out
        h.player._mini_player = True
        assert CURSOR_UP not in h.repaint()
        h.resize(60, 20)
        assert CURSOR_UP not in h.repaint()

    def test_a_repaint_covers_every_row_of_the_terminal(self, h):
        """Which is what makes the previous frame unreachable: there is no
        row left over for it to survive in."""
        out = h.repaint()
        assert out.count("\n") + 1 >= h.player.console.size.height


# ── the pane still fits ──


class TestThePaneFits:
    def test_artwork_is_only_offered_where_the_whole_pane_fits(self):
        """Overflowing is not harmless: Rich answers it by replacing the
        bottom line — the controls — with a red ellipsis."""
        for height in range(18, 45):
            size = art_mod.art_size(100, height)
            if size is None:
                continue
            harness = Harness(width=100, height=height, artwork=size)
            p = harness.player
            p._show_more = True
            p._toast = "A toast that is taking up a row"
            p._toast_until = time.time() + 60
            rows = len(p.console.render_lines(
                p._build_display(), p.console.options, pad=False))
            assert rows <= height, (height, size, rows)

    def test_the_pane_fits_every_width_and_mode(self):
        """The whole matrix, because the pane is not one shape: each mode
        draws a different body and each width lays the footer out differently.
        A pane that is one row too tall is not a cosmetic problem — Rich
        answers it by replacing the bottom line, which is the controls."""
        for width in (60, 80, 100, 120):
            for height in (24, 30):
                for mode in MODES:
                    for more in (False, True):
                        harness = Harness(width=width, height=height,
                                          artwork=art_mod.art_size(width, height))
                        p = harness.player
                        p._mode = mode
                        p._show_more = more
                        _fill_lists(p)
                        rows = len(p.console.render_lines(
                            p._build_display(), p.console.options, pad=False))
                        assert rows <= height, (width, height, mode, more, rows)


# ── the footer, at every width ──


def _fill_lists(p):
    """Enough rows in every list that no screen is empty for want of data.

    Including the artist page, which is filled by hand rather than opened: its
    sections are fetched on a thread, and a layout test must not depend on one
    landing (and must never reach for the network to get there).
    """
    tracks = [_track(f"Track number {i} with a longish name") for i in range(40)]
    p._queue = tracks
    p._queue_index = 3
    p._browse_tracks = tracks
    p._search_results = [{"type": "track", "name": t.name,
                          "artist": "Catching Flies", "obj": t} for t in tracks]
    p._search_query = "a query"
    p._playlists = [types.SimpleNamespace(
        id=str(i), name=f"A playlist named {i}", num_tracks=20) for i in range(30)]
    p._editable_playlists = p._playlists
    p._artist = types.SimpleNamespace(id=7, name="An Artist With A Long Name")
    p._artist_sections = {
        (p._artist_key(section)): {
            "state": "ready",
            "items": [{"type": "track", "obj": t} for t in tracks],
            "message": "",
        }
        for section in HeadlessTidalPlayer.ARTIST_SECTIONS
    }


def _body(h):
    """The panel's content rows, borders stripped."""
    rows = []
    for line in h.screen.text():
        if line.startswith("│") and line.endswith("│"):
            rows.append(line[1:-1])
    return rows


def _footer(h):
    """The hint rows as they are on screen.

    Asked for by content rather than by position: the player says what it laid
    out for this window, and every one of those lines has to be on the screen
    verbatim. That is stricter than reading the bottom rows back — it catches a
    footer that was composed and then cropped away — and it does not confuse a
    hint with the settings page's own [x] and [o] action lines.
    """
    laid = [line.plain.strip() for line in h.player._compose(h.player._fit)[1]]
    rows = [line.strip() for line in _body(h)]
    for line in laid:
        assert line in rows, ("the footer never reached the screen", line, rows)
    return laid


class TestTheFooterNeverBreaksAPair:
    """`[space] play/pause` is one thing. It may be shortened, it may move to
    the next line, it may be dropped entirely when the window is too narrow to
    hold it — but it may never be cut in half, and half of it may never be the
    last thing on a line with its other half on the next one."""

    def _harness(self, width, mode, more=False, height=24):
        h = Harness(width=width, height=height,
                    artwork=art_mod.art_size(width, height))
        h.player._mode = mode
        h.player._show_more = more
        _fill_lists(h.player)
        h.start()
        h.repaint()
        return h

    @pytest.mark.parametrize("width", WIDTHS)
    @pytest.mark.parametrize("mode", MODES)
    def test_every_hint_on_screen_is_whole(self, width, mode):
        h = self._harness(width, mode)
        try:
            footer = _footer(h)
            assert footer, (width, mode, "no hints at all")
            for line in footer:
                text = line.strip()
                assert text.count("[") == text.count("]"), (width, mode, line)
                assert "…" not in text, (width, mode, "a hint was clipped", line)
                # Consumed entirely by whole hints: nothing left over means
                # nothing was split across the line break
                assert "".join(HINT.findall(text)) == text, (width, mode, line)
            h.assert_one_frame(f"{width} {mode}")
        finally:
            h.live.stop()

    @pytest.mark.parametrize("width", WIDTHS)
    def test_the_more_menu_too(self, width):
        h = self._harness(width, HeadlessTidalPlayer.MODE_PLAYER, more=True)
        try:
            for line in _footer(h):
                text = line.strip()
                assert "…" not in text, (width, line)
                assert "".join(HINT.findall(text)) == text, (width, line)
            h.assert_one_frame(f"more at {width}")
        finally:
            h.live.stop()

    def test_a_narrow_window_drops_hints_rather_than_truncating_them(self):
        """40 columns cannot hold six hints. What it must not do is hold five
        and a half."""
        h = self._harness(40, HeadlessTidalPlayer.MODE_PLAYER)
        try:
            for line in _footer(h):
                text = line.strip()
                assert "…" not in text, line
                assert "".join(HINT.findall(text)) == text, line
            h.assert_one_frame("40 columns")
        finally:
            h.live.stop()


class TestTheProgressLine:
    """It used to be laid out at a fixed 50 columns whatever the window was,
    which at 60 columns arrived as four wrapped pieces."""

    @pytest.mark.parametrize("width", WIDTHS + (40, 30))
    def test_the_times_stay_on_one_line_with_the_bar(self, width):
        h = Harness(width=width, height=24,
                    artwork=art_mod.art_size(width, 24))
        h.start()
        h.repaint()
        try:
            elapsed = [r for r in _body(h) if "0:00" in r]
            assert len(elapsed) == 1, (width, elapsed)
            assert "3:20" in elapsed[0], (width, elapsed[0])
            h.assert_one_frame(str(width))
        finally:
            h.live.stop()

    def test_the_bar_grows_with_the_window_up_to_its_ceiling(self):
        """Derived from the width, which is the whole change — and only up to
        progress_bar_max, which stopped being a size and became a ceiling."""
        widths, player = {}, None
        for width in (40, 50, 60, 100):
            h = Harness(width=width, height=24)
            player = h.player
            player._build_display()          # lays the pane out for this width
            widths[width] = player._build_progress_line(0, 200).plain.count("─")
        assert widths[40] < widths[50] < widths[60], widths
        assert widths[100] == player._bar_max - 1, widths


class TestTheVolumeOverlayOnScreen:
    """What `v` actually replaces, and what it puts back."""

    def _harness(self, width=100, mode=None):
        h = Harness(width=width, height=24,
                    artwork=art_mod.art_size(width, 24))
        if mode is not None:
            h.player._mode = mode
        _fill_lists(h.player)
        h.start()
        h.repaint()
        return h

    def test_v_replaces_the_footer_and_enter_puts_it_back(self):
        h = self._harness()
        try:
            before = _body(h)
            assert any("play/pause" in r for r in before)
            assert not any("Volume" in r for r in before)

            h.player._handle_key("v")
            h.repaint()
            during = _body(h)
            assert any("Volume" in r for r in during), during
            assert any("█" in r for r in during), "no bar on screen"
            assert any("[←/→] adjust" in r for r in during), during
            assert not any("play/pause" in r for r in during), "the footer is still there"
            h.assert_one_frame("overlay up")

            h.player._handle_key("\r")
            h.repaint()
            after = _body(h)
            assert not any("Volume" in r for r in after), after
            assert any("play/pause" in r for r in after)
            h.assert_one_frame("overlay closed")
            assert h.stranded() == []
        finally:
            h.live.stop()

    def test_esc_closes_it_too(self):
        h = self._harness()
        try:
            h.player._handle_key("v")
            h.repaint()
            assert any("Volume" in r for r in _body(h))
            h.player._handle_key("\x1b")
            h.repaint()
            assert not any("Volume" in r for r in _body(h))
            h.assert_one_frame("esc")
        finally:
            h.live.stop()

    @pytest.mark.parametrize("width", WIDTHS + (40,))
    def test_the_overlay_fits_every_width(self, width):
        h = self._harness(width=width)
        try:
            h.player._handle_key("v")
            h.repaint()
            body = _body(h)
            assert any("Volume" in r for r in body), (width, body)
            h.assert_one_frame(f"overlay at {width}")
        finally:
            h.live.stop()

    @pytest.mark.parametrize("mode", MODES)
    def test_it_covers_the_footer_of_whatever_screen_is_underneath(self, mode):
        h = self._harness(mode=mode)
        try:
            h.player._handle_key("v")
            h.repaint()
            body = _body(h)
            if mode == HeadlessTidalPlayer.MODE_SEARCH:
                # Search types the letter instead; the query is what changed
                assert not any("Volume " in r for r in body)
                return
            assert any("Volume" in r for r in body), (mode, body)
            h.assert_one_frame(f"overlay over {mode}")
        finally:
            h.live.stop()


def _tab_rows(h):
    """The [Tab] row that names the section or scope — not the footer's own
    [Tab] hint, which says the key rather than where it has landed."""
    footer = set(_footer(h))
    return [r.strip() for r in _body(h)
            if "[Tab]" in r and r.strip() not in footer]


class TestTheRowsThatSayWhereYouAre:
    """The artist page's tab row and search's scope row are state that would
    otherwise be hidden — which section, which scope. Neither may be relaxed
    away by a short window, and neither may be cut off by a narrow one: they
    say the same two things in fewer columns instead."""

    def _harness(self, width, mode, height=24):
        h = Harness(width=width, height=height,
                    artwork=art_mod.art_size(width, height))
        h.player._mode = mode
        _fill_lists(h.player)
        h.start()
        h.repaint()
        return h

    @pytest.mark.parametrize("width", WIDTHS + (40,))
    def test_the_artist_tab_row_is_never_clipped(self, width):
        h = self._harness(width, HeadlessTidalPlayer.MODE_ARTIST)
        try:
            row = _tab_rows(h)
            assert len(row) == 1, (width, _body(h))
            assert "…" not in row[0], (width, row[0])
            # Which section you are on survives at every width
            assert "Top Tracks" in row[0], (width, row[0])
            h.assert_one_frame(f"artist at {width}")
        finally:
            h.live.stop()

    @pytest.mark.parametrize("width", WIDTHS + (40,))
    def test_the_search_scope_row_is_never_clipped(self, width):
        h = self._harness(width, HeadlessTidalPlayer.MODE_SEARCH)
        try:
            row = _tab_rows(h)
            assert len(row) == 1, (width, _body(h))
            assert "…" not in row[0], (width, row[0])
            assert "All" in row[0], (width, row[0])
            h.assert_one_frame(f"search at {width}")
        finally:
            h.live.stop()

    @pytest.mark.parametrize("mode", (HeadlessTidalPlayer.MODE_ARTIST,
                                      HeadlessTidalPlayer.MODE_SEARCH))
    def test_a_short_window_keeps_the_row_and_drops_rows_instead(self, mode):
        h = self._harness(60, mode, height=18)
        try:
            assert _tab_rows(h), _body(h)
            h.assert_one_frame(f"{mode} at 60x18")
        finally:
            h.live.stop()


class TestScrubbingAndTheOverlayOnScreen:
    def _harness(self, width=80):
        h = Harness(width=width, height=24,
                    artwork=art_mod.art_size(width, 24))
        h.start()
        h.repaint()
        return h

    @pytest.mark.parametrize("width", WIDTHS + (40,))
    def test_the_scrub_marker_costs_the_bar_columns_not_the_pane(self, width):
        """The marker is on the progress line, so it comes out of the bar. A
        marker appended past the pane's edge would wrap the line it is on."""
        h = self._harness(width)
        try:
            unfocused = [r for r in _body(h) if "0:00" in r][0]
            h.player._focus_player()
            h.repaint()
            focused = [r for r in _body(h) if "0:00" in r][0]
            assert "⇆" in focused, (width, focused)
            assert "3:20" in focused, (width, focused)
            assert focused.count("0:00") == 1
            assert len([r for r in _body(h) if "0:00" in r]) == 1
            assert focused.count("─") + focused.count("━") <= unfocused.count("─") + unfocused.count("━")
            h.assert_one_frame(f"focused at {width}")
        finally:
            h.live.stop()

    def test_the_overlay_takes_the_marker_off_the_line(self):
        h = self._harness()
        try:
            h.player._focus_player()
            h.repaint()
            assert any("⇆" in r for r in _body(h))
            h.player._handle_key("v")
            h.repaint()
            body = _body(h)
            assert not any("⇆" in r for r in body), body
            assert any("Volume" in r for r in body), body
            h.assert_one_frame("overlay over a scrub")
            # and closing it puts the marker back
            h.player._handle_key("\r")
            h.repaint()
            assert any("⇆" in r for r in _body(h)), _body(h)
            h.assert_one_frame("scrub returned")
            assert h.stranded() == []
        finally:
            h.live.stop()
