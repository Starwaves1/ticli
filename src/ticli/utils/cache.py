"""On-disk cache for Ticli: a metadata index (playlists and their tracks, so
"Your Playlists" paints before TIDAL answers) and an audio directory (whole
tracks, written by AudioPlayer, sized and evicted here).

Machine-owned and disposable, unlike `~/.config/ticli`, except that the same
directory also holds the download index (`downloads.json`, utils/downloads.py):
losing it forgets the user's whole download library.

The cache is a first paint, never an answer: callers pair every read with a
live fetch that replaces it. MAX_AGE_SECONDS only drops entries from a month
offline. Records are flat text so a local playlist search can scan the index.
"""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_VERSION = 1
# `_load_tracker` reads a file with a different version as empty, so bumping
# this forgets every play count. Translate old values at read time instead.
TRACKER_VERSION = 1

# Seconds of a track that must play before it counts (half of a track under a
# minute). Keeps a shuffle through previews from out-scoring a staple.
PLAY_COUNTS_AFTER = 30.0

BYTES_PER_GB = 1024 ** 3

MAX_AGE_SECONDS = 30 * 24 * 3600

# Every extension AudioPlayer can produce (player._audio_extension owns that
# list; test_cache pins the two together).
AUDIO_EXTENSIONS = (".m4a", ".mp3", ".aac", ".flac", ".ogg", ".wav")


def is_owned_audio(name: str) -> bool:
    """Whether a filename is one ticli wrote: `{track_id}{ext}`, or
    `{track_id}.part` (extension optional there, because the downloader opens
    the file before the CDN names the container). Leaked parts must match, or
    they count against the budget but can never be evicted."""
    if name.endswith(".part"):
        stem = name[:-len(".part")]
        root, ext = os.path.splitext(stem)
        if not ext:
            return stem.isdigit()
    else:
        root, ext = os.path.splitext(name)
    return root.isdigit() and ext.lower() in AUDIO_EXTENSIONS


def owned_audio_files() -> list:
    """Every cached track (and half-written track) ticli owns, by exact path.
    Never a directory wipe: files ticli did not create must survive."""
    files = []
    try:
        entries = list(audio_dir().iterdir())
    except OSError:
        return files
    for path in entries:
        try:
            if is_owned_audio(path.name) and path.is_file():
                files.append(path)
        except OSError:
            continue
    return files


def cached_audio_path(track_id):
    """The whole cached file for this track, or None. Asks the disk, not the
    tracker: a song deleted by hand must fall through to the network."""
    if track_id in (None, ""):
        return None
    try:
        for path in sorted(audio_dir().glob(f"{track_id}.*")):
            if path.suffix != ".part" and is_owned_audio(path.name) \
                    and path.is_file():
                return str(path)
    except OSError:
        pass
    return None


def _default_cache_dir() -> Path:
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


def tracker_file() -> Path:
    """Audio bookkeeping. Separate from `metadata.json` because the two are
    gated by different settings (turning metadata off must not blind
    eviction), and outside `audio_dir()` so it is not counted in the budget."""
    return CACHE_DIR / "audio.json"


def artwork_dir() -> Path:
    """Rendered cover art (utils/artwork.py), outside audio_dir so it does not
    move the budget, song count or eviction order. It has its own ceiling."""
    return CACHE_DIR / "artwork"


def clear_artwork() -> int:
    """Delete every rendered cover (`.art`, and `.tmp` leftovers). Returns how
    many went."""
    removed = 0
    try:
        entries = list(artwork_dir().iterdir())
    except OSError:
        return removed
    for path in entries:
        if path.suffix not in (".art", ".tmp"):
            continue
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError as e:
            logger.debug("Could not delete cached artwork %s: %s", path, e)
    return removed


# ── Record shims ──
#
# What comes out of the cache carries only the fields list views render, plus
# an id to resolve through the session (HeadlessTidalPlayer._resolve_track).
# These constructors are also where "corrupt → defaults, never raises" lives:
# both the metadata index and player._restore_state (synchronous at launch, no
# catch-all) build rows from machine-written JSON through them. Any
# json-decodable record yields a shim whose fields have their documented types.


class _Named:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name


def _clean_str(value):
    """A non-empty str, or None. Anything else is corruption, not a value."""
    return value if isinstance(value, str) and value else None


def _clean_number(value):
    """An int or float, or 0. bool is excluded: True is corruption, not 1."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return 0


class CachedTrack:
    """A track as far as a list row is concerned. Never raises: `name` a
    non-empty str (else "?"), `duration` a number (else 0), `artists` a list of
    `_Named` from the non-empty str elements only, `album` a `_Named` or None.
    `id` is kept verbatim."""

    cached = True
    __slots__ = ("id", "name", "duration", "artists", "album")

    def __init__(self, record: dict):
        if not isinstance(record, dict):
            record = {}
        self.id = record.get("id")
        self.name = _clean_str(record.get("name")) or "?"
        self.duration = _clean_number(record.get("duration"))
        artists = record.get("artists")
        self.artists = [_Named(artist)
                        for artist in (artists if isinstance(artists, list) else [])
                        if _clean_str(artist)]
        album = _clean_str(record.get("album"))
        self.album = _Named(album) if album else None


class CachedPlaylist:
    """A playlist as far as the playlists list is concerned. Same guarantee as
    CachedTrack: `num_tracks` a number (else 0), `creator` a `_Named` or None,
    `editable` a bool."""

    cached = True
    __slots__ = ("id", "name", "num_tracks", "creator", "editable")

    def __init__(self, record: dict):
        if not isinstance(record, dict):
            record = {}
        self.id = record.get("id")
        self.name = _clean_str(record.get("name")) or "?"
        self.num_tracks = _clean_number(record.get("num_tracks"))
        creator = _clean_str(record.get("creator"))
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


def format_gb(num_bytes: int) -> str:
    """Three decimals, so a handful of tracks does not round to "0.00 GB"."""
    return f"{num_bytes / BYTES_PER_GB:.3f} GB"


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


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write-then-rename so readers never see a half-written file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class MetadataCache:
    """The cache the player talks to.

    The index and the tracker are only ever replaced whole, so every read is
    lock-free and sees one consistent generation. Writes can lose updates:
    the index has one writer at a time, but the tracker is written from
    several threads through one shared instance, so `_tracker_lock` serializes
    each load-modify-save inside `_mutate_tracker` (a leaf: held never across
    a disk walk, never by a caller).
    """

    def __init__(self, metadata: bool = True, songs: bool = True, budget_gb: int = 2):
        self.metadata = metadata
        self.songs = songs
        self.budget_gb = budget_gb
        self._index = None  # loaded from disk on first use
        # Measured on demand; see invalidate_audio_count
        self._audio_count = None
        self._disk_bytes = None
        self._tracker = None
        self._tracker_lock = threading.Lock()

    # ── plumbing ──

    @property
    def enabled(self) -> bool:
        """Whether the metadata index may be read or written."""
        return bool(self.metadata)

    @property
    def keeps_audio(self) -> bool:
        return bool(self.songs)

    @property
    def budget_bytes(self) -> int:
        return max(0, int(self.budget_gb)) * BYTES_PER_GB

    def _load(self) -> dict:
        """Read the index. Missing, corrupt or wrong version → empty, never raises."""
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
        self._index = entries
        return entries

    def _save(self, entries: dict) -> None:
        """Atomically replace the index, then enforce the budget. Best effort."""
        self._index = entries
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            _atomic_write_json(
                index_file(), {"version": CACHE_VERSION, "entries": entries})
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
        entries = dict(self._load())
        entries[key] = {"fetched": now, "used": now, "data": list(records)}
        self._save(entries)

    def clear(self) -> None:
        """Drop everything — the index, any cached audio, any cover art."""
        self.clear_metadata()
        self.clear_audio()
        clear_artwork()

    def clear_metadata(self) -> None:
        """Forget the index, on disk too."""
        self._index = {}
        try:
            index_file().unlink()
        except OSError:
            pass

    def clear_audio(self) -> tuple:
        """Delete the cached tracks, by exact path, one at a time. Returns
        (deleted, kept).

        Nothing is skipped for being in use: on POSIX the playing track keeps
        its descriptor (verified with mpv), and if the player re-opens a
        deleted file AudioPlayer.source_vanished / _monitor_playback restart
        it from the network. On Windows a file open without delete-sharing
        raises and is reported as kept. Only files from owned_audio_files are
        touched; one that can't be removed does not abort the rest.
        """
        removed = kept = 0
        gone = []
        for path in owned_audio_files():
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass  # already gone is the state we wanted
            except OSError as e:
                logger.debug("Could not delete cached track %s: %s", path, e)
                kept += 1
                continue
            gone.append(path.stem)
        self.forget_cached(gone)
        self.invalidate_audio_count()
        return removed, kept

    # ── how much is on disk ──

    def audio_count(self) -> int:
        """How many whole tracks are cached (".part" files aren't songs).
        Remembered, because the settings page repaints far more often than the
        directory changes."""
        if self._audio_count is None:
            self._audio_count = sum(
                1 for p in owned_audio_files() if p.suffix != ".part")
        return self._audio_count

    def disk_bytes(self) -> int:
        """total_bytes, remembered (the page repaints on every keystroke) and
        invalidated with the song count."""
        if self._disk_bytes is None:
            self._disk_bytes = self.total_bytes()
        return self._disk_bytes

    def invalidate_audio_count(self) -> None:
        """Something added to or removed from the audio directory."""
        self._audio_count = None
        self._disk_bytes = None

    # ── typed access ──

    def get_playlists(self):
        records = self.get("playlists")
        return [CachedPlaylist(r) for r in records] if records else None

    def put_playlists(self, playlists, editable_type=None) -> None:
        self.put("playlists", [
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
        """Every cached track record, with the playlist it came from. Serves
        "search my own playlists" (TIDAL has no API for it)."""
        if not self.enabled:
            return
        for key, entry in self._load().items():
            if not key.startswith("playlist:"):
                continue
            playlist_id = key.split(":", 1)[1]
            for record in entry.get("data") or []:
                yield playlist_id, record

    # ── the cache tracker ──
    #
    # The tracker is the source of truth for intent and metadata (what ticli
    # meant to cache, plays, last played); the disk is the source of truth for
    # existence. `reconcile()` (off the UI thread, never per paint) drops rows
    # whose file is gone and adopts files with no row at zero plays.
    #
    # Writers run on different threads (`note_played` on the playback monitor,
    # `note_cached`, `forget_cached`, `reconcile`) over one shared `_tracker`
    # memo. Unlocked, a `note_played` that straddles a `forget_cached`
    # resurrects a row for an unlinked file, and a `forget_cached` beside a
    # `note_cached` erases a file the cache holds. So every write goes through
    # `_mutate_tracker`, which holds `_tracker_lock` across the whole
    # load-modify-save (the load half is what loses a concurrent change). The
    # lock lives at the leaves only: `enforce_budget` and `clear_audio` go
    # through `forget_cached`, and `reconcile`'s directory walk is outside it.
    # Two ticli instances still race with no lock between them; `reconcile()`
    # corrects existence on the next start.

    def _load_tracker(self) -> dict:
        """Track id (as a string) → record. Missing or corrupt reads as empty."""
        if self._tracker is not None:
            return self._tracker
        tracks = {}
        try:
            data = json.loads(tracker_file().read_text())
            if isinstance(data, dict) and data.get("version") == TRACKER_VERSION:
                raw = data.get("tracks")
                if isinstance(raw, dict):
                    tracks = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            pass
        self._tracker = tracks
        return tracks

    def _save_tracker(self, tracks: dict) -> None:
        self._tracker = tracks
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            _atomic_write_json(
                tracker_file(), {"version": TRACKER_VERSION, "tracks": tracks})
        except OSError as e:
            logger.debug("Could not write the cache tracker: %s", e)

    def _mutate_tracker(self, change) -> None:
        """Read the tracker, let `change` edit a private copy, save it. The only
        user of `_tracker_lock`. `change` may return False for "nothing moved",
        which skips the write. Nothing reachable from `change` may touch the
        tracker again."""
        with self._tracker_lock:
            tracks = dict(self._load_tracker())
            if change(tracks) is False:
                return
            self._save_tracker(tracks)

    def audio_record(self, track_id):
        """What the tracker knows about this track, or None."""
        if track_id is None:
            return None
        return self._load_tracker().get(str(track_id))

    def note_cached(self, track_id, ext: str, size: int, quality=None) -> None:
        """A track just landed in the cache directory. `quality` is the tier
        TIDAL actually granted, not the one asked for; None means unknown and
        is never treated as a match."""
        if track_id is None or not self.keeps_audio:
            return
        key = str(track_id)

        def change(tracks):
            record = dict(tracks.get(key) or {})
            record.update({"ext": ext, "bytes": int(size or 0), "at": time.time()})
            if quality is not None:
                record["quality"] = quality
            record.setdefault("quality", None)
            record.setdefault("plays", 0)
            record.setdefault("last", time.time())
            tracks[key] = record

        self._mutate_tracker(change)

    def note_played(self, track_id) -> None:
        """One more play for this track, stamped with time.time() (the
        filesystem's `atime` is daily-granular under `relatime`)."""
        if track_id is None or not self.keeps_audio:
            return
        key = str(track_id)

        def change(tracks):
            record = dict(tracks.get(key) or {})
            record["plays"] = int(record.get("plays") or 0) + 1
            record["last"] = time.time()
            record.setdefault("quality", None)
            record.setdefault("ext", "")
            record.setdefault("bytes", 0)
            tracks[key] = record

        self._mutate_tracker(change)

    def forget_cached(self, track_ids) -> None:
        """Drop entries for files that are no longer there. The removal is
        decided inside the lock, because a `note_cached` can add a row between
        a read and the save."""
        keys = {str(t) for t in track_ids if t is not None}
        if not keys:
            return

        def change(tracks):
            gone = keys & tracks.keys()
            for key in gone:
                del tracks[key]
            return bool(gone)

        self._mutate_tracker(change)

    def audio_value(self, track_id, playing: bool = False) -> tuple:
        """How much this track is worth keeping: `(plays, last played)`.

        The eviction rule: a point per play, and the oldest among the tracks
        with the fewest plays goes first. Not plain LRU, so a four-hour binge
        on a new playlist cannot evict long-term staples.

        `playing=True` counts the play being earned right now. Without it a
        new track has zero plays and loses to everything, so a full cache
        would never admit anything again.
        """
        record = self.audio_record(track_id) or {}
        plays = int(record.get("plays") or 0) + (1 if playing else 0)
        last = float(record.get("last") or 0)
        if playing:
            last = max(last, time.time())
        return (plays, last)

    def should_cache(self, track_id, size: int = 0) -> bool:
        """Whether a track being played now is worth keeping.

        Refuse only under pressure: with room in the budget the answer is yes
        (a starting song has no history to judge by). When full, the candidate
        must beat the cheapest resident, the track eviction would take next. A
        tie loses, since replacing a track with an equal is wasted work.
        """
        if not self.keeps_audio:
            return False
        budget = self.budget_bytes
        if budget <= 0:
            return False
        if self.disk_bytes() + max(0, int(size or 0)) <= budget:
            return True
        resident = self.cheapest_resident(exclude=track_id)
        if resident is None:
            return True  # nothing to displace: the budget is spent elsewhere
        return self.audio_value(track_id, playing=True) > resident

    def cheapest_resident(self, exclude=None):
        """The value of the cached track eviction would take next, or None.
        Reads the tracker, not the directory."""
        skip = str(exclude) if exclude is not None else None
        values = [self.audio_value(key) for key in self._load_tracker()
                  if key != skip]
        return min(values) if values else None

    def cached_usage(self) -> tuple:
        """`(songs cached, what they cost)` from the tracker, with no disk walk.
        `reconcile()` corrects it against the disk."""
        count = 0
        total = 0
        for record in self._load_tracker().values():
            count += 1
            try:
                total += int(record.get("bytes") or 0)
            except (TypeError, ValueError):
                continue
        return count, total

    def reconcile(self) -> tuple:
        """Make the tracker agree with the disk. Returns `(dropped, adopted)`.

        Rows for missing files are dropped; files with no row (a cache from
        before the tracker, or a crash between rename and tracker write) are
        adopted at zero plays. One directory listing plus a stat per file, so
        it runs at startup on a daemon thread and after deletions, never from
        a paint. The scan stays outside `_tracker_lock` (it would stall play
        counts behind the filesystem); the change re-reads the tracker under
        the lock, so a concurrent `note_cached` is edited, not overwritten.
        """
        if not self.keeps_audio:
            return 0, 0
        on_disk = {}
        for path in owned_audio_files():
            if path.suffix == ".part":
                continue  # still arriving; not a cached song yet
            try:
                on_disk[path.stem] = path.stat().st_size
            except OSError:
                continue
        counts = {"dropped": 0, "adopted": 0}

        def change(tracks):
            before = dict(tracks)
            for key in [k for k in tracks if k not in on_disk]:
                tracks.pop(key, None)
                counts["dropped"] += 1
            now = time.time()
            for key, size in on_disk.items():
                record = tracks.get(key)
                if not isinstance(record, dict):
                    # Unknown quality must never be mistaken for the current tier
                    tracks[key] = {"ext": "", "quality": None, "bytes": size,
                                   "plays": 0, "last": now, "at": now}
                    counts["adopted"] += 1
                elif int(record.get("bytes") or 0) != size:
                    record = dict(record)
                    record["bytes"] = size
                    tracks[key] = record
            return tracks != before

        self._mutate_tracker(change)
        if counts["dropped"]:
            self.invalidate_audio_count()
        return counts["dropped"], counts["adopted"]

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

        Audio goes first, in `audio_value` order: fewest plays, then oldest.
        A file the tracker has never heard of sorts at `(0, 0)`: litter or
        pre-tracker, and `reconcile()` adopts anything real long before. Only
        if the index alone still overshoots do metadata entries go, oldest
        first. Called after every write; being over budget is an event, not a
        state to poll for.
        """
        budget = self.budget_bytes
        freed = 0
        # Every size change on disk comes through here, so drop the memo here
        self._disk_bytes = None

        files = []
        for entry in owned_audio_files():
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            # A `.part` is worth less than any song: still arriving, or litter
            value = (-1, 0.0) if entry.suffix == ".part" \
                else self.audio_value(entry.stem)
            files.append((value, entry.name, size, entry))
        # Name is only a tiebreak, for a stated order between equal values
        files.sort(key=lambda f: (f[0], f[1]))

        total = self.total_bytes()
        evicted = []
        for _value, _name, size, path in files:
            if total <= budget:
                break
            try:
                # missing_ok: a racing sweep must not read "already gone" as
                # "still costing us" and evict a file that fits
                path.unlink(missing_ok=True)
            except OSError:
                continue
            total -= size
            freed += size
            evicted.append(path.stem)
            self._audio_count = None  # a song just left the directory
        self.forget_cached(evicted)

        if total <= budget:
            return freed

        # Still over on metadata alone (rare: the index is single-digit MB)
        entries = dict(self._load())
        for key in sorted(entries, key=lambda k: entries[k].get("used") or 0):
            if total <= budget:
                break
            entries.pop(key, None)
            self._index = entries
            try:
                _atomic_write_json(
                    index_file(),
                    {"version": CACHE_VERSION, "entries": entries})
            except OSError:
                break
            new_total = self.total_bytes()
            freed += max(0, total - new_total)
            total = new_total
        return freed
