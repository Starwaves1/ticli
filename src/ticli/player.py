
import json
import logging
import os
import re
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


PANEL_CHROME = 6
MINI_PANEL_CHROME = 4
PANEL_ROWS = 4
MINI_PANEL_ROWS = 2
MIN_INNER_WIDTH = 12

INDENT = 3
HINT_GAP = 2

IDENTITY_ROWS = 3

MIN_BAR_WIDTH = 8
VOLUME_BAR_MAX = 30

Hint = namedtuple("Hint", "key label short rank")
Hint.__new__.__defaults__ = (None, 0)

HINT_FULL, HINT_SHORT, HINT_KEYS = 0, 1, 2

DOWNLOAD_BUTTONS = (Hint("Enter", "download", "get", 0),
                    Hint("Esc", "cancel", None, 1))


def _clip_lines(text: "Text", width: int) -> list:
    lines = text.split("\n", allow_blank=True)
    for line in lines:
        line.truncate(width, overflow="ellipsis")
    return list(lines)


def _side_by_side(left: list, right: list) -> "Text":
    width = max((cell_len(line.plain) for line in left), default=0)
    top = max((len(left) - len(right)) // 2, 0)
    out = []
    for i in range(max(len(left), len(right))):
        line = Text()
        if i < len(left):
            line.append_text(left[i])
        j = i - top
        if 0 <= j < len(right):
            line.append(" " * (width - cell_len(line.plain)
                               + artwork.ART_GUTTER))
            line.append_text(right[j])
        out.append(line)
    return Text("\n").join(out)


def _boxed(rows: list, width: int, margin: int, style: str) -> list:
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


BAR_FULL, BAR_HALF, BAR_EMPTY = "━", "╸", "┈"


def _bar_split(fraction: float, width: int) -> tuple:
    fraction = max(0.0, min(1.0, fraction))
    steps = int(round(fraction * width * 2))
    whole, rem = divmod(steps, 2)
    done = BAR_FULL * whole
    if rem and whole < width:
        done += BAR_HALF
        whole += 1
    return done, BAR_EMPTY * (width - whole)


def _format_rate(per_second: float) -> str:
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
            text.append(key + sep[:1] if sep else key, style="bold")
            if sep:
                text.append(" " + label, style="dim")
    return text


class _Fit:
    __slots__ = ("inner", "rows", "page_rows", "artwork", "hint_rows",
                 "prose", "chrome", "mini", "_levers")

    def __init__(self, inner, rows, page_rows, hint_rows, levers=(), mini=False):
        self.inner = inner
        self.rows = rows
        self.page_rows = page_rows
        self.artwork = True
        self.prose = True
        self.chrome = True
        self.mini = mini
        self.hint_rows = hint_rows
        self._levers = list(levers)

    def relax(self, over: int) -> bool:
        while self._levers:
            lever = self._levers.pop(0)
            if lever == "page_rows" and self.page_rows > 1:
                self.page_rows = max(1, self.page_rows - over)
                self._levers.insert(0, lever)
                return True
            if lever in ("artwork", "prose", "chrome") and getattr(self, lever):
                setattr(self, lever, False)
                return True
            if lever == "hint_rows" and self.hint_rows > 1:
                self.hint_rows = 1
                return True
        return False


ROOMY_FIT = _Fit(inner=74, rows=1000, page_rows=1000, hint_rows=2)

KEY_UP = "\x1b[A"
KEY_DOWN = "\x1b[B"
KEY_RIGHT = "\x1b[C"
KEY_LEFT = "\x1b[D"
KEY_ESC = "\x1b"
KEY_ENTER = "\r"
KEY_ENTER2 = "\n"
KEY_BACKSPACE = "\x7f"
KEY_BACKSPACE2 = "\x08"
KEY_TAB = "\t"
KEY_SHIFT_TAB = "\x1b[Z"

HIDE_HOLD_KEYS = frozenset({KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, " "})

HIDE_HINT_RANK = 9

from ticli.commands import (
    HUMAN, OFFLINE, ONLINE, SIGNED_OUT, Commands, auth_rejected, download_tracks, unauthorized,
)
from ticli.utils.credential_store import save_tokens, load_tokens
from ticli.utils.config import (
    PROTECTED_KEYS,
    QUALITY_CHOICES,
    QUALITY_V4_RENAMES,
    SETTINGS_ROWS,
    coerce,
    cycle_value,
    display_value,
    get_spec,
    hash_ai_key,
    UNREADABLE,
    UNREADABLE_MESSAGE,
    ConfigUnreadable,
    load_config,
    update_config,
)
from ticli.utils.cache import (
    PLAY_COUNTS_AFTER,
    age_label,
    cached_audio_path,
    kind_of,
    CachedTrack,
    MetadataCache,
    format_gb,
    is_owned_audio,
    track_record,
)
from ticli.utils import artwork, backend_health, downloads, tags, throttle
from ticli.utils.net import is_transport_failure, tidal_session

STATE_DIR = Path.home() / ".config" / "ticli"
STATE_FILE = STATE_DIR / "player_state.json"

PLAYLIST_NAME_MAX = 100

AUDIO_PLAYERS = ["mpv", "ffplay"]

IS_MACOS = sys.platform == "darwin"

# mpv exceeds 100 only with a higher --volume-max (default ceiling 130); ffplay's -volume clips at 100.
VOLUME_MAX = get_spec("volume")["max"]
FFPLAY_VOLUME_MAX = 100

SAFE_VOLUME_CEILING = 100

BACKEND_VOLUME_CEILINGS = {
    "mpv": VOLUME_MAX,
    "ffplay": FFPLAY_VOLUME_MAX,
}

# mpv turns macOS media keys into these key names; rebound over IPC so mpv doesn't act itself
# (NEXT would end the playlist, STOP would quit mpv).
MEDIA_KEY_ACTIONS = {
    "PLAY": "toggle",
    "PLAYPAUSE": "toggle",
    "PLAYONLY": "play",
    "PAUSEONLY": "pause",
    "STOP": "pause",
    "NEXT": "next",
    "PREV": "prev",
}
MEDIA_KEY_PROP = "user-data/ticli/media-key"

PREV_RESTART_SECONDS = 30

SEEK_STEP_SECONDS = 10
# Landing on EOF makes the backend exit, which reads as track end and skips.
SEEK_END_MARGIN = 2
SEEK_COALESCE_SECONDS = 0.3

PREFETCH_LEAD = 20
PREFETCH_MAX_AGE = 90

# Only PKCE reaches FLAC: the device flow's client is entitled to AAC, and TIDAL answers FLAC
# requests with 320k AAC rather than an error.
LOGIN_FLOWS = ("device", "pkce")

PKCE_PASTE_TRIES = 3

HLS_KEEP = 4
# #EXT-X-MAP needs version 7; the fMP4 segments have no moov, so none decode without the init segment.
HLS_VERSION = 7
# ffmpeg's default whitelist for a file input is "file,crypto,data"; segment URLs would silently fail.
HLS_PROTOCOLS = "file,http,https,tcp,tls,crypto"
HLS_SUFFIX = ".m3u8"
# mpv's --cache=auto is off for a local playlist file, leaving 1s readahead (measured 1.02s) and
# audible hitches on 4s FLAC segments.
HLS_CACHE_SECONDS = 60
PLAYER_ERROR_CHARS = 90
PLAYER_ERROR_SECONDS = 8.0

# Both backends exit 0 with empty stderr after a mid-track 403, same as a finished track; only the
# clock tells. Generous: TIDAL's `duration` can be off by seconds. Measured failure: 164s short.
STREAM_TRUNCATED_MARGIN = 15.0

# 19/s burst got the IP blocked (docs/adr/0001-tidal-rate-limits.md).
REFETCH_MIN_INTERVAL = 2.0

# Parallelism only on CDN fetches; API resolving stays serial and paced (docs/adr/0001-tidal-rate-limits.md).
DOWNLOAD_WORKERS = 3
WORKER_POLL_SECONDS = 0.05
RATE_SAMPLES = 3

# On these, stop all requests and report; never retry (docs/adr/0001-tidal-rate-limits.md).
# Bare codes only as whole tokens: a URL in a network error carries track ids like 154291836.
RATE_LIMIT_SIGNS = re.compile(
    r"(?<![\w/=-])(?:429|4006)(?![\w/=-])|too many requests|does not have streaming privileges",
    re.IGNORECASE)


def _rough_minutes(tracks: int) -> str:
    seconds = tracks * REFETCH_MIN_INTERVAL
    if seconds < 90:
        return "a minute or so"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"at least {minutes} minutes"
    hours = minutes / 60
    return f"at least {hours:.1f} hours"


def _looks_rate_limited(message: str) -> bool:
    return bool(RATE_LIMIT_SIGNS.search(message or ""))


def _rate_limited(exc) -> bool:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429:
        return True
    if status == 401:
        # tidalapi's HTTPError text is "401 Client Error: Unauthorized"; the 4006 is only in the body.
        try:
            if response.json().get("subStatus") == 4006:
                return True
        except Exception:
            pass
    return _looks_rate_limited(str(exc))


OFFLINE_MESSAGE = "couldn't reach TIDAL"
NO_NETWORK_SIGN_IN = ("[red]Can't reach TIDAL to sign in.[/red] [dim]Signing in needs the network once; "
                      "after that ticli starts offline and plays your downloads. Check the "
                      "connection and run ticli again.[/dim]")


def _play_failure_text(exc) -> str:
    if isinstance(exc, _NoLocalCopy):
        return str(exc)
    if is_transport_failure(exc):
        return f"Can't play — {OFFLINE_MESSAGE}"
    if _rate_limited(exc):
        return "TIDAL is rate-limiting — playback stopped. Nothing will be retried."
    return f"Couldn't start the track — {type(exc).__name__}: {str(exc)[:PLAYER_ERROR_CHARS]}"


def _restore_sleep(seconds: float) -> None:
    time.sleep(seconds)


QUALITY_RANK = {"LOW": 0, "HIGH": 1, "LOSSLESS": 2, "HI_RES_LOSSLESS": 3}

IDLE_POLL_SECONDS = 0.5
# Wake just past a second boundary so the displayed second has already turned.
SECOND_EDGE = 0.005
SUBSCRIBE_TIMEOUT = 10.0
STOP_TIMEOUT = 5.0
KEY_REPEAT_WINDOW = 0.15
# tidalapi documents no more than 300 items behind any one query.
SEARCH_MAX_OFFSET = 300
SEARCH_FETCH_MIN_INTERVAL = 1.0
RADIO_LIMIT = 25
RADIO_FETCH_MIN_INTERVAL = 1.0
ESC_TAIL_SECONDS = 0.05

DOWNLOAD_CHUNK = 256 * 1024
DOWNLOAD_TIMEOUT = (10, 60)

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
AUDIO_URL_EXTENSIONS = {
    ".mp4": ".m4a", ".m4a": ".m4a", ".flac": ".flac", ".mp3": ".mp3",
    ".aac": ".aac", ".ogg": ".ogg", ".wav": ".wav",
}
DEFAULT_AUDIO_EXT = ".m4a"


def _audio_extension(content_type: Optional[str], url: str) -> str:
    if content_type:
        mime = content_type.split(";")[0].strip().lower()
        if mime in AUDIO_MIME_EXTENSIONS:
            return AUDIO_MIME_EXTENSIONS[mime]
    suffix = os.path.splitext(urlsplit(url or "").path)[1].lower()
    return AUDIO_URL_EXTENSIONS.get(suffix, DEFAULT_AUDIO_EXT)


# tidalapi's get_hls() omits #EXT-X-MAP and lists the init segment as audio.
def _hls_playlist(dash) -> str:
    urls = list(getattr(dash, "urls", None) or [])
    if not urls:
        raise ValueError("segmented stream named no segments")
    init = getattr(dash, "first_url", None) or urls[0]
    media = urls[1:] if urls[0] == init else urls
    if not media:
        raise ValueError("segmented stream named no audio segments")
    timescale = float(getattr(dash, "timescale", 0) or 44100)
    full = float(getattr(dash, "chunk_size", 0) or 0) / timescale
    last = float(getattr(dash, "last_chunk_size", 0) or 0) / timescale
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
        pass
    return str(path)


def _wire_job(job) -> Optional[dict]:
    """A download job as plain data: slots unfrozen, the monitor's private rate marks dropped."""
    if not job:
        return None
    plain = dict(job)
    if "slots" in plain:
        plain["slots"] = [{k: v for k, v in slot.items() if k != "mark"} for slot in plain["slots"]]
    return plain


class _NoLocalCopy(Exception):
    def __init__(self, track, connectivity: str):
        name = getattr(track, "name", None) or "this track"
        super().__init__(f'Signed out — "{name}" has no local copy; [o] to sign in again'
                         if connectivity == SIGNED_OUT else f'Offline — "{name}" has no local copy')


class _DownloadSuperseded(Exception):
    pass


def stream_sources(url: str) -> list:
    if url.endswith(HLS_SUFFIX):
        return _hls_segments(url)
    if url.startswith(("http://", "https://")):
        return [url]
    return []


def fetch_to_file(sources: list, part: str, abandoned=None, progress=None) -> str:
    ext = DEFAULT_AUDIO_EXT
    done = 0
    total = 0
    # One Session per call: a hi-res track is ~46 requests, and per-request TLS handshakes were 7.8x slower.
    with requests.Session() as session, open(part, "wb") as handle:
        for index, source in enumerate(sources):
            with session.get(source, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
                response.raise_for_status()
                if index == 0:
                    ext = _audio_extension(response.headers.get("Content-Type"), source)
                    if len(sources) == 1:
                        try:
                            total = int(response.headers.get("Content-Length") or 0)
                        except (TypeError, ValueError):
                            pass
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
    def __init__(self, items, resolve, fetch, alive, report,
                 workers: int = 1, slots=(), clock=None, pace=None):
        self.items = list(items)
        self.resolve = resolve
        self.fetch = fetch
        self.alive = alive
        self.report = report
        self.workers = max(1, workers)
        self.slots = tuple(slots) if slots else (None,) * self.workers
        self.clock = [None] if clock is None else clock
        # An agent's run: `pace` (throttle.acquire) before each track instead of the local spacing.
        self.pace = pace
        self.results: list = []
        self._written = 0
        self.consumed = 0
        self.closed = False
        self.offline = ""
        self._stopped_by = ""

    def run(self) -> tuple:
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
                if blocked or self.offline:
                    break
                if self.pace is not None:
                    try:
                        self.pace()
                    except throttle.Tripped:
                        self._stopped_by = self._stopped_by or "rate-limited (agent stop is in force)"
                        blocked = self._blocked()
                        break
                elif self.clock[0] is not None:
                    self._pace(self.clock[0])
                    if not self.alive():
                        return None
                    blocked = self._blocked()
                    if blocked or self.offline:
                        break
                self.clock[0] = time.monotonic()
                self._flush()
                try:
                    handle = self.resolve(item)
                except _DownloadSuperseded:
                    return None
                except Exception as e:
                    self._note_failure(e)
                    blocked = self._blocked()
                    if blocked:
                        break
                    logger.debug("Could not resolve %s: %s", item, e)
                    self.results.append((False, str(e), None))
                    self._flush()
                    if self.offline:
                        break
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
            # Announce first, then re-check; an append can still slip in, so the caller re-queues pending().
            self.closed = True
            if blocked or self.offline or self.consumed >= len(self.items):
                break
            self.closed = False
        blocked = blocked or self._blocked()
        self._flush()
        done, failed = self._counts()
        return done, failed, blocked

    def pending(self) -> list:
        return self.items[self.consumed:]

    def add(self, items) -> bool:
        self.items = self.items + list(items)
        return not self.closed

    def _work(self, item, handle, index) -> None:
        slot = self.slots[index]
        try:
            record = self.fetch(item, handle, slot)
            self.results.append((True, "", record))
        except _DownloadSuperseded:
            self.results.append((False, "cancelled", None))
        except Exception as e:
            logger.debug("Fetch of %s failed: %s", item, e)
            self._note_failure(e)
            self.results.append((False, str(e), None))
        finally:
            if slot is not None and slot.get("state") == "running":
                slot["state"] = "idle"
            self._tally()

    def _free_slot(self, threads):
        while self.alive():
            for index in range(self.workers):
                thread = threads.get(index)
                if thread is None or not thread.is_alive():
                    return index
            time.sleep(WORKER_POLL_SECONDS)
        return None

    def _pace(self, started: float) -> None:
        remaining = REFETCH_MIN_INTERVAL - (time.monotonic() - started)
        while remaining > 0 and self.alive():
            time.sleep(min(0.25, remaining))
            remaining -= 0.25

    def _note_failure(self, exc) -> None:
        if is_transport_failure(exc):
            self.offline = self.offline or str(exc)[:PLAYER_ERROR_CHARS]
        elif _rate_limited(exc):
            self._stopped_by = self._stopped_by or str(exc)[:PLAYER_ERROR_CHARS]
            if self.pace is not None:
                throttle.trip("http_429", detail=self._stopped_by)

    def _blocked(self) -> str:
        return self._stopped_by

    def _counts(self) -> tuple:
        results = self.results[:]
        done = sum(1 for ok, _m, _r in results if ok)
        return done, len(results) - done

    def _tally(self) -> None:
        done, failed = self._counts()
        self.report(done=done, failed=failed)

    def _flush(self) -> None:
        results = self.results[:]
        for _ok, _message, record in results[self._written:]:
            if record is not None:
                try:
                    record()
                except Exception as e:
                    logger.debug("Could not record a finished download: %s", e)
        self._written = len(results)
        self._tally()


def _empty_search_pool() -> dict:
    return {"tracks": [], "albums": [], "artists": [], "playlists": []}


def _empty_search_reservoir() -> dict:
    return {
        **_empty_search_pool(),
        "offset": 0,
        "exhausted": dict.fromkeys(("tracks", "albums", "artists", "playlists"), False),
        "stopped": False,
        "message": "",
    }


def _empty_search_view() -> dict:
    return {
        "loading": False, "results": [], "cursor": 0, "message": "",
        "consumed": {"tracks": 0, "albums": 0, "artists": 0}, "cached": False,
    }


def _split_keys(data: str) -> list:
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
            while j < len(data) and data[j] in "0123456789;":
                j += 1
            if j < len(data):
                j += 1
            keys.append(data[i:j])
            i = j
        else:
            keys.append("\x1b")
            i += 1
    return keys


def _incomplete_escape(data: str) -> bool:
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
    for player in AUDIO_PLAYERS:
        for path_dir in os.environ.get("PATH", "").split(os.pathsep):
            full = os.path.join(path_dir, player)
            if os.path.isfile(full) and os.access(full, os.X_OK):
                return player
    return None


def _instance_lock_path() -> Path:
    return STATE_DIR / "instance.lock"


def _read_lock_pid(fd: int) -> int:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return int(os.read(fd, 32).decode().strip() or 0)
    except (OSError, ValueError, UnicodeDecodeError):
        return 0


def _take_instance_lock():
    try:
        import fcntl
    except ImportError:
        return None, None
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(_instance_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        logger.debug("No instance lock (%s); starting anyway", e)
        return None, None
    try:
        # flock, not a pid file: the kernel drops it when the holder dies.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        other = _read_lock_pid(fd)
        os.close(fd)
        return None, other
    except OSError as e:
        # Lock can't be evaluated (e.g. NFS home): start anyway.
        logger.debug("Instance lock unavailable (%s); starting anyway", e)
        os.close(fd)
        return None, None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    return fd, None


class AudioPlayer:
    def __init__(self, player_cmd: str, volume: int = 100, cache=None):
        self.player_cmd = player_cmd
        self.cache = cache
        self.volume = volume
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._paused = False
        self._ipc_path: Optional[str] = None
        self._current_url: Optional[str] = None
        self._cache_file: Optional[str] = None
        self._play_start: Optional[float] = None
        self._seek_offset: float = 0
        self._media_keys_bound = False
        self._media_title: Optional[str] = None
        self._download_gen = 0
        self._cache_persistent = False
        self._stderr_path: Optional[str] = None
        self._stderr_handle = None

    def volume_ceiling(self) -> int:
        return BACKEND_VOLUME_CEILINGS.get(self.player_cmd, SAFE_VOLUME_CEILING)

    def _ffplay_volume(self) -> int:
        return min(FFPLAY_VOLUME_MAX, max(0, int(self.volume)))

    def _sweep_cache(self):
        try:
            self.cache.enforce_budget()
        except Exception as e:
            logger.debug("Cache sweep failed: %s", e)

    def _drop_other_copies(self, base: str, keeping: str) -> None:
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
        try:
            return self.cache.should_cache(key)
        except Exception:
            return True

    def _audio_cache_base(self, key) -> Optional[str]:
        if not self.cache or not self.cache.keeps_audio or key in (None, ""):
            return None
        try:
            return str(self.cache_audio_dir() / str(key))
        except OSError:
            return None

    def _cached_audio_path(self, key) -> Optional[str]:
        if not self._audio_cache_base(key):
            return None
        return cached_audio_path(key)

    def _start_download(self, url: str, cache_key, gen: int, quality=None):
        sources = stream_sources(url)
        if not sources:
            return
        base = self._audio_cache_base(cache_key)
        if base is not None and not self._admit(cache_key):
            base = None
        keep = base is not None
        if not keep:
            if self.player_cmd != "ffplay":
                return
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
                if keep:
                    self._drop_other_copies(base, path)
            except Exception as e:
                logger.debug("Audio download did not finish: %s", e)
                _drop(part)
                _drop(path)
                return
            if self._download_gen == gen:
                self._cache_file = path
                self._cache_persistent = keep
                if self._download_gen != gen:
                    self._cache_file = None
                    self._cache_persistent = False
                    if not keep:
                        _drop(path)
            elif not keep:
                _drop(path)
            if keep and os.path.exists(path):
                try:
                    # Track before the sweep, or the sweep values it at nothing and evicts it first.
                    self.cache.note_cached(
                        cache_key, ext, os.path.getsize(path), quality=quality)
                    self.cache.invalidate_audio_count()
                except Exception:
                    pass
                self._sweep_cache()

        threading.Thread(target=_run, daemon=True).start()

    def _open_stderr(self):
        try:
            if self._stderr_handle is not None:
                self._stderr_handle.close()
        except OSError:
            pass
        self._stderr_handle = None
        try:
            path = os.path.join(tempfile.gettempdir(), f"ticli-player-{os.getpid()}.log")
            # A file, not a pipe: nothing reads it until the process is dead, and a full pipe would wedge the player.
            self._stderr_handle = open(path, "w+")
            self._stderr_path = path
            return self._stderr_handle
        except OSError:
            self._stderr_path = None
            return subprocess.DEVNULL

    def _spawn(self, cmd: list) -> subprocess.Popen:
        try:
            return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=self._open_stderr())
        except OSError as e:
            # Own type: OSError is also the base of every requests failure, which callers handle differently.
            raise backend_health.SpawnError(
                backend_health.classify_spawn_error(self.player_cmd, e)) from e

    def _stderr_text(self) -> str:
        if not self._stderr_path:
            return ""
        try:
            return Path(self._stderr_path).read_text(errors="replace")
        except OSError:
            return ""

    def failure(self) -> Optional[backend_health.PlayerFailure]:
        # Signals count as failures: every kill of ours drops or replaces _process under this lock,
        # so a signal on a held handle came from elsewhere (e.g. dyld's SIGABRT for an unloadable binary).
        with self._lock:
            process = self._process
            if process is None:
                return None
            code = process.poll()
            if code is None or code == 0:
                return None
            stderr = self._stderr_text()
        return backend_health.classify_exit(self.player_cmd, code, stderr,
                                            limit=PLAYER_ERROR_CHARS)

    def _hls_flags(self) -> list:
        if self.player_cmd == "mpv":
            return [
                "--demuxer=lavf", "--demuxer-lavf-format=hls",
                # mpv splits key-value lists on commas; pass the whitelist length-prefixed
                f"--demuxer-lavf-o=protocol_whitelist="
                f"%{len(HLS_PROTOCOLS)}%{HLS_PROTOCOLS}",
                "--cache=yes", f"--cache-secs={HLS_CACHE_SECONDS}",
            ]
        # ffplay's read thread stops once every stream has enough packets (~2 segments, ~11 s);
        # -infbuf pulls the whole track in under 1 s (21.7 MB vs 1.4 MB served @1s).
        return ["-infbuf", "-protocol_whitelist", HLS_PROTOCOLS, "-f", "hls"]

    def cache_audio_dir(self):
        from ticli.utils import cache as cache_mod
        cache_mod.CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = cache_mod.audio_dir()
        path.mkdir(exist_ok=True, mode=0o700)
        return path

    def play_url(self, url: str, seek: float = 0, title: Optional[str] = None,
                 cache_key=None, local: Optional[str] = None, quality=None):
        with self._lock:
            have_kept, gen = self._play_url_locked(
                url, seek, title, cache_key, local)
        if not have_kept:
            self._start_download(url, cache_key, gen, quality=quality)

    def _play_url_locked(self, url: str, seek: float = 0,
                         title: Optional[str] = None, cache_key=None,
                         local: Optional[str] = None):
        self._stop_locked()
        self._paused = False
        self._current_url = url
        self._media_title = title
        self._media_keys_bound = False
        self._seek_offset = seek
        self._play_start = time.time()
        kept = local if (local and os.path.exists(local)) else \
            self._cached_audio_path(cache_key)
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
                # Not --really-quiet: it made an undecodable stream look decodable
                "--msg-level=all=error",
                f"--input-ipc-server={self._ipc_path}",
                # --volume-max first: mpv refuses anything above its default ceiling (130)
                f"--volume-max={VOLUME_MAX}",
                f"--volume={self.volume}",
            ]
            if segmented:
                cmd += self._hls_flags()
            if seek > 0:
                cmd.append(f"--start={seek}")
            cmd.append(source)
        else:
            self._ipc_path = None
            cmd = self._ffplay_cmd(source, seek)
        self._process = self._spawn(cmd)
        return have_kept, self._download_gen

    def _ffplay_cmd(self, source: str, seek: float) -> list:
        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error",
               "-volume", str(self._ffplay_volume())]
        if source.endswith(HLS_SUFFIX):
            cmd += self._hls_flags()
        if seek > 0:
            cmd += ["-ss", str(seek)]
        cmd.append(source)
        return cmd

    def _spawn_ffplay(self, source: str, seek: float):
        # Drop the handle first: if _spawn raises, a stale _process would make failure() read our own SIGTERM as an outside kill.
        self._process = None
        self._process = self._spawn(self._ffplay_cmd(source, seek))

    def _play_from_cache(self, seek: float):
        self._spawn_ffplay(self._cache_file, seek)
        self._play_start = time.time()
        self._paused = False

    @property
    def _process_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def _mpv_ipc_active(self) -> bool:
        return self.player_cmd == "mpv" and bool(self._ipc_path)

    def _reap_process(self):
        self._process.terminate()
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()
            try:
                # SIGKILL is unblockable, but without a wait() the child stays a zombie
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logger.warning("audio process %s survived SIGKILL",
                               getattr(self._process, "pid", "?"))

    def pause(self):
        with self._lock:
            if self._paused or not self._process_alive:
                return
            if self._mpv_ipc_active:
                # IPC socket takes ~100ms to come up after spawn; retry so an early pause isn't lost
                for _ in range(5):
                    if self._mpv_command({"command": ["set_property", "pause", True]}):
                        self._paused = True
                        return
                    if not self._process_alive:
                        return
                    time.sleep(0.1)
            else:
                elapsed = time.time() - self._play_start if self._play_start else 0
                self._seek_offset += elapsed
                self._play_start = None
                self._reap_process()
                self._process = None
                self._paused = True

    def resume(self) -> bool:
        download = None
        with self._lock:
            if not self._paused:
                return False
            if self._mpv_ipc_active:
                if self._mpv_command({"command": ["set_property", "pause", False]}):
                    self._paused = False
                    return True
                return False
            if self._cache_file and os.path.exists(self._cache_file):
                self._play_from_cache(self._seek_offset)
                return True
            if not self._current_url:
                return False
            # Don't release the lock to call play_url: that reopens the seam _play_url_locked closes
            url = self._current_url
            seek = self._seek_offset
            self._paused = False
            have_kept, gen = self._play_url_locked(url, seek)
            if not have_kept:
                download = (url, None, gen)
        if download:
            self._start_download(*download)
        return True

    def _mpv_request(self, cmd: dict, timeout: float = 0.5) -> Optional[dict]:
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
                # mpv also broadcasts events on this socket; scan for our request_id
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
        reply = self._mpv_request(cmd)
        return reply is not None and reply.get("error") == "success"

    def get_time_pos(self) -> Optional[float]:
        if self.player_cmd != "mpv":
            return None
        with self._lock:
            if self._paused or not self._process_alive:
                return None
            reply = self._mpv_request({"command": ["get_property", "time-pos"]}, timeout=0.2)
        if reply and reply.get("error") == "success" and isinstance(reply.get("data"), (int, float)):
            return float(reply["data"])
        return None

    def set_volume(self, value: int):
        with self._lock:
            self.volume = value
            if self._mpv_ipc_active and self._process_alive:
                self._mpv_command({"command": ["set_property", "volume", value]})

    def seek_to_start(self) -> bool:
        if self.player_cmd != "mpv":
            return False
        with self._lock:
            if self._paused or not self._process_alive:
                return False
            if not self._mpv_command({"command": ["seek", 0, "absolute"]}):
                return False
            self._seek_offset = 0
            self._play_start = time.time()
            return True

    def seek_to(self, position: float) -> bool:
        position = max(0.0, position)
        with self._lock:
            alive = self._process_alive
            if self._mpv_ipc_active and alive:
                if not self._mpv_command({"command": ["seek", position, "absolute"]}):
                    return False
                self._seek_offset = position
                self._play_start = None if self._paused else time.time()
                return True
            if self._paused and not alive:
                self._seek_offset = position
                return True
            if not alive or self.player_cmd == "mpv":
                return False
            source = self._cache_file if (
                self._cache_file and os.path.exists(self._cache_file)) else self._current_url
            if not source:
                return False
            self._reap_process()
            self._spawn_ffplay(source, position)
            self._seek_offset = position
            self._play_start = time.time()
            return True

    def _bind_media_keys(self) -> bool:
        if not all(
            self._mpv_command({"command": ["keybind", key, f"set {MEDIA_KEY_PROP} {action}"]})
            for key, action in MEDIA_KEY_ACTIONS.items()
        ):
            return False
        if self._media_title:
            self._mpv_command(
                {"command": ["set_property", "force-media-title", self._media_title]}
            )
        return True

    def poll_media_key(self) -> Optional[str]:
        if not IS_MACOS or self.player_cmd != "mpv" or not self._ipc_path:
            return None
        if not self._media_keys_bound:
            self._media_keys_bound = self._bind_media_keys()
            if not self._media_keys_bound:
                return None
        reply = self._mpv_request({"command": ["get_property", MEDIA_KEY_PROP]})
        if not reply or reply.get("error") != "success" or not reply.get("data"):
            return None
        self._mpv_command({"command": ["set_property", MEDIA_KEY_PROP, ""]})
        return reply["data"]

    def stop(self):
        with self._lock:
            self._stop_locked()

    def _stop_locked(self):
        self._download_gen += 1
        if self._process:
            if self._process.poll() is None:
                self._reap_process()
            self._process = None
        if self._cache_file and not self._cache_persistent:
            self._unlink_quietly(self._cache_file)
        self._cache_file = None
        self._cache_persistent = False
        self._paused = False
        self._play_start = None
        self._seek_offset = 0
        self._media_keys_bound = False
        if self._ipc_path:
            self._unlink_quietly(self._ipc_path)
            self._ipc_path = None

    @staticmethod
    def _unlink_quietly(path: str):
        try:
            os.unlink(path)
        except OSError:
            pass

    def source_vanished(self) -> bool:
        return bool(self._cache_persistent and self._cache_file) and not os.path.exists(self._cache_file)

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._paused or self._process_alive

    @property
    def is_paused(self) -> bool:
        with self._lock:
            if not self._paused:
                return False
            return self.player_cmd != "mpv" or self._process_alive

class HeadlessTidalPlayer:
    remote = None
    MODE_PLAYER = "player"
    MODE_SEARCH = "search"
    MODE_BROWSE = "browse"
    MODE_QUEUE = "queue"
    MODE_PLAYLISTS = "playlists"
    MODE_ADD_TO_PLAYLIST = "add_to_playlist"
    MODE_SETTINGS = "settings"
    MODE_ARTIST = "artist"
    MODE_DOWNLOADS = "downloads"

    ARTIST_SECTIONS = ("tracks", "albums", "playlists", "suggestions")
    ARTIST_SECTION_LABELS = {
        "tracks": "Top Tracks",
        "albums": "Albums",
        "playlists": "Playlists",
        "suggestions": "Suggestions",
    }
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

    # TIDAL's player names (Low/Medium/High/Max) differ from tidalapi's; persisted values stay in tidalapi's spelling
    QUALITY_MAP = {
        "LOW": tidalapi.Quality.low_96k,
        "MEDIUM": tidalapi.Quality.low_320k,
        "HIGH": tidalapi.Quality.high_lossless,
        "MAX": tidalapi.Quality.hi_res_lossless,
    }
    SEARCH_FILTERS = ("all", "tracks", "albums", "artists",
                      "tidal_playlists", "playlists", "music")
    # Answered from disk with no request; offline, search opens on "music" (ADR-0009).
    LOCAL_SCOPES = ("playlists", "music")
    SEARCH_FILTER_LABELS = {
        "all": "All",
        "tracks": "Tracks",
        "albums": "Albums",
        "artists": "Artists",
        "tidal_playlists": "Playlists",
        "playlists": "My Playlists",
        "music": "My Music",
    }
    SEARCH_FILTER_KINDS = {
        "all": ("tracks", "albums", "artists", "playlists"),
        "tracks": ("tracks",),
        "albums": ("albums",),
        "artists": ("artists",),
        "tidal_playlists": ("playlists",),
    }

    # MAX label is the tier's nominal ceiling; the master decides the real resolution
    QUALITY_LABELS = {
        "LOW": "96k AAC",
        "MEDIUM": "320k AAC",
        "HIGH": "16/44.1 FLAC",
        "MAX": "24/192 FLAC",
    }

    def __init__(self, quality: Optional[str] = None, login_flow: Optional[str] = None,
                 remote=None):
        self.console = Console()
        # Set when this instance is a TUI attached to the background player (ADR-0008).
        self.remote = remote
        self._mirror: dict = {}
        self._pending: dict = {}
        self._known: dict = {}
        self._lists: dict = {}
        self._tick_hook = None
        self._quitting = False
        self._logged_out = False
        self.start_failure = None
        # online / offline / signed_out, pushed to every client. Only an action that needs
        # TIDAL tries to leave offline (ADR-0003); a dead token waits for a human sign-in.
        self._connectivity = ONLINE
        self._reconnect_lock = threading.Lock()
        self.session = tidal_session()
        self._watch_transport()
        flow = (login_flow or LOGIN_FLOWS[0]).lower()
        self._login_flow = flow if flow in LOGIN_FLOWS else LOGIN_FLOWS[0]
        self._live = None
        self._tty_settings = None
        self._quality_ceiling: Optional[str] = None
        self.audio = None
        self.running = True
        self._mode = self.MODE_PLAYER
        self.config = load_config()
        self._page_size = self.config["page_size"]
        self._bar_max = self.config["progress_bar_max"]
        self._cache = MetadataCache(
            metadata=self.config["cache_metadata"],
            songs=self.config["cache_songs"],
            budget_gb=self.config["cache_budget_gb"],
        )
        self._current_track: Optional[tidalapi.Track] = None
        self._queue: list = []
        self._queue_index: int = -1
        self._playing = False
        self._play_start_time: Optional[float] = None
        self._play_offset: float = 0
        # Leaf lock held across one whole load-modify-save of player_state.json; nothing inside may retake it
        # (_save_state reaches the merge through its unlocked _locked half).
        self._state_lock = threading.Lock()
        # Leaf lock: tidalapi keeps one quality per session and get_stream reads it, so setting the tier and
        # building the request must not interleave. The UI thread never takes it; a request can hang.
        self._quality_lock = threading.Lock()
        # Never closed: the kernel releases the lock at exit, crashes included
        self._instance_lock_fd: Optional[int] = None
        self._liked_ids: set = set()
        self._radio_fetching = False
        self._radio_last_fetch = 0.0
        self._player_focus = False
        self._seek_target: Optional[float] = None
        self._seek_applied: Optional[float] = None
        self._seek_applying = False
        self._last_seek_apply = 0.0
        self._search_query = ""
        self._search_history: list = []
        self._search_history_cursor: Optional[int] = None
        self._search_filter = "all"
        self._search_key = ""
        self._search_reservoir: dict = _empty_search_reservoir()
        self._search_views: dict = {}
        self._search_fetching = False
        self._search_last_fetch = 0.0
        self._search_gen = 0
        self._browse_title = ""
        self._browse_tracks = []
        self._browse_cursor = 0
        self._browse_loading = False
        self._browse_message = ""
        self._browse_fetched = None
        self._playlists_fetched = None
        self._browse_playlist = None
        self._artist = None
        self._artist_section = self.ARTIST_SECTIONS[0]
        self._artist_cursor = 0
        self._artist_sections: dict = {}
        self._artist_cursors: dict = {}
        self._browse_remove_busy = False
        self._queue_cursor = 0
        self._playlists: list = []
        self._playlists_cursor = 0
        self._playlists_loading = False
        self._playlists_message = ""
        self._editable_playlists: list = []
        self._editable_playlists_time: float = 0.0
        self._picker_track = None
        self._picker_cursor = 0
        self._picker_loading = False
        self._picker_busy = False
        self._picker_new_name: Optional[str] = None
        self._last_playlist_id: Optional[str] = None
        self._download_open = False
        self._download_track = None
        self._download_tracks: list = []
        self._download_label = ""
        self._download_cursor = 0
        self._download_job = None
        self._download_job_gen = 0
        self._download_run = None
        self._api_pace = [None]
        self._download_known: Optional[tuple] = None
        self._settings_cursor = 0
        self._settings_edit: Optional[str] = None
        self._downloads_present: Optional[list] = None
        self._downloads_ids: Optional[set] = None
        self._downloads_cursor = 0
        self._downloads_delete: Optional[dict] = None
        self._reclaim_deferred = set()
        self._reclaim_lock = threading.Lock()
        self._play_counted = -1
        self._refetch_job = None
        self._refetch_gen = 0
        self._refetch_plan = None
        self._refetch_pending = False
        self._toast = ""
        self._toast_until = 0.0
        if self.config.get(UNREADABLE):
            self._set_toast(UNREADABLE_MESSAGE, seconds=PLAYER_ERROR_SECONDS)
        self._quit_pending = False
        self._logout_pending = False
        self._disable_songs_pending = False
        self._clear_cache_pending = False
        self._show_artwork = self.config["show_artwork"]
        self._artwork = None
        self._artwork_request = None
        self._mini_player = False
        self._footer_hidden = False
        self._show_more = False
        self._volume_open = False
        self._volume_from_focus = False
        self._fit = ROOMY_FIT
        self._last_toggle_key = 0.0
        self._last_segments = None
        self._resized = False
        self._wake_r = None
        self._wake_w = None
        self._user_display_name = ""
        self._restore_pending = False
        self._track_changing = False
        self._play_gen = 0
        # Bumped by pause/seek/play: the monitor's position resync is dropped if it moved meanwhile
        self._clock_epoch = 0
        self._playing_badge = None
        self._prefetch = None
        self._prefetch_id = None
        self._nav_history = []
        self._browse_source: Optional[tuple] = None
        self._settings_secret: Optional[str] = None
        self.commands = Commands(self)
        name = (quality or self.config["quality"]).upper()
        self._quality_name = name if name in self.QUALITY_MAP else self.config["quality"]
        self._asked_quality = self._quality_name if quality else None
        self._asked_pkce = (login_flow or "").lower() == "pkce"
        self.session.audio_quality = self.QUALITY_MAP[self._quality_name]

    def _get_user_display_name(self) -> str:
        u = self.session.user
        if not u:
            return "Unknown"
        first = getattr(u, "first_name", None)
        if first:
            last = getattr(u, "last_name", None)
            return f"{first} {last}" if last else first
        return getattr(u, "username", None) or getattr(u, "email", None) or f"User {u.id}"

    def _login(self, interactive: bool = True) -> bool:
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
                    if self.session.access_token != previous_token:
                        self._save_session()
                    self._user_display_name = self._get_user_display_name()
                    return True
            except Exception as e:
                if not interactive and is_transport_failure(e):
                    self._start_offline(data)
                    return True
                logger.debug("Failed to load saved session: %s", e)

        if not interactive:
            return False

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
        if not self.session.check_login():
            self.console.print("[red]Login failed.[/red]")
            return False
        self._save_session()
        self._user_display_name = self._get_user_display_name()
        return True

    def _save_session(self) -> None:
        expiry = self.session.expiry_time
        try:
            save_tokens({
                "token_type": self.session.token_type,
                "access_token": self.session.access_token,
                "refresh_token": self.session.refresh_token,
                "expiry_time": expiry.isoformat() if hasattr(expiry, "isoformat") else expiry,
                "is_pkce": bool(self.session.is_pkce),
            })
        except Exception as e:
            logger.warning("Failed to save session: %s", e)

    def _start_offline(self, data: dict) -> None:
        """No network at start: keep the stored tokens on the session and play what is on
        disk. tidalapi can't make a content request from tokens alone (no countryCode until
        GET /sessions answers), so `_reconnect` reloads before the first one."""
        for field in ("token_type", "access_token", "refresh_token", "expiry_time"):
            setattr(self.session, field, data.get(field))
        self.session.is_pkce = bool(data.get("is_pkce", False))
        self._connectivity = OFFLINE
        user = self._read_state_dict().get("user")
        self._user_display_name = user if isinstance(user, str) else ""
        logger.warning("TIDAL unreachable at start; playing offline")

    def _watch_transport(self) -> None:
        http = vars(self.session).get("request_session")
        if http is not None and hasattr(http, "on_transport_failure"):
            http.on_transport_failure = self._went_offline

    def _went_offline(self) -> None:
        if self._connectivity != ONLINE:
            return
        self._connectivity = OFFLINE
        self._set_toast("Offline — downloaded and cached music still plays",
                        seconds=PLAYER_ERROR_SECONDS)
        self._wake()

    def _reconnect(self, recheck: bool = False) -> str:
        """Called only by an action that needs TIDAL, never a timer or probe (ADR-0003).
        From offline: one `load_oauth_session` in flight; callers that arrive meanwhile
        share its answer. A 401 is signed_out: the tokens stay and the player keeps
        playing, never `_logout()` (it deletes them). `recheck`: the same load from online,
        after a request got a 401, to learn whether TIDAL still takes the tokens."""
        want = ONLINE if recheck else OFFLINE
        if self._connectivity != want:
            return self._connectivity
        if not self._reconnect_lock.acquire(blocking=False):
            with self._reconnect_lock:  # one is in flight: its answer is ours
                return self._connectivity
        try:
            return self._reconnect_locked(want)
        finally:
            self._reconnect_lock.release()

    def _reconnect_locked(self, want: str) -> str:
        if self._connectivity != want:
            return self._connectivity
        http = vars(self.session).get("request_session")
        if http is not None:
            http.refresh_status = None
        data = load_tokens()
        try:
            if not data:
                raise RuntimeError("no stored login")
            loaded = self.session.load_oauth_session(
                data["token_type"], data["access_token"], data.get("refresh_token"),
                data.get("expiry_time"), is_pkce=data.get("is_pkce", False))
        except Exception as e:
            if not data or auth_rejected(e, getattr(http, "refresh_status", None)):
                self._signed_out()
            else:
                logger.debug("Still offline: %s", e)
            return self._connectivity
        if loaded is False:
            self._signed_out()
            return self._connectivity
        if self.session.access_token != data.get("access_token"):
            self._save_session()
        if want == ONLINE:
            return ONLINE
        self._connectivity = ONLINE
        self._user_display_name = self._get_user_display_name()
        self._set_toast("Back online")
        self._load_favorites()
        self._wake()
        return ONLINE

    def _signed_out(self) -> None:
        self._connectivity = SIGNED_OUT
        self._set_toast("Signed out by TIDAL: [o] to sign in again", seconds=PLAYER_ERROR_SECONDS)
        self._wake()

    def _login_device(self) -> bool:
        self.console.print("[cyan]Starting TIDAL login...[/cyan]")
        try:
            login, future = self.session.login_oauth()
        except Exception as e:
            logger.debug("Device login failed to start: %s", type(e).__name__)
            if is_transport_failure(e):
                self.console.print(NO_NETWORK_SIGN_IN)
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
        try:
            url = self.session.pkce_login_url()
        except Exception as e:
            logger.debug("Failed to build the PKCE login URL: %s", type(e).__name__)
            return False

        self.console.print("[cyan]Starting TIDAL sign-in for higher quality...[/cyan]")
        self.console.print("\n[bold yellow]1.[/bold yellow] Open this URL and sign in:\n")
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
            redirect = pasted if "https://" in pasted else (
                f"{self.session.config.pkce_uri_redirect}?code={quote(pasted)}"
            )
            try:
                token = self.session.pkce_get_auth_token(redirect)
                self.session.process_auth_token(token, is_pkce_token=True)
                return True
            except Exception as e:
                # Type only: the exception text can quote the pasted address (live auth code)
                logger.debug("PKCE token exchange failed: %s", type(e).__name__)
                if is_transport_failure(e):
                    self.console.print(NO_NETWORK_SIGN_IN)
                    return False
                if remaining:
                    self.console.print(
                        f"[red]That didn't work.[/red] [dim]Copy the full address, "
                        f"including everything after the '?'. {remaining} "
                        f"{'try' if remaining == 1 else 'tries'} left.[/dim]"
                    )
        return False

    def _upgrade_to_pkce(self) -> None:
        if self.remote is not None:
            # The paste needs this terminal; the player then loads the tokens it saved.
            if self._is_pkce():
                return
            with self._suspended_tui():
                upgraded = self._login_pkce() and self._finish_login()
            if upgraded:
                self._run("login.reload")
            else:
                self._set_toast("Sign-in cancelled — still signed in as before")
            return
        if self.session.is_pkce:
            return
        with self._suspended_tui():
            upgraded = self._login_pkce() and self._finish_login()
        if upgraded:
            self._quality_ceiling = None
            self._set_toast(
                "Signed in for higher quality — songs already cached still play "
                "as before; [x] clears them", seconds=6)
        else:
            self._set_toast("Sign-in cancelled — still signed in as before")

    def _sign_in_again(self) -> None:
        """TIDAL rejected the stored login: sign in here (it needs this terminal), then the
        player loads the new tokens. Same flow as the tokens it replaces."""
        data = load_tokens() or {}
        with self._suspended_tui():
            signed_in = (self._login_pkce() if data.get("is_pkce") else self._login_device()) \
                and self._finish_login()
        if signed_in:
            self._run("login.reload")
        else:
            self._set_toast("Sign-in cancelled — still signed out; downloads still play")

    def _suspended_tui(self):
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
                if live is not None:
                    live.start(refresh=False)
                self._last_segments = None

        return _suspend()

    def _restore_tty(self) -> None:
        if self._tty_settings is None:
            return
        try:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._tty_settings)
        except Exception as e:
            logger.debug("Failed to restore terminal settings: %s", e)

    def _raw_tty(self) -> None:
        if self._tty_settings is None:
            return
        try:
            import tty
            tty.setcbreak(sys.stdin.fileno())
        except Exception as e:
            logger.debug("Failed to set cbreak mode: %s", e)

    def _open_browser(self, url: str) -> None:
        if not (IS_MACOS or sys.platform == "win32"
                or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    def _run(self, command: str, /, **args) -> dict:
        if self.remote is None:
            return self.commands.execute(command, args, caller=HUMAN)
        self._pending[self.remote.send(command, args)] = self._remote_refusal
        return {"ok": True, "result": {"accepted": True}}

    def _honour_start_flags(self) -> None:
        """--quality and --login-flow reach only a player this run started (ADR-0008)."""
        running = self._mirror.get("quality")
        if self._asked_quality and running and running != self._asked_quality:
            self._set_toast(f"Player already running at {running}; change it in settings, "
                            "or quit and restart", seconds=PLAYER_ERROR_SECONDS)
        if self._asked_pkce and not self._is_pkce():
            self._upgrade_to_pkce()

    def _remote_refusal(self, response: dict) -> None:
        if not response.get("ok"):
            self._set_toast(response.get("reason") or "The player refused that",
                            seconds=PLAYER_ERROR_SECONDS)
        self._forget_disk_views()

    def _fetch(self, command: str, args: dict, done, known=()) -> None:
        """Run a read that may hit TIDAL off the input path; `done(response)` gets
        `{"ok", "result"}` or `{"ok": False, "reason"}`. TIDAL calls live in commands.py."""
        for kind, obj in known:
            self._remember(kind, [obj])
        if self.remote is not None:
            self._pending[self.remote.send(command, args)] = done
            return

        def _work():
            try:
                response = self.commands.execute(command, args, caller=HUMAN)
            except Exception as e:
                response = {"ok": False, "code": "failed", "reason": str(e)}
            try:
                done(response)
            finally:
                self._wake()

        threading.Thread(target=_work, daemon=True).start()

    KNOWN_MAX = 20000

    def _remember(self, kind: str, objs) -> None:
        known = self._known
        for obj in objs or ():
            if obj is not None:
                known[(kind, self._obj_id(obj))] = obj
        while len(known) > self.KNOWN_MAX:
            known.pop(next(iter(known)))

    def _list_changed(self, source: tuple, tracks: list) -> None:
        if source in self._lists:
            self._lists[source] = list(tracks)
        if self._browse_source == source:
            self._browse_tracks = list(tracks)
            self._browse_cursor = min(self._browse_cursor, len(tracks) - 1)
        if self._list_hook is not None:
            self._list_hook(source, tracks)

    _list_hook = None

    def _is_editable(self, playlist) -> bool:
        if isinstance(playlist, tidalapi.UserPlaylist):
            return True
        return self.remote is not None and bool(getattr(playlist, "editable", False))

    @staticmethod
    def _editable_type():
        return tidalapi.UserPlaylist

    def _stop_playback(self) -> None:
        self._clock_epoch += 1
        self._shutdown()
        self._playing = False
        self._play_start_time = None

    def _reload_login(self) -> None:
        data = load_tokens()
        if not data:
            raise RuntimeError("No saved login to load")
        self.session.load_oauth_session(
            data["token_type"], data["access_token"], data.get("refresh_token"),
            data.get("expiry_time"), is_pkce=data.get("is_pkce", False))
        was = self._connectivity
        self._connectivity = ONLINE
        self._user_display_name = self._get_user_display_name()
        self._quality_ceiling = None
        if was != ONLINE:
            self._set_toast("Signed in again")
            self._load_favorites()
            return
        self._set_toast(
            "Signed in for higher quality — songs already cached still play "
            "as before; [x] clears them", seconds=6)

    def _is_pkce(self) -> bool:
        if self.remote is not None:
            return bool(self._mirror.get("pkce"))
        return bool(self.session.is_pkce)

    def _backend_name(self) -> Optional[str]:
        if self.remote is not None:
            return self._mirror.get("backend")
        return self.audio.player_cmd if self.audio else None

    # ── the player's state as the TUI sees it (ADR-0008) ──

    def snapshot(self) -> dict:
        job = self._download_job
        run = self._download_run
        spec = get_spec("volume")
        return {
            "track": self._current_track,
            "queue": list(self._queue),
            "queue_index": self._queue_index,
            "clock": [bool(self._playing), self._play_offset, self._play_start_time],
            "liked": sorted(self._liked_ids, key=str),
            "toast": [self._toast, self._toast_until],
            "quality": self._quality_name,
            "ceiling": self._quality_ceiling,
            "badge": self._playing_badge,
            "user": self._user_display_name,
            "connectivity": self._connectivity,
            "pkce": bool(getattr(self.session, "is_pkce", False)),
            "config": {k: v for k, v in self.config.items() if k not in PROTECTED_KEYS},
            "switches": {k: bool(coerce(get_spec(k), self.config.get(k))) for k in PROTECTED_KEYS},
            "backend": self._backend_name(),
            "volume_ceiling": self._setting_ceiling(spec),
            "download_job": _wire_job(job),
            "download_run": run is not None,
            "download_queued": sorted(self._download_queued_ids(), key=str),
            "refetch_job": _wire_job(self._refetch_job),
            "editable_playlists": list(self._editable_playlists),
            "last_playlist_id": self._last_playlist_id,
            "picker_busy": bool(self._picker_busy),
            "remove_busy": bool(self._browse_remove_busy),
            "history": list(self._search_history),
        }

    def _apply_state(self, state: dict) -> None:
        mirror = self._mirror
        for key, value in state.items():
            mirror[key] = value
            if key == "track":
                self._current_track = value
            elif key == "queue":
                self._queue = list(value)
                self._queue_cursor = max(0, min(self._queue_cursor, len(self._queue) - 1))
            elif key == "queue_index":
                self._queue_index = value
            elif key == "clock":
                self._playing, self._play_offset, self._play_start_time = value
            elif key == "liked":
                self._liked_ids = set(value)
            elif key == "toast":
                self._toast, self._toast_until = value
            elif key == "quality":
                self._quality_name = value
            elif key == "ceiling":
                self._quality_ceiling = value
            elif key == "badge":
                self._playing_badge = value
            elif key == "user":
                self._user_display_name = value
            elif key == "connectivity":
                self._connectivity = value
            elif key == "config":
                for name, setting in value.items():
                    if self.config.get(name) != setting:
                        self.config[name] = setting
                        self._apply_ui_setting(name, setting)
            elif key == "switches":
                for name, on in value.items():
                    if name != "ai_control_key":
                        self.config[name] = on
                    elif bool(self.config.get(name)) != on:
                        # A stand-in: the hash stays on disk, never on the socket.
                        self.config[name] = {"salt": "-", "hash": "-"} if on else None
            elif key in ("download_job", "refetch_job"):
                setattr(self, "_" + key, value)
                self._forget_disk_views()
            elif key == "download_run":
                self._download_run = True if value else None
            elif key == "editable_playlists":
                self._editable_playlists = list(value)
                self._editable_playlists_time = time.time()
            elif key == "last_playlist_id":
                self._last_playlist_id = value
            elif key == "picker_busy":
                self._picker_busy = value
            elif key == "remove_busy":
                self._browse_remove_busy = value
            elif key == "history":
                self._search_history = list(value)
                if self._search_history_cursor is not None:
                    self._search_history_cursor = min(self._search_history_cursor,
                                                      len(value) - 1) if value else None

    def _apply_ui_setting(self, key: str, value) -> None:
        if key == "quality":
            self._quality_name = value
        elif key == "page_size":
            self._page_size = value
        elif key == "progress_bar_max":
            self._bar_max = value
        elif key == "show_artwork":
            self._show_artwork = value
            if not value:
                self._artwork = None
                self._artwork_request = None

    def _on_message(self, message: dict) -> None:
        if message.get("event") == "state":
            self._apply_state(message.get("state") or {})
        elif message.get("event") == "list":
            source = tuple(message.get("source") or ())
            self._list_changed(source, message.get("tracks") or [])
        elif "id" in message:
            done = self._pending.pop(message["id"], None)
            if done is not None:
                done(message)

    def _forget_disk_views(self) -> None:
        self._forget_downloads()
        self._cache.invalidate_audio_count()

    def _logout(self):
        self._run("logout")
        if self.remote is not None:
            self._logged_out = True
            self.running = False

    def _note_agent_action(self, what: str) -> None:
        self._set_toast(f"agent: {what}")
        self._wake()

    def _load_favorites(self):
        if self._connectivity != ONLINE:
            return

        def _run():
            try:
                favs = self.session.user.favorites.tracks(limit=999)
                self._liked_ids = {t.id for t in favs}
                self._cache.put_items("favorites:tracks", favs)
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _write_state_file(self, state: dict):
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        # One fixed temp path: safe only because _take_instance_lock allows a single ticli
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_FILE)

    def _read_state_dict(self) -> dict:
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
        with self._state_lock:
            self._merge_position_into_saved_state_locked()

    def _merge_position_into_saved_state_locked(self):
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
        pid = str(getattr(playlist, "id", "") or "")
        if not pid:
            return
        self._last_playlist_id = pid
        with self._state_lock:
            data = self._read_state_dict()
            data["last_playlist_id"] = pid
            try:
                self._write_state_file(data)
            except OSError as e:
                logger.debug("Failed to persist last-used playlist: %s", e)

    def _save_state(self):
        with self._state_lock:
            # Restore never attached the queue: a full save would shrink the good file.
            # Refresh position only, via the unlocked half (lock held, not reentrant).
            if self._restore_pending and not self._queue:
                self._merge_position_into_saved_state_locked()
                return
            self._save_state_locked()

    def _save_state_locked(self):
        try:
            queue = self._queue
            queue_index = self._queue_index
            if not queue and self._current_track is not None:
                queue = [self._current_track]
                queue_index = 0
            state = {
                # Kept so a downgrade to a pre-record build still resumes
                "track_ids": [t.id for t in queue],
                "tracks": [track_record(t) for t in queue],
                "queue_index": queue_index,
                "position": self._get_position(),
                "search_history": self._search_history[:200],
            }
            if self._user_display_name:
                state["user"] = self._user_display_name
            if self._last_playlist_id:
                state["last_playlist_id"] = self._last_playlist_id
            self._write_state_file(state)
        except Exception as e:
            logger.debug("Failed to save player state: %s", e)

    def _shutdown(self):
        if self.audio:
            position = self.audio.get_time_pos()
            if position is not None:
                self._play_offset = position
                self._play_start_time = time.time()
            self.audio.stop()
        self._save_state()

    def _restore_state(self):
        data = self._read_state_dict()
        if not data:
            return
        history = data.get("search_history", [])
        if not isinstance(history, list):
            history = []
        self._search_history = [h for h in history if isinstance(h, str)][:200]
        self._last_playlist_id = data.get("last_playlist_id") or None
        track_ids = data.get("track_ids", [])
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
            offline = False
            abandoned = False
            last_start = None

            def _fetch(tid):
                nonlocal blocked, offline, abandoned, last_start
                if last_start is not None:
                    remaining = REFETCH_MIN_INTERVAL - (time.monotonic() - last_start)
                    if remaining > 0:
                        _restore_sleep(remaining)
                if not (self.running and self._restore_pending):
                    abandoned = True
                    return None
                if self._connectivity != ONLINE:
                    offline = True
                    return None
                last_start = time.monotonic()
                try:
                    return self.session.track(tid)
                except Exception as e:
                    if is_transport_failure(e):
                        offline = True
                        logger.debug("Could not reach TIDAL to restore the queue: %s", e)
                    elif _rate_limited(e):
                        blocked = str(e)[:PLAYER_ERROR_CHARS]
                    else:
                        logger.debug("Could not restore track %s: %s", tid, e)
                    return None

            try:
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
                    if blocked or offline or abandoned:
                        break
                    t = _fetch(tid)
                    if t is not None:
                        tracks.append(t)
                if blocked:
                    # Never retry (docs/adr/0001: retries turned a rate limit into an edge block)
                    self._set_toast(
                        "TIDAL is rate-limiting — restore stopped. "
                        "Nothing will be retried.",
                        seconds=PLAYER_ERROR_SECONDS)
                    self._wake()
                    return
                if offline:
                    self._set_toast(f"Queue not restored — {OFFLINE_MESSAGE}",
                                    seconds=PLAYER_ERROR_SECONDS)
                    self._wake()
                    return
                if abandoned:
                    # Don't attach: a partial queue could be saved over the whole one on disk
                    return
                if tracks and self._restore_pending and self._current_track in (None, current):
                    self._queue = tracks
                    try:
                        self._queue_index = tracks.index(current) if current is not None else min(idx, len(tracks) - 1)
                    except ValueError:
                        self._queue_index = min(idx, len(tracks) - 1)
                    if self._current_track is None:
                        self._current_track = tracks[self._queue_index]
                    # Only a SUCCESSFUL attach re-enables full saves
                    self._restore_pending = False
            except Exception as e:
                logger.debug("Failed to restore player state: %s", e)

        threading.Thread(target=_run, daemon=True).start()
    def _play_track(self, track: tidalapi.Track, seek: float = 0, automatic: bool = False):
        """`automatic` (auto-advance, a vanished source) never reconnects (ADR-0003)."""
        self._track_changing = True
        self._play_gen = gen = self._play_gen + 1
        self._clock_epoch += 1
        self._seek_target = None
        self._prefetch_id = None
        self._current_track = track
        self._playing = True
        self._play_start_time = None
        self._play_offset = seek

        def _run():
            try:
                # Look for a local copy before resolving a cached row or asking for the stream URL:
                # replaying a cached playlist spent a playbackinfo request per track, which got
                # the owner blocked, and a row with a file on disk must play with no network
                local, badge = self._local_source(track)
                if local:
                    real, url, granted = track, "", None
                else:
                    if (self._connectivity if automatic else self._reconnect()) != ONLINE:
                        raise _NoLocalCopy(track, self._connectivity)
                    # A cached row carries no stream URL; resolve it directly so a network
                    # failure propagates to the play-failure toast
                    real = self.session.track(track.id) if getattr(track, "cached", False) else track
                    if real is None:
                        raise RuntimeError("TIDAL could not find this track")
                    if real is not track:
                        self._swap_queue_entry(self._queue, track, real)
                        if self._play_gen == gen:
                            self._current_track = real
                    url, granted = (self._take_prefetched(real.id)
                                    or self._stream_description(real))
                if self._play_gen != gen or not self._playing:
                    return
                artist = ", ".join(a.name for a in real.artists) if real.artists else ""
                title = f"{real.name} — {artist}" if artist else real.name
                self._playing_badge = badge
                self.audio.play_url(url, seek=seek, title=title, cache_key=real.id,
                                    local=local, quality=granted)
                if self._play_gen != gen:
                    return
                self._clock_epoch += 1
                self._playing = True
                self._play_start_time = time.time()
                self._play_offset = seek
                if (local and getattr(track, "cached", False) and not artwork.cover_id_of(track)
                        and self._connectivity == ONLINE):
                    self._resolve_for_artwork(track, gen)
            except backend_health.SpawnError as e:
                if self._play_gen == gen:
                    self._playing = False
                    self._report_player_failure(e.failure)
            except Exception as e:
                logger.warning("Could not play %s: %r", getattr(track, "id", None), e)
                if is_transport_failure(e):
                    self._went_offline()
                elif unauthorized(e):
                    self._reconnect(recheck=True)
                if self._play_gen == gen:
                    self._playing = False
                    self._play_start_time = None
                    self._set_toast(_play_failure_text(e), seconds=PLAYER_ERROR_SECONDS)
                    self._wake()
            finally:
                if self._play_gen == gen:
                    self._track_changing = False

        threading.Thread(target=_run, daemon=True).start()

    def _swap_queue_entry(self, queue, row, resolved) -> None:
        # In place, by identity: rebuilding the list could undo a removal made meanwhile
        if self._queue is not queue:
            return
        i = next((i for i, t in enumerate(queue) if t is row), None)
        if i is not None:
            queue[i] = resolved

    def _resolve_for_artwork(self, track, gen) -> None:
        queue = self._queue

        def _run():
            if self._play_gen != gen:
                return
            real = self._resolve_track(track)
            if real is None or real is track:
                return
            self._swap_queue_entry(queue, track, real)
            if self._play_gen == gen and self._current_track is track:
                self._current_track = real
                self._wake()

        threading.Thread(target=_run, daemon=True).start()

    def _local_source(self, track) -> tuple:
        track_id = getattr(track, "id", None)
        owned = downloads.path_for(track_id)
        if owned is not None:
            entry = downloads.load_index().get(str(track_id)) or {}
            return str(owned), self._download_badge(entry.get("granted"))
        cached = cached_audio_path(track_id) if self._cache.keeps_audio else None
        if cached:
            record = self._cache.audio_record(track_id) or {}
            # Offline, a copy below the chosen tier beats silence.
            if self._connectivity != ONLINE or self._tier_is_enough(record.get("quality")):
                return cached, None
        return None, None

    def _download_badge(self, granted) -> Optional[str]:
        label = self._tier_label(granted)
        return f"{label} · downloaded" if label else "downloaded"

    def _tier_label(self, granted) -> Optional[str]:
        for name, quality in self.QUALITY_MAP.items():
            if granted == quality:
                return self.QUALITY_LABELS.get(name)
        return None

    def _tier_is_enough(self, stored) -> bool:
        wanted = self.QUALITY_MAP.get(self._quality_name)
        if stored not in QUALITY_RANK or wanted not in QUALITY_RANK:
            return True
        if self._quality_unavailable(self._quality_name):
            return True
        return QUALITY_RANK[stored] >= QUALITY_RANK[wanted]

    def _take_prefetched(self, track_id) -> Optional[tuple]:
        prefetched = self._prefetch
        self._prefetch = None
        if not prefetched:
            return None
        pid, url, granted, fetched_at = prefetched
        if pid != track_id or time.time() - fetched_at > PREFETCH_MAX_AGE:
            return None
        return url, granted

    def _maybe_prefetch_next(self):
        if self._prefetch_id is not None or not self._playing or self._connectivity != ONLINE:
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
        self._prefetch_id = track_id
        if self._local_source(nxt)[0] is not None:
            return
        queue, gen = self._queue, self._play_gen

        def _run():
            try:
                real = self._resolve_track(nxt)
                if real is not None and real is not nxt and self._play_gen == gen:
                    self._swap_queue_entry(queue, nxt, real)
                if real is not None:
                    url, granted = self._stream_description(real)
                    self._prefetch = (real.id, url, granted, time.time())
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _stream_url(self, track) -> str:
        return self._stream_description(track)[0]

    def _stream_description(self, track, quality=None) -> tuple:
        with self._quality_lock:
            wanted = quality or self.QUALITY_MAP.get(self._quality_name)
            if wanted:
                self.session.audio_quality = wanted
            stream = track.get_stream()
        granted = getattr(stream, "audio_quality", None)
        self._note_granted_quality(granted, wanted)
        manifest = stream.get_stream_manifest()
        if manifest.is_bts:
            return manifest.get_urls()[0], granted
        return (_write_hls_playlist(track.id, _hls_playlist(manifest.dash_info)),
                granted)

    def _note_granted_quality(self, granted: Optional[str], wanted) -> None:
        if granted not in QUALITY_RANK or wanted not in QUALITY_RANK:
            return
        if QUALITY_RANK[granted] < QUALITY_RANK[wanted]:
            self._quality_ceiling = granted
        elif self._quality_ceiling is not None and \
                QUALITY_RANK[granted] > QUALITY_RANK[self._quality_ceiling]:
            self._quality_ceiling = None

    def _quality_unavailable(self, choice: str) -> bool:
        ceiling = self._quality_ceiling
        wanted = self.QUALITY_MAP.get(choice)
        if ceiling is None or wanted not in QUALITY_RANK:
            return False
        return QUALITY_RANK[wanted] > QUALITY_RANK[ceiling]

    def _resolve_track(self, track):
        if track is None or not getattr(track, "cached", False):
            return track
        try:
            return self.session.track(track.id)
        except Exception:
            return None

    def _play_queue_index(self, index: int):
        if 0 <= index < len(self._queue):
            self._queue_index = index
            self._play_track(self._queue[index])

    def _next_track(self):
        self._advance(1)

    def _prev_track(self):
        if self._current_track is not None and self._get_position() > PREV_RESTART_SECONDS:
            self._restart_current_track()
            return
        self._advance(-1)

    def _has_local_copy(self, track, owned: set) -> bool:
        track_id = getattr(track, "id", None)
        return str(track_id) in owned or (
            self._cache.keeps_audio and cached_audio_path(track_id) is not None)

    def _advance(self, step: int, automatic: bool = False) -> bool:
        """Next/prev and auto-advance. Offline, entries with no local copy are skipped,
        with one toast; nothing local left stops playback. Never reconnects."""
        index = self._queue_index + step
        if self._connectivity == ONLINE:
            if not 0 <= index < len(self._queue):
                return False
            self._queue_index = index
            self._play_track(self._queue[index], **({"automatic": True} if automatic else {}))
            return True
        owned = {row["id"] for row in downloads.present()}
        skipped = 0
        while 0 <= index < len(self._queue) and not self._has_local_copy(self._queue[index], owned):
            index += step
            skipped += 1
        if not 0 <= index < len(self._queue):
            if skipped:
                self._set_toast("Offline — nothing further in the queue has a local copy",
                                seconds=PLAYER_ERROR_SECONDS)
            return False
        self._toast_skipped(skipped)
        self._queue_index = index
        self._play_track(self._queue[index], **({"automatic": True} if automatic else {}))
        return True

    def _toast_skipped(self, skipped: int) -> None:
        if skipped:
            self._set_toast(f"Offline — skipped {skipped} track{'' if skipped == 1 else 's'} "
                            "with no local copy", seconds=PLAYER_ERROR_SECONDS)

    def _restart_current_track(self):
        self._clock_epoch += 1
        self._seek_target = None
        if self._playing and self.audio and self.audio.seek_to_start():
            self._play_offset = 0
            self._play_start_time = time.time()
            return
        self._play_track(self._current_track, seek=0)

    def _seek_by(self, delta: float):
        if self._current_track is None:
            return
        self._clock_epoch += 1
        duration = self._track_duration()
        target = max(0.0, self._get_position() + delta)
        if duration > 0:
            target = min(target, max(0.0, duration - SEEK_END_MARGIN))
        self._play_offset = target
        self._play_start_time = time.time() if self._playing else None
        self._seek_target = target
        self._flush_seek()

    def _seek_pending(self) -> bool:
        return self._seek_applying or (
            self._seek_target is not None and self._seek_target != self._seek_applied)

    def _flush_seek(self):
        target = self._seek_target
        if target is None or target == self._seek_applied or self._seek_applying:
            return
        if time.monotonic() - self._last_seek_apply < SEEK_COALESCE_SECONDS:
            return
        self._seek_applying = True
        self._last_seek_apply = time.monotonic()
        gen = self._play_gen
        track = self._current_track

        def _run():
            try:
                if self._play_gen != gen:
                    return
                if not (self.audio and self.audio.seek_to(target)):
                    self._play_track(track, seek=target)
                self._seek_applied = target
            except backend_health.SpawnError as e:
                # Mark delivered anyway: retrying each tick would repeat the failure and the probe
                self._seek_applied = target
                if self._play_gen == gen:
                    self._playing = False
                    self._report_player_failure(e.failure)
            finally:
                self._seek_applying = False

        threading.Thread(target=_run, daemon=True).start()

    def _toggle_play_key(self):
        now = time.monotonic()
        repeat = now - self._last_toggle_key < KEY_REPEAT_WINDOW
        self._last_toggle_key = now
        if not repeat:
            self._run("toggle")

    def _toggle_play(self):
        self._clock_epoch += 1
        if self._playing:
            self.audio.pause()
            self._playing = False
            if self._play_start_time:
                self._play_offset += time.time() - self._play_start_time
                self._play_start_time = None
            return
        if not self._current_track:
            return
        if self.audio and self.audio.is_paused:
            try:
                resumed = self.audio.resume()
            except backend_health.SpawnError as e:
                self._playing = False
                # Off the UI thread: the report runs a version probe per backend
                threading.Thread(target=self._report_player_failure,
                                 args=(e.failure,), daemon=True).start()
                return
            if resumed:
                self._playing = True
                self._play_start_time = time.time()
                return
        self._start_current_from_position()

    def _start_current_from_position(self):
        seek = self._get_position()
        duration = self._track_duration()
        # Clamp unconditionally: an unknown duration must not let a stale
        # offset seek past EOF (mpv exits instantly on that)
        if seek < 1 or seek >= duration - 2:
            seek = 0
        self._play_track(self._current_track, seek=seek)

    def _toggle_like(self):
        if not self._current_track:
            return
        tid = self._current_track.id
        self._run("unlike" if tid in self._liked_ids else "like", track_id=tid)

    def _start_track_radio(self):
        track = self._current_track
        if track is None:
            return
        now = time.monotonic()
        if self._radio_fetching or now - self._radio_last_fetch < RADIO_FETCH_MIN_INTERVAL:
            return
        self._radio_fetching = True
        self._radio_last_fetch = now
        gen = self._play_gen
        self._set_toast("Starting radio…")

        def _run():
            tracks = None
            try:
                real = self._resolve_track(track)
                if real is not None:
                    tracks = real.get_track_radio(limit=RADIO_LIMIT)
            except Exception as e:
                logger.debug("Track radio failed: %s", e)
            finally:
                self._radio_fetching = False
            if self._play_gen != gen:
                return
            if not tracks or not self._apply_radio_queue(tracks):
                self._set_toast("Radio unavailable — queue unchanged")
            self._wake()

        threading.Thread(target=_run, daemon=True).start()

    def _apply_radio_queue(self, tracks: list):
        head = self._current_track
        if head is None:
            return False
        seed_id = getattr(head, "id", None)
        queue = [head] + [t for t in tracks if getattr(t, "id", None) != seed_id]
        if len(queue) < 2:
            return False
        self._queue = queue
        self._queue_index = 0
        self._prefetch = None
        self._prefetch_id = None
        self._queue_cursor = 0
        # A restore still fetching the old queue must not land on top of this one
        self._restore_pending = False
        self._set_toast(f"Radio started — {len(queue) - 1} tracks up next")
        return True

    def _handle_media_key(self, action):
        command = {"next": "next", "prev": "prev", "toggle": "toggle",
                   "play": "resume", "pause": "pause"}.get(action)
        if command:
            self._run(command)

    def _monitor_playback(self):
        last_save = time.time()
        dead_polls = 0
        while self.running:
            if (self._playing and not self._track_changing and self.audio
                    and not self._seek_applying
                    and not self.audio.is_paused and not self.audio.is_playing):
                # Two consecutive dead polls: a track change kills the old process
                # before spawning the new one, and one poll can land in that window
                dead_polls += 1
            else:
                dead_polls = 0
            if dead_polls >= 2:
                dead_polls = 0
                failure = self.audio.failure() if self.audio else None
                if (failure and self.audio and not self.audio.source_vanished()
                        and self._track_has_time_left()):
                    self._playing = False
                    self._play_start_time = None
                    self._report_player_failure(failure)
                elif (self.audio and self._current_track is not None
                        and self.audio.source_vanished()
                        and self._track_has_time_left()):
                    # Cached file deleted between "it exists" and the player opening it: restart
                    # from the network. Not for a finished track (a cache clear leaves it gone too)
                    if self._connectivity == ONLINE:
                        self._play_track(self._current_track, seek=self._get_position())
                    else:
                        self._playing = False
                        self._play_start_time = None
                        self._set_toast(f"Playback stopped — {OFFLINE_MESSAGE}",
                                        seconds=PLAYER_ERROR_SECONDS)
                elif self._stream_ended_early():
                    # Both backends play out their buffer and exit 0 with empty stderr on a dead
                    # source, indistinguishable from a finished song except by the clock. Stop and
                    # say so rather than advancing. Auto-restart was considered and rejected: a
                    # silent retry against a dead URL is the same silence with more requests,
                    # and [space] refetches a fresh URL.
                    position = self._get_position()
                    duration = self._track_duration()
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
                    self._report_player_failure(None)
                elif not self._advance(1, automatic=True):
                    self._playing = False
                    self._play_start_time = None
                    self._play_offset = 0
            elif self._playing and self.audio:
                # Resync with mpv's real position, but not while a scrub is still on its way
                if not self._seek_pending():
                    epoch = self._clock_epoch
                    pos = self.audio.get_time_pos()
                    if pos is not None and epoch == self._clock_epoch and self._playing:
                        self._play_offset = pos
                        self._play_start_time = time.time()
                self._maybe_count_play()
                self._maybe_prefetch_next()
            self._flush_seek()
            self._sample_download_rates()
            self._reclaim_deferred_copies()
            if time.time() - last_save > 10:
                self._save_state()
                last_save = time.time()
            if self.audio:
                self._handle_media_key(self.audio.poll_media_key())
            if self._tick_hook is not None:
                self._tick_hook()
            time.sleep(0.5)

    def _track_duration(self):
        return getattr(self._current_track, "duration", 0) or 0

    def _report_player_failure(self, failure) -> None:
        # Probes only after a failure, never at startup (a subprocess per backend). Never call on the UI thread.
        probes = backend_health.probe_backends(AUDIO_PLAYERS)
        if failure is None:
            player = getattr(self.audio, "player_cmd", None)
            active = next((p for p in probes if p.player == player), None)
            if active is None or active.ok:
                return
            failure = active.failure
        message = backend_health.describe(failure, probes)
        self._set_toast(f"Playback failed — {message}",
                        seconds=PLAYER_ERROR_SECONDS)
        logger.warning("Playback failed [%s]: %s (%s)",
                       failure.code, message, failure.detail)
        self._wake()

    def _track_has_time_left(self) -> bool:
        duration = self._track_duration()
        return duration <= 0 or self._get_position() < duration - 2.0

    def _maybe_count_play(self) -> None:
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
        except Exception as e:
            logger.debug("Could not record a play: %s", e)

    def _stream_ended_early(self) -> bool:
        duration = self._track_duration()
        return duration > 0 and self._get_position() < duration - STREAM_TRUNCATED_MARGIN

    def _get_position(self) -> float:
        if self._play_start_time and self._playing:
            return self._play_offset + (time.time() - self._play_start_time)
        return self._play_offset

    def _art_layout(self):
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
        if size is None:
            return None
        return size[0], size[1], beside

    def _artwork_text(self):
        layout = self._art_layout()
        if layout is None:
            return None
        cols, rows, _ = layout
        cover = artwork.cover_id_of(self._current_track)
        ready = self._artwork
        if ready is not None and ready[:3] == (cover, cols, rows):
            return artwork.render(ready[3], indent=INDENT) if ready[3] else None
        self._request_artwork(cover, cols, rows)
        return None

    def _request_artwork(self, cover: str, cols: int, rows: int):
        key = (cover, cols, rows)
        if self._artwork_request == key:
            return
        self._artwork_request = key

        def _run():
            try:
                pixels = artwork.load(cover, cols, rows)
            except Exception:
                pixels = None
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

        status_line = Text()
        if self._queue:
            status_line.append(f"   Queue: {self._queue_index + 1}/{len(self._queue)}", style="dim")
        quality_label = self._playing_badge or \
            self.QUALITY_LABELS.get(self._quality_name, "")
        if quality_label:
            status_line.append(f"   {quality_label}", style="dim cyan")

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
        return max(1, min(self._page_size, self._fit.page_rows))

    def _tab_row(self, order, labels, active, mark=None, tight=False) -> Text:
        for separator in (" · ", "  ") if tight else (" · ",):
            row = Text()
            row.append("\n   [Tab]", style="bold")
            for i, name in enumerate(order):
                row.append("  " if i == 0 else separator, style="dim")
                row.append(labels[name],
                           style="bold cyan" if name == active else "dim")
                if mark is not None and mark(name):
                    row.append("·", style="dim cyan")
            if cell_len(row.plain.lstrip("\n")) < self._fit.inner:
                break
        if cell_len(row.plain.lstrip("\n")) > self._fit.inner:
            row = Text("\n   [Tab]", style="bold")
            row.append(f"  {labels[active]}", style="bold cyan")
            if mark is not None and mark(active):
                row.append("·", style="dim cyan")
            row.append(f"  ({order.index(active) + 1}/{len(order)})",
                       style="dim")
        return row

    @staticmethod
    def _mark_cursor(content: Text, selected: bool) -> None:
        content.append("  ▸ " if selected else "    ",
                       style="bold cyan" if selected else "")

    def _page_footer(self, content: Text, cursor: int, total: int, page: int):
        if total > page:
            page_num = (cursor // page) + 1
            total_pages = (total + page - 1) // page
            content.append(f"\n\n   Page {page_num}/{total_pages}", style="dim")

    def _build_search_display(self) -> Text:
        content = Text()
        content.append("   Search: ", style="bold yellow")
        content.append(self._search_query, style="white")
        content.append("\u2588", style="bold white")

        content.append_text(self._tab_row(
            self.SEARCH_FILTERS, self.SEARCH_FILTER_LABELS,
            self._search_filter, mark=self._scope_answered, tight=True))

        if self._search_loading and not self._search_results:
            content.append("\n\n   Searching...", style="dim yellow")
        elif self._search_message:
            content.append(f"\n\n   {self._search_message}", style="dim green")
        elif self._search_results:
            total = len(self._search_results)
            page = self._page_rows()
            page_start = (self._search_cursor // page) * page
            page_end = min(page_start + page, total)
            content.append("\n")
            for i in range(page_start, page_end):
                item = self._search_results[i]
                content.append("\n")
                self._mark_cursor(content, i == self._search_cursor)
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
                content.append("\n\n   Loading more...", style="dim yellow")
            elif self._search_view()["cached"]:
                content.append("\n\n   Already loaded this session", style="dim")
            if total > page:
                page_num = (self._search_cursor // page) + 1
                total_pages = (total + page - 1) // page
                content.append(f"\n\n   Page {page_num}/{total_pages}", style="dim")
                content.append(f"  ({total} results)", style="dim")
                if (page_num == total_pages and self._search_done
                        and not self._pool_size(self._search_pool)):
                    content.append("  end of results", style="dim")
        elif self._search_query:
            content.append("\n\n   Press Enter to search", style="dim")
        elif self._history_rows():
            rows = self._history_rows()
            page = self._page_rows()
            page_start = (0 if self._search_history_cursor is None
                          else (self._search_history_cursor // page) * page)
            content.append("\n\n   Recent searches", style="dim")
            content.append("\n")
            for i in range(page_start, min(page_start + page, len(rows))):
                content.append("\n")
                selected = i == self._search_history_cursor
                self._mark_cursor(content, selected)
                content.append(rows[i], style="bold white" if selected else "white")

        return content

    def _age_note(self, content: Text, fetched) -> None:
        """Offline, a list from disk says how old it is (ADR-0009)."""
        if fetched is not None and self._connectivity != ONLINE:
            content.append(f"  {age_label(fetched)}", style="yellow")

    def _build_browse_display(self) -> Text:
        content = Text()
        content.append(f"   {self._browse_title}", style="bold magenta")
        self._age_note(content, self._browse_fetched)

        if self._browse_loading:
            content.append("\n\n   Loading...", style="dim yellow")
        elif self._browse_message:
            content.append(f"\n\n   {self._browse_message}", style="dim green")
        elif self._browse_tracks:
            total = len(self._browse_tracks)
            page = self._page_rows()
            page_start = max(0, ((self._browse_cursor - 1) // page) * page) if self._browse_cursor > 0 else 0
            page_end = min(page_start + page, total)
            content.append(f"  ({total} tracks)", style="dim")
            content.append("\n")

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
                self._mark_cursor(content, i == self._browse_cursor)
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
        self._age_note(content, (self._artist_record() or {}).get("fetched"))

        content.append_text(self._tab_row(
            self.ARTIST_SECTIONS, self.ARTIST_SECTION_LABELS,
            self._artist_section))

        record = self._artist_record()
        label = self.ARTIST_SECTION_LABELS[self._artist_section].lower()
        if record is None or record["state"] == "loading":
            content.append(f"\n\n   Loading {label}...", style="dim yellow")
            return content
        if record["state"] == "failed":
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
        content.append("\n")
        type_styles = {"track": "bold green", "album": "bold magenta",
                       "playlist": "bold cyan", "artist": "bold yellow"}
        for i in range(page_start, page_end):
            row = rows[i]
            obj = row["obj"]
            selected = (i == cursor)
            content.append("\n")
            self._mark_cursor(content, selected)
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
            content.append("\n")
            for i in range(page_start, page_end):
                track = self._queue[i]
                content.append("\n")
                is_current = (i == self._queue_index)
                is_cursor = (i == self._queue_cursor)
                if is_cursor:
                    self._mark_cursor(content, True)
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
        self._age_note(content, self._playlists_fetched)

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
            content.append("\n")
            for i in range(page_start, page_end):
                pl = self._playlists[i]
                content.append("\n")
                self._mark_cursor(content, i == self._playlists_cursor)
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

        content.append("\n\n")
        if self._picker_new_name is not None:
            content.append("  ▸ ", style="bold cyan")
            content.append("New playlist: ", style="bold white")
            content.append(f"‹ {self._picker_new_name}▏›", style="bold yellow")
        else:
            selected = self._picker_cursor < 0
            self._mark_cursor(content, selected)
            content.append("+ New playlist", style="bold white" if selected else "white")

        playlists = self._picker_playlists()
        if self._picker_loading:
            content.append("\n\n   Loading playlists...", style="dim yellow")
        elif playlists:
            total = len(playlists)
            page = self._page_rows()
            page_start = (max(self._picker_cursor, 0) // page) * page
            page_end = min(page_start + page, total)
            content.append("\n")
            for i in range(page_start, page_end):
                pl = playlists[i]
                content.append("\n")
                self._mark_cursor(content, i == self._picker_cursor)
                pl_name = pl.name if hasattr(pl, "name") else "?"
                num_tracks = pl.num_tracks if hasattr(pl, "num_tracks") else ""
                content.append(pl_name, style="bold white" if i == self._picker_cursor else "white")
                if num_tracks != "":
                    content.append(f"  {num_tracks} tracks", style="dim cyan")
                if i == 0 and self._last_playlist_id and str(
                        getattr(pl, "id", "") or "") == self._last_playlist_id:
                    content.append("  last used", style="dim")
            self._page_footer(content, max(self._picker_cursor, 0), total, page)
        else:
            content.append("\n\n   No playlists you can edit", style="dim")

        return content

    def _reconcile_cache(self) -> None:
        def _run():
            try:
                dropped, adopted = self._cache.reconcile()
                if dropped or adopted:
                    logger.debug("Cache tracker reconciled: -%d +%d",
                                 dropped, adopted)
                    self._wake()
            except Exception as e:
                logger.debug("Could not reconcile the cache tracker: %s", e)
            try:
                self._reclaim_download_duplicates()
            except Exception as e:
                logger.debug("Could not sweep download duplicates: %s", e)

        threading.Thread(target=_run, daemon=True).start()

    def _downloads(self) -> list:
        if self._downloads_present is None:
            self._downloads_present = downloads.present()
        return self._downloads_present

    def _downloaded(self, track_id) -> bool:
        if track_id is None:
            return False
        if self._downloads_ids is None:
            self._downloads_ids = {row["id"] for row in self._downloads()}
        return str(track_id) in self._downloads_ids

    def _forget_downloads(self) -> None:
        self._downloads_present = None
        self._downloads_ids = None
        self._download_known = None

    def _download_usage(self) -> tuple:
        rows = self._downloads()
        return len(rows), sum(row["bytes"] for row in rows)

    def _download_mark(self, content: Text, track_id) -> None:
        downloaded = self._downloaded(track_id)
        content.append("↓ " if downloaded else "  ", style="dim cyan" if downloaded else "")

    def _build_settings_display(self) -> Text:
        content = Text()
        content.append("   Settings", style="bold magenta")

        room = max(1, min(len(SETTINGS_ROWS), self._fit.page_rows))
        first = 0
        if room < len(SETTINGS_ROWS):
            first = max(0, min(self._settings_cursor - room // 2,
                               len(SETTINGS_ROWS) - room))
            content.append(f"  ({self._settings_cursor + 1}/{len(SETTINGS_ROWS)})",
                           style="dim")
        content.append("\n")

        for i in range(first, first + room):
            spec = SETTINGS_ROWS[i]
            selected = (i == self._settings_cursor)
            content.append("\n")
            self._mark_cursor(content, selected)
            label = f"{spec['label']:<20}" if self._fit.inner >= 46 else spec["label"] + " "
            content.append(label, style="bold white" if selected else "white")
            value = self.config.get(spec["key"], spec["default"])
            editing = selected and self._settings_edit is not None
            secret = selected and self._settings_secret is not None
            if editing:
                content.append(f"‹ {self._settings_edit}▏›", style="bold yellow")
            elif secret:
                content.append(f"‹ {'•' * len(self._settings_secret)}▏›", style="bold yellow")
            elif selected:
                content.append(f"‹ {display_value(spec, value)} ›", style="bold cyan")
            else:
                content.append(f"  {display_value(spec, value)}", style="dim cyan")
            meaning = spec.get("value_desc", {}).get(str(value).upper(), "")
            if meaning:
                content.append(f"  {meaning}", style="dim")
            if spec["key"] == "cache_songs":
                songs = self._cache.audio_count()
                content.append(
                    f"  {songs} song{'' if songs == 1 else 's'} on disk", style="dim")

        spec = SETTINGS_ROWS[self._settings_cursor]
        prose = self._fit.prose
        if self._settings_edit is not None:
            content.append(
                "\n\n   Typing a number — Enter or Esc saves it, Backspace deletes",
                style="dim",
            )
        if self._settings_secret is not None:
            content.append(
                "\n\n   Typing a key — Enter saves it (empty clears it), Esc cancels",
                style="dim",
            )
        if prose:
            content.append(f"\n\n   {spec['desc']}", style="dim")
        if prose and spec.get("value_desc"):
            content.append("\n   ", style="")
            current = str(self.config.get(spec["key"], spec["default"])).upper()
            for i, choice in enumerate(spec["choices"]):
                if i:
                    content.append(" · ", style="dim")
                on = choice == current
                gated = self._quality_unavailable(choice)
                content.append(choice, style="dim" if gated else ("bold cyan" if on else "dim"))
                short = self.QUALITY_LABELS.get(choice, "")
                if short and short != choice:
                    content.append(
                        f" {short}", style="dim" if gated else ("cyan" if on else "dim"))
            content.append_text(self._build_quality_gate_note())
        if self._quality_name != self.config.get("quality"):
            content.append(
                f"\n   Overridden this run by --quality {self._quality_name}",
                style="dim yellow",
            )
        songs = self._cache.audio_count()
        # One index read for both numbers; separate calls re-parsed downloads.json per track
        # (229 ms of UI-thread JSON at 500 downloads).
        kept, kept_bytes = self._download_usage()
        content.append("\n\n   Cache      ", style="dim")
        content.append(f"{songs:>4} song{' ' if songs == 1 else 's'}", style="dim")
        content.append(
            f" · {format_gb(self._cache.disk_bytes())}"
            f" of {self.config.get('cache_budget_gb', 2)}.000 GB", style="dim")
        if (self._download_job or {}).get("state") != "running":
            content.append("   [x]", style="bold")
            content.append(" clear", style="dim")
        content.append("\n   Downloads  ", style="dim")
        content.append(f"{kept:>4} song{' ' if kept == 1 else 's'}", style="dim")
        content.append(f" · {format_gb(kept_bytes)} · exempt from the budget",
                       style="dim")
        content.append("   [d]", style="bold")
        content.append(" list", style="dim")
        folder = downloads.display_dir()
        # A download never outranks a re-fetch: [Esc] here stops the re-fetch, and each
        # refuses while the other runs.
        refetch = self._build_refetch_line()
        action = (refetch
                  if (self._refetch_job or {}).get("state") in ("running", "blocked",
                                                                "failed")
                  else self._build_download_line() or refetch)
        content.append("\n   ", style="")
        busy = ((self._refetch_job or {}).get("state") in ("running", "blocked", "failed")
                or (self._download_job or {}).get("state")
                in ("running", "blocked", "failed"))
        if busy or len(folder) + len(action.plain) + 3 > max(self._fit.inner - 3, 20):
            content.append_text(action)
            content.append(f"   {folder}", style="dim")
        else:
            content.append(folder, style="dim")
            content.append_text(action)
        content.append("\n   Logged in as ", style="dim")
        content.append(self._user_display_name or "—", style="bold")
        content.append("   [o]", style="bold")
        content.append(" log out", style="dim")
        content.append_text(self._build_pkce_line())
        return content

    def _download_rows(self) -> list:
        granted_names = {quality: name for name, quality
                         in self.QUALITY_MAP.items()}
        rows = []
        for row in self._downloads():
            entry = row["entry"]
            title, artist, _album = downloads.describe(entry.get("path") or "")
            # Old indexes use the pre-rename quality names; translate at read time, never
            # rewrite the index (a migration risks the whole file).
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
        content.append(
            f"  ({total} · "
            f"{downloads.format_bytes(sum(r['bytes'] for r in rows))})",
            style="dim")
        content.append("\n")
        for index in range(page_start, page_end):
            row = rows[index]
            selected = (index == self._downloads_cursor)
            content.append("\n")
            content.append("  ▸ " if selected else "    ",
                           style="bold cyan" if selected else "")
            content.append("↓ ", style="dim cyan")
            content.append(row["title"],
                           style="bold white" if selected else "white")
            if row["artist"]:
                content.append(f"  {row['artist']}", style="dim")
            if row["tier"]:
                content.append(f"  {row['tier']}", style="dim")
            content.append(f"  {downloads.format_bytes(row['bytes'])}",
                           style="dim cyan")
        self._page_footer(content, self._downloads_cursor, total, page)
        return content

    @staticmethod
    def _styled(*parts) -> Text:
        content = Text()
        for text, style in parts:
            content.append(text, style=style)
        return content

    def _build_delete_download_confirm(self) -> Text:
        row = self._downloads_delete or {}
        return self._styled(
            (f"\n   Delete {row.get('title') or 'this download'}"
             " from your music folder?", "bold yellow"),
            ("\n   ", ""),
            ("y", "bold"),
            (" to confirm, any other key to cancel", "dim"))

    def _delete_download(self) -> None:
        row = self._downloads_delete
        self._downloads_delete = None
        if row:
            self._run("download.delete", track_id=row["id"])

    def _build_quality_gate_note(self) -> Text:
        line = Text()
        gated = [c for c in QUALITY_CHOICES if self._quality_unavailable(c)]
        if not gated:
            return line
        # The ceiling is a tidalapi value; translate to setting names.
        ceiling = self._tier_label(self._quality_ceiling) or self._quality_ceiling
        line.append(
            f"\n   {' and '.join(gated)} — this login isn't served them; "
            f"TIDAL sends {ceiling} instead",
            style="dim yellow",
        )
        # _quality_name, not the saved value: --quality can override it.
        if self._quality_name in gated:
            line.append("   [u] fixes it", style="dim")
        return line

    def _build_pkce_line(self) -> Text:
        line = Text()
        if self._is_pkce():
            line.append("\n   PKCE login ")
            line.append("✓", style="green")
            line.append("   Max quality available", style="dim")
            return line
        line.append("\n   [u]", style="bold")
        line.append(" sign in for higher quality", style="dim")
        if self._fit.prose:
            line.append(
                "\n   A clunkier sign-in — you paste back the address your browser lands on —"
                "\n   and in exchange HIGH and MAX stream as real FLAC instead of AAC.",
                style="dim",
            )
        return line

    def _build_quit_confirm(self) -> Text:
        return self._styled(
            ("\n   Quit player? ", "bold yellow"),
            ("Press ", "dim"),
            ("Esc", "bold"),
            (" again to confirm, any other key to cancel", "dim"))

    def _build_logout_confirm(self) -> Text:
        return self._styled(
            ("\n   Log out and clear saved tokens? ", "bold yellow"),
            ("Press ", "dim"),
            ("y", "bold"),
            (" to confirm, any other key to cancel", "dim"))

    def _build_disable_songs_confirm(self) -> Text:
        return self._styled(
            ("\n   Clear cached songs as well? ", "bold yellow"),
            ("y", "bold"),
            (" clear, ", "dim"),
            ("n", "bold"),
            (" keep them, ", "dim"),
            ("Esc", "bold"),
            (" cancel", "dim"))

    def _build_download_line(self) -> Optional["Text"]:
        job = self._download_job or {}
        state = job.get("state")
        if state == "blocked":
            return Text("Download stopped — TIDAL is rate-limiting. "
                        f"{job.get('done', 0)} done, nothing retried.",
                        style="red")
        if state == "failed":
            return Text(f"Download failed — {job.get('error') or 'unknown'}",
                        style="red")
        if state != "running":
            if state == "done" and job.get("failed"):
                return Text(f"Download finished — {job['failed']} failed",
                            style="red")
            return None
        slots = job.get("slots") or ()
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
        if state == "failed":
            content.append(f"Stopped — {job.get('error')}. {job.get('done', 0)} done.", style="red")
            content.append("   [R]", style="bold")
            content.append(" try again", style="dim")
            return content
        content.append("[R]", style="bold")
        content.append(f" upgrade all to {self._quality_name}", style="dim")
        return content

    def _build_refetch_confirm(self) -> Text:
        content = Text()
        plan = self._refetch_plan or {}
        total = len(plan.get("downloads", [])) + len(plan.get("cache", []))
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
        content.append("\n   ")
        content.append("y", style="bold")
        content.append(" to start, any other key to cancel", style="dim")
        return content

    def _build_clear_cache_confirm(self) -> Text:
        songs = self._cache.audio_count()
        return self._styled(
            (f"\n   Delete {songs} cached song{'' if songs == 1 else 's'}? ",
             "bold yellow"),
            ("y", "bold"),
            (" to confirm, any other key to cancel", "dim"))

    def _mode_hints(self) -> list:
        if self._footer_hidden:
            return []
        hints = self._screen_hints()
        if hints and self._can_hide_footer():
            hints.append(Hint("h", "hide", None, HIDE_HINT_RANK))
        elif hints and self._can_exit_menu_to_player():
            hints.append(Hint("h", "player", "player", HIDE_HINT_RANK))
        return hints

    def _screen_hints(self) -> list:
        if self._mini_player or self._download_open:
            return []
        if self._mode == self.MODE_SEARCH:
            hints = [
                Hint("Enter/\u2192", "search/open", "open", 0),
                Hint("\u2191/\u2193", "navigate", "move", 2),
                Hint("Tab", "filter", None, 3),
            ]
            if self._search_results:
                hints.append(Hint("Space", "pause/play", "pause", 4))
            hints += [Hint("\u2190/Esc", "back", None, 1),
                      Hint("Bksp", "delete", "del", 5)]
            return hints
        if self._mode == self.MODE_BROWSE:
            hints = [
                Hint("Enter/\u2192", "play track", "play", 0),
                Hint("\u2191/\u2193", "navigate", "move", 2),
                Hint("Space", "pause/play", "pause", 4),
                Hint("a", "play all", "all", 5),
                Hint("y", "add to playlist", "add", 6),
                Hint("d", "download", "get", 7),
                Hint("D", "download all", "all", 9),
            ]
            if self._browse_playlist is not None:
                hints.append(Hint("x", "remove", "del", 8))
            hints.append(Hint("v", "volume", "vol", 3))
            hints.append(Hint("\u2190/Esc", "back", None, 1))
            return hints
        if self._mode == self.MODE_ARTIST:
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
            hints = [Hint("\u2191/\u2193", "navigate", "move", 1)]
            if self._downloads():
                hints.append(Hint("Enter", "play", None, 0))
                hints.append(Hint("x", "delete", "del", 0))
            hints += [Hint("Space", "pause/play", "pause", 4),
                      Hint("v", "volume", "vol", 3),
                      Hint("\u2190/Esc", "back", None, 2)]
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
        hints = [Hint("space", "play/pause", "play", 0)]
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
        rows = fit.hint_rows + (2 if self._show_more and self._mode == self.MODE_PLAYER else 0)
        if room is not None:
            rows = max(rows, room)
        laid = _fit_hints(hints, max(fit.inner - INDENT, 8), rows)
        return _clip_lines(_hints_text(laid, INDENT), fit.inner)

    def _build_volume_overlay(self, fit) -> list:
        spec = get_spec("volume")
        value = coerce(spec, self.config.get("volume", spec["default"]))
        ceiling = self._setting_ceiling(spec)

        label, pct = "Volume ", f" {value}%"
        bar = Text(" " * INDENT)
        bar.append(label, style="bold blue")
        width = min(VOLUME_BAR_MAX, fit.inner - INDENT - len(label) - len(pct) - 2)
        if width >= MIN_BAR_WIDTH:
            filled = max(0, min(width, int(round(width * value / max(ceiling, 1)))))
            bar.append("▕", style="blue")
            bar.append("█" * filled, style="bold blue")
            bar.append("░" * (width - filled), style="dim blue")
            bar.append("▏", style="blue")
        bar.append(pct, style="bold blue")
        rows = [bar]

        notes = []
        if value >= 105:
            notes.append("louder than the master — quality suffers")
        backend = self._backend_name()
        if backend and ceiling < spec["max"]:
            notes.append(f"{backend} caps at {ceiling}%")
        if notes and fit.hint_rows > 1:
            rows.append(Text(" " * INDENT + "  ".join(notes), style="blue"))

        keys = [Hint("←/→", "adjust", "±", 1), Hint("Enter/Esc", "close", "ok", 0)]
        rows.append(_hints_text(_fit_hints(keys, max(fit.inner - INDENT, 8), 1), INDENT))
        out = []
        for row in rows:
            out.extend(_clip_lines(row, fit.inner))
        return out

    def _build_download_overlay(self, fit) -> list:
        avail = max(fit.inner - 4, 10)
        job = self._download_job_here()
        action = (self._download_status_row(job, avail) if job.get("state")
                  else _hints_text(_fit_hints(DOWNLOAD_BUTTONS, avail, 1), 0))
        head, size, pick = self._download_box_rows()
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
        track = self._download_track
        tier = self._download_tier()
        bulk = self._download_bulk()
        running = self._download_job or {}
        if running.get("state") != "running" or not running.get("bulk"):
            running = {}
        # Not asked for in bulk: the facts are about the first track only.
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
        queued = self._download_queued_ids()
        if not queued:
            return len(self._download_tracks)
        return len({getattr(t, "id", None) for t in self._download_tracks}
                   - queued)

    def _download_bar_rows(self, job: dict, avail: int) -> list:
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
        namew = max(6, min(20, avail - 44))
        return namew, max(6, avail - namew - 26)

    def _download_job_here(self) -> dict:
        job = self._download_job or {}
        if job.get("bulk"):
            if job.get("state") == "running":
                return job
            return job if self._download_bulk() else {}
        if job.get("track_id") != getattr(self._download_track, "id", None):
            return {}
        return job

    def _download_status_row(self, job: dict, avail: int) -> Text:
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
            # A stepped-down tier names the rung it landed on; the file is real but not the
            # tier the line above names.
            landed = job.get("landed")
            if landed and landed != job.get("tier"):
                row.append(f" {landed}", style="cyan")
            if not job.get("tags"):
                row.append(" untagged", style="dim")
            row.append("   [Esc]", style="bold")
            row.append(" close", style="dim")
        elif state == "failed":
            row.append(f"Failed — {job.get('error')}", style="red")
            row.append("   [Enter]", style="bold")
            row.append(" retry", style="dim")
        return row

    def _download_bulk_status_row(self, job: dict, avail: int) -> Text:
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
            elif state == "failed":
                row.append(f"Stopped — {job.get('error')}", style="red")
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
        body = _clip_lines(content, fit.inner)
        room = max(fit.hint_rows, fit.rows - len(body) - 1)
        footer = (self._build_volume_overlay(fit) if self._volume_open
                  else self._build_hints(fit, room))
        if footer:
            body = body + [Text("")]
        return body, footer

    def _build_display(self) -> Panel:
        width, height = self._console_size()
        mini = self._mini_player
        levers = self._fit_levers()
        fit = _Fit(
            inner=max(width - (MINI_PANEL_CHROME if mini else PANEL_CHROME),
                      MIN_INNER_WIDTH),
            rows=max(height - (MINI_PANEL_ROWS if mini else PANEL_ROWS), 1),
            page_rows=(len(SETTINGS_ROWS) if self._mode == self.MODE_SETTINGS
                       else self._page_size),
            hint_rows=2 if height >= 16 else 1,
            levers=levers,
            mini=mini,
        )
        self._fit = fit
        body, footer = self._compose(fit)
        for _ in range(len(levers) + 1):
            over = len(body) + len(footer) - fit.rows
            if over <= 0 or not fit.relax(over):
                break
            body, footer = self._compose(fit)

        if len(body) + len(footer) > fit.rows:
            if body and not body[-1].plain.strip():
                body = body[:-1]
        if len(body) + len(footer) > fit.rows:
            keep = min(len(body), max(IDENTITY_ROWS, fit.rows - len(footer)))
            footer = footer[:max(fit.rows - keep, 0)]
            body = body[:max(fit.rows - len(footer), 1)]
        lines = body + footer
        if self._download_open:
            lines = self._overlay(lines, self._build_download_overlay(fit), fit)

        content = Text("\n").join(lines)
        content.no_wrap = True
        title = "[bold cyan]Ticli[/bold cyan]"
        if self.config.get("allow_ai_control"):
            title += " [dim]· AI control[/dim]"
        if self._connectivity == OFFLINE:
            title += " [yellow]· offline[/yellow]"
        elif self._connectivity == SIGNED_OUT:
            title += " [red]· signed out: [o] sign in[/red]"
        return Panel(
            content,
            title=title,
            border_style="cyan",
            padding=(0, 1) if mini else (1, 2),
        )

    def _overlay(self, lines: list, box: list, fit) -> list:
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
            return 80, 24
        return max(int(width), 1), max(int(height), 1)

    def _push_nav(self):
        mode = self._mode
        state = {"mode": mode}
        if mode == self.MODE_SEARCH:
            state.update(query=self._search_query, filter=self._search_filter,
                         cursor=self._search_cursor)
        elif mode == self.MODE_BROWSE:
            state.update(title=self._browse_title,
                         tracks=list(self._browse_tracks),
                         cursor=self._browse_cursor)
        elif mode == self.MODE_ARTIST:
            state.update(artist=self._artist, section=self._artist_section,
                         cursor=self._artist_cursor)
        elif mode == self.MODE_QUEUE:
            state.update(cursor=self._queue_cursor)
        elif mode == self.MODE_PLAYLISTS:
            state.update(cursor=self._playlists_cursor)
        elif mode == self.MODE_SETTINGS:
            state.update(cursor=self._settings_cursor)
        else:
            state = {"mode": self.MODE_PLAYER}
        self._nav_history.append(state)

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
            # The generation is left alone: a page still in flight is for this same query.
        elif mode == self.MODE_BROWSE:
            self._mode = self.MODE_BROWSE
            self._browse_title = state.get("title", "")
            self._browse_tracks = state.get("tracks", [])
            self._browse_cursor = state.get("cursor", 0)
            self._browse_loading = False
            self._browse_message = ""
        elif mode == self.MODE_ARTIST:
            self._mode = self.MODE_ARTIST
            self._artist = state.get("artist")
            self._artist_section = state.get("section", self.ARTIST_SECTIONS[0])
            self._artist_cursor = state.get("cursor", 0)
            self._load_artist_section()
        elif mode == self.MODE_QUEUE:
            self._mode = self.MODE_QUEUE
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
        query = query.strip()
        if not query:
            return
        kept = [item for item in self._search_history if item.lower() != query.lower()]
        self._search_history = [query, *kept][:200]

    def _history_rows(self) -> list:
        if (self._search_query or self._search_results
                or self._search_loading or self._search_message):
            return []
        return self._search_history

    @staticmethod
    def _search_split(page: int) -> tuple:
        albums = max(1, page * 25 // 100)
        artists = max(1, page * 15 // 100)
        playlists = max(1, page * 15 // 100)
        return max(1, page - albums - artists - playlists), albums, artists, playlists

    def _search_kinds(self, scope: Optional[str] = None) -> tuple:
        return self.SEARCH_FILTER_KINDS.get(scope or self._search_filter, ())

    @staticmethod
    def _search_models() -> list:
        # Always all four categories: session.search() sends them in one `types=` of one GET,
        # so the request costs the same and every scope is then answered from it.
        return [tidalapi.Track, tidalapi.Album, tidalapi.Artist, tidalapi.Playlist]

    def _search_row(self, kind: str, obj) -> dict:
        if kind == "tracks":
            artist = obj.artists[0].name if obj.artists else ""
            return {"type": "track", "name": obj.name, "artist": artist, "obj": obj}
        if kind == "albums":
            artist = obj.artist.name if obj.artist else ""
            return {"type": "album", "name": obj.name, "artist": artist, "obj": obj}
        if kind == "playlists":
            creator = getattr(getattr(obj, "creator", None), "name", "") or ""
            count = getattr(obj, "num_tracks", 0) or 0
            detail = creator or (f"{count} tracks" if count > 0 else "")
            return {"type": "playlist", "name": obj.name, "artist": detail, "obj": obj}
        return {"type": "artist", "name": obj.name, "artist": "", "obj": obj}

    def _search_view(self, scope: Optional[str] = None) -> dict:
        return self._search_views.get(scope or self._search_filter) or _empty_search_view()

    def _scope_answered(self, scope: str) -> bool:
        view = self._search_views.get(scope)
        return bool(view) and not view["loading"]

    def _put_search_view(self, scope: str, **changes):
        # Both dicts are assigned, never mutated, so a racing paint sees a whole record.
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
        return self._search_view()["loading"]

    @property
    def _search_cursor(self) -> int:
        return self._search_view()["cursor"]

    @_search_cursor.setter
    def _search_cursor(self, value: int):
        self._put_search_view(self._search_filter, cursor=value)

    @property
    def _search_offset(self) -> int:
        return self._search_reservoir["offset"]

    @_search_offset.setter
    def _search_offset(self, value: int):
        self._search_reservoir = {**self._search_reservoir, "offset": value}

    @property
    def _search_pool(self) -> dict:
        consumed = self._search_view()["consumed"]
        pool = _empty_search_pool()
        for kind in self._search_kinds():
            pool[kind] = self._search_reservoir[kind][consumed.get(kind, 0):]
        return pool

    @property
    def _search_done(self) -> bool:
        return self._search_scope_done(self._search_filter)

    def _search_scope_done(self, scope: str) -> bool:
        # TIDAL has nothing past SEARCH_MAX_OFFSET (300); no categories (My Playlists) is done.
        reservoir = self._search_reservoir
        if reservoir["stopped"] or reservoir["offset"] >= SEARCH_MAX_OFFSET:
            return True
        return all(reservoir["exhausted"][kind] for kind in self._search_kinds(scope))

    def _take_search_page(self, scope: str, consumed: dict, page: int) -> tuple:
        reservoir = self._search_reservoir
        kinds = ("tracks", "albums", "artists", "playlists")
        avail = {kind: len(reservoir[kind]) - consumed.get(kind, 0) for kind in kinds}
        if scope == "all":
            n_tracks, n_albums, n_artists, n_playlists = self._search_split(page)
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
        consumed = self._search_view(scope)["consumed"]
        held = sum(len(self._search_reservoir[kind]) - consumed.get(kind, 0)
                   for kind in self._search_kinds(scope))
        if held >= self._page_size:
            return True
        return self._search_scope_done(scope) and self._search_reservoir["offset"] > 0

    def _fill_search_view(self, scope: str):
        view = self._search_view(scope)
        items, consumed = self._take_search_page(scope, view["consumed"], self._page_size)
        self._put_search_view(
            scope, results=view["results"] + items, consumed=consumed, loading=False,
            message="" if (view["results"] or items) else "No results found")

    def _fill_waiting_search_views(self):
        for scope in self.SEARCH_FILTERS:
            if scope in self._search_views and self._search_views[scope]["loading"]:
                self._fill_search_view(scope)

    def _fail_waiting_search_views(self, message: str):
        for scope in self.SEARCH_FILTERS:
            view = self._search_views.get(scope)
            if view is None or not view["loading"]:
                continue
            self._put_search_view(
                scope, loading=False, message="" if view["results"] else message)

    def _reset_search_results(self):
        self._search_gen += 1
        self._search_key = ""
        self._search_views = {}
        self._search_reservoir = _empty_search_reservoir()
        self._search_fetching = False

    def _cycle_search_filter(self, step: int = 1):
        order = self.SEARCH_FILTERS
        self._search_filter = order[(order.index(self._search_filter) + step) % len(order)]
        self._apply_search_scope()

    def _do_search(self):
        query = self._search_query.strip()
        if not query:
            return
        self._add_to_history(query)
        if self.remote is not None:
            self._run("history.add", query=query)
        self._reset_search_results()
        self._search_key = query
        self._apply_search_scope()

    def _apply_search_scope(self):
        query = self._search_query.strip()
        if not query:
            return
        scope = self._search_filter
        if query != self._search_key:
            self._reset_search_results()
            self._search_key = query
        elif scope in self._search_views:
            if not self._search_views[scope]["loading"]:
                self._put_search_view(scope, cached=True)
            return
        if scope == "playlists":
            self._search_own_playlists(query)
            return
        if scope == "music":
            self._search_own_music(query)
            return
        if self._search_reservoir["message"]:
            self._put_search_view(scope, loading=False, message=self._search_reservoir["message"])
            return
        if self._search_servable(scope):
            self._put_search_view(scope, cached=True)
            self._fill_search_view(scope)
            return
        self._put_search_view(scope, loading=True, message="", cached=False)
        if self._search_fetching:
            return
        self._search_fetching = True
        self._fetch_search_page(query, self._search_gen)

    def _fetch_search_page(self, query: str, gen: int):
        page = self._page_size
        offset = self._search_reservoir["offset"]
        self._search_last_fetch = time.monotonic()

        def _done(response):
            try:
                if gen != self._search_gen:
                    return
                if not response.get("ok"):
                    raise RuntimeError(response.get("reason") or "no answer")
                results = response["result"]
                reservoir = self._search_reservoir
                found = {kind: list(results.get(kind) or []) for kind in ("tracks", "albums", "artists", "playlists")}
                # There is never more than 300 items behind a query.
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
                    self._search_reservoir = {**self._search_reservoir, "stopped": True, "message": message}
                    self._fail_waiting_search_views(message)
            finally:
                # Only while this is still the query being searched: a superseded fetch must not open the gate.
                if gen == self._search_gen:
                    self._search_fetching = False

        # TIDAL applies `limit` per type, so one request at the page size covers every category.
        self._fetch("search", {"query": query, "limit": page, "offset": offset}, _done)

    def _search_more(self):
        scope = self._search_filter
        if self._search_loading or self._search_fetching or scope in self.LOCAL_SCOPES:
            return
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
        if not self._cache.enabled:
            self._put_search_view(
                "playlists", loading=False, results=[], cursor=0,
                message="Playlist search needs the metadata cache — "
                        "turn 'Cache playlists' on in settings")
            return
        names = {p.id: p.name for p in (self._cache.get_playlists() or [])}
        needle = query.lower()
        by_title, by_artist, by_album, by_playlist = [], [], [], []
        matched_playlists = {pid for pid, pname in names.items() if needle in (pname or "").lower()}
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
            bucket.append({"type": "track", "name": name, "artist": artists,
                           "playlist": names.get(playlist_id, "Playlist"), "obj": CachedTrack(record)})
        results = by_title + by_artist + by_album + by_playlist
        self._put_search_view(
            "playlists", loading=False, results=results, cursor=0, cached=False,
            message="" if results else (
                "No results in your playlists" if scanned else
                "Nothing cached to search yet — open Playlists once to index them"))

    def _search_own_music(self, query: str):
        needle = query.casefold()
        results = []
        for track in download_tracks():
            album = track.album.name if track.album else ""
            artist = ", ".join(a.name for a in track.artists)
            if needle in " ".join([track.name, artist, album]).casefold():
                results.append({"type": "track", "name": track.name, "artist": artist, "obj": track})
        self._put_search_view(
            "music", loading=False, results=results, cursor=0, cached=False,
            message="" if results else ("No results in your downloads" if self._downloads()
                                        else "Nothing downloaded yet — [d] on a track downloads it"))

    def _select_search_result(self):
        if not self._search_results:
            return
        item = self._search_results[self._search_cursor]
        obj = item["obj"]
        opener = {"album": self._open_album, "artist": self._open_artist,
                  "playlist": self._open_playlist}.get(item["type"])
        if opener:
            opener(obj)
        elif item["type"] == "track":
            self._run("play.track", track_ids=[obj.id])
            self._mode = self.MODE_PLAYER
            self._nav_history.clear()

    def _open_album(self, album):
        self._push_nav()
        self._mode = self.MODE_BROWSE
        self._browse_playlist = None
        source = ("album", self._obj_id(album))
        self._browse_source = source
        self._browse_title = album.name
        key = f"album:{source[1]}"
        cached = self._cache.get_items(key)
        self._browse_tracks = cached or []
        self._browse_fetched = self._cache.fetched_at(key) if cached else None
        self._browse_cursor = -1
        self._browse_loading = not cached
        self._browse_message = ""

        def _done(response):
            if self._browse_source != source:
                return
            if response.get("ok"):
                self._browse_tracks = list(response["result"]["tracks"])
                self._browse_fetched = response["result"].get("cached_at")
                self._browse_cursor = min(self._browse_cursor, len(self._browse_tracks) - 1)
                if not self._browse_tracks:
                    self._browse_message = "No tracks found"
            elif not self._browse_tracks:
                self._browse_message = response.get("reason") or "Failed to load album"
            self._browse_loading = False

        self._fetch("album.tracks", {"id": source[1]}, _done, known=[("album", album)])

    def _open_artist(self, artist):
        self._push_nav()
        self._mode = self.MODE_ARTIST
        self._artist = artist
        self._artist_section = self.ARTIST_SECTIONS[0]
        self._artist_cursor = self._artist_cursors.get(self._artist_key(), 0)
        self._load_artist_section()

    @staticmethod
    def _obj_id(obj) -> str:
        return str(getattr(obj, "id", "") or "")

    @staticmethod
    def _rows(kind: str, objs) -> list:
        return [{"type": kind, "obj": obj} for obj in objs]

    @staticmethod
    def _track_estimate(track, tier: str) -> int:
        return downloads.estimate_bytes(getattr(track, "duration", 0), tier)

    def _artist_row(self):
        rows = self._artist_rows()
        return rows[self._artist_cursor] if 0 <= self._artist_cursor < len(rows) else None

    def _artist_key(self, section: Optional[str] = None):
        return (self._obj_id(self._artist), section or self._artist_section)

    def _artist_record(self):
        return self._artist_sections.get(self._artist_key())

    def _artist_rows(self) -> list:
        record = self._artist_record()
        return record["items"] if record and record["state"] == "ready" else []

    def _load_artist_section(self, force: bool = False):
        artist = self._artist
        if artist is None:
            return
        key = self._artist_key()
        if key in self._artist_sections and not force:
            return
        section = self._artist_section
        limit = max(20, self._page_size)
        disk_key = f"artist:{key[0]}:{section}"
        cached = self._cache.get_items(disk_key)
        first = ({"state": "ready", "items": [{"type": kind_of(o) or "track", "obj": o}
                                               for o in cached],
                  "message": "", "fetched": self._cache.fetched_at(disk_key)}
                 if cached else {"state": "loading", "items": [], "message": ""})
        self._artist_sections = {**self._artist_sections, key: first}

        def _done(response):
            if response.get("ok"):
                items = list(response["result"]["items"])
                record = {"state": "ready", "items": items,
                          "message": "" if items else self.ARTIST_SECTION_EMPTY[section],
                          "fetched": response["result"].get("cached_at")}
            elif cached:
                return
            else:
                record = {"state": "failed", "items": [], "message": self.ARTIST_SECTION_FAILED[section]}
            # Whole-dict assignment: the paint thread only reads complete records.
            self._artist_sections = {**self._artist_sections, key: record}

        self._fetch("artist.section", {"id": key[0], "section": section, "limit": limit},
                    _done, known=[("artist", artist)])

    def _fetch_artist_section(self, artist, section: str, limit: int) -> list:
        if section == "tracks":
            return self._rows("track", artist.get_top_tracks(limit=limit) or [])
        if section == "albums":
            return self._rows("album", artist.get_albums(limit=limit) or [])
        if section == "playlists":
            return self._rows("playlist", self._artist_page_playlists(artist))
        return self._artist_suggestions(artist, limit)

    def _artist_page_playlists(self, artist) -> list:
        # tidalapi has no artists/{id}/playlists accessor; the rows come from the artist page (pages/artist).
        page = artist.page()
        found = []
        seen = set()
        for category in (getattr(page, "categories", None) or []):
            for item in (getattr(category, "items", None) or []):
                if not isinstance(item, tidalapi.Playlist):
                    continue
                pid = self._obj_id(item)
                if pid and pid in seen:
                    continue
                seen.add(pid)
                found.append(item)
        return found

    def _artist_suggestions(self, artist, limit: int) -> list:
        rows = []
        failed = 0
        try:
            rows += self._rows("track", artist.get_radio(limit=limit) or [])
        except Exception:
            failed += 1
        try:
            rows += self._rows("artist", artist.get_similar() or [])
        except Exception:
            failed += 1
        if failed == 2:
            raise RuntimeError("neither radio nor similar artists answered")
        return rows

    def _cycle_artist_section(self, step: int = 1):
        order = self.ARTIST_SECTIONS
        self._artist_cursors = {**self._artist_cursors, self._artist_key(): self._artist_cursor}
        self._artist_section = order[(order.index(self._artist_section) + step) % len(order)]
        self._artist_cursor = self._artist_cursors.get(self._artist_key(), 0)
        self._load_artist_section()

    def _artist_section_tracks(self) -> list:
        return [row["obj"] for row in self._artist_rows() if row["type"] == "track"]

    def _select_artist_row(self):
        record = self._artist_record()
        if record is not None and record["state"] == "failed":
            self._load_artist_section(force=True)
            return
        rows = self._artist_rows()
        if not rows or not (0 <= self._artist_cursor < len(rows)):
            return
        row = rows[self._artist_cursor]
        obj = row["obj"]
        opener = {"album": self._open_album, "playlist": self._open_playlist,
                  "artist": self._open_artist}.get(row["type"])
        if opener:
            opener(obj)
        else:
            self._play_artist_section(
                sum(1 for r in rows[:self._artist_cursor] if r["type"] == "track"))

    def _play_artist_section(self, index: int):
        if self._artist_section_tracks():
            self._run("play.artist", id=self._obj_id(self._artist),
                      section=self._artist_section, index=index)

    def _play_browse(self, index: int):
        if not self._browse_tracks or self._browse_source is None:
            return
        kind, source_id = self._browse_source
        self._run(f"play.{kind}", id=source_id, index=index)

    def _load_playlists(self):
        cached = self._cache.get_playlists()
        self._playlists = cached or []
        self._playlists_fetched = self._cache.fetched_at("playlists") if cached else None
        self._playlists_loading = not cached
        self._playlists_cursor = 0
        self._playlists_message = ""

        def _done(response):
            try:
                if not response.get("ok"):
                    if not self._playlists:
                        self._playlists_message = response.get("reason") or "Failed to load playlists"
                    return
                fresh = list(response["result"]["playlists"])
                self._playlists_fetched = response["result"].get("cached_at")
                self._playlists = fresh
                if self._playlists_cursor >= len(fresh):
                    self._playlists_cursor = max(0, len(fresh) - 1)
                if not fresh:
                    self._playlists_message = "No playlists found"
            finally:
                self._playlists_loading = False

        self._fetch("library.playlists", {}, _done)

    def _open_playlist(self, playlist):
        self._push_nav()
        self._mode = self.MODE_BROWSE
        playlist_id = self._obj_id(playlist)
        self._browse_source = ("playlist", playlist_id)
        self._browse_playlist = playlist if self._is_editable(playlist) else None
        self._browse_title = playlist.name if hasattr(playlist, "name") else "Playlist"
        cached = self._cache.get_playlist_tracks(playlist_id) if playlist_id else None
        self._browse_tracks = cached or []
        self._browse_fetched = self._cache.fetched_at(f"playlist:{playlist_id}") if cached else None
        self._browse_cursor = -1
        self._browse_loading = not cached
        self._browse_message = ""

        title = self._browse_title

        def _done(response):
            try:
                if not response.get("ok"):
                    if not self._browse_tracks:
                        self._browse_message = response.get("reason") or "Failed to load playlist"
                    return
                live, tracks = response["result"]["playlist"], list(response["result"]["tracks"])
                self._browse_fetched = response["result"].get("cached_at")
                if self._browse_source == ("playlist", playlist_id) and self._is_editable(live):
                    self._browse_playlist = live
                if self._browse_title != title:
                    return
                self._browse_tracks = tracks
                self._browse_cursor = min(self._browse_cursor, len(tracks) - 1)
                if not tracks:
                    self._browse_message = "Playlist is empty"
            finally:
                self._browse_loading = False

        self._fetch("playlist.tracks", {"id": playlist_id}, _done, known=[("playlist", playlist)])

    def _remove_from_queue(self):
        if not self._queue or self._queue_cursor >= len(self._queue):
            return
        self._run("queue.remove", index=self._queue_cursor,
                  track_id=getattr(self._queue[self._queue_cursor], "id", None))
        if self._queue:
            self._queue_cursor = min(self._queue_cursor, len(self._queue) - 1)

    def _remove_from_browse_playlist(self):
        pl = self._browse_playlist
        if pl is None or not self._browse_tracks or self._browse_cursor < 0:
            return
        # A removal in flight is refused inside: indices would shift
        self._run("playlist.remove", id=self._obj_id(pl), index=self._browse_cursor,
                  track_id=getattr(self._browse_tracks[self._browse_cursor], "id", None))

    def _set_toast(self, msg: str, seconds: float = 2.5):
        self._toast = msg
        self._toast_until = time.time() + seconds

    def _target_track_for_picker(self):
        if self._mode == self.MODE_QUEUE and self._queue and self._queue_cursor < len(self._queue):
            return self._queue[self._queue_cursor]
        if self._mode == self.MODE_BROWSE and self._browse_tracks and self._browse_cursor >= 0:
            return self._browse_tracks[self._browse_cursor]
        if self._mode == self.MODE_ARTIST:
            row = self._artist_row()
            if row and row["type"] == "track":
                return row["obj"]
        return self._current_track

    def _picker_playlists(self) -> list:
        playlists = self._editable_playlists  # read once: another thread replaces it
        pid = self._last_playlist_id
        if not pid or not playlists:
            return playlists
        pinned = [p for p in playlists if self._obj_id(p) == pid]
        if not pinned:
            return playlists
        return pinned + [p for p in playlists if self._obj_id(p) != pid]

    def _download_target(self, whole: bool = False) -> tuple:
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
            row = self._artist_row()
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
        self._download_cursor = (QUALITY_CHOICES.index(self._quality_name)
                                 if self._quality_name in QUALITY_CHOICES else 0)
        job = self._download_job
        running = bool(job) and job.get("state") == "running"
        track_id = getattr(track, "id", None)
        if job and not running and job.get("track_id") != track_id:
            self._download_job = None
        if not running and self.remote is None:
            # Exact name only: the one deletion this feature ever performs under the music folder.
            self._discard_staging(track_id)

    def _cancel_download(self):
        job = self._download_job
        if not job or job.get("state") != "running":
            return
        self._download_job_gen += 1
        self._download_job = dict(job, state="cancelled")
        self._set_toast("Download cancelled")

    def _download_tier(self) -> str:
        return QUALITY_CHOICES[self._download_cursor % len(QUALITY_CHOICES)]

    def _download_estimate(self, tier: str) -> int:
        return self._track_estimate(self._download_track, tier)

    def _download_bulk(self) -> bool:
        return len(self._download_tracks) > 1

    def _download_bulk_estimate(self, tier: str) -> int:
        # No network: 53 playbackinfo calls in 2.8 s got the IP blocked (docs/adr/0001-tidal-rate-limits.md).
        return sum(self._track_estimate(track, tier) for track in self._download_tracks)

    def _download_facts(self) -> tuple:
        track_id = getattr(self._download_track, "id", None)
        known = self._download_known
        if known is not None and known[0] == track_id:
            return known[1], known[2]
        sizes, owned = {}, None

        def note(granted, size):
            name = next((n for n, quality in self.QUALITY_MAP.items() if granted == quality), None)
            if name and size:
                sizes[name] = int(size)

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
        sizes, _ = self._download_facts()
        if tier in sizes:
            return downloads.format_bytes(sizes[tier])
        estimate = self._download_estimate(tier)
        return "~" + downloads.format_bytes(estimate) if estimate > 0 else "—"

    def _start_download_job(self, tier: str):
        track = self._download_track
        if track is None:
            return
        running = (self._download_job or {}).get("state") == "running"
        if running and self._download_run is not None:
            self._start_bulk_download_job(tier, tracks=[track], label=getattr(track, "name", "") or "")
            return
        if running:
            self._set_toast("A download is already running — [x] stops it")
            return
        self._download_job_gen = gen = self._download_job_gen + 1
        self._download_job = {
            "state": "running", "tier": tier, "track_id": getattr(track, "id", None),
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
        return tuple({"title": "", "done": 0, "total": 0, "estimate": 0,
                      "state": "idle", "size": 0, "banked": 0,
                      "samples": (), "mark": None}
                     for _ in range(DOWNLOAD_WORKERS))

    def _download_queued_ids(self) -> set:
        if self.remote is not None:
            return set(self._mirror.get("download_queued") or ())
        run = self._download_run
        if run is None:
            return set()
        return {tid for _t, _tier, tid in run.items[:] if tid is not None}

    def _start_bulk_download_job(self, tier: str, tracks=None,
                                 label: Optional[str] = None, paced: bool = False):
        tracks = [t for t in (self._download_tracks if tracks is None else tracks) if t is not None]
        if not tracks:
            return
        if (self._refetch_job or {}).get("state") == "running":
            # Two paced resolvers at once is two API requests a second, above the measured rate.
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
            "estimate": sum(self._track_estimate(t, tr) for t, tr, _i in items),
        }

        def _alive():
            return self._download_job_gen == gen

        def _update(**changes):
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
                        estimate=self._track_estimate(track, item_tier),
                        state="running")
            last = [0.0]

            def _progress(done, total):
                slot["done"] = done
                slot["total"] = total or slot["estimate"]
                now = time.monotonic()
                if now - last[0] >= 0.25:
                    last[0] = now
                    self._wake()

            _final, _written, size, _landed = self._download_deliver(
                plan, abandoned=lambda: not _alive(), progress=_progress,
                record=False)
            slot.update(done=0, total=0, size=size,
                        banked=slot["banked"] + size, state="done")
            return plan.get("record")

        run = _PacedRun(
            items=items,
            resolve=lambda item: self._download_plan(item[0], item[1]),
            fetch=_fetch,
            alive=_alive,
            report=_update,
            workers=1 if paced else DOWNLOAD_WORKERS,
            slots=slots,
            clock=self._api_pace,
            pace=throttle.acquire if paced else None,
        )
        self._download_run = run

        def _go():
            outcome = leftover = None
            try:
                outcome = run.run()
            finally:
                # Cleared before pending() is read, so a late batch is either still in the run's hands or in leftover.
                if self._download_run is run:
                    self._download_run = None
                leftover = run.pending()
            if outcome is None:
                self._wake()
                return
            done, failed, blocked = outcome
            if leftover and not blocked and not run.offline and _alive():
                labels = [name for name in ((self._download_job or {}).get("labels") or ()) if name]
                self._download_job = dict(self._download_job or {}, state="done")
                self._start_bulk_download_job(
                    leftover[0][1], tracks=[t for t, _q, _i in leftover],
                    label=labels[-1] if labels else "", paced=run.pace is not None)
                self._wake()
                return
            if blocked:
                _update(state="blocked", error=blocked)
                self._set_toast(
                    "TIDAL is rate-limiting — download stopped. "
                    "Nothing will be retried.",
                    seconds=PLAYER_ERROR_SECONDS)
            elif run.offline:
                self._went_offline()
                _update(state="failed", error=OFFLINE_MESSAGE)
                self._set_toast(f"Download stopped — {OFFLINE_MESSAGE}",
                                seconds=PLAYER_ERROR_SECONDS)
            else:
                _update(state="done")
                short = max((self._download_job or {}).get("tracks", 0) - done - failed, 0)
                self._set_toast(
                    f"Downloaded {done} song{'' if done == 1 else 's'}"
                    f" to {downloads.display_dir()}"
                    + (f" · {failed} failed" if failed else "")
                    + (f" · {short} not started" if short else ""))
            self._wake()

        threading.Thread(target=_go, daemon=True).start()

    def _add_to_download_run(self, run, items, asked: int,
                             label: Optional[str]) -> None:
        job = dict(self._download_job or {})
        if not items:
            self._set_toast(f"Already queued — all {asked} of them" if asked > 1 else "Already queued")
            return
        run.add(items)
        labels = tuple(job.get("labels") or ())
        if label and label not in labels:
            labels = labels + (label,)
        job.update(
            tracks=(job.get("tracks") or 0) + len(items),
            estimate=(job.get("estimate") or 0) + sum(
                self._track_estimate(t, tr) for t, tr, _i in items),
            labels=labels,
        )
        self._download_job = job
        skipped = asked - len(items)
        self._set_toast(f"Queued {len(items)} more" + (f" · {skipped} already queued" if skipped else ""))
        self._wake()

    def _sample_download_rates(self) -> None:
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
            slot["samples"] = (slot["samples"] + ((done - before) / (now - then),))[-RATE_SAMPLES:]
            slot["mark"] = (now, done)

    @staticmethod
    def _slot_rate(slot: dict) -> float:
        samples = slot.get("samples") or ()
        return sum(samples) / len(samples) if samples else 0.0

    def _download_to_music(self, track, tier: str, abandoned, progress=None):
        try:
            plan = self._download_plan(track, tier)
        except Exception:
            self._discard_staging(getattr(track, "id", None))
            raise
        return self._download_deliver(plan, abandoned, progress)

    def _download_plan(self, track, tier: str) -> dict:
        track_id = getattr(track, "id", None)
        real = self.session.track(track.id) if getattr(track, "cached", False) else track
        if real is None:
            raise RuntimeError("track could not be resolved")
        # Extension unknown until the CDN answers: write under a provisional per-track name, rename after.
        staging = downloads.download_dir() / f".ticli-{track_id}{downloads.PART_SUFFIX}"
        staging.parent.mkdir(parents=True, exist_ok=True)
        ext, granted = self._promote_cached_copy(track_id, tier, staging)
        sources = None
        if ext is None:
            url, granted, tier = self._stream_at_best_tier(real, tier)
            sources = stream_sources(url)
        return {"track_id": track_id, "real": real, "staging": staging,
                "meta": downloads.track_metadata(real), "tier": tier, "ext": ext,
                "granted": granted, "sources": sources, "title": getattr(real, "name", None) or ""}

    def _download_deliver(self, plan: dict, abandoned, progress=None,
                          record=True):
        track_id = plan["track_id"]
        final = None
        try:
            # A promoted cached copy never reaches fetch_to_file; this is the only place a cancel is noticed.
            if abandoned is not None and abandoned():
                raise _DownloadSuperseded()
            ext = plan["ext"]
            if ext is None:
                ext = fetch_to_file(plan["sources"], str(plan["staging"]),
                                    abandoned=abandoned, progress=progress)
            final = downloads.destination(plan["meta"], ext)
            final.parent.mkdir(parents=True, exist_ok=True)
            # A re-fetch at another tier can land under another extension; delete the old file by exact name, only ones ticli wrote.
            previous = downloads.path_for(track_id)
            os.replace(plan["staging"], final)
            if previous is not None and previous != final:
                try:
                    previous.unlink()
                except OSError:
                    pass
            written = tags.write_tags(final, plan["meta"], self._cover_bytes(plan["real"]))
            size = final.stat().st_size
            relative = final.relative_to(downloads.download_dir())
            tier, granted = plan["tier"], plan["granted"]

            def _commit():
                downloads.record(track_id, relative, tier, size, granted=granted)
                self._drop_superseded_cache_copy(track_id)

            # Both halves rewrite a whole JSON file, so both belong on the runner's single thread (see record=False).
            if record:
                _commit()
            else:
                plan["record"] = _commit
            self._forget_downloads()
            return final, written, size, tier
        except Exception:
            self._discard_staging(track_id)
            if final is not None:
                downloads.discard_scratch(final)
            raise

    def _drop_superseded_cache_copy(self, track_id) -> None:
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
                pass
            except OSError as e:
                logger.debug("Could not drop the superseded cached copy: %s", e)
                return
        self._cache.forget_cached([track_id])
        self._cache.invalidate_audio_count()

    def _reclaim_deferred_copies(self) -> None:
        with self._reclaim_lock:
            if not self._reclaim_deferred:
                return
            current = str(getattr(self._current_track, "id", None))
            ready = [t for t in self._reclaim_deferred if t != current]
        for track_id in ready:
            self._drop_superseded_cache_copy(track_id)

    def _reclaim_download_duplicates(self) -> None:
        for row in downloads.present():
            track_id = row["id"]
            if cached_audio_path(track_id) or self._cache.audio_record(track_id):
                self._drop_superseded_cache_copy(track_id)

    # Re-fetch-all is the most rate-limit-dangerous path: 53 playbackinfo calls in 2.8 s got the IP blocked
    # (docs/adr/0001-tidal-rate-limits.md). Serial, paced (REFETCH_MIN_INTERVAL between track starts, 2 requests each),
    # interruptible, and a 429 or 401 subStatus 4006 stops the run: retrying extends those blocks.

    def _upgrade_target(self) -> Optional[str]:
        wanted = self.QUALITY_MAP.get(self._quality_name)
        if wanted not in QUALITY_RANK:
            return None
        ceiling = self._quality_ceiling
        if ceiling in QUALITY_RANK and QUALITY_RANK[ceiling] < QUALITY_RANK[wanted]:
            return ceiling
        return wanted

    def _refetch_candidates(self) -> dict:
        target = self._upgrade_target()
        plan = {"downloads": [], "cache": [], "skipped": 0, "unknown": 0, "bytes": 0}

        def classify(stored, key, size, bucket):
            if stored not in QUALITY_RANK:
                plan["unknown"] += 1
            elif target not in QUALITY_RANK or QUALITY_RANK[target] <= QUALITY_RANK[stored]:
                plan["skipped"] += 1
            else:
                bucket.append(key)
                plan["bytes"] += int(size or 0)

        for key, entry in downloads.load_index().items():
            if downloads._entry_path(entry) is None:
                continue
            classify(entry.get("granted"), key, entry.get("bytes"), plan["downloads"])
        if self._cache.keeps_audio:
            downloaded = set(plan["downloads"])
            for key, record in self._cache._load_tracker().items():
                if key in downloaded:
                    continue
                classify(record.get("quality"), key, record.get("bytes"), plan["cache"])
        return plan

    def _start_refetch_job(self, paced: bool = False) -> None:
        job = self._refetch_job
        if job and job.get("state") == "running":
            return
        if (self._download_job or {}).get("state") == "running":
            # Two paced resolvers double the API request rate past what was measured.
            self._set_toast("A download is running — [x] on the box stops it")
            return
        plan = self._refetch_candidates()
        total = len(plan["downloads"]) + len(plan["cache"])
        if not total:
            self._set_toast("Nothing to upgrade at this quality")
            return
        tier = self._quality_name
        self._refetch_gen = gen = self._refetch_gen + 1
        self._refetch_job = {"state": "running", "tier": tier, "done": 0, "total": total,
                             "failed": 0, "error": ""}

        def _update(**changes):
            if self._refetch_gen != gen:
                return
            current = dict(self._refetch_job or {})
            current.update(changes)
            self._refetch_job = current
            self._wake()

        def _run():
            run = _PacedRun(
                items=[("download", k) for k in plan["downloads"]] + [("cache", k) for k in plan["cache"]],
                resolve=lambda item: item,
                fetch=lambda item, _h, _s: self._refetch_one(item[0], item[1], tier, gen),
                alive=lambda: self._refetch_gen == gen,
                report=_update,
                workers=1,
                clock=self._api_pace,
                pace=throttle.acquire if paced else None,
            )
            outcome = run.run()
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
            elif run.offline:
                self._went_offline()
                _update(state="failed", error=OFFLINE_MESSAGE)
                self._set_toast(f"Re-fetch stopped after {done} — {OFFLINE_MESSAGE}",
                                seconds=PLAYER_ERROR_SECONDS)
            else:
                _update(state="done")
                self._set_toast(f"Re-fetched {done} song{'' if done == 1 else 's'} at {tier}"
                                + (f" · {failed} failed" if failed else ""))
            self._wake()

        threading.Thread(target=_run, daemon=True).start()

    def _refetch_one(self, kind: str, key, tier: str, gen: int) -> None:
        def _abandoned():
            if self._refetch_gen != gen:
                raise _DownloadSuperseded()
            return False

        real = self.session.track(int(key) if str(key).isdigit() else key)
        if real is None:
            raise RuntimeError("track could not be resolved")
        if kind == "download":
            self._download_to_music(real, tier, abandoned=_abandoned)
            return
        self._refetch_into_cache(real, tier, _abandoned)

    def _refetch_into_cache(self, real, tier: str, abandoned) -> None:
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
        self._cache.note_cached(track_id, ext, os.path.getsize(path), quality=granted)
        self._cache.invalidate_audio_count()
        self.audio._sweep_cache()

    def _cancel_refetch(self) -> None:
        job = self._refetch_job
        self._refetch_gen += 1
        if job and job.get("state") == "running":
            self._refetch_job = dict(job, state="cancelled", current="")
            self._set_toast("Re-fetch cancelled")

    def _discard_staging(self, track_id) -> None:
        try:
            (downloads.download_dir() / f".ticli-{track_id}{downloads.PART_SUFFIX}").unlink()
        except OSError:
            pass

    def _promote_cached_copy(self, track_id, tier: str, staging) -> tuple:
        # `.m4a` is AAC-HIGH on device-flow and FLAC-in-MP4 on PKCE: promote only when the tracker knows the granted tier, never by filename.
        if not self._cache.keeps_audio:
            return None, None
        wanted = self.QUALITY_MAP.get(tier)
        record = self._cache.audio_record(track_id) or {}
        granted = record.get("quality")
        if not wanted or granted != wanted:
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
        try:
            top = QUALITY_CHOICES.index(tier)
        except ValueError:
            return [tier]
        return list(reversed(QUALITY_CHOICES[:top + 1]))

    def _stream_at_best_tier(self, real, tier: str) -> tuple:
        # TIDAL often quietly grants a lower tier, but can fail outright (region, pulled track); step down the ladder instead.
        # A rate limit (429, or 401 subStatus 4006) is never stepped past: retrying tiers turned one into an edge block (docs/adr/0001-tidal-rate-limits.md).
        # Nor is an unreachable TIDAL: every lower tier would fail the same way, one request each.
        last = None
        for candidate in self._tier_ladder(tier):
            try:
                url, granted = self._download_stream_url(real, candidate)
            except Exception as e:
                if is_transport_failure(e) or _rate_limited(e):
                    raise
                logger.debug("No %s stream for this track: %s", candidate, e)
                last = e
                continue
            if stream_sources(url):
                return url, granted, candidate
            last = RuntimeError(f"{candidate} named nothing to fetch")
        raise last or RuntimeError("no tier could be streamed")

    def _download_stream_url(self, track, tier: str) -> tuple:
        return self._stream_description(track, self.QUALITY_MAP.get(tier))

    def _cover_bytes(self, track):
        # Plain GET from resources.tidal.com, not an API request, so it cannot add to a rate limit.
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
        track = self._target_track_for_picker()
        if track is None:
            self._set_toast("No track selected")
            return
        self._push_nav()
        self._mode = self.MODE_ADD_TO_PLAYLIST
        self._picker_track = track
        self._picker_new_name = None
        self._picker_cursor = 0 if self._editable_playlists else -1
        if not self._editable_playlists or time.time() - self._editable_playlists_time > 60:
            self._picker_loading = True

            def _done(response):
                if response.get("ok") and not self._editable_playlists:
                    self._picker_cursor = -1
                self._picker_loading = False

            self._fetch("library.playlists", {}, _done)

    def _picker_add_to(self, playlist):
        if self._picker_busy:
            return
        track = self._picker_track
        self._go_back()
        self._run("playlist.add", id=self._obj_id(playlist), playlist=playlist,
                  track_ids=[track.id])

    def _picker_create_and_add(self, name: str):
        if self._picker_busy:
            return
        name = (name or "").strip()
        if not name:
            self._set_toast("Playlist name can't be empty")
            return
        track = self._picker_track
        self._picker_new_name = None
        self._go_back()
        self._run("playlist.create", name=name, track_ids=[track.id])

    def _setting_ceiling(self, spec: dict) -> int:
        if spec["key"] != "volume":
            return spec["max"]
        if self.remote is not None:
            return self._mirror.get("volume_ceiling") or min(spec["max"], SAFE_VOLUME_CEILING)
        if not self.audio:
            return min(spec["max"], SAFE_VOLUME_CEILING)
        try:
            ceiling = int(self.audio.volume_ceiling())
        except Exception:
            # Any failure: a backend that can't answer is not a licence to amplify.
            ceiling = SAFE_VOLUME_CEILING
        return max(spec["min"], min(spec["max"], ceiling))

    def _clamp_volume_to_backend(self):
        spec = get_spec("volume")
        ceiling = self._setting_ceiling(spec)
        wanted = coerce(spec, self.config.get("volume", spec["default"]))
        allowed = min(wanted, ceiling)
        if allowed != self.config.get("volume"):
            self.config["volume"] = allowed
            try:
                update_config({"volume": allowed})
            except ConfigUnreadable as e:
                logger.warning("Volume not saved: %s", e)
        if self.audio:
            self.audio.set_volume(allowed)

    def _set_setting(self, spec: dict, value):
        if spec["key"] in PROTECTED_KEYS:
            self._set_protected_setting(spec, value)
        else:
            self._run("settings.set", key=spec["key"], value=value)

    def _set_protected_setting(self, spec: dict, value):
        # The only writer of the protected rows, reached from settings keypresses alone (ADR-0007).
        if value == self.config.get(spec["key"], spec["default"]):
            return
        try:
            # Only this key, over the file as it is now: the hash never crosses the socket.
            update_config({spec["key"]: value})
        except ConfigUnreadable as e:
            self._set_toast(str(e), seconds=PLAYER_ERROR_SECONDS)
            return
        self.config[spec["key"]] = value
        if self.remote is not None:
            # Not a command: none may write these.
            self.remote.send("reload_switches")
        self._set_toast(f"{spec['label']}: {display_value(spec, value)}")

    def _change_setting(self, step: int):
        self._adjust_setting(SETTINGS_ROWS[self._settings_cursor], step)

    def _adjust_setting(self, spec: dict, step: int):
        current = self.config.get(spec["key"], spec["default"])
        value = cycle_value(spec, current, step)
        if spec["kind"] == "int":
            value = min(value, self._setting_ceiling(spec))
        if spec["key"] == "cache_songs" and value is False:
            self._disable_songs_pending = True
            return
        self._set_setting(spec, value)

    def _begin_setting_edit(self, digit: str) -> bool:
        if SETTINGS_ROWS[self._settings_cursor]["kind"] != "int":
            return False
        self._settings_edit = digit
        return True

    def _commit_setting_edit(self):
        typed, self._settings_edit = self._settings_edit, None
        if not typed:
            return
        spec = SETTINGS_ROWS[self._settings_cursor]
        value = min(coerce(spec, int(typed)), self._setting_ceiling(spec))
        self._set_setting(spec, value)

    def _clear_cached_songs(self):
        self._run("cache.clear")

    def _apply_setting(self, key: str, value):
        if key == "quality":
            self._quality_name = value
        elif key == "page_size":
            self._page_size = value
        elif key == "progress_bar_max":
            self._bar_max = value
        elif key == "show_artwork":
            self._show_artwork = value
            if not value:
                self._artwork = None
                self._artwork_request = None
        elif key == "volume":
            if self.audio:
                self.audio.set_volume(value)
        elif key == "cache_metadata":
            self._cache.metadata = value
            if not value:
                self._cache.clear_metadata()
        elif key == "cache_songs":
            self._cache.songs = value
            if value:
                self._cache.invalidate_audio_count()
                self._cache.enforce_budget()
        elif key == "cache_budget_gb":
            self._cache.budget_gb = value
            self._cache.enforce_budget()

    def _handle_key(self, key: str):
        # Any other key un-hides: a bound-keys list would be a second copy of the mode dispatch and go stale.
        if self._footer_hidden:
            self._footer_hidden = key in HIDE_HOLD_KEYS
        elif key == "h" and self._can_hide_footer():
            self._footer_hidden = True
            return
        elif key == "h" and self._can_exit_menu_to_player():
            self._exit_to_player()
            return

        if self._quit_pending:
            if key == KEY_ESC:
                self._quitting = True
                self.running = False
            else:
                self._quit_pending = False
            return

        if self._logout_pending:
            if key in ("y", "Y"):
                self._logout()
            self._logout_pending = False
            return

        confirmed = key in ("y", "Y", KEY_ENTER, KEY_ENTER2)
        if self._disable_songs_pending:
            self._disable_songs_pending = False
            if confirmed:
                self._set_setting(get_spec("cache_songs"), False)
                self._clear_cached_songs()
            elif key in ("n", "N"):
                self._set_setting(get_spec("cache_songs"), False)
            return

        if self._clear_cache_pending:
            self._clear_cache_pending = False
            if confirmed:
                self._clear_cached_songs()
            return

        if self._downloads_delete is not None:
            if confirmed:
                self._delete_download()
            else:
                self._downloads_delete = None
            return

        if self._refetch_pending:
            self._refetch_pending = False
            plan = self._refetch_plan
            self._refetch_plan = None
            if confirmed and plan and (plan["downloads"] or plan["cache"]):
                self._run("refetch")
            return

        if self._volume_open:
            self._handle_volume_key(key)
            return

        if self._download_open:
            self._handle_download_key(key)
            return

        was_focused = self._player_focus
        if self._handle_focus_key(key):
            return

        if key in ("v", "V") and self._can_open_volume():
            self._open_volume(from_focus=was_focused)
            return

        handlers = {
            self.MODE_SEARCH: self._handle_search_key,
            self.MODE_BROWSE: self._handle_browse_key,
            self.MODE_ARTIST: self._handle_artist_key,
            self.MODE_QUEUE: self._handle_queue_key,
            self.MODE_PLAYLISTS: self._handle_playlists_key,
            self.MODE_ADD_TO_PLAYLIST: self._handle_add_to_playlist_key,
            self.MODE_SETTINGS: self._handle_settings_key,
            self.MODE_DOWNLOADS: self._handle_downloads_key,
        }
        handlers.get(self._mode, self._handle_player_key)(key)

    def _focus_player(self):
        if self._current_track is not None:
            self._player_focus = True

    def _exit_to_player(self) -> None:
        self._mini_player = False
        self._player_focus = False
        self._mode = self.MODE_PLAYER
        self._nav_history.clear()

    def _handle_focus_key(self, key: str) -> bool:
        if not self._player_focus:
            return False
        if key == KEY_RIGHT:
            self._run("seek", delta=SEEK_STEP_SECONDS)
            return True
        if key == KEY_LEFT:
            self._run("seek", delta=-SEEK_STEP_SECONDS)
            return True
        if key in (" ", "k"):
            self._toggle_play_key()
            return True
        if key == KEY_UP:
            return True
        self._player_focus = False
        return key in (KEY_DOWN, KEY_ESC)

    def _can_open_volume(self) -> bool:
        return (self._mode != self.MODE_SEARCH and not self._download_open
                and self._settings_edit is None and self._settings_secret is None
                and self._picker_new_name is None)

    def _can_hide_footer(self) -> bool:
        return (self._mode == self.MODE_PLAYER and not self._mini_player
                and not self._volume_open and not self._download_open)

    def _can_exit_menu_to_player(self) -> bool:
        if self._mode in (self.MODE_PLAYER, self.MODE_SEARCH):
            return False
        return not (
            self._mini_player or self._volume_open or self._download_open
            or self._settings_edit is not None
            or self._settings_secret is not None
            or self._picker_new_name is not None
            or self._quit_pending or self._logout_pending
            or self._disable_songs_pending or self._clear_cache_pending
            or self._refetch_pending or self._downloads_delete is not None)

    def _open_volume(self, from_focus: bool = False):
        self._volume_from_focus = from_focus
        self._player_focus = False
        self._volume_open = True

    def _close_volume(self):
        self._volume_open = False
        if self._volume_from_focus:
            self._volume_from_focus = False
            self._focus_player()

    def _handle_volume_key(self, key: str):
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

    def _enter_mode(self, mode) -> None:
        self._mini_player = False
        self._mode = mode
        self._nav_history.clear()

    def _handle_player_key(self, key: str):
        if key in (" ", "k"):
            self._toggle_play_key()
        elif key in ("n", KEY_RIGHT):
            self._run("next")
        elif key == KEY_LEFT:
            self._run("prev")
        elif key == KEY_UP:
            self._focus_player()
        elif key == "s":
            self._enter_mode(self.MODE_SEARCH)
            self._search_query = ""
            self._search_history_cursor = None
            self._search_filter = "all" if self._connectivity == ONLINE else "music"
            self._reset_search_results()
        elif key == "t":
            self._mini_player = not self._mini_player
        elif key == "m":
            self._show_more = not self._show_more
        elif key == "o" and self._connectivity == SIGNED_OUT:
            self._sign_in_again()
        elif key == "l":
            self._toggle_like()
        elif key == "r":
            self._run("play.radio")
        elif key == "y":
            self._open_playlist_picker()
        elif key == "d":
            self._open_download()
        elif key == "q":
            self._enter_mode(self.MODE_QUEUE)
            self._queue_cursor = self._queue_index if self._queue else 0
        elif key == "p":
            self._enter_mode(self.MODE_PLAYLISTS)
            if not self._playlists and not self._playlists_loading:
                self._load_playlists()
        elif key == "c":
            self._enter_mode(self.MODE_SETTINGS)
            self._settings_cursor = 0
            self._settings_edit = None
            self._cache.invalidate_audio_count()
            self._forget_downloads()
        elif key == KEY_ESC:
            self._quit_pending = True

    def _handle_back_or_toggle(self, key: str, back=None) -> bool:
        if key in (KEY_ESC, KEY_LEFT):
            (back or self._go_back)()
            return True
        if key == " ":
            self._toggle_play_key()
            return True
        return False

    def _show_player(self) -> None:
        self._mode = self.MODE_PLAYER

    def _cursor_up(self, attr: str, count: int, floor: int = 0) -> None:
        if count and getattr(self, attr) > floor:
            setattr(self, attr, getattr(self, attr) - 1)
        else:
            self._focus_player()

    def _cursor_down(self, attr: str, count: int) -> None:
        if count:
            setattr(self, attr, min(count - 1, getattr(self, attr) + 1))

    def _handle_search_key(self, key: str):
        if key in (KEY_ESC, KEY_LEFT):
            self._go_back()
            return
        if key == " " and self._search_results:
            self._toggle_play_key()
            return
        if key in (KEY_TAB, KEY_SHIFT_TAB):
            self._cycle_search_filter(1 if key == KEY_TAB else -1)
            return
        history = self._history_rows()
        if key == KEY_UP:
            if self._search_results and self._search_cursor > 0:
                self._search_cursor -= 1
            elif history and self._search_history_cursor is not None:
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
                    self._search_more()
            elif history:
                self._search_history_cursor = (
                    0 if self._search_history_cursor is None
                    else min(self._search_history_cursor + 1, len(history) - 1))
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            if self._search_results:
                self._select_search_result()
            elif key != KEY_RIGHT:
                if history and self._search_history_cursor is not None:
                    self._search_query = history[self._search_history_cursor]
                    self._search_history_cursor = None
                self._do_search()
        elif key in (KEY_BACKSPACE, KEY_BACKSPACE2):
            if history and self._search_history_cursor is not None:
                idx = self._search_history_cursor
                if self.remote is not None:
                    self._run("history.forget", query=history[idx])
                self._search_history = self._search_history[:idx] + self._search_history[idx + 1:]
                remaining = self._history_rows()
                self._search_history_cursor = min(idx, len(remaining) - 1) if remaining else None
            else:
                self._search_query = self._search_query[:-1]
                self._search_history_cursor = None
                self._reset_search_results()
        elif len(key) == 1 and key.isprintable():
            self._search_query += key
            self._search_history_cursor = None
            self._reset_search_results()

    def _handle_browse_key(self, key: str):
        if self._handle_back_or_toggle(key):
            return
        if key == KEY_UP:
            self._cursor_up("_browse_cursor", len(self._browse_tracks), floor=-1)
        elif key == KEY_DOWN:
            self._cursor_down("_browse_cursor", len(self._browse_tracks))
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            self._play_browse(max(self._browse_cursor, 0))
        elif key == "a":
            self._play_browse(0)
        elif key == "x":
            self._remove_from_browse_playlist()
        elif key == "y":
            self._open_playlist_picker()
        elif key == "d":
            self._open_download()
        elif key == "D":
            self._open_download(whole=True)

    def _handle_artist_key(self, key: str):
        if self._handle_back_or_toggle(key):
            return
        if key in (KEY_TAB, KEY_SHIFT_TAB):
            self._cycle_artist_section(1 if key == KEY_TAB else -1)
            return
        row_count = len(self._artist_rows())
        if key == KEY_UP:
            self._cursor_up("_artist_cursor", row_count)
        elif key == KEY_DOWN:
            self._cursor_down("_artist_cursor", row_count)
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            self._select_artist_row()
        elif key == "a":
            self._play_artist_section(0)
        elif key == "y":
            self._open_playlist_picker()
        elif key == "d":
            self._open_download()
        elif key == "D":
            self._open_download(whole=True)

    def _handle_queue_key(self, key: str):
        if self._handle_back_or_toggle(key, back=self._show_player):
            return
        if key == KEY_UP:
            self._cursor_up("_queue_cursor", len(self._queue))
        elif key == KEY_DOWN:
            self._cursor_down("_queue_cursor", len(self._queue))
        elif key in (KEY_ENTER, KEY_ENTER2):
            if self._queue:
                self._run("queue.play", index=self._queue_cursor,
                          track_id=getattr(self._queue[self._queue_cursor], "id", None))
        elif key == "x":
            self._remove_from_queue()
        elif key == "y":
            self._open_playlist_picker()
        elif key == "d":
            self._open_download()
        elif key == "D":
            self._open_download(whole=True)

    def _handle_playlists_key(self, key: str):
        if self._handle_back_or_toggle(key, back=self._show_player):
            return
        if key == KEY_UP:
            self._cursor_up("_playlists_cursor", len(self._playlists))
        elif key == KEY_DOWN:
            self._cursor_down("_playlists_cursor", len(self._playlists))
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT) and self._playlists:
            self._open_playlist(self._playlists[self._playlists_cursor])

    def _handle_add_to_playlist_key(self, key: str):
        if self._picker_new_name is not None:
            if key == KEY_ESC:
                self._picker_new_name = None
            elif key in (KEY_BACKSPACE, KEY_BACKSPACE2):
                self._picker_new_name = self._picker_new_name[:-1]
            elif key in (KEY_ENTER, KEY_ENTER2):
                self._picker_create_and_add(self._picker_new_name)
            elif len(key) == 1 and key.isprintable() and len(self._picker_new_name) < PLAYLIST_NAME_MAX:
                self._picker_new_name += key
            return
        if self._handle_back_or_toggle(key):
            return
        playlists = self._picker_playlists()
        if key == KEY_UP:
            self._picker_cursor = max(-1, self._picker_cursor - 1)
        elif key == KEY_DOWN:
            self._picker_cursor = min(len(playlists) - 1, self._picker_cursor + 1)
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            if self._picker_cursor < 0:
                self._picker_new_name = ""
            elif self._picker_cursor < len(playlists):
                self._picker_add_to(playlists[self._picker_cursor])

    def _handle_download_key(self, key: str):
        if key in (KEY_ESC, KEY_LEFT):
            self._download_open = False
        elif key == " ":
            self._toggle_play_key()
        elif key in ("x", "X"):
            self._run("download.cancel")
        elif key == KEY_UP:
            self._download_cursor = max(0, self._download_cursor - 1)
        elif key == KEY_DOWN:
            self._download_cursor = min(len(QUALITY_CHOICES) - 1, self._download_cursor + 1)
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT, "d", "D"):
            bulk = self._download_bulk()
            targets = [t for t in (self._download_tracks if bulk else [self._download_track])
                       if t is not None]
            if targets:
                self._run("download", tier=self._download_tier(), label=self._download_label,
                          track_ids=[t.id for t in targets])
            if not bulk:
                if self._download_run is None:
                    self._download_open = False

    def _handle_secret_key(self, key: str):
        if key == KEY_ESC:
            self._settings_secret = None
        elif key in (KEY_ENTER, KEY_ENTER2):
            typed, self._settings_secret = self._settings_secret, None
            self._set_protected_setting(get_spec("ai_control_key"), hash_ai_key(typed))
        elif key in (KEY_BACKSPACE, KEY_BACKSPACE2):
            self._settings_secret = self._settings_secret[:-1]
        elif len(key) == 1 and key.isprintable():
            self._settings_secret += key

    def _handle_settings_key(self, key: str):
        if self._settings_secret is not None:
            self._handle_secret_key(key)
            return
        if self._settings_edit is not None:
            if key.isdigit():
                if len(self._settings_edit) < 4:
                    self._settings_edit += key
                return
            if key in (KEY_BACKSPACE, KEY_BACKSPACE2):
                self._settings_edit = self._settings_edit[:-1]
                return
            self._commit_setting_edit()
            if key in (KEY_ESC, KEY_ENTER, KEY_ENTER2, KEY_LEFT, KEY_RIGHT):
                return
        elif key.isdigit() and self._begin_setting_edit(key):
            return

        if key in (KEY_ESC, "c"):
            if (self._refetch_job or {}).get("state") == "running":
                self._run("refetch.cancel")
            else:
                self._show_player()
        elif key == " ":
            self._toggle_play_key()
        elif key in ("o", "O") and self._connectivity == SIGNED_OUT:
            self._sign_in_again()
        elif key in ("o", "O"):
            self._logout_pending = True
        elif key in ("x", "X"):
            if (self._download_job or {}).get("state") == "running":
                self._run("download.cancel")
            else:
                self._clear_cache_pending = True
        # Not "p": that already opens playlists on the player screen.
        elif key in ("u", "U"):
            self._run("login.pkce")
        elif key in ("d", "D"):
            self._open_downloads()
        # Asks first: this deliberately makes hundreds of requests.
        elif key in ("r", "R"):
            if (self._refetch_job or {}).get("state") != "running":
                self._refetch_plan = self._refetch_candidates()
                self._refetch_pending = True
        elif key == KEY_UP:
            self._settings_cursor = max(0, self._settings_cursor - 1)
        elif key == KEY_DOWN:
            self._settings_cursor = min(len(SETTINGS_ROWS) - 1, self._settings_cursor + 1)
        elif key == KEY_LEFT:
            self._change_setting(-1)
        elif key in (KEY_RIGHT, KEY_ENTER, KEY_ENTER2):
            if SETTINGS_ROWS[self._settings_cursor]["kind"] == "secret":
                self._settings_secret = ""
            else:
                self._change_setting(1)

    def _open_downloads(self):
        self._push_nav()
        self._mode = self.MODE_DOWNLOADS
        self._downloads_cursor = 0
        self._forget_downloads()

    def _handle_downloads_key(self, key: str):
        if self._handle_back_or_toggle(key):
            return
        rows = self._download_rows()
        if key == KEY_UP:
            if self._downloads_cursor <= 0:
                self._focus_player()
            else:
                self._downloads_cursor -= 1
        elif key == KEY_DOWN:
            self._downloads_cursor = min(len(rows) - 1, self._downloads_cursor + 1)
        elif key in ("x", "X") and rows:
            self._downloads_delete = rows[min(self._downloads_cursor, len(rows) - 1)]
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT) and rows:
            row = rows[min(self._downloads_cursor, len(rows) - 1)]
            self._run("play.downloads", index=self._downloads_cursor, track_id=row["id"])

    def _read_keys(self, select_mod, timeout=IDLE_POLL_SECONDS):
        watch = [sys.stdin]
        if self._wake_r is not None:
            watch.append(self._wake_r)
        if self.remote is not None:
            watch.append(self.remote)
        ready = select_mod.select(watch, [], [], timeout)[0]
        if self._wake_r is not None and self._wake_r in ready:
            try:
                os.read(self._wake_r, 4096)
            except OSError:
                pass
        if self.remote is not None and self.remote in ready:
            self._drain_remote()
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
        if self._wake_w is None:
            return
        try:
            os.write(self._wake_w, b"\x01")
        except OSError:
            pass

    def _repaint(self, live, force=False):
        display = self._build_display()
        key = (self.console.size, tuple(self.console.render(display, self.console.options)))
        if not force and key == self._last_segments:
            return
        self._last_segments = key
        live.update(display, refresh=True)

    def _make_live(self) -> "Live":
        # screen=True (alternate screen) is load-bearing: Rich's cursor-up repaint strands frames on resize.
        # Repaints are driven by _repaint, not auto_refresh.
        return Live(self._build_display(), console=self.console, auto_refresh=False, screen=True)

    def _drain_remote(self) -> None:
        for message in self.remote.read_messages():
            self._on_message(message)
        if self.remote.closed:
            self.running = False

    def _wait_timeout(self, now: Optional[float] = None) -> Optional[float]:
        """How long the TUI may sleep: to the next displayed second while playing, to a
        toast's end while one shows, else until input or an event (ADR-0003)."""
        now = time.time() if now is None else now
        waits = []
        if self._playing and self._play_start_time and self._current_track is not None:
            position = self._play_offset + (now - self._play_start_time)
            waits.append(int(position) + 1 - position + SECOND_EDGE)
        if self._toast and self._toast_until > now:
            waits.append(self._toast_until - now + SECOND_EDGE)
        return max(0.0, min(waits)) if waits else None

    def start(self, interactive: bool = True) -> bool:
        """Bring up the player core: lock, backend, login, saved state, monitor.
        `start_failure` says why not: "running", "login", or an error line."""
        self.start_failure = None
        # One player at a time, decided before anything touches config, cache tracker or audio backend; two instances overwrite the same saved position.
        self._instance_lock_fd, other = _take_instance_lock()
        if other is not None:
            named = f" (pid {other})" if other else ""
            # SIGTERM exits through the same save-and-stop path as quitting
            stop = f", or stop it with: kill {other}" if other else "."
            self.console.print(f"[red]ticli is already running{named}.[/red]\n"
                               f"Switch to its terminal{stop}")
            self.start_failure = "running"
            return False

        player_cmd = _find_audio_player()
        if not player_cmd:
            self.console.print("[red]No audio player found. Install mpv or ffplay.[/red]")
            self.start_failure = "error: no audio player found; install mpv or ffplay"
            return False
        self.audio = AudioPlayer(player_cmd, volume=self.config["volume"], cache=self._cache)
        # Saved volume may predate the backend: 250% written next to mpv, read where only ffplay exists.
        self._clamp_volume_to_backend()

        if not self._login(interactive):
            self.start_failure = "login" if not interactive else "error: login failed"
            return False

        self._reconcile_cache()

        self._load_favorites()

        self._restore_state()

        threading.Thread(target=self._monitor_playback, daemon=True).start()
        return True

    def run(self):
        """The TUI, attached to the background player over `self.remote`."""
        import tty
        import termios
        import select

        if not sys.stdin.isatty():
            self.console.print("[red]Player requires an interactive terminal.[/red]")
            return
        remote = self.remote
        remote.wait_for(remote.send("subscribe"), timeout=SUBSCRIBE_TIMEOUT)
        held, remote.held = remote.held, []
        for message in held:
            self._on_message(message)
        if remote.closed or not self._mirror:
            self.console.print("[red]The player did not answer.[/red] "
                               f"[dim]Its log: {player_log_path()}[/dim]")
            return

        # SIGHUP/SIGTERM leave the player playing: only quitting stops it. The wake matters:
        # Python retries an interrupted select, which while paused has no timeout.
        def _on_signal(signum, frame):
            self.running = False
            self._wake()
        for _sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                signal.signal(_sig, _on_signal)
            except (ValueError, OSError):
                pass

        def _on_resize(signum, frame):
            self._resized = True
            self._wake()
        try:
            signal.signal(signal.SIGWINCH, _on_resize)
        except (AttributeError, ValueError, OSError):
            pass

        try:
            self._wake_r, self._wake_w = os.pipe()
            os.set_blocking(self._wake_r, False)
            os.set_blocking(self._wake_w, False)
            # A signal can land on another thread, which never interrupts this select.
            signal.set_wakeup_fd(self._wake_w)
        except (OSError, ValueError):
            pass

        old_settings = termios.tcgetattr(sys.stdin)
        # Kept so a PKCE sign-in can hand the terminal back for a paste.
        self._tty_settings = old_settings
        try:
            tty.setcbreak(sys.stdin.fileno())

            with self._make_live() as live:
                self._live = live
                self._repaint(live, force=True)
                self._honour_start_flags()
                self._repaint(live, force=True)
                while self.running:
                    keys = self._read_keys(select, timeout=self._wait_timeout())
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
            if self._quitting and not remote.closed:
                # Quit means quit: the player stops and saves, then leaves once nobody needs it.
                remote.request("stop", timeout=STOP_TIMEOUT)
            lost = remote.closed and not self._quitting and not self._logged_out
            remote.close()
            try:
                signal.set_wakeup_fd(-1)
            except ValueError:
                pass
            for fd in (self._wake_r, self._wake_w):
                try:
                    if fd is not None:
                        os.close(fd)
                except OSError:
                    pass
            self._wake_r = self._wake_w = None

        if self._logged_out:
            self.console.print("[yellow]Logged out. Tokens cleared.[/yellow]")
        elif lost:
            self.console.print("[red]The player stopped.[/red] "
                               f"[dim]Its log: {player_log_path()}[/dim]")
        elif self._quitting:
            self.console.print("[dim]Player closed.[/dim]")
        else:
            self.console.print("[dim]Detached; the player keeps playing. Run ticli to return.[/dim]")


def run_tui(quality: Optional[str] = None, login_flow: Optional[str] = None) -> None:
    """`ticli`: attach to the background player, starting it first if needed."""
    from ticli import ipc
    console = Console()
    if not sys.stdin.isatty():
        console.print("[red]Player requires an interactive terminal.[/red]")
        return
    conn, status = ipc.connect_or_start(quality, login_flow)
    if conn is None and status == "login":
        # The player has no terminal, so a first sign-in happens here, then it starts again.
        if not HeadlessTidalPlayer(quality=quality, login_flow=login_flow)._login():
            return
        conn, status = ipc.connect_or_start(quality, login_flow)
    if conn is None:
        console.print(f"[red]Could not start the player:[/red] {status}")
        return
    HeadlessTidalPlayer(quality=quality, login_flow=login_flow, remote=conn).run()


def player_log_path():
    from ticli import ipc
    return ipc.log_path()


def main():
    from ticli.cli import main as _main
    _main()

if __name__ == "__main__":
    main()

