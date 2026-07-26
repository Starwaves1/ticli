"""Ticli - Terminal music player for TIDAL.

Uses tidalapi for TIDAL API access and ffplay/mpv for audio playback.
OAuth login via browser, session persisted to disk.
"""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import threading
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

from ticli.utils.credential_store import save_tokens, load_tokens
from ticli.utils.config import (
    QUALITY_CHOICES,
    QUALITY_MEANINGS,
    SETTINGS_SPEC,
    coerce,
    cycle_value,
    display_value,
    get_spec,
    load_config,
    save_config,
)
from ticli.utils.cache import CachedTrack, MetadataCache, format_gb
from ticli.utils import artwork, downloads, tags

STATE_DIR = Path.home() / ".config" / "ticli"
STATE_FILE = STATE_DIR / "player_state.json"

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
# and no more, and TIDAL answers its LOSSLESS requests with HIGH rather than
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
# How much of the backend's own complaint fits on the toast line, and how long
# it stays up — longer than an ordinary toast, because it is the only notice
# that the thing the user asked for did not happen.
PLAYER_ERROR_CHARS = 90
PLAYER_ERROR_SECONDS = 8.0

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
    """
    ext = DEFAULT_AUDIO_EXT
    done = 0
    total = 0
    with open(part, "wb") as handle:
        for index, source in enumerate(sources):
            with requests.get(source, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
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


def _empty_search_pool() -> dict:
    """Fetched-but-not-yet-shown search rows, per category."""
    return {"tracks": [], "albums": [], "artists": []}


def _empty_search_reservoir() -> dict:
    """Everything one query has brought back, kept whole and never consumed.

    A fetch asks for all three categories at once, so this is what every scope
    draws on: `offset` is how deep into the query it has gone, `exhausted` is
    per category because they run out at different depths, and `stopped` is a
    failure the scopes should stop asking about. In memory only, for the length
    of the run — deliberately not the on-disk index in utils/cache.py.
    """
    return {
        "tracks": [], "albums": [], "artists": [],
        "offset": 0,
        "exhausted": {"tracks": False, "albums": False, "artists": False},
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
        base = self._audio_cache_base(key)
        if not base:
            return None
        directory, stem = os.path.split(base)
        try:
            for path in sorted(Path(directory).glob(f"{stem}.*")):
                if path.suffix != ".part" and path.is_file():
                    return str(path)
        except OSError:
            pass
        return None

    def _start_download(self, url: str, cache_key, gen: int):
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
                # One more song on disk: the settings page's count is stale
                try:
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
        """
        if self.player_cmd == "mpv":
            return [
                "--demuxer=lavf", "--demuxer-lavf-format=hls",
                # mpv's key-value lists split on commas, so the whitelist has
                # to be passed length-prefixed to survive intact
                f"--demuxer-lavf-o=protocol_whitelist="
                f"%{len(HLS_PROTOCOLS)}%{HLS_PROTOCOLS}",
            ]
        return ["-protocol_whitelist", HLS_PROTOCOLS, "-f", "hls"]

    def cache_audio_dir(self):
        """The audio cache directory, created if needed. Private like the rest
        of the cache — these are someone's listening habits."""
        from ticli.utils import cache as cache_mod
        cache_mod.CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = cache_mod.audio_dir()
        path.mkdir(exist_ok=True, mode=0o700)
        return path

    def play_url(self, url: str, seek: float = 0, title: Optional[str] = None,
                 cache_key=None, local: Optional[str] = None):
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
        self.stop()
        with self._lock:
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
            gen = self._download_gen
        # Off the lock and off this thread: fetching the track must never hold
        # up the process that is playing it
        if not have_kept:
            self._start_download(url, cache_key, gen)

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
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                self._process = None
                self._paused = True

    def resume(self) -> bool:
        """Resume paused playback. False if it failed (e.g. player died)."""
        with self._lock:
            if not self._paused:
                return False
            if self.player_cmd == "mpv" and self._ipc_path:
                if self._mpv_command({"command": ["set_property", "pause", False]}):
                    self._paused = False
                    return True
                return False
            else:
                # ffplay: restart from cached local file (instant, no network)
                if self._cache_file and os.path.exists(self._cache_file):
                    self._play_from_cache(self._seek_offset)
                    return True
                elif self._current_url:
                    # Cache not ready — fall back to URL
                    self._paused = False
                    url = self._current_url
                    seek = self._seek_offset
                    self._lock.release()
                    try:
                        self.play_url(url, seek=seek)
                    finally:
                        self._lock.acquire()
                    return True
            return False

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
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
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
            # Any download still running is for a track we are leaving: this
            # is what tells it to abandon itself and clean up its .part
            self._download_gen += 1
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
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
    MODE_DOWNLOAD = "download"

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

    # Setting name → tidalapi quality, and the terse badge shown on the player.
    # One name per tidalapi tier, same spelling as tidalapi uses, so the setting
    # name, the badge and the bytes TIDAL sends can never disagree. (Older
    # configs named every tier one step low; utils.config migrates them.)
    QUALITY_MAP = {
        "LOW": tidalapi.Quality.low_96k,
        "HIGH": tidalapi.Quality.low_320k,
        "LOSSLESS": tidalapi.Quality.high_lossless,
        "HIRES": tidalapi.Quality.hi_res_lossless,
    }
    # Search scopes, in the order Tab cycles them. "playlists" is the odd one
    # out on purpose: TIDAL has no server-side search of your own playlists, so
    # that scope is answered from the local index and never asks the network.
    SEARCH_FILTERS = ("all", "tracks", "albums", "artists", "playlists")
    SEARCH_FILTER_LABELS = {
        "all": "All",
        "tracks": "Tracks",
        "albums": "Albums",
        "artists": "Artists",
        "playlists": "My Playlists",
    }
    # Which categories a scope asks TIDAL for. Keyed by the plural names
    # session.search() answers with, so the fetch never has to translate.
    SEARCH_FILTER_KINDS = {
        "all": ("tracks", "albums", "artists"),
        "tracks": ("tracks",),
        "albums": ("albums",),
        "artists": ("artists",),
    }

    QUALITY_LABELS = {
        "LOW": "96k AAC",
        "HIGH": "320k AAC",
        "LOSSLESS": "LOSSLESS",
        "HIRES": "HI-RES",
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
        self._bar_width = self.config["progress_bar_width"]
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
        self._liked_ids: set = set()
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
        # Download screen state. _download_track is what [d] was pressed on,
        # _download_cursor the tier under the cursor (pre-placed on the one
        # settings is set to), and _download_job the one job at a time — a
        # whole dict, replaced rather than mutated, so the paint thread can
        # never read it half-written.
        self._download_track = None
        self._download_cursor = 0
        self._download_job = None
        self._download_job_gen = 0
        # Settings page state. _settings_edit is the digits typed into a number
        # row so far, or None when the arrows are just navigating; it is only
        # ever replaced wholesale, never appended to in place
        self._settings_cursor = 0
        self._settings_edit: Optional[str] = None
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
        # Show more controls
        self._show_more = False
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
        """Atomically write the state file (temp + rename, never torn)."""
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_FILE)

    def _merge_position_into_saved_state(self):
        """Refresh only the position in the saved file, if the track matches."""
        if self._current_track is None or not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
            ids = data.get("track_ids", [])
            idx = data.get("queue_index", 0)
            if ids and 0 <= idx < len(ids) and ids[idx] == self._current_track.id:
                data["position"] = self._get_position()
                self._write_state_file(data)
        except Exception as e:
            logger.debug("Failed to merge position into saved state: %s", e)

    def _save_state(self):
        """Save queue and playback state to disk for next session."""
        # If the restore never finished attaching the queue, our in-memory
        # state is incomplete — a full save would shrink the good file. Still
        # refresh the position in case the user played the restored track.
        if self._restore_pending and not self._queue:
            self._merge_position_into_saved_state()
            return
        try:
            track_ids = [t.id for t in self._queue]
            queue_index = self._queue_index
            if not track_ids and self._current_track is not None:
                track_ids = [self._current_track.id]
                queue_index = 0
            state = {
                "track_ids": track_ids,
                "queue_index": queue_index,
                "position": self._get_position(),
                "search_history": self._search_history[:20],
            }
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
        """Restore queue and search history from previous session."""
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return
        self._search_history = data.get("search_history", [])[:20]
        track_ids = data.get("track_ids", [])
        queue_index = data.get("queue_index", 0)
        position = data.get("position", 0) or 0
        if not track_ids:
            return

        self._restore_pending = True

        def _run():
            try:
                # Fetch the current track first so it appears immediately,
                # paused at its saved position, before the rest of the queue loads
                idx = min(max(queue_index, 0), len(track_ids) - 1)
                current = None
                try:
                    current = self.session.track(track_ids[idx])
                except Exception:
                    pass
                if current is not None and not self._playing and self._current_track is None:
                    duration = getattr(current, "duration", 0) or 0
                    if 1 <= position < duration - 2:
                        self._play_offset = position
                    self._current_track = current

                tracks = []
                for i, tid in enumerate(track_ids):
                    if i == idx and current is not None:
                        tracks.append(current)
                        continue
                    try:
                        t = self.session.track(tid)
                        if t:
                            tracks.append(t)
                    except Exception:
                        pass
                # Don't clobber anything the user started while we were loading
                # (playing the restored track itself is fine — attach its queue)
                if tracks and self._current_track in (None, current):
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
                # A deliberate download is played from the user's own folder
                # and costs no request at all — so it is looked for before the
                # stream URL is asked for, not after
                owned = downloads.path_for(real.id)
                url = "" if owned else (
                    self._take_prefetched(real.id) or self._stream_url(real))
                # Superseded by a newer track, or paused while we were
                # fetching — either way this start is no longer wanted
                if self._play_gen != gen or not self._playing:
                    return
                artist = ", ".join(a.name for a in real.artists) if real.artists else ""
                title = f"{real.name} — {artist}" if artist else real.name
                self.audio.play_url(url, seek=seek, title=title, cache_key=real.id,
                                    local=str(owned) if owned else None)
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

    def _take_prefetched(self, track_id) -> Optional[str]:
        """A stream URL fetched moments ago for this track, if there is one.
        Consumed on use — a signed URL is worth having exactly once."""
        prefetched = self._prefetch
        self._prefetch = None
        if not prefetched:
            return None
        pid, url, fetched_at = prefetched
        if pid != track_id or time.time() - fetched_at > PREFETCH_MAX_AGE:
            return None
        return url

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

        def _run():
            try:
                real = self._resolve_track(nxt)
                if real is not None:
                    self._prefetch = (real.id, self._stream_url(real), time.time())
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
        stream = track.get_stream()
        self._note_granted_quality(getattr(stream, "audio_quality", None))
        manifest = stream.get_stream_manifest()
        if manifest.is_bts:
            return manifest.get_urls()[0]
        return _write_hls_playlist(track.id, _hls_playlist(manifest.dash_info))

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
        """Start radio based on current track."""
        if not self._current_track:
            return
        track_id = self._current_track.id
        def _run():
            try:
                radio_tracks = self._current_track.get_track_radio(limit=25)
                if radio_tracks:
                    self._queue = radio_tracks
                    self._queue_index = 0
                    self._play_track(self._queue[0])
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

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
                self._maybe_prefetch_next()
            # A press that came in too soon after the last one left its
            # position waiting; this tick is what delivers it
            self._flush_seek()
            # Save state periodically so a crash doesn't lose the position
            if time.time() - last_save > 10:
                self._save_state()
                last_save = time.time()
            if self.audio:
                self._handle_media_key(self.audio.poll_media_key())
            time.sleep(0.5)

    # ── Display builders ──

    def _track_has_time_left(self, margin: float = 2.0) -> bool:
        """Whether the current track stopped early enough to be worth
        resuming. An unknown duration counts as "yes" — better one extra
        respawn than a silently dropped track."""
        duration = getattr(self._current_track, "duration", None) or 0
        if duration <= 0:
            return True
        return self._get_position() < duration - margin

    def _get_position(self) -> float:
        if self._play_start_time and self._playing:
            return self._play_offset + (time.time() - self._play_start_time)
        return self._play_offset

    def _artwork_text(self):
        """The current cover as half-block pixel art, or None.

        None is the normal answer for most of a track's first second: nothing
        is fetched on this thread. The first paint that wants artwork starts a
        daemon thread and returns None; when that thread lands it assigns the
        pixels and wakes the loop, and the next paint has a picture. Every
        reason there might never be one — no cover, no colour, no room, a
        failed fetch, an undecodable image — comes out here as None too.
        """
        if (not self._show_artwork or self._mini_player
                or self._mode != self.MODE_PLAYER):
            return None
        cover = artwork.cover_id_of(self._current_track)
        if not cover or not artwork.supports_art(self.console):
            return None
        try:
            width, height = self.console.size
        except Exception:
            return None
        size = artwork.art_size(width, height)
        if size is None:  # terminal too small to give artwork the room
            return None
        cols, rows = size
        ready = self._artwork
        if ready is not None and ready[:3] == (cover, cols, rows):
            # A stored None is an answered question: this cover has no art
            return artwork.render(ready[3], indent=3) if ready[3] else None
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

        # Full player display
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

        progress_pct = (position / duration * 100) if duration > 0 else 0
        pos_str = format_time(position)
        dur_str = format_time(duration) if duration > 0 else "--:--"

        bar_width = self._bar_width
        filled = int(bar_width * min(progress_pct, 100) / 100)
        if duration > 0:
            bar = "\u2501" * filled + "\u2578" + "\u2500" * max(0, bar_width - filled - 1)
        else:
            bar = "\u2500" * bar_width
        progress_line = Text()
        progress_line.append(f"   {pos_str} ", style="cyan")
        progress_line.append(bar, style="bold cyan" if self._playing else "dim")
        progress_line.append(f" {dur_str}", style="cyan")
        if self._player_focus:
            # Focus is a state the arrows are in, so it has to be on screen —
            # one marker on the line the arrows now move
            progress_line.append(f"  \u21c6 {SEEK_STEP_SECONDS}s", style="bold yellow")

        # Queue info
        status_line = Text()
        if self._queue:
            status_line.append(f"   Queue: {self._queue_index + 1}/{len(self._queue)}", style="dim")
        quality_label = self.QUALITY_LABELS.get(self._quality_name, "")
        if quality_label:
            status_line.append(f"   {quality_label}", style="dim cyan")

        # Next track preview (only in player mode)
        up_next = Text()
        if self._mode == self.MODE_PLAYER and self._queue and self._queue_index < len(self._queue) - 1:
            t = self._queue[self._queue_index + 1]
            t_name = t.name if hasattr(t, "name") else "?"
            t_artist = t.artists[0].name if hasattr(t, "artists") and t.artists else ""
            up_next.append("\n   Next: ", style="dim")
            up_next.append(t_name, style="dim white")
            if t_artist:
                up_next.append(f" \u2022 {t_artist}", style="dim")

        content = Text()
        art = self._artwork_text()
        if art is not None:
            content.append_text(art)
            content.append("\n\n")
        content.append_text(track_line)
        content.append("\n")
        content.append_text(album_line)
        content.append("\n")
        content.append_text(progress_line)
        content.append("\n")
        content.append_text(status_line)
        content.append_text(up_next)
        return content

    def _build_search_display(self) -> Text:
        content = Text()
        content.append("   Search: ", style="bold yellow")
        content.append(self._search_query, style="white")
        content.append("\u2588", style="bold white")

        # The scope row. It is always on screen, so Tab has something to point
        # at and the active scope is never hidden state. A scope already shown
        # for this query is marked, so "Tab is free from here on" is visible
        # rather than something you have to have read the source to know.
        content.append("\n   [Tab]", style="bold")
        for i, name in enumerate(self.SEARCH_FILTERS):
            content.append("  " if i == 0 else " \u00b7 ", style="dim")
            active = name == self._search_filter
            content.append(
                self.SEARCH_FILTER_LABELS[name],
                style="bold cyan" if active else "dim")
            if name in self._search_views and not self._search_views[name]["loading"]:
                content.append("\u00b7", style="dim cyan")

        if self._search_loading and not self._search_results:
            # A scope waiting for its first page. Not the same as one that came
            # back empty (green, below) and not the same as one served out of
            # what the session already had (marked under the rows).
            content.append("\n\n   Searching...", style="dim yellow")
        elif self._search_message:
            content.append(f"\n\n   {self._search_message}", style="dim green")
        elif self._search_results:
            total = len(self._search_results)
            page = self._page_size
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
                type_styles = {"track": "bold green", "album": "bold magenta", "artist": "bold yellow"}
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
            page = self._page_size
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
                    content.append("\u25b6 Play All", style="bold cyan")
                else:
                    content.append("    ", style="")
                    content.append("\u25b6 Play All", style="dim green")

            for i in range(page_start, page_end):
                track = self._browse_tracks[i]
                content.append("\n")
                if i == self._browse_cursor:
                    content.append("  \u25b8 ", style="bold cyan")
                else:
                    content.append("    ", style="")
                content.append(f"{i+1:>2}. ", style="dim")
                content.append(track.name, style="bold white" if i == self._browse_cursor else "white")
                if track.artists:
                    content.append(f"  {track.artists[0].name}", style="dim")
                content.append(f"  {format_time(track.duration)}", style="dim cyan")
            if total > page:
                page_num = (max(0, self._browse_cursor - 1) // page) + 1 if self._browse_cursor > 0 else 1
                total_pages = (total + page - 1) // page
                content.append(f"\n\n   Page {page_num}/{total_pages}", style="dim")

        return content

    def _build_artist_display(self) -> Text:
        content = Text()
        name = getattr(self._artist, "name", "") or "Artist"
        content.append(f"   {name}", style="bold magenta")

        # The tab row, always on screen for the same reason search's scope row
        # is: which section you are in is never hidden state, and Tab has
        # something to point at.
        content.append("\n   [Tab]", style="bold")
        for i, key in enumerate(self.ARTIST_SECTIONS):
            content.append("  " if i == 0 else " · ", style="dim")
            active = key == self._artist_section
            content.append(
                self.ARTIST_SECTION_LABELS[key],
                style="bold cyan" if active else "dim")

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
        page = self._page_size
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
        if total > page:
            page_num = (cursor // page) + 1
            total_pages = (total + page - 1) // page
            content.append(f"\n\n   Page {page_num}/{total_pages}", style="dim")

        return content

    def _build_queue_display(self) -> Text:
        content = Text()
        content.append("   Queue", style="bold yellow")
        if not self._queue:
            content.append("\n\n   Queue is empty", style="dim")
        else:
            total = len(self._queue)
            page = self._page_size
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
            if total > page:
                page_num = (self._queue_cursor // page) + 1
                total_pages = (total + page - 1) // page
                content.append(f"\n\n   Page {page_num}/{total_pages}", style="dim")
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
            page = self._page_size
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
            if total > page:
                page_num = (self._playlists_cursor // page) + 1
                total_pages = (total + page - 1) // page
                content.append(f"\n\n   Page {page_num}/{total_pages}", style="dim")
        else:
            content.append("\n\n   No playlists found", style="dim")

        return content

    def _build_add_to_playlist_display(self) -> Text:
        content = Text()
        track_name = self._picker_track.name if self._picker_track is not None else "?"
        content.append("   Add to playlist: ", style="bold magenta")
        content.append(track_name, style="bold white")

        if self._picker_loading:
            content.append("\n\n   Loading playlists...", style="dim yellow")
        elif self._editable_playlists:
            total = len(self._editable_playlists)
            page = self._page_size
            page_start = (self._picker_cursor // page) * page
            page_end = min(page_start + page, total)
            content.append("\n", style="")
            for i in range(page_start, page_end):
                pl = self._editable_playlists[i]
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
            if total > page:
                page_num = (self._picker_cursor // page) + 1
                total_pages = (total + page - 1) // page
                content.append(f"\n\n   Page {page_num}/{total_pages}", style="dim")
        else:
            content.append("\n\n   No playlists you can edit", style="dim")

        return content

    def _build_settings_display(self) -> Text:
        content = Text()
        content.append("   Settings", style="bold magenta")
        content.append("\n", style="")

        for i, spec in enumerate(SETTINGS_SPEC):
            selected = (i == self._settings_cursor)
            content.append("\n")
            if selected:
                content.append("  ▸ ", style="bold cyan")
            else:
                content.append("    ", style="")
            content.append(f"{spec['label']:<20}", style="bold white" if selected else "white")
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
            if spec["key"] == "volume":
                # Deliberately blue, not dim: past unity mpv is amplifying a
                # finished master, and that is a thing to notice, not to skim
                if not editing and int(value) >= 105:
                    content.append("  louder than the master — quality suffers", style="blue")
                # The ceiling in effect, not the spec's — the row must never
                # advertise a number this backend cannot reach. Named, so the
                # limit reads as a fact about ffplay rather than a mystery
                ceiling = self._setting_ceiling(spec)
                if self.audio and ceiling < spec["max"]:
                    content.append(
                        f"  {self.audio.player_cmd} caps at {ceiling}%", style="blue")

        spec = SETTINGS_SPEC[self._settings_cursor]
        if self._settings_edit is not None:
            content.append(
                "\n\n   Typing a number — Enter or Esc saves it, Backspace deletes",
                style="dim",
            )
        content.append(f"\n\n   {spec['desc']}", style="dim")
        # The whole ladder, so ←/→ lands somewhere you already understand.
        # Fits 80 columns for the four quality tiers.
        if spec.get("value_desc"):
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
                # Badge text, unless it just repeats the name (HIRES / HI-RES)
                short = self.QUALITY_LABELS.get(choice, "")
                if short and short.replace("-", "") != choice:
                    content.append(
                        f" {short}", style="dim" if gated else ("cyan" if on else "dim"))
            content.append_text(self._build_quality_gate_note())
        # A --quality flag beats the saved value for this run without changing it
        if self._quality_name != self.config.get("quality"):
            content.append(
                f"\n   Overridden this run by --quality {self._quality_name}",
                style="dim yellow",
            )
        # Shown like logout, for the same reason: an action the page offers,
        # which the value table above has no way to express
        # Bare gigabytes, not "x / y GB": the budget is its own row above and
        # can be 0, which would read as a broken fraction down here
        songs = self._cache.audio_count()
        content.append(
            f"\n\n   {songs} song{'' if songs == 1 else 's'} cached"
            f" · {format_gb(self._cache.disk_bytes())}", style="dim")
        content.append("   [x]", style="bold")
        content.append(" clear cache", style="dim")
        # Downloads are the other tier and read as one: their own folder, their
        # own count, and deliberately no budget and no [x] — nothing in ticli
        # deletes a track somebody asked for. Saying where they are is the
        # whole point of the line; a folder you cannot find is not user-owned.
        kept = downloads.downloaded_count()
        content.append(
            f"\n   {kept} song{'' if kept == 1 else 's'} downloaded"
            f" · {format_gb(downloads.total_bytes())}", style="dim")
        content.append(f"\n   {downloads.display_dir()}", style="dim")
        content.append("   never evicted, never counted against the budget",
                       style="dim")
        # Account lives here rather than on the player screen — it's a thing you
        # look at when you're already in settings, not while listening
        content.append("\n   Logged in as ", style="dim")
        content.append(self._user_display_name or "—", style="bold")
        content.append("   [o]", style="bold")
        content.append(" log out", style="dim")
        content.append_text(self._build_pkce_line())
        return content

    def _build_download_display(self) -> Text:
        """The download screen: one track, one column of tiers.

        A single column because that is what a cursor reads down. The tier
        under the cursor says `download now` — the action belongs to the row
        you are on, not to a separate button somewhere else — and *every* row
        shows its size, because all four estimates are arithmetic on a
        duration we already have and cost nothing to show. They are prefixed
        `~` and the line under the picker says how good the `~` is for the
        tier you are on, which is not the same answer for AAC and for FLAC.
        """
        content = Text()
        track = self._download_track
        content.append("   Download", style="bold magenta")
        if track is None:
            content.append("\n\n   No track selected", style="dim")
            return content

        artist = ", ".join(a.name for a in (getattr(track, "artists", None) or [])
                           if getattr(a, "name", None))
        album = getattr(track, "album", None)
        content.append(f"\n\n   {getattr(track, 'name', '?')}", style="bold white")
        if artist:
            content.append(f" — {artist}", style="white")
        if album is not None and getattr(album, "name", None):
            content.append(f"\n   {album.name}", style="dim")
        content.append(f"   {format_time(getattr(track, 'duration', 0))}", style="dim")

        existing = downloads.path_for(getattr(track, "id", None))
        if existing is not None:
            content.append(f"\n\n   Already downloaded · {existing}", style="green")

        content.append("\n")
        for index, tier in enumerate(QUALITY_CHOICES):
            selected = index == self._download_cursor
            gated = self._quality_unavailable(tier)
            content.append("\n")
            content.append("  ▸ " if selected else "    ",
                           style="bold cyan" if selected else "")
            style = "dim" if gated else ("bold white" if selected else "white")
            content.append(f"{tier:<10}", style=style)
            # The action sits on the hovered row and nowhere else
            content.append(f"{'download now' if selected else '':<14}",
                           style="bold cyan" if selected and not gated else "dim")
            estimate = self._download_estimate(tier)
            # Right-aligned, so four estimates read as a column of sizes
            content.append(f"{'~' + downloads.format_bytes(estimate):>10}",
                           style="dim" if gated or not selected else "cyan")

        # What the hovered tier is, and what its `~` is worth. One line rather
        # than four, because a description repeated on every row is noise and
        # the accuracy genuinely differs between AAC and FLAC.
        tier = self._download_tier()
        content.append(f"\n\n   {QUALITY_MEANINGS.get(tier, '')}", style="dim")
        content.append(f"\n   {downloads.ESTIMATE_ACCURACY.get(tier, '')}", style="dim")
        if self._quality_unavailable(tier):
            content.append(
                f"\n   This login isn't served {tier}; TIDAL sends "
                f"{self._quality_ceiling} instead — [u] on the settings page fixes it",
                style="dim yellow")
        content.append_text(self._build_download_status())
        content.append(f"\n\n   Saved to {downloads.display_dir()}"
                       "/<artist>/<album>/", style="dim")
        return content

    def _build_download_status(self) -> Text:
        """Loading, done and failed as three different screens, and honest
        about the tags: the file's own folder and filename always carry the
        artist, album and title, and what got written *inside* the file is
        reported from what was actually written, never from intent."""
        line = Text()
        job = self._download_job
        if not job:
            return line
        state = job.get("state")
        if state == "running":
            done, total = job.get("done") or 0, job.get("total") or 0
            share = f" · {done * 100 // total}%" if total else ""
            line.append(f"\n\n   Downloading {job.get('tier')}"
                        f" · {downloads.format_bytes(done)}{share}", style="yellow")
            line.append("   [x] cancel", style="dim")
        elif state == "cancelled":
            line.append("\n\n   Download cancelled", style="dim")
        elif state == "done":
            line.append(f"\n\n   Saved {job.get('path')}", style="green")
            written = job.get("tags")
            line.append(
                f"\n   Tagged with {written}" if written else
                "\n   No tags could be written into this container — the folder "
                "and filename carry the artist, album and title", style="dim")
        elif state == "failed":
            line.append(f"\n\n   Download failed — {job.get('error')}", style="red")
            line.append("   [Enter] retry", style="dim")
        return line

    def _build_quality_gate_note(self) -> Text:
        """Why the dimmed tiers in the ladder above are dimmed — and, if the
        tier actually in effect is one of them, where the fix is. Empty
        whenever nothing is gated, which is the normal case."""
        line = Text()
        gated = [c for c in QUALITY_CHOICES if self._quality_unavailable(c)]
        if not gated:
            return line
        line.append(
            f"\n   {' and '.join(gated)} — this login isn't served them; "
            f"TIDAL sends {self._quality_ceiling} instead",
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
        line.append(
            "\n   A clunkier sign-in — you paste back the address your browser lands on —"
            "\n   and in exchange LOSSLESS and HI-RES stream as real FLAC instead of AAC.",
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

    def _build_clear_cache_confirm(self) -> Text:
        content = Text()
        songs = self._cache.audio_count()
        content.append(
            f"\n   Delete {songs} cached song{'' if songs == 1 else 's'}? ",
            style="bold yellow")
        content.append("y", style="bold")
        content.append(" to confirm, any other key to cancel", style="dim")
        return content

    def _build_display(self) -> Panel:
        player = self._build_player_display()

        controls = Text()
        if self._mode == self.MODE_PLAYER:
            controls.append("   [space]", style="bold")
            controls.append(" play/pause  ", style="dim")
            controls.append("[\u2190/\u2192]", style="bold")
            if self._player_focus:
                controls.append(f" seek {SEEK_STEP_SECONDS}s  ", style="dim")
                controls.append("[\u2193]", style="bold")
                controls.append(" back  ", style="dim")
            else:
                controls.append(" prev/next  ", style="dim")
            controls.append("[s]", style="bold")
            controls.append(" search  ", style="dim")
            controls.append("[t]", style="bold")
            controls.append(" tiny  ", style="dim")
            controls.append("[m]", style="bold")
            controls.append(" more", style="dim")
            if self._show_more:
                controls.append("\n   [\u2191]", style="bold")
                controls.append(" scrub  ", style="dim")
                controls.append("[l]", style="bold")
                controls.append(" like  ", style="dim")
                controls.append("[r]", style="bold")
                controls.append(" radio  ", style="dim")
                controls.append("[y]", style="bold")
                controls.append(" add to playlist  ", style="dim")
                controls.append("[d]", style="bold")
                controls.append(" download  ", style="dim")
                controls.append("[q]", style="bold")
                controls.append(" queue  ", style="dim")
                controls.append("[p]", style="bold")
                controls.append(" playlists  ", style="dim")
                controls.append("[c]", style="bold")
                controls.append(" settings  ", style="dim")
                controls.append("[Esc]", style="bold")
                controls.append(" quit", style="dim")
        elif self._mode == self.MODE_SEARCH:
            controls.append("   [Enter/\u2192]", style="bold")
            controls.append(" search/open  ", style="dim")
            controls.append("[\u2191/\u2193]", style="bold")
            controls.append(" navigate  ", style="dim")
            controls.append("[Tab]", style="bold")
            controls.append(" filter  ", style="dim")
            if self._search_results:
                controls.append("[Space]", style="bold")
                controls.append(" pause/play  ", style="dim")
            controls.append("[\u2190/Esc]", style="bold")
            controls.append(" back  ", style="dim")
            controls.append("[Bksp]", style="bold")
            controls.append(" delete", style="dim")
        elif self._mode == self.MODE_BROWSE:
            controls.append("   [Enter/\u2192]", style="bold")
            controls.append(" play track  ", style="dim")
            controls.append("[\u2191/\u2193]", style="bold")
            controls.append(" navigate  ", style="dim")
            controls.append("[Space]", style="bold")
            controls.append(" pause/play  ", style="dim")
            controls.append("[a]", style="bold")
            controls.append(" play all  ", style="dim")
            controls.append("[y]", style="bold")
            controls.append(" add to playlist  ", style="dim")
            if self._browse_playlist is not None:
                controls.append("[x]", style="bold")
                controls.append(" remove  ", style="dim")
            controls.append("[\u2190/Esc]", style="bold")
            controls.append(" back", style="dim")
        elif self._mode == self.MODE_ARTIST:
            controls.append("   [Enter/→]", style="bold")
            controls.append(" open  ", style="dim")
            controls.append("[↑/↓]", style="bold")
            controls.append(" navigate  ", style="dim")
            controls.append("[Tab]", style="bold")
            controls.append(" section  ", style="dim")
            controls.append("[a]", style="bold")
            controls.append(" play all  ", style="dim")
            controls.append("[←/Esc]", style="bold")
            controls.append(" back", style="dim")
        elif self._mode == self.MODE_QUEUE:
            controls.append("   [Enter]", style="bold")
            controls.append(" play  ", style="dim")
            controls.append("[\u2191/\u2193]", style="bold")
            controls.append(" navigate  ", style="dim")
            controls.append("[Space]", style="bold")
            controls.append(" pause/play  ", style="dim")
            controls.append("[x]", style="bold")
            controls.append(" remove  ", style="dim")
            controls.append("[y]", style="bold")
            controls.append(" add to playlist  ", style="dim")
            controls.append("[\u2190/Esc]", style="bold")
            controls.append(" back", style="dim")
        elif self._mode == self.MODE_PLAYLISTS:
            controls.append("   [Enter/\u2192]", style="bold")
            controls.append(" open  ", style="dim")
            controls.append("[\u2191/\u2193]", style="bold")
            controls.append(" navigate  ", style="dim")
            controls.append("[Space]", style="bold")
            controls.append(" pause/play  ", style="dim")
            controls.append("[\u2190/Esc]", style="bold")
            controls.append(" back", style="dim")
        elif self._mode == self.MODE_ADD_TO_PLAYLIST:
            controls.append("   [Enter]", style="bold")
            controls.append(" add  ", style="dim")
            controls.append("[\u2191/\u2193]", style="bold")
            controls.append(" navigate  ", style="dim")
            controls.append("[\u2190/Esc]", style="bold")
            controls.append(" cancel", style="dim")
        elif self._mode == self.MODE_DOWNLOAD:
            controls.append("   [Enter]", style="bold")
            controls.append(" download  ", style="dim")
            controls.append("[\u2191/\u2193]", style="bold")
            controls.append(" quality  ", style="dim")
            controls.append("[Space]", style="bold")
            controls.append(" pause/play  ", style="dim")
            controls.append("[\u2190/Esc]", style="bold")
            controls.append(" back", style="dim")
        elif self._mode == self.MODE_SETTINGS:
            controls.append("   [\u2191/\u2193]", style="bold")
            controls.append(" select  ", style="dim")
            controls.append("[\u2190/\u2192]", style="bold")
            controls.append(" change  ", style="dim")
            controls.append("[Space]", style="bold")
            controls.append(" pause/play  ", style="dim")
            # No [x] here on purpose: this row already fills 74 columns, which
            # is exactly the panel's inner width at an 80-column terminal, and
            # clear cache has its own line above (the way logout does)
            controls.append("[o]", style="bold")
            controls.append(" log out  ", style="dim")
            controls.append("[Esc]", style="bold")
            controls.append(" back", style="dim")

        content = Text()
        content.append_text(player)
        if time.time() < self._toast_until:
            content.append(f"\n   {self._toast}", style="bold green")

        if self._mini_player:
            # Tiny mode: just the player line, no controls
            if self._quit_pending:
                content.append_text(self._build_quit_confirm())
            return Panel(
                content,
                title="[bold cyan]Ticli[/bold cyan]",
                border_style="cyan",
                padding=(0, 1),
            )

        if self._mode != self.MODE_PLAYER:
            content.append("\n\n")
            content.append("  " + "\u2500" * 56, style="dim")
            content.append("\n\n")
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
            elif self._mode == self.MODE_DOWNLOAD:
                content.append_text(self._build_download_display())

        if self._quit_pending:
            content.append_text(self._build_quit_confirm())
        elif self._logout_pending:
            content.append_text(self._build_logout_confirm())
        elif self._disable_songs_pending:
            content.append_text(self._build_disable_songs_confirm())
        elif self._clear_cache_pending:
            content.append_text(self._build_clear_cache_confirm())

        content.append("\n\n")
        content.append_text(controls)

        return Panel(
            content,
            title="[bold cyan]Ticli[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )

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
            self._queue_cursor = state.get("cursor", 0)
        elif mode == self.MODE_PLAYLISTS:
            self._mode = self.MODE_PLAYLISTS
            self._playlists_cursor = state.get("cursor", 0)
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
        self._search_history = self._search_history[:20]

    @staticmethod
    def _search_split(page: int) -> tuple:
        """How many tracks / albums / artists make up one page of results.
        Keeps the old 50/30/20 feel, but scaled to "Songs per page" instead of
        a hardcoded 5/3/2 — one page of search is one page of rows, like every
        other list. Albums and artists never round away to nothing."""
        # Floor division, so the rounding always falls to tracks and songs stay
        # at least half the page even at the 5-row minimum
        albums = max(1, page * 3 // 10)
        artists = max(1, page * 2 // 10)
        return max(1, page - albums - artists), albums, artists

    def _search_kinds(self, scope: Optional[str] = None) -> tuple:
        """Which of the reservoir's categories a scope shows."""
        return self.SEARCH_FILTER_KINDS.get(scope or self._search_filter, ())

    @staticmethod
    def _search_models() -> list:
        """What a fetch asks TIDAL for — always all three categories, whatever
        scope asked for it.

        `limit` is applied per type, so this is the same single request either
        way; the payload is bigger and every other scope is then answered out
        of it for free. That is the whole trade: one request buys the query,
        not the scope, so Tab can apply immediately without costing anything.
        """
        return [tidalapi.Track, tidalapi.Album, tidalapi.Artist]

    def _search_row(self, kind: str, obj) -> dict:
        """One result row. Everything downstream reads these, not the objects."""
        if kind == "tracks":
            artist = obj.artists[0].name if obj.artists else ""
            return {"type": "track", "name": obj.name, "artist": artist, "obj": obj}
        if kind == "albums":
            artist = obj.artist.name if obj.artist else ""
            return {"type": "album", "name": obj.name, "artist": artist, "obj": obj}
        return {"type": "artist", "name": obj.name, "artist": "", "obj": obj}

    # ── The per-scope views over one query's reservoir ──

    def _search_view(self, scope: Optional[str] = None) -> dict:
        """The view for a scope, or an empty one if it has never been shown.
        Presence in `_search_views` — not any flag inside the record — is the
        answer to "has this scope been applied to this query", exactly as
        `_artist_sections` answers it for the artist page's tabs."""
        return self._search_views.get(scope or self._search_filter) or _empty_search_view()

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
        50/30/20 split, still exactly `page` rows. Reads the reservoir, never
        touches it — two scopes can be at two depths in the same rows.
        """
        reservoir = self._search_reservoir
        kinds = ("tracks", "albums", "artists")
        avail = {kind: len(reservoir[kind]) - consumed.get(kind, 0) for kind in kinds}
        if scope == "all":
            n_tracks, n_albums, n_artists = self._search_split(page)
            # A query with one album shouldn't waste the other album rows —
            # hand the shortfall to tracks, which is what you searched for
            n_albums = min(n_albums, avail["albums"])
            n_artists = min(n_artists, avail["artists"])
            n_tracks = min(page - n_albums - n_artists, avail["tracks"])
            counts = {"tracks": n_tracks, "albums": n_albums, "artists": n_artists}
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
                         for kind in ("tracks", "albums", "artists")}
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

    # ── Downloads ──

    def _open_download(self):
        """Open the download screen for whatever [d] was pressed on.

        The same target rule the add-to-playlist picker uses, so [d] and [y]
        never disagree about which track the screen you are looking at means.
        The cursor is pre-placed on the tier settings is set to: that is the
        tier this session is already listening at, so Enter twice is the
        answer for the common case and the other three are one arrow away.
        """
        track = self._target_track_for_picker()
        if track is None:
            self._set_toast("No track selected")
            return
        self._push_nav()
        self._mode = self.MODE_DOWNLOAD
        self._mini_player = False
        self._download_track = track
        try:
            self._download_cursor = QUALITY_CHOICES.index(self._quality_name)
        except ValueError:
            self._download_cursor = 0
        # A job for a different track is finished business; one for this track
        # is what the screen should still be reporting on
        job = self._download_job
        running = bool(job) and job.get("state") == "running"
        if job and job.get("track_id") != getattr(track, "id", None):
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
        job = self._download_job
        if job and job.get("state") == "running":
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
            final = None
            try:
                real = self._resolve_track(track)
                if real is None:
                    raise RuntimeError("track could not be resolved")
                meta = downloads.track_metadata(real)
                url = self._download_stream_url(real, tier)
                sources = stream_sources(url)
                if not sources:
                    raise RuntimeError("stream named nothing to fetch")
                # The extension is not known until the CDN answers, so the
                # file is written under a provisional name and moved once the
                # container has identified itself
                staging = downloads.download_dir() / f".ticli-{track_id}{downloads.PART_SUFFIX}"
                staging.parent.mkdir(parents=True, exist_ok=True)
                last = [0.0]

                def _progress(done, total):
                    # Cheap enough to call per chunk, but a repaint per chunk
                    # is not — one every quarter second is already faster than
                    # the eye and slower than the monitor's own tick
                    now = time.time()
                    if now - last[0] < 0.25:
                        return
                    last[0] = now
                    _update(done=done, total=total)

                ext = fetch_to_file(
                    sources, str(staging),
                    abandoned=lambda: self._download_job_gen != gen,
                    progress=_progress)
                final = downloads.destination(meta, ext)
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, final)
                written = tags.write_tags(final, meta, self._cover_bytes(real))
                size = final.stat().st_size
                downloads.record(track_id, final.relative_to(downloads.download_dir()),
                                 tier, size)
                if self._download_job_gen != gen:
                    return
                _update(state="done", path=str(final), tags=written,
                        done=size, total=size)
                self._set_toast(f"Downloaded to {final.parent}")
            except _DownloadSuperseded:
                self._discard_staging(track_id)
                if final is not None:
                    downloads.discard_scratch(final)
            except Exception as e:
                logger.debug("Download failed: %s", e)
                self._discard_staging(track_id)
                if final is not None:
                    downloads.discard_scratch(final)
                if self._download_job_gen == gen:
                    _update(state="failed", error=str(e)[:PLAYER_ERROR_CHARS])
            finally:
                self._wake()

        threading.Thread(target=_run, daemon=True).start()

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

    def _download_stream_url(self, track, tier: str) -> str:
        """A stream URL for this track at a tier that may not be the one
        playing. Network — callers are already off the UI thread.

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
            return self._stream_url(track)
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
        self._picker_cursor = 0
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
                if added:
                    self._set_toast(f'Added to "{playlist.name}"')
                else:
                    self._set_toast(f'Already in "{playlist.name}"')
            except Exception:
                self._set_toast("Failed to add to playlist")
            finally:
                self._picker_busy = False

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
        spec = SETTINGS_SPEC[self._settings_cursor]
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
        if SETTINGS_SPEC[self._settings_cursor]["kind"] != "int":
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
        spec = SETTINGS_SPEC[self._settings_cursor]
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
        elif key == "progress_bar_width":
            self._bar_width = value
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

        if self._handle_focus_key(key):
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
        elif self._mode == self.MODE_DOWNLOAD:
            self._handle_download_key(key)
        elif self._mode == self.MODE_SETTINGS:
            self._handle_settings_key(key)
        else:
            self._handle_player_key(key)

    def _focus_player(self):
        """Give the player the arrow keys. ↑ is what asks for it: from the top
        of a list, where ↑ had nowhere left to go, and from the player screen,
        where it did nothing at all. Pointless with no track to scrub."""
        if self._current_track is not None:
            self._player_focus = True

    def _handle_focus_key(self, key: str) -> bool:
        """Keys that belong to the player while it has focus. True when the key
        was used up here.

        This is the whole of the seeking interaction, and it is one rule in one
        place rather than a branch per screen: focused, ←/→ scrub and ↓ (or
        Esc) hands the arrows back. Anything else — typing a query, [t], Enter —
        means the user is done scrubbing, so focus drops and the key goes on to
        the screen it was meant for. Nothing is stolen: unfocused, every key
        means exactly what it always did, including ←/→ for prev/next.
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
            # one you last tabbed to half an hour ago
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
            # The song count could have moved since the page was last open
            self._cache.invalidate_audio_count()
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
        if key == KEY_UP:
            if self._search_results and self._search_cursor > 0:
                self._search_cursor -= 1
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
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            if self._search_results:
                self._select_search_result()
            elif key != KEY_RIGHT:
                self._do_search()
        elif key in (KEY_BACKSPACE, KEY_BACKSPACE2):
            self._search_query = self._search_query[:-1]
            self._reset_search_results()
        elif len(key) == 1 and key.isprintable():
            self._search_query += key
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
        if key == KEY_ESC or key == KEY_LEFT:
            self._go_back()
            return
        if key == " ":
            self._toggle_play_key()
            return
        if key == KEY_UP:
            if self._editable_playlists:
                self._picker_cursor = max(0, self._picker_cursor - 1)
        elif key == KEY_DOWN:
            if self._editable_playlists:
                self._picker_cursor = min(len(self._editable_playlists) - 1, self._picker_cursor + 1)
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            if self._editable_playlists and self._picker_cursor < len(self._editable_playlists):
                self._picker_add_to(self._editable_playlists[self._picker_cursor])

    def _handle_download_key(self, key: str):
        """One column, so ↑/↓ is the whole navigation and Enter is the whole
        action. ↑ at the top does *not* hand the arrows to the player here, the
        same as the add-to-playlist picker: this is a chooser you came to on
        purpose and leave on purpose, not a list you were browsing."""
        if key == KEY_ESC or key == KEY_LEFT:
            self._go_back()
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
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            self._start_download_job(self._download_tier())

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
            self._mode = self.MODE_PLAYER
            return
        if key == " ":
            self._toggle_play_key()
            return
        if key in ("o", "O"):
            self._logout_pending = True
            return
        # Clearing the cache is an action, not a value, so it lives outside
        # SETTINGS_SPEC as a keybinding — the same reason logout does
        if key in ("x", "X"):
            self._clear_cache_pending = True
            return
        # Deliberately not "p" — that already opens playlists on the player
        # screen, and one letter meaning two things is how muscle memory breaks
        if key in ("u", "U"):
            self._upgrade_to_pkce()
            return
        if key == KEY_UP:
            self._settings_cursor = max(0, self._settings_cursor - 1)
        elif key == KEY_DOWN:
            self._settings_cursor = min(len(SETTINGS_SPEC) - 1, self._settings_cursor + 1)
        elif key == KEY_LEFT:
            self._change_setting(-1)
        elif key in (KEY_RIGHT, KEY_ENTER, KEY_ENTER2):
            self._change_setting(1)

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
    import click

    @click.command()
    @click.option("--quality", default=None, type=click.Choice(["LOW", "HIGH", "LOSSLESS", "HIRES"], case_sensitive=False), help="Audio quality for this run (overrides the saved setting)")
    @click.option("--login-flow", default=None, type=click.Choice(["device", "pkce"], case_sensitive=False), help="How to log in when there is no saved session")
    def headless(quality, login_flow):
        """Launch Ticli terminal player."""
        HeadlessTidalPlayer(quality=quality, login_flow=login_flow).run()

    headless()


if __name__ == "__main__":
    main()
