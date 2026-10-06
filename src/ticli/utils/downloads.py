"""Downloads: the user-owned tier (`~/Music/Ticli/<Artist>/<Album>/<NN> <Title><ext>`).

The cache (utils/cache.py) is machine-owned and disposable. Downloads are the
opposite: never evicted, never counted against the cache budget, and nothing
removes one except `remove()`, one track at a time, after the user confirmed.

- `remove()` deletes only the exact path recorded in the index. Never a glob,
  sweep, budget or background thread; a file ticli did not record is unreachable.
- No directory is ever removed (`~/Music` belongs to somebody).
- `cache.enforce_budget` and `cache.clear_audio` work from `owned_audio_files()`,
  which lists `audio_dir()` only, so they can never reach a download.

The path is a large part of the metadata (tags are written too, see
utils/tags.py, but the path survives when they cannot be).

Size estimates cost no request: duration x nominal bitrate. One `playbackinfo`
per track for a real Content-Length is the pattern that got the owner's IP
blocked (docs/adr/0001-tidal-rate-limits.md). Estimates are prefixed `~`; a size
without one is a byte count already on disk.

The index is advisory: every read stats the file, so a track the user trashed
is simply not downloaded.
"""

import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

from ticli.utils import cache as cache_mod

logger = logging.getLogger(__name__)

# `load_index` reads a file with a different version as empty, so bumping this
# forgets the user's whole download library. Translate old values at read time
# instead (precedent: QUALITY_V4_RENAMES).
INDEX_VERSION = 1

# Set to a Path to override where downloads go (tests point it at tmp_path).
DOWNLOAD_ROOT = None

# Bits per second per tier. LOW/MEDIUM are AAC (near-constant bitrate; the
# nominal rate under-reads real files by 0.3-3.4%, never over). HIGH is FLAC
# 16/44.1: measured 765 kbps for a whole track, 502-852 kbps per 12 s window,
# so roughly +/-20%. MAX is FLAC above CD, whose resolution is unknown until the
# stream is fetched (24/44.1 vs 24/192 differ 4x): 2500 kbps is 24/96, an
# order of magnitude only.
NOMINAL_BITRATE = {
    "LOW": 96_000,
    "MEDIUM": 320_000,
    "HIGH": 850_000,
    "MAX": 2_500_000,
}

# Unlinked by name, whenever the download they belong to is abandoned.
PART_SUFFIX = ".part"
TAGGING_SUFFIX = ".tagging"

# 90 leaves room for " (2)" and the extension inside a 255-byte limit in UTF-8.
MAX_COMPONENT = 90

# The colon is for macOS (Finder shows it as a slash); the Windows set is there
# because a downloads folder gets synced.
_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def _default_download_root() -> Path:
    """`~/Music/Ticli`, or the folder XDG `user-dirs.dirs` names on Linux."""
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
    return Path(DOWNLOAD_ROOT) if DOWNLOAD_ROOT else _default_download_root()


def display_dir() -> str:
    """Absolute download path. `expanduser()`, deliberately not `resolve()`: on
    macOS `resolve()` rewrites /Users/x to /System/Volumes/Data/Users/x."""
    return str(download_dir().expanduser())


def index_file() -> Path:
    """In the cache directory, not the music folder: losing it costs a re-download."""
    return cache_mod.CACHE_DIR / "downloads.json"


# ── names on disk ──


def safe_component(text, fallback: str = "Unknown") -> str:
    """One path component from metadata: `AC/DC` must not become two directories,
    and a trailing dot or space is unopenable on Windows."""
    cleaned = _UNSAFE.sub("_", str(text or "")).strip().rstrip(". ")
    if not cleaned or cleaned in (".", ".."):
        return fallback
    if len(cleaned) > MAX_COMPONENT:
        cleaned = cleaned[:MAX_COMPONENT].rstrip(". ") or fallback
    return cleaned


def track_metadata(track) -> dict:
    """Everything a downloaded file's name and tags come from. No request."""
    artists = [a.name for a in (getattr(track, "artists", None) or [])
               if getattr(a, "name", None)]
    album = getattr(track, "album", None)
    album_artist = getattr(album, "artist", None)
    released = getattr(album, "release_date", None)
    date = None
    if released is not None:
        date = str(getattr(released, "year", None) or released)[:10]
    if not date and getattr(album, "year", None):
        date = str(album.year)
    return {
        "id": getattr(track, "id", None),
        "title": getattr(track, "name", None) or "",
        "artist": ", ".join(artists),
        "album_artist": getattr(album_artist, "name", None) or (artists[0] if artists else ""),
        "album": getattr(album, "name", None) or "",
        "track_num": getattr(track, "track_num", None) or 0,
        "track_total": getattr(album, "num_tracks", None) or 0,
        "disc_num": getattr(track, "volume_num", None) or 0,
        "disc_total": getattr(album, "num_volumes", None) or 0,
        "date": date or "",
        "copyright": getattr(track, "copyright", None) or "",
        "isrc": getattr(track, "isrc", None) or "",
        "duration": getattr(track, "duration", None) or 0,
    }


def relative_path(meta: dict, ext: str) -> Path:
    """`<Album artist>/<Album>/<NN> <Title><ext>`. Album artist keeps a
    compilation in one folder; the zero-padded number is left off when TIDAL
    gave none rather than written as a misleading `00`."""
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

# Serializes `record` (bulk runner's writer thread) and `remove` (UI thread).
# Held only inside `_mutate_index`, across one whole load-modify-save cycle,
# taken exactly once: a leaf lock like cache.py's `_tracker_lock`.
_index_lock = threading.Lock()


def load_index() -> dict:
    """Track id (as a string) -> record. Missing or corrupt reads as empty."""
    try:
        data = json.loads(index_file().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
        return {}
    tracks = data.get("tracks")
    return tracks if isinstance(tracks, dict) else {}


def _save_index(tracks: dict) -> None:
    # Temp + `os.replace`: the file is replaced whole, which is what keeps the
    # lock-free readers correct.
    try:
        cache_mod.CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = index_file()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": INDEX_VERSION, "tracks": tracks}))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as e:
        logger.debug("Could not write the download index: %s", e)


def _mutate_index(change) -> None:
    """Load the index under `_index_lock`, let `change` edit a private copy,
    save it. The lock covers the whole cycle, the read included: a stale read
    erases the other writer's update. `change` returns False for "nothing
    moved", which skips the write. Nothing reachable from `change` may take
    another lock. Readers stay lock-free: every save replaces the file whole.
    """
    with _index_lock:
        tracks = dict(load_index())
        if change(tracks) is False:
            return
        _save_index(tracks)


def record(track_id, relpath: Path, quality: str, size: int,
           granted=None) -> None:
    """Remember a finished download.

    `quality` is the tier the user asked for (settings spelling; older entries
    hold pre-rename names and are translated at display time). `granted` is the
    tier TIDAL served, in tidalapi's spelling: a device-flow session asks for
    hi-res and gets 320k AAC, and recording the request would misdescribe the
    file for ever.
    """
    if track_id is None:
        return

    def change(tracks):
        tracks[str(track_id)] = {
            "path": str(relpath), "quality": quality, "granted": granted,
            "bytes": int(size), "at": time.time(),
        }

    _mutate_index(change)


def _entry_path(entry):
    """The file an index record points at, if it is really there."""
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
    """The downloaded file for this track, or None. Stats the file every time;
    the index is a hint about where to look, never an answer about what exists."""
    if track_id is None:
        return None
    return _entry_path(load_index().get(str(track_id)))


def present() -> list:
    """Every recorded download that is really on disk, newest first.

    The one walk of the index: the settings readouts, track-row markers and the
    downloads list all derive from it, so they cost one index read and one
    `stat` per entry. Per-track `path_for` calls re-parsed the whole file each
    time (200 downloads: 42.7 ms per repaint, twice a second). Pure; callers
    memoize on the player instance.

    Each row is `{"id", "path", "entry", "bytes"}`; `bytes` is the size on disk.
    """
    rows = []
    for track_id, entry in load_index().items():
        path = _entry_path(entry)
        if path is None:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        rows.append({"id": str(track_id), "path": path, "entry": entry,
                     "bytes": size})
    rows.sort(key=lambda row: row["entry"].get("at") or 0, reverse=True)
    return rows


def usage() -> tuple:
    """`(how many downloads are really on disk, what they cost)`."""
    rows = present()
    return len(rows), sum(row["bytes"] for row in rows)


def downloaded_count() -> int:
    return usage()[0]


def total_bytes() -> int:
    return usage()[1]


def describe(relpath) -> tuple:
    """`(title, artist, album)` read back out of the path `relative_path` wrote."""
    parts = Path(relpath).parts
    stem = Path(relpath).stem
    # Our track number is exactly two digits and a space, so stripping it
    # cannot eat a title that merely begins with a number.
    match = re.match(r"^\d{2} (.+)$", stem)
    title = match.group(1) if match else stem
    artist = parts[0] if len(parts) >= 3 else ""
    album = parts[1] if len(parts) >= 3 else ""
    return title, artist, album


def remove(track_id) -> bool:
    """Delete one downloaded file and forget it. True if the row is gone.

    A row whose file the user already deleted is not an error (the row goes,
    True). A file that is there but cannot be unlinked leaves the row alone.
    The directory is left behind even when empty. Pop, unlink and save all run
    inside one `_mutate_index` cycle, so a racing `record` can neither
    resurrect the row nor have its own erased.
    """
    key = str(track_id)
    outcome = {"removed": False}

    def change(tracks):
        entry = tracks.pop(key, None)
        if entry is None:
            return False
        path = _entry_path(entry)
        if path is not None:
            try:
                path.unlink()
            except OSError as e:
                logger.debug("Could not delete the download %s: %s", path, e)
                return False
        outcome["removed"] = True

    _mutate_index(change)
    return outcome["removed"]


# ── estimates ──


def estimate_bytes(duration, tier: str) -> int:
    """Bytes a track of this length is likely to be at this tier. No network.
    Zero (shown as a dash) when the duration isn't known."""
    try:
        seconds = float(duration or 0)
    except (TypeError, ValueError):
        return 0
    rate = NOMINAL_BITRATE.get(str(tier).upper())
    if seconds <= 0 or not rate:
        return 0
    return int(seconds * rate / 8)


def format_bytes(num: int) -> str:
    """A size the way the download screen says it (callers add the `~`)."""
    if num <= 0:
        return "—"
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if num >= scale:
            return f"{num / scale:.1f} {unit}"
    return f"{num} B"


# ── the files this module owns ──


def scratch_files(final: Path) -> list:
    """The half-written companions of a destination, by exact name."""
    return [final.with_name(final.name + PART_SUFFIX),
            final.with_name(final.name + TAGGING_SUFFIX)]


def discard_scratch(final: Path) -> None:
    for path in scratch_files(final):
        try:
            path.unlink()
        except OSError:
            pass
