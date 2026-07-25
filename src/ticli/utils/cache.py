"""On-disk cache for Ticli.

Two things live here, because they share one budget and one directory:

* a **metadata index** — the playlists you own and the tracks in them, as
  plain records. It exists so opening "Your Playlists" paints instantly
  instead of waiting on a TIDAL round trip.
* an **audio directory** — whole tracks, only when the cache setting asks
  for them. Playback writes it (see AudioPlayer), this module only sizes
  and evicts it.

Deliberately not in `~/.config/ticli`: config is user-owned and precious,
this is machine-owned and disposable. Deleting the whole cache directory at
any moment is always safe — the app just gets slow again for one visit.
The location follows each OS's own convention (XDG on Linux, ~/Library/Caches
on macOS, %LOCALAPPDATA% on Windows) rather than one hardcoded path.

Freshness policy: the cache is a *first paint*, never an answer. Every read
is paired with a live fetch by the caller, and the live result replaces what
was shown as soon as it lands — so a playlist edited on your phone is wrong
on screen for exactly as long as one network request takes, which is the
same time you'd otherwise have spent staring at "Loading...". MAX_AGE only
bounds the pathological case (offline for a month), where showing a year-old
list would be worse than showing nothing.

The index is deliberately record-shaped, not object-shaped: every track
carries its name, artists and album as text, so a later "search my
playlists" can scan the whole index locally without touching the network.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_VERSION = 1

# Cache modes, cheapest first. Rows in SETTINGS_SPEC; meaning enforced here.
MODE_OFF = "OFF"
MODE_METADATA = "METADATA"
MODE_FULL = "FULL"

# Beyond this an entry is treated as absent. Only reachable by being offline
# (or not opening a playlist) for a month — every visit rewrites the entry.
MAX_AGE_SECONDS = 30 * 24 * 3600

# Index keys. Flat strings so the file stays greppable by hand.
KEY_PLAYLISTS = "playlists"


def _default_cache_dir() -> Path:
    """The OS's own cache location, not a hardcoded one."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "ticli" / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ticli"
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "ticli"


CACHE_DIR = _default_cache_dir()


def index_file() -> Path:
    """Read CACHE_DIR at call time so tests can redirect the whole cache."""
    return CACHE_DIR / "metadata.json"


def audio_dir() -> Path:
    return CACHE_DIR / "audio"


# ── Record shims ──
#
# What comes back out of the cache is not a tidalapi object and never
# pretends to be one: it carries exactly the fields the list views render,
# and an id to resolve with when the user acts on it. Anything that needs
# the real thing (a stream URL, a playlist edit) resolves through the
# session first — see HeadlessTidalPlayer._resolve_track.


class _Named:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name


class CachedTrack:
    """A track as far as a list row is concerned."""

    cached = True
    __slots__ = ("id", "name", "duration", "artists", "album")

    def __init__(self, record: dict):
        self.id = record.get("id")
        self.name = record.get("name") or "?"
        self.duration = record.get("duration") or 0
        self.artists = [_Named(n) for n in record.get("artists") or []]
        album = record.get("album")
        self.album = _Named(album) if album else None


class CachedPlaylist:
    """A playlist as far as the playlists list is concerned."""

    cached = True
    __slots__ = ("id", "name", "num_tracks", "creator", "editable")

    def __init__(self, record: dict):
        self.id = record.get("id")
        self.name = record.get("name") or "?"
        self.num_tracks = record.get("num_tracks") or 0
        creator = record.get("creator")
        self.creator = _Named(creator) if creator else None
        self.editable = bool(record.get("editable"))


def track_record(track) -> dict:
    """Flatten a tidalapi Track into a stored record."""
    artists = [a.name for a in (getattr(track, "artists", None) or []) if getattr(a, "name", None)]
    album = getattr(track, "album", None)
    return {
        "id": getattr(track, "id", None),
        "name": getattr(track, "name", None),
        "duration": getattr(track, "duration", None),
        "artists": artists,
        "album": getattr(album, "name", None) if album else None,
    }


def playlist_record(playlist, editable: bool) -> dict:
    creator = getattr(playlist, "creator", None)
    return {
        "id": str(getattr(playlist, "id", "") or ""),
        "name": getattr(playlist, "name", None),
        "num_tracks": getattr(playlist, "num_tracks", None),
        "creator": getattr(creator, "name", None) if creator else None,
        "editable": bool(editable),
    }


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in path.iterdir():
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


class MetadataCache:
    """The cache the player talks to.

    No locks: the index is only ever replaced as a whole object, never
    mutated in place, so a background revalidation and a foreground read
    can never see a half-written dict.
    """

    def __init__(self, mode: str = MODE_METADATA, budget_mb: int = 1024):
        self.mode = mode
        self.budget_mb = budget_mb
        self._index = None  # loaded from disk on first use

    # ── plumbing ──

    @property
    def enabled(self) -> bool:
        return self.mode != MODE_OFF

    @property
    def keeps_audio(self) -> bool:
        return self.mode == MODE_FULL

    def _load(self) -> dict:
        """Read the index. Missing, corrupt or from a future version → empty,
        never raises. Same contract as load_config."""
        if self._index is not None:
            return self._index
        entries = {}
        path = index_file()
        try:
            if path.exists():
                data = json.loads(path.read_text())
                if isinstance(data, dict) and data.get("version") == CACHE_VERSION:
                    raw = data.get("entries")
                    if isinstance(raw, dict):
                        entries = {
                            k: v for k, v in raw.items()
                            if isinstance(v, dict) and isinstance(v.get("data"), list)
                        }
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as e:
            logger.debug("Unusable metadata cache, starting empty: %s", e)
            entries = {}
        self._index = entries
        return entries

    def _save(self, entries: dict) -> None:
        """Atomically replace the index, then bring the directory back inside
        its budget. Best effort — a cache that can't be written is a slow
        player, never a broken one."""
        self._index = entries
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = index_file()
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": CACHE_VERSION, "entries": entries}))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as e:
            logger.debug("Failed to write metadata cache: %s", e)
            return
        self.enforce_budget()

    # ── generic entry access ──

    def get(self, key: str):
        """Stored records for a key, or None if absent, stale or disabled."""
        if not self.enabled:
            return None
        entry = self._load().get(key)
        if not entry:
            return None
        fetched = entry.get("fetched") or 0
        if time.time() - fetched > MAX_AGE_SECONDS:
            return None
        return entry["data"]

    def put(self, key: str, records: list) -> None:
        if not self.enabled:
            return
        now = time.time()
        # Whole-dict replacement, so a reader never sees a partial update
        entries = dict(self._load())
        entries[key] = {"fetched": now, "used": now, "data": list(records)}
        self._save(entries)

    def clear(self) -> None:
        """Drop everything — the metadata index and any cached audio."""
        self._index = {}
        try:
            index_file().unlink()
        except OSError:
            pass
        try:
            for entry in audio_dir().iterdir():
                try:
                    entry.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    # ── typed access ──

    def get_playlists(self):
        records = self.get(KEY_PLAYLISTS)
        return [CachedPlaylist(r) for r in records] if records else None

    def put_playlists(self, playlists, editable_type=None) -> None:
        self.put(KEY_PLAYLISTS, [
            playlist_record(p, editable_type is not None and isinstance(p, editable_type))
            for p in playlists
        ])

    def get_playlist_tracks(self, playlist_id):
        records = self.get(f"playlist:{playlist_id}")
        return [CachedTrack(r) for r in records] if records else None

    def put_playlist_tracks(self, playlist_id, tracks) -> None:
        self.put(f"playlist:{playlist_id}", [track_record(t) for t in tracks])

    # ── local search index ──

    def iter_tracks(self):
        """Every cached track record, with the playlist it came from.

        Nothing calls this yet; it is the shape "search my own playlists"
        needs — TIDAL has no server-side API for that, so it can only ever
        be answered from an index like this one.
        """
        for key, entry in self._load().items():
            if not key.startswith("playlist:"):
                continue
            playlist_id = key.split(":", 1)[1]
            for record in entry.get("data") or []:
                yield playlist_id, record

    # ── budget ──

    def total_bytes(self) -> int:
        """Everything the cache directory is currently costing on disk."""
        total = _dir_size(audio_dir())
        try:
            total += index_file().stat().st_size
        except OSError:
            pass
        return total

    def enforce_budget(self) -> int:
        """Evict until the cache fits its budget. Returns bytes freed.

        Audio goes first and least-recently-used first: it is by far the
        bulk, and it is the cheapest to lose (one re-download). Only if the
        index alone still overshoots do metadata entries go, oldest first.
        Called after every write, so nothing schedules a sweep — being over
        budget is an event, not a state to poll for.
        """
        budget = max(0, int(self.budget_mb)) * 1024 * 1024
        freed = 0

        files = []
        try:
            for entry in audio_dir().iterdir():
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                if entry.is_file():
                    files.append((stat.st_atime, stat.st_size, entry))
        except OSError:
            pass
        files.sort()

        total = self.total_bytes()
        for _atime, size, path in files:
            if total <= budget:
                break
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            freed += size

        if total <= budget:
            return freed

        # Still over on metadata alone. Rare (the index is single-digit MB
        # even with hundreds of playlists) but the budget has to be real.
        entries = dict(self._load())
        for key in sorted(entries, key=lambda k: entries[k].get("used") or 0):
            if total <= budget:
                break
            entries.pop(key, None)
            self._index = entries
            try:
                path = index_file()
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"version": CACHE_VERSION, "entries": entries}))
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except OSError:
                break
            new_total = self.total_bytes()
            freed += max(0, total - new_total)
            total = new_total
        return freed
