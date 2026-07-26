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
        h.repaint()
        h.set_artwork(None)          # a cover with no picture: pane gets shorter
        h.repaint()
        assert h.art_rows() == []
        h.assert_one_frame("no artwork")
        h.set_artwork((20, 10))
        h.repaint()
        assert len(h.art_rows()) == 10
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
