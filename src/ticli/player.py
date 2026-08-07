"""Ticli - Terminal music player for TIDAL.

Uses tidalapi for TIDAL API access and ffplay/mpv for audio playback.
OAuth login via browser, session persisted to disk.
"""

import json
import logging
import os
import signal
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from collections import namedtuple
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)

try:
    import tidalapi
    # A hard tidalapi dependency, so importing it here adds nothing to install
    import requests
except ImportError:
    print("This feature requires 'tidalapi'. Install it with: pip install tidalapi")
    sys.exit(1)

try:
    from rich.cells import cell_len
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
except ImportError:
    print("This feature requires 'rich'. Install it with: pip install rich")
    sys.exit(1)


def format_time(seconds):
    if seconds is None or seconds != seconds:
        return "--:--"
    seconds = int(seconds)
    if seconds < 0:
        return "0:00"
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


# ── Fitting the pane to the window ──
#
# One rule underneath all of it: every line the pane draws is cut to the
# panel's inner width before Rich ever sees it. Rich only reflows a line that
# is too long and none ever is, so nothing wraps mid-word, no progress bar
# arrives in two pieces and no `[key] label` pair comes apart. The same rule
# makes the pane's height *countable* — with no wrapping, one line is one row
# — which is what lets _build_display measure itself against the terminal and
# give ground before it overflows. Overflowing is not cosmetic: Rich answers
# it by replacing the bottom line, and the bottom line is the controls.

# What the panel costs around its content. The full pane pads (1, 2), the mini
# player (0, 1); the rows are the two borders plus the blank padding row above
# and below.
PANEL_CHROME = 6
MINI_PANEL_CHROME = 4
PANEL_ROWS = 4
MINI_PANEL_ROWS = 2
# A window narrower than this has nothing to lay out; the clamp only keeps the
# arithmetic positive.
MIN_INNER_WIDTH = 12

# Where the pane's text starts, and the gap between two hints.
INDENT = 3
HINT_GAP = 2

# The rows at the top of the pane that say what is playing: the track line,
# the album under it and the progress line. They outrank the footer when the
# window is too short for both, which is the one place the ordering is
# reversed — everywhere above it the footer wins, because what a long body
# loses to a crop is its tail and not its identity. Three, because the
# progress line is the third of them; two would keep the album and lose the
# time, which is the wrong half of "what is playing, and where in it".
IDENTITY_ROWS = 3

# Under this a progress bar is not a bar, it is a smear — three cells that
# round to the same place for half the track. The times alone say more.
MIN_BAR_WIDTH = 8
# The volume bar has 0–250 to say and says it in a fixed 30 cells at most: it
# is a level, not a position in a track, so growing it with the window would
# only make a wide terminal look like it had a second progress bar.
VOLUME_BAR_MAX = 30

# One `[key] label` pair: the unit that is never split across a line break,
# and the unit that gets dropped whole when even the keys don't fit. `short`
# is the same thing said in fewer columns; `rank` orders the dropping, higher
# first, and has nothing to do with the order they are shown in.
Hint = namedtuple("Hint", "key label short rank")
Hint.__new__.__defaults__ = (None, 0)

HINT_FULL, HINT_SHORT, HINT_KEYS = 0, 1, 2

# The download box's own footer. Hints rather than a string, so it goes
# through the same _fit_hints as every other footer in the app and gets
# terser in a narrow window instead of being cut off mid-key.
DOWNLOAD_BUTTONS = (Hint("Enter", "download", "get", 0),
                    Hint("Esc", "cancel", None, 1))


def _clip_lines(text: "Text", width: int) -> list:
    """`text` as a list of lines, each cut to `width` with an ellipsis."""
    lines = text.split("\n", allow_blank=True)
    for line in lines:
        line.truncate(width, overflow="ellipsis")
    return list(lines)


def _side_by_side(left: list, right: list) -> "Text":
    """Two blocks of lines as one, `left` padded to its widest row.

    The cover on the left and the track beside it. Rich has no column
    primitive that survives being counted — everything else in this pane is a
    list of lines whose length _build_display can measure — so the two blocks
    are zipped by hand into lines of exactly that kind.

    The text is centred against the picture rather than sitting at its top,
    because a five-row block pinned to the top of a sixteen-row cover reads as
    a layout that ran out rather than one that was chosen. Padding is measured
    in *cells*: a title with 的 in it is wider than it is long, and a left
    column padded by character count leaves the right column ragged.
    """
    width = max((cell_len(line.plain) for line in left), default=0)
    top = max((len(left) - len(right)) // 2, 0)
    out = []
    for i in range(max(len(left), len(right))):
        line = Text()
        if i < len(left):
            line.append_text(left[i])
        j = i - top
        if 0 <= j < len(right):
            # Only where there is something to align to: a row of trailing
            # spaces is a row the pane pays for and nobody can see
            line.append(" " * (width - cell_len(line.plain)
                               + artwork.ART_GUTTER))
            line.append_text(right[j])
        out.append(line)
    return Text("\n").join(out)


def _boxed(rows: list, width: int, margin: int, style: str) -> list:
    """`rows` inside a rounded border, each line centred within `width` and
    the whole box pushed right by `margin`.

    A list of lines, like everything else that goes into the pane, so
    _build_display can still count the height of what it is drawing.
    """
    rule = "─" * (width + 2)
    out = [Text(" " * margin + "╭" + rule + "╮", style=style)]
    for row in rows:
        row = row.copy()
        row.truncate(width, overflow="ellipsis")
        pad = max(width - cell_len(row.plain), 0)
        line = Text(" " * margin)
        line.append("│", style=style)
        line.append(" " * (pad // 2 + 1))
        line.append_text(row)
        line.append(" " * (pad - pad // 2 + 1))
        line.append("│", style=style)
        out.append(line)
    out.append(Text(" " * margin + "╰" + rule + "╯", style=style))
    return out


# The download bar: a heavy rule into a dotted remainder. `╸` is the left half
# of `━`, which buys a half-cell step for nothing — a bar that jumps a whole
# character at a time reads as a progress *report*, one that moves every frame
# reads as a thing happening. Owner's pick out of ten, ai/bar-styles-demo.py.
BAR_FULL, BAR_HALF, BAR_EMPTY = "━", "╸", "┈"


def _bar_split(fraction: float, width: int) -> tuple:
    """`(filled, remainder)`, split so the two halves can be coloured apart.

    Lifted from `_split` in ai/bar-styles-demo.py rather than re-derived: the
    rounding is the whole of the half-cell step and getting it subtly wrong
    is a bar that sticks at the ends.
    """
    fraction = max(0.0, min(1.0, fraction))
    steps = int(round(fraction * width * 2))
    whole, rem = divmod(steps, 2)
    done = BAR_FULL * whole
    if rem and whole < width:
        done += BAR_HALF
        whole += 1
    return done, BAR_EMPTY * (width - whole)


def _format_rate(per_second: float) -> str:
    """`5.2MB/s`. Empty before the first sample window has closed, which reads
    as measuring rather than as stalled."""
    if per_second <= 0:
        return ""
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if per_second >= scale:
            return f"{per_second / scale:.1f}{unit}/s"
    return f"{int(per_second)}B/s"


def _hint_piece(hint: Hint, level: int) -> str:
    if level >= HINT_KEYS:
        return f"[{hint.key}]"
    label = hint.short if (level == HINT_SHORT and hint.short) else hint.label
    return f"[{hint.key}] {label}"


def _wrap_hints(hints, width: int, level: int) -> list:
    """Hints packed into lines of `width`, breaking only between pairs."""
    rows, row, used = [], [], 0
    for hint in hints:
        piece = _hint_piece(hint, level)
        cost = cell_len(piece) + (HINT_GAP if row else 0)
        if row and used + cost > width:
            rows.append(row)
            row, used, cost = [], 0, cell_len(piece)
        row.append(piece)
        used += cost
    if row:
        rows.append(row)
    return rows


def _fit_hints(hints, width: int, max_rows: int) -> list:
    """The richest form of these hints that fits `max_rows` lines of `width`.

    Ranked by (lines used, terseness) in that order, which is the one thing
    here that was chosen rather than derived: a footer that stays on one line
    by saying `[Space] pause` beats the same hints spread over two lines
    saying `pause/play`, because the second line is a row the music does not
    get. Only when nothing fits one line do two lines with full labels win —
    at that width the alternative is keys with no labels at all, and a key
    without its label is a footer that has stopped explaining anything.

    Below that: keys alone, and then keys with the least important dropped one
    at a time. A pair is never split and a line never overflows at any step —
    the footer gets terser as the window narrows instead of turning into
    `[space]` above `play/pause`.
    """
    laid = [_wrap_hints(hints, width, level) for level in (HINT_FULL, HINT_SHORT)]
    best = min(
        (rows for rows in laid if len(rows) <= max_rows),
        key=len, default=None)
    if best is not None:
        return best
    rows = _wrap_hints(hints, width, HINT_KEYS)
    if len(rows) <= max_rows:
        return rows
    kept = list(hints)
    while len(kept) > 1:
        # Highest rank goes first; ties break to the right, so whatever
        # survives is still in the order it was declared in
        kept.pop(max(range(len(kept)), key=lambda i: (kept[i].rank, i)))
        rows = _wrap_hints(kept, width, HINT_KEYS)
        if len(rows) <= max_rows:
            return rows
    return _wrap_hints(kept, width, HINT_KEYS)[:max(max_rows, 1)]


def _hints_text(rows, indent: int) -> "Text":
    text = Text()
    for i, row in enumerate(rows):
        if i:
            text.append("\n")
        text.append(" " * indent)
        for j, piece in enumerate(row):
            if j:
                text.append(" " * HINT_GAP)
            key, sep, label = piece.partition("] ")
            # A keys-only piece has no "] " in it and must not be given one
            text.append(key + sep[:1] if sep else key, style="bold")
            if sep:
                text.append(" " + label, style="dim")
    return text


class _Fit:
    """One attempt at the pane, and the order in which it gives ground.

    Five things are elastic — the cover, the prose under the settings table,
    how many rows a page shows, the chrome between the player and a list, and
    how many rows the footer gets — and which of them goes first is per
    screen, because the cheapest thing to lose is not the same on all of
    them. Each screen names its own order (`levers`), and _build_display
    pulls them in turn until the pane fits. What is never given up on any
    screen is the track and the progress line — IDENTITY_ROWS — and when even
    this runs out it is the footer that gives way to them rather than the
    other way round; see the crop in _build_display.
    """

    __slots__ = ("inner", "rows", "page_rows", "artwork", "hint_rows",
                 "prose", "chrome", "mini", "_levers")

    def __init__(self, inner, rows, page_rows, hint_rows, levers=(), mini=False):
        self.inner = inner
        self.rows = rows
        self.page_rows = page_rows
        self.artwork = True
        self.prose = True
        # The rows between the player block and a list screen's own content
        # that say nothing — the blank-ruled separator and the queue/quality
        # status line. Worth three or four rows, and the difference between a
        # short window showing the search scope row and eating it.
        self.chrome = True
        # Which pane is being drawn, read by the composer rather than taken
        # from the player. Not a lever today — nothing relaxes into the tiny
        # player — but a lever is where it would go: `relax` walks an ordered
        # list and _build_display recomposes after each pull, so "drop to a
        # different screen entirely" is one more entry, plus restating `inner`
        # and `rows`, which differ because the mini panel pads (0, 1).
        self.mini = mini
        self.hint_rows = hint_rows
        self._levers = list(levers)

    def relax(self, over: int) -> bool:
        """Give up `over` rows if there are any left to give."""
        while self._levers:
            lever = self._levers.pop(0)
            if lever == "page_rows" and self.page_rows > 1:
                self.page_rows = max(1, self.page_rows - over)
                # A page can go on shrinking, so it stays on the list
                self._levers.insert(0, lever)
                return True
            if lever == "artwork" and self.artwork:
                self.artwork = False
                return True
            if lever == "prose" and self.prose:
                self.prose = False
                return True
            if lever == "chrome" and self.chrome:
                self.chrome = False
                return True
            if lever == "hint_rows" and self.hint_rows > 1:
                self.hint_rows = 1
                return True
        return False


# The fit a display builder assumes when it is called outside _build_display
# (tests do, and so does anything that only wants the text): a wide window
# with room for everything, so nothing is elided for reasons of layout.
ROOMY_FIT = _Fit(inner=74, rows=1000, page_rows=1000, hint_rows=2)


# Key constants
KEY_UP = "\x1b[A"
KEY_DOWN = "\x1b[B"
KEY_RIGHT = "\x1b[C"
KEY_LEFT = "\x1b[D"
KEY_ESC = "\x1b"
KEY_ENTER = "\r"
KEY_ENTER2 = "\n"
KEY_BACKSPACE = "\x7f"
KEY_BACKSPACE2 = "\x08"
# Tab and Shift-Tab. Search types the query with every printable key it gets,
# so a filter can only ever be bound to a key that isn't one — Tab is the only
# such key left unbound, and cycling is what it means everywhere else.
KEY_TAB = "\t"
KEY_SHIFT_TAB = "\x1b[Z"

# The keys that leave a hidden footer hidden: the ones you press while you are
# only listening. Skip, navigate, play/pause and nothing else — so `[h]` buys
# a clear screen for exactly as long as you are doing those, and the first
# thing you do that isn't puts the controls back.
#
# Deliberately not `k`, the other play/pause key. The rule the user is told is
# "arrows and space"; an alias that behaves differently from every other letter
# is a rule nobody can read off the screen, and `k` is not what the footer
# advertises.
HIDE_HOLD_KEYS = frozenset({KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, " "})

# `[h] hide` ranks above every other hint in every mode (the highest here is
# 8), so it is the first thing a narrow window drops. A footer with room for
# four keys should spend them on what the screen does, not on how to put the
# footer away.
HIDE_HINT_RANK = 9

from ticli.utils.credential_store import save_tokens, load_tokens
from ticli.utils.config import (
    QUALITY_CHOICES,
    QUALITY_V4_RENAMES,
    SETTINGS_ROWS,
    coerce,
    cycle_value,
    display_value,
    get_spec,
    load_config,
    save_config,
)
from ticli.utils.cache import (
    PLAY_COUNTS_AFTER,
    cached_audio_path,
    CachedTrack,
    MetadataCache,
    format_gb,
    is_owned_audio,
    track_record,
)
from ticli.utils import artwork, downloads, tags

STATE_DIR = Path.home() / ".config" / "ticli"
STATE_FILE = STATE_DIR / "player_state.json"

# How long a name the "+ New playlist" row will take. A cap so the row cannot
# grow past the panel, not a rule from TIDAL — nothing in tidalapi documents a
# limit, so this errs generous.
PLAYLIST_NAME_MAX = 100

AUDIO_PLAYERS = ["mpv", "ffplay"]

IS_MACOS = sys.platform == "darwin"

# What the volume setting is allowed to reach. Above 100 is software gain, so
# the backends differ: mpv only exceeds unity when told a higher --volume-max
# (its default ceiling is 130), while ffplay's -volume is documented 0=min
# 100=max and clips there — so ffplay is given 100 and the setting says so.
VOLUME_MAX = get_spec("volume")["max"]
FFPLAY_VOLUME_MAX = 100

# Unity: no gain, every backend can do it, and it is what a track was mastered
# at. The answer whenever the real ceiling can't be established — an unknown
# backend, or none running yet — because guessing high would apply a volume the
# backend then reinterprets, and the number on screen would be a lie.
SAFE_VOLUME_CEILING = 100

# The ceiling each backend can actually be driven to, by backend name. mpv
# amplifies in software up to the --volume-max every spawn passes it; ffplay
# clips at 100. Keyed by name rather than branched on so an unrecognised
# backend has one obvious answer (SAFE_VOLUME_CEILING) instead of falling into
# whichever branch happened to be the else.
BACKEND_VOLUME_CEILINGS = {
    "mpv": VOLUME_MAX,
    "ffplay": FFPLAY_VOLUME_MAX,
}

# macOS media keys (keyboard, AirPods taps, Control Center): mpv registers with
# MPRemoteCommandCenter and turns remote commands into these key names. We rebind
# them over IPC so they set a property ticli polls, instead of mpv acting on them
# itself (NEXT would otherwise end the playlist, STOP would quit mpv).
MEDIA_KEY_ACTIONS = {
    "PLAY": "toggle",       # togglePlayPause
    "PLAYPAUSE": "toggle",
    "PLAYONLY": "play",     # play
    "PAUSEONLY": "pause",   # pause
    "STOP": "pause",        # stop — pause rather than let mpv quit
    "NEXT": "next",         # nextTrack
    "PREV": "prev",         # previousTrack
}
MEDIA_KEY_PROP = "user-data/ticli/media-key"

# Going back this far into a track restarts it instead of skipping backwards —
# the rule every other player uses, for ← and the PREV media key alike
PREV_RESTART_SECONDS = 30

# Scrubbing. One press of ←/→ with the player focused moves the track this far,
# and never nearer the end than SEEK_END_MARGIN: landing on EOF makes the
# backend exit at once, which reads as "the track ended" and skips.
SEEK_STEP_SECONDS = 10
SEEK_END_MARGIN = 2
# The floor between two seeks actually reaching the backend. The screen moves on
# every press regardless; this is only about how often mpv is spoken to and how
# often ffplay — which has no runtime control at all — is respawned. A press
# that arrives too soon leaves its position pending, and the monitor's existing
# 0.5s tick delivers it, so a held-down arrow costs a couple of seeks a second
# rather than one respawn per key repeat.
SEEK_COALESCE_SECONDS = 0.3

# Next-track stream URL prefetch. Fired from the monitor's existing tick this
# far before the end of a track, and thrown away if it isn't used almost at
# once — TIDAL's URLs are signed and short-lived, so a stale one is worse than
# no prefetch at all.
PREFETCH_LEAD = 20
PREFETCH_MAX_AGE = 90

# Login flows. "device" is the default: a code you type on your phone, nothing
# to paste back, and it is what every saved session already used. Only "pkce"
# can reach FLAC, though — the device flow's TIDAL client is entitled to AAC
# and no more, and TIDAL answers its FLAC requests with 320k AAC rather than
# with an error. So PKCE is offered as a deliberate upgrade from the settings
# page (or asked for up front with --login-flow pkce), never chosen for you.
LOGIN_FLOWS = ("device", "pkce")

# How many times a bad paste can be retried before the PKCE prompt gives up.
# A one-time code is easy to truncate when it is being relayed by hand.
PKCE_PASTE_TRIES = 3

# A lossless stream arrives as an MPEG-DASH manifest — a list of fragmented-MP4
# segment URLs rather than a file. Written out as an HLS playlist, which is the
# one segmented format ffmpeg (and therefore both backends) can demux, in a
# per-process directory so two ticlis can't tread on each other. Only the
# newest few survive each write: a playlist is worthless once its segment URLs
# expire an hour later, and pruning on write means no timer.
HLS_KEEP = 4
# Version 7 is the floor for #EXT-X-MAP, and #EXT-X-MAP is not optional here:
# the segments are fMP4 fragments with no moov of their own, so without the
# initialization segment every one of them is undecodable on its own. That is
# exactly how this broke — ffmpeg reported "trun track id unknown, no tfhd was
# found" for each segment in turn and played nothing.
HLS_VERSION = 7
# ffmpeg refuses to follow a local playlist out to the network unless the
# protocols are named. The default whitelist for a file input is
# "file,crypto,data", which silently turns every segment into an error.
HLS_PROTOCOLS = "file,http,https,tcp,tls,crypto"
HLS_SUFFIX = ".m3u8"
# How far ahead mpv is told to buffer a segmented stream. mpv's --cache
# defaults to "auto", which means "on for a network stream" — and the playlist
# it is handed is a *local file*, so it decides the stream is local and leaves
# the cache off. The readahead is then --demuxer-readahead-secs, whose default
# is 1: measured with `demuxer-cache-duration` over IPC, mpv sat at exactly
# 1.02s of buffer for the whole track while fetching 4-second FLAC segments off
# the network. Any fetch that takes longer than that second is an audible
# hitch, which is precisely what was reported. --cache=yes restores the
# buffering mpv would have applied to a plain https:// URL; --cache-secs bounds
# it in time, because its own default is 3600000 (i.e. "the whole track"), and
# the ceiling should be seconds of audio rather than megabytes of hi-res.
HLS_CACHE_SECONDS = 60
# How much of the backend's own complaint fits on the toast line, and how long
# it stays up — longer than an ordinary toast, because it is the only notice
# that the thing the user asked for did not happen.
PLAYER_ERROR_CHARS = 90
PLAYER_ERROR_SECONDS = 8.0

# How far short of a track's stated end a clean exit has to fall before it is
# read as a dead stream rather than as the end of the song. Both backends
# answer a mid-track 403 (an expired signed URL, a network blip) by playing
# what they had buffered and then exiting **0 with an empty stderr** — byte for
# byte what finishing a track looks like, so `failure()` cannot tell them apart
# and is right not to try. The clock can. Generous on purpose: TIDAL's
# `duration` is metadata and can disagree with the audio by a second or two,
# and a false positive here stops the queue on a track that really did end,
# which is worse than the occasional missed truncation. The measured failure
# stopped 164 seconds short.
STREAM_TRUNCATED_MARGIN = 15.0

# The floor between the start of one track and the next in a bulk re-fetch.
# Two requests per track (resolve, then stream), so the ceiling is about one
# request a second — an order of magnitude below the 19/s burst that got the
# owner's IP blocked, and in practice far slower because each track is also
# fetched. See _start_refetch_job for why every part of this is structural.
REFETCH_MIN_INTERVAL = 2.0

# How many CDN fetches a bulk job runs at once, and it is a constant rather
# than a setting on purpose. The parallelism goes **only** on the fetch: a
# `fetch_to_file` is a plain GET to the CDN, which the API limiter never sees,
# so three of them cost the API nothing. Resolving — `session.track()` then
# `get_stream()` — stays serial and paced (see _PacedRun), so the peak API
# request rate of a three-wide run is identical to a one-wide run's. "There is
# no parallelism to tune and no way to ask for more" is the property that keeps
# ai/INCIDENTS #1 from happening again, and it is still true where it matters.
DOWNLOAD_WORKERS = 3
# How often the resolver looks for a free worker slot while all of them are
# busy. Not a new polling loop: it is inside a job that is already running and
# already sleeping out its pacing floor, and it stops when the job does.
WORKER_POLL_SECONDS = 0.05
# The rate on screen is the mean of the last this-many byte samples, taken on
# the monitor thread's existing 0.5 s tick — a 1.5 s window. No new thread and
# no new timer: the tick already exists. See _sample_download_rates.
RATE_SAMPLES = 3

# What TIDAL saying "stop" looks like in an exception. On any of these the
# rule (ai/WORKING-RULES.md) is to stop making requests entirely and report —
# never to retry, which is what turned a rate limit into an edge block.
RATE_LIMIT_SIGNS = ("429", "too many requests", "4006",
                    "does not have streaming privileges")


def _rough_minutes(tracks: int) -> str:
    """How long a paced re-fetch of `tracks` songs will take, said the way a
    person would. The floor is the only part that is knowable without the
    network, so this is a lower bound and reads as one."""
    seconds = tracks * REFETCH_MIN_INTERVAL
    if seconds < 90:
        return "a minute or so"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"at least {minutes} minutes"
    hours = minutes / 60
    return f"at least {hours:.1f} hours"


def _looks_rate_limited(message: str) -> bool:
    lowered = (message or "").lower()
    return any(sign in lowered for sign in RATE_LIMIT_SIGNS)


def _restore_sleep(seconds: float) -> None:
    """The legacy restore's one pacing sleep — the wait that keeps its
    fetches at least REFETCH_MIN_INTERVAL apart. A module function rather
    than an inline `time.sleep` so a test can replace the wait and still
    prove the floor was asked for; it holds no state to leak between tests."""
    time.sleep(seconds)

# Ascending, so "did TIDAL give us less than we asked for?" is a comparison.
# Keys are tidalapi's own Quality values, which is what a Stream reports back
# as the tier it actually granted.
QUALITY_RANK = {"LOW": 0, "HIGH": 1, "LOSSLESS": 2, "HI_RES_LOSSLESS": 3}

# Input / repaint timing. The idle poll doubles as the repaint tick, so it is
# capped at the monitor thread's own 0.5s cadence — an idle player wakes twice
# a second and writes nothing unless the screen actually changed.
IDLE_POLL_SECONDS = 0.5
# Two space (or k) events closer together than this are terminal key repeat,
# not two presses. Comfortably above every platform's repeat interval
# (macOS 15ms floor, Linux 33ms default) and below a deliberate double tap.
KEY_REPEAT_WINDOW = 0.15
# Search paging. tidalapi documents no more than 300 items behind any one
# query, so the offset stops climbing there instead of asking for pages TIDAL
# will never fill. The interval is a floor between network pages: one fetch in
# flight already blocks the next, and a fetch brings back several screens'
# worth of rows, so a held-down arrow can never fan out into requests.
SEARCH_MAX_OFFSET = 300
SEARCH_FETCH_MIN_INTERVAL = 1.0
# Track radio. One request buys a whole endless mix, so the same brakes the
# search pages use apply here: one fetch in flight blocks the next, and the
# interval is the floor between them, so a held-down [r] is one request.
RADIO_LIMIT = 25
RADIO_FETCH_MIN_INTERVAL = 1.0
# How long to wait for the tail of an escape sequence that straddled a read.
# Only a bare Esc ever pays it in full; kept generous so an arrow key over a
# slow SSH link can't decode as Esc and quit the player.
ESC_TAIL_SECONDS = 0.05


# Audio downloads. TIDAL serves a track unencrypted over plain HTTP — as one
# file, or as a run of segments that concatenate into one — so keeping a copy
# is a GET (or a run of them). No ffmpeg, no remux, and nothing that depends
# on which player is running.
DOWNLOAD_CHUNK = 256 * 1024
# (connect, read). Generous on read: a slow link should finish the download
# late, not abandon it. Nothing waits on either.
DOWNLOAD_TIMEOUT = (10, 60)

# What the CDN says the bytes are → the extension they belong in. The file is
# named for what actually arrived, never for what was asked for: a device-flow
# session gets AAC-in-MP4 even when it requests lossless, and a PKCE one gets
# FLAC-in-MP4 through the same code.
AUDIO_MIME_EXTENSIONS = {
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "video/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}
# The same question answered from the URL when the CDN declines to say.
AUDIO_URL_EXTENSIONS = {
    ".mp4": ".m4a", ".m4a": ".m4a", ".flac": ".flac", ".mp3": ".mp3",
    ".aac": ".aac", ".ogg": ".ogg", ".wav": ".wav",
}
DEFAULT_AUDIO_EXT = ".m4a"


def _audio_extension(content_type: Optional[str], url: str) -> str:
    """The container the bytes on the wire actually are.

    Content-Type first (it is the stream describing itself), the URL's own
    suffix second, and MP4 as the last resort — that is what every tier this
    client can reach returns today.
    """
    if content_type:
        mime = content_type.split(";")[0].strip().lower()
        if mime in AUDIO_MIME_EXTENSIONS:
            return AUDIO_MIME_EXTENSIONS[mime]
    suffix = os.path.splitext(urlsplit(url or "").path)[1].lower()
    return AUDIO_URL_EXTENSIONS.get(suffix, DEFAULT_AUDIO_EXT)


def _hls_playlist(dash) -> str:
    """An HLS playlist for a decoded MPEG-DASH manifest.

    Written here rather than taken from tidalapi's own `get_hls()`, which
    omits the initialization segment's #EXT-X-MAP and lists it as if it were
    audio — a playlist no ffmpeg build can decode a sample from. The URLs are
    absolute and already signed, so everything the player needs is inside.
    """
    urls = list(getattr(dash, "urls", None) or [])
    if not urls:
        raise ValueError("segmented stream named no segments")
    # tidalapi numbers the segment template from 0, and segment 0 *is* the
    # initialization segment — same URL as the manifest's own `initialization`.
    init = getattr(dash, "first_url", None) or urls[0]
    media = urls[1:] if urls[0] == init else urls
    if not media:
        raise ValueError("segmented stream named no audio segments")
    timescale = float(getattr(dash, "timescale", 0) or 44100)
    full = float(getattr(dash, "chunk_size", 0) or 0) / timescale
    last = float(getattr(dash, "last_chunk_size", 0) or 0) / timescale
    # A duration of 0 would make the playlist a lie; fall back to the one the
    # rest of the segments report, and to something sane if there is no such
    # thing. Only the target duration has to be an over-estimate.
    full = full or last or 10.0
    last = last or full
    lines = [
        "#EXTM3U",
        f"#EXT-X-VERSION:{HLS_VERSION}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXT-X-TARGETDURATION:{int(max(full, last)) + 1}",
        "#EXT-X-MEDIA-SEQUENCE:1",
        f'#EXT-X-MAP:URI="{init}"',
    ]
    for index, url in enumerate(media):
        lines.append("#EXTINF:%0.3f," % (last if index == len(media) - 1 else full))
        lines.append(url)
    lines.append("#EXT-X-ENDLIST")
    lines.append("")
    return "\n".join(lines)


def _hls_segments(path: str) -> list:
    """The remote segment URLs a local playlist names, initialization first.

    Reading them back out of the playlist keeps a segmented stream a single
    string everywhere else — the same path that is handed to the player is
    all the downloader needs to fetch the whole track.
    """
    urls = []
    try:
        text = Path(path).read_text()
    except OSError:
        return urls
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            _, _, rest = line.partition('URI="')
            uri = rest.rpartition('"')[0]
            if uri:
                urls.insert(0, uri)
        elif line and not line.startswith("#"):
            urls.append(line)
    return urls


def _write_hls_playlist(track_id, playlist: str) -> str:
    """Write a segmented stream's HLS playlist to a temp file and return its
    path. Everything the player needs is inside it — the segment URLs are
    absolute and already signed — so the path can be handed straight to
    mpv/ffplay wherever a stream URL would have gone."""
    directory = Path(tempfile.gettempdir()) / f"ticli-hls-{os.getpid()}"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{track_id}.m3u8"
    path.write_text(playlist)
    try:
        by_age = sorted(directory.glob("*.m3u8"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in by_age[HLS_KEEP:]:
            stale.unlink()
    except OSError:
        pass  # a playlist that vanished under us needed deleting anyway
    return str(path)


class _DownloadSuperseded(Exception):
    """The track a download was for is no longer the one playing."""


def stream_sources(url: str) -> list:
    """Every URL that has to be fetched to have the whole track.

    One for a BTS stream, and for a segmented one the initialization segment
    followed by every media segment — written end to end, that *is* the
    fragmented MP4 file. Empty when the string is not something that can be
    fetched at all.
    """
    if url.endswith(HLS_SUFFIX):
        return _hls_segments(url)
    if url.startswith(("http://", "https://")):
        return [url]
    return []


def fetch_to_file(sources: list, part: str, abandoned=None, progress=None) -> str:
    """Write every source into `part`, end to end. Returns the extension.

    The one downloader: the cache's background copy and a deliberate download
    are the same bytes off the same CDN, and having written it twice is how
    one of them would quietly stop working. TIDAL serves audio unencrypted
    over plain HTTP, so this is a GET — no ffmpeg, no remux, and identical on
    both backends because it does not ride on the player process.

    `abandoned()` is checked every chunk and raises out of the download when
    it answers True; `progress(done, total)` is told how far along it is, with
    a total of 0 when the CDN didn't say. Raises on anything else — the caller
    decides what a failed download means.

    One `Session` for the whole track, because a hi-res track is not one
    request: the real cached 24/48 FLAC (175.9 s, 29,575,234 bytes) parses to
    **45 media segments plus an initialization segment**, mean 657 KB each. A
    bare `requests.get` builds its own connection pool and its own TLS
    connection every time, so that was 46 handshakes to the same host.
    Measured over loopback HTTPS, 46 x 657 KB, median of 5:

        no added latency                  0.250 s  ->  0.032 s  (7.8x)
        +40 ms per connection setup       2.430 s  ->  0.086 s  (+2.34 s)

    — the second row models TCP+TLS against a CDN at a 20 ms RTT. Nothing
    user-visible waits on the *cache* copy, but the deliberate-download
    progress bar is watched, and 45 avoidable handshakes per track is
    CDN-side load ticli has no business creating.

    Per call, not module-level: the `with` closes the pool on the way out —
    including the abandoned path, which raises through it — so an abandoned
    download cannot leave sockets alive, and the cache thread and a download
    job never share one. Failure behaviour is unchanged: a segment that 4xxes
    still raises out of `raise_for_status` at the same place.
    """
    ext = DEFAULT_AUDIO_EXT
    done = 0
    total = 0
    with requests.Session() as session, open(part, "wb") as handle:
        for index, source in enumerate(sources):
            with session.get(source, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
                response.raise_for_status()
                if index == 0:
                    # The first segment describes the container the whole file
                    # is going to be
                    ext = _audio_extension(response.headers.get("Content-Type"), source)
                    if len(sources) == 1:
                        try:
                            total = int(response.headers.get("Content-Length") or 0)
                        except (TypeError, ValueError):
                            total = 0
                for chunk in response.iter_content(DOWNLOAD_CHUNK):
                    if abandoned is not None and abandoned():
                        raise _DownloadSuperseded()
                    if chunk:
                        handle.write(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(done, total)
    return ext


class _PacedRun:
    """The engine behind every bulk job: **resolve serially and paced, fetch
    N-wide.** One copy, because two copies is how one of them quietly stops
    honouring the rate limit.

    The split is not an optimisation, it is where the limiter is. Resolving a
    track is API traffic — `session.track()` then `get_stream()` — and 53 of
    those in 2.8 s got the owner's IP blocked and his music stopped
    (ai/INCIDENTS #1). So resolving stays exactly as the serial re-fetch left
    it: one at a time, on this thread, never closer together than
    `REFETCH_MIN_INTERVAL` between *starts*, so a run of instant failures
    cannot become a burst either. Fetching is a plain GET to a CDN that the
    API limiter never sees, so up to `workers` of those run at once and the
    **peak API request rate is identical to a one-wide run's**. The re-fetch
    passes `workers=1` and is therefore the same code path it always was.

    Four properties, all structural:

    * **Bounded.** The resolver will not run ahead of the fetchers — it waits
      for a free slot before resolving the next item, so a signed stream URL
      is never left sitting for the hour it takes to expire, and a 500-track
      run holds three streams rather than five hundred.
    * **Interruptible.** `alive()` is checked before every item, while waiting
      for a slot, inside the pacing sleep, and inside `fetch_to_file`'s chunk
      loop through the caller's `abandoned`.
    * **Stop, never retry, on evidence of a block.** A 429, a 401 subStatus
      4006 or a bot-detection page from *either* half ends the whole run. The
      check sits after the slot wait, so at one worker the previous item's
      verdict is always in before the next one is resolved.
    * **Lock-free.** Workers only ever append to `results` and write their own
      slot; this thread is the only reader of the tally and the only writer of
      anything shared. `records` is still drained here alone, but for
      ordering and simplicity, not as the defence: the download index holds
      `downloads._index_lock` across its whole load-modify-save cycle — it
      has to, the UI thread's [x] delete overlaps a run no matter what this
      thread does — so the lock is the guarantee, and this thread just keeps
      commits in landing order and off the fetch workers.
    """

    def __init__(self, items, resolve, fetch, alive, report,
                 workers: int = 1, slots=(), clock=None):
        self.items = list(items)
        self.resolve = resolve
        self.fetch = fetch
        self.alive = alive
        self.report = report
        self.workers = max(1, workers)
        # One per worker, or None where the caller wants no per-slot display.
        # A slot has exactly one writer at a time — the worker holding it —
        # so its fields need no lock either.
        self.slots = tuple(slots) if slots else (None,) * self.workers
        # When the API was last asked anything, in a one-element list the
        # caller owns and every run shares. The floor has to outlive a single
        # run: a run that ends and another that starts a millisecond later
        # are two requests a millisecond apart, which is exactly what the
        # floor exists to prevent, and it does not care which run they were.
        self.clock = [None] if clock is None else clock
        # (ok, message, record) per finished item. Appended by workers, read
        # only here; append is atomic under the GIL.
        self.results: list = []
        self._written = 0
        # How far into `items` the resolver has got, and whether it has
        # announced that it is finishing. Written only by the run's own
        # thread; read by `add` and by the caller once the run is over.
        self.consumed = 0
        self.closed = False

    # ── what the caller asks for ──

    def run(self) -> tuple:
        """`(done, failed, blocked)`. Returns early and reports nothing when
        the run was cancelled — the canceller owns the state at that point.

        Indexed rather than `for item in self.items`, because `items` **grows
        while this is running**: asking for a second playlist appends to the
        list this loop is walking (`add`), and a run that has already reached
        the end goes round again and picks the new work up. That is the whole
        of the queueing, and it is why there is no second scheduler — one
        paced resolver, one pool, so "is the floor still honoured across the
        boundary between two batches" is not a new question. `clock`
        deliberately outlives the run itself, which is what makes that true
        of the boundary between two *runs* as well.
        """
        threads: dict = {}
        blocked = ""
        while True:
            while self.consumed < len(self.items):
                item = self.items[self.consumed]
                self.consumed += 1
                if not self.alive():
                    return None
                slot = self._free_slot(threads)
                if slot is None:
                    return None
                blocked = self._blocked()
                if blocked:
                    break
                # The floor goes *before* the next resolve rather than after
                # the last one, so it is still a floor between starts and a
                # run with nothing left to do finishes at once instead of
                # sleeping through an interval it does not need. One track
                # used to take two seconds to say "saved" for that reason.
                if self.clock[0] is not None:
                    self._pace(self.clock[0])
                    if not self.alive():
                        return None
                self.clock[0] = time.monotonic()
                self._flush()
                try:
                    handle = self.resolve(item)
                except _DownloadSuperseded:
                    return None
                except Exception as e:
                    message = str(e)
                    if _looks_rate_limited(message):
                        blocked = message[:PLAYER_ERROR_CHARS]
                        break
                    logger.debug("Could not resolve %s: %s", item, e)
                    self.results.append((False, message, None))
                    self._flush()
                    continue
                thread = threading.Thread(
                    target=self._work, args=(item, handle, slot), daemon=True)
                threads = {**threads, slot: thread}
                thread.start()
            for thread in threads.values():
                thread.join()
            threads = {}
            if not self.alive():
                return None
            # Announce **first**, then look again. Anything `add` published
            # before this flag was set is caught by the re-check below, so
            # the window in which an append can miss a run that is ending is
            # a couple of bytecodes rather than the whole of the join above
            # it. It is not zero: closing it outright wants a
            # compare-and-swap, and the rule here is no locks. So the caller
            # re-queues `pending()` afterwards, and the completion toast
            # counts anything that still never started.
            self.closed = True
            if blocked or self.consumed >= len(self.items):
                break
            self.closed = False
        blocked = blocked or self._blocked()
        self._flush()
        done = sum(1 for ok, _m, _r in self.results[:] if ok)
        return done, len(self.results) - done, blocked

    def pending(self) -> list:
        """Items published but never taken — empty except in the sliver of a
        window above. Read by the caller once the run is no longer live, so
        it can start them rather than lose them."""
        return self.items[self.consumed:]

    def add(self, items) -> bool:
        """Put more work on the end of a run that is already going.

        Whole-list replacement, never `append`, so `run()` can be part way
        through `self.items` on another thread and still see a consistent
        list — the same convention as everywhere else here. `items` is also
        the record of everything this run has *ever* accepted, which is what
        makes deduplicating against pending, in-flight and finished work one
        question rather than three.
        """
        items = list(items)
        if items:
            self.items = self.items + items
        # Published before this read, so a run that has not yet announced is
        # guaranteed to see them: True means "this run will do them"
        return not self.closed

    # ── the machinery ──

    def _work(self, item, handle, index) -> None:
        slot = self.slots[index]
        try:
            record = self.fetch(item, handle, slot)
            self.results.append((True, "", record))
        except _DownloadSuperseded:
            self.results.append((False, "cancelled", None))
        except Exception as e:
            logger.debug("Fetch of %s failed: %s", item, e)
            self.results.append((False, str(e), None))
        finally:
            # A fetch that got as far as finishing has already said so on its
            # own slot, and that row stays on screen as a tick until the slot
            # is taken again. Only an unfinished one is cleared here.
            if slot is not None and slot.get("state") == "running":
                slot["state"] = "idle"
            # The tally, but **not** the records: those are written by the one
            # thread that runs `_flush`, because a read-modify-write of the
            # download index from three threads at once loses rows
            self._tally()

    def _free_slot(self, threads):
        """The index of a worker that is not busy, waiting for one if they all
        are. None means the run was cancelled while waiting."""
        while self.alive():
            for index in range(self.workers):
                thread = threads.get(index)
                if thread is None or not thread.is_alive():
                    return index
            time.sleep(WORKER_POLL_SECONDS)
        return None

    def _pace(self, started: float) -> None:
        """The floor between the *start* of one item and the next. Read from
        the module at call time, so a test can shorten it."""
        remaining = REFETCH_MIN_INTERVAL - (time.monotonic() - started)
        while remaining > 0 and self.alive():
            time.sleep(min(0.25, remaining))
            remaining -= 0.25

    def _blocked(self) -> str:
        for ok, message, _record in self.results[:]:
            if not ok and _looks_rate_limited(message):
                return message[:PLAYER_ERROR_CHARS]
        return ""

    def _tally(self) -> None:
        """Tell the caller the whole tally, never a delta — every counter is
        recomputed from `results`, so two workers reporting at once cannot
        lose an update the way a read-modify-write of a shared dict would."""
        results = self.results[:]
        done = sum(1 for ok, _m, _r in results if ok)
        self.report(done=done, failed=len(results) - done)

    def _flush(self) -> None:
        """The tally, plus the side effects only the resolver thread may run.

        A finished fetch hands back a `record` callable rather than writing
        the download index itself: `downloads.record` is a read-modify-write
        of one JSON file, and three of those at once used to lose rows. The
        index now holds `downloads._index_lock` across that cycle — it has
        to, because the UI thread's [x] delete overlaps a run no matter what
        this thread does — so the lock is the guarantee and this thread is
        the design: commits stay in landing order, off the fetch workers,
        and this is still the only thread that reads or writes `_written`.
        """
        results = self.results[:]
        for _ok, _message, record in results[self._written:]:
            if record is not None:
                try:
                    record()
                except Exception as e:  # an index row is advisory, never fatal
                    logger.debug("Could not record a finished download: %s", e)
        self._written = len(results)
        self._tally()


def _empty_search_pool() -> dict:
    """Fetched-but-not-yet-shown search rows, per category."""
    return {"tracks": [], "albums": [], "artists": [], "playlists": []}


def _empty_search_reservoir() -> dict:
    """Everything one query has brought back, kept whole and never consumed.

    A fetch asks for all four categories at once, so this is what every scope
    draws on: `offset` is how deep into the query it has gone, `exhausted` is
    per category because they run out at different depths, and `stopped` is a
    failure the scopes should stop asking about. In memory only, for the length
    of the run — deliberately not the on-disk index in utils/cache.py.
    """
    return {
        "tracks": [], "albums": [], "artists": [], "playlists": [],
        "offset": 0,
        "exhausted": {"tracks": False, "albums": False,
                      "artists": False, "playlists": False},
        "stopped": False,
        "message": "",
    }


def _empty_search_view() -> dict:
    """One scope's answer to one query.

    `consumed` is how far into each of the reservoir's categories this scope
    has already drawn — the scope's paging depth, which is what makes tabbing
    away and back come back to the same rows. `cached` records that the view
    was shown without a request of its own, because the display says so.
    """
    return {
        "loading": False, "results": [], "cursor": 0, "message": "",
        "consumed": {"tracks": 0, "albums": 0, "artists": 0}, "cached": False,
    }


def _split_keys(data: str) -> list:
    """Split one raw stdin read into individual keys.

    Key repeat delivers several keys per read, and arrows are multi-byte
    escape sequences — so the split has to keep each sequence whole rather
    than letting one arrow swallow the bytes of the next.
    """
    keys = []
    i = 0
    while i < len(data):
        ch = data[i]
        if ch != "\x1b":
            keys.append(ch)
            i += 1
            continue
        j = i + 1
        if j < len(data) and data[j] in "[O":
            j += 1
            # Parameter bytes, then one final byte closes the sequence
            while j < len(data) and data[j] in "0123456789;":
                j += 1
            if j < len(data):
                j += 1
            keys.append(data[i:j])
            i = j
        else:
            # Bare Esc (or Alt-<key>, which the player doesn't bind)
            keys.append("\x1b")
            i += 1
    return keys


def _incomplete_escape(data: str) -> bool:
    """True if the read ended mid escape sequence, so the tail is still in
    flight — waiting for it stops an arrow key decoding as a bare Esc."""
    i = data.rfind("\x1b")
    if i < 0:
        return False
    tail = data[i:]
    if len(tail) == 1:
        return True
    if tail[1] not in "[O":
        return False
    return all(c in "0123456789;" for c in tail[2:])


def _find_audio_player():
    """Find an available audio player binary."""
    for player in AUDIO_PLAYERS:
        for path_dir in os.environ.get("PATH", "").split(os.pathsep):
            full = os.path.join(path_dir, player)
            if os.path.isfile(full) and os.access(full, os.X_OK):
                return player
    return None


def _instance_lock_path() -> Path:
    """Where the one-at-a-time lock lives.

    Derived at call time rather than bound at import, so redirecting
    `STATE_DIR` redirects this with it — the same reason `download_dir()`
    reads `DOWNLOAD_ROOT` when it is called.
    """
    return STATE_DIR / "instance.lock"


def _read_lock_pid(fd: int) -> int:
    """The pid the lock holder wrote, or 0 if it cannot be read.

    Only ever used to make the refusal message specific. Nothing depends on
    it being right — the lock itself is the mechanism.
    """
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return int(os.read(fd, 32).decode().strip() or 0)
    except (OSError, ValueError, UnicodeDecodeError):
        return 0


def _take_instance_lock():
    """Try to become the one running ticli.

    Returns `(fd, other)`. `other` is not None exactly when another instance
    holds the lock — the pid it recorded, or 0 if that could not be read —
    and that is the only case where the caller must refuse to start. `fd` is
    the descriptor holding the lock; it stays open for the life of the
    process, because closing it is what drops the lock.

    An advisory `flock`, deliberately not the pid file BUGS-2026-07-24 item 8
    proposed. The kernel releases a flock when the holder dies — SIGKILL, a
    closed terminal, a power cut — so there is no stale lock to detect and no
    recycled pid to misjudge, which is the entire failure mode of pid files
    and the reason this needs no cleanup path at all.

    When locking cannot be *evaluated* — no `fcntl`, or a filesystem that
    refuses to take one, which is the NFS home directory case — this returns
    `(None, None)` and ticli starts exactly as it did before. The guard is
    here to catch an honest mistake, and it must never be the thing that
    stops the owner playing his music.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - POSIX only, and Windows cannot start anyway
        return None, None
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(_instance_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        logger.debug("No instance lock (%s); starting anyway", e)
        return None, None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Somebody has it. This is the one case worth stopping for.
        other = _read_lock_pid(fd)
        os.close(fd)
        return None, other
    except OSError as e:
        # We could not ask the question; do not stand in the way of an answer
        # we never got.
        logger.debug("Instance lock unavailable (%s); starting anyway", e)
        os.close(fd)
        return None, None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass  # the pid is a courtesy for the message, never the mechanism
    return fd, None


class AudioPlayer:
    """Manages audio playback via external player (mpv or ffplay).

    Supports pause/resume:
    - mpv: uses IPC socket to send pause property commands
    - ffplay: kills process on pause, restarts from cached local file on resume
    """

    def __init__(self, player_cmd: str, volume: int = 100, cache=None):
        self.player_cmd = player_cmd
        # Shared MetadataCache — only consulted for where cached audio lives
        # and whether this build is allowed to keep it. None = never keep.
        self.cache = cache
        # 0–VOLUME_MAX. mpv takes it live over IPC, ffplay only at spawn — so
        # every spawn passes it too, and ffplay picks up a change on the next
        # track. Above 100 only mpv can actually go (see _ffplay_volume)
        self.volume = volume
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._paused = False
        self._ipc_path: Optional[str] = None
        # Local copy of the playing track: what ffplay resumes from, and what
        # FULL cache mode keeps
        self._current_url: Optional[str] = None
        self._cache_file: Optional[str] = None
        self._play_start: Optional[float] = None
        self._seek_offset: float = 0
        # macOS media keys: rebound per mpv process, title shown in Now Playing
        self._media_keys_bound = False
        self._media_title: Optional[str] = None
        # Bumped by every stop(), so a download still running for a track the
        # user has moved past knows to abandon itself
        self._download_gen = 0
        # True when _cache_file is a kept track, so stop() must not delete it —
        # the whole point of having cached it
        self._cache_persistent = False
        # Where the running player writes its complaints. Kept rather than
        # discarded: a backend that cannot play what it was handed says so on
        # stderr, and that sentence is the difference between a visible error
        # and a UI that pretends to be playing silence.
        self._stderr_path: Optional[str] = None
        self._stderr_handle = None

    def volume_ceiling(self) -> int:
        """The loudest this backend can actually be told to go.

        Discovered from the running backend rather than assumed per platform:
        ffplay refuses anything over 100 ("-volume=250 > 100, setting to 100",
        straight out of ffplay.c), while mpv amplifies in software up to the
        --volume-max every spawn passes it. mpv-on-Linux and mpv-on-macOS are
        the same answer, so nothing here has to predict an OS.

        Anything unrecognised — a backend added later, a mangled player_cmd —
        gets unity, never the higher number: a ceiling guessed too high shows
        the user a volume they will not hear.
        """
        return BACKEND_VOLUME_CEILINGS.get(self.player_cmd, SAFE_VOLUME_CEILING)

    def _ffplay_volume(self) -> int:
        """What ffplay can be told. Its -volume is 0=min 100=max and clips
        there, so amplification above unity is an mpv-only capability."""
        return min(FFPLAY_VOLUME_MAX, max(0, int(self.volume)))

    def _sweep_cache(self):
        """Make the cache fit its budget again. Called when a download lands,
        never on a timer."""
        try:
            self.cache.enforce_budget()
        except Exception as e:  # a cache sweep must never break playback
            logger.debug("Cache sweep failed: %s", e)

    def _drop_other_copies(self, base: str, keeping: str) -> None:
        """Remove any earlier cached copy of the same track.

        Only `{track_id}{ext}` under the cache's own audio directory, only
        ones this module wrote, never the one that just landed and never a
        directory — the same rule as everywhere else here.
        """
        directory, stem = os.path.split(base)
        try:
            for path in Path(directory).glob(f"{stem}.*"):
                if str(path) == keeping or path.suffix == ".part":
                    continue
                if not is_owned_audio(path.name) or not path.is_file():
                    continue
                try:
                    path.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def _admit(self, key) -> bool:
        """Whether the cache will take this track. One question, one place.

        Asked at the start of the download today; the rule is written so that
        asking it at the *end* of the track instead — when "was it actually
        listened to?" is answerable — needs no change to the rule, only a
        different caller.
        """
        try:
            return self.cache.should_cache(key)
        except Exception:  # pragma: no cover - a bookkeeping failure caches
            return True

    def _audio_cache_base(self, key) -> Optional[str]:
        """Where a kept copy of this track lives, minus the extension — which
        isn't known until the CDN answers. None when audio caching is off (or
        there's nothing to key it by)."""
        if not self.cache or not self.cache.keeps_audio or key in (None, ""):
            return None
        try:
            return str(self.cache_audio_dir() / str(key))
        except OSError:
            return None

    def _cached_audio_path(self, key) -> Optional[str]:
        """A whole track already on disk for this key, or None.

        Looked up by stem, because the extension is whatever the stream turned
        out to be. One glob of a directory we own beats an index that could go
        stale; a half-written ".part" is never an answer.
        """
        if not self._audio_cache_base(key):
            return None
        return cached_audio_path(key)

    def _start_download(self, url: str, cache_key, gen: int, quality=None):
        """Fetch the whole track to disk on a daemon thread.

        TIDAL hands out unencrypted URLs, so this is a plain GET: the bytes
        that land are the bytes it sent, byte for byte. That makes it
        independent of the player process — mpv and ffplay both get a cached
        track — and it needs no ffmpeg. Nothing waits on it, and the file only
        becomes an answer once it is whole.

        A segmented (MPEG-DASH) stream is the same job with more requests:
        the initialization segment followed by every media segment, written
        end to end, *is* the fragmented MP4 file. Verified by playing the
        result — both backends open it with no flags at all.
        """
        sources = stream_sources(url)
        if not sources:
            return
        base = self._audio_cache_base(cache_key)
        if base is not None and not self._admit(cache_key):
            # The cache is full of things worth more than this one. Refusing
            # is not a failure: the track streams exactly as it would with
            # song caching switched off, which is the behaviour this is
            # deliberately identical to.
            base = None
        keep = base is not None
        if not keep:
            if self.player_cmd != "ffplay":
                return  # mpv pauses in place and needs no local copy
            # ffplay's pause kills the process, so it needs something to
            # resume from even when nothing is being kept
            base = os.path.join(tempfile.gettempdir(), f"ticli-cache-{os.getpid()}")
        part = base + ".part"

        def _drop(path):
            if not path:
                return
            try:
                os.unlink(path)
            except OSError:
                pass

        def _run():
            path = None
            try:
                ext = fetch_to_file(sources, part,
                                    abandoned=lambda: self._download_gen != gen)
                path = base + ext
                os.replace(part, path)
                # A re-fetch at a higher tier can change the container
                # (AAC-in-MP4 to FLAC-in-MP4 is the same .m4a, but not
                # always), and _cached_audio_path globs by stem — so the
                # copy this one replaces has to go, or the stale one could
                # be the answer. By exact name, and only ones ticli wrote.
                if keep:
                    self._drop_other_copies(base, path)
            except Exception as e:
                # A missed cache is a slower track, never a broken one — and
                # a partial file must not survive to be mistaken for a whole
                logger.debug("Audio download did not finish: %s", e)
                _drop(part)
                _drop(path)
                return
            if self._download_gen == gen:
                self._cache_file = path
                self._cache_persistent = keep
                if self._download_gen != gen:
                    # stop() ran between the check and the assignment
                    self._cache_file = None
                    self._cache_persistent = False
                    if not keep:
                        _drop(path)
            elif not keep:
                # The user moved on. A whole track still belongs in the cache;
                # a scratch copy for a player that has stopped does not.
                _drop(path)
            if keep and os.path.exists(path):
                # One more song on disk: the settings page's count is stale,
                # and the tracker has to learn about it *before* the sweep,
                # or the sweep will value it at nothing and evict it first
                try:
                    self.cache.note_cached(
                        cache_key, ext, os.path.getsize(path), quality=quality)
                    self.cache.invalidate_audio_count()
                except Exception:  # pragma: no cover - bookkeeping only
                    pass
                self._sweep_cache()

        threading.Thread(target=_run, daemon=True).start()

    def _open_stderr(self):
        """A fresh file for the next player process to complain into.

        One file per AudioPlayer, truncated per spawn, so nothing accumulates
        and the contents always belong to the process now running. A pipe
        would be wrong here: nothing reads it until the process is already
        dead, and a full pipe buffer would wedge the player mid-track.
        """
        try:
            if self._stderr_handle is not None:
                self._stderr_handle.close()
        except OSError:
            pass
        self._stderr_handle = None
        try:
            path = os.path.join(tempfile.gettempdir(), f"ticli-player-{os.getpid()}.log")
            self._stderr_handle = open(path, "w+")
            self._stderr_path = path
            return self._stderr_handle
        except OSError:
            # Losing the log costs an error message, never playback
            self._stderr_path = None
            return subprocess.DEVNULL

    def _last_stderr(self) -> str:
        """The last thing the player said, trimmed to fit a toast."""
        if not self._stderr_path:
            return ""
        try:
            lines = [ln.strip() for ln in
                     Path(self._stderr_path).read_text(errors="replace").splitlines()]
        except OSError:
            return ""
        said = [ln for ln in lines if ln]
        if not said:
            return ""
        return said[-1][:PLAYER_ERROR_CHARS]

    def failure(self) -> Optional[str]:
        """Why playback stopped, when it stopped because it failed.

        None while the process is running, when it reached the end of the
        track (exit 0), and when *we* ended it (a signal, i.e. a negative
        return code — that is stop() and pause() doing their job). Anything
        else is the backend refusing the stream, which the user has to be
        told about rather than left listening to silence.
        """
        with self._lock:
            process = self._process
            if process is None:
                return None
            code = process.poll()
            if code is None or code <= 0:
                return None
            detail = self._last_stderr()
        return f"{self.player_cmd} error: {detail}" if detail else \
            f"{self.player_cmd} exited with status {code}"

    def _hls_flags(self) -> list:
        """What each backend needs to demux a local HLS playlist.

        Both end up in the same ffmpeg HLS demuxer; only the spelling differs.
        mpv would otherwise treat an .m3u8 as a list of files to play one
        after another, and each fMP4 fragment on its own is not a file any
        demuxer can open.

        The cache flags are here for the same reason as the rest: handing mpv
        a local playlist for a remote stream is what made it stop buffering
        like a network player (see HLS_CACHE_SECONDS).
        """
        if self.player_cmd == "mpv":
            return [
                "--demuxer=lavf", "--demuxer-lavf-format=hls",
                # mpv's key-value lists split on commas, so the whitelist has
                # to be passed length-prefixed to survive intact
                f"--demuxer-lavf-o=protocol_whitelist="
                f"%{len(HLS_PROTOCOLS)}%{HLS_PROTOCOLS}",
                "--cache=yes", f"--cache-secs={HLS_CACHE_SECONDS}",
            ]
        # ffplay had the same weakness mpv did, and here is the measurement on
        # one 45-segment playlist (990 kbps, 176 s, 21.8 MB), bytes served:
        #
        #   mpv, --cache=yes --cache-secs=60   9.19 MB @1s   10.16 MB @8s
        #   mpv, no cache flags (the old bug)  1.88          2.86
        #   ffplay, before this line           1.40          2.37
        #   ffplay, with -infbuf              21.71         21.71
        #
        # ffplay sat at the *un-fixed* mpv level — about two segments, ~11 s —
        # because its read thread stops once every stream has enough packets
        # queued (MIN_FRAMES / stream_has_enough_packets in ffplay.c). Any
        # segment slower than that window is an audible hitch, which is the
        # symptom that was reported for mpv.
        #
        # There is no --cache-secs analogue for ffplay, so the choice is
        # between ~11 s and unbounded, and -infbuf pulls the whole track in
        # under a second. For a VOD track of known, bounded length (~30 MB
        # hi-res) unbounded is the right side of that trade. Segmented only:
        # on the BTS path both backends already read the whole file at once
        # (ffplay 23.7 MB @1s) and need nothing.
        return ["-infbuf", "-protocol_whitelist", HLS_PROTOCOLS, "-f", "hls"]

    def cache_audio_dir(self):
        """The audio cache directory, created if needed. Private like the rest
        of the cache — these are someone's listening habits."""
        from ticli.utils import cache as cache_mod
        cache_mod.CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = cache_mod.audio_dir()
        path.mkdir(exist_ok=True, mode=0o700)
        return path

    def play_url(self, url: str, seek: float = 0, title: Optional[str] = None,
                 cache_key=None, local: Optional[str] = None, quality=None):
        """Play an audio URL, stopping any current playback.

        With audio caching on, a track played before is already on disk, so
        the URL is never touched; a track played for the first time is fetched
        alongside playback and kept. Both halves are backend-independent —
        only the source path and a background download change.

        `local` is a deliberately downloaded copy (see utils/downloads.py) and
        outranks both: it is the same bytes, it is the user's file rather than
        ours, and it means a downloaded library plays offline. Like the cache,
        it is verified at the moment of use rather than trusted — a file the
        user deleted by hand falls straight through to the network, which is
        the whole of "handled durably" on this path.
        """
        with self._lock:
            have_kept, gen = self._play_url_locked(
                url, seek, title, cache_key, local)
        # Off the lock and off this thread: fetching the track must never hold
        # up the process that is playing it
        if not have_kept:
            self._start_download(url, cache_key, gen, quality=quality)

    def _play_url_locked(self, url: str, seek: float = 0,
                         title: Optional[str] = None, cache_key=None,
                         local: Optional[str] = None):
        """play_url's body, for callers already holding self._lock.

        Kills the previous backend and spawns the new one inside a *single*
        acquisition. This used to be `stop()` — which takes the lock, reaps,
        and releases — followed by a second `with self._lock:` around the
        spawn, and the instant between the two was a real bug: a second
        starter arriving there found `_process` already None, reaped nothing,
        and spawned on top. Both processes then played, and only the last one
        was reachable — `_process` is the sole handle anything stops, pauses or
        seeks, and mpv's IPC socket is one fixed path per ticli pid that the
        last spawn takes over. The loser kept making sound, ignored the space
        bar, and outlived the app.

        `self._lock` is a plain, non-reentrant Lock, so a caller holding it
        cannot call `stop()` at all; the unlocked `_locked` half is the shape
        ai/WORKING-RULES.md prescribes for exactly this, and the one `seek_to`
        has always had. Returns (have_kept, gen) so the caller can start the
        download after releasing.
        """
        self._stop_locked()
        self._paused = False
        self._current_url = url
        self._media_title = title
        self._media_keys_bound = False
        self._seek_offset = seek
        self._play_start = time.time()
        kept = local if (local and os.path.exists(local)) else \
            self._cached_audio_path(cache_key)
        # A track played before is already whole on disk: play the file and
        # never touch the network. Works on both backends, because only the
        # source path changes.
        have_kept = bool(kept) and os.path.exists(kept)
        source = kept if have_kept else url
        self._cache_persistent = have_kept
        if have_kept:
            self._cache_file = kept
        segmented = source.endswith(HLS_SUFFIX)
        if self.player_cmd == "mpv":
            self._ipc_path = f"/tmp/ticli-mpv-{os.getpid()}.sock"
            try:
                os.unlink(self._ipc_path)
            except OSError:
                pass
            cmd = [
                "mpv", "--no-video",
                # Not --really-quiet: that is what made a stream mpv
                # could not decode look exactly like one it could
                "--msg-level=all=error",
                f"--input-ipc-server={self._ipc_path}",
                # --volume-max first: mpv refuses anything above it, and
                # its default ceiling (130) is below what the setting allows
                f"--volume-max={VOLUME_MAX}",
                f"--volume={self.volume}",
            ]
            if segmented:
                cmd += self._hls_flags()
            if seek > 0:
                cmd.append(f"--start={seek}")
            cmd.append(source)
        else:  # ffplay
            self._ipc_path = None
            # Plays from `source`: a kept file when there is one, the URL
            # otherwise — a download that has only just started can't
            # satisfy a seek yet
            cmd = self._ffplay_cmd(source, seek)
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=self._open_stderr(),
        )
        return have_kept, self._download_gen

    def _ffplay_cmd(self, source: str, seek: float) -> list:
        """How ffplay is asked to play `source` from `seek`.

        The one place that spelling lives, because ffplay is respawned from
        three of them — a fresh track, a resume, and a scrub — and a flag
        missing from one of those is a stream that plays everywhere but there.
        """
        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error",
               "-volume", str(self._ffplay_volume())]
        if source.endswith(HLS_SUFFIX):
            cmd += self._hls_flags()
        if seek > 0:
            cmd += ["-ss", str(seek)]
        cmd.append(source)
        return cmd

    def _play_from_cache(self, seek: float):
        """Resume ffplay from local cached file at given position."""
        self._process = subprocess.Popen(
            self._ffplay_cmd(self._cache_file, seek),
            stdout=subprocess.DEVNULL,
            stderr=self._open_stderr(),
        )
        self._play_start = time.time()
        self._paused = False

    def _reap_process(self):
        """Terminate the audio subprocess, escalating to kill after 2s.

        Callers hold self._lock and decide what _process becomes afterwards.
        """
        self._process.terminate()
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()
            try:
                # SIGKILL is unblockable, but the reap is not finished until
                # the child has been waited for — and the caller is about to
                # drop the last handle that could ever collect it. Without
                # this it stays a zombie for the life of the session.
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logger.warning("audio process %s survived SIGKILL",
                               getattr(self._process, "pid", "?"))

    def pause(self):
        """Pause playback."""
        with self._lock:
            if not self._process or self._process.poll() is not None or self._paused:
                return
            if self.player_cmd == "mpv" and self._ipc_path:
                # The IPC socket takes ~100ms to come up after spawn — retry
                # briefly so an immediate pause isn't silently lost, and only
                # mark paused once mpv actually acknowledged the command
                for _ in range(5):
                    if self._mpv_command({"command": ["set_property", "pause", True]}):
                        self._paused = True
                        return
                    if not self._process or self._process.poll() is not None:
                        return
                    time.sleep(0.1)
            else:
                # ffplay: record position, kill process (instant silence)
                elapsed = time.time() - self._play_start if self._play_start else 0
                self._seek_offset += elapsed
                self._play_start = None
                self._reap_process()
                self._process = None
                self._paused = True

    def resume(self) -> bool:
        """Resume paused playback. False if it failed (e.g. player died)."""
        download = None
        with self._lock:
            if not self._paused:
                return False
            if self.player_cmd == "mpv" and self._ipc_path:
                if self._mpv_command({"command": ["set_property", "pause", False]}):
                    self._paused = False
                    return True
                return False
            # ffplay: restart from cached local file (instant, no network)
            if self._cache_file and os.path.exists(self._cache_file):
                self._play_from_cache(self._seek_offset)
                return True
            if not self._current_url:
                return False
            # Cache not ready — fall back to URL. This used to release the
            # lock by hand to call play_url and re-acquire afterwards, which
            # is the same seam as the one _play_url_locked exists to close,
            # reached by pressing space on ffplay before the copy is ready.
            url = self._current_url
            seek = self._seek_offset
            self._paused = False
            have_kept, gen = self._play_url_locked(url, seek)
            if not have_kept:
                download = (url, None, gen)
        # Off the lock, exactly as play_url does it
        if download:
            self._start_download(*download)
        return True

    def _mpv_request(self, cmd: dict, timeout: float = 0.5) -> Optional[dict]:
        """Send a JSON IPC command to mpv and return its reply, or None."""
        if not self._ipc_path:
            return None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                sock.connect(self._ipc_path)
                payload = dict(cmd)
                payload["request_id"] = 1
                sock.sendall((json.dumps(payload) + "\n").encode())
                # mpv also broadcasts events on this socket — scan lines for
                # the reply carrying our request_id
                buf = b""
                deadline = time.time() + timeout
                while time.time() < deadline:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    for line in buf.split(b"\n"):
                        if not line.strip():
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("request_id") == 1:
                            return msg
            finally:
                sock.close()
        except OSError:
            pass
        return None

    def _mpv_command(self, cmd: dict) -> bool:
        """Send an mpv IPC command; True only if mpv acknowledged it."""
        reply = self._mpv_request(cmd)
        return reply is not None and reply.get("error") == "success"

    def get_time_pos(self) -> Optional[float]:
        """Ask mpv for its actual playback position. None if unavailable."""
        if self.player_cmd != "mpv":
            return None
        with self._lock:
            if self._paused or not self._process or self._process.poll() is not None:
                return None
            reply = self._mpv_request({"command": ["get_property", "time-pos"]}, timeout=0.2)
        if reply and reply.get("error") == "success" and isinstance(reply.get("data"), (int, float)):
            return float(reply["data"])
        return None

    def set_volume(self, value: int):
        """Set playback volume (0–VOLUME_MAX). mpv applies it to the playing track
        right away; ffplay can't be told mid-track, so it waits for the next
        spawn — either way the stored value is what future tracks start at."""
        with self._lock:
            self.volume = value
            if (self.player_cmd == "mpv" and self._ipc_path
                    and self._process and self._process.poll() is None):
                self._mpv_command({"command": ["set_property", "volume", value]})

    def seek_to_start(self) -> bool:
        """Jump the playing track back to 0:00 in place. True only if mpv
        acknowledged the seek — every other case has to respawn instead."""
        if self.player_cmd != "mpv":
            return False
        with self._lock:
            if self._paused or not self._process or self._process.poll() is not None:
                return False
            if not self._mpv_command({"command": ["seek", 0, "absolute"]}):
                return False
            self._seek_offset = 0
            self._play_start = time.time()
            return True

    def seek_to(self, position: float) -> bool:
        """Move the current track to an absolute position. False when the
        backend could not be moved and the caller has to start the track again.

        Three cases, and none of them costs a request — the source this is
        already playing is the source it seeks in:

        - **mpv**, playing or paused: one IPC seek, gapless, and mpv keeps
          whatever pause state it had.
        - **ffplay while paused**: there is no process (pause kills it), so
          the position it will resume from is just a number, and moving that
          number is the whole seek.
        - **ffplay while playing**: no runtime control exists at all, so the
          process is killed and started again at the offset from the same
          source. Deliberately not stop()/play_url(): those bump the download
          generation, which would abandon the copy being fetched for this very
          track and make every scrub start it over.
        """
        position = max(0.0, position)
        with self._lock:
            alive = self._process is not None and self._process.poll() is None
            if self.player_cmd == "mpv" and self._ipc_path and alive:
                if not self._mpv_command({"command": ["seek", position, "absolute"]}):
                    return False
                self._seek_offset = position
                self._play_start = None if self._paused else time.time()
                return True
            if self._paused and not alive:
                # ffplay's pause killed the process, so there is nothing to
                # seek — only the number resume() will start from
                self._seek_offset = position
                return True
            if not alive or self.player_cmd == "mpv":
                # Nothing to talk to (or an mpv that stopped answering): the
                # caller starts the track again at the new position instead
                return False
            source = self._cache_file if (
                self._cache_file and os.path.exists(self._cache_file)) else self._current_url
            if not source:
                return False
            self._reap_process()
            self._process = subprocess.Popen(
                self._ffplay_cmd(source, position),
                stdout=subprocess.DEVNULL,
                stderr=self._open_stderr(),
            )
            self._seek_offset = position
            self._play_start = time.time()
            return True

    def _bind_media_keys(self) -> bool:
        """Point mpv's media keys at MEDIA_KEY_PROP so ticli handles them."""
        for key, action in MEDIA_KEY_ACTIONS.items():
            if not self._mpv_command(
                {"command": ["keybind", key, f"set {MEDIA_KEY_PROP} {action}"]}
            ):
                return False
        if self._media_title:
            # Shown in Control Center / lock screen instead of the raw stream URL
            self._mpv_command(
                {"command": ["set_property", "force-media-title", self._media_title]}
            )
        return True

    def poll_media_key(self) -> Optional[str]:
        """Return a pending media-key action, or None. macOS + mpv only."""
        if not IS_MACOS or self.player_cmd != "mpv" or not self._ipc_path:
            return None
        if not self._media_keys_bound:
            self._media_keys_bound = self._bind_media_keys()
            if not self._media_keys_bound:
                return None
        reply = self._mpv_request({"command": ["get_property", MEDIA_KEY_PROP]})
        # The property only exists once a key was actually pressed
        if not reply or reply.get("error") != "success" or not reply.get("data"):
            return None
        self._mpv_command({"command": ["set_property", MEDIA_KEY_PROP, ""]})
        return reply["data"]

    def stop(self):
        """Stop current playback."""
        with self._lock:
            self._stop_locked()

    def _stop_locked(self):
        """stop()'s body, for callers already holding self._lock.

        Split out for `_play_url_locked`, which must reap and respawn without
        ever handing the lock back — see the note there for what the gap cost.
        """
        # Any download still running is for a track we are leaving: this
        # is what tells it to abandon itself and clean up its .part
        self._download_gen += 1
        if self._process:
            if self._process.poll() is None:
                self._reap_process()
            # Cleared even when it had already exited: a dead handle left here
            # is not a process, it is a stale answer. `failure()` reads it, so
            # keeping it meant a stream the backend had refused went on
            # reporting its error after we stopped the track.
            self._process = None
        # A kept track is the whole point of having cached it; a scratch
        # copy existed only to resume this one, so it goes.
        if self._cache_file and not self._cache_persistent:
            try:
                os.unlink(self._cache_file)
            except OSError:
                pass
        self._cache_file = None
        self._cache_persistent = False
        self._paused = False
        self._play_start = None
        self._seek_offset = 0
        self._media_keys_bound = False
        # Clean up mpv socket
        if self._ipc_path:
            try:
                os.unlink(self._ipc_path)
            except OSError:
                pass
            self._ipc_path = None

    def source_vanished(self) -> bool:
        """True when the cached file we were playing has been deleted out from
        under us — the millisecond window between checking it exists and the
        player opening it. Playing from a URL is never this."""
        path = self._cache_file if self._cache_persistent else None
        return bool(path) and not os.path.exists(path)

    @property
    def is_playing(self) -> bool:
        with self._lock:
            if self._paused:
                return True  # Paused but track is active
            return self._process is not None and self._process.poll() is None

    @property
    def is_paused(self) -> bool:
        with self._lock:
            if not self._paused:
                return False
            if self.player_cmd == "mpv":
                # A paused mpv must still be alive; a dead one can't resume.
                # (ffplay pause kills the process by design, so no check there.)
                return self._process is not None and self._process.poll() is None
            return True


class HeadlessTidalPlayer:
    """Headless TIDAL player - no desktop app required."""

    MODE_PLAYER = "player"
    MODE_SEARCH = "search"
    MODE_BROWSE = "browse"
    MODE_QUEUE = "queue"
    MODE_PLAYLISTS = "playlists"
    MODE_ADD_TO_PLAYLIST = "add_to_playlist"
    MODE_SETTINGS = "settings"
    MODE_ARTIST = "artist"
    MODE_DOWNLOADS = "downloads"

    # The artist page's tabs, in the order Tab walks them. Each one is a
    # different TIDAL endpoint and each is fetched the first time you land on
    # it — see _load_artist_section.
    ARTIST_SECTIONS = ("tracks", "albums", "playlists", "suggestions")
    ARTIST_SECTION_LABELS = {
        "tracks": "Top Tracks",
        "albums": "Albums",
        "playlists": "Playlists",
        "suggestions": "Suggestions",
    }
    # Empty and failed are different facts and get different words: the first
    # says what TIDAL knows about this artist, the second that we never got to
    # ask. A blank screen would say neither.
    ARTIST_SECTION_EMPTY = {
        "tracks": "No tracks for this artist",
        "albums": "No albums for this artist",
        "playlists": "No playlists feature this artist",
        "suggestions": "No suggestions for this artist",
    }
    ARTIST_SECTION_FAILED = {
        "tracks": "Failed to load top tracks",
        "albums": "Failed to load albums",
        "playlists": "Failed to load playlists",
        "suggestions": "Failed to load suggestions",
    }

    # Setting name → tidalapi quality. The names are TIDAL's own player's
    # (Low / Medium / High / Max), which is *not* tidalapi's spelling — this
    # dict is the single translation between the two vocabularies. Everything
    # persisted (`granted`, the cache tracker's tier) and QUALITY_RANK stay in
    # tidalapi's spelling; the settings page, the badge and the CLI speak
    # these. (Older configs used tidalapi-style names; utils.config migrates
    # them.)
    QUALITY_MAP = {
        "LOW": tidalapi.Quality.low_96k,
        "MEDIUM": tidalapi.Quality.low_320k,
        "HIGH": tidalapi.Quality.high_lossless,
        "MAX": tidalapi.Quality.hi_res_lossless,
    }
    # Search scopes, in the order Tab cycles them. The four server-side ones
    # come first and share one request; "playlists" — *your* playlists — is
    # last on purpose, because it is the odd one out: TIDAL has no server-side
    # search of your own playlists, so that scope is answered from the local
    # index and never asks the network at all.
    #
    # Two playlist scopes, and the scope key is not the category key: the
    # reservoir's categories are named the way session.search() names them,
    # and "tidal_playlists" is a *scope* over the "playlists" category, while
    # the scope literally called "playlists" reads no category at all.
    SEARCH_FILTERS = ("all", "tracks", "albums", "artists",
                      "tidal_playlists", "playlists")
    SEARCH_FILTER_LABELS = {
        "all": "All",
        "tracks": "Tracks",
        "albums": "Albums",
        "artists": "Artists",
        "tidal_playlists": "Playlists",
        "playlists": "My Playlists",
    }
    # Which categories a scope asks TIDAL for. Keyed by the plural names
    # session.search() answers with, so the fetch never has to translate.
    SEARCH_FILTER_KINDS = {
        "all": ("tracks", "albums", "artists", "playlists"),
        "tracks": ("tracks",),
        "albums": ("albums",),
        "artists": ("artists",),
        "tidal_playlists": ("playlists",),
    }

    # The badge is the stream's format — the owner's call (2026-08-02): "I'd
    # like formats even if sometimes it falls back to a slightly different
    # quality." The drift he is accepting sits in MAX, whose label is the
    # tier's nominal ceiling: the master decides the real resolution, and a
    # 24/192 badge over a 24/48 file is a promise being quoted, not a
    # measurement. The AAC rates are effectively exact, and 16/44.1 is what
    # HIGH always is.
    QUALITY_LABELS = {
        "LOW": "96k AAC",
        "MEDIUM": "320k AAC",
        "HIGH": "16/44.1 FLAC",
        "MAX": "24/192 FLAC",
    }

    def __init__(self, quality: Optional[str] = None, login_flow: Optional[str] = None):
        self.console = Console()
        self.session = tidalapi.Session()
        # Only consulted when there is no stored session to reuse, so it is a
        # per-run flag rather than a setting: a settings row for it would sit
        # there doing nothing on every run after the first. Moving an existing
        # session onto PKCE is the settings page's job instead.
        flow = (login_flow or LOGIN_FLOWS[0]).lower()
        self._login_flow = flow if flow in LOGIN_FLOWS else LOGIN_FLOWS[0]
        # Set once the TUI owns the terminal, so a PKCE sign-in from the
        # settings page can hand it back for the length of a paste
        self._live = None
        self._tty_settings = None
        # The best tier TIDAL has actually granted us, when that was less than
        # what we asked for. None means "never seen a downgrade", which is the
        # only honest default — the settings page gates nothing until then.
        self._quality_ceiling: Optional[str] = None
        self.audio = None  # set after finding player
        self.running = True
        self._mode = self.MODE_PLAYER
        # Persisted user settings (see utils/config.py). Only the main thread
        # mutates this dict; every edit is written through immediately.
        self.config = load_config()
        self._page_size = self.config["page_size"]
        self._bar_max = self.config["progress_bar_max"]
        # Disk cache. Nothing is read or written until a list actually asks
        # for it, so building a player never touches the cache directory.
        self._cache = MetadataCache(
            metadata=self.config["cache_metadata"],
            songs=self.config["cache_songs"],
            budget_gb=self.config["cache_budget_gb"],
        )
        # Playback state
        self._current_track: Optional[tidalapi.Track] = None
        self._queue: list = []
        self._queue_index: int = -1
        self._playing = False
        self._play_start_time: Optional[float] = None
        self._play_offset: float = 0
        # Serializes every load-modify-save cycle over player_state.json.
        # The writers are on different threads — `_save_state` on the monitor
        # thread every 10s and on the main thread at shutdown,
        # `_merge_position_into_saved_state` under it while a restore is
        # pending, `_remember_last_playlist` on the add-to-playlist thread —
        # and each one reads the file (or in-memory fields), edits, and writes
        # the whole file back. Unlocked, a writer that reads before another's
        # write and writes after it erases that write: an autosave landing
        # inside `_remember_last_playlist`'s cycle lost the fresh queue and
        # position, the reverse lost the playlist pin. Same shape as the cache
        # tracker's `_tracker_lock` (utils/cache.py): a leaf lock held only
        # across one whole cycle, taken exactly once per cycle — nothing
        # inside it may call another lock-taking state function, which is why
        # `_save_state` reaches the merge through its unlocked `_locked` half.
        # Reads (`_restore_state`, bare `_read_state_dict` callers) stay
        # lock-free, and stay right: `_write_state_file` replaces the file
        # whole (temp + rename), so a reader sees one generation of it, never
        # a torn one.
        self._state_lock = threading.Lock()
        # The one-at-a-time lock, taken in run(). Held open for the life of
        # the process on purpose: closing this descriptor is what releases it,
        # so it is never closed — the kernel does it at exit, which is also
        # what makes a crash release it.
        self._instance_lock_fd: Optional[int] = None
        self._liked_ids: set = set()
        # Track radio: a fetch is in flight, and when the last one started
        self._radio_fetching = False
        self._radio_last_fetch = 0.0
        # Scrubbing. _player_focus is what ↑ hands to the player: while it is
        # set, ←/→ move within the track instead of between tracks (or in the
        # list below). _seek_target is the position the user has scrubbed to
        # and _seek_applied the last one the backend was actually told about —
        # they differ only while a press is waiting out SEEK_COALESCE_SECONDS.
        self._player_focus = False
        self._seek_target: Optional[float] = None
        self._seek_applied: Optional[float] = None
        self._seek_applying = False
        self._last_seek_apply = 0.0
        # Search state. Results, cursor and message are not attributes: they
        # are properties over the *view* for the scope on screen, so Tab is a
        # change of which view is being read and nothing has to be copied.
        self._search_query = ""
        self._search_history: list = []  # recent searches, newest first
        # The recall list's cursor. None means focus is in the query box —
        # the state search always starts in — and an index means ↓ has
        # entered the list of recent searches the blank pane shows. Every
        # path that dismisses that list (typing, running a search, entering
        # search mode afresh) puts this back to None, so it can never point
        # into a list that is gone or past one that shrank.
        self._search_history_cursor: Optional[int] = None
        # Which scope the query runs in, and what the session has in hand for
        # it: one reservoir of rows per query, and one view per scope drawing
        # on it. Both are dropped the moment the query changes.
        self._search_filter = "all"
        self._search_key = ""  # the query the reservoir and views belong to
        self._search_reservoir: dict = _empty_search_reservoir()
        self._search_views: dict = {}
        self._search_fetching = False  # a page is in flight (any scope)
        self._search_last_fetch = 0.0
        # Bumped whenever the query is reset, so a fetch that lands after the
        # fact knows to throw its page away. Not on a scope change: that page
        # is still for this query, and every scope is served out of it.
        self._search_gen = 0
        # Browse state
        self._browse_title = ""
        self._browse_tracks = []
        self._browse_cursor = 0
        self._browse_loading = False
        self._browse_message = ""
        # Set when browse is showing one of the user's own (editable) playlists
        self._browse_playlist = None
        # Artist page state. Every section is its own request, so none of them
        # runs until you tab onto it, and the answer is kept for the session in
        # _artist_sections keyed by (artist id, section): walking the tabs back
        # and forth costs one request per tab, ever, and never a second.
        # _artist_cursors remembers where you were in each of them.
        self._artist = None
        self._artist_section = self.ARTIST_SECTIONS[0]
        self._artist_cursor = 0
        self._artist_sections: dict = {}
        self._artist_cursors: dict = {}
        self._browse_remove_busy = False
        # Queue view state
        self._queue_cursor = 0
        # Playlists state
        self._playlists: list = []
        self._playlists_cursor = 0
        self._playlists_loading = False
        self._playlists_message = ""
        # Add-to-playlist picker state
        self._editable_playlists: list = []
        self._editable_playlists_time: float = 0.0
        self._picker_track = None
        self._picker_cursor = 0
        self._picker_loading = False
        self._picker_busy = False
        # The name being typed into the "+ New playlist" row, or None when it
        # is not open. None vs "" is the whole state: "" is an open prompt with
        # nothing typed yet, which is not the same as no prompt at all.
        self._picker_new_name: Optional[str] = None
        # The playlist a track was last added to, pinned to the top of the
        # picker and persisted in the state file (see _remember_last_playlist).
        self._last_playlist_id: Optional[str] = None
        # Download overlay state. _download_open is the box being up — it is a
        # flag rather than a mode because the box is drawn *over* whatever
        # screen [d] was pressed on, the same shape as _volume_open.
        # _download_track is what it was pressed on, _download_cursor the tier
        # under the cursor (pre-placed on the one settings is set to), and
        # _download_job the one job at a time — a whole dict, replaced rather
        # than mutated, so the paint thread can never read it half-written.
        self._download_open = False
        self._download_track = None
        # Everything the box is about. One entry is the ordinary single-track
        # box, unchanged; more than one makes it a confirmation for a whole
        # playlist, album, artist section or queue, and _download_label is
        # what to call them. _download_track stays the first of them, so the
        # single-track readouts need no branch.
        self._download_tracks: list = []
        self._download_label = ""
        self._download_cursor = 0
        self._download_job = None
        self._download_job_gen = 0
        # The live _PacedRun, while there is one. A second ask appends to it
        # rather than starting a run beside it, so there is only ever one
        # serial paced resolver — see _start_bulk_download_job.
        self._download_run = None
        # When the API was last asked for a stream, shared by every paced run
        # (the bulk download and the re-fetch) so the floor holds between one
        # run and the next as well as inside one — a run that ends and
        # another that starts a millisecond later is two requests a
        # millisecond apart, whoever made them. A list of one, written only
        # by whichever resolver thread is going.
        self._api_pace = [None]
        # `(track id, tier → exact bytes already on this disk, downloaded
        # path)`, read once per opening of the box. Two index reads answer it
        # for all four tiers at once, and the box must not pay them again on
        # every repaint — see _download_usage and ai/reference F6 for the
        # same bargain and the milliseconds that made it necessary.
        self._download_known: Optional[tuple] = None
        # Settings page state. _settings_edit is the digits typed into a number
        # row so far, or None when the arrows are just navigating; it is only
        # ever replaced wholesale, never appended to in place
        self._settings_cursor = 0
        self._settings_edit: Optional[str] = None
        # What is really in the music folder: `downloads.present()`, measured
        # on demand and kept until something moves it — the same bargain
        # MetadataCache makes for audio_count / disk_bytes, and for the same
        # reason: the page repaints twice a second and the folder changes
        # about once a song. On the instance rather than in the module, so
        # nothing outlives a test.
        #
        # One measurement, three readers: the settings page's count and size,
        # the ↓ beside a downloaded track row, and the downloads list. The set
        # of ids is derived from the same list and memoised beside it, because
        # a marker is asked for once per row and rebuilding a set per row is
        # how a memo becomes an O(N²) loop. `_forget_downloads` drops all of
        # it, and is called by everything that can move the folder.
        self._downloads_present: Optional[list] = None
        self._downloads_ids: Optional[set] = None
        # The downloads list screen: where the cursor is, and the row [x] is
        # asking about. Nothing is unlinked until that answer comes back.
        self._downloads_cursor = 0
        self._downloads_delete: Optional[dict] = None
        # Cached copies a landing download could not reclaim because that
        # very track was playing (see _drop_superseded_cache_copy). Track ids
        # as strings. Written from download writer threads and read on the
        # monitor's tick, so every touch is under the lock — a set mutated
        # from two threads is not one of the whole-object-replacement cases.
        self._reclaim_deferred = set()
        self._reclaim_lock = threading.Lock()
        # The _play_gen whose play has already been counted, so one
        # listen is one point however many ticks go past (see
        # _maybe_count_play). -1 is "none yet", which no gen ever is.
        self._play_counted = -1
        # "Re-fetch everything at the current quality": one job, a generation
        # counter that cancels it, and a whole-dict state the paint thread
        # reads. See _start_refetch_job.
        self._refetch_job = None
        self._refetch_gen = 0
        # What [R] is about to do, held between the key and the answer —
        # nothing is touched until the confirmation comes back
        self._refetch_plan = None
        self._refetch_pending = False
        # Transient toast message
        self._toast = ""
        self._toast_until = 0.0
        # Quit confirmation
        self._quit_pending = False
        # Logout confirmation
        self._logout_pending = False
        # Turning song caching off asks what to do with the files already
        # there; clearing the cache is its own action with its own prompt.
        # Neither touches the setting or the disk until the answer comes back
        self._disable_songs_pending = False
        self._clear_cache_pending = False
        # Album art. _artwork is (cover_id, cols, rows, pixels-or-None) and is
        # only ever replaced whole, by the fetch thread; _artwork_request is
        # the key that thread was started for, so a repaint at the same size
        # for the same cover never starts a second one. A stored None means
        # "asked, and there is nothing to show" — not "ask again".
        self._show_artwork = self.config["show_artwork"]
        self._artwork = None
        self._artwork_request = None
        # Mini player mode
        self._mini_player = False
        # `[h]`: the footer is put away until something other than an arrow or
        # the spacebar happens. Transient on purpose — not a setting, not in
        # SETTINGS_SPEC and not in the saved state, because it is a thing you
        # do to this moment rather than a preference about the app.
        self._footer_hidden = False
        # Show more controls
        self._show_more = False
        # The [v] volume overlay, which takes the footer's rows while it is up,
        # and whether the player had the arrows when it opened (so closing it
        # gives them back to the scrub rather than to prev/next)
        self._volume_open = False
        self._volume_from_focus = False
        # How the pane was last laid out. Replaced on every _build_display;
        # ROOMY_FIT until then, so a display builder called on its own (the
        # tests do) elides nothing for want of a window.
        self._fit = ROOMY_FIT
        # Timestamp of the last play/pause key, for repeat suppression
        self._last_toggle_key = 0.0
        # Last rendered frame *and the size it was rendered for*, so an idle
        # repaint can skip an identical write. The size belongs in the key:
        # most of the panel does not depend on the terminal's height, so a
        # window that only got shorter renders byte-identical segments and
        # would otherwise be skipped — the one repaint that must not be.
        self._last_segments = None
        # Set by the SIGWINCH handler; read by the main loop, which repaints
        # the whole screen rather than trusting anything already on it
        self._resized = False
        # Self-pipe: a background thread with fresh data writes a byte, which
        # wakes the input select immediately instead of leaving the new list
        # to wait for the next idle tick. Costs nothing while nothing happens.
        self._wake_r = None
        self._wake_w = None
        # User display name (set after login)
        self._user_display_name = ""
        # True while the restore-state thread is still fetching tracks
        self._restore_pending = False
        # True while _play_track is between killing the old player process
        # and spawning the new one (guards the monitor's end-of-track check)
        self._track_changing = False
        # Bumped per play request so a stale one can't clobber a newer one
        self._play_gen = 0
        # What the player badge says instead of the quality setting, set when
        # the bytes come from somewhere the setting does not describe (a
        # download). None means the setting is the truth.
        self._playing_badge = None
        # Next-track URL prefetch: the result, and the id we already asked for
        self._prefetch = None
        self._prefetch_id = None
        # Navigation
        self._nav_history = []
        # Quality: an explicit --quality wins for this run only; it is never
        # written back to config.json, so the saved default survives
        name = (quality or self.config["quality"]).upper()
        # config["quality"] is already validated, so it is a safe last resort
        self._quality_name = name if name in self.QUALITY_MAP else self.config["quality"]
        self.session.audio_quality = self.QUALITY_MAP[self._quality_name]

    def _get_user_display_name(self) -> str:
        """Get a display name for the logged-in user."""
        u = self.session.user
        if not u:
            return "Unknown"
        # LoggedInUser has username; FetchedUser has first_name/last_name
        first = getattr(u, "first_name", None)
        last = getattr(u, "last_name", None)
        if first and last:
            return f"{first} {last}"
        if first:
            return first
        username = getattr(u, "username", None)
        if username:
            return username
        email = getattr(u, "email", None)
        if email:
            return email
        return f"User {u.id}"

    def _login(self) -> bool:
        """Log in to TIDAL, reusing stored tokens when they still work.

        Two flows can have issued those tokens and they refresh against
        different TIDAL clients, so `is_pkce` has to survive the round trip
        through the credential store. Get that wrong and nothing looks broken
        until the access token expires, at which point the refresh is rejected
        and the user is logged out mid-listen for no visible reason.
        """
        data = load_tokens()
        if data:
            try:
                previous_token = data.get("access_token")
                self.session.load_oauth_session(
                    data["token_type"],
                    data["access_token"],
                    data.get("refresh_token"),
                    data.get("expiry_time"),
                    is_pkce=data.get("is_pkce", False),
                )
                if self.session.check_login():
                    # Loading may have refreshed on the way in; keep the stored
                    # copy current so the next start doesn't repeat the round trip
                    if self.session.access_token != previous_token:
                        self._save_session()
                    self._user_display_name = self._get_user_display_name()
                    return True
            except Exception as e:
                logger.debug("Failed to load saved session: %s", e)

        # Fresh login. The device flow unless PKCE was asked for; a PKCE
        # attempt that doesn't complete is never quietly downgraded, because a
        # silent drop back to AAC is exactly the surprise this all exists to
        # remove — the settings page can upgrade later, at a calmer moment.
        if self._login_flow == "pkce":
            if self._login_pkce():
                return self._finish_login()
            self.console.print(
                "[red]Login cancelled.[/red] [dim]Run ticli again to retry, or "
                "plain `ticli` for the quicker AAC-only sign-in.[/dim]"
            )
            return False

        if self._login_device():
            return self._finish_login()

        self.console.print("[red]Login failed.[/red]")
        return False

    def _finish_login(self) -> bool:
        """Common tail of a fresh login: persist the tokens and name the user."""
        if not self.session.check_login():
            self.console.print("[red]Login failed.[/red]")
            return False
        self._save_session()
        self._user_display_name = self._get_user_display_name()
        return True

    def _save_session(self) -> None:
        """Write the current tokens to the keychain (or the chmod-600 file)."""
        expiry = self.session.expiry_time
        try:
            save_tokens({
                "token_type": self.session.token_type,
                "access_token": self.session.access_token,
                "refresh_token": self.session.refresh_token,
                "expiry_time": expiry.isoformat() if hasattr(expiry, "isoformat") else expiry,
                # Which flow issued these decides which client may refresh them
                "is_pkce": bool(self.session.is_pkce),
            })
        except Exception as e:
            logger.warning("Failed to save session: %s", e)

    def _login_device(self) -> bool:
        """Device-authorization login: a code typed on another device, nothing
        to paste back. Smooth, but its TIDAL client is only entitled to AAC —
        a LOSSLESS request from it comes back granted HIGH, without complaint."""
        self.console.print("[cyan]Starting TIDAL login...[/cyan]")
        try:
            login, future = self.session.login_oauth()
        except Exception as e:
            logger.debug("Device login failed to start: %s", type(e).__name__)
            return False
        self.console.print("\n[bold yellow]Open this URL to login:[/bold yellow]")
        self.console.print(f"[bold white]https://{login.verification_uri_complete}[/bold white]\n")
        self.console.print(f"[dim]Or go to [bold]{login.verification_uri}[/bold] and enter code: [bold]{login.user_code}[/bold][/dim]\n")
        self.console.print("[dim]Waiting for authorization...[/dim]")
        try:
            future.result()
        except Exception as e:
            logger.debug("Device login did not complete: %s", type(e).__name__)
            return False
        return True

    def _login_pkce(self) -> bool:
        """PKCE authorization-code login — the flow that reaches FLAC.

        Driven a step at a time rather than through tidalapi's `login_pkce()`,
        which prints with `print()` and reads with a bare `input()`: we want
        Rich's output, a retry when the paste comes back short, and no
        exception escaping into the caller with a live one-time code inside
        its message.

        There is no way around the paste. The redirect URI is fixed to
        `https://tidal.com/android/login/auth` in tidalapi's config and is sent
        again, unchanged, in the token exchange — where TIDAL matches it — so a
        localhost listener cannot be substituted. It would be no use over SSH
        anyway, which is how this player is often run.
        """
        try:
            url = self.session.pkce_login_url()
        except Exception as e:
            logger.debug("Failed to build the PKCE login URL: %s", type(e).__name__)
            return False

        self.console.print("[cyan]Starting TIDAL sign-in for higher quality...[/cyan]")
        self.console.print("\n[bold yellow]1.[/bold yellow] Open this URL and sign in:\n")
        # markup off: the URL is data, not Rich markup. Soft wrap keeps it one
        # logical line, so selecting it copies the whole thing.
        self.console.print(url, markup=False, highlight=False, soft_wrap=True)
        self.console.print(
            "\n[bold yellow]2.[/bold yellow] TIDAL then sends you to a page that fails to load. "
            "[dim]That is expected — the address bar is carrying your login code.[/dim]"
            "\n[bold yellow]3.[/bold yellow] Copy that whole address and paste it below.\n"
        )
        self._open_browser(url)

        for remaining in range(PKCE_PASTE_TRIES - 1, -1, -1):
            try:
                pasted = input("Paste the address (or just the code): ").strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                return False
            if not pasted:
                continue
            # A bare code is accepted too: relaying the address by hand from a
            # phone into an SSH session, the code is the only part worth typing.
            redirect = pasted if "https://" in pasted else (
                f"{self.session.config.pkce_uri_redirect}?code={quote(pasted)}"
            )
            try:
                token = self.session.pkce_get_auth_token(redirect)
                self.session.process_auth_token(token, is_pkce_token=True)
                return True
            except Exception as e:
                # Type only — the exception text can quote the pasted address,
                # and that address contains a live authorization code
                logger.debug("PKCE token exchange failed: %s", type(e).__name__)
                if remaining:
                    self.console.print(
                        f"[red]That didn't work.[/red] [dim]Copy the full address, "
                        f"including everything after the '?'. {remaining} "
                        f"{'try' if remaining == 1 else 'tries'} left.[/dim]"
                    )
        return False

    def _upgrade_to_pkce(self) -> None:
        """Sign in again over PKCE from inside the running player, keeping the
        session you already have if it doesn't go through.

        The paste needs a normal cooked terminal and a console to print to, so
        the TUI stands down for the duration and comes back afterwards — the
        one place in the app where the Live display is deliberately paused.
        """
        if self.session.is_pkce:
            return
        with self._suspended_tui():
            upgraded = self._login_pkce() and self._finish_login()
        if upgraded:
            # A new entitlement means the old evidence is stale: whatever this
            # session is granted has to be observed again from scratch, which
            # is what un-gates the higher tiers without a restart.
            self._quality_ceiling = None
            # The queue and the playing track are untouched — the current
            # signed URL keeps working and the next track picks the new session
            # up on its own. Songs already cached are the one thing that does
            # not improve, so say so rather than let him wonder.
            self._set_toast(
                "Signed in for higher quality — songs already cached still play "
                "as before; [x] clears them", seconds=6)
        else:
            self._set_toast("Sign-in cancelled — still signed in as before")

    def _suspended_tui(self):
        """Stand the TUI down for a block of plain console I/O, then bring it
        back. Terminal handling mirrors run()'s: cbreak while the player owns
        the keyboard, the user's own settings while they are typing."""
        import contextlib

        @contextlib.contextmanager
        def _suspend():
            live = self._live
            if live is not None:
                live.stop()
            self._restore_tty()
            self.console.print()
            try:
                yield
            finally:
                self._raw_tty()
                # No clear() either: live.start() goes back to the alternate
                # screen, so what was typed here stays in the scrollback
                if live is not None:
                    live.start(refresh=False)
                # The screen was someone else's for a while, so the cached
                # frame no longer describes it: the next repaint must write
                self._last_segments = None

        return _suspend()

    def _restore_tty(self) -> None:
        """Hand the terminal back to its owner's line discipline."""
        if self._tty_settings is None:
            return
        try:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._tty_settings)
        except Exception as e:
            logger.debug("Failed to restore terminal settings: %s", e)

    def _raw_tty(self) -> None:
        """Take the keyboard back for single-key input."""
        if self._tty_settings is None:
            return
        try:
            import tty
            tty.setcbreak(sys.stdin.fileno())
        except Exception as e:
            logger.debug("Failed to set cbreak mode: %s", e)

    def _open_browser(self, url: str) -> None:
        """Open the login URL locally, if there plausibly is a browser to open
        it in. Deliberately skipped on a bare SSH session, where $BROWSER may
        be a text browser that would take the terminal over. Never fatal."""
        if not (IS_MACOS or sys.platform == "win32"
                or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    def _logout(self):
        """Log out and clear saved tokens."""
        from ticli.utils.credential_store import delete_tokens
        delete_tokens()
        self.audio.stop()
        self._playing = False
        self._current_track = None
        self._queue = []
        self._queue_index = -1
        self.running = False
        self.console.print("[yellow]Logged out. Tokens cleared.[/yellow]")

    def _load_favorites(self):
        """Load liked track IDs in background."""
        def _run():
            try:
                favs = self.session.user.favorites.tracks(limit=999)
                self._liked_ids = {t.id for t in favs}
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _write_state_file(self, state: dict):
        """Atomically write the state file (temp + rename, never torn).

        Every caller is a load-modify-save cycle holding `_state_lock` — the
        atomic rename keeps a *reader* from seeing a torn file, but only the
        lock keeps a concurrent *writer* from erasing this one's changes.
        """
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_FILE)

    def _read_state_dict(self) -> dict:
        """Read the state file as a dict. Missing or corrupt → {}, never raises.

        Same contract as load_config and the cache loaders: a file that is
        absent, unreadable, undecodable, or valid JSON that is not a dict all
        read as empty. This is the single place player state is read — every
        reader goes through it (writers through _write_state_file), and a test
        can monkeypatch it. Deliberately lock-free: pure readers like
        `_restore_state` see one whole generation of the file (writes go
        through an atomic rename), so only the writers' read-modify-write
        cycles need `_state_lock`, and they take it around their whole cycle
        rather than in here.
        """
        try:
            data = json.loads(STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.debug("Unusable player state, starting empty: %s", e)
            return {}
        if not isinstance(data, dict):
            logger.debug("Player state is not a dict, starting empty")
            return {}
        return data

    def _merge_position_into_saved_state(self):
        """Refresh only the position in the saved file, if the track matches."""
        with self._state_lock:
            self._merge_position_into_saved_state_locked()

    def _merge_position_into_saved_state_locked(self):
        """The read-modify-write itself; the caller holds `_state_lock`.

        Split from the public wrapper because `_save_state` needs the merge
        *inside* its own locked cycle and a plain Lock is not reentrant — the
        lock is taken exactly once per cycle, in the wrapper or in
        `_save_state`, never here.
        """
        if self._current_track is None:
            return
        try:
            data = self._read_state_dict()
            ids = data.get("track_ids", [])
            idx = data.get("queue_index", 0)
            if ids and 0 <= idx < len(ids) and ids[idx] == self._current_track.id:
                data["position"] = self._get_position()
                self._write_state_file(data)
        except Exception as e:
            logger.debug("Failed to merge position into saved state: %s", e)

    def _remember_last_playlist(self, playlist):
        """Pin the playlist a track was just added to, and persist it.

        This lives in the *state* file, not the config: the config is a
        schema'd set of things the user chose, each with a row on the settings
        page and a migration when it changes. Which playlist you last used is
        none of those — it is incidental, machine-written, and it belongs next
        to the queue and the search history, which are already here.

        Written through immediately (read-modify-write of the same atomic
        file) rather than left to the shutdown save, because a session that
        ends by anything other than [q] would otherwise lose it. Always called
        from the background thread that did the adding, never the UI thread.
        """
        pid = str(getattr(playlist, "id", "") or "")
        if not pid:
            return
        self._last_playlist_id = pid
        # The whole read-modify-write under _state_lock: an autosave landing
        # between this read and this write would otherwise be clobbered by
        # the stale copy read here, losing the fresh queue and position.
        with self._state_lock:
            data = self._read_state_dict()
            data["last_playlist_id"] = pid
            try:
                self._write_state_file(data)
            except OSError as e:
                logger.debug("Failed to persist last-used playlist: %s", e)

    def _save_state(self):
        """Save queue and playback state to disk for next session.

        The whole build-and-write runs under `_state_lock`, so a playlist pin
        or position merge on another thread cannot land between reading the
        in-memory fields and replacing the file — and cannot itself be erased
        by this save straddling *its* cycle.
        """
        with self._state_lock:
            # If the restore never finished attaching the queue, our in-memory
            # state is incomplete — a full save would shrink the good file.
            # Still refresh the position in case the user played the restored
            # track — through the unlocked half, since the lock is already
            # held here and is not reentrant.
            if self._restore_pending and not self._queue:
                self._merge_position_into_saved_state_locked()
                return
            self._save_state_locked()

    def _save_state_locked(self):
        """The full build-and-write; the caller holds `_state_lock`."""
        try:
            queue = self._queue
            queue_index = self._queue_index
            if not queue and self._current_track is not None:
                queue = [self._current_track]
                queue_index = 0
            state = {
                # Kept even though "tracks" carries strictly more: the ids
                # are what every pre-record build reads, so a downgrade still
                # resumes. "tracks" is the same record shape the metadata
                # cache stores — flattened through track_record, which reads
                # tidalapi Tracks and restored CachedTrack shims alike, so a
                # save straight after a restore loses nothing.
                "track_ids": [t.id for t in queue],
                "tracks": [track_record(t) for t in queue],
                "queue_index": queue_index,
                "position": self._get_position(),
                "search_history": self._search_history[:200],
            }
            # A full save rewrites the whole file, so the pin has to be carried
            # through it or the last add of the session would be forgotten
            if self._last_playlist_id:
                state["last_playlist_id"] = self._last_playlist_id
            self._write_state_file(state)
        except Exception as e:
            logger.debug("Failed to save player state: %s", e)

    def _shutdown(self):
        """Quit cleanly, silence first.

        _save_state asks mpv where it is over IPC and then writes a file, so
        saving before stopping left music playing for a beat after the UI was
        already gone. Read the position while the player is still alive to
        answer it, stop the audio, and write the file into the quiet — the
        saved position is if anything better for being taken from mpv itself.
        """
        if self.audio:
            position = self.audio.get_time_pos()
            if position is not None:
                self._play_offset = position
                self._play_start_time = time.time()
            self.audio.stop()
        self._save_state()

    def _restore_state(self):
        """Restore queue and search history from previous session.

        Two formats, one file. A file this build wrote carries "tracks" —
        the record shape the metadata cache stores — and restores with **zero
        network requests**: the queue is built from `CachedTrack` shims, and
        `_play_track`'s existing `_resolve_track` swap fetches the real track
        lazily, one request, when a track is actually played. Restoring a
        whole saved playlist used to spend one `session.track()` per id at
        every launch, serially and unpaced — the burst shape of ai/INCIDENTS
        #1, in production. The record path is synchronous on purpose: run()
        calls this before the UI loop starts, the attach cannot fail, so full
        saves stay enabled and no `_restore_pending` latch is needed.

        A file with only "track_ids" — every pre-upgrade file — still fetches
        on a daemon thread, but frugally: paced to REFETCH_MIN_INTERVAL
        between request starts, stood down quietly if the user moves on, and
        ended outright — no attach, latch left set, a toast saying so — on
        any sign TIDAL is rate-limiting. The legacy path retires itself: the
        first full save of the session writes records next to the ids.
        """
        data = self._read_state_dict()
        if not data:
            return
        # Same "corrupt → defaults" contract as the fields below: a history
        # that is not a list reads as empty, and non-str elements of one are
        # dropped — either shape used to crash here (the slice) or on the
        # search screen. The write side stays a plain [:200] of what memory
        # holds, which this guarantees is a list of strings.
        history = data.get("search_history", [])
        if not isinstance(history, list):
            history = []
        self._search_history = [h for h in history if isinstance(h, str)][:200]
        self._last_playlist_id = data.get("last_playlist_id") or None
        track_ids = data.get("track_ids", [])
        # Sanitized here because the record path below runs synchronously —
        # unlike the legacy thread there is no catch-all around it, and the
        # contract since the non-dict incident is "corrupt → defaults, never
        # raises". Wrong types read as their defaults, same as absent. The
        # *record fields* honor the same contract one layer down, in the
        # CachedTrack/CachedPlaylist constructors themselves, because the
        # metadata cache builds the same shims from the same record shape —
        # only the state file's own scalars are this reader's to check.
        queue_index = data.get("queue_index", 0)
        if not isinstance(queue_index, int):
            queue_index = 0
        position = data.get("position", 0) or 0
        if not isinstance(position, (int, float)):
            position = 0

        records = data.get("tracks")
        if (isinstance(records, list) and records
                and all(isinstance(r, dict) and r.get("id") is not None
                        for r in records)):
            tracks = [CachedTrack(r) for r in records]
            idx = min(max(queue_index, 0), len(tracks) - 1)
            current = tracks[idx]
            # Same clamp as ever: a position at the very end restarts from
            # 0:00 rather than seeking past EOF, and never autoplay
            duration = current.duration if isinstance(
                current.duration, (int, float)) else 0
            if 1 <= position < duration - 2:
                self._play_offset = position
            self._queue = tracks
            self._queue_index = idx
            self._current_track = current
            return

        if not track_ids:
            return

        self._restore_pending = True

        def _run():
            blocked = ""
            abandoned = False
            last_start = None

            def _fetch(tid):
                """One paced fetch, or None. The floor sits between request
                *starts*, so a run of instant failures cannot burst either.
                Flags rather than raises: `abandoned` when the user moved on
                while we waited, `blocked` on any sign TIDAL is refusing —
                and a plain failure just drops the track, as it always has."""
                nonlocal blocked, abandoned, last_start
                if last_start is not None:
                    remaining = REFETCH_MIN_INTERVAL - (time.monotonic() - last_start)
                    if remaining > 0:
                        _restore_sleep(remaining)
                if not (self.running and self._restore_pending):
                    abandoned = True
                    return None
                last_start = time.monotonic()
                try:
                    return self.session.track(tid)
                except Exception as e:
                    if _looks_rate_limited(str(e)):
                        blocked = str(e)[:PLAYER_ERROR_CHARS]
                    else:
                        logger.debug("Could not restore track %s: %s", tid, e)
                    return None

            try:
                # Fetch the current track first so it appears immediately,
                # paused at its saved position, before the rest of the queue loads
                idx = min(max(queue_index, 0), len(track_ids) - 1)
                current = _fetch(track_ids[idx])
                if current is not None and not self._playing and self._current_track is None:
                    duration = getattr(current, "duration", 0) or 0
                    if 1 <= position < duration - 2:
                        self._play_offset = position
                    self._current_track = current

                tracks = []
                for i, tid in enumerate(track_ids):
                    if i == idx:
                        if current is not None:
                            tracks.append(current)
                        continue
                    if blocked or abandoned:
                        break
                    t = _fetch(tid)
                    if t is not None:
                        tracks.append(t)
                if blocked:
                    # Stop entirely and report — never retry (WORKING-RULES:
                    # retries are what turned a rate limit into an edge
                    # block). No attach either: the latch stays set, full
                    # saves stay suppressed, and the file keeps the whole
                    # queue for a launch when TIDAL is willing again.
                    self._set_toast(
                        "TIDAL is rate-limiting — restore stopped. "
                        "Nothing will be retried.",
                        seconds=PLAYER_ERROR_SECONDS)
                    self._wake()
                    return
                if abandoned:
                    # The user moved on (or the app is quitting): whatever
                    # was fetched must not attach — a partial queue landing
                    # now could be saved over the whole one on disk.
                    return
                # Don't clobber anything the user started while we were loading
                # (playing the restored track itself is fine — attach its queue)
                # `_restore_pending` is also the claim on the queue: anything
                # that deliberately set one while we were fetching (radio keeps
                # the same track playing, so the check below can't see it) drops
                # the flag, and this attach stands down rather than clobbering it
                if tracks and self._restore_pending and self._current_track in (None, current):
                    self._queue = tracks
                    try:
                        self._queue_index = tracks.index(current) if current is not None else min(idx, len(tracks) - 1)
                    except ValueError:
                        self._queue_index = min(idx, len(tracks) - 1)
                    if self._current_track is None:
                        self._current_track = tracks[self._queue_index]
                    # Latch: only a SUCCESSFUL attach re-enables full saves.
                    # If restore failed, empty-queue saves stay suppressed so
                    # they can't shrink the good file to a single track.
                    self._restore_pending = False
            except Exception as e:
                logger.debug("Failed to restore player state: %s", e)

        threading.Thread(target=_run, daemon=True).start()

    def _play_track(self, track: tidalapi.Track, seek: float = 0):
        """Play a track via the audio player, optionally starting at an offset.

        get_url() is a TIDAL round trip, so the whole start-up runs on a
        daemon thread — the UI used to freeze for its duration on every skip.
        The new track is shown straight away with its clock held at the seek
        point; the clock starts when audio actually does. A generation counter
        keeps a slow request from a track the user already skipped past from
        landing on top of the newer one.
        """
        self._track_changing = True
        self._play_gen = gen = self._play_gen + 1
        # This start decides where the track begins; any position still
        # waiting to be scrubbed to belonged to the track we are leaving
        self._seek_target = None
        self._prefetch_id = None  # re-arm the prefetch for this track's successor
        self._current_track = track
        self._playing = True
        self._play_start_time = None
        self._play_offset = seek

        def _run():
            try:
                # A row from the cache carries no stream URL — swap it for the
                # real track before doing anything with it. Only reachable in
                # the second or so before revalidation replaces the whole list.
                real = self._resolve_track(track)
                if real is None:
                    if self._play_gen == gen:
                        self._playing = False
                    return
                if real is not track:
                    # Swap the queue's copy too, so a queue built from cached
                    # rows only ever pays for one resolve per track
                    queue = self._queue
                    if track in queue:
                        self._queue = [real if t is track else t for t in queue]
                    if self._play_gen == gen:
                        self._current_track = real
                # A copy already on this disk costs no request at all, so it
                # is looked for *before* the stream URL is asked for rather
                # than after. Both tiers, not just downloads: the cache used
                # to be discovered inside play_url, one whole `get_stream()`
                # too late, so replaying a 20-track cached playlist spent 20
                # playbackinfo requests on URLs it then threw away — the
                # exact endpoint whose burst rate got the owner blocked.
                local, badge = self._local_source(real)
                if local:
                    url, granted = "", None
                else:
                    url, granted = (self._take_prefetched(real.id)
                                    or self._stream_description(real))
                # Superseded by a newer track, or paused while we were
                # fetching — either way this start is no longer wanted
                if self._play_gen != gen or not self._playing:
                    return
                artist = ", ".join(a.name for a in real.artists) if real.artists else ""
                title = f"{real.name} — {artist}" if artist else real.name
                # What the badge on the player says from here on. Assigned
                # whole, next to the play it describes and behind the same
                # generation check, so the screen can never claim a tier for
                # a track that is not the one playing.
                self._playing_badge = badge
                self.audio.play_url(url, seek=seek, title=title, cache_key=real.id,
                                    local=local, quality=granted)
                if self._play_gen != gen:
                    return
                self._playing = True
                self._play_start_time = time.time()
                self._play_offset = seek
            except Exception:
                if self._play_gen == gen:
                    self._playing = False
            finally:
                if self._play_gen == gen:
                    self._track_changing = False

        threading.Thread(target=_run, daemon=True).start()

    def _local_source(self, track) -> tuple:
        """`(path, badge)` — the copy of this track already on disk that will
        be played, and what the player should say it is. `(None, None)` when
        nothing here will do and the track has to be streamed.

        Two tiers, and now **two different rules**, because the two tiers are
        owned by different people.

        The **download** is the user's own file, and it always wins. It is
        played whatever the quality setting says, and no tier is compared:
        having set the app to MAX is not a request to re-fetch a song that
        is already sitting in `~/Music/Ticli`, and doing it anyway spends the
        user's data on audio he deliberately put on his own disk. The one
        thing that changes a download's quality is the one thing that asks:
        `[R]` on the settings page. That is the consent, and this path must
        not quietly borrow it.

        The **cache** is machine-owned and disposable, so the old rule stands
        there and both wrong answers still matter: a cached copy stored
        *below* the tier now selected is skipped, so the track is fetched at
        the quality that was asked for rather than silently served the old
        one; one stored *above* it is kept, because being temporarily set to
        LOW must never destroy a hi-res copy. A cached copy whose tier was
        never recorded counts as good enough — unknown is not evidence of a
        downgrade, and re-fetching a whole library on the strength of a
        missing field is not something to do without being asked.

        Either tier is verified at the moment of use rather than trusted, so
        a file deleted by hand falls straight through to the network, which
        is the whole of "manual deletion is handled durably".

        `badge` is the honesty half. A download can now play *below* the tier
        the settings page shows, and a player that went on displaying the
        setting would be claiming a quality it is not producing — the exact
        lie this codebase has been burned by before. So a download says what
        it actually is (`HIGH · downloaded`), on the status line it was
        already drawing, and the setting's label is only shown when the
        setting is really what is being played. Nothing is toasted: this is
        true for every track of a downloaded album, and a notice per track
        would be nagging.

        The quality gate learns nothing on this path, since no stream is
        described — unchanged by the download rule, which only ever turns a
        stream *into* a local play for a track the user already fetched at a
        tier the gate has seen. It still learns on every miss, which is every
        new track; the only user who would notice is one whose entire library
        is on this disk, and for them `[R]` re-fetches at the new tier.
        """
        track_id = getattr(track, "id", None)
        owned = downloads.path_for(track_id)
        if owned is not None:
            entry = downloads.load_index().get(str(track_id)) or {}
            return str(owned), self._download_badge(entry.get("granted"))
        cached = cached_audio_path(track_id) if self._cache.keeps_audio else None
        if cached:
            record = self._cache.audio_record(track_id) or {}
            if self._tier_is_enough(record.get("quality")):
                return cached, None
        return None, None

    def _local_copy(self, track) -> Optional[str]:
        """Just the path from `_local_source` — for the callers that only
        want to know whether the network is needed at all."""
        return self._local_source(track)[0]

    def _download_badge(self, granted) -> Optional[str]:
        """What the player says while a downloaded file is playing.

        The tier the file really holds, plus where it came from. A tier that
        was never recorded says only `downloaded`: "we do not know" is an
        honest answer and the setting's label would not be one.
        """
        label = self._tier_label(granted)
        return f"{label} · downloaded" if label else "downloaded"

    def _tier_label(self, granted) -> Optional[str]:
        """The badge for a tidalapi tier (`LOSSLESS`), or None if it is not
        one of ours. `QUALITY_MAP` is the only translation table there is, so
        it is read backwards rather than duplicated."""
        for name, quality in self.QUALITY_MAP.items():
            if granted == quality:
                return self.QUALITY_LABELS.get(name)
        return None

    def _tier_is_enough(self, stored) -> bool:
        """Whether a **cached** copy stored at `stored` still satisfies the
        current quality setting. Unknown says yes — see `_local_source`.
        Downloads do not ask: they are the user's files and always win."""
        wanted = self.QUALITY_MAP.get(self._quality_name)
        if stored not in QUALITY_RANK or wanted not in QUALITY_RANK:
            return True
        if self._quality_unavailable(self._quality_name):
            # TIDAL has shown it will not serve this tier, so re-fetching for
            # it would spend a request to be handed the same thing again
            return True
        return QUALITY_RANK[stored] >= QUALITY_RANK[wanted]

    def _take_prefetched(self, track_id) -> Optional[tuple]:
        """`(url, granted tier)` fetched moments ago for this track, if there
        is one. Consumed on use — a signed URL is worth having exactly once."""
        prefetched = self._prefetch
        self._prefetch = None
        if not prefetched:
            return None
        pid, url, granted, fetched_at = prefetched
        if pid != track_id or time.time() - fetched_at > PREFETCH_MAX_AGE:
            return None
        return url, granted

    def _maybe_prefetch_next(self):
        """Fetch the next track's stream URL just before we need it.

        Called from the monitor's existing tick, so it costs no new wakeups,
        and only inside the last PREFETCH_LEAD seconds of a track — which
        both keeps the signed URL fresh and means a track you skip past
        early never causes a request at all.
        """
        if self._prefetch_id is not None or not self._playing:
            return
        if not self._queue or self._queue_index >= len(self._queue) - 1:
            return
        duration = getattr(self._current_track, "duration", 0) or 0
        if duration <= 0 or duration - self._get_position() > PREFETCH_LEAD:
            return
        nxt = self._queue[self._queue_index + 1]
        track_id = getattr(nxt, "id", None)
        if track_id is None:
            return
        self._prefetch_id = track_id  # latch, so the tick can't refire it
        if self._local_copy(nxt) is not None:
            # It is already on this disk at a tier that will do. The URL would
            # be fetched and then thrown away by _play_track, which is the
            # same wasted request the cache check there removes.
            return

        def _run():
            try:
                real = self._resolve_track(nxt)
                if real is not None:
                    url, granted = self._stream_description(real)
                    self._prefetch = (real.id, url, granted, time.time())
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _stream_url(self, track) -> str:
        """Something mpv/ffplay can play for this track. Network — callers must
        already be off the UI thread.

        One request either way, so this asks for the whole stream description
        rather than just a URL: the reply says which tier TIDAL *granted*, and
        that is the only free evidence of what this session is entitled to.
        `get_url()` isn't an option on a PKCE session in any case — tidalapi
        refuses it outright (media.py get_url), because the hi-res tiers it
        unlocks aren't served that way. A BTS manifest still names one whole
        file; an MPEG-DASH one names segments, which become a local playlist.
        """
        return self._stream_description(track)[0]

    def _stream_description(self, track) -> tuple:
        """`(url, the tier TIDAL actually granted)`. One request, both facts.

        The tier is not decoration: it is what the cache tracker records
        against the file, which is what makes "is this copy below the quality
        I have now set?" and "is this copy already what a download at this
        tier would fetch?" answerable without asking TIDAL again.
        """
        stream = track.get_stream()
        granted = getattr(stream, "audio_quality", None)
        self._note_granted_quality(granted)
        manifest = stream.get_stream_manifest()
        if manifest.is_bts:
            return manifest.get_urls()[0], granted
        return (_write_hls_playlist(track.id, _hls_playlist(manifest.dash_info)),
                granted)

    def _note_granted_quality(self, granted: Optional[str]) -> None:
        """Record a tier TIDAL served that was below the one we asked for.

        Called from a stream fetch, so it costs nothing and needs no probe. It
        only ever remembers a *downgrade*: getting what you asked for says
        nothing about the tiers above it, and an unrecognised answer says
        nothing at all. Anything it doesn't understand leaves the ceiling
        alone, which leaves the settings page ungated — a tier wrongly marked
        unavailable is worse than one that is quietly disappointing.
        """
        wanted = self.QUALITY_MAP.get(self._quality_name)
        if granted not in QUALITY_RANK or wanted not in QUALITY_RANK:
            return
        if QUALITY_RANK[granted] < QUALITY_RANK[wanted]:
            self._quality_ceiling = granted
        elif self._quality_ceiling is not None and \
                QUALITY_RANK[granted] > QUALITY_RANK[self._quality_ceiling]:
            # Something changed for the better — believe the newer evidence
            self._quality_ceiling = None

    def _quality_unavailable(self, choice: str) -> bool:
        """Is this settings tier one TIDAL has shown it won't actually serve?

        False whenever we can't tell, which is most of the time: before the
        first track plays there is no evidence at all.
        """
        ceiling = self._quality_ceiling
        wanted = self.QUALITY_MAP.get(choice)
        if ceiling is None or wanted not in QUALITY_RANK:
            return False
        return QUALITY_RANK[wanted] > QUALITY_RANK[ceiling]

    def _resolve_track(self, track):
        """Turn a cached row into a real tidalapi Track. Anything that already
        is one is handed straight back, so this costs nothing on the normal
        path. Network — callers must already be off the UI thread."""
        if track is None or not getattr(track, "cached", False):
            return track
        try:
            return self.session.track(track.id)
        except Exception:
            return None

    def _play_queue_index(self, index: int):
        """Play track at queue index."""
        if 0 <= index < len(self._queue):
            self._queue_index = index
            self._play_track(self._queue[index])

    def _next_track(self):
        """Skip to next track in queue."""
        if self._queue and self._queue_index < len(self._queue) - 1:
            self._play_queue_index(self._queue_index + 1)

    def _prev_track(self):
        """Go back: to the start of this track if we're already past
        PREV_RESTART_SECONDS into it, otherwise to the previous track."""
        if self._current_track is not None and self._get_position() > PREV_RESTART_SECONDS:
            self._restart_current_track()
            return
        if self._queue and self._queue_index > 0:
            self._play_queue_index(self._queue_index - 1)

    def _restart_current_track(self):
        """Send the current track back to 0:00 — a gapless mpv seek when the
        process is alive and playing, a fresh spawn from 0 otherwise."""
        self._seek_target = None  # 0:00 supersedes anything still being scrubbed to
        if self._playing and self.audio and self.audio.seek_to_start():
            self._play_offset = 0
            self._play_start_time = time.time()
            return
        self._play_track(self._current_track, seek=0)

    def _seek_by(self, delta: float):
        """Scrub the current track by delta seconds.

        The clock moves here, on the keypress, so the bar and the timer answer
        the arrow before anything has been asked of the backend — a seek that
        only appeared on the next position poll felt broken. Everything else
        reads the position through _get_position(), so moving _play_offset is
        the whole of the bookkeeping: the progress bar, the saved resume
        position, the prefetch and the auto-advance all follow from it.

        Both ends clamp. Past the start is 0:00, not the previous track, and
        past the end stops just short of it rather than advancing: ←/→ mean
        "move inside this song" here, and skipping is what they mean when the
        player does not have focus.
        """
        if self._current_track is None:
            return
        duration = getattr(self._current_track, "duration", 0) or 0
        target = max(0.0, self._get_position() + delta)
        if duration > 0:
            target = min(target, max(0.0, duration - SEEK_END_MARGIN))
        self._play_offset = target
        self._play_start_time = time.time() if self._playing else None
        self._seek_target = target
        self._flush_seek()

    def _seek_pending(self) -> bool:
        """Whether a scrubbed position is still on its way to the backend."""
        return self._seek_applying or (
            self._seek_target is not None and self._seek_target != self._seek_applied)

    def _flush_seek(self):
        """Hand a scrubbed position to the backend, at most one every
        SEEK_COALESCE_SECONDS.

        Called by the press that made it and, when that press landed too soon
        after the last one, by the monitor's existing tick — no new thread of
        control, and a held-down arrow costs a couple of seeks a second
        instead of an mpv round trip (or, worse, an ffplay respawn) per key
        repeat. Nothing is dropped: a position the backend has not been told
        about stays visible as _seek_target != _seek_applied until it has.
        """
        target = self._seek_target
        if target is None or target == self._seek_applied or self._seek_applying:
            return
        if time.monotonic() - self._last_seek_apply < SEEK_COALESCE_SECONDS:
            return  # too soon — the next press or the monitor tick delivers it
        self._seek_applying = True
        self._last_seek_apply = time.monotonic()
        gen = self._play_gen
        track = self._current_track

        def _run():
            # Off the UI thread: an mpv that has stopped answering makes this
            # wait out the IPC timeout, and killing ffplay is not instant
            try:
                if self._play_gen != gen:
                    return  # the track moved on; this seek belongs to the old one
                if not (self.audio and self.audio.seek_to(target)):
                    # No live process to move — the only way to a position is
                    # to start the track there
                    self._play_track(track, seek=target)
                self._seek_applied = target
            finally:
                self._seek_applying = False

        threading.Thread(target=_run, daemon=True).start()

    def _toggle_play_key(self):
        """Play/pause from a keypress, on any screen.

        Holding the key makes the terminal repeat it, so events closer
        together than the repeat window count as one press. This replaces a
        held-flag that only cleared on an idle poll — which meant a genuine
        second press soon after the first was swallowed instead.
        """
        now = time.monotonic()
        repeat = now - self._last_toggle_key < KEY_REPEAT_WINDOW
        self._last_toggle_key = now
        if repeat:
            return
        self._toggle_play()

    def _toggle_play(self):
        """Toggle play/pause — pauses in place, resumes from same position."""
        if self._playing:
            self.audio.pause()
            self._playing = False
            if self._play_start_time:
                self._play_offset += time.time() - self._play_start_time
                self._play_start_time = None
        else:
            if self._current_track and self.audio and self.audio.is_paused:
                # Resume from paused position
                if self.audio.resume():
                    self._playing = True
                    self._play_start_time = time.time()
                else:
                    # Player died while paused — restart from where we were
                    self._start_current_from_position()
            elif self._current_track:
                # No paused process — start fresh from the last known position
                self._start_current_from_position()

    def _start_current_from_position(self):
        """(Re)start the current track from the last known position."""
        seek = self._get_position()
        duration = getattr(self._current_track, "duration", 0) or 0
        # Clamp unconditionally: an unknown duration must not let a stale
        # offset seek past EOF (mpv exits instantly on that)
        if seek < 1 or seek >= duration - 2:
            seek = 0
        self._play_track(self._current_track, seek=seek)

    def _toggle_like(self):
        """Toggle like on current track."""
        if not self._current_track:
            return
        tid = self._current_track.id
        def _run():
            try:
                if tid in self._liked_ids:
                    self.session.user.favorites.remove_track(tid)
                    self._liked_ids.discard(tid)
                else:
                    self.session.user.favorites.add_track(tid)
                    self._liked_ids.add(tid)
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _start_track_radio(self):
        """Seed an endless mix from the song that is playing — without
        interrupting it.

        Radio is a queue change and nothing else: the track you pressed [r] on
        keeps playing, from where it was, out of the process and the stream URL
        it already had. It used to call `_play_track` on the first radio track,
        which restarted the song you were enjoying from 0:00 and paid a second
        `get_stream()` for the privilege. Nothing about the mix requires that —
        `get_track_radio()` is "give me N tracks", so the whole job is to make
        those tracks the *upcoming* queue with the current one at its head.

        The fetch is a round trip, so it runs on a daemon thread, and the
        brakes are the ones already in use elsewhere: `_radio_fetching`
        single-flights it, `RADIO_FETCH_MIN_INTERVAL` floors the gap between
        one fetch and the next (so a held-down [r] cannot fan out even between
        two fast replies), and `_play_gen` — bumped by every `_play_track` —
        is the generation counter that throws away a mix for a song that has
        since been skipped past. Failure keeps the music playing and says so.
        """
        track = self._current_track
        if track is None:
            return
        now = time.monotonic()
        if self._radio_fetching or now - self._radio_last_fetch < RADIO_FETCH_MIN_INTERVAL:
            return
        self._radio_fetching = True
        self._radio_last_fetch = now
        gen = self._play_gen
        # Playback does not change, so without this the key looks dead
        self._set_toast("Starting radio…")

        def _run():
            tracks = None
            try:
                # The current track can still be a cached shim — restored
                # from a saved record, or a cached row in its first second —
                # and a shim has no get_track_radio. Resolve it first, the
                # same swap _play_track does; one extra request, only ever
                # on a track that has not been played yet.
                real = self._resolve_track(track)
                if real is not None:
                    tracks = real.get_track_radio(limit=RADIO_LIMIT)
            except Exception as e:
                logger.debug("Track radio failed: %s", e)
            finally:
                self._radio_fetching = False
            if self._play_gen != gen:
                # A different song is playing now — this mix is for a song the
                # user has already left, and must not take its queue
                return
            if not tracks or not self._apply_radio_queue(tracks):
                self._set_toast("Radio unavailable — queue unchanged")
            self._wake()

        threading.Thread(target=_run, daemon=True).start()

    def _apply_radio_queue(self, tracks: list):
        """Put the playing track at the head of a fresh radio queue.

        Whole-object assignment, never a mutation of the list the paint thread
        and the monitor are reading. The current track becomes position 0, so
        auto-advance, "up next" and the saved state all agree the moment the
        swap lands, and the seed is dropped from the mix so it can't play
        twice. The prefetch is re-armed rather than kept: a URL fetched for
        whatever used to be next is no longer next (`_take_prefetched` would
        discard it on the id check anyway).
        """
        head = self._current_track
        if head is None:
            return False
        seed_id = getattr(head, "id", None)
        queue = [head] + [t for t in tracks if getattr(t, "id", None) != seed_id]
        if len(queue) < 2:
            return False  # the mix was the song itself: nothing to queue
        self._queue = queue
        self._queue_index = 0
        self._prefetch = None
        self._prefetch_id = None
        # The rows under the queue screen's cursor are all different rows now
        self._queue_cursor = 0
        # This queue is the user's, not the restore's: a restore still fetching
        # the old one must not land on top of it, and saves are whole again
        self._restore_pending = False
        self._set_toast(f"Radio started — {len(queue) - 1} tracks up next")
        return True

    def _handle_media_key(self, action):
        """Apply a media-key action coming from mpv (macOS only)."""
        if action == "next":
            self._next_track()
        elif action == "prev":
            self._prev_track()
        elif action == "toggle":
            self._toggle_play()
        elif action == "play" and not self._playing:
            self._toggle_play()
        elif action == "pause" and self._playing:
            self._toggle_play()

    def _monitor_playback(self):
        """Background thread: auto-advance, position resync, periodic save."""
        last_save = time.time()
        dead_polls = 0
        while self.running:
            if (self._playing and not self._track_changing and self.audio
                    and not self._seek_applying
                    and not self.audio.is_paused and not self.audio.is_playing):
                # Require two consecutive dead polls before advancing: a track
                # change kills the old process before spawning the new one,
                # and a single poll can land inside that window. A seek being
                # applied is the same window on ffplay, which has to be killed
                # and respawned to land anywhere but where it already is.
                dead_polls += 1
            else:
                dead_polls = 0
            if dead_polls >= 2:
                dead_polls = 0
                failure = self.audio.failure() if self.audio else None
                if (failure and self.audio and not self.audio.source_vanished()
                        and self._track_has_time_left()):
                    # The backend refused the stream. Say so and stop, rather
                    # than advancing through the whole queue in silence —
                    # whatever it could not play, the next track is usually
                    # the same kind of thing.
                    self._set_toast(f"Playback failed — {failure}",
                                    seconds=PLAYER_ERROR_SECONDS)
                    logger.warning("Playback failed: %s", failure)
                    self._playing = False
                    self._play_start_time = None
                elif (self.audio and self._current_track is not None
                        and self.audio.source_vanished()
                        and self._track_has_time_left()):
                    # The cached file was deleted between "it exists" and the
                    # player opening it, so the player exited at once. Without
                    # this the track is silently skipped; start it again from
                    # the network, where it was left. A track that had already
                    # finished playing needs no rescue — clearing the cache
                    # mid-track leaves its file gone at the natural end too.
                    self._play_track(self._current_track, seek=self._get_position())
                elif self._stream_ended_early():
                    # The stream died mid-track. Both backends answer a dead
                    # source by playing their buffer out and exiting 0 with an
                    # empty stderr, which is indistinguishable from the end of
                    # a song — except by the clock. Stop and say so rather
                    # than advancing: a track that died must not look like a
                    # track that finished (ai/INCIDENTS #3, by another door).
                    position = self._get_position()
                    duration = getattr(self._current_track, "duration", 0) or 0
                    self._set_toast(
                        "Playback stopped early — the stream ended at "
                        f"{format_time(position)} of {format_time(duration)}."
                        " [space] to resume",
                        seconds=PLAYER_ERROR_SECONDS)
                    logger.warning("Stream ended early at %.0fs of %.0fs",
                                   position, duration)
                    self._playing = False
                    self._play_start_time = None
                    self._play_offset = position
                elif self._queue and self._queue_index < len(self._queue) - 1:
                    self._play_queue_index(self._queue_index + 1)
                else:
                    self._playing = False
                    self._play_start_time = None
                    self._play_offset = 0
            elif self._playing and self.audio:
                # Resync wall-clock position with mpv's real position so
                # startup latency and drift never accumulate — but not while a
                # scrub is still on its way to the backend, or the bar would
                # snap back to where the track was before the arrow key
                if not self._seek_pending():
                    pos = self.audio.get_time_pos()
                    if pos is not None:
                        self._play_offset = pos
                        self._play_start_time = time.time()
                self._maybe_count_play()
                self._maybe_prefetch_next()
            # A press that came in too soon after the last one left its
            # position waiting; this tick is what delivers it
            self._flush_seek()
            # The download bars' rate number, on this tick and no other
            self._sample_download_rates()
            # A cached copy whose reclaim was deferred because its track was
            # playing gets its retry here, on the first tick it no longer is
            self._reclaim_deferred_copies()
            # Save state periodically so a crash doesn't lose the position
            if time.time() - last_save > 10:
                self._save_state()
                last_save = time.time()
            if self.audio:
                self._handle_media_key(self.audio.poll_media_key())
            time.sleep(0.5)

    # ── Display builders ──

    def _track_has_time_left(self) -> bool:
        """Whether the current track stopped early enough to be worth
        resuming. An unknown duration counts as "yes" — better one extra
        respawn than a silently dropped track. Within 2s of the end counts
        as finished."""
        duration = getattr(self._current_track, "duration", None) or 0
        if duration <= 0:
            return True
        return self._get_position() < duration - 2.0

    def _maybe_count_play(self) -> None:
        """Give the playing track its point, once, when it has earned it.

        A skip is not a play. The eviction rule is points per play, so
        counting one the moment a track starts would let a four-hour shuffle
        through a hundred previews out-score a staple — the exact thing the
        rule exists to prevent. `PLAY_COUNTS_AFTER` seconds in, or half way
        through anything shorter than that.

        On the monitor's existing 0.5 s tick, so it costs no new wakeups, and
        latched on `_play_gen` so it cannot fire twice for one play or once
        for a track the user has already skipped past. The write is a few
        hundred bytes of JSON, off the UI thread.
        """
        if self._play_counted == self._play_gen or not self._playing:
            return
        track = self._current_track
        track_id = getattr(track, "id", None)
        if track_id is None:
            return
        duration = getattr(track, "duration", 0) or 0
        threshold = min(PLAY_COUNTS_AFTER, duration / 2) if duration > 0 \
            else PLAY_COUNTS_AFTER
        if self._get_position() < threshold:
            return
        self._play_counted = self._play_gen
        try:
            self._cache.note_played(track_id)
        except Exception as e:  # pragma: no cover - bookkeeping only
            logger.debug("Could not record a play: %s", e)

    def _stream_ended_early(self) -> bool:
        """Whether the backend stopped a long way short of the end of the track.

        The thing this exists to catch is invisible to `failure()`. Measured
        against a loopback HLS server returning `403 Request has expired` from
        segment 3 of 45, **both** mpv and ffplay played their buffer out and
        exited `0` with an **empty stderr**, 12.1 s into a 176 s track — which
        is byte for byte what reaching the end of a song looks like. So the
        monitor advanced, and the user heard twelve seconds of a three-minute
        track and nothing to say why. If the cause is systemic (an expired
        session, the network down) it walks the whole queue in near-silence.

        That is reachable without any exotic failure: a pause longer than the
        ~1 h life of a signed URL, since ffplay's `resume()` respawns against
        the *same* expired string, and any network blip mid-track.

        The clock is the only witness. Deliberately the opposite of
        `_track_has_time_left`'s handling of an unknown duration: there, "can't
        say" means resume, because a wrong guess costs one extra spawn. Here a
        wrong "yes" **stops the queue on a track that really did end**, so
        "can't say" means advance exactly as before.
        """
        track = self._current_track
        if track is None:
            return False
        duration = getattr(track, "duration", None) or 0
        if duration <= 0:
            return False
        return self._get_position() < duration - STREAM_TRUNCATED_MARGIN

    def _get_position(self) -> float:
        if self._play_start_time and self._playing:
            return self._play_offset + (time.time() - self._play_start_time)
        return self._play_offset

    def _art_layout(self):
        """`(cols, rows, beside)` for this window, or None for no artwork.

        The one place the size and the placement are decided, because they are
        one decision: `beside` changes what size fits, the size is what the
        fetch thread is asked for and what the disk cache is keyed on, and two
        callers computing it separately is how a picture ends up rendered at
        one size and laid out at another.
        """
        # `self._mini_player`, not `self._fit.mini`: this is asked outside a
        # build too, where the fit is whatever the last one left behind
        if (not self._show_artwork or self._mini_player
                or self._mode != self.MODE_PLAYER or not self._fit.artwork):
            return None
        cover = artwork.cover_id_of(self._current_track)
        if not cover or not artwork.supports_art(self.console):
            return None
        try:
            width, height = self.console.size
        except Exception:
            return None
        beside = artwork.art_beside(width, height)
        size = artwork.art_size(width, height, beside)
        if size is None:  # terminal too small to give artwork the room
            return None
        return size[0], size[1], beside

    def _artwork_text(self):
        """The current cover as half-block pixel art, or None.

        None is the normal answer for most of a track's first second: nothing
        is fetched on this thread. The first paint that wants artwork starts a
        daemon thread and returns None; when that thread lands it assigns the
        pixels and wakes the loop, and the next paint has a picture. Every
        reason there might never be one — no cover, no colour, no room, a
        failed fetch, an undecodable image — comes out here as None too.
        """
        layout = self._art_layout()
        if layout is None:
            return None
        cols, rows, _ = layout
        cover = artwork.cover_id_of(self._current_track)
        ready = self._artwork
        if ready is not None and ready[:3] == (cover, cols, rows):
            # A stored None is an answered question: this cover has no art
            return artwork.render(ready[3], indent=INDENT) if ready[3] else None
        self._request_artwork(cover, cols, rows)
        return None

    def _request_artwork(self, cover: str, cols: int, rows: int):
        """Fetch and render one cover at one size, off the UI thread.

        Guarded by the request key rather than a lock: the same key means the
        thread already running will answer it, and a different one (new track,
        resized terminal) makes whatever that thread returns stale, which it
        checks for itself before assigning anything.
        """
        key = (cover, cols, rows)
        if self._artwork_request == key:
            return
        self._artwork_request = key

        def _run():
            try:
                pixels = artwork.load(cover, cols, rows)
            except Exception:
                pixels = None  # artwork never takes the player down with it
            if self._artwork_request != key:
                return
            self._artwork = (cover, cols, rows, pixels)
            self._wake()

        threading.Thread(target=_run, daemon=True).start()

    def _build_player_display(self) -> Text:
        s = self._current_track
        title = s.name if s else "No track"
        artist = ", ".join(a.name for a in s.artists) if s and s.artists else ""
        album = s.album.name if s and s.album else ""
        duration = s.duration if s else 0
        position = self._get_position() if s else 0
        liked = (s.id in self._liked_ids) if s else None

        state_icon = "\u25b6" if self._playing else "\u23f8"

        # Mini player: single compact line
        if self._mini_player:
            content = Text()
            content.append(f" {state_icon} ", style="bold cyan")
            if liked is True:
                content.append("\u2665 ", style="bold red")
            content.append(title, style="bold white")
            if artist:
                content.append(f" \u2022 {artist}", style="dim white")
            pos_str = format_time(position)
            dur_str = format_time(duration) if duration > 0 else "--:--"
            content.append(f"  {pos_str}/{dur_str}", style="cyan")
            if self._queue:
                content.append(f"  [{self._queue_index + 1}/{len(self._queue)}]", style="dim")
            if self._player_focus:
                content.append(f"  \u21c6 {SEEK_STEP_SECONDS}s", style="bold yellow")
            return content

        # Full player display. Beside the cover the text has the columns the
        # cover left; above it, or while the picture is still being fetched,
        # it has the pane. The floor is what makes that safe to ask outside a
        # build (ROOMY_FIT is 74 columns whatever the terminal is): a text
        # column narrower than the layout was designed for goes back to
        # stacked rather than clipping the title to nothing.
        layout = self._art_layout()
        art = self._artwork_text()
        text_width = self._fit.inner
        beside = art is not None and layout is not None and layout[2]
        if beside:
            text_width = self._fit.inner - INDENT - layout[0] - artwork.ART_GUTTER
            if text_width < artwork.MIN_TEXT_WIDTH:
                beside, text_width = False, self._fit.inner

        track_line = Text()
        track_line.append(f" {state_icon} ", style="bold cyan")
        if liked is True:
            track_line.append("\u2665 ", style="bold red")
        elif liked is False:
            track_line.append("\u2661 ", style="dim")
        track_line.append(title, style="bold white")
        if artist:
            track_line.append(f"  {artist}", style="dim white")

        album_line = Text()
        if album:
            album_line.append(f"   {album}", style="dim")

        progress_line = self._build_progress_line(position, duration, text_width)

        # Queue info
        status_line = Text()
        if self._queue:
            status_line.append(f"   Queue: {self._queue_index + 1}/{len(self._queue)}", style="dim")
        # The setting's label is only the truth while the setting is what is
        # being played. A downloaded file is the user's own and is played
        # whatever the setting says (see `_local_source`), so it says what it
        # actually is instead — a badge reading MAX over a HIGH download
        # would be exactly the kind of claim this project does not make.
        quality_label = self._playing_badge or \
            self.QUALITY_LABELS.get(self._quality_name, "")
        if quality_label:
            status_line.append(f"   {quality_label}", style="dim cyan")

        # Next track preview (only in player mode)
        up_next = Text()
        if self._mode == self.MODE_PLAYER and self._queue and self._queue_index < len(self._queue) - 1:
            t = self._queue[self._queue_index + 1]
            t_name = t.name if hasattr(t, "name") else "?"
            t_artist = t.artists[0].name if hasattr(t, "artists") and t.artists else ""
            up_next.append("   Next: ", style="dim")
            up_next.append(t_name, style="dim white")
            if t_artist:
                up_next.append(f" \u2022 {t_artist}", style="dim")

        lines = [track_line, album_line, progress_line]
        if self._fit.chrome:
            # Under pressure the queue position and the quality badge are the
            # first player rows to go: both are summaries, and the screens
            # this is given up on (the lists) show the queue itself. Never
            # pulled on the player screen — chrome is not one of its levers —
            # so the full player always says them.
            lines.append(status_line)
        if up_next.plain:
            lines.append(up_next)

        if beside:
            return _side_by_side(_clip_lines(art, INDENT + layout[0]),
                                 [line for row in lines
                                  for line in _clip_lines(row, text_width)])

        content = Text()
        if art is not None:
            content.append_text(art)
            content.append("\n\n")
        content.append_text(Text("\n").join(lines))
        return content

    def _build_progress_line(self, position, duration, width=None) -> Text:
        """Elapsed, the bar, and the duration — on one line, always.

        The bar used to be laid out at `progress_bar_width` columns whatever
        the window was, so a narrow one wrapped it: the elapsed time, half a
        bar, the other half, and the duration stranded on a fourth line. It
        reads as a broken display, and it is the reason that setting is a
        ceiling now. What the bar actually gets is whatever the two times and
        the indent leave over, and under MIN_BAR_WIDTH there is no honest bar
        to draw at all — the times alone are more informative than four cells
        that round to the same place for half the track.

        The scrub marker is part of this line and not an afterthought appended
        to it: focus is a state the arrows are in, so it has to be on screen,
        and the columns it costs have to come out of the bar rather than off
        the end of the pane.

        `width` is what the line has to lay itself out in, which is the pane
        unless a cover is sitting beside it — then it is the columns the cover
        left, and the bar shortens rather than the line wrapping under it.
        """
        if width is None:
            width = self._fit.inner
        pos_str = format_time(position)
        dur_str = format_time(duration) if duration > 0 else "--:--"
        marker = f"  ⇆ {SEEK_STEP_SECONDS}s" if self._player_focus else ""
        room = (width - INDENT - len(pos_str) - len(dur_str)
                - len(marker) - 2)
        bar_width = min(self._bar_max, room)
        line = Text()
        if bar_width < MIN_BAR_WIDTH:
            line.append(f"{' ' * INDENT}{pos_str} / {dur_str}", style="cyan")
            if marker:
                line.append(marker, style="bold yellow")
            return line
        if duration > 0:
            filled = int(bar_width * min(position / duration, 1.0))
            bar = "━" * filled + "╸" + "─" * max(0, bar_width - filled - 1)
        else:
            bar = "─" * bar_width
        line.append(f"{' ' * INDENT}{pos_str} ", style="cyan")
        line.append(bar, style="bold cyan" if self._playing else "dim")
        line.append(f" {dur_str}", style="cyan")
        if marker:
            line.append(marker, style="bold yellow")
        return line

    def _page_rows(self) -> int:
        """How many rows a list page may show: the setting, capped by what the
        window actually has room for (see _Fit). The setting is a preference
        about density; the window is a fact."""
        return max(1, min(self._page_size, self._fit.page_rows))

    def _tab_row(self, order, labels, active, mark=None) -> Text:
        """The '[Tab] label · label' row for a tabbed screen: every entry with
        the active one lit, or — below the width that holds them all — the
        active label alone with its place in the cycle. `mark(name)` (when
        given) decides which entries get the answered-dot after their label;
        the narrow form keeps the active entry's dot too."""
        row = Text()
        row.append("\n   [Tab]", style="bold")
        for i, name in enumerate(order):
            row.append("  " if i == 0 else " · ", style="dim")
            row.append(labels[name],
                       style="bold cyan" if name == active else "dim")
            if mark is not None and mark(name):
                row.append("·", style="dim cyan")
        if cell_len(row.plain.lstrip("\n")) > self._fit.inner:
            row = Text("\n   [Tab]", style="bold")
            row.append(f"  {labels[active]}", style="bold cyan")
            if mark is not None and mark(active):
                row.append("·", style="dim cyan")
            row.append(f"  ({order.index(active) + 1}/{len(order)})",
                       style="dim")
        return row

    def _page_footer(self, content: Text, cursor: int, total: int, page: int):
        """The 'Page X/Y' footer every paged list shows, when there is more
        than one page. (The search screen keeps its own copy because it
        appends the result count inside the same branch.)"""
        if total > page:
            page_num = (cursor // page) + 1
            total_pages = (total + page - 1) // page
            content.append(f"\n\n   Page {page_num}/{total_pages}", style="dim")

    def _build_search_display(self) -> Text:
        content = Text()
        content.append("   Search: ", style="bold yellow")
        content.append(self._search_query, style="white")
        content.append("\u2588", style="bold white")

        # The scope row. It is always on screen, so Tab has something to point
        # at and the active scope is never hidden state. A scope already shown
        # for this query is marked, so "Tab is free from here on" is visible
        # rather than something you have to have read the source to know.
        #
        # Six scopes do not fit a narrow pane, and the answer is not to let the
        # row run off the end of one: below the width that holds them all, the
        # active scope stands alone with its place in the cycle, which says the
        # same two things — which scope you are in, and that Tab moves through
        # more of them.
        content.append_text(self._tab_row(
            self.SEARCH_FILTERS, self.SEARCH_FILTER_LABELS,
            self._search_filter, mark=self._scope_answered))

        if self._search_loading and not self._search_results:
            # A scope waiting for its first page. Not the same as one that came
            # back empty (green, below) and not the same as one served out of
            # what the session already had (marked under the rows).
            content.append("\n\n   Searching...", style="dim yellow")
        elif self._search_message:
            content.append(f"\n\n   {self._search_message}", style="dim green")
        elif self._search_results:
            total = len(self._search_results)
            page = self._page_rows()
            page_start = (self._search_cursor // page) * page
            page_end = min(page_start + page, total)
            content.append("\n", style="")
            for i in range(page_start, page_end):
                item = self._search_results[i]
                content.append("\n")
                if i == self._search_cursor:
                    content.append("  \u25b8 ", style="bold cyan")
                else:
                    content.append("    ", style="")
                self._download_mark(
                    content, getattr(item.get("obj"), "id", None)
                    if item["type"] == "track" else None)
                type_styles = {"track": "bold green", "album": "bold magenta",
                               "artist": "bold yellow", "playlist": "bold blue"}
                badge = item["type"].upper()
                content.append(f"[{badge}]", style=type_styles.get(item["type"], "dim"))
                content.append(f" {item['name']}", style="bold white" if i == self._search_cursor else "white")
                if item.get("artist"):
                    content.append(f"  {item['artist']}", style="dim")
                if item.get("playlist"):
                    content.append(f"  in {item['playlist']}", style="dim cyan")
            if self._search_loading:
                # The next page is on its way — say so under the last row, so
                # scrolling off the bottom never looks like a frozen player
                content.append("\n\n   Loading more...", style="dim yellow")
            elif self._search_view()["cached"]:
                # These rows cost nothing: they were already in hand when Tab
                # landed here. Said plainly, because "instant" and "stale" are
                # the same thing seen from two sides.
                content.append("\n\n   Already loaded this session", style="dim")
            if total > page:
                page_num = (self._search_cursor // page) + 1
                total_pages = (total + page - 1) // page
                content.append(f"\n\n   Page {page_num}/{total_pages}", style="dim")
                content.append(f"  ({total} results)", style="dim")
                # Only on the last page, where "down" has stopped doing
                # anything and the reason needs saying
                if (page_num == total_pages and self._search_done
                        and not self._pool_size(self._search_pool)):
                    content.append("  end of results", style="dim")
        elif self._search_query:
            content.append("\n\n   Press Enter to search", style="dim")
        elif self._history_rows():
            # The blank pane remembers. These are memories, not answers — no
            # type badges — and the first printable key replaces them with
            # the live query exactly as if they had never been there. Paged
            # the same way as results: the window follows the cursor, so a
            # long history is walked, not truncated.
            rows = self._history_rows()
            page = self._page_rows()
            page_start = (0 if self._search_history_cursor is None
                          else (self._search_history_cursor // page) * page)
            content.append("\n\n   Recent searches", style="dim")
            content.append("\n")
            for i in range(page_start, min(page_start + page, len(rows))):
                content.append("\n")
                if i == self._search_history_cursor:
                    content.append("  ▸ ", style="bold cyan")
                    content.append(rows[i], style="bold white")
                else:
                    content.append("    ", style="")
                    content.append(rows[i], style="white")

        return content

    def _build_browse_display(self) -> Text:
        content = Text()
        content.append(f"   {self._browse_title}", style="bold magenta")

        if self._browse_loading:
            content.append("\n\n   Loading...", style="dim yellow")
        elif self._browse_message:
            content.append(f"\n\n   {self._browse_message}", style="dim green")
        elif self._browse_tracks:
            total = len(self._browse_tracks)
            page = self._page_rows()
            # browse_cursor -1 = "Play All" row, 0..N-1 = tracks
            page_start = max(0, ((self._browse_cursor - 1) // page) * page) if self._browse_cursor > 0 else 0
            page_end = min(page_start + page, total)
            content.append(f"  ({total} tracks)", style="dim")
            content.append("\n", style="")

            # "Play All" row (always visible when on first page)
            if self._browse_cursor <= 0 or page_start == 0:
                content.append("\n")
                if self._browse_cursor == -1:
                    content.append("  \u25b8 ", style="bold cyan")
                    content.append("  \u25b6 Play All", style="bold cyan")
                else:
                    content.append("    ", style="")
                    content.append("  \u25b6 Play All", style="dim green")

            for i in range(page_start, page_end):
                track = self._browse_tracks[i]
                content.append("\n")
                if i == self._browse_cursor:
                    content.append("  \u25b8 ", style="bold cyan")
                else:
                    content.append("    ", style="")
                self._download_mark(content, getattr(track, "id", None))
                content.append(f"{i+1:>2}. ", style="dim")
                content.append(track.name, style="bold white" if i == self._browse_cursor else "white")
                if track.artists:
                    content.append(f"  {track.artists[0].name}", style="dim")
                content.append(f"  {format_time(track.duration)}", style="dim cyan")
            self._page_footer(content, max(0, self._browse_cursor - 1), total, page)

        return content

    def _build_artist_display(self) -> Text:
        content = Text()
        name = getattr(self._artist, "name", "") or "Artist"
        content.append(f"   {name}", style="bold magenta")

        # The tab row, always on screen for the same reason search's scope row
        # is: which section you are in is never hidden state, and Tab has
        # something to point at. Narrower than the four tabs need, it says the
        # same thing about the one you are on and counts the rest — which is
        # the pair of facts, and is not "Suggesti…" cut off at the border.
        content.append_text(self._tab_row(
            self.ARTIST_SECTIONS, self.ARTIST_SECTION_LABELS,
            self._artist_section))

        record = self._artist_record()
        label = self.ARTIST_SECTION_LABELS[self._artist_section].lower()
        if record is None or record["state"] == "loading":
            content.append(f"\n\n   Loading {label}...", style="dim yellow")
            return content
        if record["state"] == "failed":
            # Red and with a way out: a failure is not "there is nothing here",
            # and the difference has to be visible at a glance
            content.append(f"\n\n   {record['message']}", style="bold red")
            content.append("\n   [Enter] retry  [Tab] another section", style="dim")
            return content

        rows = record["items"]
        if not rows:
            content.append(f"\n\n   {record['message']}", style="dim green")
            return content

        total = len(rows)
        page = self._page_rows()
        cursor = min(max(self._artist_cursor, 0), total - 1)
        page_start = (cursor // page) * page
        page_end = min(page_start + page, total)
        content.append(f"  ({total})", style="dim")
        content.append("\n", style="")
        type_styles = {"track": "bold green", "album": "bold magenta",
                       "playlist": "bold cyan", "artist": "bold yellow"}
        for i in range(page_start, page_end):
            row = rows[i]
            obj = row["obj"]
            selected = (i == cursor)
            content.append("\n")
            content.append("  ▸ " if selected else "    ",
                           style="bold cyan" if selected else "")
            # Same gutter as browse, queue and search. A mixed section holds
            # albums and artists too, and only a track can be downloaded — so
            # the columns are reserved on every row rather than only on the
            # track ones, or the badges below a downloaded track would sit two
            # columns left of the badges above it.
            self._download_mark(
                content, getattr(obj, "id", None)
                if row["type"] == "track" else None)
            content.append(f"[{row['type'].upper()}]",
                           style=type_styles.get(row["type"], "dim"))
            item_name = getattr(obj, "name", None) or "?"
            content.append(f" {item_name}", style="bold white" if selected else "white")
            if row["type"] == "track":
                artists = getattr(obj, "artists", None)
                if artists:
                    content.append(f"  {artists[0].name}", style="dim")
                content.append(f"  {format_time(getattr(obj, 'duration', 0) or 0)}",
                               style="dim cyan")
            elif row["type"] == "album":
                artist = getattr(obj, "artist", None)
                if artist is not None and getattr(artist, "name", None):
                    content.append(f"  {artist.name}", style="dim")
                year = getattr(obj, "year", None)
                if year:
                    content.append(f"  {year}", style="dim cyan")
            elif row["type"] == "playlist":
                num_tracks = getattr(obj, "num_tracks", None)
                if num_tracks:
                    content.append(f"  {num_tracks} tracks", style="dim cyan")
                creator = getattr(obj, "creator", None)
                if creator is not None and getattr(creator, "name", None):
                    content.append(f"  by {creator.name}", style="dim")
        self._page_footer(content, cursor, total, page)

        return content

    def _build_queue_display(self) -> Text:
        content = Text()
        content.append("   Queue", style="bold yellow")
        if not self._queue:
            content.append("\n\n   Queue is empty", style="dim")
        else:
            total = len(self._queue)
            page = self._page_rows()
            page_start = (self._queue_cursor // page) * page
            page_end = min(page_start + page, total)
            content.append(f"  ({total} tracks)", style="dim")
            content.append("\n", style="")
            for i in range(page_start, page_end):
                track = self._queue[i]
                content.append("\n")
                is_current = (i == self._queue_index)
                is_cursor = (i == self._queue_cursor)
                if is_cursor:
                    content.append("  \u25b8 ", style="bold cyan")
                elif is_current:
                    content.append("  \u266b ", style="bold cyan")
                else:
                    content.append("    ", style="")
                self._download_mark(content, getattr(track, "id", None))
                t_name = track.name if hasattr(track, "name") else "?"
                t_artist = track.artists[0].name if hasattr(track, "artists") and track.artists else ""
                t_dur = format_time(track.duration) if hasattr(track, "duration") else ""
                name_style = "bold cyan" if is_current else ("bold white" if is_cursor else "white")
                content.append(f"{i + 1:>2}. ", style="dim")
                content.append(t_name, style=name_style)
                if is_current:
                    content.append("  \u25b6" if self._playing else "  \u23f8", style="bold cyan")
                if t_artist:
                    content.append(f"  {t_artist}", style="dim")
                if t_dur:
                    content.append(f"  {t_dur}", style="dim cyan")
            self._page_footer(content, self._queue_cursor, total, page)
        return content

    def _build_playlists_display(self) -> Text:
        content = Text()
        content.append("   Your Playlists", style="bold magenta")

        if self._playlists_loading:
            content.append("\n\n   Loading playlists...", style="dim yellow")
        elif self._playlists_message:
            content.append(f"\n\n   {self._playlists_message}", style="dim green")
        elif self._playlists:
            total = len(self._playlists)
            page = self._page_rows()
            page_start = (self._playlists_cursor // page) * page
            page_end = min(page_start + page, total)
            content.append(f"  ({total})", style="dim")
            content.append("\n", style="")
            for i in range(page_start, page_end):
                pl = self._playlists[i]
                content.append("\n")
                if i == self._playlists_cursor:
                    content.append("  \u25b8 ", style="bold cyan")
                else:
                    content.append("    ", style="")
                pl_name = pl.name if hasattr(pl, "name") else "?"
                num_tracks = pl.num_tracks if hasattr(pl, "num_tracks") else ""
                creator = ""
                if hasattr(pl, "creator") and pl.creator:
                    creator = pl.creator.name if hasattr(pl.creator, "name") else ""
                content.append(pl_name, style="bold white" if i == self._playlists_cursor else "white")
                if num_tracks:
                    content.append(f"  {num_tracks} tracks", style="dim cyan")
                if creator:
                    content.append(f"  by {creator}", style="dim")
            self._page_footer(content, self._playlists_cursor, total, page)
        else:
            content.append("\n\n   No playlists found", style="dim")

        return content

    def _build_add_to_playlist_display(self) -> Text:
        content = Text()
        track_name = self._picker_track.name if self._picker_track is not None else "?"
        content.append("   Add to playlist: ", style="bold magenta")
        content.append(track_name, style="bold white")

        # The "+ New playlist" row is cursor -1 and always on screen, above the
        # list and off the paging, so it is reachable from any page and from an
        # empty list. Typing turns it into a textbox with a visible caret, the
        # way the settings page does, so typing never looks like navigating.
        content.append("\n\n")
        if self._picker_new_name is not None:
            content.append("  ▸ ", style="bold cyan")
            content.append("New playlist: ", style="bold white")
            content.append(f"‹ {self._picker_new_name}▏›", style="bold yellow")
            # The two keys this row takes are the footer's whole content while
            # it is open (see _mode_hints), and a second copy of them here is
            # the first thing to run off the end of a narrow pane — the only
            # hint in the app that was written outside _fit_hints, and it was
            # the only one that could be cut in half
        else:
            selected = self._picker_cursor < 0
            content.append("  ▸ " if selected else "    ", style="bold cyan" if selected else "")
            content.append("+ New playlist", style="bold white" if selected else "white")

        playlists = self._picker_playlists()
        if self._picker_loading:
            content.append("\n\n   Loading playlists...", style="dim yellow")
        elif playlists:
            total = len(playlists)
            page = self._page_rows()
            # The cursor sits at -1 on the "+ New playlist" row, which belongs
            # to no page — clamp rather than divide a negative into one
            page_start = (max(self._picker_cursor, 0) // page) * page
            page_end = min(page_start + page, total)
            content.append("\n", style="")
            for i in range(page_start, page_end):
                pl = playlists[i]
                content.append("\n")
                if i == self._picker_cursor:
                    content.append("  ▸ ", style="bold cyan")
                else:
                    content.append("    ", style="")
                pl_name = pl.name if hasattr(pl, "name") else "?"
                num_tracks = pl.num_tracks if hasattr(pl, "num_tracks") else ""
                content.append(pl_name, style="bold white" if i == self._picker_cursor else "white")
                if num_tracks != "":
                    content.append(f"  {num_tracks} tracks", style="dim cyan")
                # Say why this one is at the top, rather than reordering silently
                if i == 0 and self._last_playlist_id and str(
                        getattr(pl, "id", "") or "") == self._last_playlist_id:
                    content.append("  last used", style="dim")
            self._page_footer(content, max(self._picker_cursor, 0), total, page)
        else:
            content.append("\n\n   No playlists you can edit", style="dim")

        return content

    def _reconcile_cache(self) -> None:
        """Bring the cache tracker back in line with what is on disk.

        The tracker is the source of truth for *intent* — what ticli meant to
        cache, at which tier, and how often it has been played — and the disk
        is the source of truth for *existence*. Songs deleted from the cache
        folder by hand have to be handled durably, so an entry whose file is
        gone is dropped and the totals corrected, and a file with no entry is
        adopted at zero plays (which is also how a cache from before the
        tracker, or a crash between the rename and the tracker write, is
        picked up).

        A daemon thread, because it is a directory listing plus a stat per
        file, and the settings page must never pay for that.
        """
        def _run():
            try:
                dropped, adopted = self._cache.reconcile()
                if dropped or adopted:
                    logger.debug("Cache tracker reconciled: -%d +%d",
                                 dropped, adopted)
                    self._wake()
            except Exception as e:  # pragma: no cover - bookkeeping only
                logger.debug("Could not reconcile the cache tracker: %s", e)
            # The one moment duplicates that survived a quit are caught.
            # After reconcile on purpose, so a cache file adopted just now
            # is already in the tracker when its download shadows it.
            try:
                self._reclaim_download_duplicates()
            except Exception as e:  # pragma: no cover - bookkeeping only
                logger.debug("Could not sweep download duplicates: %s", e)

        threading.Thread(target=_run, daemon=True).start()

    def _downloads(self) -> list:
        """What is really in the music folder, measured once and kept.

        `downloads.present()` is one index read and one stat per entry, which
        is already 229x cheaper than the calls it replaced — but it is still
        disk work, and `_repaint` builds the display on every idle tick before
        deciding whether the frame changed. So it is remembered, and dropped
        by the things that move it: opening the settings page or the list, a
        download landing, and a delete.
        """
        if self._downloads_present is None:
            self._downloads_present = downloads.present()
        return self._downloads_present

    def _downloaded(self, track_id) -> bool:
        """Is this track already in the music folder?

        Answered out of the one memoised measurement, so a 200-row page costs
        the same single index read a 1-row page does — the alternative is a
        `path_for` per row per repaint, which is finding F6 of
        ai/reference/data-path-audit-2026-07-26.md: 229 ms of UI-thread JSON,
        twice a second.
        """
        if track_id is None:
            return False
        if self._downloads_ids is None:
            self._downloads_ids = {row["id"] for row in self._downloads()}
        return str(track_id) in self._downloads_ids

    def _forget_downloads(self) -> None:
        """Re-measure the music folder next time anything asks."""
        self._downloads_present = None
        self._downloads_ids = None
        self._download_known = None

    def _download_usage(self) -> tuple:
        """`(downloads on disk, what they cost)`."""
        rows = self._downloads()
        return len(rows), sum(row["bytes"] for row in rows)

    def _download_mark(self, content: Text, track_id) -> None:
        """The two columns every track row starts with.

        A dim `↓` says the file is already on this disk. Reserved rather than
        inserted, so the titles under it stay in one column whether or not
        anything on the page is downloaded — and drawn at the *left*, before
        the number, because it is the one new thing on the row and the right
        edge is what a narrow window clips.

        `↓` and not a dot or a diamond: those read as bullets, and the two
        marks this app already draws in that gutter are `▸` (the cursor) and
        `▶` (Play All).
        """
        if self._downloaded(track_id):
            content.append("↓ ", style="dim cyan")
        else:
            content.append("  ", style="")

    def _build_settings_display(self) -> Text:
        content = Text()
        content.append("   Settings", style="bold magenta")

        # The page scrolls now rather than running off the bottom of a short
        # window: a window of rows around the cursor, and a (n/N) beside the
        # title whenever it is a window rather than the whole table — so a
        # table with rows above or below it never looks like the whole table.
        room = max(1, min(len(SETTINGS_ROWS), self._fit.page_rows))
        first = 0
        if room < len(SETTINGS_ROWS):
            first = max(0, min(self._settings_cursor - room // 2,
                               len(SETTINGS_ROWS) - room))
            content.append(f"  ({self._settings_cursor + 1}/{len(SETTINGS_ROWS)})",
                           style="dim")
        content.append("\n", style="")

        for i in range(first, first + room):
            spec = SETTINGS_ROWS[i]
            selected = (i == self._settings_cursor)
            content.append("\n")
            if selected:
                content.append("  ▸ ", style="bold cyan")
            else:
                content.append("    ", style="")
            # The value column only lines up while there is width to line it
            # up in; below that the padding is just columns the value needed
            label = f"{spec['label']:<20}" if self._fit.inner >= 46 else spec["label"] + " "
            content.append(label, style="bold white" if selected else "white")
            value = self.config.get(spec["key"], spec["default"])
            editing = selected and self._settings_edit is not None
            if editing:
                # A visible caret, so typing never looks like navigating
                content.append(f"‹ {self._settings_edit}▏›", style="bold yellow")
            elif selected:
                # Chevrons on the selected row signal that ←/→ change it
                content.append(f"‹ {display_value(spec, value)} ›", style="bold cyan")
            else:
                content.append(f"  {display_value(spec, value)}", style="dim cyan")
            # A choice value on its own says nothing about what you'll hear —
            # spell the stream out next to it
            meaning = spec.get("value_desc", {}).get(str(value).upper(), "")
            if meaning:
                content.append(f"  {meaning}", style="dim")
            if spec["key"] == "cache_songs":
                # Tiny status: what the toggle is actually holding right now
                songs = self._cache.audio_count()
                content.append(
                    f"  {songs} song{'' if songs == 1 else 's'} on disk", style="dim")

        # What is below the table is two different kinds of thing, and only one
        # of them is expendable: the prose explains the selected row, and the
        # three lines under it are the *only* place [x], [o] and [u] are
        # written down. A short window drops the prose and keeps the actions.
        spec = SETTINGS_ROWS[self._settings_cursor]
        prose = self._fit.prose
        if self._settings_edit is not None:
            content.append(
                "\n\n   Typing a number — Enter or Esc saves it, Backspace deletes",
                style="dim",
            )
        if prose:
            content.append(f"\n\n   {spec['desc']}", style="dim")
        # The whole ladder, so ←/→ lands somewhere you already understand.
        # Fits 80 columns for the four quality tiers.
        if prose and spec.get("value_desc"):
            content.append("\n   ", style="")
            current = str(self.config.get(spec["key"], spec["default"])).upper()
            for i, choice in enumerate(spec["choices"]):
                if i:
                    content.append(" · ", style="dim")
                on = choice == current
                # A tier TIDAL has shown it won't serve is dimmed even when
                # it's the selected one — hiding it would look like a missing
                # feature, where a dimmed one explains itself
                gated = self._quality_unavailable(choice)
                content.append(choice, style="dim" if gated else ("bold cyan" if on else "dim"))
                # The format beside the name, now that the two say different
                # things
                short = self.QUALITY_LABELS.get(choice, "")
                if short and short != choice:
                    content.append(
                        f" {short}", style="dim" if gated else ("cyan" if on else "dim"))
            content.append_text(self._build_quality_gate_note())
        # A --quality flag beats the saved value for this run without changing it
        if self._quality_name != self.config.get("quality"):
            content.append(
                f"\n   Overridden this run by --quality {self._quality_name}",
                style="dim yellow",
            )
        # The two tiers, as two rows of the same little table. They are
        # different in lifetime and ownership, so what each row says is
        # different too: the cache has a budget and an [x], the downloads have
        # a folder and neither — nothing in ticli deletes a track somebody
        # asked for, and their bytes are not the cache's to reclaim. Aligned
        # labels rather than two prose sentences, because the whole point of
        # the request was to be able to read the two numbers against each
        # other. Fits 80 columns; below that it degrades like everything else.
        songs = self._cache.audio_count()
        # One index read for both download numbers, and remembered until
        # something moves them: this pair used to be two calls that each
        # re-read and re-parsed downloads.json once per track, i.e. 229 ms of
        # UI-thread JSON at 500 downloads, twice a second while the page open
        kept, kept_bytes = self._download_usage()
        content.append("\n\n   Cache      ", style="dim")
        content.append(f"{songs:>4} song{' ' if songs == 1 else 's'}", style="dim")
        # "of", not "/", because the budget can be 0 and a fraction with a
        # zero denominator reads as broken rather than as "no room"
        content.append(
            f" · {format_gb(self._cache.disk_bytes())}"
            f" of {self.config.get('cache_budget_gb', 2)}.000 GB", style="dim")
        if (self._download_job or {}).get("state") != "running":
            content.append("   [x]", style="bold")
            content.append(" clear", style="dim")
        content.append("\n   Downloads  ", style="dim")
        content.append(f"{kept:>4} song{' ' if kept == 1 else 's'}", style="dim")
        # "exempt from the budget" rather than the longer "not counted
        # against the budget" it used to say: the same fact in six fewer
        # columns, which is what bought the `[d]` beside it inside 80. The
        # fact stays on the row rather than moving to the list screen,
        # because it is a fact about the number next to it and the whole
        # point of these two rows is reading one number against the other.
        content.append(f" · {format_gb(kept_bytes)} · exempt from the budget",
                       style="dim")
        # The way in to the list. The two rows now offer the same shape of
        # thing in the same place — the cache can be cleared wholesale, the
        # downloads are gone through one at a time — and until this existed
        # the only evidence of a download was this count.
        content.append("   [d]", style="bold")
        content.append(" list", style="dim")
        # A path you can paste into Finder or a terminal, with the one action
        # that spans both tiers beside it — this page has no rows to spare
        # (80x24 shows three settings rows as it is), so a line of its own for
        # an action that is only being *offered* would cost one of those.
        #
        # Order depends on what fits, and the tie-break is stated: a path that
        # runs off the end is a truncated path, while an action that runs off
        # the end is a feature nobody can find. So when both do not fit, the
        # action goes first and the path is what gets cut.
        folder = downloads.display_dir()
        # A download outranks the [R] offer on this row, and costs no row of
        # its own — the page shows three settings rows at 80x24 as it is, and
        # a download is over in minutes while the offer is always there. It
        # never outranks a re-fetch, because a re-fetch is *this page's* job
        # and [Esc] here is what stops it. (The two cannot both be running:
        # each refuses while the other is, so there is only ever one paced
        # resolver.) Empty unless something is happening, so an idle page is
        # exactly the page it was.
        refetch = self._build_refetch_line()
        action = (refetch
                  if (self._refetch_job or {}).get("state") in ("running",
                                                                "blocked")
                  else self._build_download_line() or refetch)
        content.append("\n   ", style="")
        # A run in progress goes first for the same reason, only more so: it
        # is the only thing on the page that is changing
        busy = ((self._refetch_job or {}).get("state") in ("running", "blocked")
                or (self._download_job or {}).get("state")
                in ("running", "blocked", "failed"))
        if busy or len(folder) + len(action.plain) + 3 > max(self._fit.inner - 3, 20):
            content.append_text(action)
            content.append(f"   {folder}", style="dim")
        else:
            content.append(folder, style="dim")
            content.append_text(action)
        # Account lives here rather than on the player screen — it's a thing you
        # look at when you're already in settings, not while listening
        content.append("\n   Logged in as ", style="dim")
        content.append(self._user_display_name or "—", style="bold")
        content.append("   [o]", style="bold")
        content.append(" log out", style="dim")
        content.append_text(self._build_pkce_line())
        return content

    def _download_rows(self) -> list:
        """The downloads list, as rows a screen can draw.

        Derived from the same one measurement everything else in this tier
        reads (`_downloads`), so opening the list costs no extra index read
        and **no network at all** — every field comes off the path the
        downloader wrote or out of the index row beside it.

        The disk is the answer and the index is a hint: `downloads.present()`
        stats every file, so a track dragged to the trash in Finder is simply
        not a row here. The tier is the one TIDAL **granted**, not the one
        that was asked for — a device-flow session asks for hi-res and is
        handed 320k AAC, and a list that said MAX over an AAC file would be
        lying about bytes the user can go and look at.
        """
        granted_names = {quality: name for name, quality
                         in self.QUALITY_MAP.items()}
        rows = []
        for row in self._downloads():
            entry = row["entry"]
            title, artist, _album = downloads.describe(entry.get("path") or "")
            # The fallback is the *asked-for* name the downloader stored, which
            # older indexes wrote in the pre-rename spelling. Translated at
            # read time — never rewritten in the index, where a migration pass
            # would risk the whole file for a display string.
            asked = str(entry.get("quality") or "").upper()
            tier = granted_names.get(entry.get("granted")) or \
                QUALITY_V4_RENAMES.get(asked) or entry.get("quality")
            rows.append({
                "id": row["id"], "path": row["path"], "bytes": row["bytes"],
                "title": title, "artist": artist,
                "tier": str(tier or "").upper(),
            })
        return rows

    def _build_downloads_display(self) -> Text:
        """What is actually in the music folder, newest first.

        Deliberately not a player: Enter does nothing here and there is no
        `[a] play all`. Playing a row means resolving a track id through the
        session, and this screen's whole claim is that it makes no request —
        the songs are all reachable from search and the queue anyway, marked
        with the same `↓`.
        """
        content = Text()
        content.append("   Downloads", style="bold magenta")
        rows = self._download_rows()
        if not rows:
            content.append("\n\n   Nothing downloaded yet", style="dim green")
            content.append(f"\n   {downloads.display_dir()}", style="dim")
            return content

        total = len(rows)
        self._downloads_cursor = min(max(self._downloads_cursor, 0), total - 1)
        page = self._page_rows()
        page_start = (self._downloads_cursor // page) * page
        page_end = min(page_start + page, total)
        # Bytes, not the settings page's gigabytes: there is no budget on this
        # screen to read the number against, and a real library of forty songs
        # is "1.2 GB" there and an honest "1.2 GB" here but "0.073 GB" is not
        # a number anybody reads
        content.append(
            f"  ({total} · "
            f"{downloads.format_bytes(sum(r['bytes'] for r in rows))})",
            style="dim")
        content.append("\n", style="")
        for i in range(page_start, page_end):
            row = rows[i]
            selected = (i == self._downloads_cursor)
            content.append("\n")
            content.append("  ▸ " if selected else "    ",
                           style="bold cyan" if selected else "")
            # The same `↓` the browse and queue rows carry, for the same
            # reason: every row on this screen is downloaded, and a column
            # that appears and disappears between screens is a column you
            # have to re-learn
            content.append("↓ ", style="dim cyan")
            content.append(row["title"],
                           style="bold white" if selected else "white")
            if row["artist"]:
                content.append(f"  {row['artist']}", style="dim")
            if row["tier"]:
                content.append(f"  {row['tier']}", style="dim")
            # No `~`: these are bytes on this disk, counted, not estimated
            content.append(f"  {downloads.format_bytes(row['bytes'])}",
                           style="dim cyan")
        self._page_footer(content, self._downloads_cursor, total, page)
        return content

    def _build_delete_download_confirm(self) -> Text:
        row = self._downloads_delete or {}
        content = Text()
        # Two lines, like the re-fetch prompt and unlike the cache one: the
        # title is the whole point of asking and it is the part that can be
        # any length, so it must not be what pushes `y to confirm` off the end
        content.append(f"\n   Delete {row.get('title') or 'this download'}"
                       " from your music folder?", style="bold yellow")
        content.append("\n   ", style="")
        content.append("y", style="bold")
        content.append(" to confirm, any other key to cancel", style="dim")
        return content

    def _delete_download(self) -> None:
        """Unlink the one file [x] asked about, and forget it.

        Everything destructive about this feature is these four lines, and
        all of it is `downloads.remove` — one exact path out of the index, no
        directory, nothing that was not written and recorded here. The memos
        go with it, so the count, the size and every `↓` on every other
        screen agree on the next frame rather than at the next restart.
        """
        row = self._downloads_delete
        self._downloads_delete = None
        if not row:
            return
        if downloads.remove(row["id"]):
            self._forget_downloads()
            self._set_toast(f"Deleted {row['title']}")
        else:
            self._set_toast("Could not delete that file")

    def _build_quality_gate_note(self) -> Text:
        """Why the dimmed tiers in the ladder above are dimmed — and, if the
        tier actually in effect is one of them, where the fix is. Empty
        whenever nothing is gated, which is the normal case."""
        line = Text()
        gated = [c for c in QUALITY_CHOICES if self._quality_unavailable(c)]
        if not gated:
            return line
        # The ceiling is a tidalapi value; the ladder above speaks setting
        # names, so translate before putting the two in one sentence
        ceiling = self._tier_label(self._quality_ceiling) or self._quality_ceiling
        line.append(
            f"\n   {' and '.join(gated)} — this login isn't served them; "
            f"TIDAL sends {ceiling} instead",
            style="dim yellow",
        )
        # _quality_name, not the saved value: --quality can be overriding it,
        # and what he is hearing right now is the thing worth explaining
        if self._quality_name in gated:
            line.append("   [u] fixes it", style="dim")
        return line

    def _build_pkce_line(self) -> Text:
        """Either the offer to sign in for higher quality, or — once that is
        done — the quiet confirmation that it was. Same idiom as the [o] logout
        and [x] clear-cache actions above: something the page lets you do, which
        SETTINGS_SPEC (a table of values) has no way to express."""
        line = Text()
        if self.session.is_pkce:
            line.append("\n   PKCE login ")
            line.append("✓", style="green")
            line.append("   Max quality available", style="dim")
            return line
        line.append("\n   [u]", style="bold")
        line.append(" sign in for higher quality", style="dim")
        # The offer is the line that matters; the two lines saying what the
        # trade is are prose, and go with the rest of it in a short window
        if self._fit.prose:
            line.append(
                "\n   A clunkier sign-in — you paste back the address your browser lands on —"
                "\n   and in exchange HIGH and MAX stream as real FLAC instead of AAC.",
                style="dim",
            )
        return line

    def _build_quit_confirm(self) -> Text:
        content = Text()
        content.append("\n   Quit player? ", style="bold yellow")
        content.append("Press ", style="dim")
        content.append("Esc", style="bold")
        content.append(" again to confirm, any other key to cancel", style="dim")
        return content

    def _build_logout_confirm(self) -> Text:
        content = Text()
        content.append("\n   Log out and clear saved tokens? ", style="bold yellow")
        content.append("Press ", style="dim")
        content.append("y", style="bold")
        content.append(" to confirm, any other key to cancel", style="dim")
        return content

    def _build_disable_songs_confirm(self) -> Text:
        """Disabling and clearing are separate: you can stop caching and keep
        what you already have."""
        content = Text()
        content.append("\n   Clear cached songs as well? ", style="bold yellow")
        content.append("y", style="bold")
        content.append(" clear, ", style="dim")
        content.append("n", style="bold")
        content.append(" keep them, ", style="dim")
        content.append("Esc", style="bold")
        content.append(" cancel", style="dim")
        return content

    def _build_download_line(self) -> Optional["Text"]:
        """What a download is doing, for the settings page. `None` when there
        is nothing to say, and that is the normal case — this borrows the
        `[R]` row rather than adding one, so an idle page is unchanged and
        80x24 still fits.

        The same three-states idiom `_build_refetch_line` uses, and the third
        one is the point: **a failure used to be silent** the moment the box
        was closed, which is ai/INCIDENTS #3 in miniature. A run that was
        rate-limited, or a track that could not be fetched, says so here in
        red until the next download replaces it. A clean finish says nothing
        — the toast already did, and a permanent "Saved 12 ✓" on a settings
        page is furniture.
        """
        job = self._download_job or {}
        state = job.get("state")
        if state == "blocked":
            line = Text("Download stopped — TIDAL is rate-limiting. ",
                        style="red")
            line.append(f"{job.get('done', 0)} done, nothing retried.",
                        style="red")
            return line
        if state == "failed":
            # The single-track job's own failure, in the backend's words
            line = Text(f"Download failed — {job.get('error') or 'unknown'}",
                        style="red")
            return line
        if state != "running":
            if state == "done" and job.get("failed"):
                return Text(f"Download finished — {job['failed']} failed",
                            style="red")
            return None
        # Bulk and single are one line: the counts are the run's when there
        # is a run and 1 when there is not, and the rate is the same mean the
        # monitor tick already put on the slots.
        slots = job.get("slots") or ()
        # `done/total`, the same reading as the re-fetch line right beside it:
        # how many have landed, out of how many were asked for — including
        # anything appended since, because that is what is being downloaded
        line = Text(f"Downloading {job.get('done') or 0}"
                    f"/{job.get('tracks') or 1}", style="yellow")
        rate = _format_rate(sum(self._slot_rate(s) for s in slots))
        if rate:
            line.append(f" · {rate}", style="dim")
        if job.get("failed"):
            line.append(f" · {job['failed']} failed", style="dim")
        line.append("   [x]", style="bold")
        line.append(" cancel", style="dim")
        return line

    def _build_refetch_line(self) -> Text:
        """The `[R]` action, or what it is doing right now.

        Three states read differently, the way every other long job here does:
        an offer, a running count, and how it ended. A run that stopped
        because TIDAL started rate-limiting says so in red — that is the one
        outcome the user has to understand, because the rule is that nothing
        is retried.
        """
        content = Text()
        job = self._refetch_job or {}
        state = job.get("state")
        if state == "running":
            content.append(
                f"Re-fetching at {job.get('tier', '')} — "
                f"{job.get('done', 0)}/{job.get('total', 0)}", style="yellow")
            if job.get("failed"):
                content.append(f" · {job['failed']} failed", style="dim")
            content.append("   [Esc]", style="bold")
            content.append(" stop", style="dim")
            return content
        if state == "blocked":
            content.append(
                f"Stopped — TIDAL is rate-limiting. {job.get('done', 0)}"
                " done, nothing retried.", style="red")
            return content
        content.append("[R]", style="bold")
        content.append(f" upgrade all to {self._quality_name}", style="dim")
        return content

    def _build_refetch_confirm(self) -> Text:
        """What `[R]` is about to do, before it does any of it.

        The one operation in the app that deliberately makes a large number of
        network requests, so it says how many songs and roughly how many bytes
        *first*. The estimate is the size already recorded for each copy, so
        it costs nothing — asking TIDAL for real sizes would be one request
        per track, which is the pattern that caused the incident this is
        trying not to repeat.
        """
        content = Text()
        plan = self._refetch_plan or {}
        total = len(plan.get("downloads", [])) + len(plan.get("cache", []))
        # "Nothing to upgrade" and "nothing at all" are different answers, and
        # so is "nothing this login can be served" — a ceiling below the
        # setting means [R] would spend a request per track to be handed back
        # the same file, so it says that instead of pretending there is
        # nothing to do
        target = self._upgrade_target()
        if not total:
            if target and target != self.QUALITY_MAP.get(self._quality_name):
                content.append(
                    f"\n   This login isn't served {self._quality_name} —"
                    " nothing to upgrade. ", style="bold yellow")
            else:
                content.append("\n   Nothing is below "
                               f"{self._quality_name}. ", style="bold yellow")
            if plan.get("unknown"):
                content.append(
                    f"{plan['unknown']} of unrecorded quality, left alone. ",
                    style="dim")
            content.append("Any key to close", style="dim")
            return content
        content.append(
            f"\n   Upgrade {total} song{'' if total == 1 else 's'}"
            f" to {self._quality_name}?", style="bold yellow")
        content.append(
            f" About {downloads.format_bytes(plan.get('bytes', 0))} over"
            f" {_rough_minutes(total)}, one at a time.", style="dim")
        if plan.get("skipped"):
            content.append(f" {plan['skipped']} already at this quality"
                           " or better.", style="dim")
        if plan.get("unknown"):
            content.append(f" {plan['unknown']} of unrecorded quality,"
                           " left alone.", style="dim")
        content.append("\n   ", style="")
        content.append("y", style="bold")
        content.append(" to start, any other key to cancel", style="dim")
        return content

    def _build_clear_cache_confirm(self) -> Text:
        content = Text()
        songs = self._cache.audio_count()
        content.append(
            f"\n   Delete {songs} cached song{'' if songs == 1 else 's'}? ",
            style="bold yellow")
        content.append("y", style="bold")
        content.append(" to confirm, any other key to cancel", style="dim")
        return content

    # ── The pane ──

    def _mode_hints(self) -> list:
        """The footer's hints: what the screen offers, plus the `[h]` hint.

        On the player screen that hint is `[h] hide`; on a menu it is instead
        `[h] player`, because there `h` dismisses the list and returns to the
        now-playing screen rather than hiding a footer. Two things live here
        rather than in the eight branches below, because both are true of every
        screen at once and a copy per screen is how the two of them would drift
        apart. Hidden is simply a footer with no hints in it — the same answer
        the mini player gives — so everything downstream (the blank row, the
        rows the body gets back, the crop) follows from one empty list instead
        of from a flag threaded through the layout.
        """
        if self._footer_hidden:
            return []
        hints = self._screen_hints()
        if hints and self._can_hide_footer():
            # Last, after `back`: the footer reads as what this screen does,
            # then how to put the footer away
            hints.append(Hint("h", "hide", None, HIDE_HINT_RANK))
        elif hints and self._can_exit_menu_to_player():
            # Same slot on a menu, but `h` goes home rather than hiding
            hints.append(Hint("h", "player", "player", HIDE_HINT_RANK))
        return hints

    def _screen_hints(self) -> list:
        """What this mode's footer offers, in the order it is shown.

        `short` is the same thing in fewer columns and `rank` decides what is
        dropped first when even the keys won't fit; neither has anything to do
        with the order. Every mode that is not typing carries [v], because the
        volume overlay is reachable from all of them — and the two that are
        typing (a search query, a new playlist's name) do not, because there
        `v` is the letter v.

        Every hint on every screen goes through here and out through
        _fit_hints. Nothing is appended to the footer anywhere else, which is
        what makes "a pair is never split" true of all of them at once rather
        than of the ones somebody remembered.
        """
        if self._mini_player:
            # Tiny mode is the whole point of tiny mode: one line, no footer
            return []
        if self._download_open:
            # The box carries its own keys on its own bottom line, so a footer
            # here would be a second copy of them — and the mode's real footer
            # would be advertising keys the box is swallowing.
            return []
        if self._mode == self.MODE_SEARCH:
            hints = [
                Hint("Enter/\u2192", "search/open", "open", 0),
                Hint("\u2191/\u2193", "navigate", "move", 2),
                Hint("Tab", "filter", None, 3),
            ]
            if self._search_results:
                hints.append(Hint("Space", "pause/play", "pause", 4))
            hints.append(Hint("\u2190/Esc", "back", None, 1))
            hints.append(Hint("Bksp", "delete", "del", 5))
            return hints
        if self._mode == self.MODE_BROWSE:
            hints = [
                Hint("Enter/\u2192", "play track", "play", 0),
                Hint("\u2191/\u2193", "navigate", "move", 2),
                Hint("Space", "pause/play", "pause", 4),
                Hint("a", "play all", "all", 5),
                Hint("y", "add to playlist", "add", 6),
            ]
            hints.append(Hint("d", "download", "get", 7))
            hints.append(Hint("D", "download all", "all", 9))
            if self._browse_playlist is not None:
                hints.append(Hint("x", "remove", "del", 8))
            hints.append(Hint("v", "volume", "vol", 3))
            hints.append(Hint("\u2190/Esc", "back", None, 1))
            return hints
        if self._mode == self.MODE_ARTIST:
            # [Tab] outranks nearly everything here: the four sections are the
            # page, and a footer that dropped it would leave three of them
            # unreachable with no sign they exist
            return [
                Hint("Enter/\u2192", "open", None, 0),
                Hint("\u2191/\u2193", "navigate", "move", 3),
                Hint("Tab", "section", None, 1),
                Hint("a", "play all", "all", 5),
                Hint("d", "download", "get", 6),
                Hint("D", "download all", "all", 7),
                Hint("v", "volume", "vol", 4),
                Hint("\u2190/Esc", "back", None, 2),
            ]
        if self._mode == self.MODE_QUEUE:
            return [
                Hint("Enter", "play", None, 0),
                Hint("\u2191/\u2193", "navigate", "move", 2),
                Hint("Space", "pause/play", "pause", 4),
                Hint("x", "remove", "del", 6),
                Hint("y", "add to playlist", "add", 7),
                Hint("d", "download", "get", 8),
                Hint("D", "download all", "all", 9),
                Hint("v", "volume", "vol", 3),
                Hint("\u2190/Esc", "back", None, 1),
            ]
        if self._mode == self.MODE_PLAYLISTS:
            return [
                Hint("Enter/\u2192", "open", None, 0),
                Hint("\u2191/\u2193", "navigate", "move", 2),
                Hint("Space", "pause/play", "pause", 4),
                Hint("v", "volume", "vol", 3),
                Hint("\u2190/Esc", "back", None, 1),
            ]
        if self._mode == self.MODE_ADD_TO_PLAYLIST:
            if self._picker_new_name is not None:
                # Typing: the arrows do nothing here, so the row must not
                # advertise them — and [v] is a letter of the name
                return [
                    Hint("Enter", "create", None, 0),
                    Hint("Esc", "cancel", None, 1),
                ]
            return [
                Hint("Enter", "add", None, 0),
                Hint("\u2191/\u2193", "navigate", "move", 2),
                Hint("v", "volume", "vol", 3),
                Hint("\u2190/Esc", "cancel", None, 1),
            ]
        if self._mode == self.MODE_DOWNLOADS:
            # No Enter and no [a]: this screen makes no request, and playing a
            # row would be one. [x] is "remove" here for the same reason it is
            # on the browse screen and on the settings page.
            hints = [Hint("\u2191/\u2193", "navigate", "move", 1)]
            if self._downloads():
                # Not offered over an empty folder: a key that can only
                # refuse is the "hidden option looks like a missing feature"
                # rule read the other way round
                hints.append(Hint("x", "delete", "del", 0))
            hints.append(Hint("Space", "pause/play", "pause", 4))
            hints.append(Hint("v", "volume", "vol", 3))
            hints.append(Hint("\u2190/Esc", "back", None, 2))
            return hints
        if self._mode == self.MODE_SETTINGS:
            return [
                Hint("\u2191/\u2193", "select", None, 1),
                Hint("\u2190/\u2192", "change", None, 0),
                Hint("Space", "pause/play", "pause", 4),
                Hint("v", "volume", "vol", 3),
                Hint("o", "log out", "out", 5),
                Hint("Esc", "back", None, 2),
            ]
        # The player screen, where the arrows mean two different things and the
        # footer has to say which: scrubbing is a state, so it is never left to
        # be inferred from the marker on the progress line alone
        hints = [
            Hint("space", "play/pause", "play", 0),
        ]
        if self._player_focus:
            hints += [
                Hint("\u2190/\u2192", f"seek {SEEK_STEP_SECONDS}s", "seek", 1),
                Hint("\u2193", "back", None, 1),
            ]
        else:
            hints.append(Hint("\u2190/\u2192", "prev/next", "skip", 1))
        hints += [
            Hint("s", "search", None, 2),
            Hint("v", "volume", "vol", 3),
            Hint("t", "tiny", None, 5),
            Hint("m", "more", None, 4),
        ]
        if self._show_more:
            hints += [
                Hint("\u2191", "scrub", None, 6),
                Hint("l", "like", None, 6),
                Hint("r", "radio", None, 6),
                Hint("y", "add to playlist", "add", 7),
                Hint("d", "download", "get", 6),
                Hint("q", "queue", None, 6),
                Hint("p", "playlists", "lists", 6),
                Hint("c", "settings", "config", 6),
                Hint("Esc", "quit", None, 6),
            ]
        return hints

    def _build_hints(self, fit, room=None) -> list:
        hints = self._mode_hints()
        if not hints:
            return []
        # The "more" set is asked for, so it is allowed more room than the
        # single row of hints a player screen normally spends
        rows = fit.hint_rows + (2 if self._show_more and self._mode == self.MODE_PLAYER else 0)
        if room is not None:
            rows = max(rows, room)
        laid = _fit_hints(hints, max(fit.inner - INDENT, 8), rows)
        return _clip_lines(_hints_text(laid, INDENT), fit.inner)

    def _build_volume_overlay(self, fit) -> list:
        """The [v] overlay, drawn where the hints would have been.

        Every number on it comes from the place the settings page's volume row
        used to read them from — `get_spec("volume")`, `_setting_ceiling()`
        and, for the arrows, `_adjust_setting()` — so the backend's real
        ceiling, the clamping, the live apply and the write to disk cannot
        drift from what is on screen. Moving the control did not fork it.
        """
        spec = get_spec("volume")
        value = coerce(spec, self.config.get("volume", spec["default"]))
        ceiling = self._setting_ceiling(spec)

        label, pct = "Volume ", f" {value}%"
        bar = Text(" " * INDENT)
        bar.append(label, style="bold blue")
        # Two more columns for the end caps, and never wider than the pane
        width = min(VOLUME_BAR_MAX, fit.inner - INDENT - len(label) - len(pct) - 2)
        if width >= MIN_BAR_WIDTH:
            # Filled against the ceiling actually in effect, so a full bar
            # means "as loud as this backend goes" rather than as loud as some
            # other backend would have gone
            filled = max(0, min(width, int(round(width * value / max(ceiling, 1)))))
            bar.append("▕", style="blue")
            bar.append("█" * filled, style="bold blue")
            bar.append("░" * (width - filled), style="dim blue")
            bar.append("▏", style="blue")
        bar.append(pct, style="bold blue")
        rows = [bar]

        # The two things that made the old settings row honest, kept
        notes = []
        if value >= 105:
            notes.append("louder than the master — quality suffers")
        if self.audio and ceiling < spec["max"]:
            notes.append(f"{self.audio.player_cmd} caps at {ceiling}%")
        if notes and fit.hint_rows > 1:
            rows.append(Text(" " * INDENT + "  ".join(notes), style="blue"))

        keys = [Hint("←/→", "adjust", "±", 1), Hint("Enter/Esc", "close", "ok", 0)]
        rows.append(_hints_text(_fit_hints(keys, max(fit.inner - INDENT, 8), 1), INDENT))
        out = []
        for row in rows:
            out.extend(_clip_lines(row, fit.inner))
        return out

    def _build_download_overlay(self, fit) -> list:
        """The [d] box: four lines, centred over the screen it was opened on.

        A download is a confirmation, not a destination. This replaced a
        whole mode — a column of four tiers, an action on the hovered row, a
        size column and four lines of prose about how good each estimate
        was. Every line of that was defensible and collectively it was the
        thing the owner called janky. What is left is the track, the number,
        the tier and the button.

        The honesty affordances survive as *marks*, not sentences: a tier
        this login is not served is dimmed and labelled `unavailable`, one
        already on this disk is ticked, and a real byte count is printed
        without the `~` every estimate carries (`_download_facts`).

        A whole playlist is **the same box** with a count on the size line
        and, once it is running, up to three bars where the button was. The
        bars are the first thing a short window gives up — three of them,
        then two, then one, then the summary line alone, which is already the
        count and the button. A box that overflowed would be answered by Rich
        replacing its bottom row with a red ellipsis, and the bottom row is
        the one that says how to stop.
        """
        # Two columns of border and one of padding on each side
        avail = max(fit.inner - 4, 10)
        job = self._download_job_here()
        # The button goes through the same _fit_hints every footer does, so a
        # narrow window makes it terser rather than cutting it in half. A
        # clipped `[Es…` is a key nobody can read.
        action = (self._download_status_row(job, avail) if job.get("state")
                  else _hints_text(_fit_hints(DOWNLOAD_BUTTONS, avail, 1), 0))
        head, size, pick = self._download_box_rows()
        # What a pane too short for the whole box gives up, and in this
        # order: the title only repeats what you pressed [d] on, and the tier
        # is one arrow away. The number and the button are the box.
        rows = [(2, head), (0, size), (1, pick)]
        rows += [(3 + i, bar) for i, bar
                 in enumerate(self._download_bar_rows(job, avail))]
        rows.append((0, action))
        for priority in (5, 4, 3, 2, 1):
            if len(rows) + 2 <= max(fit.rows, 3):
                break
            rows = [row for row in rows if row[0] != priority]
        rows = [row for _, row in rows]
        width = min(max(cell_len(row.plain) for row in rows), avail)
        return _boxed(rows, width, max((fit.inner - width - 4) // 2, 0), "magenta")

    def _download_box_rows(self) -> list:
        """The three lines above the button: what, how big, at which tier.

        **A live run outranks the selection.** Once something is downloading,
        those two lines describe the run — its labels, its count, its summed
        estimate — because that is what is actually happening, and a box
        still reading "6 tracks" while eighteen are in the run would be
        wrong. What you are about to add moves onto the tier line as
        `+ N to add`, which is also where "all of them are already queued"
        can be read before pressing anything.
        """
        track = self._download_track
        tier = self._download_tier()
        bulk = self._download_bulk()
        running = self._download_job or {}
        if running.get("state") != "running" or not running.get("bulk"):
            running = {}
        # Deliberately not asked for in bulk: it is two index reads about the
        # *first* track, and "✓ on disk" under a count of thirteen would be a
        # claim about twelve songs nobody checked
        sizes, owned = ({}, None) if bulk or running else self._download_facts()

        if running:
            labels = [label for label in (running.get("labels") or ()) if label]
            head = Text(labels[0] if labels else "Downloading",
                        style="bold white")
            if len(labels) > 1:
                head.append(f"  +{len(labels) - 1} more", style="dim")
        elif bulk:
            head = Text(self._download_label or "These tracks",
                        style="bold white")
        else:
            head = Text(str(getattr(track, "name", None) or "Unknown track"),
                        style="bold white")
            artist = ", ".join(a.name for a in (getattr(track, "artists", None) or [])
                               if getattr(a, "name", None))
            if artist:
                head.append(" — " + artist, style="dim")

        # The line he asked to be able to read at a glance. White, alone, and
        # exact whenever the bytes are already known here for free.
        if running or bulk:
            count = ((running.get("tracks") or 0) if running
                     else len(self._download_tracks))
            total = ((running.get("estimate") or 0) if running
                     else self._download_bulk_estimate(tier))
            size = Text(f"{count} tracks", style="bold white")
            size.append("   ~" + downloads.format_bytes(total) if total > 0
                        else "   —", style="bold white")
        else:
            size = Text(self._download_size(tier), style="bold white")

        pick = Text("↑↓  ", style="dim")
        gated = self._quality_unavailable(tier)
        pick.append(tier, style="dim" if gated else "bold cyan")
        if gated:
            pick.append("  unavailable", style="yellow")
        elif running:
            fresh = self._download_unqueued()
            if fresh:
                pick.append(f"   + {fresh} to add", style="cyan")
            else:
                pick.append("   already queued", style="dim")
        elif owned is not None and tier in sizes:
            pick.append("  ✓ on disk", style="green")
        return [head, size, pick]

    def _download_unqueued(self) -> int:
        """How many of the tracks under the cursor the live run has not been
        asked for yet. Set arithmetic over ids — no disk, no index, no
        network, so a repaint can pay for it twice a second."""
        queued = self._download_queued_ids()
        if not queued:
            return len(self._download_tracks)
        return len({getattr(t, "id", None) for t in self._download_tracks}
                   - queued)

    def _download_bar_rows(self, job: dict, avail: int) -> list:
        """One line per fetch that is under way, in the style the owner picked.

            ▸ Nightcall           ━━━━━━━━━━━╸┈┈┈┈┈┈┈┈┈┈  12.4 MB  5.2MB/s

        Nothing here reads the disk or the index: every number is on the slot
        the worker is writing, and the rate is the mean the monitor tick
        already computed. A repaint is twice a second.
        """
        if not job.get("bulk") or job.get("state") != "running":
            return []
        namew, barw = self._download_bar_columns(avail)
        rows = []
        for slot in job.get("slots") or ():
            state = slot.get("state")
            if state not in ("running", "done"):
                continue
            title = str(slot.get("title") or "")[:namew].ljust(namew)
            row = Text()
            if state == "done":
                # Collapsed: a tick, the name, and the size that is really on
                # the disk — no `~`, because this one was counted
                row.append("✓ ", style="bold green")
                row.append(title, style="dim")
                row.append(" ")
                row.append(BAR_FULL * barw, style="green")
                row.append(f"  {downloads.format_bytes(slot.get('size') or 0):>9}",
                           style="green")
                row.append(" " * 10)
            else:
                done = slot.get("done") or 0
                total = slot.get("total") or slot.get("estimate") or 0
                filled, rest = _bar_split(done / total if total else 0.0, barw)
                row.append("▸ ", style="bold cyan")
                row.append(title)
                row.append(" ")
                row.append(filled, style="cyan")
                row.append(rest, style="dim")
                row.append(f"  {downloads.format_bytes(done):>9}", style="white")
                row.append(f"  {_format_rate(self._slot_rate(slot)):<8}",
                           style="dim")
            rows.append(row)
        return rows

    @staticmethod
    def _download_bar_columns(avail: int) -> tuple:
        """`(name width, bar width)` for a box this wide.

        Every bar row is padded to exactly `name + bar + 24`, so `_boxed` —
        which centres each line inside the border — centres the block as a
        block and the columns line up. A ragged ✓ row sitting a few columns
        off the bars above it was the first thing that looked wrong.
        """
        namew = max(6, min(20, avail - 44))
        return namew, max(6, avail - namew - 26)

    def _download_job_here(self) -> dict:
        """The one job, if it happens to be about what the box is showing.
        A job for anything else is finished business and says nothing about
        this track."""
        job = self._download_job or {}
        if job.get("bulk"):
            # A run that is *going* is shown on every box, because the box
            # describes the run while there is one — otherwise a single-track
            # box would hide the bars and the only [x] that stops them. A
            # finished one is history and belongs to the bulk box alone.
            if job.get("state") == "running":
                return job
            return job if self._download_bulk() else {}
        if job.get("track_id") != getattr(self._download_track, "id", None):
            return {}
        return job

    def _download_status_row(self, job: dict, avail: int) -> Text:
        """What the job is doing, in the button's place — it is the only thing
        on the box that changes. Running, done and failed read differently,
        in one line each."""
        row = Text()
        state = job.get("state")
        if job.get("bulk"):
            return self._download_bulk_status_row(job, avail)
        if state == "running":
            done, total = job.get("done") or 0, job.get("total") or 0
            row.append(downloads.format_bytes(done), style="yellow")
            if total:
                row.append(f"  {done * 100 // total}%", style="yellow")
            row.append("   [x]", style="bold")
            row.append(" cancel", style="dim")
        elif state == "done":
            row.append("Saved ✓", style="green")
            # A tier that had to step down says which rung it stopped on.
            # "Saved ✓" under a row reading `MAX unavailable` was the one
            # genuinely dishonest thing the box could show: the file is real,
            # but it is not the tier the line above it names.
            landed = job.get("landed")
            if landed and landed != job.get("tier"):
                row.append(f" {landed}", style="cyan")
            # Reported from what was written, never from intent
            if not job.get("tags"):
                row.append(" untagged", style="dim")
            row.append("   [Esc]", style="bold")
            row.append(" close", style="dim")
        elif state == "failed":
            # In the backend's own words: a download that stopped without
            # saying why is INCIDENTS #3 in miniature
            row.append(f"Failed — {job.get('error')}", style="red")
            row.append("   [Enter]", style="bold")
            row.append(" retry", style="dim")
        return row

    def _download_bulk_status_row(self, job: dict, avail: int) -> Text:
        """The line under the bars: what has landed, what is left, how fast,
        and how to stop.

        It is also what the box degrades *to*. With every bar dropped this
        line alone still answers "is it working and how much is left", which
        is the whole question — so it is the one row that must never be
        clipped, and it gives up its own pieces (the rate, then the byte
        count) rather than let Rich take `[x] cancel` off the end.
        """
        slots = job.get("slots") or ()
        state = job.get("state")
        done, failed = job.get("done") or 0, job.get("failed") or 0
        total = job.get("tracks") or 0
        if state != "running":
            row = Text()
            if state == "blocked":
                row.append(f"Stopped — {job.get('error')}", style="red")
                row.append("   nothing retried", style="dim")
            elif state == "cancelled":
                row.append(f"Cancelled after {done}", style="yellow")
            else:
                row.append(f"Saved {done} ✓", style="green")
            if failed:
                row.append(f"  {failed} failed", style="red")
            row.append("   [Esc]", style="bold")
            row.append(" close", style="dim")
            return row

        fetched = sum((s.get("banked") or 0) + (s.get("done") or 0) for s in slots)
        rate = _format_rate(sum(self._slot_rate(s) for s in slots))
        pieces = [(downloads.format_bytes(fetched) + " fetched", "white"),
                  (f"{max(total - done - failed, 0)} to go", "dim")]
        if rate:
            pieces.append((rate, "white"))
        cancel = ("[x] cancel", "bold")
        # Widest arrangement that fits, and the last one always does
        for parts, gap in ((pieces + [cancel], "   ·   "),
                           (pieces + [cancel], "  "),
                           (pieces[:2] + [cancel], "  "),
                           ([pieces[1], cancel], "  ")):
            row = Text()
            for index, (text, style) in enumerate(parts):
                if index:
                    row.append(gap, style="dim")
                row.append(text, style=style)
            if cell_len(row.plain) <= avail:
                break
        namew, barw = self._download_bar_columns(avail)
        pad = namew + barw + 24 - cell_len(row.plain)
        if 0 < pad:
            row.append(" " * pad)
        return row

    def _compose(self, fit) -> tuple:
        """The pane under one fit: the lines above the footer, and the footer.

        Both are already clipped to the panel's inner width, so a line is
        exactly a row and _build_display can count them. The body is built
        first because the footer is allowed the rows the body did not use —
        `[m]` at 40 columns is thirteen hints that fit no line, and a window
        with ten spare rows should spend them on labels rather than show a
        row of bare keys.
        """
        content = Text()
        content.append_text(self._build_player_display())
        if time.time() < self._toast_until:
            content.append(f"\n   {self._toast}", style="bold green")

        if fit.mini:
            if self._quit_pending:
                content.append_text(self._build_quit_confirm())
            return self._with_footer(content, fit)

        if self._mode != self.MODE_PLAYER:
            if fit.chrome:
                content.append("\n\n")
                content.append("  " + "─" * max(fit.inner - 4, 4), style="dim")
                content.append("\n\n")
            else:
                # The separator's three rows say nothing a short window can
                # afford: the list's own header row (the search query and
                # scope, the artist tabs) is what they would cost
                content.append("\n")
            if self._mode == self.MODE_SEARCH:
                content.append_text(self._build_search_display())
            elif self._mode == self.MODE_BROWSE:
                content.append_text(self._build_browse_display())
            elif self._mode == self.MODE_ARTIST:
                content.append_text(self._build_artist_display())
            elif self._mode == self.MODE_QUEUE:
                content.append_text(self._build_queue_display())
            elif self._mode == self.MODE_PLAYLISTS:
                content.append_text(self._build_playlists_display())
            elif self._mode == self.MODE_ADD_TO_PLAYLIST:
                content.append_text(self._build_add_to_playlist_display())
            elif self._mode == self.MODE_SETTINGS:
                content.append_text(self._build_settings_display())
            elif self._mode == self.MODE_DOWNLOADS:
                content.append_text(self._build_downloads_display())

        if self._quit_pending:
            content.append_text(self._build_quit_confirm())
        elif self._logout_pending:
            content.append_text(self._build_logout_confirm())
        elif self._disable_songs_pending:
            content.append_text(self._build_disable_songs_confirm())
        elif self._clear_cache_pending:
            content.append_text(self._build_clear_cache_confirm())
        elif self._refetch_pending:
            content.append_text(self._build_refetch_confirm())
        elif self._downloads_delete is not None:
            content.append_text(self._build_delete_download_confirm())

        return self._with_footer(content, fit)

    def _with_footer(self, content: "Text", fit) -> tuple:
        """Body lines plus the footer that goes under them, blank row and all.

        The footer's row allowance is whatever the body left, floored at the
        fit's own `hint_rows` — the floor is what makes a body that already
        overflows relax instead of squeezing the footer to nothing, and the
        allowance is what lets a short body pay for full labels.
        """
        body = _clip_lines(content, fit.inner)
        room = max(fit.hint_rows, fit.rows - len(body) - 1)
        footer = (self._build_volume_overlay(fit) if self._volume_open
                  else self._build_hints(fit, room))
        if footer:
            # One blank row between the pane and its footer
            body = body + [Text("")]
        return body, footer

    def _build_display(self) -> Panel:
        """The pane, laid out for the window it is actually in.

        Composed more than once on purpose. Every line comes back clipped to
        the inner width, so the height is countable; if it overflows the
        terminal the fit gives ground — in this screen's own order — and it is
        composed again. Only when there is nothing left to give does the pane
        get cropped, and it is cropped from the bottom of the *content* rather
        than by Rich, which would take the last line — and the last line is
        the one that says how to get out.
        """
        width, height = self._console_size()
        mini = self._mini_player
        levers = self._fit_levers()
        fit = _Fit(
            inner=max(width - (MINI_PANEL_CHROME if mini else PANEL_CHROME),
                      MIN_INNER_WIDTH),
            rows=max(height - (MINI_PANEL_ROWS if mini else PANEL_ROWS), 1),
            # The settings page windows over its own rows, not the list setting
            page_rows=(len(SETTINGS_ROWS) if self._mode == self.MODE_SETTINGS
                       else self._page_size),
            # Two rows of hints, until the window is short enough that the
            # rows are worth more to the music than to the footer
            hint_rows=2 if height >= 16 else 1,
            levers=levers,
            mini=mini,
        )
        self._fit = fit
        body, footer = self._compose(fit)
        # One pass per lever this screen has, plus the one that finds out
        # nothing is left
        for _ in range(len(levers) + 1):
            over = len(body) + len(footer) - fit.rows
            if over <= 0 or not fit.relax(over):
                break
            body, footer = self._compose(fit)

        if len(body) + len(footer) > fit.rows:
            # The blank row between the pane and its footer says nothing, so
            # it goes before anything that does
            if body and not body[-1].plain.strip():
                body = body[:-1]
        if len(body) + len(footer) > fit.rows:
            # Identity before instructions, but only where they are actually
            # in competition. The body is cut from the bottom, so down to
            # IDENTITY_ROWS what it loses is its tail and the footer is worth
            # more than another line of it — that is the old order and it
            # stands. Past that the footer would be eating the song: which
            # key searches is worth less, at any size, than knowing what is
            # playing, and it is worth nothing at all on a pane with room for
            # one line. So the last thing on screen is the track, not the
            # controls.
            keep = min(len(body), max(IDENTITY_ROWS, fit.rows - len(footer)))
            footer = footer[:max(fit.rows - keep, 0)]
            body = body[:max(fit.rows - len(footer), 1)]
        lines = body + footer
        if self._download_open:
            lines = self._overlay(lines, self._build_download_overlay(fit), fit)

        content = Text("\n").join(lines)
        content.no_wrap = True
        return Panel(
            content,
            title="[bold cyan]Ticli[/bold cyan]",
            border_style="cyan",
            padding=(0, 1) if mini else (1, 2),
        )

    def _overlay(self, lines: list, box: list, fit) -> list:
        """`box` drawn over the middle of `lines`, replacing rows rather than
        pushing them down — a pane that grew when a box opened would move the
        screen underneath it, which is the opposite of an overlay.

        The panel is only as tall as its content, so a short screen (the
        player alone, the tiny player) has to be padded out to hold the box.
        Never past the terminal: a box that would not fit is cropped, the
        same as everything else here.
        """
        box = box[:fit.rows]
        rows = list(lines)
        if len(rows) < len(box):
            rows += [Text("")] * (min(len(box), fit.rows) - len(rows))
        top = max((len(rows) - len(box)) // 2, 0)
        for index, row in enumerate(box):
            if top + index < len(rows):
                rows[top + index] = row
        return rows

    def _fit_levers(self) -> tuple:
        """What this screen gives up, cheapest first, when it doesn't fit.

        The order is per screen because the cheapest thing to lose is not the
        same on all of them. On the player screen it is the cover — the one
        thing on the pane that says nothing. On a list it is rows, which ↑/↓
        reaches anyway. On the settings page it is the *prose*, not the rows:
        the rows are seven and the prose is eleven lines, so shrinking the
        table first bought four rows and cost six of the seven settings — a
        page showing one row out of seven at 80x24, which is what the first
        attempt at this actually did. The footer is last everywhere, and only
        ever from two rows to one.

        Nothing here can relax the artist page's tab row or the search screen's
        scope row, and that is deliberate rather than an omission: both are
        state that would otherwise be hidden — which section you are in, which
        scope Tab landed on — so they are drawn at every height and it is the
        rows under them that give way. (The scope row still gets *narrower*
        with the pane; that is width, and it is decided where it is drawn.)
        `chrome` is what keeps that promise once the page is down to one row:
        without it the crop in _build_display, which cuts the body from the
        bottom, reached those header rows while the separator's three blank
        rows and the queue/quality line — rows that say nothing — sat safe
        above them. The bug that lever exists for was the search scope row
        vanishing from a 13-row terminal that still had four rows of
        decoration on screen.
        """
        if self._mini_player:
            return ()
        if self._mode == self.MODE_PLAYER:
            return ("artwork", "hint_rows")
        if self._mode == self.MODE_SETTINGS:
            return ("prose", "page_rows", "hint_rows")
        return ("page_rows", "chrome", "hint_rows")

    def _console_size(self):
        try:
            width, height = self.console.size
        except Exception:
            # A console that can't be measured is not a reason to stop drawing
            return 80, 24
        return max(int(width), 1), max(int(height), 1)

    # ── Actions ──

    def _push_nav(self):
        if self._mode == self.MODE_SEARCH:
            self._nav_history.append({
                "mode": self.MODE_SEARCH,
                # The rows, the pool and the paging depth are all still in
                # _search_views and the reservoir behind it, so coming back
                # from an album needs nothing but the query and the scope it
                # was in — and asks TIDAL for nothing at all
                "query": self._search_query,
                "filter": self._search_filter,
                "cursor": self._search_cursor,
            })
        elif self._mode == self.MODE_BROWSE:
            self._nav_history.append({
                "mode": self.MODE_BROWSE,
                "title": self._browse_title,
                "tracks": list(self._browse_tracks),
                "cursor": self._browse_cursor,
            })
        elif self._mode == self.MODE_ARTIST:
            self._nav_history.append({
                "mode": self.MODE_ARTIST,
                "artist": self._artist,
                "section": self._artist_section,
                "cursor": self._artist_cursor,
            })
        elif self._mode == self.MODE_QUEUE:
            self._nav_history.append({
                "mode": self.MODE_QUEUE,
                "cursor": self._queue_cursor,
            })
        elif self._mode == self.MODE_PLAYLISTS:
            self._nav_history.append({
                "mode": self.MODE_PLAYLISTS,
                "cursor": self._playlists_cursor,
            })
        elif self._mode == self.MODE_SETTINGS:
            # Only reachable since the downloads list, which is the one screen
            # you can open *from* settings — ← out of it has to land back on
            # the row you pressed [d] on rather than on the player
            self._nav_history.append({
                "mode": self.MODE_SETTINGS,
                "cursor": self._settings_cursor,
            })
        else:
            self._nav_history.append({"mode": self.MODE_PLAYER})

    def _go_back(self):
        if not self._nav_history:
            self._mode = self.MODE_PLAYER
            return
        state = self._nav_history.pop()
        mode = state["mode"]
        if mode == self.MODE_SEARCH:
            self._mode = self.MODE_SEARCH
            self._search_query = state.get("query", "")
            self._search_filter = state.get("filter", "all")
            if self._search_filter in self._search_views:
                self._search_cursor = state.get("cursor", 0)
            # The generation is deliberately left alone: a page still in flight
            # is for this same query, and the scope that asked for it is still
            # waiting for it here.
        elif mode == self.MODE_BROWSE:
            self._mode = self.MODE_BROWSE
            self._browse_title = state.get("title", "")
            self._browse_tracks = state.get("tracks", [])
            self._browse_cursor = state.get("cursor", 0)
            self._browse_loading = False
            self._browse_message = ""
        elif mode == self.MODE_ARTIST:
            # The sections are still in _artist_sections, so coming back lands
            # on the same tab with the same rows and makes no request at all
            self._mode = self.MODE_ARTIST
            self._artist = state.get("artist")
            self._artist_section = state.get("section", self.ARTIST_SECTIONS[0])
            self._artist_cursor = state.get("cursor", 0)
            self._load_artist_section()
        elif mode == self.MODE_QUEUE:
            self._mode = self.MODE_QUEUE
            # Clamped: the queue can have been replaced (radio) or shortened
            # while we were away, and a cursor past the end pages the screen
            # to rows that no longer exist
            self._queue_cursor = min(state.get("cursor", 0), max(len(self._queue) - 1, 0))
        elif mode == self.MODE_PLAYLISTS:
            self._mode = self.MODE_PLAYLISTS
            self._playlists_cursor = state.get("cursor", 0)
        elif mode == self.MODE_SETTINGS:
            self._mode = self.MODE_SETTINGS
            self._settings_cursor = min(state.get("cursor", 0),
                                        len(SETTINGS_ROWS) - 1)
        else:
            self._mode = self.MODE_PLAYER

    def _add_to_history(self, query: str):
        """Add a search query to history (deduped, newest first)."""
        q = query.strip()
        if not q:
            return
        # Remove if already present, then prepend
        self._search_history = [h for h in self._search_history if h.lower() != q.lower()]
        self._search_history.insert(0, q)
        self._search_history = self._search_history[:200]

    def _history_rows(self) -> list:
        """The recent searches the blank pane belongs to right now — empty
        whenever anything else has a claim on that pane (a query being typed,
        rows, a message, a fetch in flight). The keys and the display both ask
        this one question, so the cursor can only ever land on a row that
        exists; which page of these rows is on screen is the display's
        business, the same as for results."""
        if (self._search_query or self._search_results
                or self._search_loading or self._search_message):
            return []
        return self._search_history

    @staticmethod
    def _search_split(page: int) -> tuple:
        """How many tracks / albums / artists / playlists make up one page of
        results.

        45/25/15/15, scaled to "Songs per page" rather than hardcoded — one
        page of search is one page of rows, like every other list. Tracks keep
        the plurality because the common case is looking for a song; the
        fourth category came out of albums and artists (was 50/30/20) rather
        than out of tracks. No category ever rounds away to nothing.
        """
        # Floor division, so the rounding always falls to tracks
        albums = max(1, page * 25 // 100)
        artists = max(1, page * 15 // 100)
        playlists = max(1, page * 15 // 100)
        return max(1, page - albums - artists - playlists), albums, artists, playlists

    def _search_kinds(self, scope: Optional[str] = None) -> tuple:
        """Which of the reservoir's categories a scope shows."""
        return self.SEARCH_FILTER_KINDS.get(scope or self._search_filter, ())

    @staticmethod
    def _search_models() -> list:
        """What a fetch asks TIDAL for — always all four categories, whatever
        scope asked for it.

        `limit` is applied per type (session.search() puts the model names in
        one `types=` parameter of one GET), so this is the same single request
        either way; the payload is bigger and every other scope is then
        answered out of it for free. That is the whole trade: one request buys
        the query, not the scope, so Tab can apply immediately without costing
        anything.
        """
        return [tidalapi.Track, tidalapi.Album, tidalapi.Artist, tidalapi.Playlist]

    def _search_row(self, kind: str, obj) -> dict:
        """One result row. Everything downstream reads these, not the objects."""
        if kind == "tracks":
            artist = obj.artists[0].name if obj.artists else ""
            return {"type": "track", "name": obj.name, "artist": artist, "obj": obj}
        if kind == "albums":
            artist = obj.artist.name if obj.artist else ""
            return {"type": "album", "name": obj.name, "artist": artist, "obj": obj}
        if kind == "playlists":
            # Whose playlist it is, and failing that how big it is. A curated
            # playlist often has no creator name, and "Chill Vibes" on its own
            # says nothing about which of the twelve of them this is.
            creator = getattr(getattr(obj, "creator", None), "name", "") or ""
            count = getattr(obj, "num_tracks", 0) or 0
            detail = creator or (f"{count} tracks" if count > 0 else "")
            return {"type": "playlist", "name": obj.name, "artist": detail, "obj": obj}
        return {"type": "artist", "name": obj.name, "artist": "", "obj": obj}

    # ── The per-scope views over one query's reservoir ──

    def _search_view(self, scope: Optional[str] = None) -> dict:
        """The view for a scope, or an empty one if it has never been shown.
        Presence in `_search_views` — not any flag inside the record — is the
        answer to "has this scope been applied to this query", exactly as
        `_artist_sections` answers it for the artist page's tabs."""
        return self._search_views.get(scope or self._search_filter) or _empty_search_view()

    def _scope_answered(self, scope: str) -> bool:
        """Whether this scope already has its rows for this query — the thing
        the mark on the scope row means, asked in one place because the row is
        drawn two ways (all six scopes, or the active one alone when the pane
        is too narrow for six) and both must mark it the same."""
        view = self._search_views.get(scope)
        return bool(view) and not view["loading"]

    def _put_search_view(self, scope: str, **changes):
        """Replace a view whole. Both dicts are assigned, never mutated, so a
        paint racing a fetch reads one complete record or the other."""
        self._search_views = {
            **self._search_views, scope: {**self._search_view(scope), **changes}}

    @property
    def _search_results(self) -> list:
        return self._search_view()["results"]

    @_search_results.setter
    def _search_results(self, value: list):
        self._put_search_view(self._search_filter, results=list(value))

    @property
    def _search_message(self) -> str:
        return self._search_view()["message"]

    @property
    def _search_loading(self) -> bool:
        """The scope on screen is waiting for rows — its first page or its
        next one; the display tells those apart by whether it has any yet."""
        return self._search_view()["loading"]

    @property
    def _search_cursor(self) -> int:
        return self._search_view()["cursor"]

    @_search_cursor.setter
    def _search_cursor(self, value: int):
        self._put_search_view(self._search_filter, cursor=value)

    @property
    def _search_offset(self) -> int:
        """How deep into the query the reservoir has gone — one number for
        every scope, because one fetch deepens all of them."""
        return self._search_reservoir["offset"]

    @_search_offset.setter
    def _search_offset(self, value: int):
        self._search_reservoir = {**self._search_reservoir, "offset": value}

    @property
    def _search_pool(self) -> dict:
        """What the reservoir still holds that the scope on screen has not
        shown. Only that scope's categories: a page of albums nobody asked for
        is not "more results" under Tracks."""
        consumed = self._search_view()["consumed"]
        pool = _empty_search_pool()
        for kind in self._search_kinds():
            pool[kind] = self._search_reservoir[kind][consumed.get(kind, 0):]
        return pool

    @property
    def _search_done(self) -> bool:
        return self._search_scope_done(self._search_filter)

    @_search_done.setter
    def _search_done(self, value: bool):
        self._search_reservoir = {**self._search_reservoir, "stopped": bool(value)}

    def _search_scope_done(self, scope: str) -> bool:
        """Nothing more will ever come for this scope: the query failed, TIDAL
        has nothing past 300, or every category it shows has run short. An
        empty tuple of categories (My Playlists) is done by construction."""
        reservoir = self._search_reservoir
        if reservoir["stopped"] or reservoir["offset"] >= SEARCH_MAX_OFFSET:
            return True
        return all(reservoir["exhausted"][kind] for kind in self._search_kinds(scope))

    def _take_search_page(self, scope: str, consumed: dict, page: int) -> tuple:
        """One page of rows for a scope, out of the reservoir at `consumed`,
        and the depth that leaves behind.

        Under a type filter the page is all of that type; under "All" it is the
        45/25/15/15 split, still exactly `page` rows. Reads the reservoir, never
        touches it — two scopes can be at two depths in the same rows.
        """
        reservoir = self._search_reservoir
        kinds = ("tracks", "albums", "artists", "playlists")
        avail = {kind: len(reservoir[kind]) - consumed.get(kind, 0) for kind in kinds}
        if scope == "all":
            n_tracks, n_albums, n_artists, n_playlists = self._search_split(page)
            # A query with one album shouldn't waste the other album rows —
            # hand the shortfall to tracks, which is what you searched for
            n_albums = min(n_albums, avail["albums"])
            n_artists = min(n_artists, avail["artists"])
            n_playlists = min(n_playlists, avail["playlists"])
            n_tracks = min(page - n_albums - n_artists - n_playlists, avail["tracks"])
            counts = {"tracks": n_tracks, "albums": n_albums,
                      "artists": n_artists, "playlists": n_playlists}
        else:
            counts = {kind: page for kind in self._search_kinds(scope)}

        items = []
        rest = dict(consumed)
        for kind in kinds:
            start = consumed.get(kind, 0)
            take = min(counts.get(kind, 0), avail[kind])
            items.extend(self._search_row(kind, obj)
                         for obj in reservoir[kind][start:start + take])
            rest[kind] = start + take
        return items, rest

    @staticmethod
    def _pool_size(pool: dict) -> int:
        return sum(len(v) for v in pool.values())

    def _search_servable(self, scope: str) -> bool:
        """Can the rows already in hand answer this scope's next page?

        A short page — or none at all — only counts once the query has nothing
        left to give: otherwise the first Tab onto a thin category would settle
        for four rows. But once it *is* done, an empty category is the answer,
        and asking again would only fetch a page TIDAL has already said it does
        not have.
        """
        consumed = self._search_view(scope)["consumed"]
        held = sum(len(self._search_reservoir[kind]) - consumed.get(kind, 0)
                   for kind in self._search_kinds(scope))
        if held >= self._page_size:
            return True
        return self._search_scope_done(scope) and self._search_reservoir["offset"] > 0

    def _fill_search_view(self, scope: str):
        """Append one page to a scope out of the reservoir, and say so when
        there was nothing there to append."""
        view = self._search_view(scope)
        items, consumed = self._take_search_page(scope, view["consumed"], self._page_size)
        self._put_search_view(
            scope, results=view["results"] + items, consumed=consumed, loading=False,
            message="" if (view["results"] or items) else "No results found")

    def _fill_waiting_search_views(self):
        """A page landed: hand it to every scope that was waiting for one.
        Not just the scope on screen — a Tab while the request was out left
        others loading, and they are answered by the same rows."""
        for scope in self.SEARCH_FILTERS:
            if scope in self._search_views and self._search_views[scope]["loading"]:
                self._fill_search_view(scope)

    def _fail_waiting_search_views(self, message: str):
        """The request the waiting scopes were waiting for did not come back.
        One that already has rows keeps them and simply stops paging."""
        for scope in self.SEARCH_FILTERS:
            view = self._search_views.get(scope)
            if view is None or not view["loading"]:
                continue
            self._put_search_view(
                scope, loading=False, message="" if view["results"] else message)

    def _reset_search_results(self):
        """Forget the query and everything filed under it — every scope's view
        and the reservoir they all draw on. Bumping the generation is what
        makes a fetch already in flight drop its page instead of pouring it
        into a reservoir that no longer belongs to that query."""
        self._search_gen += 1
        self._search_key = ""
        self._search_views = {}
        self._search_reservoir = _empty_search_reservoir()
        self._search_fetching = False

    def _cycle_search_filter(self, step: int = 1):
        """Tab through the scopes, applying each one as you land on it — no
        second keystroke. It is not the fan-out it looks like: a fetch asks for
        all three categories at once, so the scopes share one reservoir and
        cycling every one of them after a search costs nothing at all. The
        cursor lives in the view, so coming back comes back to where you were.
        """
        order = self.SEARCH_FILTERS
        self._search_filter = order[(order.index(self._search_filter) + step) % len(order)]
        self._apply_search_scope()

    def _do_search(self):
        """Enter: the explicit search, and the explicit refresh. Everything
        filed under this query goes — every scope's rows, not just the one on
        screen — so Enter is how you get past the session's cache."""
        query = self._search_query.strip()
        if not query:
            return
        self._add_to_history(query)
        self._reset_search_results()
        self._search_key = query
        self._apply_search_scope()

    def _apply_search_scope(self):
        """Show the scope on screen, and ask TIDAL for it only if nothing
        already in hand can answer it.

        The three ways out without a request are the point: a scope visited
        before, a scope the reservoir can fill, and a scope whose page is
        already on its way — that last one is what makes a held-down Tab one
        request rather than five.
        """
        query = self._search_query.strip()
        if not query:
            return
        scope = self._search_filter
        if query != self._search_key:
            # Tabbed onto a query nothing is filed under (typing never
            # searches): start the query here, in this scope
            self._reset_search_results()
            self._search_key = query
        elif scope in self._search_views:
            # Visited already, loading or ready — either way there is nothing
            # to do. Only a finished one counts as free: a scope still waiting
            # on the request it started must not claim it cost nothing.
            if not self._search_views[scope]["loading"]:
                self._put_search_view(scope, cached=True)
            return
        if scope == "playlists":
            self._search_own_playlists(query)
            return
        if self._search_reservoir["message"]:
            self._put_search_view(scope, loading=False,
                                  message=self._search_reservoir["message"])
            return
        if self._search_servable(scope):
            self._put_search_view(scope, cached=True)
            self._fill_search_view(scope)
            return
        self._put_search_view(scope, loading=True, message="", cached=False)
        if self._search_fetching:
            return  # the page already in flight will fill this view too
        self._search_fetching = True
        self._fetch_search_page(query, self._search_gen)

    def _fetch_search_page(self, query: str, gen: int):
        """One page of results from TIDAL, on a daemon thread.

        It fills the reservoir, not a scope: whatever comes back belongs to the
        query, and every scope waiting on it is served out of the same rows.

        Snapshot the page size now: changing the setting mid-flight retunes the
        next search, it never refetches this one.
        """
        page = self._page_size
        offset = self._search_reservoir["offset"]
        models = self._search_models()
        self._search_last_fetch = time.monotonic()

        def _run():
            try:
                # TIDAL applies `limit` per type, so a single request at the page
                # size covers every category — including the slack we need when
                # one category comes up short.
                results = self.session.search(query, models=models, limit=page, offset=offset)
                if gen != self._search_gen:
                    return  # the query moved on while we were out
                reservoir = self._search_reservoir
                found = {kind: list(results.get(kind) or [])
                         for kind in ("tracks", "albums", "artists", "playlists")}
                # A category TIDAL couldn't fill has nothing more behind it, and
                # there is never more than 300 items behind a query anyway
                self._search_reservoir = {
                    **reservoir,
                    **{kind: reservoir[kind] + rows for kind, rows in found.items()},
                    "offset": offset + page,
                    "exhausted": {kind: reservoir["exhausted"][kind] or len(rows) < page
                                  for kind, rows in found.items()},
                }
                self._fill_waiting_search_views()
            except Exception as e:
                if gen == self._search_gen:
                    message = f"Search failed: {e}"
                    self._search_reservoir = {
                        **self._search_reservoir, "stopped": True, "message": message}
                    self._fail_waiting_search_views(message)
            finally:
                # Only while this is still the query being searched: a fetch
                # that has been superseded must not open the gate on the one
                # that replaced it
                if gen == self._search_gen:
                    self._search_fetching = False
                self._wake()  # results landed off the UI thread; repaint now

        threading.Thread(target=_run, daemon=True).start()

    def _search_more(self):
        """The next page, asked for by scrolling off the bottom of this one.

        Free whenever the reservoir still holds rows this scope has not shown —
        which is most of the time, since every fetch brings back a page of each
        category. Only an empty one costs a request, and only one is ever in
        flight, so holding the down arrow can't fan out.
        """
        if self._search_loading or self._search_fetching:
            return
        scope = self._search_filter
        if scope == "playlists":
            return  # a local scan already returned everything it has
        if self._search_servable(scope):
            self._fill_search_view(scope)
            return
        if self._search_scope_done(scope):
            return
        if time.monotonic() - self._search_last_fetch < SEARCH_FETCH_MIN_INTERVAL:
            return
        query = self._search_query.strip()
        if not query:
            return
        self._put_search_view(scope, loading=True, cached=False)
        self._search_fetching = True
        self._fetch_search_page(query, self._search_gen)

    def _search_own_playlists(self, query: str):
        """Search the user's own playlists — from the local index, never the
        network. TIDAL has no API for this, and the cache was built for it:
        every playlist it has fetched keeps the plain-text name, artists and
        album of each track, and the playlist's own name alongside. A scan of a
        few thousand rows is instant, so this runs on the UI thread and there
        is nothing to wait for.

        Four fields, case-insensitive substring on each, and the *order* is the
        design: title, then artist, then album, then playlist name. Ranking
        matters more the more fields there are — the playlist name is one
        string standing in for every track under it, so a query that matched
        only it would otherwise bury the track actually called that under a
        whole "Late Night" playlist. Last place keeps "type the playlist's name
        and get its tracks" working without letting it flood anything.

        The one indexed field deliberately left out is the playlist's creator:
        on your own playlists it is your own name on every row, which matches
        everything or nothing and tells you neither.
        """
        # A local scan returns everything it has in one go, so this view is
        # complete the moment it is written — nothing pages it and nothing
        # refetches it for the rest of the query's life
        if not self._cache.enabled:
            self._put_search_view(
                "playlists", loading=False, results=[], cursor=0,
                message="Playlist search needs the metadata cache — "
                        "turn 'Cache playlists' on in settings")
            return
        names = {p.id: p.name for p in (self._cache.get_playlists() or [])}
        needle = query.lower()
        # One bucket per field, kept in rank order and concatenated at the end.
        # Within a bucket the index's own order survives, so two tracks that
        # matched the same way stay in the order their playlists are in.
        by_title, by_artist, by_album, by_playlist = [], [], [], []
        matched_playlists = {pid for pid, pname in names.items()
                             if needle in (pname or "").lower()}
        scanned = 0
        for playlist_id, record in self._cache.iter_tracks():
            scanned += 1
            name = record.get("name") or ""
            artists = ", ".join(record.get("artists") or [])
            album = record.get("album") or ""
            if needle in name.lower():
                bucket = by_title
            elif needle in artists.lower():
                bucket = by_artist
            elif needle in album.lower():
                bucket = by_album
            elif playlist_id in matched_playlists:
                bucket = by_playlist
            else:
                continue
            bucket.append({
                "type": "track",
                "name": name,
                "artist": artists,
                "playlist": names.get(playlist_id, "Playlist"),
                "obj": CachedTrack(record),
            })
        results = by_title + by_artist + by_album + by_playlist
        self._put_search_view(
            "playlists", loading=False, results=results, cursor=0, cached=False,
            message="" if results else (
                "No results in your playlists" if scanned else
                "Nothing cached to search yet — open Playlists once to index them"))

    def _select_search_result(self):
        if not self._search_results:
            return
        item = self._search_results[self._search_cursor]
        obj = item["obj"]

        if item["type"] == "track":
            self._queue = [obj]
            self._queue_index = 0
            self._play_track(obj)
            self._mode = self.MODE_PLAYER
            self._nav_history.clear()
        elif item["type"] == "album":
            self._open_album(obj)
        elif item["type"] == "artist":
            self._open_artist(obj)
        elif item["type"] == "playlist":
            # The same door the playlists page and the artist page use. It
            # decides for itself whether the playlist is editable, so one that
            # arrived from search is read-only unless it really is yours.
            self._open_playlist(obj)

    def _open_album(self, album):
        self._push_nav()
        self._mode = self.MODE_BROWSE
        self._browse_playlist = None
        self._browse_title = album.name
        self._browse_tracks = []
        self._browse_cursor = -1
        self._browse_loading = True
        self._browse_message = ""

        def _run():
            try:
                tracks = album.tracks()
                self._browse_tracks = list(tracks)
                if not self._browse_tracks:
                    self._browse_message = "No tracks found"
            except Exception:
                self._browse_message = "Failed to load album"
            finally:
                self._browse_loading = False

        threading.Thread(target=_run, daemon=True).start()

    def _open_artist(self, artist):
        """Open the artist page on its first tab. Nothing but that tab is
        fetched: the other three are several more requests, and opening a page
        is one keystroke."""
        self._push_nav()
        self._mode = self.MODE_ARTIST
        self._artist = artist
        self._artist_section = self.ARTIST_SECTIONS[0]
        self._artist_cursor = self._artist_cursors.get(self._artist_key(), 0)
        self._load_artist_section()

    # ── Artist page ──

    def _artist_key(self, section: Optional[str] = None):
        """What a section's results are filed under: the artist and the tab.
        Keyed by id rather than by object so the same artist reached from two
        places is still the same page."""
        return (str(getattr(self._artist, "id", "") or ""), section or self._artist_section)

    def _artist_record(self):
        """The record for the tab on screen: state, rows and a message. None
        only before the first visit, which the display reads as loading."""
        return self._artist_sections.get(self._artist_key())

    def _artist_rows(self) -> list:
        record = self._artist_record()
        return record["items"] if record and record["state"] == "ready" else []

    def _load_artist_section(self, force: bool = False):
        """Fetch the tab on screen, once. The presence of a record — loading,
        ready or failed — *is* the answer to "has this tab been visited", so a
        held-down Tab cannot fan out into requests and a second look costs
        nothing. `force` is the retry a failed tab offers."""
        artist = self._artist
        if artist is None:
            return
        key = self._artist_key()
        if key in self._artist_sections and not force:
            return
        section = self._artist_section
        limit = max(20, self._page_size)
        self._artist_sections = {
            **self._artist_sections, key: {"state": "loading", "items": [], "message": ""}}

        def _run():
            try:
                items = self._fetch_artist_section(artist, section, limit)
            except Exception:
                record = {"state": "failed", "items": [],
                          "message": self.ARTIST_SECTION_FAILED[section]}
            else:
                record = {"state": "ready", "items": items,
                          "message": "" if items else self.ARTIST_SECTION_EMPTY[section]}
            # Whole-dict assignment: the paint thread only ever reads a record
            # that is already complete
            self._artist_sections = {**self._artist_sections, key: record}
            self._wake()

        threading.Thread(target=_run, daemon=True).start()

    def _fetch_artist_section(self, artist, section: str, limit: int) -> list:
        """One tab, one round of requests, off the UI thread. Rows are tagged
        with what they are, because a section can hold more than one kind."""
        if section == "tracks":
            # At least a page — a 40-row page must not show 20 tracks and
            # claim that's all the artist has
            return [{"type": "track", "obj": t}
                    for t in (artist.get_top_tracks(limit=limit) or [])]
        if section == "albums":
            return [{"type": "album", "obj": a}
                    for a in (artist.get_albums(limit=limit) or [])]
        if section == "playlists":
            return [{"type": "playlist", "obj": p}
                    for p in self._artist_page_playlists(artist)]
        return self._artist_suggestions(artist, limit)

    def _artist_page_playlists(self, artist) -> list:
        """The playlists an artist appears on. There is no
        artists/{id}/playlists endpoint — tidalapi's Artist has no accessor for
        one — so this comes from the artist page itself (pages/artist), which
        is where listen.tidal.com gets the same rows. One request; keep the
        playlist items out of it and drop everything else."""
        page = artist.page()
        found = []
        seen = set()
        for category in (getattr(page, "categories", None) or []):
            for item in (getattr(category, "items", None) or []):
                if not isinstance(item, tidalapi.Playlist):
                    continue
                pid = str(getattr(item, "id", "") or "")
                if pid and pid in seen:
                    continue
                seen.add(pid)
                found.append(item)
        return found

    def _artist_suggestions(self, artist, limit: int) -> list:
        """What TIDAL suggests off the back of this artist: the artist radio —
        the same tracks its mix is made of — then the artists it considers
        similar. Two endpoints in one tab, and either half alone is still a
        useful page, so only losing both counts as a failure."""
        rows = []
        failed = 0
        try:
            rows += [{"type": "track", "obj": t}
                     for t in (artist.get_radio(limit=limit) or [])]
        except Exception:
            failed += 1
        try:
            rows += [{"type": "artist", "obj": a}
                     for a in (artist.get_similar() or [])]
        except Exception:
            failed += 1
        if failed == 2:
            raise RuntimeError("neither radio nor similar artists answered")
        return rows

    def _cycle_artist_section(self, step: int = 1):
        """Tab between the tabs, wrapping, and landing on one for the first
        time is what fetches it. The cursor is per tab, so coming back to
        Albums comes back to where you were in them."""
        order = self.ARTIST_SECTIONS
        self._artist_cursors = {**self._artist_cursors, self._artist_key(): self._artist_cursor}
        self._artist_section = order[(order.index(self._artist_section) + step) % len(order)]
        self._artist_cursor = self._artist_cursors.get(self._artist_key(), 0)
        self._load_artist_section()

    def _artist_section_tracks(self) -> list:
        return [row["obj"] for row in self._artist_rows() if row["type"] == "track"]

    def _select_artist_row(self):
        """Enter on a row does the obvious thing for what the row is, through
        the same navigation everything else uses. On a failed tab it is the
        retry instead — a section that lost its one request would otherwise
        stay lost for the session."""
        record = self._artist_record()
        if record is not None and record["state"] == "failed":
            self._load_artist_section(force=True)
            return
        rows = self._artist_rows()
        if not rows or not (0 <= self._artist_cursor < len(rows)):
            return
        row = rows[self._artist_cursor]
        obj = row["obj"]
        if row["type"] == "album":
            self._open_album(obj)
        elif row["type"] == "playlist":
            self._open_playlist(obj)
        elif row["type"] == "artist":
            self._open_artist(obj)
        else:
            # Position counted rather than searched for: a section can hold
            # tracks and artists at once, and two tracks can compare equal
            tracks = self._artist_section_tracks()
            index = sum(1 for r in rows[:self._artist_cursor] if r["type"] == "track")
            self._queue = tracks
            self._queue_index = index
            self._play_track(obj)

    def _play_all_artist(self):
        tracks = self._artist_section_tracks()
        if not tracks:
            return
        self._queue = tracks
        self._play_queue_index(0)

    def _play_browse_track(self):
        if not self._browse_tracks:
            return
        track = self._browse_tracks[self._browse_cursor]
        self._queue = list(self._browse_tracks)
        self._queue_index = self._browse_cursor
        self._play_track(track)

    def _play_all_browse(self):
        if not self._browse_tracks:
            return
        self._queue = list(self._browse_tracks)
        self._play_queue_index(0)

    def _load_playlists(self):
        """Show the cached playlist list at once, then replace it with the
        live one. Cache never answers on its own: the fetch always runs, so a
        playlist added elsewhere shows up one round trip after you look —
        the same wait the list used to cost every single time."""
        cached = self._cache.get_playlists()
        self._playlists = cached or []
        self._playlists_loading = not cached
        self._playlists_cursor = 0
        self._playlists_message = ""

        def _run():
            try:
                playlists = self.session.user.playlists()
                fresh = list(playlists) if playlists else []
                self._playlists = fresh
                if self._playlists_cursor >= len(fresh):
                    self._playlists_cursor = max(0, len(fresh) - 1)
                if not fresh:
                    self._playlists_message = "No playlists found"
                self._cache.put_playlists(fresh, editable_type=tidalapi.UserPlaylist)
            except Exception:
                # Offline with a cached list is a usable player, not an error
                if not self._playlists:
                    self._playlists_message = "Failed to load playlists"
            finally:
                self._playlists_loading = False
                self._wake()

        threading.Thread(target=_run, daemon=True).start()

    def _open_playlist(self, playlist):
        """Open a playlist and show its tracks in browse mode.

        Cached track rows paint immediately; the live fetch runs anyway and
        overwrites them with real objects, which is also what turns the rows
        back into something playable and (for your own playlists) editable.
        """
        self._push_nav()
        self._mode = self.MODE_BROWSE
        playlist_id = str(getattr(playlist, "id", "") or "")
        # Removal is only offered on the user's own playlists — and only once
        # we hold the real object, so a cached row can't try to edit anything
        self._browse_playlist = playlist if isinstance(playlist, tidalapi.UserPlaylist) else None
        self._browse_title = playlist.name if hasattr(playlist, "name") else "Playlist"
        cached = self._cache.get_playlist_tracks(playlist_id) if playlist_id else None
        self._browse_tracks = cached or []
        self._browse_cursor = -1
        self._browse_loading = not cached
        self._browse_message = ""

        title = self._browse_title

        def _run():
            try:
                live = playlist
                if getattr(playlist, "cached", False):
                    # Opened from a cached row: get the real playlist first, so
                    # removal and playback have something to work with.
                    # session.playlist() already hands back a UserPlaylist for
                    # the ones you own.
                    live = self.session.playlist(playlist_id)
                    if isinstance(live, tidalapi.UserPlaylist):
                        self._browse_playlist = live
                tracks = list(live.tracks() or [])
                if playlist_id:
                    # Store even if the user walked away — the fetch is paid
                    # for either way, and the next visit gets it for free
                    self._cache.put_playlist_tracks(playlist_id, tracks)
                # Don't stamp on a list the user has already navigated away from
                if self._browse_title != title:
                    return
                self._browse_tracks = tracks
                if self._browse_cursor >= len(tracks):
                    self._browse_cursor = len(tracks) - 1
                if not tracks:
                    self._browse_message = "Playlist is empty"
            except Exception:
                if not self._browse_tracks:
                    self._browse_message = "Failed to load playlist"
            finally:
                self._browse_loading = False
                self._wake()

        threading.Thread(target=_run, daemon=True).start()

    def _remove_from_queue(self):
        """Remove the selected track from the queue."""
        if not self._queue or self._queue_cursor >= len(self._queue):
            return
        removing_current = (self._queue_cursor == self._queue_index)
        removing_before_current = (self._queue_cursor < self._queue_index)
        self._queue.pop(self._queue_cursor)
        if removing_before_current:
            self._queue_index -= 1
        elif removing_current:
            # If we removed the playing track, play the next one or stop
            if self._queue and self._queue_index < len(self._queue):
                self._play_track(self._queue[self._queue_index])
            elif self._queue and self._queue_index > 0:
                self._queue_index = len(self._queue) - 1
                self._play_track(self._queue[self._queue_index])
            else:
                self._playing = False
                self._current_track = None
                if self.audio:
                    self.audio.stop()
        # Adjust cursor
        if self._queue_cursor >= len(self._queue) and self._queue:
            self._queue_cursor = len(self._queue) - 1

    def _remove_from_browse_playlist(self):
        """Remove the track under the cursor from the open (own) playlist."""
        pl = self._browse_playlist
        if pl is None or not self._browse_tracks or self._browse_cursor < 0:
            return
        if self._browse_remove_busy:
            return  # previous removal still in flight — indices would shift
        index = self._browse_cursor
        track = self._browse_tracks[index]
        self._browse_remove_busy = True

        def _run():
            try:
                if pl.remove_by_index(index):
                    remaining = [t for i, t in enumerate(self._browse_tracks) if i != index]
                    self._browse_tracks = remaining
                    if self._browse_cursor >= len(remaining):
                        self._browse_cursor = len(remaining) - 1
                    self._set_toast(f'Removed "{track.name}" from {pl.name}')
                else:
                    self._set_toast("Failed to remove from playlist")
            except Exception:
                self._set_toast("Failed to remove from playlist")
            finally:
                self._browse_remove_busy = False

        threading.Thread(target=_run, daemon=True).start()

    def _set_toast(self, msg: str, seconds: float = 2.5):
        """Show a transient message; the 4fps render loop expires it."""
        self._toast = msg
        self._toast_until = time.time() + seconds

    def _target_track_for_picker(self):
        """Resolve which track 'add to playlist' refers to in the current mode."""
        if self._mode == self.MODE_QUEUE and self._queue and self._queue_cursor < len(self._queue):
            return self._queue[self._queue_cursor]
        if self._mode == self.MODE_BROWSE and self._browse_tracks and self._browse_cursor >= 0:
            return self._browse_tracks[self._browse_cursor]
        if self._mode == self.MODE_ARTIST:
            rows = self._artist_rows()
            if rows and 0 <= self._artist_cursor < len(rows):
                row = rows[self._artist_cursor]
                # Only a track row is addable; on an album or an artist row the
                # sensible target is still whatever is playing
                if row["type"] == "track":
                    return row["obj"]
        return self._current_track

    def _picker_playlists(self) -> list:
        """The picker's rows, last-used first.

        Derived on read rather than stored: the cache in _editable_playlists
        stays whatever TIDAL said, and a pin naming a playlist that is no
        longer in it — deleted on the web since the last run — simply matches
        nothing and the normal order comes back. No ghost row, no error.
        """
        playlists = self._editable_playlists  # read once: another thread replaces it
        pid = self._last_playlist_id
        if not pid or not playlists:
            return playlists
        pinned = [p for p in playlists if str(getattr(p, "id", "") or "") == pid]
        if not pinned:
            return playlists
        return pinned + [p for p in playlists if str(getattr(p, "id", "") or "") != pid]

    # ── Downloads ──

    def _download_target(self, whole: bool = False) -> tuple:
        """`(the tracks [d] means here, what to call them)`. No network.

        A row means the track on it. A **"Play All" row means the whole
        list** — that was the bug: `_target_track_for_picker` fell through to
        `self._current_track`, so pressing `d` on Play All opened a box about
        an unrelated song. The artist page's Albums and Playlists tabs are
        the same shape (no track under the cursor at all), and there the
        section's own tracks are the sensible target.

        `whole` is `[D]`, which asks for the list wherever the cursor is. It
        exists because the queue has no Play All row to press `d` on and the
        whole queue is the one thing a queue can obviously mean.
        """
        if self._mode == self.MODE_BROWSE and self._browse_tracks:
            if whole or self._browse_cursor < 0:
                return list(self._browse_tracks), self._browse_title or "This list"
            return [self._browse_tracks[self._browse_cursor]], ""
        if self._mode == self.MODE_QUEUE and self._queue:
            if whole:
                return list(self._queue), "Queue"
            if self._queue_cursor < len(self._queue):
                return [self._queue[self._queue_cursor]], ""
        if self._mode == self.MODE_ARTIST:
            rows = self._artist_rows()
            row = (rows[self._artist_cursor]
                   if rows and 0 <= self._artist_cursor < len(rows) else None)
            if row is not None and row["type"] == "track" and not whole:
                return [row["obj"]], ""
            section = self._artist_section_tracks()
            if section:
                name = getattr(self._artist, "name", "") or "Artist"
                label = self.ARTIST_SECTION_LABELS[self._artist_section]
                return list(section), f"{name} · {label}"
        track = self._current_track
        return ([track] if track is not None else []), ""

    def _open_download(self, whole: bool = False):
        """Open the download box over whatever [d] was pressed on.

        The same target rule the add-to-playlist picker uses wherever there is
        one track to mean, so [d] and [y] never disagree about which track the
        screen you are looking at means — and `_download_target` where there
        is not. Nothing is pushed onto the nav history and no mode changes:
        the box is drawn over the screen you were on and Esc takes it away
        again, so there is nowhere to be lost. The cursor is pre-placed on the
        tier settings is set to, which is what makes the common case `d` then
        `Enter` — or `d` then `d` — and no reading.
        """
        tracks, label = self._download_target(whole)
        if not tracks:
            self._set_toast("No track selected")
            return
        track = tracks[0]
        self._download_open = True
        self._download_known = None
        self._download_tracks = tracks
        self._download_label = label if len(tracks) > 1 else ""
        self._download_track = track
        try:
            self._download_cursor = QUALITY_CHOICES.index(self._quality_name)
        except ValueError:
            self._download_cursor = 0
        # A job for a different track is finished business; one for this track
        # is what the screen should still be reporting on. A *running* one is
        # never dropped — a bulk run keeps its bars while you look at
        # something else.
        job = self._download_job
        running = bool(job) and job.get("state") == "running"
        if job and not running and job.get("track_id") != getattr(track, "id", None):
            self._download_job = None
        if not running:
            # A download the app was killed in the middle of left a scratch
            # file behind. It is named for this track and nothing is writing
            # it, so it goes — by exact name, the only deletion this feature
            # ever performs under the music folder.
            self._discard_staging(getattr(track, "id", None))

    def _cancel_download(self):
        """Stop the running download and take its half-written file with it.

        The generation counter is what tells the thread; it checks it every
        chunk, so the file is gone within one chunk of the keypress rather
        than at the end of the track it was fetching.
        """
        job = self._download_job
        if not job or job.get("state") != "running":
            return
        self._download_job_gen += 1
        self._download_job = dict(job, state="cancelled")
        self._set_toast("Download cancelled")

    def _download_tier(self) -> str:
        return QUALITY_CHOICES[self._download_cursor % len(QUALITY_CHOICES)]

    def _download_estimate(self, tier: str) -> int:
        """Bytes this track is likely to be at this tier. **No network.**

        Duration is already on the track (and already in the metadata index),
        so all four tiers can be shown at once for free. Asking TIDAL instead
        would be one `playbackinfo` request per track per tier against a
        limiter that revokes streaming at about fifty — see ai/INCIDENTS #1.
        """
        return downloads.estimate_bytes(
            getattr(self._download_track, "duration", 0), tier)

    def _download_bulk(self) -> bool:
        """Whether the box is about a list rather than a song."""
        return len(self._download_tracks) > 1

    def _download_bulk_estimate(self, tier: str) -> int:
        """What a whole playlist is likely to cost at `tier`. **No network,
        and it must stay that way.** Every duration is already on the tracks,
        so this is arithmetic; a real per-track size is one `playbackinfo`
        each, and 53 of those in 2.8 s is ai/INCIDENTS #1 exactly."""
        return sum(downloads.estimate_bytes(getattr(t, "duration", 0), tier)
                   for t in self._download_tracks)

    def _download_facts(self) -> tuple:
        """`(tier → exact bytes already on this disk, the downloaded file)`.

        The only two places a real byte count can be had for nothing: the
        download index, which recorded the size of every file it wrote, and
        the cache tracker, which recorded the size of every song it kept.
        Both key their tier by what TIDAL **granted**, so the answer is
        exact for at most two of the four tiers and estimated for the rest.
        The download wins where they disagree — it is the file [d] would be
        about to replace.

        Measured once per opening of the box and kept, because the box
        repaints twice a second and `load_index()` re-reads and re-parses the
        whole index every call: 229 ms per repaint at 500 downloads, on the
        UI thread (ai/reference/data-path-audit-2026-07-26.md F6). Dropped
        when a download lands, which is the only thing that changes it.
        """
        track_id = getattr(self._download_track, "id", None)
        known = self._download_known
        if known is not None and known[0] == track_id:
            return known[1], known[2]
        sizes, owned = {}, None

        def note(granted, size):
            # Both stores name the tier the way tidalapi does and the box
            # names it the way the settings page does. A tier nobody
            # recognises is left showing an estimate: unknown is not
            # evidence of anything, the rule `_tier_is_enough` follows too.
            for name, quality in self.QUALITY_MAP.items():
                if granted == quality and size:
                    sizes[name] = int(size)
                    return

        if track_id is not None:
            record = self._cache.audio_record(track_id) or {}
            note(record.get("quality"), record.get("bytes"))
            owned = downloads.path_for(track_id)
            if owned is not None:
                entry = downloads.load_index().get(str(track_id)) or {}
                note(entry.get("granted"), entry.get("bytes"))
        self._download_known = (track_id, sizes, owned)
        return sizes, owned

    def _download_size(self, tier: str) -> str:
        """What the box says the download will cost.

        A `~` on everything is the honest default: a real size is one
        `playbackinfo` per tier, which is the request pattern that took the
        owner's session down (ai/INCIDENTS #1). The `~` comes off only when
        the bytes are already here to be counted.
        """
        sizes, _ = self._download_facts()
        if tier in sizes:
            return downloads.format_bytes(sizes[tier])
        estimate = self._download_estimate(tier)
        return "~" + downloads.format_bytes(estimate) if estimate > 0 else "—"

    def _start_download_job(self, tier: str):
        """Download the targeted track at `tier`, on a daemon thread.

        One job at a time, by the same single-flight rule the search paging
        uses: a held-down Enter must not fan out into requests. The generation
        counter is what a job checks to know it has been superseded — leaving
        the screen or starting another download bumps it, and the running job
        abandons its half-written file rather than landing on top of a newer
        one.
        """
        track = self._download_track
        if track is None:
            return
        if (self._download_run is not None
                and (self._download_job or {}).get("state") == "running"):
            # One more track on the end of the run rather than a second
            # resolver beside it: two paced resolvers is two API requests a
            # second, which is not the rate this was measured at
            self._start_bulk_download_job(
                tier, tracks=[track], label=getattr(track, "name", "") or "")
            return
        job = self._download_job
        if job and job.get("state") == "running":
            # Single-flight, but not silent: a key that refuses has to say
            # why rather than look dead
            self._set_toast("A download is already running — [x] stops it")
            return
        self._download_job_gen = gen = self._download_job_gen + 1
        track_id = getattr(track, "id", None)
        self._download_job = {
            "state": "running", "tier": tier, "track_id": track_id,
            "done": 0, "total": 0, "path": None, "error": "", "tags": "",
        }

        def _update(**changes):
            job = dict(self._download_job or {})
            job.update(changes)
            self._download_job = job
            self._wake()

        def _run():
            last = [0.0]

            def _progress(done, total):
                # Cheap enough to call per chunk, but a repaint per chunk is
                # not — one every quarter second is already faster than the
                # eye and slower than the monitor's own tick
                now = time.time()
                if now - last[0] < 0.25:
                    return
                last[0] = now
                _update(done=done, total=total)

            try:
                final, written, size, landed = self._download_to_music(
                    track, tier,
                    abandoned=lambda: self._download_job_gen != gen,
                    progress=_progress)
                if self._download_job_gen != gen:
                    return
                # `landed` is what arrived, which is not always what was asked
                # for — the box says so rather than letting a MAX request
                # that came back HIGH read as a MAX file
                _update(state="done", path=str(final), tags=written,
                        done=size, total=size, landed=landed)
                self._set_toast(f"Downloaded to {final.parent}")
            except _DownloadSuperseded:
                pass
            except Exception as e:
                logger.debug("Download failed: %s", e)
                if self._download_job_gen == gen:
                    _update(state="failed", error=str(e)[:PLAYER_ERROR_CHARS])
            finally:
                self._wake()

        threading.Thread(target=_run, daemon=True).start()

    @staticmethod
    def _new_download_slots() -> tuple:
        """One display slot per fetch worker.

        A slot has exactly one writer at a time — the worker holding it — and
        the monitor thread only ever touches `samples`/`mark`, which no worker
        reads. So the bars need no lock, and they are deliberately *not* kept
        inside the job dict's own fields: two workers merging changes into one
        dict could lose an update, and losing a bar is visible.
        """
        return tuple({"title": "", "done": 0, "total": 0, "estimate": 0,
                      "state": "idle", "size": 0, "banked": 0,
                      "samples": (), "mark": None}
                     for _ in range(DOWNLOAD_WORKERS))

    def _download_queued_ids(self) -> set:
        """Every track id the live run has ever accepted — pending, in flight
        and finished. One set, because `_PacedRun.items` is never consumed."""
        run = self._download_run
        if run is None:
            return set()
        return {tid for _t, _tier, tid in run.items[:] if tid is not None}

    def _start_bulk_download_job(self, tier: str, tracks=None,
                                 label: Optional[str] = None):
        """Download every track the box is about, at `tier` — **or add them to
        the run already going.**

        Asking for a second playlist while the first is downloading used to
        be refused. It is now the same run with more work on the end of it:
        `_PacedRun.add`. Deliberately *not* a queue of runs, which would need
        a scheduler of its own and would make "is the pacing floor still
        honoured between one batch and the next" a new question — appending
        keeps it the question it already is, because there is still exactly
        one serial paced resolver and one pool of three fetchers.

        Each batch carries **its own tier**, so a MAX ask appended to a
        HIGH run downloads at MAX: the tier is already per-track inside
        the plan, and quietly fetching somebody's hi-res request as CD
        quality because a different batch got there first is the kind of
        dishonesty this box exists to avoid.

        Anything already queued, in flight or finished is dropped by track id
        — appending a playlist that overlaps one already running must not
        fetch a song twice.
        """
        tracks = list(self._download_tracks if tracks is None else tracks)
        tracks = [t for t in tracks if t is not None]
        if not tracks:
            return
        if (self._refetch_job or {}).get("state") == "running":
            # Two paced resolvers at once is two API requests a second, which
            # is not the rate this run's tests measured. One at a time.
            self._set_toast("Re-fetching — [Esc] on the settings page stops it")
            return
        label = self._download_label if label is None else label

        queued = self._download_queued_ids()
        items, seen = [], set()
        for track in tracks:
            tid = getattr(track, "id", None)
            if tid is not None and (tid in queued or tid in seen):
                continue
            seen.add(tid)
            items.append((track, tier, tid))

        run = self._download_run
        if run is not None and (self._download_job or {}).get("state") == "running":
            self._add_to_download_run(run, items, len(tracks), label)
            return
        if (self._download_job or {}).get("state") == "running":
            # A single-track job holds the resolver; it is one request and it
            # is nearly over, so this is the one ask that still waits
            self._set_toast("A download is already running — [x] stops it")
            return
        if not items:
            return

        self._download_job_gen = gen = self._download_job_gen + 1
        slots = self._new_download_slots()
        self._download_job = {
            "state": "running", "bulk": True, "tier": tier, "track_id": None,
            "tracks": len(items), "done": 0, "failed": 0, "slots": slots,
            "error": "", "labels": (label,) if label else (),
            "estimate": sum(downloads.estimate_bytes(
                getattr(t, "duration", 0), tr) for t, tr, _i in items),
        }

        def _alive():
            return self._download_job_gen == gen

        def _update(**changes):
            # A superseded run's workers can still be finishing; their tally
            # must not land on the job that replaced this one
            if not _alive():
                return
            current = dict(self._download_job or {})
            current.update(changes)
            self._download_job = current
            self._wake()

        def _fetch(item, plan, slot):
            track, item_tier, _tid = item
            slot.update(title=plan["title"] or str(plan["track_id"]),
                        done=0, total=0, size=0, samples=(), mark=None,
                        estimate=downloads.estimate_bytes(
                            getattr(track, "duration", 0), item_tier),
                        state="running")
            last = [0.0]

            def _progress(done, total):
                slot["done"] = done
                slot["total"] = total or slot["estimate"]
                # Cheap per chunk; a repaint per chunk is not
                now = time.monotonic()
                if now - last[0] >= 0.25:
                    last[0] = now
                    self._wake()

            _final, _written, size, _landed = self._download_deliver(
                plan, abandoned=lambda: not _alive(), progress=_progress,
                record=False)
            # `banked` is this slot's share of the run's total, and `done`
            # goes back to zero so the two never count the same bytes twice
            slot.update(done=0, total=0, size=size,
                        banked=slot["banked"] + size, state="done")
            return plan.get("record")

        run = _PacedRun(
            items=items,
            resolve=lambda item: self._download_plan(item[0], item[1]),
            fetch=_fetch,
            alive=_alive,
            report=_update,
            workers=DOWNLOAD_WORKERS,
            slots=slots,
            clock=self._api_pace,
        )
        self._download_run = run

        def _go():
            outcome = leftover = None
            try:
                outcome = run.run()
            finally:
                # Whole-object assignment, and only by the thread that owns
                # the run: after this a new ask starts a new run rather than
                # appending to a finished one. Cleared **before** `pending()`
                # is read, so a batch that arrived while this was winding up
                # is either still in the run's hands or in `leftover` here.
                if self._download_run is run:
                    self._download_run = None
                leftover = run.pending()
            if outcome is None:
                self._wake()
                return
            done, failed, blocked = outcome
            if leftover and not blocked and _alive():
                # A batch that landed in the sliver of a window where the run
                # had stopped looking. Start it rather than lose it.
                labels = [l for l in ((self._download_job or {}).get("labels")
                                      or ()) if l]
                self._download_job = dict(self._download_job or {},
                                          state="done")
                self._start_bulk_download_job(
                    leftover[0][1], tracks=[t for t, _q, _i in leftover],
                    label=labels[-1] if labels else "")
                self._wake()
                return
            if blocked:
                _update(state="blocked", error=blocked)
                self._set_toast(
                    "TIDAL is rate-limiting — download stopped. "
                    "Nothing will be retried.",
                    seconds=PLAYER_ERROR_SECONDS)
            else:
                _update(state="done")
                # Anything asked for that never started is said out loud
                # rather than left to be inferred from a count that does not
                # add up — see the window `_PacedRun.run` documents.
                short = max((self._download_job or {}).get("tracks", 0)
                            - done - failed, 0)
                self._set_toast(
                    f"Downloaded {done} song{'' if done == 1 else 's'}"
                    f" to {downloads.display_dir()}"
                    + (f" · {failed} failed" if failed else "")
                    + (f" · {short} not started" if short else ""))
            self._wake()

        threading.Thread(target=_go, daemon=True).start()

    def _add_to_download_run(self, run, items, asked: int,
                             label: Optional[str]) -> None:
        """Put a second batch on the end of a run, and say what happened.

        The count and the estimate on the box are the *run's* from here on,
        because that is what is being downloaded. Nothing is refused and
        nothing is silent: an ask that was entirely duplicates says so rather
        than looking like a key that did nothing.
        """
        job = dict(self._download_job or {})
        if not items:
            self._set_toast(f"Already queued — all {asked} of them"
                            if asked > 1 else "Already queued")
            return
        run.add(items)
        labels = tuple(job.get("labels") or ())
        if label and label not in labels:
            labels = labels + (label,)
        job.update(
            tracks=(job.get("tracks") or 0) + len(items),
            estimate=(job.get("estimate") or 0) + sum(
                downloads.estimate_bytes(getattr(t, "duration", 0), tr)
                for t, tr, _i in items),
            labels=labels,
        )
        self._download_job = job
        skipped = asked - len(items)
        self._set_toast(
            f"Queued {len(items)} more"
            + (f" · {skipped} already queued" if skipped else ""))
        self._wake()

    def _sample_download_rates(self) -> None:
        """Move every active download's byte counter into its rate window.

        Called from `_monitor_playback`, on the 0.5 s tick that already
        exists — no new thread and no new timer, which is a hard rule here
        and the reason the window is 1.5 s rather than some number chosen for
        smoothness. Three samples is long enough that the digit stops
        twitching and short enough that a real stall shows within a second.
        The whole of the arithmetic is a three-element tuple and a mean.

        Zero until the first sample has landed, which `_format_rate` prints
        as nothing at all: measuring, rather than stalled.
        """
        job = self._download_job
        if not job or job.get("state") != "running":
            return
        now = time.monotonic()
        for slot in job.get("slots") or ():
            if slot.get("state") != "running":
                slot["mark"] = None
                continue
            done = slot.get("done") or 0
            mark = slot.get("mark")
            if mark is None or now <= mark[0]:
                slot["mark"] = (now, done)
                continue
            then, before = mark
            slot["samples"] = (slot["samples"]
                               + ((done - before) / (now - then),))[-RATE_SAMPLES:]
            slot["mark"] = (now, done)

    @staticmethod
    def _slot_rate(slot: dict) -> float:
        samples = slot.get("samples") or ()
        return sum(samples) / len(samples) if samples else 0.0

    def _download_to_music(self, track, tier: str, abandoned, progress=None):
        """Put one track in the music folder at `tier`. Returns
        `(path, tags written, bytes, the tier it actually landed at)`; raises
        on anything that went wrong.

        The last of those is not always the one asked for — `tier` is a
        starting rung and `_stream_at_best_tier` walks down from it — so it is
        returned rather than assumed, and the box reports what arrived instead
        of what was requested.

        The single place a download is performed, because there are now three
        callers — the download screen, and the two halves of "re-fetch
        everything at the current quality" — and having written it twice is
        how one of them would quietly stop working. Cleaning up after a
        failure is in here too, for the same reason.

        Split in two behind this signature, and the seam is where the rate
        limiter is: `_download_plan` is the API half and `_download_deliver`
        is the CDN half. A bulk run drives the two halves separately so that
        three tracks can be *fetched* at once while still being *resolved*
        one at a time; everything else calls this and gets the old behaviour.
        """
        try:
            plan = self._download_plan(track, tier)
        except Exception:
            self._discard_staging(getattr(track, "id", None))
            raise
        return self._download_deliver(plan, abandoned, progress)

    def _download_plan(self, track, tier: str) -> dict:
        """Everything a download needs from TIDAL, and **nothing else**.

        This is the whole API cost of one download — `session.track()` if the
        row was a cached one, then one `get_stream()` — which is why a bulk
        run does exactly this much serially and paced, and parallelises only
        what comes after it. Anything that is a plain CDN GET, or disk, is
        deliberately on the other side of the seam.
        """
        track_id = getattr(track, "id", None)
        real = self._resolve_track(track)
        if real is None:
            raise RuntimeError("track could not be resolved")
        # The extension is not known until the CDN answers, so the file is
        # written under a provisional name and moved once the container has
        # identified itself. Named for the track, so three at once cannot
        # collide.
        staging = (downloads.download_dir()
                   / f".ticli-{track_id}{downloads.PART_SUFFIX}")
        staging.parent.mkdir(parents=True, exist_ok=True)
        ext, granted = self._promote_cached_copy(track_id, tier, staging)
        sources = None
        if ext is None:
            # Steps down a tier at a time rather than failing outright, so a
            # track this login cannot have at MAX still lands as HIGH
            # instead of not landing at all
            url, granted, tier = self._stream_at_best_tier(real, tier)
            sources = stream_sources(url)
        return {"track_id": track_id, "real": real, "staging": staging,
                "meta": downloads.track_metadata(real), "tier": tier,
                "ext": ext, "granted": granted, "sources": sources,
                "title": getattr(real, "name", None) or ""}

    def _download_deliver(self, plan: dict, abandoned, progress=None,
                          record=True):
        """The bytes, the name, the tags and the index row. No API request at
        all, which is what makes it safe to run three of these at once.

        `record=False` hands the index write back to the caller as
        `plan["record"]`, because `downloads.record` is a read-modify-write of
        one JSON file and three at once belong on one thread. The bulk runner
        takes that callable and runs it on its own single thread; the index's
        own `_index_lock` is what makes the cycle safe against the UI
        thread's delete, which no arrangement of these threads could be.
        """
        track_id = plan["track_id"]
        final = None
        try:
            # A promoted cached copy never reaches `fetch_to_file`, so this is
            # the only place a cancel between resolving and delivering can be
            # noticed. Without it, cancelling a bulk run could still land one
            # more file after the key was pressed.
            if abandoned is not None and abandoned():
                raise _DownloadSuperseded()
            ext = plan["ext"]
            if ext is None:
                ext = fetch_to_file(plan["sources"], str(plan["staging"]),
                                    abandoned=abandoned, progress=progress)
            final = downloads.destination(plan["meta"], ext)
            final.parent.mkdir(parents=True, exist_ok=True)
            # A re-fetch at another tier can land under another extension, and
            # the old file is then a second copy of the same song in the same
            # folder. By exact name, and only ones ticli wrote.
            previous = downloads.path_for(track_id)
            os.replace(plan["staging"], final)
            if previous is not None and previous != final:
                try:
                    previous.unlink()
                except OSError:
                    pass
            written = tags.write_tags(final, plan["meta"],
                                      self._cover_bytes(plan["real"]))
            size = final.stat().st_size
            relative = final.relative_to(downloads.download_dir())
            tier, granted = plan["tier"], plan["granted"]
            def _commit():
                downloads.record(track_id, relative, tier, size,
                                 granted=granted)
                self._drop_superseded_cache_copy(track_id)

            # Both halves of _commit write a whole JSON file back after
            # reading it, so both belong on the runner's single thread — see
            # the `record=False` note above. Bundled rather than handed back
            # separately, because a download that recorded itself and left the
            # cache copy behind is the state this exists to avoid.
            if record:
                _commit()
            else:
                plan["record"] = _commit
            # The folder changed, and so did the one exact size the box could
            # show for this track
            self._forget_downloads()
            return final, written, size, tier
        except Exception:
            self._discard_staging(track_id)
            if final is not None:
                downloads.discard_scratch(final)
            raise

    def _drop_superseded_cache_copy(self, track_id) -> None:
        """Delete the cached copy of a track that has just been downloaded.

        `_local_source` answers from the downloads tier *unconditionally*, so
        from the moment a download lands the cached copy of the same track
        can never be played again. It is not a spare: it is a second copy of identical
        audio, counted against the cache budget and competing in eviction
        against songs that can still be used. Keeping it means a deliberate
        download quietly evicts somebody else's staple.

        Deliberately one-directional. The cache never touches a download —
        that is the whole point of the two tiers — but a download is entitled
        to reclaim the machine-owned copy it has replaced, because the cache
        is disposable by definition and this deletes nothing the user cannot
        get back by playing the track.

        **Never the track playing right now** — but skipped is not forgiven.
        On POSIX unlinking an open file is safe and mpv plays on, but if the
        backend has not opened it yet it exits and `source_vanished` restarts
        from the network: an audible hiccup, paid for a bookkeeping tidy-up.
        So the id goes into `_reclaim_deferred` instead, and the monitor's
        existing 0.5 s tick retries it the moment the track is no longer
        current (`_reclaim_deferred_copies`). It has to, because nothing else
        ever would: `reconcile()` *keeps* an entry whose file is on disk, and
        eviction ranks by (plays, last) — the much-played track this deferral
        protects is exactly the one eviction keeps for ever. A quit mid-track
        strands the pair on disk, which is what the startup sweep
        (`_reclaim_download_duplicates`) is for.

        Gated on evidence, never on the `cache_songs` setting: turn the
        setting off and answer "n" to `Clear cached songs as well?` and the
        files are still on disk, still unreachable behind the download, and
        this is the only thing that will ever reclaim them —
        `cached_audio_path` and `forget_cached` both work with the setting
        off. A file already gone at unlink time is the state we wanted (the
        reading `clear()` gives FileNotFoundError), so the tracker entry is
        still forgotten: eviction racing this drop must not leave the exact
        entry-with-no-file this function cleans up when it finds it. Only a
        real OSError keeps the entry — a Windows PermissionError means the
        file genuinely remains, and forgetting it would lie.

        Runs wherever `_commit` runs: the bulk runner's single writer thread
        for `record=False`, but inline on the download screen's daemon thread
        and the re-fetch thread for `record=True` — and on the monitor thread
        for deferred retries. The tracker's read-modify-write is serialized
        by `MetadataCache` itself; the deferred set is under `_reclaim_lock`.
        """
        if track_id is None:
            return
        key = str(track_id)
        if str(getattr(self._current_track, "id", None)) == key:
            with self._reclaim_lock:
                self._reclaim_deferred.add(key)
            return
        with self._reclaim_lock:
            self._reclaim_deferred.discard(key)
        path = cached_audio_path(track_id)
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass  # already gone is the state we wanted
            except OSError as e:
                # The file genuinely remains, so the entry stays too
                logger.debug("Could not drop the superseded cached copy: %s", e)
                return
        self._cache.forget_cached([track_id])
        self._cache.invalidate_audio_count()

    def _reclaim_deferred_copies(self) -> None:
        """Retry the reclaims `_drop_superseded_cache_copy` deferred.

        On `_monitor_playback`'s existing 0.5 s tick — no new thread, no new
        timer — and almost always a lock, an empty check and nothing else. An
        id whose track is still playing stays deferred; anything else goes
        back through the same drop logic, which removes it from the set (or
        re-defers it, if a race made it current again). The snapshot is taken
        under the lock and the drops happen outside it, because the drop
        takes the lock itself.
        """
        with self._reclaim_lock:
            if not self._reclaim_deferred:
                return
            current = str(getattr(self._current_track, "id", None))
            ready = [t for t in self._reclaim_deferred if t != current]
        for track_id in ready:
            self._drop_superseded_cache_copy(track_id)

    def _reclaim_download_duplicates(self) -> None:
        """The startup sweep: every track in both tiers loses its cached copy.

        The per-download drop misses two kinds of pair: the track that was
        playing when its download landed and kept playing until quit, so the
        deferral never got its retry; and every duplicate created before the
        drop existed at all. Left alone they survive for ever — `reconcile()`
        keeps a tracker entry whose file is present, and eviction ranks by
        (plays, last), which the much-played tracks people download score
        best on. So this runs once at startup, beside `reconcile()` on its
        daemon thread, and hands each pair to the same drop logic; a track
        already audible by then (a restore can be) is deferred to the
        monitor's tick like any other.

        `downloads.present()` rather than the raw index, because a download
        the user deleted by hand is not a download: `_local_source` falls
        back to the cached copy then, and reclaiming it would delete the one
        copy that still plays. Runs whatever `cache_songs` says — the files
        on disk are the evidence, and with the setting off nothing else would
        ever reclaim them.
        """
        for row in downloads.present():
            track_id = row["id"]
            if cached_audio_path(track_id) or self._cache.audio_record(track_id):
                self._drop_superseded_cache_copy(track_id)

    # ── Re-fetch everything at the current quality ──
    #
    # This is the single most rate-limit-dangerous thing in the app, and the
    # incident it could repeat is the worst one in this project's history: 53
    # `playbackinfo` calls in 2.8 seconds got the owner's IP blocked and his
    # music stopped mid-session (ai/INCIDENTS #1). So the design question is
    # not "how fast can this go" — it is "how do we make it impossible for a
    # user to do that to themselves".
    #
    # Four answers, all of them structural rather than advisory:
    #
    #   * **Serial.** One track at a time, one thread, no fan-out. There is no
    #     parallelism to tune and no way to ask for more.
    #   * **Paced.** At least REFETCH_MIN_INTERVAL between the *start* of one
    #     track and the next, whatever happened in between — so a run of
    #     instant failures cannot become a burst. A track costs two requests
    #     (`session.track` then `get_stream`), so the ceiling is one request a
    #     second and the sustained rate is well under that: an order of
    #     magnitude below what caused the block, and slower still in practice
    #     because each track also has to be fetched.
    #   * **Interruptible.** A generation counter, bumped by Esc and by
    #     leaving the settings page, checked before every track and passed
    #     into `fetch_to_file` as its `abandoned` callback, so a cancel lands
    #     inside a chunk read rather than at the end of a 30 MB file.
    #   * **Stop, never retry, on the evidence of a block.** A 429, or a 401
    #     with subStatus 4006, ends the whole run and says so. Retrying is
    #     what extends those blocks.
    #
    # And it is opt-in twice: an explicit key, and a confirmation that says
    # how many tracks and roughly how many bytes before it will start.

    def _upgrade_target(self) -> Optional[str]:
        """The tier a re-fetch could actually land, or None if none could.

        The **lower** of two things, because both have to be true before a
        byte is worth moving: the tier you have asked for, and the tier TIDAL
        has shown it will serve this login (`_quality_ceiling`, learned for
        free from a granted downgrade). Asking for hi-res on a device-flow
        session does not make hi-res available, and re-fetching a whole
        library to be handed back the same AAC is a great many requests for
        nothing — against the limiter that caused ai/INCIDENTS #1.
        """
        wanted = self.QUALITY_MAP.get(self._quality_name)
        if wanted not in QUALITY_RANK:
            return None
        ceiling = self._quality_ceiling
        if ceiling in QUALITY_RANK and QUALITY_RANK[ceiling] < QUALITY_RANK[wanted]:
            return ceiling
        return wanted

    @staticmethod
    def _is_upgrade(stored, target) -> bool:
        """Whether replacing a copy stored at `stored` with `target` gains
        anything.

        **Strictly greater.** An equal tier is the file already on the disk,
        and a *lower* one would quietly make the library worse — which is
        exactly what this did when it compared `granted != wanted`: setting
        the quality down to HIGH and pressing [R] re-fetched every lossless
        song as AAC, spent a request per track to do it, and reported it as
        success. A re-fetch only ever moves upwards now.

        An unrecorded tier is not an upgrade either, because it cannot be
        shown to be one. Those are counted and reported rather than silently
        folded into "already at this quality" — see `_refetch_candidates`.
        """
        if target not in QUALITY_RANK or stored not in QUALITY_RANK:
            return False
        return QUALITY_RANK[target] > QUALITY_RANK[stored]

    def _refetch_candidates(self) -> dict:
        """What "re-fetch everything at the current quality" would actually do.

        Reads both trackers and no network. Returns `{"downloads": [...],
        "cache": [...], "skipped": n, "unknown": n, "bytes": n}`.

        A copy is a candidate only when the target tier is **strictly above**
        the tier it was granted (`_is_upgrade`), and the target is itself
        capped by what TIDAL will serve (`_upgrade_target`). `skipped` counts
        copies already at or above the target; `unknown` counts copies whose
        tier was never recorded — from before the tracker, or adopted by
        `reconcile()` — which are left alone because "higher than unknown" is
        not a thing anyone can honestly claim.
        """
        target = self._upgrade_target()
        plan = {"downloads": [], "cache": [], "skipped": 0, "unknown": 0,
                "bytes": 0}

        def classify(stored, key, size, bucket):
            # The one statement of the policy, applied to both tiers
            if stored not in QUALITY_RANK:
                plan["unknown"] += 1
            elif not self._is_upgrade(stored, target):
                plan["skipped"] += 1
            else:
                bucket.append(key)
                plan["bytes"] += int(size or 0)

        for key, entry in downloads.load_index().items():
            if downloads._entry_path(entry) is None:
                continue  # deleted by hand: the disk decides
            classify(entry.get("granted"), key, entry.get("bytes"),
                     plan["downloads"])
        if self._cache.keeps_audio:
            downloaded = set(plan["downloads"])
            for key, record in self._cache._load_tracker().items():
                if key in downloaded:
                    continue  # the download tier covers it
                classify(record.get("quality"), key, record.get("bytes"),
                         plan["cache"])
        return plan

    def _start_refetch_job(self) -> None:
        """Re-fetch every local copy at the tier currently selected.

        Both tiers, because Garrett asked for both: the downloads go back into
        `~/Music/Ticli` through the same `_download_to_music` the download
        screen uses (tagged, and the old file removed if the container
        changed), and the cached songs go back into the cache directory.

        The engine is `_PacedRun` at **one** worker, which is the serial,
        paced loop this used to spell out inline — shared now with the bulk
        download rather than copied, because two copies of the pacing is how
        one of them quietly stops honouring it.
        """
        job = self._refetch_job
        if job and job.get("state") == "running":
            return
        if (self._download_job or {}).get("state") == "running":
            # One paced resolver, ever. Two of them is two API requests a
            # second between them, which is not the rate either was measured
            # at and not a rate anybody chose.
            self._set_toast("A download is running — [x] on the box stops it")
            return
        plan = self._refetch_candidates()
        total = len(plan["downloads"]) + len(plan["cache"])
        if not total:
            self._set_toast("Nothing to upgrade at this quality")
            return
        tier = self._quality_name
        self._refetch_gen = gen = self._refetch_gen + 1
        self._refetch_job = {
            "state": "running", "tier": tier, "done": 0, "total": total,
            "failed": 0, "error": "",
        }

        def _update(**changes):
            if self._refetch_gen != gen:
                return
            current = dict(self._refetch_job or {})
            current.update(changes)
            self._refetch_job = current
            self._wake()

        def _run():
            outcome = _PacedRun(
                items=([("download", k) for k in plan["downloads"]]
                       + [("cache", k) for k in plan["cache"]]),
                # A re-fetch resolves inside _refetch_one, which is also where
                # the cache half writes its own file — so at one worker the
                # whole of it is still one track at a time, paced
                resolve=lambda item: item,
                fetch=lambda item, _h, _s: self._refetch_one(
                    item[0], item[1], tier, gen),
                alive=lambda: self._refetch_gen == gen,
                report=_update,
                workers=1,
                clock=self._api_pace,
            ).run()
            if outcome is None:
                self._wake()
                return
            done, failed, blocked = outcome
            if blocked:
                _update(state="blocked", error=blocked)
                self._set_toast(
                    "TIDAL is rate-limiting — re-fetch stopped. "
                    "Nothing will be retried.",
                    seconds=PLAYER_ERROR_SECONDS)
            else:
                _update(state="done")
                self._set_toast(
                    f"Re-fetched {done} song{'' if done == 1 else 's'}"
                    f" at {tier}"
                    + (f" · {failed} failed" if failed else ""))
            self._wake()

        threading.Thread(target=_run, daemon=True).start()

    def _refetch_one(self, kind: str, key, tier: str, gen: int) -> None:
        """One track of the re-fetch, in whichever tier it belongs to."""
        def _abandoned():
            if self._refetch_gen != gen:
                raise _DownloadSuperseded()
            return False

        real = self.session.track(int(key)) if str(key).isdigit() \
            else self.session.track(key)
        if real is None:
            raise RuntimeError("track could not be resolved")
        if kind == "download":
            self._download_to_music(real, tier, abandoned=_abandoned)
            return
        self._refetch_into_cache(real, tier, _abandoned)

    def _refetch_into_cache(self, real, tier: str, abandoned) -> None:
        """Replace a cached copy with the same track at `tier`.

        The cache's own writer (`AudioPlayer._start_download`) is tied to a
        playback generation and to admission, neither of which applies here —
        this is a copy the user already has and has explicitly asked to
        refresh, so it is not up for admission and cannot be refused.
        """
        track_id = getattr(real, "id", None)
        if self.audio is None:
            raise RuntimeError("no audio player")
        base = str(self.audio.cache_audio_dir() / str(track_id))
        part = base + ".part"
        url, granted = self._download_stream_url(real, tier)
        sources = stream_sources(url)
        if not sources:
            raise RuntimeError("stream named nothing to fetch")
        ext = fetch_to_file(sources, part, abandoned=abandoned)
        path = base + ext
        os.replace(part, path)
        self.audio._drop_other_copies(base, path)
        self._cache.note_cached(track_id, ext, os.path.getsize(path),
                                quality=granted)
        self._cache.invalidate_audio_count()
        self.audio._sweep_cache()

    def _cancel_refetch(self) -> None:
        """Stop the run. The generation counter is what tells the thread, and
        it is checked inside `fetch_to_file`'s chunk loop, so a cancel lands
        mid-file rather than at the end of a 30 MB one."""
        job = self._refetch_job
        self._refetch_gen += 1
        if job and job.get("state") == "running":
            self._refetch_job = dict(job, state="cancelled", current="")
            self._set_toast("Re-fetch cancelled")

    def _discard_staging(self, track_id) -> None:
        """Remove the half-written file this job was writing, by exact name.

        The only thing ticli ever deletes under the music folder. A finished
        download is never removed, and no directory ever is — ~/Music is the
        user's own, and the rule there is stricter than in the cache, not
        looser.
        """
        try:
            (downloads.download_dir()
             / f".ticli-{track_id}{downloads.PART_SUFFIX}").unlink()
        except OSError:
            pass

    def _promote_cached_copy(self, track_id, tier: str, staging) -> tuple:
        """Copy an existing cached file into the download's staging path.

        Returns `(extension, granted tier)`, or `(None, None)` when there is
        nothing to promote and the bytes have to come off the network. Play a
        hi-res track and then press `[d]` and this is **1 API request and
        ~30 MB saved** — the same bytes are already on the disk.

        Three conditions, and each of them is a way of getting it wrong:

        * the tracker has to know the tier the cached file was **granted**.
          `.m4a` is AAC-HIGH on a device-flow session and FLAC-in-MP4 on a
          PKCE one and the names are identical, so promoting on the strength
          of a filename would quietly install the wrong quality in somebody's
          music folder — which is worse than the re-fetch.
        * it has to be an **exact** match for what this download asked for.
          A cached 320k copy does not satisfy a MAX download; nothing is
          promoted upwards or downwards.
        * the file has to actually be there, checked at the moment of use.

        Anything else falls through to the network, which is the same "the
        index is a hint, the file is the answer" rule `path_for` already uses.
        The copy can lose its source to eviction mid-way; on POSIX the open
        descriptor survives and the copy completes, and if it does not, the
        exception falls through to the network too.
        """
        if not self._cache.keeps_audio:
            return None, None
        wanted = self.QUALITY_MAP.get(tier)
        record = self._cache.audio_record(track_id) or {}
        granted = record.get("quality")
        if not wanted or not granted or granted != wanted:
            return None, None
        source = cached_audio_path(track_id)
        if not source:
            return None, None
        try:
            shutil.copyfile(source, staging)
        except OSError as e:
            logger.debug("Could not promote the cached copy: %s", e)
            return None, None
        return os.path.splitext(source)[1], granted

    @staticmethod
    def _tier_ladder(tier: str) -> list:
        """`tier` and every tier below it, best first."""
        try:
            top = QUALITY_CHOICES.index(tier)
        except ValueError:
            return [tier]
        return list(reversed(QUALITY_CHOICES[:top + 1]))

    def _stream_at_best_tier(self, real, tier: str) -> tuple:
        """`(url, granted, the tier that worked)`, stepping down until one does.

        A tier this login cannot have is not usually an error — TIDAL accepts
        the request and quietly grants something lower, which is why
        `_note_granted_quality` exists at all. But it *can* fail outright: a
        device-flow session asked for a hi-res stream, a region without the
        master, a track pulled from a tier. A download that gives up there
        leaves the user with nothing when a perfectly good CD-quality copy
        was one step down, so this walks the ladder instead.

        **A rate limit is never stepped past.** A 429, or a 401 with subStatus
        4006, means stop — retrying at another tier is three more requests
        into a limiter that is already saying no, and retrying is what turned
        a rate limit into an edge block (ai/INCIDENTS #1). That one re-raises
        immediately; everything else moves down a rung.

        In the common case this is exactly one request, the same as before.
        """
        last = None
        for candidate in self._tier_ladder(tier):
            try:
                url, granted = self._download_stream_url(real, candidate)
            except Exception as e:
                if _looks_rate_limited(str(e)):
                    raise
                logger.debug("No %s stream for this track: %s", candidate, e)
                last = e
                continue
            if stream_sources(url):
                return url, granted, candidate
            last = RuntimeError(f"{candidate} named nothing to fetch")
        raise last or RuntimeError("no tier could be streamed")

    def _download_stream_url(self, track, tier: str) -> tuple:
        """`(url, granted tier)` at a tier that may not be the one playing.
        Network — callers are already off the UI thread.

        tidalapi reads the quality off the session, and there is no per-call
        override, so the setting is moved for the length of one request and
        put back in a `finally`. One assignment of one string, restored
        whatever happens: the worst case is a track starting in the same
        breath being fetched at the download's tier instead of the player's,
        which is a quality difference for one track and not a failure.
        """
        wanted = self.QUALITY_MAP.get(tier)
        previous = self.session.audio_quality
        try:
            if wanted:
                self.session.audio_quality = wanted
            return self._stream_description(track)
        finally:
            self.session.audio_quality = previous

    def _cover_bytes(self, track):
        """The album cover as JPEG, for embedding. None is an ordinary answer.

        A plain unauthenticated GET from resources.tidal.com — the same URL
        the pixel-art cover already uses, so it is not an API request and
        cannot contribute to a rate limit. Any failure at all means a file
        without embedded art, never a failed download.
        """
        cover = artwork.cover_id_of(track)
        if not cover:
            return None
        try:
            response = requests.get(artwork.cover_url(cover), timeout=10)
            response.raise_for_status()
            return response.content
        except Exception as e:
            logger.debug("No cover for the downloaded file: %s", e)
            return None

    def _open_playlist_picker(self):
        """Open the add-to-playlist picker for the targeted track."""
        track = self._target_track_for_picker()
        if track is None:
            self._set_toast("No track selected")
            return
        self._push_nav()
        self._mode = self.MODE_ADD_TO_PLAYLIST
        self._picker_track = track
        self._picker_new_name = None
        # -1 is the "+ New playlist" row, the same way -1 is "Play All" in
        # browse. With nothing to add to, that row is the only one there is.
        self._picker_cursor = 0 if self._editable_playlists else -1
        # Reuse the cached list for a minute — listing re-fetches every playlist
        if not self._editable_playlists or time.time() - self._editable_playlists_time > 60:
            self._picker_loading = True

            def _run():
                try:
                    playlists = self.session.user.playlists()
                    self._editable_playlists = [
                        p for p in (playlists or []) if isinstance(p, tidalapi.UserPlaylist)
                    ]
                    self._editable_playlists_time = time.time()
                    if not self._editable_playlists:
                        self._picker_cursor = -1
                except Exception:
                    pass
                finally:
                    self._picker_loading = False

            threading.Thread(target=_run, daemon=True).start()

    def _picker_add_to(self, playlist):
        """Add the picked track to a playlist (network in background)."""
        if self._picker_busy:
            return
        track = self._picker_track
        self._picker_busy = True
        self._go_back()

        def _run():
            try:
                added = playlist.add([str(track.id)])  # server skips duplicates
                # Pinned before the toast, so "the toast appeared" implies the
                # pin is already on disk — both for the user and for the tests
                self._remember_last_playlist(playlist)
                if added:
                    self._set_toast(f'Added to "{playlist.name}"')
                else:
                    self._set_toast(f'Already in "{playlist.name}"')
            except Exception:
                self._set_toast("Failed to add to playlist")
            finally:
                self._picker_busy = False
            self._wake()

        threading.Thread(target=_run, daemon=True).start()

    def _picker_create_and_add(self, name: str):
        """Create a playlist and put the picked track in it, in one gesture.

        Two requests on a background thread, and they fail separately: a
        creation that worked followed by an add that didn't is not "failed to
        create a playlist", and saying so would send the user looking for a
        playlist that is already there.
        """
        if self._picker_busy:
            return
        name = (name or "").strip()
        if not name:
            # Nothing is created for an empty name, and the prompt stays open
            # rather than closing as if something had happened
            self._set_toast("Playlist name can't be empty")
            return
        track = self._picker_track
        # Set before anything else: a second Enter arriving on the same tick
        # has to find this already true, or the playlist is created twice
        self._picker_busy = True
        self._picker_new_name = None
        self._go_back()

        def _run():
            try:
                playlist = self.session.user.create_playlist(name, "")
            except Exception:
                self._set_toast(f'Failed to create "{name}"')
                self._picker_busy = False
                self._wake()
                return
            # Created, so it is the last one used whatever the add does next
            self._remember_last_playlist(playlist)
            # Whole-object assignment, never an insert into the shared list
            pid = str(getattr(playlist, "id", "") or "")
            self._editable_playlists = [playlist] + [
                p for p in self._editable_playlists
                if not pid or str(getattr(p, "id", "") or "") != pid
            ]
            try:
                playlist.add([str(track.id)])
                self._set_toast(f'Created "{name}" and added track')
            except Exception:
                self._set_toast(f'Created "{name}", but failed to add track')
            finally:
                self._picker_busy = False
            self._wake()

        threading.Thread(target=_run, daemon=True).start()

    def _setting_ceiling(self, spec: dict) -> int:
        """A row's usable maximum. Normally the spec's, except for volume,
        where it is whatever the running backend can really reach.

        With no backend yet, unity — the value every backend can do. Erring
        high here would let a config be saved that the audio player then
        quietly reinterprets, which is the exact drift this exists to stop.
        """
        if spec["key"] != "volume":
            return spec["max"]
        if not self.audio:
            return min(spec["max"], SAFE_VOLUME_CEILING)
        try:
            ceiling = int(self.audio.volume_ceiling())
        except Exception:
            # For any reason at all: a backend that can't answer is not a
            # licence to amplify, and a settings row is never worth a crash
            ceiling = SAFE_VOLUME_CEILING
        return max(spec["min"], min(spec["max"], ceiling))

    def _clamp_volume_to_backend(self):
        """Bring the saved volume inside what the running backend can do.

        Called once the backend is known, because the two are chosen
        independently: a config written while mpv was installed is read on the
        next run whether or not mpv is still there. The clamp is written back
        to disk as well as applied, so the number on the settings page, the
        number in the file and the number the user hears are one number.
        """
        spec = get_spec("volume")
        ceiling = self._setting_ceiling(spec)
        wanted = coerce(spec, self.config.get("volume", spec["default"]))
        allowed = min(wanted, ceiling)
        if allowed != self.config.get("volume"):
            self.config["volume"] = allowed
            save_config(self.config)
        if self.audio:
            self.audio.set_volume(allowed)

    def _set_setting(self, spec: dict, value):
        """Write one setting: apply it live, then persist it."""
        current = self.config.get(spec["key"], spec["default"])
        if value == current:
            return  # a number clamped at its bound — nothing to write
        self.config[spec["key"]] = value
        self._apply_setting(spec["key"], value)
        # The file is <1 KB; writing inline keeps the setting safe from a crash
        # and is invisible at the 4fps render loop
        save_config(self.config)

    def _change_setting(self, step: int):
        """Move the selected setting one step, apply it live, and persist it."""
        self._adjust_setting(SETTINGS_ROWS[self._settings_cursor], step)

    def _adjust_setting(self, spec: dict, step: int):
        """One step of one setting, whichever surface asked for it — the
        settings page's ←/→, or the [v] overlay's. Shared rather than
        reimplemented so the clamp against the backend's real ceiling can
        never apply to one of them and not the other."""
        current = self.config.get(spec["key"], spec["default"])
        value = cycle_value(spec, current, step)
        if spec["kind"] == "int":
            value = min(value, self._setting_ceiling(spec))
        if spec["key"] == "cache_songs" and value is False:
            # Ask what happens to the files already there. Nothing changes yet
            self._disable_songs_pending = True
            return
        self._set_setting(spec, value)

    def _begin_setting_edit(self, digit: str) -> bool:
        """Start typing a number into the selected row. False if that row isn't
        a number — a digit on a choice row means nothing."""
        if SETTINGS_ROWS[self._settings_cursor]["kind"] != "int":
            return False
        self._settings_edit = digit
        return True

    def _commit_setting_edit(self):
        """Leave the textbox. Whatever is in it is saved and applied right
        there — there is no separate confirm step, so Esc and Enter and
        arrowing away all mean the same thing. An empty box reverts (typing
        nothing must not be read as zero), and anything out of range clamps
        to the row's bounds."""
        typed, self._settings_edit = self._settings_edit, None
        if not typed:
            return
        spec = SETTINGS_ROWS[self._settings_cursor]
        value = min(coerce(spec, int(typed)), self._setting_ceiling(spec))
        self._set_setting(spec, value)

    def _clear_cached_songs(self):
        """Delete the cached tracks and say what really happened.

        Inline, not on a thread: a full 2 GB budget is on the order of a
        hundred unlinks. A track playing from one of these files keeps
        playing — the file is unlinked, not closed (and if the player had not
        opened it yet, _monitor_playback restarts it from the network).
        """
        removed, kept = self._cache.clear_audio()
        if kept:
            # Honest rather than silent: on Windows a file open without
            # delete-sharing cannot be removed at all
            self._set_toast(f"Cleared {removed} songs, {kept} still in use")
        else:
            self._set_toast(f"Cleared {removed} song{'' if removed == 1 else 's'}")

    def _apply_setting(self, key: str, value):
        """Apply a setting to the running player. Sizes take effect on the next
        render; quality takes effect on the next track (get_url sends it)."""
        if key == "quality":
            self._quality_name = value
            self.session.audio_quality = self.QUALITY_MAP[value]
        elif key == "page_size":
            self._page_size = value
        elif key == "progress_bar_max":
            self._bar_max = value
        elif key == "show_artwork":
            self._show_artwork = value
            if not value:
                # Forget both the picture and the request that produced it, so
                # turning it back on re-asks rather than showing a stale cover
                self._artwork = None
                self._artwork_request = None
        elif key == "volume":
            if self.audio:
                self.audio.set_volume(value)
        elif key == "cache_metadata":
            self._cache.metadata = value
            if not value:
                # Turning it off has to mean the disk is empty, not just unread
                self._cache.clear_metadata()
        elif key == "cache_songs":
            # Only stops keeping new ones. What is already on disk is the
            # user's to keep or clear — see the prompt in _handle_key
            self._cache.songs = value
            if value:
                # Nothing is downloaded yet, but the count on screen has to
                # match the directory either way
                self._cache.invalidate_audio_count()
                self._cache.enforce_budget()
        elif key == "cache_budget_gb":
            self._cache.budget_gb = value
            # Lowering the budget evicts right now, not at some later write
            self._cache.enforce_budget()

    # ── Key handlers ──

    def _handle_key(self, key: str):
        # `[h]`, before everything — including the confirmations, which return
        # early, and the volume overlay, which would otherwise swallow the key
        # that puts the footer back.
        #
        # `h` means one of two things depending on where you are. On the player
        # screen it hides the footer, tucking the controls away. On a list or
        # menu screen it means the opposite direction of travel — dismiss the
        # menu and drop back to the player (now-playing) screen; there is no
        # footer worth hiding on a menu, and "take me home" is the useful thing
        # `h` can do there. The two are mutually exclusive by screen, so only
        # one of the branches below ever fires. Typing screens and overlays get
        # neither: there `h` is the letter h, or the key belongs to a box.
        #
        # Un-hiding does *not* consume the key: `s` restores the footer and
        # opens search, in that order, because a key that has to be pressed
        # twice is worse than a footer that came back a moment early. That is
        # the opposite of scrub focus, which consumes ←/→ deliberately — there
        # the two meanings are in competition and only one of them can win;
        # here nothing is competing, since `h` is the only key this feature
        # binds and no screen binds it for anything else. (Un-hiding is a
        # player-screen affair only, since that is the only screen the footer
        # ever hides on now.)
        #
        # Any key that is not an arrow or the spacebar un-hides, including one
        # this mode does nothing with. The alternative — un-hide only on a key
        # that is bound here — needs a second copy of the mode dispatch to say
        # which those are, and that copy is exactly what 609e423 deleted from
        # the footer; it would go stale the first time a binding was added. It
        # is also the better behaviour: a key that did nothing is precisely the
        # moment the cheat-sheet is worth having back.
        if self._footer_hidden:
            if key not in HIDE_HOLD_KEYS:
                self._footer_hidden = False
        elif key == "h" and self._can_hide_footer():
            self._footer_hidden = True
            return
        elif key == "h" and self._can_exit_menu_to_player():
            self._exit_to_player()
            return

        # Handle quit confirmation first
        if self._quit_pending:
            if key == KEY_ESC:
                self.running = False
            else:
                self._quit_pending = False
            return

        # Handle logout confirmation
        if self._logout_pending:
            if key == "y" or key == "Y":
                self._logout()
            self._logout_pending = False
            return

        # Three answers, because disabling and clearing are different things:
        # yes stops caching and deletes, no stops caching and keeps the files,
        # Esc (or anything unrecognised) leaves the setting alone entirely
        if self._disable_songs_pending:
            self._disable_songs_pending = False
            if key in ("y", "Y", KEY_ENTER, KEY_ENTER2):
                self._set_setting(get_spec("cache_songs"), False)
                self._clear_cached_songs()
            elif key in ("n", "N"):
                self._set_setting(get_spec("cache_songs"), False)
            return

        if self._clear_cache_pending:
            self._clear_cache_pending = False
            if key in ("y", "Y", KEY_ENTER, KEY_ENTER2):
                self._clear_cached_songs()
            return

        # Nothing is unlinked until this is answered, and cancel is a true
        # no-op: the row is dropped and neither the file nor the index has
        # been touched, rather than a delete that gets put back.
        if self._downloads_delete is not None:
            if key in ("y", "Y", KEY_ENTER, KEY_ENTER2):
                self._delete_download()
            else:
                self._downloads_delete = None
            return

        # Nothing is fetched until this is answered, and cancel is a true
        # no-op — the same shape as every other confirmation here
        if self._refetch_pending:
            self._refetch_pending = False
            plan = self._refetch_plan
            self._refetch_plan = None
            if key in ("y", "Y", KEY_ENTER, KEY_ENTER2) and plan and (
                    plan["downloads"] or plan["cache"]):
                self._start_refetch_job()
            return

        # The volume overlay is over whatever mode is underneath — including
        # the player holding the arrows for scrubbing — so it takes the key
        # before anything else and gives none of them back until it closes.
        if self._volume_open:
            self._handle_volume_key(key)
            return

        # The download box is the other overlay, and it is modal for the same
        # reason the volume one is not: it is a confirmation with a [d] and an
        # [Esc], and a key falling through it would act on a screen the box is
        # covering. It is checked after volume only because volume can be over
        # anything — the two are never up at once (_can_open_volume).
        if self._download_open:
            self._handle_download_key(key)
            return

        # Scrub focus comes next, and `v` reaches it as "anything else": focus
        # drops and the key falls through to open the overlay. That is the
        # right way round, because the two are competing for the same arrows —
        # scrubbing while a volume bar is on screen would leave ←/→ meaning two
        # things at once. Read before, because _handle_focus_key is what drops
        # it and the overlay needs to know it is holding them on loan.
        was_focused = self._player_focus
        if self._handle_focus_key(key):
            return

        if key in ("v", "V") and self._can_open_volume():
            self._open_volume(from_focus=was_focused)
            return

        if self._mode == self.MODE_SEARCH:
            self._handle_search_key(key)
        elif self._mode == self.MODE_BROWSE:
            self._handle_browse_key(key)
        elif self._mode == self.MODE_ARTIST:
            self._handle_artist_key(key)
        elif self._mode == self.MODE_QUEUE:
            self._handle_queue_key(key)
        elif self._mode == self.MODE_PLAYLISTS:
            self._handle_playlists_key(key)
        elif self._mode == self.MODE_ADD_TO_PLAYLIST:
            self._handle_add_to_playlist_key(key)
        elif self._mode == self.MODE_SETTINGS:
            self._handle_settings_key(key)
        elif self._mode == self.MODE_DOWNLOADS:
            self._handle_downloads_key(key)
        else:
            self._handle_player_key(key)

    def _focus_player(self):
        """Give the player the arrow keys. \u2191 is what asks for it: from the top
        of a list, where \u2191 had nowhere left to go, and from the player screen,
        where it did nothing at all. Pointless with no track to scrub."""
        if self._current_track is not None:
            self._player_focus = True

    def _exit_to_player(self) -> None:
        """`h` from a menu: put the list away and show the player. The whole
        navigation stack goes with it \u2014 this is "take me home", not one step
        back \u2014 and the list state each screen keeps in its own attributes is
        still there for [q]/[p]/[c] to reopen."""
        self._mini_player = False
        self._player_focus = False
        self._mode = self.MODE_PLAYER
        self._nav_history.clear()

    def _handle_focus_key(self, key: str) -> bool:
        """Keys that belong to the player while it has focus. True when the key
        was used up here.

        This is the whole of the seeking interaction, and it is one rule in one
        place rather than a branch per screen: focused, \u2190/\u2192 scrub and \u2193 (or
        Esc) hands the arrows back. Anything else \u2014 typing a query, [t], Enter,
        [v] \u2014 means the user is done scrubbing, so focus drops and the key goes
        on to the screen it was meant for. Nothing is stolen: unfocused, every
        key means exactly what it always did, including \u2190/\u2192 for prev/next.
        """
        if not self._player_focus:
            return False
        if key == KEY_RIGHT:
            self._seek_by(SEEK_STEP_SECONDS)
            return True
        if key == KEY_LEFT:
            self._seek_by(-SEEK_STEP_SECONDS)
            return True
        if key in (" ", "k"):
            self._toggle_play_key()
            return True
        if key == KEY_UP:
            return True  # already as far up as focus goes
        self._player_focus = False
        # The two keys that mean "give the list back" and nothing more; every
        # other key falls through to the screen underneath, now unfocused
        return key in (KEY_DOWN, KEY_ESC)

    def _can_open_volume(self) -> bool:
        """Whether `v` means "volume" right now.

        Everywhere except the three places where it means the letter v \u2014 a
        search query, which types every printable key it gets; a settings row
        being typed into; and the picker's new-playlist name \u2014 and under the
        download box, which is modal, so two overlays are never up at once.
        Nothing else binds v: the player screen, browse, the artist page, the
        queue, playlists, the picker's list and the settings page were all
        checked, and none of them wanted it.
        """
        return (self._mode != self.MODE_SEARCH
                and not self._download_open
                and self._settings_edit is None
                and self._picker_new_name is None)

    def _can_hide_footer(self) -> bool:
        """Whether `h` means "put the controls away" right now.

        Only on the player screen. Everywhere else `h` either types the
        letter (a search query, a settings row, the picker's new-playlist
        name) or dismisses the menu back to the player — see
        _can_exit_menu_to_player. The mini player is one line by design and
        the volume overlay has taken the footer's rows, so neither has a
        footer to hide; the download box is modal and owns the key.

        Only hiding is gated. Un-hiding is not: whatever screen you end up
        on, the next thing that isn't an arrow or the spacebar brings it
        back.
        """
        return (self._mode == self.MODE_PLAYER
                and not self._mini_player
                and not self._volume_open
                and not self._download_open)

    def _can_exit_menu_to_player(self) -> bool:
        """Whether `h` means "dismiss this menu and show the player" now.

        Every list screen except the player itself, and never while a key
        that is really the letter h is being typed — a search query, a
        settings row mid-edit, the picker's new-playlist name. The overlays
        (volume, the download box) and the mini player stand down for the
        same reasons they do for hiding the footer, and a pending
        confirmation keeps the key so `h` cannot answer a yes/no box by
        accident.
        """
        if self._mode in (self.MODE_PLAYER, self.MODE_SEARCH):
            return False
        if self._mini_player or self._volume_open or self._download_open:
            return False
        if self._settings_edit is not None or self._picker_new_name is not None:
            return False
        if (self._quit_pending or self._logout_pending
                or self._disable_songs_pending or self._clear_cache_pending
                or self._refetch_pending or self._downloads_delete is not None):
            return False
        return True

    def _open_volume(self, from_focus: bool = False):
        """Open the overlay, recording whether the player had the arrows.

        It usually has not, and then this is a bool that stays False. When it
        has, the arrows are on loan rather than taken: you were scrubbing, you
        turned it down, and Enter puts you back where you were instead of
        making you press \u2191 again. The caller passes it in because by the time
        we get here `_handle_focus_key` has already dropped the focus.
        """
        self._volume_from_focus = from_focus
        self._player_focus = False
        self._volume_open = True

    def _close_volume(self):
        self._volume_open = False
        if self._volume_from_focus:
            self._volume_from_focus = False
            self._focus_player()

    def _handle_volume_key(self, key: str):
        """The overlay's keys. Arrows move the level by the same step the
        settings row moved it by (the spec's), Enter and Esc close it, and v
        closes it too so the key that opened it also puts it away. Play/pause
        keeps working, because reaching for the volume is not a reason to lose
        the spacebar. Anything else is ignored rather than acted on
        underneath \u2014 an overlay that swallowed a `q` and opened the queue
        would be worse than one that did nothing."""
        if key in (KEY_ENTER, KEY_ENTER2, KEY_ESC, "v", "V"):
            self._close_volume()
            return
        spec = get_spec("volume")
        if key in (KEY_RIGHT, KEY_UP):
            self._adjust_setting(spec, 1)
        elif key in (KEY_LEFT, KEY_DOWN):
            self._adjust_setting(spec, -1)
        elif key in (" ", "k"):
            self._toggle_play_key()

    def _handle_player_key(self, key: str):
        if key in (" ", "k"):
            self._toggle_play_key()
        elif key == "n" or key == KEY_RIGHT:
            self._next_track()
        elif key == KEY_LEFT:
            self._prev_track()
        elif key == KEY_UP:
            # No list here, so ↑ has always been a no-op: it is where scrubbing
            # goes, and ←/→ keep meaning prev/next until it is pressed
            self._focus_player()
        elif key == "s":
            self._mini_player = False
            self._mode = self.MODE_SEARCH
            self._search_query = ""
            # A fresh search starts in the scope you'd expect, not in whatever
            # one you last tabbed to half an hour ago — and with focus in the
            # query box, not on a row some earlier visit left highlighted
            self._search_history_cursor = None
            self._search_filter = "all"
            self._reset_search_results()
            self._nav_history.clear()
        elif key == "t":
            self._mini_player = not self._mini_player
        elif key == "m":
            self._show_more = not self._show_more
        # Commands below are in the "more" menu but still work even when hidden
        elif key == "l":
            self._toggle_like()
        elif key == "r":
            self._start_track_radio()
        elif key == "y":
            self._open_playlist_picker()
        elif key == "d":
            self._open_download()
        elif key == "q":
            self._mini_player = False
            self._mode = self.MODE_QUEUE
            self._queue_cursor = self._queue_index if self._queue else 0
            self._nav_history.clear()
        elif key == "p":
            self._mini_player = False
            self._mode = self.MODE_PLAYLISTS
            self._nav_history.clear()
            if not self._playlists and not self._playlists_loading:
                self._load_playlists()
        elif key == "c":
            self._mini_player = False
            self._mode = self.MODE_SETTINGS
            self._settings_cursor = 0
            self._settings_edit = None
            # Either tier's numbers could have moved since the page was last
            # open — including by a deletion made outside ticli, which is the
            # only way that is ever noticed
            self._cache.invalidate_audio_count()
            self._forget_downloads()
            self._nav_history.clear()
        elif key == KEY_ESC:
            self._quit_pending = True

    def _handle_search_key(self, key: str):
        if key == KEY_ESC or key == KEY_LEFT:
            self._go_back()
            return
        if key == " " and self._search_results:
            # Space toggles play/pause when browsing search results
            self._toggle_play_key()
            return
        if key == KEY_TAB:
            self._cycle_search_filter(1)
            return
        if key == KEY_SHIFT_TAB:
            self._cycle_search_filter(-1)
            return
        history = self._history_rows()
        if key == KEY_UP:
            if self._search_results and self._search_cursor > 0:
                self._search_cursor -= 1
            elif history and self._search_history_cursor is not None:
                # From row 0 ↑ leaves the list for the query box; only the
                # press after that hands the arrows to the player
                self._search_history_cursor = (
                    self._search_history_cursor - 1
                    if self._search_history_cursor > 0 else None)
            else:
                self._focus_player()
        elif key == KEY_DOWN:
            if self._search_results:
                if self._search_cursor < len(self._search_results) - 1:
                    self._search_cursor += 1
                else:
                    # The bottom of the list is where the next page comes from.
                    # The cursor stays put: rows are appended below it, so it
                    # never jumps, and the next press walks into them.
                    self._search_more()
            elif history:
                # ↓ from the query box enters the recall list at the top;
                # the bottom is the bottom — history has no next page
                self._search_history_cursor = (
                    0 if self._search_history_cursor is None
                    else min(self._search_history_cursor + 1, len(history) - 1))
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            if self._search_results:
                self._select_search_result()
            elif (history and self._search_history_cursor is not None
                    and key != KEY_RIGHT):
                # A remembered query runs exactly like a retyped one — through
                # _do_search, which puts it back on top of the history
                self._search_query = history[self._search_history_cursor]
                self._search_history_cursor = None
                self._do_search()
            elif key != KEY_RIGHT:
                self._do_search()
        elif key in (KEY_BACKSPACE, KEY_BACKSPACE2):
            if history and self._search_history_cursor is not None:
                # On a highlighted row Backspace deletes the memory, not the
                # (empty) query. Reassigned, never mutated, like every list a
                # paint might be walking. The cursor stays where the eye is —
                # on the row that slid up — and an emptied list hands focus
                # back to the query box.
                idx = self._search_history_cursor
                self._search_history = (
                    self._search_history[:idx] + self._search_history[idx + 1:])
                remaining = self._history_rows()
                self._search_history_cursor = (
                    min(idx, len(remaining) - 1) if remaining else None)
            else:
                self._search_query = self._search_query[:-1]
                self._search_history_cursor = None
                self._reset_search_results()
        elif len(key) == 1 and key.isprintable():
            self._search_query += key
            self._search_history_cursor = None
            self._reset_search_results()

    def _handle_browse_key(self, key: str):
        if key == KEY_ESC or key == KEY_LEFT:
            self._go_back()
            return
        if key == " ":
            self._toggle_play_key()
            return
        if key == KEY_UP:
            if self._browse_tracks and self._browse_cursor > -1:
                self._browse_cursor -= 1
            else:
                self._focus_player()
        elif key == KEY_DOWN:
            if self._browse_tracks:
                self._browse_cursor = min(len(self._browse_tracks) - 1, self._browse_cursor + 1)
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            if self._browse_cursor == -1:
                self._play_all_browse()
            else:
                self._play_browse_track()
        elif key == "a":
            self._play_all_browse()
        elif key == "y":
            self._open_playlist_picker()
        elif key == "d":
            self._open_download()
        elif key == "D":
            self._open_download(whole=True)
        elif key == "x":
            self._remove_from_browse_playlist()

    def _handle_artist_key(self, key: str):
        """Tab is the only key the artist page adds. ↑ from the top still hands
        the arrows to the player for scrubbing, ←/Esc still goes back, and Tab
        works while a section is loading or failed — there is no state this
        page can be in that you cannot leave."""
        if key == KEY_ESC or key == KEY_LEFT:
            self._go_back()
            return
        if key == " ":
            self._toggle_play_key()
            return
        if key == KEY_TAB:
            self._cycle_artist_section(1)
            return
        if key == KEY_SHIFT_TAB:
            self._cycle_artist_section(-1)
            return
        rows = self._artist_rows()
        if key == KEY_UP:
            if rows and self._artist_cursor > 0:
                self._artist_cursor -= 1
            else:
                self._focus_player()
        elif key == KEY_DOWN:
            if rows:
                self._artist_cursor = min(len(rows) - 1, self._artist_cursor + 1)
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            self._select_artist_row()
        elif key == "a":
            self._play_all_artist()
        elif key == "y":
            self._open_playlist_picker()
        elif key == "d":
            self._open_download()
        elif key == "D":
            self._open_download(whole=True)

    def _handle_queue_key(self, key: str):
        if key == KEY_ESC or key == KEY_LEFT:
            self._mode = self.MODE_PLAYER
            return
        if key == " ":
            self._toggle_play_key()
            return
        if key == KEY_UP:
            if self._queue and self._queue_cursor > 0:
                self._queue_cursor -= 1
            else:
                self._focus_player()
        elif key == KEY_DOWN:
            if self._queue:
                self._queue_cursor = min(len(self._queue) - 1, self._queue_cursor + 1)
        elif key in (KEY_ENTER, KEY_ENTER2):
            if self._queue:
                self._play_queue_index(self._queue_cursor)
        elif key == "x":
            self._remove_from_queue()
        elif key == "y":
            self._open_playlist_picker()
        elif key == "d":
            self._open_download()
        elif key == "D":
            self._open_download(whole=True)

    def _handle_playlists_key(self, key: str):
        if key == KEY_ESC or key == KEY_LEFT:
            self._mode = self.MODE_PLAYER
            return
        if key == " ":
            self._toggle_play_key()
            return
        if key == KEY_UP:
            if self._playlists and self._playlists_cursor > 0:
                self._playlists_cursor -= 1
            else:
                self._focus_player()
        elif key == KEY_DOWN:
            if self._playlists:
                self._playlists_cursor = min(len(self._playlists) - 1, self._playlists_cursor + 1)
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            if self._playlists:
                self._open_playlist(self._playlists[self._playlists_cursor])

    def _handle_add_to_playlist_key(self, key: str):
        # While a name is being typed the picker is a textbox, so this runs
        # before anything else and swallows every key it doesn't use: Space
        # types a space rather than pausing, and an arrow must not navigate a
        # list the user cannot see the cursor on.
        if self._picker_new_name is not None:
            if key == KEY_ESC:
                self._picker_new_name = None  # nothing was created
            elif key in (KEY_BACKSPACE, KEY_BACKSPACE2):
                self._picker_new_name = self._picker_new_name[:-1]
            elif key in (KEY_ENTER, KEY_ENTER2):
                self._picker_create_and_add(self._picker_new_name)
            elif len(key) == 1 and key.isprintable():
                if len(self._picker_new_name) < PLAYLIST_NAME_MAX:
                    self._picker_new_name = self._picker_new_name + key
            return
        if key == KEY_ESC or key == KEY_LEFT:
            self._go_back()
            return
        if key == " ":
            self._toggle_play_key()
            return
        playlists = self._picker_playlists()
        if key == KEY_UP:
            self._picker_cursor = max(-1, self._picker_cursor - 1)
        elif key == KEY_DOWN:
            self._picker_cursor = min(len(playlists) - 1, self._picker_cursor + 1)
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            if self._picker_cursor < 0:
                self._picker_new_name = ""
            elif playlists and self._picker_cursor < len(playlists):
                self._picker_add_to(playlists[self._picker_cursor])

    def _handle_download_key(self, key: str):
        """The box's keys, and it is modal: it takes every key and passes
        none of them to the screen underneath, so nothing can be triggered
        by a keystroke aimed at a box that is in the way.

        `d` starts the download as well as `Enter` — the key that opened the
        box also completes it, which is the whole of "d does exactly what's
        expected". ↑/↓ pick the tier and change the number above it, and
        both cost nothing at all. ↑ at the top does *not* hand the arrows to
        the player, the same as the add-to-playlist picker.
        """
        if key in (KEY_ESC, KEY_LEFT):
            self._download_open = False
            return
        if key == " ":
            self._toggle_play_key()
            return
        if key in ("x", "X"):
            self._cancel_download()
            return
        if key == KEY_UP:
            self._download_cursor = max(0, self._download_cursor - 1)
        elif key == KEY_DOWN:
            self._download_cursor = min(len(QUALITY_CHOICES) - 1,
                                        self._download_cursor + 1)
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT, "d", "D"):
            if self._download_bulk():
                # The box stays up: it is where the bars are, and a run with
                # no visible progress is the janky version of this
                self._start_bulk_download_job(self._download_tier())
            else:
                self._start_download_job(self._download_tier())
                # ...and it stays up when one track joined a run, for the
                # same reason: the bars are the answer to what just happened
                if self._download_run is None:
                    self._download_open = False

    def _handle_settings_key(self, key: str):
        # Typing a number into a row: digits extend it, Backspace shortens it,
        # and anything that means "leave" saves what is there on the way out
        if self._settings_edit is not None:
            if key.isdigit():
                # Four digits is more than any row's ceiling needs
                if len(self._settings_edit) < 4:
                    self._settings_edit = self._settings_edit + key
                return
            if key in (KEY_BACKSPACE, KEY_BACKSPACE2):
                self._settings_edit = self._settings_edit[:-1]
                return
            self._commit_setting_edit()
            # Esc left the textbox, not the page — a second Esc leaves the page
            if key == KEY_ESC or key in (KEY_ENTER, KEY_ENTER2, KEY_LEFT, KEY_RIGHT):
                return
            # Up/Down fall through: committing and moving on is one gesture
        elif key.isdigit() and self._begin_setting_edit(key):
            return

        # ← is the value-decrease key here (the universal settings idiom), so
        # Esc — or the "c" that opened the page — is what goes back
        if key == KEY_ESC or key == "c":
            # Esc stops a running re-fetch rather than leaving the page, so
            # the key that starts the one long job on this screen is also the
            # key that stops it and you never have to leave to do it
            if (self._refetch_job or {}).get("state") == "running":
                self._cancel_refetch()
                return
            self._mode = self.MODE_PLAYER
            return
        if key == " ":
            self._toggle_play_key()
            return
        if key in ("o", "O"):
            self._logout_pending = True
            return
        # Clearing the cache is an action, not a value, so it lives outside
        # SETTINGS_SPEC as a keybinding — the same reason logout does. While a
        # download is running [x] stops it instead, which is what [x] means on
        # every other screen: the Cache row drops its own `[x] clear` for the
        # duration rather than advertising a key it is not getting, and
        # clearing the cache out from under a download is not a thing anybody
        # should do by accident.
        if key in ("x", "X"):
            if (self._download_job or {}).get("state") == "running":
                self._cancel_download()
                return
            self._clear_cache_pending = True
            return
        # Deliberately not "p" — that already opens playlists on the player
        # screen, and one letter meaning two things is how muscle memory breaks
        if key in ("u", "U"):
            self._upgrade_to_pkce()
            return
        # The Downloads row's own action, beside the count it explains. `d`
        # rather than a new letter: it is the key that means "downloads"
        # everywhere else in the app, and there is nothing on this page to
        # download so the two cannot be confused.
        if key in ("d", "D"):
            self._open_downloads()
            return
        # Re-fetch everything at the current quality. An action, so it lives
        # outside SETTINGS_SPEC like [x] and [o]; and it asks first, because
        # it is the one thing here that deliberately makes hundreds of
        # requests. Reading the plan is two index reads and no network.
        if key in ("r", "R"):
            if (self._refetch_job or {}).get("state") == "running":
                return
            self._refetch_plan = self._refetch_candidates()
            self._refetch_pending = True
            return
        if key == KEY_UP:
            self._settings_cursor = max(0, self._settings_cursor - 1)
        elif key == KEY_DOWN:
            self._settings_cursor = min(len(SETTINGS_ROWS) - 1, self._settings_cursor + 1)
        elif key == KEY_LEFT:
            self._change_setting(-1)
        elif key in (KEY_RIGHT, KEY_ENTER, KEY_ENTER2):
            self._change_setting(1)

    def _open_downloads(self):
        """Show what is in the music folder. One re-measurement, no network.

        The folder is the one thing on this page that changes without ticli
        (a file dragged to the trash in Finder), so opening the list is one of
        the moments the memo is dropped — the same rule opening the settings
        page follows.
        """
        self._push_nav()
        self._mode = self.MODE_DOWNLOADS
        self._downloads_cursor = 0
        self._forget_downloads()

    def _handle_downloads_key(self, key: str):
        if key == KEY_ESC or key == KEY_LEFT:
            self._go_back()
            return
        if key == " ":
            self._toggle_play_key()
            return
        rows = self._download_rows()
        if key == KEY_UP:
            if self._downloads_cursor <= 0:
                # The top of a list is where ↑ hands the arrows to the player
                # for scrubbing, on this screen as on every other one
                self._focus_player()
            else:
                self._downloads_cursor -= 1
            return
        if key == KEY_DOWN:
            self._downloads_cursor = min(len(rows) - 1, self._downloads_cursor + 1)
            return
        if key in ("x", "X") and rows:
            # Only remembered here. Nothing is unlinked until _handle_key sees
            # the answer.
            self._downloads_delete = rows[min(self._downloads_cursor,
                                              len(rows) - 1)]

    # ── Main loop ──

    def _read_keys(self, select_mod, timeout=IDLE_POLL_SECONDS):
        """Block until input arrives (or the idle timeout), then return every
        key already buffered.

        Taking the whole burst in one pass is what makes held arrow keys
        scroll smoothly — repeat arrives faster than one key per loop turn.
        """
        watch = [sys.stdin]
        if self._wake_r is not None:
            watch.append(self._wake_r)
        ready = select_mod.select(watch, [], [], timeout)[0]
        if self._wake_r is not None and self._wake_r in ready:
            # Drain the whole backlog: many wakes still mean one repaint
            try:
                os.read(self._wake_r, 4096)
            except OSError:
                pass
        if sys.stdin not in ready:
            return []
        data = os.read(sys.stdin.fileno(), 1024)
        if not data:
            return []
        text = data.decode("utf-8", errors="ignore")
        if _incomplete_escape(text) and select_mod.select([sys.stdin], [], [], ESC_TAIL_SECONDS)[0]:
            text += os.read(sys.stdin.fileno(), 16).decode("utf-8", errors="ignore")
        return _split_keys(text)

    def _wake(self):
        """Ask the main loop to repaint now. Safe from any thread, and a
        no-op before the loop starts (or if the pipe couldn't be made) —
        the idle tick still picks the change up, just later."""
        if self._wake_w is None:
            return
        try:
            os.write(self._wake_w, b"\x01")
        except OSError:
            pass

    def _repaint(self, live, force=False):
        """Push the current UI to the terminal.

        Rich's Live only swaps the renderable on update(); the pixels change
        on its own refresh thread, so a keypress could sit up to a quarter
        second before anything moved. Painting inline with refresh=True makes
        input land immediately. On an idle tick the write is skipped when the
        render is identical to what is already on screen, so a player nobody
        is touching costs one cheap render per poll and no terminal traffic.
        """
        display = self._build_display()
        key = (self.console.size, tuple(self.console.render(display, self.console.options)))
        if not force and key == self._last_segments:
            return
        self._last_segments = key
        live.update(display, refresh=True)

    def _make_live(self) -> "Live":
        """The session's one Live display.

        `screen=True` — the alternate screen buffer — is load-bearing, not
        cosmetic. Without it Rich repaints by counting: it walks the cursor
        up exactly as many rows as the last frame was tall, erasing each one,
        and prints the new frame over the top. That accounting is only true
        while the terminal has not moved underneath it. Resize the window and
        both halves of it are wrong at once — the old frame's lines reflow or
        clip to a width they were not laid out for, and a window that got
        shorter has already scrolled part of that frame into the scrollback
        where no cursor-up can reach it. What is left over stays on screen
        forever, and the next repaint strands another one above it: the
        reported artifact was eight bands of album art and three stacked
        `Ticli` panels showing three different timestamps.

        On the alternate screen there is no accounting to get wrong. Every
        refresh homes the cursor and writes every row of the terminal, so a
        frame cannot be stranded by anything — a resize, a frame that shrank
        (full player to mini), artwork that appeared or changed size. It also
        means the player no longer scribbles on the scrollback it was
        launched from: on exit the terminal is handed back exactly as it was,
        the way `less` and `htop` do it.
        """
        return Live(
            self._build_display(),
            console=self.console,
            # auto_refresh off: repaints are driven by _repaint, immediately
            # after input and otherwise only when the screen actually changed
            auto_refresh=False,
            screen=True,
        )

    def run(self):
        """Start the headless player."""
        # One ticli at a time, decided before anything else touches the
        # config directory, the cache tracker or the audio backend. Two
        # instances is the other way the owner's "a primary song playing and
        # another song playing at that same time" happens (INCIDENTS #7 is
        # the in-process route), and they also take turns overwriting the
        # same saved position — BUGS-2026-07-24 item 8, the half that the
        # atomic write did not cover.
        self._instance_lock_fd, other = _take_instance_lock()
        if other is not None:
            named = f" (pid {other})" if other else ""
            self.console.print(
                f"[red]ticli is already running{named}.[/red]\n"
                "Only one copy can run at a time: two play over each other, "
                "and each one's saved position overwrites the other's.\n"
                "Quit the running copy first, or use the terminal it is in.")
            return

        # Find audio player
        player_cmd = _find_audio_player()
        if not player_cmd:
            self.console.print("[red]No audio player found. Install mpv or ffplay.[/red]")
            return
        self.audio = AudioPlayer(player_cmd, volume=self.config["volume"], cache=self._cache)
        # The backend is only known now, and the saved volume may predate it —
        # 250% written next to mpv, read on a machine that only has ffplay
        self._clamp_volume_to_backend()

        # Login
        if not self._login():
            return

        # Make the cache tracker agree with the disk before anything reads
        # either. Off the UI thread because it is a directory listing plus a
        # stat per file — the one place that walk is allowed to happen.
        self._reconcile_cache()

        # Load favorites in background
        self._load_favorites()

        # Restore previous session state (queue, current track)
        self._restore_state()

        # Start playback monitor
        monitor = threading.Thread(target=self._monitor_playback, daemon=True)
        monitor.start()

        # A closed terminal (SIGHUP) or kill (SIGTERM) should still exit the
        # main loop cleanly so the finally block saves state and stops audio
        def _on_signal(signum, frame):
            self.running = False
        for _sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                signal.signal(_sig, _on_signal)
            except (ValueError, OSError):
                pass

        # A resize changes what a frame should look like *and* what is
        # already on screen. The handler only records it and wakes the loop —
        # no new thread and no timer; the repaint happens where every other
        # repaint happens.
        def _on_resize(signum, frame):
            self._resized = True
            self._wake()
        try:
            signal.signal(signal.SIGWINCH, _on_resize)
        except (AttributeError, ValueError, OSError):
            pass  # no SIGWINCH here; the idle tick still picks the size up

        import tty
        import termios
        import select

        if not sys.stdin.isatty():
            self.console.print("[red]Player requires an interactive terminal.[/red]")
            return

        try:
            self._wake_r, self._wake_w = os.pipe()
            os.set_blocking(self._wake_r, False)
        except OSError:
            self._wake_r = self._wake_w = None

        old_settings = termios.tcgetattr(sys.stdin)
        # Kept on self so a PKCE sign-in from the settings page can hand the
        # terminal back for the length of a paste and then take it again
        self._tty_settings = old_settings
        try:
            tty.setcbreak(sys.stdin.fileno())
            # No clear(): the TUI lives on the alternate screen now, so the
            # scrollback it was launched from is none of its business

            with self._make_live() as live:
                self._live = live
                self._repaint(live, force=True)
                while self.running:
                    keys = self._read_keys(select)
                    for key in keys:
                        self._handle_key(key)
                        if not self.running:
                            break
                    resized = self._resized
                    self._resized = False
                    self._repaint(live, force=bool(keys) or resized)
        finally:
            self._live = None
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            self._tty_settings = None
            self._shutdown()
            for fd in (self._wake_r, self._wake_w):
                try:
                    if fd is not None:
                        os.close(fd)
                except OSError:
                    pass
            self._wake_r = self._wake_w = None

        self.console.print("[dim]Player closed.[/dim]")


def main():
    # One CLI, defined once: this used to carry its own copy of the click
    # options, which had already drifted from cli.py's by the time the tier
    # rename found it. Imported lazily, mirroring cli.py's lazy import of
    # this module the other way around.
    from ticli.cli import main as _main
    _main()


if __name__ == "__main__":
    main()
