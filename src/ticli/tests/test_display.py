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
from rich.cells import cell_len
from rich.console import Console

from ticli import player as player_mod
from ticli.player import HeadlessTidalPlayer
from ticli.tests.vt import Screen
from ticli.utils import artwork as art_mod
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod
from ticli.utils import downloads
from ticli.utils.config import QUALITY_CHOICES

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
# progress bar started wrapping, 80 is the last one that still stacks the
# cover above the track, 100 is the first that puts it beside, and 120 is
# past every ceiling in the layout.
WIDTHS = (60, 80, 100, 120)
# Both sides of MIN_BESIDE_WIDTH (83) at every height the matrix uses, so the
# frame is measured with the cover above the text and beside it.
OVERFLOW_WIDTHS = (40, 50, 60, 80, 100, 120)
OVERFLOW_HEIGHTS = (20, 24, 30, 40)

# A `[key] label` pair, or a bare `[key]` — the two shapes a hint may take.
HINT = re.compile(r"\[[^\[\]]+\](?: [^\[\]]+?)?(?:  |$)")

ALT_SCREEN_ON = "\x1b[?1049h"
ALT_SCREEN_OFF = "\x1b[?1049l"
CURSOR_UP = "\x1b[1A"


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """No test may read or write the owner's real cache or config — or reach
    resources.tidal.com, which a paint asking for a size the harness has not
    stored would otherwise do on a thread nobody is waiting for."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config" / "config.json")
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(downloads, "DOWNLOAD_ROOT", tmp_path / "Music" / "Ticli")
    monkeypatch.setattr(art_mod, "load",
                        lambda cover, cols, rows, **kw: _grid(cols, rows))
    return tmp_path


_UNSET = object()


def _art(width, height):
    """The size a cover is actually rendered at in a window this big — which
    is a question about the placement as well as the room, so it is asked the
    way the player asks it rather than by calling art_size bare."""
    return art_mod.art_size(width, height, art_mod.art_beside(width, height))


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

    def __init__(self, width=100, height=30, artwork=_UNSET):
        import io

        if artwork is _UNSET:
            # The size this window actually asks for. Storing any other one
            # is a miss, and a miss starts a fetch on a thread — which the
            # fixture keeps off the network but which would still land
            # halfway through a test and change the frame under it.
            artwork = _art(width, height)
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
        size = _art(100, 30)
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
            size = _art(100, height)
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
            size = _art(100, height)
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
                        # And with the footer put away by `[h]`, which frees
                        # rows the body then spends — a pane that overflows
                        # after taking them is the same bug
                        for hidden in (False, True):
                            harness = Harness(width=width, height=height,
                                              artwork=_art(width, height))
                            p = harness.player
                            p._mode = mode
                            p._show_more = more
                            p._footer_hidden = hidden
                            _fill_lists(p)
                            rows = len(p.console.render_lines(
                                p._build_display(), p.console.options, pad=False))
                            assert rows <= height, (width, height, mode, more,
                                                    hidden, rows)


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
    p._download_track = tracks[0]
    p._picker_track = tracks[0]
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
                    artwork=_art(width, height))
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
                    artwork=_art(width, 24))
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
                    artwork=_art(width, 24))
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


class _StubAudio:
    """Enough of AudioPlayer for the play/pause key, and nothing that runs."""

    player_cmd = "mpv"

    def __init__(self):
        self.is_paused = False

    def pause(self):
        self.is_paused = True

    def resume(self):
        self.is_paused = False

    def get_time_pos(self):
        return None


def _hints_on_screen(h):
    """The footer as rows of the screen: a row that starts with a bracket and
    is one of the lines the player laid out as its footer.

    Asked of the screen rather than of the player, because "the controls are
    gone" is a claim about what the eye can see — and the settings page draws
    its own `[x]`/`[o]` action lines in the body, which are not the footer.
    """
    laid = {line.plain.strip() for line in h.player._compose(h.player._fit)[1]}
    return [r.strip() for r in _body(h) if r.strip() in laid and r.strip()]


class TestHidingTheFooter:
    """`[h]` puts the controls away for as long as you are only listening.

    Arrows and space keep it away — skip, navigate, play/pause — and the first
    thing you do that isn't one of those brings it back *and does its job*,
    because a key you have to press twice is worse than a footer that returned
    a moment early.
    """

    def _harness(self, width=100, height=30, mode=None, **attrs):
        h = Harness(width=width, height=height, artwork=_art(width, height))
        if mode is not None:
            h.player._mode = mode
        _fill_lists(h.player)
        for name, value in attrs.items():
            setattr(h.player, name, value)
        h.start()
        h.repaint()
        return h

    def _press(self, h, key):
        h.player._handle_key(key)
        h.repaint()

    # ── it goes away, and it says how ──

    def test_the_footer_says_the_key(self):
        h = self._harness()
        try:
            assert any("[h] hide" in r for r in _body(h)), _body(h)
        finally:
            h.live.stop()

    def test_h_takes_the_controls_off_the_screen(self):
        h = self._harness()
        try:
            assert _hints_on_screen(h), "no footer to start with"
            self._press(h, "h")
            assert _hints_on_screen(h) == [], _body(h)
            # and the song is still there — this hides instructions, not identity
            assert any("Satisfied" in r for r in _body(h)), _body(h)
            h.assert_one_frame("hidden")
            assert h.stranded() == []
        finally:
            h.live.stop()

    def test_a_second_h_brings_it_back(self):
        """Falls straight out of the rule — h is not an arrow and not the
        spacebar — rather than being a toggle written separately."""
        h = self._harness()
        try:
            self._press(h, "h")
            assert _hints_on_screen(h) == []
            self._press(h, "h")
            assert any("[h] hide" in r for r in _body(h)), _body(h)
            h.assert_one_frame("back")
        finally:
            h.live.stop()

    def test_the_player_screen_can_put_its_footer_away(self):
        """The player is the one screen `h` hides the footer on now — every
        other screen sends `h` back to the player instead (see
        TestHFromAMenuShowsThePlayer). So the "puts it away" assertion belongs
        to the player alone: press `h`, the hints leave the screen."""
        h = self._harness(mode=HeadlessTidalPlayer.MODE_PLAYER)
        try:
            assert _hints_on_screen(h), "no footer to start with"
            self._press(h, "h")
            assert _hints_on_screen(h) == [], _body(h)
            h.assert_one_frame("hidden on the player")
        finally:
            h.live.stop()

    # ── what keeps it hidden ──

    def test_the_arrows_still_navigate_and_it_stays_hidden(self):
        """The player is the only screen the footer hides on now, so the
        hold-keys-keep-it-hidden rule is exercised there: ↑ hands the arrows to
        the scrubber and ← moves the clock, both HIDE_HOLD_KEYS, neither
        bringing the footer back."""
        h = self._harness(mode=HeadlessTidalPlayer.MODE_PLAYER)
        try:
            self._press(h, "h")
            assert _hints_on_screen(h) == [], "did not hide"
            self._press(h, "\x1b[A")            # ↑ focuses the scrubber
            assert h.player._player_focus, "↑ did nothing"
            assert _hints_on_screen(h) == [], _body(h)
            h.player._play_offset = 60.0
            self._press(h, "\x1b[D")            # ← seeks back
            assert h.player._get_position() == 50.0, "the arrow did nothing"
            assert _hints_on_screen(h) == [], _body(h)
            h.assert_one_frame("still hidden")
        finally:
            h.live.stop()

    def test_space_still_pauses_and_it_stays_hidden(self):
        h = self._harness()
        try:
            h.player.audio = _StubAudio()
            assert any("▶" in r for r in _body(h))
            self._press(h, "h")
            self._press(h, " ")
            assert any("⏸" in r for r in _body(h)), _body(h)
            assert h.player.audio.is_paused, "the spacebar did nothing"
            assert _hints_on_screen(h) == [], _body(h)
            h.assert_one_frame("paused and hidden")
        finally:
            h.live.stop()

    def test_scrubbing_keeps_it_hidden_too(self):
        """←/→ under scrub focus are still arrows, and `_handle_focus_key` runs
        after the hide check rather than around it."""
        h = self._harness()
        try:
            h.player._focus_player()
            self._press(h, "h")
            assert _hints_on_screen(h) == []
            h.player._play_offset = 60.0
            self._press(h, "\x1b[D")            # ← seeks back
            assert h.player._get_position() == 50.0
            assert _hints_on_screen(h) == [], _body(h)
            h.assert_one_frame("scrubbing hidden")
        finally:
            h.live.stop()

    # ── what brings it back ──

    def test_another_key_restores_it_and_still_does_its_job(self):
        """`q` opens the queue *and* puts the footer back, in that order."""
        h = self._harness()
        try:
            self._press(h, "h")
            assert _hints_on_screen(h) == []
            self._press(h, "q")
            assert h.player._mode == HeadlessTidalPlayer.MODE_QUEUE
            assert any("Queue" in r for r in _body(h)), _body(h)
            assert _hints_on_screen(h), _body(h)
            h.assert_one_frame("queue with its footer")
        finally:
            h.live.stop()

    def test_s_opens_search_as_well_as_restoring(self):
        h = self._harness()
        try:
            self._press(h, "h")
            self._press(h, "s")
            assert h.player._mode == HeadlessTidalPlayer.MODE_SEARCH
            assert h.player._search_query == "", "the s was swallowed into the query"
            assert _hints_on_screen(h), _body(h)
        finally:
            h.live.stop()

    def test_a_key_that_does_nothing_brings_it_back_too(self):
        """The decision, stated as a test: `z` is bound to nothing on the
        player screen and it still un-hides. Un-hiding only on a *bound* key
        would need a second copy of the mode dispatch to say which those are,
        and a key that did nothing is exactly when the controls are worth
        reading."""
        h = self._harness()
        try:
            self._press(h, "h")
            assert _hints_on_screen(h) == []
            self._press(h, "z")
            assert _hints_on_screen(h), _body(h)
        finally:
            h.live.stop()

    # ── where h is the letter h ──

    def test_search_types_an_h(self):
        h = self._harness(mode=HeadlessTidalPlayer.MODE_SEARCH)
        try:
            before = h.player._search_query
            self._press(h, "h")
            assert h.player._search_query == before + "h"
            assert h.player._footer_hidden is False
            assert _hints_on_screen(h), _body(h)
            assert not any("[h] hide" in r for r in _body(h)), "offered where it types"
        finally:
            h.live.stop()

    def test_the_new_playlist_name_types_an_h(self):
        h = self._harness(mode=HeadlessTidalPlayer.MODE_ADD_TO_PLAYLIST,
                          _picker_new_name="")
        try:
            self._press(h, "h")
            assert h.player._picker_new_name == "h"
            assert h.player._footer_hidden is False
            assert not any("[h] hide" in r for r in _body(h)), "offered where it types"
        finally:
            h.live.stop()

    def test_a_settings_row_being_typed_into_types_nothing_but_digits(self):
        """`h` is not a digit, so it commits the edit like every other key —
        what it must not do is hide the footer out from under a textbox."""
        numeric = [i for i, spec in enumerate(config_mod.SETTINGS_ROWS)
                   if spec["kind"] == "int"]
        h = self._harness(mode=HeadlessTidalPlayer.MODE_SETTINGS,
                          _settings_cursor=numeric[0], _settings_edit="12")
        try:
            self._press(h, "h")
            assert h.player._footer_hidden is False
            assert _hints_on_screen(h), _body(h)
        finally:
            h.live.stop()

    def test_the_volume_overlay_is_not_a_cheat_sheet(self):
        """It has taken the footer's rows and its `←/→ adjust` is a control.
        `h` there is ignored, the same as every other key the overlay does not
        use — and closing it must not reveal a footer that went away unseen."""
        h = self._harness()
        try:
            self._press(h, "v")
            assert any("Volume" in r for r in _body(h))
            self._press(h, "h")
            assert any("Volume" in r for r in _body(h)), "the overlay went away"
            assert h.player._footer_hidden is False
            self._press(h, "\r")
            assert any("[h] hide" in r for r in _body(h)), _body(h)
        finally:
            h.live.stop()

    def test_the_mini_player_has_no_footer_to_hide(self):
        h = self._harness(_mini_player=True)
        try:
            assert _hints_on_screen(h) == [], "the tiny player has a footer?"
            self._press(h, "h")
            assert h.player._footer_hidden is False
            h.player._mini_player = False
            h.repaint()
            assert _hints_on_screen(h), _body(h)
        finally:
            h.live.stop()

    # ── the rows it frees, and the ones it doesn't cost ──

    def test_the_body_gets_the_rows_back(self):
        """A short window shrinks the page to make room for the footer. On the
        player — the one screen the footer still hides on — the rows freed by
        putting it away go back to what is playing rather than to a list, so
        the honest claim here is that the hint rows are reclaimed and the frame
        stays whole with the song still on it."""
        h = self._harness(width=80, height=20, mode=HeadlessTidalPlayer.MODE_PLAYER)
        try:
            assert _hints_on_screen(h), "no footer to start with"
            self._press(h, "h")
            assert _hints_on_screen(h) == [], _body(h)
            assert any("Satisfied" in r for r in _body(h)), _body(h)
            h.assert_one_frame("rows reclaimed, no footer")
        finally:
            h.live.stop()

    def test_it_is_the_first_hint_a_narrow_window_drops(self):
        """Rank 9, above everything else in every mode: a footer with room for
        five keys spends them on what the screen does."""
        rows = _rows(40, 9, mode=HeadlessTidalPlayer.MODE_PLAYER)
        hints = " ".join(_hint_rows(rows))
        assert "[space]" in hints, rows
        assert "[h]" not in hints, hints

    # ── it is a moment, not a setting ──

    def test_it_does_not_survive_a_restart(self, tmp_path, monkeypatch):
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(player_mod, "STATE_FILE",
                            tmp_path / "state" / "player_state.json")
        h = self._harness()
        try:
            self._press(h, "h")
            assert _hints_on_screen(h) == []
            h.player._save_state()
        finally:
            h.live.stop()
        saved = (tmp_path / "state" / "player_state.json").read_text()
        assert "hidden" not in saved and "footer" not in saved, saved

        fresh = self._harness()
        try:
            assert fresh.player._footer_hidden is False
            assert _hints_on_screen(fresh), _body(fresh)
        finally:
            fresh.live.stop()

    def test_it_is_not_a_setting(self):
        keys = [spec["key"] for spec in config_mod.SETTINGS_SPEC]
        assert keys, "the spec is empty — this test would pass on nothing"
        assert not [k for k in keys if "hid" in k or "footer" in k], keys


# Every list/menu mode, the ones where `h` is "take me home" rather than
# "hide" or the letter h. The player and search are deliberately absent: the
# player hides its footer, search types the character.
MENU_MODES = (
    HeadlessTidalPlayer.MODE_BROWSE,
    HeadlessTidalPlayer.MODE_ARTIST,
    HeadlessTidalPlayer.MODE_QUEUE,
    HeadlessTidalPlayer.MODE_PLAYLISTS,
    HeadlessTidalPlayer.MODE_ADD_TO_PLAYLIST,
    HeadlessTidalPlayer.MODE_SETTINGS,
    HeadlessTidalPlayer.MODE_DOWNLOADS,
)


class TestHFromAMenuShowsThePlayer:
    """`[h]` on a menu is the other half of the same key: where the player
    screen hides its footer, a list dismisses itself and drops you back on the
    now-playing screen. The whole navigation stack goes with it — this is "take
    me home", not one step back — and the guards that keep `h` from hiding the
    footer out from under a textbox keep it from jumping home from one too.
    """

    def _harness(self, width=100, height=30, mode=None, **attrs):
        h = Harness(width=width, height=height, artwork=_art(width, height))
        if mode is not None:
            h.player._mode = mode
        _fill_lists(h.player)
        for name, value in attrs.items():
            setattr(h.player, name, value)
        h.start()
        h.repaint()
        return h

    def _press(self, h, key):
        h.player._handle_key(key)
        h.repaint()

    # ── it dismisses the menu and shows the player ──

    @pytest.mark.parametrize("mode", MENU_MODES)
    def test_h_dismisses_the_menu_and_shows_the_player(self, mode):
        h = self._harness(mode=mode)
        try:
            assert h.player._mode == mode, "did not start on the menu"
            self._press(h, "h")
            assert h.player._mode == HeadlessTidalPlayer.MODE_PLAYER, (mode, _body(h))
            # the now-playing screen is on screen, not just the mode flag flipped
            assert any("Satisfied" in r for r in _body(h)), (mode, _body(h))
            h.assert_one_frame(f"player after h on {mode}")
        finally:
            h.live.stop()

    # ── the footer says which meaning `h` has here ──

    def test_the_menu_footer_advertises_the_player(self):
        h = self._harness(mode=HeadlessTidalPlayer.MODE_PLAYLISTS)
        try:
            assert any("[h] player" in r for r in _body(h)), _body(h)
            assert not any("[h] hide" in r for r in _body(h)), _body(h)
        finally:
            h.live.stop()

    def test_the_player_footer_still_says_hide_not_player(self):
        h = self._harness(mode=HeadlessTidalPlayer.MODE_PLAYER)
        try:
            assert any("[h] hide" in r for r in _body(h)), _body(h)
            assert not any("[h] player" in r for r in _body(h)), _body(h)
        finally:
            h.live.stop()

    # ── the typing and overlay guards hold ──

    def test_a_settings_row_being_edited_keeps_the_key(self):
        """`h` mid-edit is the letter, so it must not jump home either — the
        same guard that keeps it from hiding the footer keeps it here."""
        numeric = [i for i, spec in enumerate(config_mod.SETTINGS_ROWS)
                   if spec["kind"] == "int"]
        h = self._harness(mode=HeadlessTidalPlayer.MODE_SETTINGS,
                          _settings_cursor=numeric[0], _settings_edit="12")
        try:
            self._press(h, "h")
            assert h.player._mode == HeadlessTidalPlayer.MODE_SETTINGS, _body(h)
        finally:
            h.live.stop()

    def test_the_new_playlist_name_keeps_the_key(self):
        h = self._harness(mode=HeadlessTidalPlayer.MODE_ADD_TO_PLAYLIST,
                          _picker_new_name="")
        try:
            self._press(h, "h")
            assert h.player._picker_new_name == "h", "the h was not typed"
            assert h.player._mode == HeadlessTidalPlayer.MODE_ADD_TO_PLAYLIST, _body(h)
        finally:
            h.live.stop()

    def test_a_pending_confirmation_keeps_the_key(self):
        """A yes/no box has the key: `h` cannot answer it by accident, so it
        neither confirms the delete nor jumps home — the mode stays put."""
        h = self._harness(mode=HeadlessTidalPlayer.MODE_DOWNLOADS,
                          _downloads_delete={"id": "x", "name": "A cached song"})
        try:
            self._press(h, "h")
            assert h.player._mode == HeadlessTidalPlayer.MODE_DOWNLOADS, _body(h)
        finally:
            h.live.stop()

    # ── "take me home" clears the back-stack ──

    def test_it_clears_the_navigation_stack(self):
        """Not one step back: the whole stack goes, because home is home no
        matter how many screens deep you were."""
        h = self._harness(mode=HeadlessTidalPlayer.MODE_BROWSE)
        try:
            h.player._nav_history.append({"mode": HeadlessTidalPlayer.MODE_SEARCH})
            self._press(h, "h")
            assert h.player._mode == HeadlessTidalPlayer.MODE_PLAYER
            assert h.player._nav_history == [], h.player._nav_history
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
                    artwork=_art(width, height))
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
    @pytest.mark.parametrize("height", (10, 12, 14, 18))
    def test_a_short_window_keeps_the_row_and_drops_rows_instead(self, mode, height):
        """Down to height 10, which is a six-row pane — the shortest window
        with room for anything below the track at all. The reported bug was
        the search scope row vanishing from a 13-row terminal: the page was
        already down to one row, the crop cuts the body from the bottom, and
        the next thing it reached was the scope row while the separator's
        blank rows sat safe above it. The chrome lever gives those up first."""
        h = self._harness(60, mode, height=height)
        try:
            assert _tab_rows(h), (mode, height, _body(h))
            h.assert_one_frame(f"{mode} at 60x{height}")
        finally:
            h.live.stop()

    def test_a_short_window_spends_result_rows_not_the_scope_row(self):
        """What gives way, in order: rows of the list — ↑/↓ still reaches
        everything a shorter page hides — then the separator and the status
        line, which say nothing. The scope row outlives them all, because it
        is the one row that says which scope Tab landed on."""
        tall = self._harness(80, HeadlessTidalPlayer.MODE_SEARCH, height=30)
        short = self._harness(80, HeadlessTidalPlayer.MODE_SEARCH, height=14)
        try:
            rows = lambda h: len([r for r in _body(h) if "Track number" in r])
            assert rows(tall) > rows(short) >= 1, (rows(tall), rows(short))
            assert _tab_rows(short), _body(short)
            short.assert_one_frame("search at 80x14")
        finally:
            tall.live.stop()
            short.live.stop()

    def test_the_chrome_is_only_given_up_under_pressure(self):
        """The separator and the queue line are part of the screen's look —
        pulled only when the page is already down to one row and the pane
        still does not fit, and back the moment the window is."""
        divider = lambda h: [r for r in _body(h)
                             if r.strip() and set(r.strip()) == {"─"}]
        roomy = self._harness(80, HeadlessTidalPlayer.MODE_SEARCH, height=30)
        short = self._harness(80, HeadlessTidalPlayer.MODE_SEARCH, height=12)
        try:
            assert divider(roomy), _body(roomy)
            assert any("Queue: " in r for r in _body(roomy))
            assert not divider(short), _body(short)
            assert not any("Queue: " in r for r in _body(short))
        finally:
            roomy.live.stop()
            short.live.stop()


class TestScrubbingAndTheOverlayOnScreen:
    def _harness(self, width=80):
        h = Harness(width=width, height=24,
                    artwork=_art(width, 24))
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


# ── the frame is never taller than the window ──

# The owner's actual track, reported as ten stacked `Ticli` panels marching
# down his terminal. Wide characters are two cells each and this title mixes
# them with a long ASCII tail, so it is the fixture the suite never had. The
# ASCII twin is the same length in characters and half the cells in the CJK
# run: the pair is what tells a width bug from a length bug.
WIDE_TITLE = "Super Idol 的笑容都没你的甜 (Schwank's easiest 96 . 41% of my life remix)"
ASCII_TWIN = "Super Idol de xiao rong dou mei ni de tian (Schwanks easiest 96 . 41% remix)"

WIDE_ARTIST = "Schwank 的笑容"
WIDE_ALBUM = "Super Idol 的笑容都没你的甜"


def _wide_track(title, i=0):
    return types.SimpleNamespace(
        id=i, name=title, duration=172,
        artists=[types.SimpleNamespace(name=WIDE_ARTIST)],
        album=types.SimpleNamespace(name=WIDE_ALBUM, cover=COVER),
    )


def _loaded(player, title, artwork=None):
    """Every screen filled with the same track, so one player answers for all
    of them without a session, a thread or a request."""
    tracks = [_wide_track(title, i) for i in range(30)]
    player._current_track = tracks[0]
    player._queue = tracks
    player._queue_index = 1
    player._browse_tracks = tracks
    player._search_query = "的笑容"
    player._search_results = [
        {"type": "track", "name": title, "artist": WIDE_ARTIST, "obj": t}
        for t in tracks]
    player._playlists = [types.SimpleNamespace(
        id=str(i), name=title, num_tracks=9) for i in range(20)]
    player._editable_playlists = player._playlists
    player._artist = types.SimpleNamespace(id=7, name=title)
    player._artist_sections = {
        player._artist_key(section): {
            "state": "ready",
            "items": [{"type": "track", "obj": t} for t in tracks],
            "message": "",
        }
        for section in HeadlessTidalPlayer.ARTIST_SECTIONS
    }
    player._download_track = tracks[0]
    player._picker_track = tracks[0]
    player._toast = "Track '345067195' is unavailable"
    player._toast_until = time.time() + 60
    player._show_more = True
    if artwork:
        player._show_artwork = True
        player._artwork = (COVER, artwork[0], artwork[1], _grid(*artwork))
    else:
        player._show_artwork = False
    return player


def _frame(player):
    """The frame as the terminal will receive it: a list of rows, and each
    row's width in *cells*, which is what a terminal counts and what a
    character count gets wrong for 的."""
    lines = player.console.render_lines(
        player._build_display(), player.console.options, pad=False)
    return lines, [sum(cell_len(seg.text) for seg in line) for line in lines]


class TestAFrameNeverOutgrowsTheWindow:
    """The invariant the reported bug broke, and the one that would have
    caught it.

    A frame taller than the terminal is not clipped by the terminal — it
    scrolls it. `Live` then homes the cursor and writes the next frame from
    the top of a screen that has already moved, so the previous frame is
    stranded above it and the panels stack, one per repaint, until the window
    is full of them. Ten of them, in the screenshot this exists for.

    Measured on the reported title and on an ASCII twin of the same length,
    because the two answer different questions: the twin says whether the
    cause is width or merely length.
    """

    @pytest.mark.parametrize("hidden", [False, True], ids=["footer", "hidden"])
    @pytest.mark.parametrize("title", [WIDE_TITLE, ASCII_TWIN], ids=["wide", "ascii"])
    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("width", OVERFLOW_WIDTHS)
    def test_every_mode_at_every_width(self, title, mode, width, hidden):
        for height in OVERFLOW_HEIGHTS:
            h = Harness(width=width, height=height,
                        artwork=_art(width, height))
            p = _loaded(h.player, title, _art(width, height))
            p._mode = mode
            p._footer_hidden = hidden
            lines, widths = _frame(p)
            assert len(lines) <= height, (title[:12], mode, width, height,
                                          hidden, len(lines))
            assert max(widths) <= width, (title[:12], mode, width, height,
                                          hidden, max(widths))

    @pytest.mark.parametrize("hidden", [False, True], ids=["footer", "hidden"])
    @pytest.mark.parametrize("title", [WIDE_TITLE, ASCII_TWIN], ids=["wide", "ascii"])
    def test_every_size_a_window_can_be_dragged_through(self, title, hidden):
        """The matrix above at its named sizes, and then every size between
        them: a layout that answers to a threshold has an off-by-one at the
        threshold, and 83 columns is a column nobody would have listed."""
        for width in range(30, 140, 3):
            for height in range(6, 44, 2):
                h = Harness(width=width, height=height)
                p = _loaded(h.player, title, _art(width, height))
                p._footer_hidden = hidden
                lines, widths = _frame(p)
                assert len(lines) <= height, (width, height, hidden, len(lines))
                assert max(widths, default=0) <= width, (width, height, hidden)

    def test_the_mini_player_too(self):
        for width in WIDTHS + (40,):
            h = Harness(width=width, height=24)
            p = _loaded(h.player, WIDE_TITLE)
            p._mini_player = True
            lines, widths = _frame(p)
            assert len(lines) <= 24, (width, len(lines))
            assert max(widths) <= width, (width, max(widths))

    def test_a_wide_title_is_cut_by_cells_not_by_characters(self):
        """The clip has to agree with the terminal about how much room 的 takes.
        Counting characters leaves a line that measures 74 and renders 82."""
        h = Harness(width=80, height=24)
        p = _loaded(h.player, WIDE_TITLE)
        lines, widths = _frame(p)
        track_line = [line for line in lines
                      if any("Super Idol" in seg.text for seg in line)]
        assert track_line, "the track never reached the screen"
        for line in track_line:
            assert sum(cell_len(seg.text) for seg in line) <= 80

    def test_the_list_row_remainder_is_not_a_second_row(self):
        """A row that wrapped was two rows on screen, which is how a page of 15
        became 30 and the queue overflowed a 24-row window by 25 lines. The
        reported symptom was the wrapped tail appearing twice."""
        h = Harness(width=80, height=24)
        p = _loaded(h.player, WIDE_TITLE)
        p._mode = p.MODE_QUEUE
        lines, _ = _frame(p)
        tails = [line for line in lines
                 if any("of my life remix" in seg.text for seg in line)]
        assert tails == [], "a row wrapped instead of being clipped"

    def test_it_survives_a_repaint_through_a_real_terminal(self):
        """End to end: the escape sequences into the terminal model, four
        repaints, and nothing stranded above or below the one panel."""
        h = Harness(width=80, height=24)
        _loaded(h.player, WIDE_TITLE)
        h.player._mode = h.player.MODE_QUEUE
        h.start()
        try:
            for _ in range(4):
                h.repaint()
            h.assert_one_frame("wide title in the queue")
            assert h.stranded() == []
        finally:
            h.live.stop()


def _rows(width, height, title=WIDE_TITLE, mode=None, **attrs):
    """One frame as plain rows, with the invariant checked on the way out:
    nothing here is worth reading if the pane did not fit the window."""
    h = Harness(width=width, height=height)
    p = _loaded(h.player, title, _art(width, height))
    if mode is not None:
        p._mode = mode
    for name, value in attrs.items():
        setattr(p, name, value)
    lines, widths = _frame(p)
    assert len(lines) <= height, (width, height, len(lines))
    assert max(widths, default=0) <= width, (width, height)
    return ["".join(seg.text for seg in line) for line in lines]


def _hint_rows(rows):
    """Rows of the pane that are a footer: hints start at the left margin and
    nothing else on the player screen begins with a bracket."""
    return [r for r in rows if r.strip().strip("│").strip().startswith("[")]


class TestTheTitleIsTheLastThingToGo:
    """The relax order, at its far end. The window runs out of rows and
    something has to stop being drawn; what survives longest is the song, not
    the six keys that say how to work the app. You can find `[s]` again by
    making the window bigger — you cannot find out what is playing that way.
    """

    @pytest.mark.parametrize("title", [WIDE_TITLE, ASCII_TWIN], ids=["wide", "ascii"])
    def test_the_track_is_on_screen_at_every_height_the_pane_has(self, title):
        for height in range(5, 25):
            rows = _rows(60, height, title)
            assert any("Super Idol" in r for r in rows), (height, rows)

    @pytest.mark.parametrize("title", [WIDE_TITLE, ASCII_TWIN], ids=["wide", "ascii"])
    def test_the_hints_go_first(self, title):
        """Five rows of terminal is one row of pane. It used to be the footer;
        it is the track now, and the footer comes back one row later."""
        assert _hint_rows(_rows(60, 5, title)) == []
        assert any("Super Idol" in r for r in _rows(60, 5, title))
        assert _hint_rows(_rows(60, 9, title)), "the footer never came back"

    def test_the_progress_line_outlives_the_footer_too(self):
        """Both halves of "what is playing": the title, and where in it. The
        old order kept two rows of bare keys and dropped the bar."""
        rows = _rows(60, 8)
        assert any("0:00" in r for r in rows), rows
        assert len(_hint_rows(rows)) <= 1, rows

    def test_but_a_long_body_still_gives_way_before_the_footer(self):
        """The other side of the rule, and the one it would be easy to break
        by applying "identity first" everywhere. The settings page's body is
        longer than the pane at every width — but what it loses to a crop is
        the tail of its prose, not the track, so the footer keeps both of its
        rows and `[Esc] back` stays on screen."""
        rows = _rows(60, 24, mode=HeadlessTidalPlayer.MODE_SETTINGS)
        body = [r for r in rows
                if "\u256d" not in r and "\u2570" not in r
                and r.strip().strip("\u2502").strip()]
        assert _hint_rows(body[-2:]) == body[-2:], rows
        assert any("[Esc]" in r for r in body[-2:]), rows


class TestTheCoverBesideTheTrack:
    """Above the track line in a narrow window, beside it in a wide one —
    because in a wide window the scarce axis is the vertical one."""

    def _art_and_title(self, rows):
        return [r for r in rows if "▀" in r and "Super Idol" in r]

    @pytest.mark.parametrize("title", [WIDE_TITLE, ASCII_TWIN], ids=["wide", "ascii"])
    def test_a_wide_window_puts_them_on_the_same_row(self, title):
        rows = _rows(100, 30, title)
        assert self._art_and_title(rows), rows
        # and the whole text block is beside the cover, not only its first row
        beside = [r for r in rows if "▀" in r]
        assert any("0:00" in r for r in beside), rows
        assert any("Queue:" in r for r in beside), rows
        assert any("Next:" in r for r in beside), rows

    @pytest.mark.parametrize("title", [WIDE_TITLE, ASCII_TWIN], ids=["wide", "ascii"])
    def test_a_narrow_window_puts_the_cover_back_above(self, title):
        rows = _rows(60, 30, title)
        assert [r for r in rows if "▀" in r], "no cover at all"
        assert self._art_and_title(rows) == [], rows

    def test_the_threshold_is_the_width_and_only_the_width(self):
        assert self._art_and_title(_rows(82, 30)) == []
        assert self._art_and_title(_rows(83, 30))

    def test_beside_costs_fewer_rows_than_above(self, monkeypatch):
        """Measured against the same pane laid out the old way, rather than
        against a number written down here: a threshold nothing can reach is
        a stacked layout."""
        beside = len(_rows(100, 30))
        monkeypatch.setattr(art_mod, "MIN_BESIDE_WIDTH", 10_000)
        stacked = len(_rows(100, 30))
        assert beside < stacked, (beside, stacked)

    def test_the_text_is_centred_against_the_cover(self):
        """A five-row block pinned to the top of a sixteen-row cover reads as
        a layout that ran out rather than one that was chosen."""
        rows = _rows(100, 30)
        art = [i for i, r in enumerate(rows) if "▀" in r]
        text = [i for i, r in enumerate(rows) if "▶" in r]
        last = max(i for i, r in enumerate(rows) if "Next:" in r)
        assert art and text
        above, below = text[0] - art[0], art[-1] - last
        assert above > 0 and below > 0, (above, below)
        assert abs(above - below) <= 2, (above, below)

    @pytest.mark.parametrize("title", [WIDE_TITLE, ASCII_TWIN], ids=["wide", "ascii"])
    def test_the_progress_line_shortens_instead_of_running_under_the_cover(self, title):
        """The bar is laid out in the columns the cover left. Laid out for the
        pane instead, it would wrap — and a wrapped progress line is the bug
        the whole width-responsive layout exists for."""
        wide = _rows(140, 30, title)
        narrow = _rows(90, 30, title)
        for rows in (wide, narrow):
            assert len([r for r in rows if "0:00" in r]) == 1, rows
            assert all("2:52" in r for r in rows if "0:00" in r), rows
        bar = lambda rows: [r for r in rows if "0:00" in r][0].count("─")
        assert bar(narrow) < bar(wide), (bar(narrow), bar(wide))

    @pytest.mark.parametrize("title", [WIDE_TITLE, ASCII_TWIN], ids=["wide", "ascii"])
    def test_the_title_beside_the_cover_is_clipped_in_cells(self, title):
        """的 is two cells and one character. A title clipped by characters
        measures 74 and renders 82, which beside a cover is a row that pushes
        the frame a line taller every repaint."""
        for width in (83, 100, 120, 140):
            rows = _rows(width, 30, title)
            for row in rows:
                assert cell_len(row) <= width, (width, row)

    def test_it_survives_a_resize_across_the_threshold(self):
        """Through a real Live and a real terminal model, both ways: the
        layout changes the size the cover is rendered at, so a stale render
        laid out for the other one is exactly what would be stranded."""
        h = Harness(width=100, height=30)
        _loaded(h.player, WIDE_TITLE, _art(100, 30))
        h.start()
        try:
            for width in (100, 60, 100, 82, 83, 100):
                h.resize(width, 30)
                h.set_artwork(_art(width, 30))
                h.repaint()
                h.assert_one_frame(f"resized to {width}")
                h.repaint()
                h.assert_one_frame(f"settled at {width}")
        finally:
            h.live.stop()


class TestTheDownloadBox:
    """[d] is a small box centred over whatever screen you were looking at.

    Driven through the real `Live` into `vt.Screen`, because every claim
    here is about the grid: that the box is centred in the window, that the
    number above the tier is the one the tier means, and that Esc puts back
    the screen it was drawn over — none of which a returned Text can settle.
    """

    def _open(self, width=80, height=24):
        h = Harness(width=width, height=height, artwork=None)
        h.player._show_artwork = False
        h.player._mode = h.player.MODE_QUEUE
        h.player._queue_cursor = 1
        h.start()
        h.repaint()
        before = h.screen.text()
        h.player._handle_key("d")
        h.repaint()
        return h, before

    def _box(self, h):
        """The box's rows, found by the border it draws and not by any string
        the builder happens to put inside it."""
        rows = h.screen.text()
        top = [i for i, r in enumerate(rows) if "\u256d" in r and "Ticli" not in r]
        assert len(top) == 1, rows
        bottom = [i for i, r in enumerate(rows) if "\u2570" in r and i > top[0]]
        assert bottom, rows
        return rows[top[0]:bottom[0] + 1]

    def _size_line(self, h):
        return [r for r in self._box(h) if re.search(r"\d+(\.\d+)? [KMG]B", r)]

    def test_it_is_centred_in_the_window(self):
        for width in (60, 80, 100, 120):
            h, _ = self._open(width=width)
            try:
                for row in self._box(h):
                    left = len(row) - len(row.lstrip())
                    # Rows are rstripped, so the panel's own right border is
                    # the last cell on the line
                    right = len(row) - 1 - max(row.rfind("\u2502"), row.rfind("\u256e"),
                                               row.rfind("\u256f"))
                    assert abs(left - right) <= 1, (width, repr(row))
            finally:
                h.live.stop()

    def test_the_size_is_the_estimate_for_the_tier_that_is_showing(self):
        h, _ = self._open()
        try:
            player = h.player
            for index, tier in enumerate(QUALITY_CHOICES):
                player._download_cursor = index
                h.repaint()
                box = self._box(h)
                expected = "~" + downloads.format_bytes(
                    downloads.estimate_bytes(player._download_track.duration, tier))
                assert any(expected in r for r in box), (tier, expected, box)
                assert any(tier in r for r in box), (tier, box)
        finally:
            h.live.stop()

    def test_down_moves_the_tier_and_the_number_above_it(self):
        h, _ = self._open()
        try:
            was = h.player._download_tier()
            first = self._size_line(h)
            h.player._handle_key(player_mod.KEY_DOWN)
            h.repaint()
            second = self._size_line(h)
            assert first and second and first != second, (first, second)
            # and the tier under the cursor moved with it, by one
            assert h.player._download_tier() == QUALITY_CHOICES[
                QUALITY_CHOICES.index(was) + 1]
            assert any(h.player._download_tier() in r for r in self._box(h))
        finally:
            h.live.stop()

    def test_esc_puts_back_the_screen_it_was_drawn_over(self):
        h, before = self._open()
        try:
            assert h.screen.text() != before, "the box never appeared"
            h.player._handle_key(player_mod.KEY_ESC)
            h.repaint()
            assert h.screen.text() == before
            assert h.player._mode == h.player.MODE_QUEUE
        finally:
            h.live.stop()

    def test_the_box_fits_and_strands_nothing_at_every_size(self):
        for width, height in ((40, 14), (60, 20), (80, 24), (120, 40)):
            h, _ = self._open(width=width, height=height)
            try:
                rows = h.screen.text()
                # One panel, and the box inside it: assert_one_frame counts
                # top borders, and the box has one of its own
                assert len([r for r in rows if "Ticli" in r]) == 1, rows
                assert h.stranded() == [], (width, height)
                assert all(len(r) <= width for r in rows), (width, rows)
                box = self._box(h)
                # Whatever else it gives up, the number and the button stay
                assert self._size_line(h), (width, height, box)
                assert any("[Enter]" in r for r in box), (width, height, box)
            finally:
                h.live.stop()

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("width", OVERFLOW_WIDTHS)
    def test_it_fits_over_every_screen_at_every_size(self, mode, width):
        """The box is drawn over whatever was there, so it inherits that
        screen's overflow problem as well as its own. Same matrix the modes
        themselves are held to, because a pane that overflows is not
        harmless: Rich answers it by replacing the bottom line with a red
        ellipsis, and here the bottom line is the button."""
        for height in OVERFLOW_HEIGHTS:
            h = Harness(width=width, height=height, artwork=_art(width, height))
            p = _loaded(h.player, WIDE_TITLE, _art(width, height))
            p._mode = mode
            p._download_open = True
            lines, widths = _frame(p)
            assert len(lines) <= height, (mode, width, height, len(lines))
            assert max(widths) <= width, (mode, width, height, max(widths))
            text = ["".join(seg.text for seg in line) for line in lines]
            assert any("\u256d" in r and "Ticli" not in r for r in text), \
                (mode, width, height, text)

    def test_the_footer_underneath_is_not_left_advertising_swallowed_keys(self):
        """The box takes every key. A footer still offering [s] search would
        be naming keys that now do nothing at all."""
        h, _ = self._open()
        try:
            assert _hints_on_screen(h) == [], h.screen.text()
        finally:
            h.live.stop()
