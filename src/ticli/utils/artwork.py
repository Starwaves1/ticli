"""Album artwork as terminal pixel art.

One cell, two pixels: `▀` painted with a foreground colour for its top half
and a background colour for its bottom half. A cell is about twice as tall
as it is wide, so `cols` columns by `cols // 2` rows comes out square.

No new dependencies, which is the whole design constraint here. TIDAL serves
covers as JPEG from an unauthenticated CDN, and Python's standard library
cannot decode a JPEG — so this module does, in about two hundred lines, by
noticing that it never needs full resolution. The DC coefficient of an 8x8
JPEG block *is* that block's average value: decoding nothing but the DC
coefficients yields the image at 1/8 scale, exactly (to within a rounding
error, measured against ffmpeg at 1 part in 255), with no IDCT, no
upsampling and no colour-space work beyond one YCbCr matrix per block.
A 320x320 cover becomes a 40x40 pixel grid in a couple of milliseconds,
which is already more pixels than a 20x10 cell rendering can show.

It is even less work than that on the images TIDAL actually sends, which
are *progressive* (SOF2): a progressive JPEG's first scan is the DC scan,
so the decoder reads that one scan and stops — the AC scans, which are the
bulk of the file, are never touched. Baseline JPEG (SOF0/SOF1) is supported
too, by decoding each block's DC and stepping over its AC coefficients.

Anything this decoder does not handle — arithmetic coding, 12-bit samples,
lossless, a non-interleaved scan — falls through to ffmpeg if ffmpeg is
installed (it usually is; ffplay is one of the two audio backends), and to
no artwork at all if it is not. Every failure path in this module returns
None. Artwork is decoration: it may never take the player down with it.
"""

import logging
import os
import re
import subprocess

from .cache import artwork_dir

logger = logging.getLogger(__name__)

# The CDN path template tidalapi builds album.image() from. Reproduced here
# rather than called, so rendering artwork needs no session and no API call:
# the cover id is already on the album object, and the CDN is unauthenticated.
IMAGE_URL = "https://resources.tidal.com/images/{path}/{size}x{size}.jpg"

# Valid CDN sizes are 80, 160, 320, 640, 1280. 320 is the smallest one that
# still has pixels to spare: DC-only decoding divides by 8, so 320 gives a
# 40x40 grid — wider than any cell size offered below, so downscaling always
# averages down and never blurs up. It is also ~6 KB, a fraction of a second
# on any connection.
SOURCE_SIZE = 320

# Cell sizes, widest first. Square on screen, since a cell is ~2:1 — every
# entry here is (n, n/2) and the renderer packs two pixels into a cell.
ART_SIZES = ((32, 16), (28, 14), (24, 12), (20, 10), (16, 8), (12, 6))

# The most of the terminal's width a *stacked* cover may take. Height alone
# used to choose the size, so a tall, narrow window put a 20-column cover
# above a track line with eight columns of room left — the picture looked
# fine and everything around it looked cramped.
#
# Three fifths, raised from two: stacked, nothing shares the cover's rows, so
# the share is a question of balance rather than of competition, and two
# fifths was answering it as though something did. A narrow window is exactly
# where a big cover is worth having, and it is now the only place a stacked
# cover is drawn at all — past MIN_BESIDE_WIDTH the picture moves beside the
# text and the budget below takes over. The absolute ceiling is unchanged:
# ART_SIZES still stops at 32 columns, so nothing gets bigger in a wide
# window, only in a narrow one.
ART_WIDTH_SHARE = 0.6

# Rows the rest of the player pane needs, below which artwork is not worth
# its vertical cost, and the narrowest terminal art is offered in at all.
# Sixteen, not fourteen: the pane is not one height. Borders, padding, the
# four track rows, the next-track line and the controls come to fourteen,
# but `[m]` adds a second controls row and a toast adds another, and at
# fourteen a 24-row terminal showing both overflowed by a row — which Rich
# answers by replacing the bottom line with a red ellipsis, i.e. eating the
# controls. Two rows of headroom means every state of the pane fits.
MIN_ROWS_AROUND_ART = 16
# The same count for a cover drawn *beside* the text rather than above it,
# where the track rows cost nothing extra because they are inside the
# picture's own rows. What is left is the borders and padding (4), the blank
# row above the footer (1), the footer (up to 4 with `[m]` open) and a toast
# (1) — ten, plus a row of headroom. The smallest cover offered is six rows
# and the text block is at most six, so the text always has somewhere to go.
MIN_ROWS_BESIDE_ART = 11
# Columns a picture costs beyond its own width: the panel's border and
# padding (6) plus the indent that lines it up with the track title (3). A
# picture that doesn't clear this would wrap, which looks like corruption.
ART_MARGIN = 9
MIN_ART_WIDTH = min(cols for cols, _ in ART_SIZES) + ART_MARGIN

# Columns between a cover and the text beside it, and the least the text may
# be left with. Forty holds the three-space indent, both times, a full-width
# progress bar and the scrub marker with room over for a title — under that
# the text column is a column of ellipses and the picture has won an argument
# it should not have been in.
ART_GUTTER = 2
MIN_TEXT_WIDTH = 40
# Where the cover stops sitting above the text and starts sitting beside it:
# the width at which the *largest* cover there is can sit beside a full text
# column. Derived, not chosen — the margin (9), the biggest entry in
# ART_SIZES (32), the gutter (2) and MIN_TEXT_WIDTH (40) — and the derivation
# is what makes widening the window safe. Below it the cover is capped by a
# share of the width and above it by this budget, and because the budget can
# hold 32 from the very first column past the threshold while beside also
# answers to the *smaller* MIN_ROWS_BESIDE_ART, the cover crossing the
# threshold can only grow. Picking a threshold that merely felt wide enough
# (71 was tried) shrank a 28-column cover to 20 the moment the window got one
# column wider, which is the one thing a responsive layout must not do.
#
# Vertical is the scarce axis in a wide window, and that is the point of the
# move: stacked spends art_rows + 2 + 5 rows, side by side spends
# max(art_rows, 5) — eleven rows back at 100x30, where the cover also goes
# from 28x14 to 32x16.
MIN_BESIDE_WIDTH = (ART_MARGIN + max(cols for cols, _ in ART_SIZES)
                    + ART_GUTTER + MIN_TEXT_WIDTH)  # 83

ART_VERSION = 1
ART_SUFFIX = ".art"
# Kilobytes each, and never more than this many. Deliberately its own small
# ceiling rather than a share of the cache budget: the budget is about audio,
# and a few hundred files of a few hundred bytes must not perturb it.
MAX_ART_FILES = 256

# What a cover id may contain before it is allowed to name a file
_SAFE_ID = re.compile(r"^[0-9a-zA-Z._-]{1,64}$")

FFMPEG_TIMEOUT = 10


class ArtworkError(Exception):
    """Any reason this image cannot become pixels."""


# ── JPEG ──

# Coefficient order within a block. Only entry 0 (the DC term) is ever read,
# but the quantisation table arrives in this order and has to be unpicked.
ZIGZAG = (
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
)

SOF_BASELINE = (0xC0, 0xC1)
SOF_PROGRESSIVE = 0xC2
# Every other SOFn: arithmetic-coded, lossless, differential, 12-bit.
SOF_UNSUPPORTED = (0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)


class _Huffman:
    """A canonical JPEG Huffman table, as a (bit length, code) -> symbol map."""

    __slots__ = ("lookup",)

    def __init__(self, counts, symbols):
        self.lookup = {}
        code = 0
        k = 0
        for length in range(1, 17):
            for _ in range(counts[length - 1]):
                if k >= len(symbols):
                    raise ArtworkError("truncated huffman table")
                self.lookup[(length, code)] = symbols[k]
                code += 1
                k += 1
            code <<= 1


class _BitReader:
    """MSB-first bit reader over entropy-coded data, with byte unstuffing."""

    __slots__ = ("data", "pos", "byte", "nbits")

    def __init__(self, data, pos):
        self.data = data
        self.pos = pos
        self.byte = 0
        self.nbits = 0

    def read_bit(self):
        if self.nbits == 0:
            if self.pos >= len(self.data):
                raise ArtworkError("entropy data ran out")
            byte = self.data[self.pos]
            self.pos += 1
            if byte == 0xFF:
                # 0xFF00 is a stuffed literal 0xFF; anything else is a marker,
                # which means the scan ended earlier than the geometry said
                following = self.data[self.pos] if self.pos < len(self.data) else 0
                if following == 0x00:
                    self.pos += 1
                else:
                    raise ArtworkError("marker inside scan")
            self.byte = byte
            self.nbits = 8
        self.nbits -= 1
        return (self.byte >> self.nbits) & 1

    def receive(self, count):
        value = 0
        for _ in range(count):
            value = (value << 1) | self.read_bit()
        return value

    def decode(self, table):
        code = 0
        for length in range(1, 17):
            code = (code << 1) | self.read_bit()
            symbol = table.lookup.get((length, code))
            if symbol is not None:
                return symbol
        raise ArtworkError("bad huffman code")

    def restart(self):
        """Step over an RSTn marker at a restart interval boundary."""
        self.nbits = 0
        while self.pos + 1 < len(self.data):
            if self.data[self.pos] == 0xFF and 0xD0 <= self.data[self.pos + 1] <= 0xD7:
                self.pos += 2
                return
            self.pos += 1
        raise ArtworkError("missing restart marker")


def _extend(value, length):
    """JPEG's signed-value encoding."""
    if length and value < (1 << (length - 1)):
        return value - (1 << length) + 1
    return value


def decode_jpeg_dc(data: bytes) -> list:
    """Decode a JPEG at 1/8 scale — one pixel per 8x8 block.

    Returns rows of (r, g, b). Raises ArtworkError for anything it can't read;
    it never returns a partial image.
    """
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        raise ArtworkError("not a JPEG")
    pos = 2
    quant = {}
    dc_tables = {}
    ac_tables = {}
    frame = None
    restart_interval = 0

    while pos + 3 < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        if marker == 0xD9:  # EOI
            break
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # no payload
            continue
        length = int.from_bytes(data[pos:pos + 2], "big")
        if length < 2:
            raise ArtworkError("bad segment length")
        segment = data[pos + 2:pos + length]

        if marker == 0xDB:  # DQT
            quant.update(_read_quant_tables(segment))
        elif marker == 0xC4:  # DHT
            _read_huffman_tables(segment, dc_tables, ac_tables)
        elif marker == 0xDD:  # DRI
            restart_interval = int.from_bytes(segment[0:2], "big")
        elif marker in SOF_BASELINE or marker == SOF_PROGRESSIVE:
            frame = _read_frame(segment, progressive=marker == SOF_PROGRESSIVE)
        elif marker in SOF_UNSUPPORTED:
            raise ArtworkError(f"unsupported JPEG type {marker:#x}")
        elif marker == 0xDA:  # SOS — the first scan is all we ever need
            if frame is None:
                raise ArtworkError("scan before frame")
            scan, spectral_start, approx_high, approx_low = _read_scan_header(
                segment, frame)
            if frame["progressive"]:
                # A progressive file's first scan must be the DC scan; if it
                # isn't, this file is shaped in a way we don't read
                if spectral_start != 0 or approx_high != 0:
                    raise ArtworkError("first scan is not the DC scan")
            planes = _decode_dc_scan(
                data, pos + length, frame, scan, quant, dc_tables, ac_tables,
                restart_interval,
                shift=approx_low if frame["progressive"] else 0,
                skip_ac=frame["progressive"],
            )
            return _to_rgb(frame, planes)
        pos += length
    raise ArtworkError("no scan found")


def _read_quant_tables(segment) -> dict:
    tables = {}
    i = 0
    while i < len(segment):
        precision, target = segment[i] >> 4, segment[i] & 15
        i += 1
        table = [0] * 64
        for k in range(64):
            if precision:
                table[ZIGZAG[k]] = int.from_bytes(segment[i:i + 2], "big")
                i += 2
            else:
                if i >= len(segment):
                    raise ArtworkError("truncated quantisation table")
                table[ZIGZAG[k]] = segment[i]
                i += 1
        tables[target] = table
    return tables


def _read_huffman_tables(segment, dc_tables, ac_tables) -> None:
    i = 0
    while i < len(segment):
        table_class, target = segment[i] >> 4, segment[i] & 15
        counts = list(segment[i + 1:i + 17])
        if len(counts) < 16:
            raise ArtworkError("truncated huffman header")
        total = sum(counts)
        symbols = list(segment[i + 17:i + 17 + total])
        table = _Huffman(counts, symbols)
        (ac_tables if table_class else dc_tables)[target] = table
        i += 17 + total


def _read_frame(segment, progressive: bool) -> dict:
    if len(segment) < 6:
        raise ArtworkError("truncated frame header")
    height = int.from_bytes(segment[1:3], "big")
    width = int.from_bytes(segment[3:5], "big")
    count = segment[5]
    if not width or not height or not count:
        raise ArtworkError("empty frame")
    components = []
    for i in range(count):
        base = 6 + i * 3
        if base + 2 >= len(segment):
            raise ArtworkError("truncated component list")
        sampling = segment[base + 1]
        h, v = sampling >> 4, sampling & 15
        if not h or not v:
            raise ArtworkError("bad sampling factors")
        components.append(
            {"id": segment[base], "h": h, "v": v, "tq": segment[base + 2]})
    return {"w": width, "h": height, "comps": components,
            "progressive": progressive}


def _read_scan_header(segment, frame) -> tuple:
    count = segment[0]
    if count != len(frame["comps"]):
        # A scan naming only some components is legal and rare; the block
        # walk below assumes the interleaved case
        raise ArtworkError("non-interleaved scan unsupported")
    scan = []
    for i in range(count):
        selector = segment[1 + i * 2]
        tables = segment[2 + i * 2]
        component = next(
            (c for c in frame["comps"] if c["id"] == selector), None)
        if component is None:
            raise ArtworkError("scan names an unknown component")
        scan.append((component, tables >> 4, tables & 15))
    tail = 1 + count * 2
    if tail + 2 >= len(segment):
        raise ArtworkError("truncated scan header")
    spectral_start = segment[tail]
    approx = segment[tail + 2]
    return scan, spectral_start, approx >> 4, approx & 15


def _decode_dc_scan(data, pos, frame, scan, quant, dc_tables, ac_tables,
                    restart_interval, shift, skip_ac) -> list:
    """Walk the scan, keeping one value per block and discarding the rest."""
    comps = frame["comps"]
    hmax = max(c["h"] for c in comps)
    vmax = max(c["v"] for c in comps)
    mcus_x = (frame["w"] + 8 * hmax - 1) // (8 * hmax)
    mcus_y = (frame["h"] + 8 * vmax - 1) // (8 * vmax)
    planes = []
    for comp in comps:
        comp["bx"] = mcus_x * comp["h"]
        comp["by"] = mcus_y * comp["v"]
        planes.append([0.0] * (comp["bx"] * comp["by"]))

    # Tables and planes cannot change between MCUs, so resolve them once —
    # for a 320x320 cover the scan loop below runs thousands of times
    resolved = []
    for index, (comp, dc_id, ac_id) in enumerate(scan):
        dc_table = dc_tables.get(dc_id)
        ac_table = ac_tables.get(ac_id)
        if dc_table is None or (not skip_ac and ac_table is None):
            raise ArtworkError("missing huffman table")
        table = quant.get(comp["tq"])
        if table is None:
            raise ArtworkError("missing quantisation table")
        resolved.append((index, comp, dc_table, ac_table, table, planes[index]))

    bits = _BitReader(data, pos)
    predictions = [0] * len(comps)
    for mcu in range(mcus_x * mcus_y):
        if restart_interval and mcu and mcu % restart_interval == 0:
            bits.restart()
            predictions = [0] * len(comps)
        mcu_row, mcu_col = divmod(mcu, mcus_x)
        for index, comp, dc_table, ac_table, table, plane in resolved:
            for by in range(comp["v"]):
                for bx in range(comp["h"]):
                    size = bits.decode(dc_table)
                    if size:
                        predictions[index] += _extend(bits.receive(size), size)
                    # Dequantise, undo the IDCT's 1/8 scaling for a flat
                    # block, and undo the level shift: this is the block mean
                    value = ((predictions[index] << shift) * table[0]) / 8.0 + 128.0
                    row = mcu_row * comp["v"] + by
                    col = mcu_col * comp["h"] + bx
                    plane[row * comp["bx"] + col] = value
                    if skip_ac:
                        continue
                    _skip_ac(bits, ac_table)
    return planes


def _skip_ac(bits, table) -> None:
    """Consume a baseline block's 63 AC coefficients without keeping them."""
    k = 1
    while k < 64:
        symbol = bits.decode(table)
        run, size = symbol >> 4, symbol & 15
        if size == 0:
            if run != 15:  # end of block
                return
            k += 16  # a run of sixteen zeroes
        else:
            k += run + 1
            bits.receive(size)


def _to_rgb(frame, planes) -> list:
    """Combine the per-component block planes into rows of (r, g, b)."""
    comps = frame["comps"]
    hmax = max(c["h"] for c in comps)
    vmax = max(c["v"] for c in comps)
    width = max(1, (frame["w"] + 7) // 8)
    height = max(1, (frame["h"] + 7) // 8)
    grey = len(comps) < 3
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            values = []
            for index, comp in enumerate(comps[:3]):
                # A subsampled component covers more pixels per block
                cx = min(comp["bx"] - 1, x * comp["h"] // hmax)
                cy = min(comp["by"] - 1, y * comp["v"] // vmax)
                values.append(planes[index][cy * comp["bx"] + cx])
            if grey:
                level = _clamp(values[0])
                row.append((level, level, level))
            else:
                luma, cb, cr = values[0], values[1] - 128.0, values[2] - 128.0
                row.append((
                    _clamp(luma + 1.402 * cr),
                    _clamp(luma - 0.344136 * cb - 0.714136 * cr),
                    _clamp(luma + 1.772 * cb),
                ))
        rows.append(row)
    return rows


def _clamp(value) -> int:
    return 0 if value < 0 else (255 if value > 255 else int(value + 0.5))


# ── ffmpeg fallback ──


def decode_with_ffmpeg(data: bytes, width: int, height: int):
    """Decode straight to the target size with ffmpeg, or None if it can't.

    Only reached when the decoder above refuses the file. ffmpeg is not a
    dependency — ffplay is one of two possible audio backends and mpv users
    may not have it — so a missing binary is an ordinary answer, not an error.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "image2pipe", "-i", "pipe:0",
             "-vf", f"scale={width}:{height}:flags=area",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=FFMPEG_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("ffmpeg artwork decode unavailable: %s", e)
        return None
    raw = result.stdout
    if result.returncode != 0 or len(raw) < width * height * 3:
        return None
    return [
        [tuple(raw[(y * width + x) * 3 + c] for c in range(3))
         for x in range(width)]
        for y in range(height)
    ]


# ── scaling and rendering ──


def resample(pixels: list, width: int, height: int) -> list:
    """Box-average an image down to width x height."""
    source_h = len(pixels)
    source_w = len(pixels[0]) if source_h else 0
    if not source_w or not source_h:
        raise ArtworkError("empty image")
    out = []
    for ty in range(height):
        y0 = ty * source_h // height
        y1 = max(y0 + 1, (ty + 1) * source_h // height)
        row = []
        for tx in range(width):
            x0 = tx * source_w // width
            x1 = max(x0 + 1, (tx + 1) * source_w // width)
            r = g = b = count = 0
            for y in range(y0, y1):
                line = pixels[y]
                for x in range(x0, x1):
                    pixel = line[x]
                    r += pixel[0]
                    g += pixel[1]
                    b += pixel[2]
                    count += 1
            row.append((r // count, g // count, b // count))
        out.append(row)
    return out


def decode(data: bytes, cols: int, rows: int):
    """Bytes off the CDN to a cols x (2*rows) pixel grid, or None.

    The stdlib decoder first; ffmpeg only if it declines. Returns None rather
    than raising: every caller of this is decoration.
    """
    if not data:
        return None
    try:
        return resample(decode_jpeg_dc(data), cols, rows * 2)
    except ArtworkError as e:
        logger.debug("Built-in JPEG decode failed (%s), trying ffmpeg", e)
    except Exception as e:  # a malformed file must not escape as a traceback
        logger.debug("Built-in JPEG decode error (%s), trying ffmpeg", e)
    return decode_with_ffmpeg(data, cols, rows * 2)


def supports_art(console) -> bool:
    """Whether this terminal can show pixel art at all.

    Rich reports what the terminal advertises; it also downgrades 24-bit
    colours to a 256-colour palette itself, so a 256-colour terminal gets
    real artwork for free. Sixteen colours is where it stops being a picture
    and starts being noise, so that (and no colour at all) means no artwork.
    """
    try:
        system = console.color_system
    except Exception:
        return False
    return system in ("truecolor", "256")


def render(pixels: list, indent: int = 0):
    """A pixel grid as Rich Text: one `▀` per cell, two pixels per cell."""
    from rich.text import Text  # local: keeps this module importable headless

    text = Text(no_wrap=True)
    rows = len(pixels) // 2
    pad = " " * indent
    for row in range(rows):
        if row:
            text.append("\n")
        if pad:
            text.append(pad)
        top = pixels[row * 2]
        bottom = pixels[row * 2 + 1]
        for col in range(len(top)):
            tr, tg, tb = top[col]
            br, bg, bb = bottom[col]
            text.append(
                "▀",
                style=f"#{tr:02x}{tg:02x}{tb:02x} on #{br:02x}{bg:02x}{bb:02x}",
            )
    return text


def art_beside(width: int, height: int) -> bool:
    """Whether this window is wide enough to put the cover next to the text.

    One threshold and no hysteresis: above MIN_BESIDE_WIDTH the cover and the
    text both have a share worth having, below it they do not and the cover
    goes back above. A rule that also consulted the height would put the
    layout somewhere nobody could predict from the width they can see.
    """
    return width >= MIN_BESIDE_WIDTH and art_size(width, height, True) is not None


def art_size(width: int, height: int, beside: bool = False):
    """Cell size for a terminal this big, or None if artwork doesn't fit.

    Both axes decide it, and which budget each one gets depends on where the
    picture is going to sit.

    **Stacked** — the cover above the track line, which is what a window
    narrower than MIN_BESIDE_WIDTH gets. Its rows are rows the rest of the
    pane does not get (MIN_ROWS_AROUND_ART), and its width is capped as a
    *share* of the window's: nothing shares those rows, so the cap is about
    balance, and a narrow window is allowed a cover that dominates.

    **Beside** — the cover on the left with the track line, the progress bar
    and the next-track line to its right. The text is inside the picture's
    rows, so it costs the pane nothing (MIN_ROWS_BESIDE_ART is five rows
    cheaper) — but it is now competing for the *same* columns, so the share
    gives way to a budget: everything left after the margin, the gutter and
    MIN_TEXT_WIDTH. That is a bigger cover than stacked at the heights a real
    window has (24 rows: 24x12 beside against 16x8 above) and a smaller one
    only where vertical room is free anyway.
    """
    if width < MIN_ART_WIDTH:
        return None
    if beside:
        room = width - ART_MARGIN - ART_GUTTER - MIN_TEXT_WIDTH
        spare = MIN_ROWS_BESIDE_ART
    else:
        room = int(width * ART_WIDTH_SHARE)
        spare = MIN_ROWS_AROUND_ART
    for cols, rows in ART_SIZES:
        if (cols + ART_MARGIN <= width and cols <= room
                and rows + spare <= height):
            return cols, rows
    return None


# ── disk cache ──
#
# Keyed on the cover *and* the size it was rendered at, because the pixels
# are the render: resizing the terminal is a different picture. Files are
# named so the cache module can tell them apart from cached audio, which it
# counts and evicts on its own budget — artwork is kilobytes and keeps its
# own small ceiling instead (see MAX_ART_FILES).


def cover_url(cover_id: str, size: int = SOURCE_SIZE) -> str:
    return IMAGE_URL.format(path=str(cover_id).replace("-", "/"), size=size)


def art_path(cover_id: str, cols: int, rows: int):
    """Where this rendering lives, or None if the id can't name a file."""
    if not cover_id or not _SAFE_ID.match(str(cover_id)):
        return None
    return artwork_dir() / f"{cover_id}-{cols}x{rows}{ART_SUFFIX}"


def read_art(cover_id: str, cols: int, rows: int):
    """A previously rendered grid, or None. Corrupt files are just misses."""
    path = art_path(cover_id, cols, rows)
    if path is None:
        return None
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return _parse_art(text, cols, rows)
    except (ValueError, IndexError):
        logger.debug("Unusable artwork cache entry %s", path)
        return None


def _parse_art(text: str, cols: int, rows: int):
    lines = text.strip().splitlines()
    if not lines:
        raise ValueError("empty")
    header = lines[0].split()
    if len(header) != 4 or header[0] != "ticli-art":
        raise ValueError("bad header")
    if (int(header[1]), int(header[2]), int(header[3])) != (ART_VERSION, cols, rows):
        raise ValueError("not this rendering")
    body = lines[1:]
    if len(body) != rows * 2:
        raise ValueError("wrong row count")
    pixels = []
    for line in body:
        if len(line) != cols * 6:
            raise ValueError("wrong row width")
        pixels.append([
            (int(line[i:i + 2], 16), int(line[i + 2:i + 4], 16),
             int(line[i + 4:i + 6], 16))
            for i in range(0, len(line), 6)
        ])
    return pixels


def write_art(cover_id: str, cols: int, rows: int, pixels: list) -> None:
    """Store a rendering. Best effort: a cache that can't be written is a
    redecode, never a failure."""
    path = art_path(cover_id, cols, rows)
    if path is None:
        return
    body = "\n".join(
        "".join(f"{r:02x}{g:02x}{b:02x}" for r, g, b in row) for row in pixels)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(f"ticli-art {ART_VERSION} {cols} {rows}\n{body}\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as e:
        logger.debug("Failed to write artwork cache: %s", e)
        return
    _evict()


def _evict() -> None:
    """Keep the artwork directory to MAX_ART_FILES, oldest out first."""
    try:
        files = [p for p in artwork_dir().iterdir()
                 if p.name.endswith(ART_SUFFIX)]
    except OSError:
        return
    if len(files) <= MAX_ART_FILES:
        return
    stamped = []
    for path in files:
        try:
            stamped.append((path.stat().st_mtime, path))
        except OSError:
            continue
    stamped.sort()
    for _mtime, path in stamped[:len(stamped) - MAX_ART_FILES]:
        try:
            path.unlink()
        except OSError:
            continue


# ── the whole job, as one call ──


def load(cover_id: str, cols: int, rows: int, fetch=None):
    """Artwork for this cover at this size, from disk or from the CDN.

    Blocking and network-touching by design — the caller runs it on a daemon
    thread. Returns a pixel grid, or None if artwork isn't possible for this
    cover, which is a perfectly ordinary outcome.
    """
    if not cover_id:
        return None
    cached = read_art(cover_id, cols, rows)
    if cached is not None:
        return cached
    if fetch is None:
        import requests

        def fetch(url):
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.content

    try:
        data = fetch(cover_url(cover_id))
    except Exception as e:  # offline, DNS, 404, timeout — all the same here
        logger.debug("Artwork fetch failed for %s: %s", cover_id, e)
        return None
    pixels = decode(data, cols, rows)
    if pixels is None:
        return None
    write_art(cover_id, cols, rows, pixels)
    return pixels


def cover_id_of(track):
    """The cover id on a track's album, if it has one.

    Cached rows carry only an album name, and a track can have no album at
    all, so this is allowed to come back empty.
    """
    album = getattr(track, "album", None)
    cover = getattr(album, "cover", None) if album is not None else None
    return cover if isinstance(cover, str) and cover else None
