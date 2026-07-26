"""Tests for album artwork as terminal pixel art.

Nothing here touches the network or the real cache directory: the two JPEG
fixtures are decoded from disk, every fetch is a fake, and CACHE_DIR is
redirected to tmp_path. The fixtures are the same generated image twice —
`cover_baseline.jpg` is baseline 4:2:0, `cover_progressive.jpg` is that file
run through jpegtran -progressive — which is what lets one test assert the
two code paths agree, and which matters because TIDAL's CDN serves the
progressive kind.
"""

import threading
import time
import types

import pytest
from rich.console import Console

from ticli.player import HeadlessTidalPlayer
from ticli.utils import artwork as art_mod
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod

FIXTURES = cache_mod.Path(__file__).parent / "fixtures"
BASELINE = (FIXTURES / "cover_baseline.jpg").read_bytes()
PROGRESSIVE = (FIXTURES / "cover_progressive.jpg").read_bytes()

COVER = "3f2d1c0b-1111-2222-3333-444455556666"


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """No test may read or write the owner's real cache or config."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config" / "config.json")
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    return tmp_path


def _wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return False


def _grid(cols, rows, colour=(10, 20, 30)):
    return [[colour] * cols for _ in range(rows * 2)]


def _no_ffmpeg(monkeypatch):
    """A machine with no ffmpeg — mpv users need not have one."""
    def _boom(*a, **kw):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(art_mod.subprocess, "run", _boom)


# ── the decoder ──


class TestDecode:
    def test_baseline_jpeg_decodes_at_eighth_scale(self):
        pixels = art_mod.decode_jpeg_dc(BASELINE)
        # 320x320 source, one pixel per 8x8 block
        assert len(pixels) == 40
        assert all(len(row) == 40 for row in pixels)
        assert all(0 <= c <= 255 for row in pixels for px in row for c in px)

    def test_progressive_jpeg_decodes(self):
        """What TIDAL actually serves. Its first scan is the DC scan, so the
        decoder reads that one scan and never touches the AC scans at all."""
        pixels = art_mod.decode_jpeg_dc(PROGRESSIVE)
        assert len(pixels) == 40 and len(pixels[0]) == 40

    def test_both_encodings_of_one_image_agree(self):
        baseline = art_mod.decode_jpeg_dc(BASELINE)
        progressive = art_mod.decode_jpeg_dc(PROGRESSIVE)
        worst = max(
            abs(baseline[y][x][c] - progressive[y][x][c])
            for y in range(40) for x in range(40) for c in range(3)
        )
        assert worst <= 4, "the two scan paths disagree about the same image"

    def test_image_is_not_uniform(self):
        """A decoder that returned grey everywhere would pass the shape
        assertions above. The fixture is a gradient, so it must not."""
        pixels = art_mod.decode_jpeg_dc(PROGRESSIVE)
        seen = {px for row in pixels for px in row}
        assert len(seen) > 50

    def test_not_a_jpeg(self):
        with pytest.raises(art_mod.ArtworkError):
            art_mod.decode_jpeg_dc(b"GIF89a" + b"\x00" * 100)

    def test_truncated_file(self):
        cut = PROGRESSIVE.index(b"\xff\xda") + 20  # a few bytes into the scan
        with pytest.raises(art_mod.ArtworkError):
            art_mod.decode_jpeg_dc(PROGRESSIVE[:cut])

    def test_half_a_progressive_file_is_still_a_picture(self):
        """The DC scan comes first, so most of the download is refinement the
        decoder never reads — which is also why it is fast."""
        pixels = art_mod.decode_jpeg_dc(PROGRESSIVE[:len(PROGRESSIVE) // 2])
        assert len(pixels) == 40

    def test_unsupported_jpeg_flavour(self):
        """Arithmetic coding, lossless, 12-bit: named and refused, not
        misread. 0xC3 is SOF3 (lossless)."""
        data = bytearray(PROGRESSIVE)
        data[data.index(b"\xff\xc2") + 1] = 0xC3
        with pytest.raises(art_mod.ArtworkError):
            art_mod.decode_jpeg_dc(bytes(data))

    def test_decode_failure_is_not_fatal(self, monkeypatch):
        """Every caller gets None, never an exception — artwork is
        decoration and may not take the player down with it."""
        _no_ffmpeg(monkeypatch)
        assert art_mod.decode(b"not an image at all", 12, 6) is None
        assert art_mod.decode(b"", 12, 6) is None
        assert art_mod.decode(PROGRESSIVE[:40], 12, 6) is None

    def test_decode_produces_the_requested_size(self):
        pixels = art_mod.decode(PROGRESSIVE, 20, 10)
        assert len(pixels) == 20, "two pixel rows per cell row"
        assert all(len(row) == 20 for row in pixels)


class TestFfmpegFallback:
    def test_used_when_the_builtin_decoder_declines(self, monkeypatch):
        calls = []

        def _run(cmd, **kw):
            calls.append(cmd)
            return types.SimpleNamespace(
                returncode=0, stdout=bytes([7, 8, 9] * (12 * 12)))

        monkeypatch.setattr(art_mod.subprocess, "run", _run)
        pixels = art_mod.decode(b"unreadable", 12, 6)
        assert calls, "ffmpeg was never tried"
        assert pixels[0][0] == (7, 8, 9)

    def test_missing_ffmpeg_is_an_answer_not_a_crash(self, monkeypatch):
        _no_ffmpeg(monkeypatch)
        assert art_mod.decode_with_ffmpeg(b"x", 4, 4) is None

    def test_short_output_is_rejected(self, monkeypatch):
        monkeypatch.setattr(
            art_mod.subprocess, "run",
            lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout=b"\x00"))
        assert art_mod.decode_with_ffmpeg(b"x", 4, 4) is None


class TestResample:
    def test_box_average(self):
        pixels = [[(0, 0, 0), (100, 100, 100)] for _ in range(2)]
        assert art_mod.resample(pixels, 1, 1) == [[(50, 50, 50)]]

    def test_exact_shape(self):
        out = art_mod.resample(_grid(40, 20), 20, 10)
        assert len(out) == 10 and len(out[0]) == 20

    def test_empty_image(self):
        with pytest.raises(art_mod.ArtworkError):
            art_mod.resample([], 4, 4)


# ── rendering ──


class TestRender:
    def test_half_blocks_carry_two_pixels_per_cell(self):
        pixels = [[(255, 0, 0)] * 3, [(0, 0, 255)] * 3]
        text = art_mod.render(pixels)
        assert str(text) == "▀▀▀"
        style = text.spans[0].style
        assert "#ff0000" in style and "on #0000ff" in style

    def test_one_line_per_two_pixel_rows(self):
        text = art_mod.render(_grid(20, 10))
        assert str(text).count("\n") == 9
        assert all(len(line) == 20 for line in str(text).splitlines())

    def test_indent_does_not_widen_the_picture(self):
        text = art_mod.render(_grid(16, 8), indent=3)
        lines = str(text).splitlines()
        assert all(line.startswith("   ") and len(line) == 19 for line in lines)

    def test_truecolor_and_256_render_the_same_text(self):
        """Rich downgrades 24-bit colour to the terminal's palette itself, so
        a 256-colour terminal gets real artwork with no separate code path."""
        text = art_mod.render(_grid(8, 4))
        wide = Console(width=40, color_system="truecolor").render_lines(text)
        narrow = Console(width=40, color_system="256").render_lines(text)
        assert "".join(s.text for s in wide[0]).strip() == "▀" * 8
        assert "".join(s.text for s in narrow[0]).strip() == "▀" * 8

    def test_colour_support_detection(self):
        assert art_mod.supports_art(Console(color_system="truecolor"))
        assert art_mod.supports_art(Console(color_system="256"))
        assert not art_mod.supports_art(Console(color_system="standard"))
        assert not art_mod.supports_art(Console(color_system=None))
        assert not art_mod.supports_art(object())


def _size(width, height):
    """The size and placement this window gets, asked the way the player asks
    it: the two are one decision (see art_size)."""
    beside = art_mod.art_beside(width, height)
    return art_mod.art_size(width, height, beside), beside


class TestArtSize:
    def test_a_normal_terminal_gets_the_big_size(self):
        assert art_mod.art_size(80, 26) == (20, 10)

    def test_a_short_terminal_gets_a_smaller_one(self):
        assert art_mod.art_size(80, 24) == (16, 8)
        assert art_mod.art_size(80, 22) == (12, 6)

    def test_no_room_at_all(self):
        assert art_mod.art_size(80, 20) is None
        assert art_mod.art_size(20, 60) is None

    def test_a_wide_terminal_gets_a_bigger_one(self):
        assert art_mod.art_size(100, 60) == (32, 16)
        assert art_mod.art_size(60, 60) == (32, 16)

    def test_the_size_steps_down_with_the_width(self):
        """Width, not only height: a tall narrow window used to keep the
        20-column cover and cramp everything beside it. Three fifths of it
        now rather than two, which is why 50 columns keeps a 28-column cover
        where it used to step down to 20."""
        assert art_mod.art_size(50, 60) == (28, 14)
        assert art_mod.art_size(40, 60) == (24, 12)
        assert art_mod.art_size(30, 60) == (16, 8)
        assert art_mod.art_size(29, 60) == (16, 8)
        assert art_mod.art_size(20, 60) is None

    def test_the_picture_never_takes_most_of_the_width(self):
        """Stacked, the cap is a share of the window. Beside the text it is a
        budget instead — everything the margin, the gutter and the text
        column have not already claimed — because there the two are competing
        for the same columns rather than merely sharing a pane."""
        for height in (20, 24, 40, 60):
            for width in range(10, 200):
                size, beside = _size(width, height)
                if size is None:
                    continue
                if beside:
                    assert (size[0] + art_mod.ART_MARGIN + art_mod.ART_GUTTER
                            + art_mod.MIN_TEXT_WIDTH) <= width, (width, height)
                else:
                    assert size[0] <= width * art_mod.ART_WIDTH_SHARE

    def test_the_absolute_ceiling_did_not_move(self):
        """The share was raised, not the largest cover there is: a wide window
        looks exactly as it did."""
        for height in range(12, 80):
            for width in range(10, 400):
                size, _ = _size(width, height)
                if size is not None:
                    assert size[0] <= 32 and size[1] <= 16, (width, height, size)

    def test_the_picture_always_fits_the_pane(self):
        """Panel border and padding take six columns and the indent three;
        art that overran them would wrap, which reads as corruption."""
        for height in (16, 20, 22, 24, 40, 60):
            for width in range(10, 120):
                size, _ = _size(width, height)
                if size is not None:
                    assert size[0] + 3 <= width - 6, (width, height, size)


class TestWhereTheCoverSits:
    """Above the track line in a narrow window, beside it in a wide one."""

    def test_the_threshold_is_where_the_biggest_cover_fits_beside_the_text(self):
        assert art_mod.MIN_BESIDE_WIDTH == 83
        assert not art_mod.art_beside(82, 40)
        assert art_mod.art_beside(83, 40)

    def test_beside_costs_fewer_rows_and_gives_a_bigger_cover(self):
        """The point of the move. At 100x30 the cover goes up a size *and*
        the pane it sits in gets shorter, because the track line, the bar and
        the next-track line are inside the picture's rows instead of under
        them."""
        assert art_mod.art_size(100, 30, False) == (28, 14)
        assert art_mod.art_size(100, 30, True) == (32, 16)
        stacked = 14 + 2 + 5      # cover, gap, the five text rows
        beside = max(16, 5)
        assert beside < stacked

    def test_a_short_wide_window_gets_a_cover_where_it_used_to_get_none(self):
        assert art_mod.art_size(100, 20, False) is None
        assert art_mod.art_beside(100, 20)
        assert art_mod.art_size(100, 20, True) == (16, 8)

    def test_widening_the_window_never_shrinks_the_cover(self):
        """The property MIN_BESIDE_WIDTH is derived from, and the reason it is
        derived rather than chosen: at 71 — wide enough to *look* like enough —
        crossing the threshold took a 28-column cover down to 20, so dragging
        the window one column wider made the picture smaller. Checked at every
        width and height rather than at a few, because the failure was a single
        column."""
        for height in range(10, 80):
            widest = 0
            for width in range(10, 240):
                size, _ = _size(width, height)
                cols = size[0] if size else 0
                assert cols >= widest, (width, height, widest, cols)
                widest = cols

    def test_a_taller_window_never_shrinks_it_either(self):
        for width in range(10, 240):
            tallest = 0
            for height in range(6, 80):
                size, _ = _size(width, height)
                cols = size[0] if size else 0
                assert cols >= tallest, (width, height, tallest, cols)
                tallest = cols


# ── the disk cache ──


class TestArtCache:
    def test_round_trip(self):
        pixels = art_mod.decode(PROGRESSIVE, 12, 6)
        art_mod.write_art(COVER, 12, 6, pixels)
        assert art_mod.read_art(COVER, 12, 6) == pixels

    def test_keyed_on_size_so_a_resize_re_renders(self):
        art_mod.write_art(COVER, 12, 6, _grid(12, 6))
        assert art_mod.read_art(COVER, 12, 6) is not None
        assert art_mod.read_art(COVER, 20, 10) is None, \
            "a different cell size is a different picture"

    def test_keyed_on_cover(self):
        art_mod.write_art(COVER, 12, 6, _grid(12, 6))
        assert art_mod.read_art("some-other-cover", 12, 6) is None

    def test_written_privately_and_atomically(self):
        art_mod.write_art(COVER, 12, 6, _grid(12, 6))
        path = art_mod.art_path(COVER, 12, 6)
        assert path.stat().st_mode & 0o777 == 0o600
        assert not list(path.parent.glob("*.tmp")), "left a temp file behind"

    def test_a_corrupt_entry_is_a_miss_not_a_crash(self):
        art_mod.write_art(COVER, 12, 6, _grid(12, 6))
        art_mod.art_path(COVER, 12, 6).write_text("ticli-art 1 12 6\nzzzz\n")
        assert art_mod.read_art(COVER, 12, 6) is None

    def test_a_binary_entry_is_a_miss(self):
        art_mod.write_art(COVER, 12, 6, _grid(12, 6))
        art_mod.art_path(COVER, 12, 6).write_bytes(b"\xff\xd8\xff\xe0\x00")
        assert art_mod.read_art(COVER, 12, 6) is None

    def test_a_future_format_is_a_miss(self):
        art_mod.write_art(COVER, 12, 6, _grid(12, 6))
        path = art_mod.art_path(COVER, 12, 6)
        path.write_text(path.read_text().replace("ticli-art 1", "ticli-art 9", 1))
        assert art_mod.read_art(COVER, 12, 6) is None

    def test_an_unwritable_cache_is_survivable(self, monkeypatch):
        monkeypatch.setattr(cache_mod, "CACHE_DIR", cache_mod.Path("/proc/nope"))
        art_mod.write_art(COVER, 12, 6, _grid(12, 6))  # must not raise
        assert art_mod.read_art(COVER, 12, 6) is None

    def test_a_cover_id_can_never_name_a_path(self):
        assert art_mod.art_path("../../etc/passwd", 12, 6) is None
        assert art_mod.art_path("", 12, 6) is None

    def test_eviction_keeps_the_directory_small(self, monkeypatch):
        monkeypatch.setattr(art_mod, "MAX_ART_FILES", 3)
        for n in range(6):
            art_mod.write_art(f"cover{n}", 4, 2, _grid(4, 2))
            time.sleep(0.01)  # distinct mtimes, so "oldest" means something
        left = sorted(p.name for p in cache_mod.artwork_dir().iterdir())
        assert len(left) == 3
        assert left[0].startswith("cover3"), "the oldest should have gone first"

    def test_artwork_does_not_disturb_the_audio_budget(self):
        """Artwork lives beside the audio, not in it: the song count, the
        bytes the settings page reports and the eviction list must not move."""
        cache = cache_mod.MetadataCache()
        cache_mod.audio_dir().mkdir(parents=True, exist_ok=True)
        (cache_mod.audio_dir() / "42.m4a").write_bytes(b"x" * 100)
        before = (cache.audio_count(), cache.total_bytes(),
                  len(cache_mod.owned_audio_files()))
        art_mod.write_art(COVER, 20, 10, _grid(20, 10))
        cache.invalidate_audio_count()
        after = (cache.audio_count(), cache.total_bytes(),
                 len(cache_mod.owned_audio_files()))
        assert before == after

    def test_clearing_the_cache_clears_the_artwork(self):
        art_mod.write_art(COVER, 12, 6, _grid(12, 6))
        stranger = cache_mod.artwork_dir() / "somebody-elses.png"
        stranger.write_bytes(b"not ours")
        assert cache_mod.MetadataCache().clear() is None
        assert art_mod.read_art(COVER, 12, 6) is None
        assert stranger.exists(), "only files ticli wrote may be deleted"

    def test_clear_artwork_on_an_empty_machine(self):
        assert cache_mod.clear_artwork() == 0


class TestLoad:
    def test_fetches_decodes_and_caches(self):
        calls = []

        def _fetch(url):
            calls.append(url)
            return PROGRESSIVE

        pixels = art_mod.load(COVER, 12, 6, fetch=_fetch)
        assert pixels is not None and len(pixels) == 12
        assert calls == [
            "https://resources.tidal.com/images/"
            "3f2d1c0b/1111/2222/3333/444455556666/320x320.jpg"
        ]
        # Second time is free: the rendering is on disk
        assert art_mod.load(COVER, 12, 6, fetch=_fetch) == pixels
        assert len(calls) == 1

    def test_a_failed_fetch_is_not_fatal(self):
        def _fetch(url):
            raise OSError("offline")

        assert art_mod.load(COVER, 12, 6, fetch=_fetch) is None
        assert art_mod.read_art(COVER, 12, 6) is None, "nothing was cached"

    def test_an_undecodable_image_is_not_cached(self, monkeypatch):
        _no_ffmpeg(monkeypatch)
        assert art_mod.load(COVER, 12, 6, fetch=lambda url: b"junk") is None
        assert art_mod.read_art(COVER, 12, 6) is None

    def test_no_cover_no_request(self):
        def _fetch(url):
            raise AssertionError("should never fetch")

        assert art_mod.load("", 12, 6, fetch=_fetch) is None

    def test_cover_id_off_a_track(self):
        track = types.SimpleNamespace(
            album=types.SimpleNamespace(name="A", cover=COVER))
        assert art_mod.cover_id_of(track) == COVER
        # A cached row carries a name and no cover, and a single can have no
        # album at all — both are ordinary
        assert art_mod.cover_id_of(
            types.SimpleNamespace(album=types.SimpleNamespace(name="A"))) is None
        assert art_mod.cover_id_of(types.SimpleNamespace(album=None)) is None
        assert art_mod.cover_id_of(None) is None


# ── the player ──


def _player(console=None, cover=COVER):
    player = HeadlessTidalPlayer()
    player.console = console or Console(
        width=80, height=26, color_system="truecolor")
    album = types.SimpleNamespace(name="An Album", cover=cover)
    player._current_track = types.SimpleNamespace(
        id=1, name="A Track", duration=200, artists=[], album=album)
    return player


class TestPlayerArtwork:
    def test_the_first_paint_never_waits_for_the_network(self, monkeypatch):
        """The fetch is a daemon thread: the paint that starts it returns at
        once, and the picture arrives on a later one."""
        started = threading.Event()

        def _slow_load(cover, cols, rows, **kw):
            started.set()
            time.sleep(0.4)
            return _grid(cols, rows)

        monkeypatch.setattr(art_mod, "load", _slow_load)
        player = _player()
        start = time.monotonic()
        assert player._artwork_text() is None
        assert time.monotonic() - start < 0.1, "the UI thread blocked"
        assert started.wait(2)
        assert _wait_for(lambda: player._artwork is not None)
        assert player._artwork_text() is not None

    def test_the_arriving_picture_asks_for_a_repaint(self, monkeypatch):
        """Live runs with auto_refresh off, so artwork that lands in the
        background has to knock (see _wake) or it waits for the idle tick."""
        woken = []
        monkeypatch.setattr(art_mod, "load",
                            lambda cover, cols, rows, **kw: _grid(cols, rows))
        player = _player()
        monkeypatch.setattr(player, "_wake", lambda: woken.append(True))
        player._artwork_text()
        assert _wait_for(lambda: woken)

    def test_one_request_per_cover_and_size(self, monkeypatch):
        calls = []

        def _load(cover, cols, rows, **kw):
            calls.append((cover, cols, rows))
            return _grid(cols, rows)

        monkeypatch.setattr(art_mod, "load", _load)
        player = _player()
        for _ in range(5):
            player._artwork_text()
        assert _wait_for(lambda: calls)
        time.sleep(0.05)
        for _ in range(5):
            player._artwork_text()
        assert len(calls) == 1, "repainting must not re-fetch"

    def test_a_resize_re_renders(self, monkeypatch):
        sizes = []

        def _load(cover, cols, rows, **kw):
            sizes.append((cols, rows))
            return _grid(cols, rows)

        monkeypatch.setattr(art_mod, "load", _load)
        player = _player()
        player._artwork_text()
        assert _wait_for(lambda: player._artwork is not None)
        player.console = Console(width=80, height=22, color_system="truecolor")
        player._artwork_text()
        assert _wait_for(lambda: len(sizes) == 2)
        assert sizes == [(20, 10), (12, 6)]

    def test_crossing_the_side_by_side_threshold_re_renders(self, monkeypatch):
        """The placement decides the size, so moving the cover beside the text
        is a resize as far as the fetch and the disk cache are concerned. One
        column of width does it, and the render made for the other layout must
        not be shown in the meantime: the pixels *are* the render."""
        sizes = []

        def _load(cover, cols, rows, **kw):
            sizes.append((cols, rows))
            return _grid(cols, rows)

        monkeypatch.setattr(art_mod, "load", _load)
        player = _player(Console(width=82, height=30, color_system="truecolor"))
        player._artwork_text()
        assert _wait_for(lambda: player._artwork is not None)
        assert sizes == [(28, 14)], "stacked, capped by three fifths of 82"

        player.console = Console(width=83, height=30, color_system="truecolor")
        assert player._artwork_text() is None, "a render for the old layout"
        assert _wait_for(lambda: len(sizes) == 2)
        assert sizes[1] == (32, 16), "beside, and bigger for it"
        assert _wait_for(lambda: player._artwork_text() is not None)

    def test_a_failed_fetch_shows_nothing_and_is_not_retried(self, monkeypatch):
        calls = []

        def _load(cover, cols, rows, **kw):
            calls.append(1)
            return None

        monkeypatch.setattr(art_mod, "load", _load)
        player = _player()
        player._artwork_text()
        assert _wait_for(lambda: player._artwork is not None)
        for _ in range(3):
            assert player._artwork_text() is None
        time.sleep(0.05)
        assert len(calls) == 1, "a cover with no art must not be asked for twice"

    def test_a_load_that_raises_is_survivable(self, monkeypatch):
        def _load(cover, cols, rows, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(art_mod, "load", _load)
        player = _player()
        player._artwork_text()
        assert _wait_for(lambda: player._artwork is not None)
        assert player._artwork[3] is None
        assert player._artwork_text() is None

    def test_nothing_is_requested_without_a_cover(self, monkeypatch):
        monkeypatch.setattr(art_mod, "load", lambda *a, **kw: 1 / 0)
        assert _player(cover=None)._artwork_text() is None

    def test_a_terminal_without_colour_gets_no_artwork(self, monkeypatch):
        monkeypatch.setattr(art_mod, "load", lambda *a, **kw: 1 / 0)
        for system in (None, "standard"):
            player = _player(console=Console(
                width=80, height=24, color_system=system))
            assert player._artwork_text() is None
            assert player._artwork_request is None, "and never asks for any"

    def test_a_tiny_terminal_gets_no_artwork(self, monkeypatch):
        monkeypatch.setattr(art_mod, "load", lambda *a, **kw: 1 / 0)
        player = _player(console=Console(
            width=80, height=12, color_system="truecolor"))
        assert player._artwork_text() is None

    def test_the_mini_player_and_the_list_views_stay_clean(self, monkeypatch):
        monkeypatch.setattr(art_mod, "load",
                            lambda cover, cols, rows, **kw: _grid(cols, rows))
        player = _player()
        player._artwork_text()
        assert _wait_for(lambda: player._artwork is not None)
        assert player._artwork_text() is not None
        player._mini_player = True
        assert player._artwork_text() is None
        player._mini_player = False
        player._mode = player.MODE_SEARCH
        assert player._artwork_text() is None

    def test_the_setting_applies_live(self, monkeypatch):
        monkeypatch.setattr(art_mod, "load",
                            lambda cover, cols, rows, **kw: _grid(cols, rows))
        player = _player()
        assert player._show_artwork is True, "artwork is on by default"
        player._artwork_text()
        assert _wait_for(lambda: player._artwork is not None)

        player._apply_setting("show_artwork", False)
        assert player._artwork_text() is None
        assert player._artwork is None and player._artwork_request is None

        player._apply_setting("show_artwork", True)
        player._artwork_text()
        assert _wait_for(lambda: player._artwork is not None)

    def test_the_panel_still_fits_an_80x24_terminal(self, monkeypatch):
        """Artwork may not push the player out of the terminal it is in."""
        monkeypatch.setattr(art_mod, "load",
                            lambda cover, cols, rows, **kw: _grid(cols, rows))
        player = _player()
        player._artwork_text()
        assert _wait_for(lambda: player._artwork is not None)
        lines = player.console.render_lines(
            player._build_display(), player.console.options)
        assert len(lines) <= 24, "the player grew taller than the terminal"
        for line in lines:
            assert sum(len(seg.text) for seg in line) <= 80

    def test_the_settings_page_still_fits(self, monkeypatch):
        """One more row in SETTINGS_SPEC must not overflow the pane."""
        player = _player()
        player._mode = player.MODE_SETTINGS
        lines = player.console.render_lines(
            player._build_display(), player.console.options)
        for line in lines:
            assert sum(len(seg.text) for seg in line) <= 80
