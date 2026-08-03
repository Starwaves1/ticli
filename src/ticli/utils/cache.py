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
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_VERSION = 1
TRACKER_VERSION = 1

# What a play is worth waiting for before it counts. A skip is not a play, and
# the whole eviction rule is "points per play", so the threshold is what keeps
# a four-hour shuffle through a hundred previews from out-scoring a staple.
# Thirty seconds, or half of a track shorter than a minute.
PLAY_COUNTS_AFTER = 30.0

# The budget is a whole number of gigabytes on the settings page and bytes
# everywhere below it. Read at call time, so a test can shrink a gigabyte.
BYTES_PER_GB = 1024 ** 3

# Beyond this an entry is treated as absent. Only reachable by being offline
# (or not opening a playlist) for a month — every visit rewrites the entry.
MAX_AGE_SECONDS = 30 * 24 * 3600

# Index keys. Flat strings so the file stays greppable by hand.
KEY_PLAYLISTS = "playlists"

# What ticli itself writes into the audio directory: "{track_id}{ext}" once a
# track is whole, and "{track_id}.part" while it is still arriving. Every
# extension AudioPlayer can produce is here (player._audio_extension owns that
# list; test_cache pins the two together). The rule matters because deleting
# cached songs deletes an explicit list of files ticli owns — the cache
# directory is a shared place, and anything else in it is somebody else's.
AUDIO_EXTENSIONS = (".m4a", ".mp3", ".aac", ".flac", ".ogg", ".wav")


def is_owned_audio(name: str) -> bool:
    """Whether a filename is one ticli wrote.

    A part file is bare — `467461385.part`, no extension — because
    `AudioPlayer._start_download` opens the handle before the CDN has said
    what container it is sending; the extension only arrives with the first
    response header, and the file is renamed to `{track_id}{ext}` at the end.
    So the extension is *optional* on a `.part` and required on a whole file.

    This asked for `{track_id}{ext}.part` for a year and got False for every
    part file production ever wrote. A leaked one — SIGKILL, a crash, power
    loss, anything that isn't `stop()` — was then counted against the budget
    by `total_bytes` (a plain directory size, which does not consult this)
    while being invisible to `owned_audio_files`, so it could be neither
    evicted nor cleared. Once the leak exceeded the budget every sweep
    deleted every real song and kept the leak. See ai/INCIDENTS #6.
    """
    if name.endswith(".part"):
        stem = name[:-len(".part")]
        root, ext = os.path.splitext(stem)
        # Bare "{track_id}.part" is what the downloader opens; the extended
        # form is kept because a rename that lands mid-sweep is still ours
        if not ext:
            return bool(stem) and stem.isdigit()
    else:
        root, ext = os.path.splitext(name)
    return bool(root) and root.isdigit() and ext.lower() in AUDIO_EXTENSIONS


def owned_audio_files() -> list:
    """Every cached track (and half-written track) ticli owns, by exact path.

    Built by name, never by wiping the directory: a file in there that ticli
    did not create must survive anything this module does.
    """
    files = []
    try:
        entries = list(audio_dir().iterdir())
    except OSError:
        return files
    for path in entries:
        try:
            # A path that has become a directory is not a track ticli wrote
            if is_owned_audio(path.name) and path.is_file():
                files.append(path)
        except OSError:
            continue
    return files


def cached_audio_path(track_id):
    """The whole cached file for this track, or None.

    Looked up by stem, because the extension is whatever the stream turned
    out to be, and a half-written `.part` is never an answer. The disk, not
    the tracker, because existence is the disk's to say — a song deleted by
    hand has to fall through to the network rather than be claimed.
    """
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


def tracker_file() -> Path:
    """The cache tracker: what ticli meant to have cached, and why.

    Its own file rather than a section of `metadata.json`, because the two
    are gated by different settings — this is bookkeeping about *audio*, so it
    lives and dies with `cache_songs`, and turning the metadata index off must
    not blind eviction. Outside `audio_dir()` too, so it never counts against
    a budget that is about songs.
    """
    return CACHE_DIR / "audio.json"


def artwork_dir() -> Path:
    """Rendered cover art (see utils/artwork.py).

    Its own directory, deliberately outside audio_dir: the budget, the song
    count and the eviction order are all about audio, and a few hundred files
    of a few hundred bytes each must not move any of those numbers. Artwork
    keeps its own small ceiling instead.
    """
    return CACHE_DIR / "artwork"


def clear_artwork() -> int:
    """Delete every rendered cover. Returns how many went.

    By name, like every other deletion here: only files ticli wrote (the
    `.art` renderings and any `.tmp` left by an interrupted write) are
    touched, never the directory.
    """
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
# What comes back out of the cache is not a tidalapi object and never
# pretends to be one: it carries exactly the fields the list views render,
# and an id to resolve with when the user acts on it. Anything that needs
# the real thing (a stream URL, a playlist edit) resolves through the
# session first — see HeadlessTidalPlayer._resolve_track.
#
# The shims are also where the "corrupt → defaults, never raises" contract
# for records lives. Two consumers build rows straight from machine-written
# JSON through these constructors — the metadata index here and the saved
# player state (player._restore_state) — and the second runs synchronously
# at launch with no catch-all around it, so a corrupt field that merely
# *rode along* would crash either the restore or the first frame that
# renders it. The contract sits in the one place both import rather than
# being copied per caller (three copies of a contract is how one goes
# missing — see the guarded state reader's history), so: any json-decodable
# record yields a shim whose every field has its documented type, with
# wrong-typed fields reading as their defaults.


class _Named:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name


def _clean_str(value):
    """A non-empty str, or None. Anything else is corruption, not a value."""
    return value if isinstance(value, str) and value else None


def _clean_number(value):
    """An int or float, or 0. bool is excluded on purpose: True is a number
    to isinstance, but a duration of True is corruption, not one second."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return 0


class CachedTrack:
    """A track as far as a list row is concerned.

    Guaranteed typed, whatever the record held: `name` a non-empty str
    (else "?"), `duration` an int or float (else 0), `artists` a list of
    `_Named` built from the record's non-empty str elements only — a number
    in an artists list is corruption, not an artist — and `album` a `_Named`
    or None. `id` alone is kept verbatim: it is only compared and resolved,
    never rendered or done arithmetic on. Never raises.
    """

    cached = True
    __slots__ = ("id", "name", "duration", "artists", "album")

    def __init__(self, record: dict):
        if not isinstance(record, dict):
            record = {}
        self.id = record.get("id")
        self.name = _clean_str(record.get("name")) or "?"
        self.duration = _clean_number(record.get("duration"))
        artists = record.get("artists")
        self.artists = [_Named(n)
                        for n in (artists if isinstance(artists, list) else [])
                        if isinstance(n, str) and n]
        album = _clean_str(record.get("album"))
        self.album = _Named(album) if album else None


class CachedPlaylist:
    """A playlist as far as the playlists list is concerned.

    Same guarantee as CachedTrack: `name` a non-empty str (else "?"),
    `num_tracks` an int or float (else 0), `creator` a `_Named` or None,
    `editable` a plain bool, `id` verbatim. Never raises.
    """

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
    """Bytes the way the settings page says them. Three decimals because two
    would round a handful of tracks down to "0.00 GB" — the number is there to
    show the cache filling up, so it has to move when a song lands."""
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
    """Write-then-rename so readers never see a half-written file. Callers
    keep their own mkdir and their own idea of what a failed write means."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class MetadataCache:
    """The cache the player talks to.

    Whole-object replacement everywhere: neither the index nor the tracker
    is ever mutated in place, so **every read is lock-free and correct** —
    `get`, `audio_record`, `audio_value`, `cheapest_resident` and
    `cached_usage` see one consistent generation of a dict, never a
    half-written one. That is what the old "no locks" note was actually
    right about, and it has not changed.

    What it does not buy is *lost updates*, and that is a different
    question with a different answer per store. The **index** is written
    from one place at a time and can stay unlocked. The **tracker** cannot:
    several threads write it concurrently through one shared MetadataCache
    (see the tracker section below), so `_tracker_lock` serializes the
    load-modify-save cycle of every tracker write. It is held only inside
    `_mutate_tracker` — never across a disk walk, never by a caller — so
    there is nothing to acquire twice.
    """

    def __init__(self, metadata: bool = True, songs: bool = True, budget_gb: int = 2):
        # Two independent switches, not one ladder: keeping lists on disk and
        # keeping tracks on disk are different sizes of promise
        self.metadata = metadata
        self.songs = songs
        self.budget_gb = budget_gb
        self._index = None  # loaded from disk on first use
        # Both measured on demand and remembered until something moves them —
        # see audio_count / disk_bytes and invalidate_audio_count
        self._audio_count = None
        self._disk_bytes = None
        # The tracker (see tracker_file). Loaded once, replaced whole, and
        # written only under _tracker_lock — one MetadataCache is shared by
        # every thread in the player, so the memo below is shared state
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
        # Whole-dict replacement, so a reader never sees a partial update
        entries = dict(self._load())
        entries[key] = {"fetched": now, "used": now, "data": list(records)}
        self._save(entries)

    def clear(self) -> None:
        """Drop everything — the index, any cached audio, any cover art."""
        self.clear_metadata()
        self.clear_audio()
        clear_artwork()

    def clear_metadata(self) -> None:
        """Forget the index. Turning metadata caching off has to mean the disk
        is empty, not just unread."""
        self._index = {}
        try:
            index_file().unlink()
        except OSError:
            pass

    def clear_audio(self) -> tuple:
        """Delete the cached tracks, by exact path, one at a time.

        Clearing the cache clears it: nothing is skipped for being in use.
        On POSIX, unlinking a file another process has open succeeds and that
        process keeps its descriptor, so the track playing from a file that
        has just been deleted plays on to its end (verified with mpv). If the
        player does re-open it — the race where it had not opened it yet — it
        exits at once and AudioPlayer.source_vanished / _monitor_playback
        restart the track from the network where it left off. Windows is
        stricter: a file open without delete-sharing raises instead, and that
        file is reported as kept rather than silently forgotten.

        Never a directory wipe: the list is the files ticli wrote (see
        owned_audio_files), so an unrelated file sharing the directory is
        left alone. Anything that can't be removed — already gone, no
        permission, turned into a directory — is skipped rather than aborting
        the rest, and the count is recomputed from what is really left.
        Returns (deleted, kept).
        """
        removed = kept = 0
        gone = []
        for path in owned_audio_files():
            try:
                path.unlink()
                removed += 1
                gone.append(path.stem)
            except FileNotFoundError:
                gone.append(path.stem)  # already gone is the state we wanted
            except OSError as e:
                logger.debug("Could not delete cached track %s: %s", path, e)
                kept += 1
        self.forget_cached(gone)  # the tracker is the cache; both go together
        self.invalidate_audio_count()  # remeasure from disk, not from intent
        return removed, kept

    # ── how much is on disk ──

    def audio_count(self) -> int:
        """How many whole tracks are cached.

        Counted once and remembered: the settings page repaints far more often
        than the directory changes, and everything that can change it says so
        (see invalidate_audio_count). Half-written ".part" files aren't songs.
        """
        if self._audio_count is None:
            self._audio_count = sum(
                1 for p in owned_audio_files() if p.suffix != ".part")
        return self._audio_count

    def disk_bytes(self) -> int:
        """What the cache is costing on disk, for the settings page.

        The same sizing enforce_budget uses (total_bytes), just remembered:
        the page repaints on every keystroke and stat-ing a full audio
        directory each frame would be felt. Invalidated by the same events as
        the song count, so the two can never disagree.
        """
        if self._disk_bytes is None:
            self._disk_bytes = self.total_bytes()
        return self._disk_bytes

    def invalidate_audio_count(self) -> None:
        """Something added to or removed from the audio directory."""
        self._audio_count = None
        self._disk_bytes = None

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

        What "search my own playlists" is served from — TIDAL has no
        server-side API for that, so it can only ever be answered from an
        index like this one. Disabled means disabled: with metadata caching
        off this yields nothing rather than reading a file that shouldn't be
        consulted, the same as every other read.
        """
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
    # The tracker is the source of truth for **intent and metadata**: what
    # ticli meant to cache, at which tier, how often it has been played and
    # when it was last played. The files are downstream of it.
    #
    # The disk stays the source of truth for **existence**. Songs deleted from
    # the cache folder by hand have to be handled durably, so a tracker entry
    # whose file is gone is a fact to absorb — dropped, and the totals
    # corrected — never an error and never something to go on reporting. The
    # two are reconciled by `reconcile()`, off the UI thread; the one thing
    # that must not happen is a full directory walk on every paint, which is
    # what the settings page used to do.
    #
    # Crash safety is the `.part`-and-rename discipline used everywhere else:
    # the tracker is written atomically, and either order of a crash is
    # survivable because reconcile() reads both sides. A tracker entry with no
    # file is dropped; a file with no entry is adopted at zero plays, which is
    # also how a cache from before the tracker existed is picked up.
    #
    # Inside one process the tracker has several writers and they are all on
    # different threads: `note_played` on the playback monitor, `note_cached`
    # and `enforce_budget`'s `forget_cached` on the download daemon, another
    # `note_cached` on the refetch job, `reconcile` on a startup daemon, and
    # `forget_cached` again on the bulk-download writer. They share one
    # MetadataCache and therefore one `self._tracker` memo, so every one of
    # them is a copy-modify-replace over the same object.
    #
    # Unlocked, two overlapping cycles keep only the later one's change, and
    # the old note here called that acceptable: "the loser of a race loses
    # one play count". That stopped being true the moment `forget_cached`
    # joined the writers. A `note_played` that reads the memo before a delete
    # and saves after it **resurrects** a row for a file that has just been
    # unlinked — the settings page then counts a song that is not there and
    # eviction weighs it — and a `forget_cached` landing beside a
    # `note_cached` erases a file the cache really does hold. Neither is a
    # play count.
    #
    # So every write goes through `_mutate_tracker`, which holds
    # `_tracker_lock` across the *whole* load-modify-save: it is the load
    # half that makes a concurrent writer's change vanish, so a lock around
    # the save alone would fix nothing. The lock lives at the leaves only —
    # `enforce_budget` and `clear_audio` reach the tracker through
    # `forget_cached` and never hold it themselves, and `reconcile`'s
    # directory listing and per-file stat happen outside it.
    #
    # Reads are still lock-free, and still right: the dict is only ever
    # replaced whole, so a reader gets some generation of it, never a torn
    # one. Two ticli *instances* still race for the file with no lock between
    # them, by the project's rule — the loser loses that process's recent
    # bookkeeping, and `reconcile()` corrects existence on the next start.

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
            tracks = {}
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
        """Read the tracker, let `change` edit a private copy, save it.

        The one read-modify-write cycle in this module, and the only thing
        that takes `_tracker_lock` — every tracker writer funnels through it
        so that "load" and "save" cannot be prised apart by another thread.
        `change` mutates the dict it is handed and may return False for
        "nothing moved", which skips the write entirely.

        A leaf by construction: nothing reachable from `change` may touch
        the tracker again, which is why the callers that delete files
        (`enforce_budget`, `clear_audio`) hand their casualties to
        `forget_cached` instead of removing rows themselves.
        """
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
        """A track just landed in the cache directory.

        `quality` is the tier TIDAL actually *granted*, not the one that was
        asked for — it is what makes "is this file already what a download at
        this tier would fetch?" and "is this file below the tier I have now
        set?" answerable without a request. None means "not known", which is
        what every file cached before the tracker existed is, and which is
        deliberately never treated as a match.
        """
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
        """One more point for this track, and the time it earned it.

        `time.time()` stamped here rather than the filesystem's `atime`:
        `relatime` makes that roughly daily-granular, which is useless for
        ordering a listening session, and this is a write we control.

        The increment is a read-modify-write and this runs on the monitor
        thread, so it is one of the reasons `_mutate_tracker` exists: two
        plays counted at once used to make one.
        """
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
        """Drop entries for files that are no longer there. Whole-dict
        replacement, and a no-op when nothing actually changed.

        The removal is decided *inside* the lock rather than from a dict
        read before it, because between the two a `note_cached` can add a
        row this call never meant to take.
        """
        keys = {str(t) for t in track_ids if t is not None}
        if not keys:
            return

        def change(tracks):
            gone = [k for k in tracks if k in keys]
            for key in gone:
                tracks.pop(key, None)
            return bool(gone)

        self._mutate_tracker(change)

    def audio_value(self, track_id, playing: bool = False) -> tuple:
        """How much this track is worth keeping: `(plays, last played)`.

        Garrett's rule, in one place so eviction and admission cannot drift:
        **a point per play, and the oldest among the tracks with the fewest
        plays goes first.** All one-play tracks oldest-first, then the
        two-play ones, and so on — which is the whole reason it is not plain
        LRU, because a four-hour binge on a new playlist must not evict
        long-term staples.

        `playing=True` counts the play the track is earning *right now*. That
        is what stops admission freezing the cache: without it a brand-new
        track has zero plays, loses to everything, and once the cache is full
        nothing new ever gets in again. With it, a song being listened to
        displaces the oldest *other* one-play track and nothing else — which
        is the same rule, honestly applied to a song that is being played.

        Deliberately expressible at any moment, so the decision can move from
        the start of a track to its end (where "was it actually listened to?"
        is answerable) without the rule itself changing.
        """
        record = self.audio_record(track_id) or {}
        plays = int(record.get("plays") or 0) + (1 if playing else 0)
        last = float(record.get("last") or 0)
        if playing:
            last = max(last, time.time())
        return (plays, last)

    def should_cache(self, track_id, size: int = 0) -> bool:
        """Whether a track being played now is worth keeping.

        **Refuse only under pressure.** With room in the budget the answer is
        yes, exactly as it was before there was a rule at all — at the moment
        a song starts you know almost nothing about it, and an unconditional
        value test would freeze the cache into whatever it held the day the
        rule was switched on. Only when the cache is full do both sides have
        a history to compare, which is the whole of Garrett's decision.

        Under pressure it is the value function and nothing else: the
        candidate has to beat the cheapest thing already in there, i.e. the
        track eviction would take next. A tie loses — the resident is already
        on disk and moving bytes to replace it with an equal is work for
        nothing.

        One function, called from one place, so that moving the decision from
        the start of a track to its end is a change of caller and not of rule.
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

        Reads the tracker, not the directory — the point of maintaining an
        index is that "what is in the cache and what is it worth" costs no
        disk walk. Existence is still checked at the moment of eviction.
        """
        skip = str(exclude) if exclude is not None else None
        values = [self.audio_value(key) for key in self._load_tracker()
                  if key != skip]
        return min(values) if values else None

    def cached_usage(self) -> tuple:
        """`(songs cached, what they cost)` — from the tracker.

        The settings page's cache readout. Cheap by construction: no stat, no
        directory walk, no JSON per track. It is corrected against the disk by
        `reconcile()`, which is the only place the two are compared.
        """
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

        Disk, not the tracker, is the authority on existence — a song deleted
        from the cache folder by hand has to be handled durably, and the only
        honest answer is to stop claiming it. The reverse is real too: a file
        with no entry is adopted at zero plays, which covers a cache written
        before the tracker existed and a crash between the rename and the
        tracker write.

        Not cheap (one directory listing plus a stat per file), so it is never
        called from a paint — the player runs it once at startup on a daemon
        thread, and again after anything that deletes files. That expense is
        also why the scan stays *outside* `_tracker_lock`: holding a lock
        across a directory walk would stall the monitor thread's play counts
        behind the filesystem for no benefit, and the scan reads the disk,
        which the lock does not protect anyway. Only the load-modify-save is
        serialized, and it re-reads the tracker under the lock, so a
        `note_cached` that landed during the walk is edited, not overwritten.
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
                    # Zero plays and no known tier: unknown is not the same as
                    # "the tier you have set now", and must never be mistaken
                    # for it (that would silently hand someone the wrong
                    # quality)
                    tracks[key] = {"ext": "", "quality": None, "bytes": size,
                                   "plays": 0, "last": now, "at": now}
                    counts["adopted"] += 1
                elif int(record.get("bytes") or 0) != size:
                    record = dict(record)
                    record["bytes"] = size
                    tracks[key] = record
            # dropped and adopted keys both change the dict, so this one
            # comparison covers them and the bytes corrections alike
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

        Audio goes first — it is by far the bulk and the cheapest to lose
        (one re-download) — and in the order the tracker says: **fewest plays
        first, and oldest first within the same number of plays.** Not plain
        LRU, deliberately: a four-hour binge on a new playlist would evict
        long-term staples under LRU, and the point of counting plays is that
        it cannot. `audio_value` is that rule, in one place.

        A file the tracker has never heard of sorts at `(0, 0)` and therefore
        goes first, which is right — it is either litter or a leftover from
        before the tracker, and `reconcile()` adopts anything real long before
        a sweep sees it.

        Only if the index alone still overshoots do metadata entries go,
        oldest first. Called after every write, so nothing schedules a sweep —
        being over budget is an event, not a state to poll for.
        """
        budget = self.budget_bytes
        freed = 0
        # Every path that changes the size on disk — a write, an eviction, a
        # clear — comes through here, so this is the one place the remembered
        # total has to be dropped. Re-measured lazily on the next paint.
        self._disk_bytes = None

        # Only ticli's own files are ever evicted — the budget is about what
        # ticli put there, and a stranger's file in the same directory is not
        # ours to reclaim
        files = []
        for entry in owned_audio_files():
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            # Our own stamp, never the filesystem's atime: `relatime` makes
            # that roughly daily-granular, which cannot order a listening
            # session. A half-written `.part` has no track id and no value.
            # A half-written `.part` is worth strictly less than any song:
            # it is either still arriving or litter from a kill, and either
            # way it should go before a track somebody has listened to
            value = (-1, 0.0) if entry.suffix == ".part" \
                else self.audio_value(entry.stem)
            files.append((value, entry.name, size, entry))
        # Name is only a tiebreak, so two files of equal value evict in a
        # stated order rather than whatever the directory happened to yield
        files.sort(key=lambda f: (f[0], f[1]))

        total = self.total_bytes()
        evicted = []
        for _value, _name, size, path in files:
            if total <= budget:
                break
            try:
                # missing_ok, because a sweep racing another one (two
                # downloads landing together) must not read "already gone" as
                # "still costing us" and go on to evict a file that fits
                path.unlink(missing_ok=True)
            except OSError:
                continue
            total -= size
            freed += size
            evicted.append(path.stem)
            self._audio_count = None  # a song just left the directory
        # The tracker is what the cache *is*, so a file leaving it leaves the
        # tracker in the same breath — otherwise the settings page would go on
        # counting a song that is not there
        self.forget_cached(evicted)

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
                _atomic_write_json(
                    index_file(),
                    {"version": CACHE_VERSION, "entries": entries})
            except OSError:
                break
            new_total = self.total_bytes()
            freed += max(0, total - new_total)
            total = new_total
        return freed
