"""Downloads: the user-owned tier.

The cache (utils/cache.py) is machine-owned and disposable — deleting it at
any moment is safe, and a budget evicts from it without asking. Downloads are
the opposite of all three. They live in the user's own music folder, they are
never evicted, they are never counted against the cache budget, and nothing in
ticli ever deletes a finished one. A track someone deliberately downloaded must
not lose a fight to a track that happened to play twice.

That separation is structural rather than a policy flag: `cache.enforce_budget`
and `cache.clear_audio` both work from `owned_audio_files()`, which lists
`audio_dir()` and nothing else, so a file under `~/Music/Ticli` is not
reachable from either. The test suite pins that rather than trusting it.

**Layout.** `~/Music/Ticli/<Artist>/<Album>/<NN> <Title><ext>` — the same shape
every music player already understands. This is not decoration: without an
embedded-tag dependency the path *is* a large fraction of the metadata, and a
player that reads no tags at all still shows the right thing. Tags are written
too (see utils/tags.py); the path is what survives when they cannot be.

**Size estimates cost nothing.** Duration is already in the metadata index, so
`duration x nominal bitrate` gives every tier at once with no request at all.
The alternative — one `playbackinfo` per track for a real `Content-Length` —
is exactly the pattern that got the owner's IP blocked (see ai/INCIDENTS #1),
and it takes the user's playback down with it. Everything shown is prefixed
`~` and the accuracy of each tier is stated where it is shown, because the
four tiers are not equally knowable: AAC is near-constant-bitrate and lands
within a couple of percent, FLAC is variable and does not.

**Manual deletion is expected, not an error.** The index below is advisory —
every read stats the file, so a track the user dragged to the trash is simply
not downloaded, and downloading it again is the ordinary path.
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from ticli.utils import cache as cache_mod

logger = logging.getLogger(__name__)

INDEX_VERSION = 1

# Set to a Path to override where downloads go. Tests point it at tmp_path;
# nothing in the app writes it, so the answer is stable for a run.
DOWNLOAD_ROOT = None

# What each tier is worth per second, in bits. Two of these are measured and
# two are arithmetic, and the difference is stated in the interface rather
# than smoothed over:
#
#   LOW / HIGH  — AAC, effectively constant bitrate. Estimating from the
#     nominal rate under-reads the real file by 0.3-2.1% (four real TIDAL
#     tracks, ai/reference/download-research-2026-07-25.md §1.4) and by 0.67%
#     at 320k / 3.4% at 96k when re-measured locally against ffmpeg's encoder.
#     One-signed and small: the estimate is never high.
#
#   LOSSLESS    — FLAC 16/44.1. PCM is 1411.2 kbps and FLAC keeps roughly 60%
#     of that on commercial material, so 850 kbps. Measured on a real PKCE
#     track from this machine's own cache (24/88.2 FLAC, 124 s), transcoded
#     locally to 16/44.1: the whole track came to 765 kbps (54% of PCM), while
#     twelve-second windows of it ranged 502-852 kbps (36-60%) — the quiet
#     intro is the low end and the body of the song is the high one. So the
#     honest bound is roughly +/-20% on ordinary material and up to a third
#     high on sparse or quiet recordings. Not the +/-1% AAC gets.
#
#   HIRES       — FLAC above CD, and the master's resolution is not known
#     until the stream is fetched: 24/44.1, 24/96 and 24/192 differ by 4x.
#     2500 kbps is 24/96 at the same ~52% ratio the one measured hi-res track
#     showed (2200 kbps at 24/88.2). Treat it as an order of magnitude.
NOMINAL_BITRATE = {
    "LOW": 96_000,
    "HIGH": 320_000,
    "LOSSLESS": 850_000,
    "HIRES": 2_500_000,
}

# One line per tier, shown under the picker for whichever tier is hovered.
# Says what the `~` is worth, in the tier's own terms.
ESTIMATE_ACCURACY = {
    "LOW": "AAC is near-constant bitrate — measured within 3%, and always a little low.",
    "HIGH": "AAC is near-constant bitrate — measured within 2%, and always a little low.",
    "LOSSLESS": "FLAC is variable — usually within 20%, and up to a third high on quiet records.",
    "HIRES": "The master's resolution isn't known until it's fetched — this is a rough guide only.",
}

# Only ever unlinked by name, and only ever ones this module wrote. A finished
# download is never deleted by ticli at all.
PART_SUFFIX = ".part"
TAGGING_SUFFIX = ".tagging"

# Filesystem limits are per-component; 90 leaves room for " (2)" and the
# extension inside every common 255-byte limit even in UTF-8.
MAX_COMPONENT = 90

# Characters no filesystem in play here should be asked to hold. The colon is
# in the list for macOS's benefit (Finder still shows it as a slash), and the
# Windows set is there because a downloads folder gets synced.
_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def _default_download_root() -> Path:
    """`~/Music/Ticli`, or the user's own music folder if XDG names one.

    Linux users move ~/Music; `user-dirs.dirs` is where that is recorded, and
    reading it is a plain file read with no dependency. Anything unparseable
    falls back rather than failing — a download folder is not worth an error.
    """
    home = Path.home()
    music = home / "Music"
    if sys.platform not in ("darwin", "win32"):
        configured = os.environ.get("XDG_MUSIC_DIR")
        if not configured:
            try:
                text = (Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
                        / "user-dirs.dirs").read_text()
                for line in text.splitlines():
                    if line.startswith("XDG_MUSIC_DIR="):
                        configured = line.split("=", 1)[1].strip().strip('"')
                        break
            except OSError:
                configured = None
        if configured:
            music = Path(os.path.expandvars(configured.replace("$HOME", str(home))))
    return music / "Ticli"


def download_dir() -> Path:
    """Where downloads go. Read at call time so a test can redirect it."""
    return Path(DOWNLOAD_ROOT) if DOWNLOAD_ROOT else _default_download_root()


def display_dir() -> str:
    """The download folder the way a person says it — `~/Music/Ticli`. The
    settings page and the download screen both show a path, and an absolute
    one wraps onto two lines for no information."""
    path = download_dir()
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def index_file() -> Path:
    """The download index — in the *cache* directory, not in the music folder.

    It is machine-owned bookkeeping about user-owned files: losing it costs a
    re-download and nothing else, and the user's music folder stays a folder
    of music rather than a folder of music and a JSON file.
    """
    return cache_mod.CACHE_DIR / "downloads.json"


# ── names on disk ──


def safe_component(text, fallback: str = "Unknown") -> str:
    """One path component from a piece of metadata.

    Deliberately conservative: an artist called `AC/DC` must not become two
    directories, and a title ending in a dot or a space is a file Windows
    cannot open and macOS quietly renames.
    """
    cleaned = _UNSAFE.sub("_", str(text or "")).strip()
    cleaned = cleaned.rstrip(". ")
    if not cleaned or cleaned in (".", ".."):
        return fallback
    if len(cleaned) > MAX_COMPONENT:
        cleaned = cleaned[:MAX_COMPONENT].rstrip(". ") or fallback
    return cleaned


def track_metadata(track) -> dict:
    """Everything a downloaded file's name and tags come from.

    All of it is already on a Track that has been fetched, so this costs no
    request. Missing pieces are simply absent — a single released without an
    album still has a title and an artist, and that is enough.
    """
    artists = [a.name for a in (getattr(track, "artists", None) or [])
               if getattr(a, "name", None)]
    album = getattr(track, "album", None)
    album_artist = getattr(album, "artist", None) if album is not None else None
    date = None
    released = getattr(album, "release_date", None) if album is not None else None
    if released is not None:
        date = str(getattr(released, "year", None) or released)[:10]
    if not date and album is not None and getattr(album, "year", None):
        date = str(album.year)
    return {
        "id": getattr(track, "id", None),
        "title": getattr(track, "name", None) or "",
        "artist": ", ".join(artists),
        "album_artist": getattr(album_artist, "name", None) or (artists[0] if artists else ""),
        "album": (getattr(album, "name", None) or "") if album is not None else "",
        "track_num": getattr(track, "track_num", None) or 0,
        "track_total": (getattr(album, "num_tracks", None) or 0) if album is not None else 0,
        "disc_num": getattr(track, "volume_num", None) or 0,
        "disc_total": (getattr(album, "num_volumes", None) or 0) if album is not None else 0,
        "date": date or "",
        "copyright": getattr(track, "copyright", None) or "",
        "isrc": getattr(track, "isrc", None) or "",
        "duration": getattr(track, "duration", None) or 0,
    }


def relative_path(meta: dict, ext: str) -> Path:
    """`<Album artist>/<Album>/<NN> <Title><ext>`.

    Album artist rather than track artist, so a compilation stays one folder
    instead of one folder per featured guest. The track number is zero-padded
    because that is the only thing making a plain alphabetical listing play in
    album order, and it is left off entirely when TIDAL didn't give one rather
    than written as a misleading `00`.
    """
    artist = safe_component(meta.get("album_artist") or meta.get("artist"),
                            "Unknown Artist")
    album = safe_component(meta.get("album"), "Unknown Album")
    title = safe_component(meta.get("title"), f"Track {meta.get('id') or ''}".strip())
    number = int(meta.get("track_num") or 0)
    name = f"{number:02d} {title}" if number else title
    return Path(artist) / album / f"{name}{ext}"


def destination(meta: dict, ext: str) -> Path:
    return download_dir() / relative_path(meta, ext)


# ── the index ──


def load_index() -> dict:
    """Track id (as a string) → record. Missing or corrupt reads as empty."""
    try:
        data = json.loads(index_file().read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
        return {}
    tracks = data.get("tracks")
    return tracks if isinstance(tracks, dict) else {}


def _save_index(tracks: dict) -> None:
    try:
        cache_mod.CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = index_file()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": INDEX_VERSION, "tracks": tracks}))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as e:
        logger.debug("Could not write the download index: %s", e)


def record(track_id, relpath: Path, quality: str, size: int) -> None:
    """Remember a finished download. Whole-dict replacement, no locks."""
    if track_id is None:
        return
    tracks = dict(load_index())
    tracks[str(track_id)] = {
        "path": str(relpath), "quality": quality, "bytes": int(size),
        "at": time.time(),
    }
    _save_index(tracks)


def _entry_path(entry):
    """The file an index record points at, if it is really there.

    Split out of `path_for` because the whole-folder readouts need it without
    paying for another `load_index()` each — see `usage()`.
    """
    if not isinstance(entry, dict):
        return None
    relative = entry.get("path")
    if not isinstance(relative, str) or not relative:
        return None
    path = download_dir() / relative
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def path_for(track_id):
    """The downloaded file for this track, or None.

    Stats the file every time. The index is a hint about where to look, never
    an answer about what exists — which is the whole of "manual deletion is
    handled durably": a file the user removed is not downloaded, and the next
    thing that wants it fetches it.
    """
    if track_id is None:
        return None
    return _entry_path(load_index().get(str(track_id)))


def usage() -> tuple:
    """`(how many downloads are really on disk, what they cost)`.

    **One** index read for both numbers, and one `stat` per entry. The two
    readouts used to call `path_for` per track, and `path_for` re-read and
    re-parsed the whole JSON file every call — so a settings-page repaint was
    2(N+1) reads and 2(N+1) parses of an O(N)-sized file:

        50 downloads     4.54 ms per repaint
        200 downloads   42.70 ms
        500 downloads  229.48 ms

    paid **twice a second** while the page is open, because `_repaint` builds
    the display before deciding whether the frame changed. That is the same
    order as the 231 ms keypress latency `auto_refresh=False` existed to
    remove (bd4f95f), on the UI thread, in a new place.

    The semantics are unchanged and deliberately so: every file is still
    stat-ed, and the index is still only a hint about where to look. Kept
    pure — the remembering is the caller's, on the player instance, because
    module-level memo state would outlive a test and leak into the next one.
    """
    count = 0
    total = 0
    for entry in load_index().values():
        path = _entry_path(entry)
        if path is None:
            continue
        count += 1
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return count, total


def downloaded_count() -> int:
    """How many recorded downloads are really on disk."""
    return usage()[0]


def total_bytes() -> int:
    """What the download folder is costing, from the files that are there."""
    return usage()[1]


# ── estimates ──


def estimate_bytes(duration, tier: str) -> int:
    """Bytes a track of this length is likely to be at this tier. No network.

    Zero when the duration isn't known, which reads as "can't say" rather than
    as "empty file" — the caller shows a dash.
    """
    try:
        seconds = float(duration or 0)
    except (TypeError, ValueError):
        return 0
    rate = NOMINAL_BITRATE.get(str(tier).upper())
    if seconds <= 0 or not rate:
        return 0
    return int(seconds * rate / 8)


def format_bytes(num: int) -> str:
    """A size the way the download screen says it. Always prefixed `~` by the
    caller — this only picks the unit."""
    if num <= 0:
        return "—"
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if num >= scale:
            return f"{num / scale:.1f} {unit}"
    return f"{num} B"


# ── the files this module owns ──


def scratch_files(final: Path) -> list:
    """The half-written companions of a destination, by exact name.

    The only files ticli ever deletes under the music folder. A finished
    download is never removed by anything here, and no directory ever is —
    `~/Music` belongs to somebody.
    """
    return [final.with_name(final.name + PART_SUFFIX),
            final.with_name(final.name + TAGGING_SUFFIX)]


def discard_scratch(final: Path) -> None:
    for path in scratch_files(final):
        try:
            path.unlink()
        except OSError:
            pass
