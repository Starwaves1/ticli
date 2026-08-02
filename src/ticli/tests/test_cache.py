"""Tests for the on-disk metadata cache.

The point of the cache is that a playlist you have opened before paints with
no network wait at all, while still converging on the truth — so these tests
run a fake session with deliberate latency and assert on both the timing and
the eventual contents. Nothing here touches the network or the real cache
directory: CACHE_DIR and the config file are both redirected to tmp_path.
"""

import json
import os
import tempfile
import threading
import time
import types

import pytest

from ticli import player as player_mod
from ticli.player import HeadlessTidalPlayer
from ticli.tests.fakes import patch_get
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod
from ticli.utils.cache import CachedPlaylist, CachedTrack, MetadataCache

# Every network call the fake session makes waits this long. Roughly what the
# user actually sees against TIDAL, and the thing the cache exists to remove.
LATENCY = 0.4


def _wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return False


def _settle():
    """The gap between a fake request returning and the background thread
    having assigned its result. Milliseconds, but not zero."""
    time.sleep(0.05)


def _track(tid):
    return types.SimpleNamespace(
        id=tid,
        name=f"Track {tid}",
        duration=180 + tid,
        artists=[types.SimpleNamespace(name=f"Artist {tid}")],
        album=types.SimpleNamespace(name=f"Album {tid}"),
    )


class _FakePlaylist:
    def __init__(self, pid, name, tracks, latency):
        self.id = pid
        self.name = name
        self.num_tracks = len(tracks)
        self.creator = types.SimpleNamespace(name="Garrett")
        self._tracks = tracks
        self._latency = latency
        self.track_calls = 0

    def tracks(self):
        self.track_calls += 1
        time.sleep(self._latency)
        return list(self._tracks)


class _FakeSession:
    """A TIDAL session that is slow, the way the real one is."""

    def __init__(self, playlists=None, latency=LATENCY):
        self.latency = latency
        self.audio_quality = None
        self.is_pkce = False
        self.playlist_calls = 0
        self.list_calls = 0
        self._playlists = playlists if playlists is not None else [
            _FakePlaylist("p1", "Morning", [_track(i) for i in range(1, 6)], latency),
            _FakePlaylist("p2", "Focus", [_track(i) for i in range(6, 9)], latency),
        ]
        self.user = types.SimpleNamespace(playlists=self._list_playlists)

    def _list_playlists(self):
        self.list_calls += 1
        time.sleep(self.latency)
        return list(self._playlists)

    def playlist(self, pid):
        self.playlist_calls += 1
        time.sleep(self.latency)
        for p in self._playlists:
            if p.id == pid:
                return p
        raise KeyError(pid)

    def track(self, tid):
        time.sleep(self.latency)
        return _track(tid)


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Keep every player built here off the real config, cache and temp
    directories — ffplay's scratch download lands in the last of those."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config" / "config.json")
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    return tmp_path


# ── audio download fixtures ──
#
# The download is a plain HTTP GET, so a fake `requests.get` is the whole
# network. Every assertion downstream is about bytes on disk.

URL = "https://cdn.example/track.mp4?token=1785015974"
BODY = bytes(range(256)) * 64  # 16,384 bytes — several chunks at any size


class _FakeResponse:
    """Just enough of a streaming requests response to write from."""

    def __init__(self, body, content_type, on_chunk=None, chunk_size=4096):
        self.headers = {"Content-Type": content_type} if content_type else {}
        self._body = body
        self._on_chunk = on_chunk
        self._chunk_size = chunk_size

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        for n, start in enumerate(range(0, len(self._body), self._chunk_size)):
            if self._on_chunk:
                self._on_chunk(n)
            yield self._body[start:start + self._chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_get(monkeypatch, body=BODY, content_type="audio/mp4", on_chunk=None):
    """Serve `body` to the downloader. Returns the list of URLs asked for."""
    calls = []

    def _get(url, stream=False, timeout=None, **kw):
        calls.append(url)
        return _FakeResponse(body, content_type, on_chunk)

    patch_get(monkeypatch, player_mod, _get)
    return calls


def _no_spawn(monkeypatch):
    """Stand in for mpv/ffplay. Returns the processes that were 'spawned'."""
    procs = []

    class _Proc:
        def __init__(self, cmd, **kw):
            self.cmd = cmd
            procs.append(self)

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(player_mod.subprocess, "Popen", _Proc)
    return procs


def _audio_files(pending=False):
    """Whole tracks in the audio cache — or, with pending, everything
    including half-written .part files."""
    try:
        return sorted(f for f in cache_mod.audio_dir().iterdir()
                      if f.is_file() and (pending or f.suffix != ".part"))
    except OSError:
        return []


def _scratch_files():
    return sorted(cache_mod.Path(tempfile.gettempdir()).glob("ticli-cache-*"))


def _player(session=None):
    p = HeadlessTidalPlayer()
    p.session = session or _FakeSession()
    return p


def _stamp(cache, track_id, plays=0, last=0.0, quality=None, size=0, ext=".m4a"):
    """Write a tracker record with stated numbers.

    Times are stated rather than measured for the same reason the eviction
    test places its files by hand: the rule under test is an ordering, and an
    ordering that depended on how long the test took would be noise.
    """
    tracks = dict(cache._load_tracker())
    tracks[str(track_id)] = {"ext": ext, "quality": quality, "bytes": size,
                             "plays": plays, "last": last, "at": last}
    cache._save_tracker(tracks)


def _first_paint(action, rows):
    """Seconds until the list has something in it — what the user actually
    waits for, as opposed to when the background fetch happens to finish."""
    start = time.monotonic()
    action()
    assert _wait_for(rows), "nothing ever painted"
    return time.monotonic() - start


def _load_playlists(player):
    """Load the playlists and wait for the live answer to land."""
    calls = player.session.list_calls
    paint = _first_paint(player._load_playlists, lambda: player._playlists)
    assert _wait_for(lambda: player.session.list_calls > calls and not player._playlists_loading)
    _settle()
    return paint


def _open(player, playlist):
    """Open a playlist and wait for the live track list to land."""
    calls = playlist.track_calls
    paint = _first_paint(lambda: player._open_playlist(playlist), lambda: player._browse_tracks)
    assert _wait_for(lambda: playlist.track_calls > calls and not player._browse_loading)
    _settle()
    return paint


class TestCacheDirectory:
    def test_follows_the_os_convention(self, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", "/xdg")
        monkeypatch.setattr(cache_mod.sys, "platform", "linux")
        assert cache_mod._default_cache_dir() == cache_mod.Path("/xdg/ticli")

        monkeypatch.setattr(cache_mod.sys, "platform", "darwin")
        assert cache_mod._default_cache_dir().parts[-3:] == ("Library", "Caches", "ticli")

        monkeypatch.setattr(cache_mod.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\g\AppData\Local")
        assert cache_mod._default_cache_dir().parts[-2:] == ("ticli", "Cache")

    def test_never_inside_the_config_directory(self):
        assert config_mod.CONFIG_DIR not in cache_mod._default_cache_dir().parents


class TestColdVersusWarm:
    def test_second_visit_paints_without_waiting(self):
        session = _FakeSession()
        cold = _load_playlists(_player(session))
        assert cold >= LATENCY * 0.8, "cold load should have paid the network"

        warm = _load_playlists(_player(session))
        assert warm < 0.05, f"warm load still cost {warm:.3f}s"

    def test_warm_playlist_shows_the_cached_rows_immediately(self):
        session = _FakeSession()
        first = _player(session)
        _load_playlists(first)
        _open(first, session._playlists[0])

        second = _player(session)
        _load_playlists(second)
        second._playlists_loading = True  # pretend the refetch is still in flight
        cached = second._cache.get_playlists()
        assert [p.name for p in cached] == ["Morning", "Focus"]

        start = time.monotonic()
        second._open_playlist(cached[0])
        first_paint = time.monotonic() - start
        assert first_paint < 0.05
        assert [t.name for t in second._browse_tracks] == [f"Track {i}" for i in range(1, 6)]
        assert second._browse_loading is False

    def test_cached_rows_carry_what_the_list_renders(self):
        session = _FakeSession()
        p = _player(session)
        _load_playlists(p)
        _open(p, session._playlists[0])

        rows = MetadataCache().get_playlist_tracks("p1")
        assert isinstance(rows[0], CachedTrack)
        assert rows[0].name == "Track 1"
        assert rows[0].duration == 181
        assert rows[0].artists[0].name == "Artist 1"
        assert rows[0].album.name == "Album 1"

        lists = MetadataCache().get_playlists()
        assert isinstance(lists[0], CachedPlaylist)
        assert lists[0].num_tracks == 5
        assert lists[0].creator.name == "Garrett"


class TestRevalidation:
    def test_stale_entries_are_replaced_by_the_live_answer(self):
        session = _FakeSession()
        _load_playlists(_player(session))

        # The user renames a playlist and adds one somewhere else
        session._playlists[0].name = "Mornings (2026)"
        session._playlists.append(
            _FakePlaylist("p3", "New One", [_track(99)], session.latency)
        )

        p = _player(session)
        p._load_playlists()
        # First paint is the stale list — that is the whole point
        assert [pl.name for pl in p._playlists] == ["Morning", "Focus"]
        # ...and one round trip later it is the truth
        assert _wait_for(lambda: len(p._playlists) == 3)
        assert [pl.name for pl in p._playlists] == ["Mornings (2026)", "Focus", "New One"]
        _settle()
        assert MetadataCache().get_playlists()[0].name == "Mornings (2026)"

    def test_playlist_tracks_revalidate_too(self):
        session = _FakeSession()
        pl = session._playlists[0]
        p = _player(session)
        _open(p, pl)

        pl._tracks = [_track(41), _track(42)]
        p2 = _player(session)
        p2._open_playlist(pl)
        assert len(p2._browse_tracks) == 5  # stale rows, painted at once
        assert _wait_for(lambda: len(p2._browse_tracks) == 2)
        assert [t.id for t in p2._browse_tracks] == [41, 42]

    def test_a_fetch_always_runs_even_on_a_cache_hit(self):
        session = _FakeSession()
        _load_playlists(_player(session))
        _load_playlists(_player(session))
        assert session.list_calls == 2, "cache must never answer on its own"

    def test_expired_entries_are_ignored(self, monkeypatch):
        cache = MetadataCache()
        cache.put("playlists", [{"id": "p1", "name": "Old"}])
        later = time.time() + cache_mod.MAX_AGE_SECONDS + 1
        monkeypatch.setattr(cache_mod.time, "time", lambda: later)
        assert MetadataCache().get("playlists") is None

    def test_opening_a_cached_row_gets_the_real_playlist(self):
        session = _FakeSession()
        first = _player(session)
        _load_playlists(first)
        _open(first, session._playlists[0])

        p = _player(session)
        row = p._cache.get_playlists()[0]
        p._open_playlist(row)
        assert p._browse_tracks, "cached tracks should paint at once"
        assert _wait_for(lambda: session.playlist_calls == 1)
        assert _wait_for(lambda: not p._browse_loading
                         and not any(getattr(t, "cached", False) for t in p._browse_tracks))
        # Real objects, so playback and playlist edits have something to use
        assert not any(getattr(t, "cached", False) for t in p._browse_tracks)

    def test_a_cached_row_still_plays(self):
        """Playing a cached row resolves it into a real track first."""
        session = _FakeSession()
        p = _player(session)
        p.audio = types.SimpleNamespace(plays=[])
        row = CachedTrack({"id": 7, "name": "Track 7", "duration": 100, "artists": ["A"]})
        resolved = p._resolve_track(row)
        assert resolved.id == 7 and not getattr(resolved, "cached", False)
        assert p._resolve_track(resolved) is resolved  # real tracks pass through

    def test_playing_a_cached_row_upgrades_the_queue(self):
        """So a queue built from cached rows resolves each track once, not
        once per play."""
        session = _FakeSession(latency=0.01)
        p = _player(session)
        p.audio = types.SimpleNamespace(
            plays=[], play_url=lambda *a, **k: p.audio.plays.append(a),
        )
        row = CachedTrack({"id": 7, "name": "Track 7", "duration": 100, "artists": []})
        p._queue = [row]
        p._queue_index = 0
        p._play_track(row)
        assert _wait_for(lambda: not getattr(p._queue[0], "cached", False))
        assert p._current_track.id == 7


class TestCorruptCache:
    def test_garbage_file_falls_back_to_empty(self, isolated_dirs):
        cache_mod.CACHE_DIR.mkdir(parents=True)
        cache_mod.index_file().write_text("{not json at all")
        assert MetadataCache().get_playlists() is None

    def test_partial_file_falls_back_to_empty(self, isolated_dirs):
        cache_mod.CACHE_DIR.mkdir(parents=True)
        cache_mod.index_file().write_text('{"version": 1, "entries": {"playlists":')
        assert MetadataCache().get_playlists() is None

    def test_wrong_shape_is_ignored_entry_by_entry(self, isolated_dirs):
        cache_mod.CACHE_DIR.mkdir(parents=True)
        cache_mod.index_file().write_text(json.dumps({
            "version": 1,
            "entries": {
                "playlists": "not a list at all",
                "playlist:p1": {"fetched": time.time(), "data": [{"id": 1, "name": "T"}]},
            },
        }))
        cache = MetadataCache()
        assert cache.get_playlists() is None
        assert [t.name for t in cache.get_playlist_tracks("p1")] == ["T"]

    def test_a_future_version_is_not_misread(self, isolated_dirs):
        cache_mod.CACHE_DIR.mkdir(parents=True)
        cache_mod.index_file().write_text(json.dumps({
            "version": cache_mod.CACHE_VERSION + 1,
            "entries": {"playlists": {"fetched": time.time(), "data": [{"id": "x"}]}},
        }))
        assert MetadataCache().get_playlists() is None

    def test_a_corrupt_cache_never_stops_a_load(self, isolated_dirs):
        cache_mod.CACHE_DIR.mkdir(parents=True)
        cache_mod.index_file().write_text("\x00\x01garbage")
        session = _FakeSession()
        p = _player(session)
        _load_playlists(p)
        assert [pl.name for pl in p._playlists] == ["Morning", "Focus"]

    def test_an_unwritable_cache_directory_is_survivable(self, isolated_dirs, monkeypatch):
        monkeypatch.setattr(cache_mod, "CACHE_DIR", isolated_dirs / "nope" / "deeper")
        monkeypatch.setattr(
            cache_mod.Path, "mkdir",
            lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")),
        )
        cache = MetadataCache()
        cache.put("playlists", [{"id": "p1", "name": "Morning"}])  # must not raise
        # Still served from memory for this run
        assert cache.get_playlists()[0].name == "Morning"


class TestCorruptRecordFields:
    """The shim constructors are where "corrupt → defaults, never raises"
    lives for records: two consumers (the metadata index and the saved player
    state) build rows straight from machine-written JSON through them, and
    the restore runs synchronously at launch with no catch-all — so a corrupt
    field must neither raise here nor ride onward to crash the first frame
    that renders it. Asserted on the resulting values and types, not just on
    "no raise"."""

    def test_a_non_list_artists_field_reads_as_no_artists(self):
        row = CachedTrack({"id": 1, "duration": 200, "artists": 5})
        assert row.artists == []
        assert row.duration == 200  # the good fields survive the bad one
        assert row.id == 1

    def test_non_string_artist_elements_are_dropped_not_stringified(self):
        # A number in an artists list is corruption, not an artist — "5"
        # on screen would be a value that isn't real
        row = CachedTrack({"id": 1, "artists": [5, "X", ""]})
        assert [a.name for a in row.artists] == ["X"]

    def test_a_wrong_typed_duration_reads_as_zero(self):
        row = CachedTrack({"id": 1, "name": "Track 1", "duration": "long"})
        assert row.duration == 0
        assert row.name == "Track 1"

    def test_a_boolean_duration_is_corruption_not_a_number(self):
        # bool passes isinstance(int); a duration of True must still read 0
        assert CachedTrack({"id": 1, "duration": True}).duration == 0

    def test_a_real_fractional_duration_is_kept(self):
        assert CachedTrack({"id": 1, "duration": 200.5}).duration == 200.5

    def test_a_non_string_name_reads_as_the_placeholder(self):
        assert CachedTrack({"id": 1, "name": 5}).name == "?"

    def test_a_non_string_album_reads_as_none(self):
        assert CachedTrack({"id": 1, "album": 7}).album is None

    def test_nested_junk_yields_a_fully_typed_shim(self):
        row = CachedTrack({"id": 1, "name": ["x"], "duration": {"n": 1},
                           "artists": {"not": "a list"}, "album": ["y"]})
        assert (row.id, row.name, row.duration, row.artists, row.album) \
            == (1, "?", 0, [], None)

    def test_the_id_is_kept_verbatim_whatever_it_is(self):
        # Only ever compared and resolved, so it passes through untouched
        assert CachedTrack({"id": "uuid-x"}).id == "uuid-x"

    def test_a_non_dict_record_yields_an_all_defaults_shim(self):
        row = CachedTrack("not a record at all")
        assert (row.id, row.name, row.duration, row.artists, row.album) \
            == (None, "?", 0, [], None)

    def test_a_playlist_record_gets_the_same_treatment(self):
        row = CachedPlaylist({"id": "p1", "name": 5, "num_tracks": "many",
                              "creator": 7, "editable": "yes"})
        assert row.id == "p1"
        assert row.name == "?"
        assert row.num_tracks == 0
        assert row.creator is None
        assert row.editable is True  # bool(...) as ever: truthy, never a raise

    def test_a_playlist_boolean_num_tracks_reads_as_zero(self):
        assert CachedPlaylist({"id": "p1", "num_tracks": True}).num_tracks == 0

    def test_a_non_dict_playlist_record_yields_an_all_defaults_shim(self):
        row = CachedPlaylist([1, 2, 3])
        assert (row.id, row.name, row.num_tracks, row.creator, row.editable) \
            == (None, "?", 0, None, False)


class TestBudget:
    def _audio_file(self, size, name="7"):
        """A file named the way AudioPlayer names one — eviction only ever
        touches files ticli owns."""
        path = cache_mod.audio_dir()
        path.mkdir(parents=True, exist_ok=True)
        f = path / f"{name}.m4a"
        f.write_bytes(b"\0" * size)
        return f

    @pytest.fixture(autouse=True)
    def _small_gigabyte(self, monkeypatch):
        """The budget is whole gigabytes on screen, which is far more than a
        test wants to write. Shrink what a "GB" means so eviction can be
        proven against files small enough to create — the arithmetic under
        test is the same either way."""
        monkeypatch.setattr(cache_mod, "BYTES_PER_GB", 1024 * 1024)

    def test_a_gigabyte_budget_is_a_gigabyte_of_bytes(self, monkeypatch):
        monkeypatch.setattr(cache_mod, "BYTES_PER_GB", 1024 ** 3)
        assert MetadataCache(budget_gb=2).budget_bytes == 2 * 1024 ** 3
        assert MetadataCache(budget_gb=0).budget_bytes == 0

    def test_eviction_brings_the_directory_under_budget(self):
        cache = MetadataCache(budget_gb=1)
        files = [self._audio_file(400_000, name=str(i)) for i in range(5)]
        assert cache.total_bytes() > 1024 * 1024

        freed = cache.enforce_budget()

        assert freed > 0
        assert cache.total_bytes() <= 1024 * 1024
        assert any(f.exists() for f in files), "eviction must not empty the cache"

    def test_least_recently_used_audio_goes_first(self):
        cache = MetadataCache(budget_gb=1)
        old = self._audio_file(700_000, name="101")
        new = self._audio_file(700_000, name="102")
        past = time.time() - 10_000
        import os
        os.utime(old, (past, past))

        cache.enforce_budget()

        assert not old.exists()
        assert new.exists()

    def test_a_zero_budget_evicts_metadata_too(self):
        cache = MetadataCache(budget_gb=0)
        cache.put("playlist:p1", [{"id": 1, "name": "T"}])
        cache.put("playlist:p2", [{"id": 2, "name": "U"}])
        cache.enforce_budget()
        assert cache.total_bytes() == 0 or cache.get("playlist:p1") is None

    def test_writing_enforces_the_budget_without_being_asked(self):
        cache = MetadataCache(budget_gb=1)
        self._audio_file(2_000_000, name="103")
        cache.put("playlist:p1", [{"id": 1, "name": "T"}])
        assert cache.total_bytes() <= 1024 * 1024

    def test_lowering_the_setting_evicts_immediately(self):
        p = _player()
        self._audio_file(2_000_000, name="103")
        p.config["cache_budget_gb"] = 1
        p._apply_setting("cache_budget_gb", 1)
        assert p._cache.total_bytes() <= 1024 * 1024

    def test_the_default_budget_is_two_gigabytes(self):
        assert config_mod.DEFAULTS["cache_budget_gb"] == 2


class TestCacheSwitches:
    """The two booleans have to gate the two behaviours, independently."""

    def test_metadata_off_writes_nothing_and_reads_nothing(self):
        session = _FakeSession()
        p = _player(session)
        p.config["cache_metadata"] = False
        p._apply_setting("cache_metadata", False)
        _load_playlists(p)
        assert not cache_mod.index_file().exists()

        warm = _load_playlists(p)
        assert warm >= LATENCY * 0.8, "metadata off must not serve a cached first paint"

    def test_switching_metadata_off_clears_what_was_already_stored(self):
        session = _FakeSession()
        p = _player(session)
        _load_playlists(p)
        assert cache_mod.index_file().exists()

        p._apply_setting("cache_metadata", False)

        assert not cache_mod.index_file().exists()
        assert MetadataCache().get_playlists() is None

    def test_songs_off_leaves_the_metadata_index_alone(self):
        session = _FakeSession()
        p = _player(session)
        _load_playlists(p)

        p._apply_setting("cache_songs", False)

        assert cache_mod.index_file().exists()
        assert MetadataCache().get_playlists(), "songs off must not drop the index"

    def test_songs_off_stops_keeping_them_without_deleting_them(self):
        """Disabling and clearing are separate concerns: turning caching off
        keeps what is already there unless the user asks for it to go."""
        p = _player()
        audio_dir = cache_mod.audio_dir()
        audio_dir.mkdir(parents=True, exist_ok=True)
        song = audio_dir / "12.m4a"
        song.write_bytes(b"x" * 10)

        p._apply_setting("cache_songs", False)

        assert song.exists()
        assert p._cache.keeps_audio is False

    def test_each_switch_gates_only_its_own_half(self):
        assert MetadataCache(metadata=True, songs=False).keeps_audio is False
        assert MetadataCache(metadata=False, songs=True).keeps_audio is True
        assert MetadataCache(metadata=False, songs=True).enabled is False
        assert MetadataCache(metadata=True, songs=False).enabled is True

    def test_the_settings_page_offers_the_cache_rows(self):
        keys = [spec["key"] for spec in config_mod.SETTINGS_SPEC]
        assert keys.index("cache_metadata") < keys.index("cache_songs") < keys.index("cache_budget_gb")
        assert config_mod.coerce(config_mod.get_spec("cache_budget_gb"), 99_999) == 64
        assert config_mod.coerce(config_mod.get_spec("cache_metadata"), "nonsense") is True


class TestAudioRetention:
    """FULL mode has to put real bytes in the directory the budget sizes.

    The version of this that shipped never wrote one: the download was an
    ffmpeg call with an extension ffmpeg couldn't mux to, inside the
    ffplay-only branch, so on mpv it never even ran. Every test here therefore
    asserts on file *contents*, not on bookkeeping — bookkeeping was green
    the whole time the feature was inert.
    """

    def _audio(self, songs=True, player_cmd="mpv"):
        from ticli.player import AudioPlayer
        return AudioPlayer(player_cmd, cache=MetadataCache(songs=songs))

    def _download(self, audio, url=URL, cache_key=12):
        """Run one download to completion the way play_url would."""
        audio._start_download(url, cache_key, audio._download_gen)
        assert _wait_for(lambda: _audio_files() or _scratch_files()), "nothing was written"
        _settle()

    # ── the bytes ──

    @pytest.mark.parametrize("player_cmd", ["mpv", "ffplay"])
    def test_a_played_track_lands_on_disk_byte_for_byte(self, monkeypatch, player_cmd):
        """The regression test for the whole bug, on *both* backends — mpv is
        the default, and it is where FULL did literally nothing."""
        _fake_get(monkeypatch)
        procs = _no_spawn(monkeypatch)
        audio = self._audio(player_cmd=player_cmd)

        audio.play_url(URL, title="T", cache_key=12)

        assert _wait_for(lambda: _audio_files())
        _settle()
        files = _audio_files()
        assert [f.name for f in files] == ["12.m4a"]
        assert files[0].read_bytes() == BODY
        # and it streamed the URL rather than a local file it didn't have yet
        assert URL in procs[0].cmd

    def test_a_track_already_on_disk_is_played_without_the_network(self, monkeypatch):
        calls = _fake_get(monkeypatch)
        procs = _no_spawn(monkeypatch)
        audio = self._audio()
        kept = cache_mod.audio_dir()
        kept.mkdir(parents=True, exist_ok=True)
        (kept / "12.m4a").write_bytes(BODY)

        audio.play_url(URL, title="T", cache_key=12)
        _settle()

        assert calls == [], "a cached track must not be fetched again"
        assert str(kept / "12.m4a") in procs[0].cmd

    def test_the_extension_is_what_the_stream_says_it_is(self, monkeypatch):
        """Named for the bytes that arrived, never for the tier requested —
        this session gets AAC even when it asks for lossless, and a session
        that does get FLAC must land a .flac through the same code."""
        _fake_get(monkeypatch, content_type="audio/flac")
        self._download(self._audio())
        assert [f.name for f in _audio_files()] == ["12.flac"]

    def test_the_extension_falls_back_to_the_url_then_to_mp4(self):
        assert player_mod._audio_extension("audio/mp4; charset=utf-8", URL) == ".m4a"
        assert player_mod._audio_extension("audio/x-flac", URL) == ".flac"
        assert player_mod._audio_extension(None, "https://cdn/x.flac?token=1") == ".flac"
        assert player_mod._audio_extension(None, URL) == ".m4a"
        assert player_mod._audio_extension("application/octet-stream", "https://cdn/x") == ".m4a"

    def test_the_bytes_are_kept_after_the_track_ends(self, monkeypatch):
        _fake_get(monkeypatch)
        audio = self._audio()
        self._download(audio)

        audio.stop()

        assert [f.name for f in _audio_files()] == ["12.m4a"]
        assert _audio_files()[0].read_bytes() == BODY

    # ── nothing kept when nothing was asked for ──

    def test_nothing_is_stored_with_song_caching_off(self, monkeypatch):
        calls = _fake_get(monkeypatch)
        _no_spawn(monkeypatch)
        audio = self._audio(songs=False, player_cmd="mpv")
        audio.play_url(URL, title="T", cache_key=12)
        _settle()
        assert calls == [], "songs off must not download a track"
        assert _audio_files() == []
        assert audio._cached_audio_path(12) is None

    def test_ffplays_scratch_copy_is_deleted_when_the_track_ends(self, monkeypatch):
        """ffplay's pause kills the process, so it still needs something local
        to resume from even when nothing is being kept — but that copy is
        scratch, and it must not outlive the track."""
        _fake_get(monkeypatch)
        audio = self._audio(songs=False, player_cmd="ffplay")
        self._download(audio, cache_key=None)
        assert _scratch_files(), "ffplay lost its resume copy"
        assert audio._cache_file and cache_mod.Path(audio._cache_file).read_bytes() == BODY

        audio.stop()

        assert _scratch_files() == []
        assert _audio_files() == []

    # ── failure leaves no junk ──

    def test_an_abandoned_download_leaves_nothing_behind(self, monkeypatch):
        stopped = threading.Event()
        audio = self._audio()
        _fake_get(monkeypatch, on_chunk=lambda n: (
            audio.stop(), stopped.set()) if n == 1 else None)

        audio._start_download(URL, 12, audio._download_gen)

        assert _wait_for(stopped.is_set)
        assert _wait_for(lambda: not _audio_files(pending=True))
        assert _audio_files() == [], "a skipped track must not leave a partial file"

    def test_a_failed_download_leaves_nothing_behind(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("connection reset")
        patch_get(monkeypatch, player_mod, _boom)
        audio = self._audio()

        audio._start_download(URL, 12, audio._download_gen)
        _settle()

        assert _audio_files(pending=True) == []
        assert audio._cache_file is None

    def test_a_half_written_file_is_never_served_as_whole(self):
        audio = self._audio()
        path = cache_mod.audio_dir()
        path.mkdir(parents=True, exist_ok=True)
        (path / "12.part").write_bytes(BODY[:10])
        assert audio._cached_audio_path(12) is None

    # ── the budget, against files that now really exist ──

    def test_downloaded_bytes_count_against_the_budget(self, monkeypatch):
        _fake_get(monkeypatch)
        audio = self._audio()
        self._download(audio)
        assert audio.cache.total_bytes() >= len(BODY)

    def test_a_landed_track_is_swept_against_the_budget(self, monkeypatch):
        """Eviction had never run against a real audio file, because there
        had never been one."""
        _fake_get(monkeypatch)
        audio = self._audio()
        audio.cache.budget_gb = 0

        audio._start_download(URL, 12, audio._download_gen)

        assert _wait_for(lambda: audio.cache.total_bytes() == 0)
        assert _audio_files() == []

    def test_the_oldest_download_is_the_one_evicted(self, monkeypatch):
        # Both files are placed directly, with times stated rather than
        # measured: what is under test is the eviction *order*, and nothing
        # about that should depend on when the test ran, how long a download
        # thread took, or whether a sweep it started is still in flight. The
        # version of this that raced one was flaky about 1 run in 4.
        audio = self._audio()
        directory = cache_mod.audio_dir()
        directory.mkdir(parents=True, exist_ok=True)
        old = directory / "12.m4a"
        old.write_bytes(BODY)
        # A second track that only overshoots the budget with the first there
        big = directory / "13.m4a"
        big.write_bytes(b"x" * 1_040_000)
        # Equal plays, so the older one goes — the tracker's own stamp, not
        # the filesystem's atime, which `relatime` makes ~daily-granular
        _stamp(audio.cache, 12, plays=1, last=1_000_000)
        _stamp(audio.cache, 13, plays=1, last=2_000_000)
        monkeypatch.setattr(cache_mod, "BYTES_PER_GB", 1024 * 1024)
        audio.cache.budget_gb = 1

        audio._sweep_cache()

        assert not old.exists()
        assert big.exists()

    def test_a_sweep_racing_another_stops_at_the_budget(self, monkeypatch):
        """Two downloads landing together sweep at the same time. A file the
        other sweep already unlinked has been freed, not kept — reading it the
        other way evicts one file too many, which is what made the eviction
        test flaky rather than wrong."""
        audio = self._audio()
        directory = cache_mod.audio_dir()
        directory.mkdir(parents=True, exist_ok=True)
        gone = directory / "12.m4a"
        gone.write_bytes(b"x" * 600_000)
        os.utime(gone, (1_000_000, 1_000_000))
        keep = directory / "13.m4a"
        keep.write_bytes(b"x" * 400_000)
        os.utime(keep, (2_000_000, 2_000_000))
        monkeypatch.setattr(cache_mod, "BYTES_PER_GB", 1024 * 1024)
        audio.cache.budget_gb = 1
        # The other sweep got there first
        gone.unlink()

        audio._sweep_cache()

        assert keep.exists()

    def test_the_settings_row_does_not_promise_a_backend(self):
        """Caching songs is a plain HTTP GET, so it is the same on mpv and
        ffplay — the row once said "(ffplay backend)" while doing nothing on
        either."""
        desc = config_mod.get_spec("cache_songs")["desc"]
        assert "ffplay" not in desc and "mpv" not in desc


class TestSongCount:
    """The tiny status next to the "Cache songs" toggle. It has to be cheap —
    the settings page repaints on every keystroke — and it has to be right
    after anything that could have moved it."""

    def _song(self, name):
        path = cache_mod.audio_dir()
        path.mkdir(parents=True, exist_ok=True)
        f = path / name
        f.write_bytes(b"x" * 32)
        return f

    def test_counts_whole_tracks_only(self):
        self._song("1.m4a")
        self._song("2.flac")
        self._song("3.part")  # still downloading — not a song yet
        assert MetadataCache().audio_count() == 2

    def test_an_empty_or_missing_directory_is_zero(self):
        assert MetadataCache().audio_count() == 0

    def test_the_directory_is_read_once_not_per_repaint(self):
        cache = MetadataCache()
        self._song("1.m4a")
        assert cache.audio_count() == 1
        self._song("2.m4a")
        assert cache.audio_count() == 1, "a repaint must not re-stat the directory"
        cache.invalidate_audio_count()
        assert cache.audio_count() == 2

    def test_a_landed_download_refreshes_it(self, monkeypatch):
        from ticli.player import AudioPlayer
        _fake_get(monkeypatch)
        audio = AudioPlayer("mpv", cache=MetadataCache(songs=True))
        assert audio.cache.audio_count() == 0

        audio._start_download(URL, 12, audio._download_gen)
        assert _wait_for(lambda: _audio_files())
        _settle()

        assert audio.cache.audio_count() == 1

    def test_eviction_refreshes_it(self, monkeypatch):
        monkeypatch.setattr(cache_mod, "BYTES_PER_GB", 1024)
        cache = MetadataCache(budget_gb=0)
        self._song("1.m4a")
        assert cache.audio_count() == 1
        cache.enforce_budget()
        assert cache.audio_count() == 0

    def test_the_settings_page_shows_it(self):
        p = _player()
        self._song("1.m4a")
        p._cache.invalidate_audio_count()
        assert "1 song on disk" in p._build_settings_display().plain
        self._song("2.m4a")
        p._cache.invalidate_audio_count()
        assert "2 songs on disk" in p._build_settings_display().plain

    def test_opening_the_page_recounts(self):
        p = _player()
        p._cache.audio_count()  # warm it at zero
        self._song("1.m4a")
        p._handle_player_key("c")
        assert p._cache.audio_count() == 1


class TestOwnedFilesOnly:
    """Deleting cached songs deletes an explicit list of files ticli wrote —
    the cache directory is a shared place, and a stranger's file in it must
    survive everything this module does."""

    def _dir(self):
        path = cache_mod.audio_dir()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write(self, name, size=32):
        f = self._dir() / name
        f.write_bytes(b"x" * size)
        return f

    def test_the_names_ticli_writes_are_the_names_it_owns(self):
        assert cache_mod.is_owned_audio("12345.m4a")
        assert cache_mod.is_owned_audio("12345.flac")
        # What _start_download actually opens: no extension, because the CDN
        # has not said what container it is sending yet
        assert cache_mod.is_owned_audio("12345.part")
        # And the extended form, which a rename landing mid-sweep can show
        assert cache_mod.is_owned_audio("12345.m4a.part")
        assert not cache_mod.is_owned_audio("important.txt")
        assert not cache_mod.is_owned_audio("mixtape.m4a")
        assert not cache_mod.is_owned_audio("mixtape.part")
        assert not cache_mod.is_owned_audio("12345")
        assert not cache_mod.is_owned_audio(".part")

    def test_the_part_file_the_downloader_opens_is_owned(self, monkeypatch):
        """Pins the predicate to the writer rather than to a name someone
        thought the writer used. The bug was exactly this gap."""
        opened = []
        monkeypatch.setattr(player_mod, "fetch_to_file",
                            lambda sources, part, **kw: opened.append(part))
        audio = player_mod.AudioPlayer("mpv", cache=MetadataCache())
        audio._start_download(URL, 12, audio._download_gen)
        assert _wait_for(lambda: opened)
        assert cache_mod.is_owned_audio(os.path.basename(opened[0])), opened[0]

    def test_a_leaked_part_file_is_evicted_before_a_real_song(self, monkeypatch):
        """The whole of the bug, in bytes on disk.

        A part file left behind by a kill is counted by total_bytes (a plain
        directory size) whether or not this module recognises it. When it did
        not, every sweep evicted the real songs and kept the leak for ever.
        """
        monkeypatch.setattr(cache_mod, "BYTES_PER_GB", 1024)
        cache = MetadataCache(budget_gb=1)
        leak = self._write("111.part", 1536)   # over budget on its own
        song = self._write("222.m4a", 256)
        os.utime(leak, (1, 1))                 # least recently used: goes first

        cache.enforce_budget()

        assert not leak.exists(), "a leaked .part must be evictable"
        assert song.exists(), "the real song must not be the one deleted"

    def test_clearing_the_cache_clears_a_leaked_part_file(self):
        cache = MetadataCache()
        leak = self._write("111.part")
        assert cache.clear_audio() == (1, 0)
        assert not leak.exists()

    def test_a_leaked_part_file_is_not_counted_as_a_song(self):
        self._write("111.part")
        assert MetadataCache().audio_count() == 0

    def test_every_extension_the_downloader_can_produce_is_owned(self):
        """If player._audio_extension grows a container, the delete list has
        to grow with it or those files become undeletable litter."""
        produced = set(player_mod.AUDIO_MIME_EXTENSIONS.values())
        produced |= set(player_mod.AUDIO_URL_EXTENSIONS.values())
        produced.add(player_mod.DEFAULT_AUDIO_EXT)
        for ext in produced:
            assert cache_mod.is_owned_audio(f"12{ext}"), ext

    def test_a_decoy_file_survives_the_delete(self):
        cache = MetadataCache()
        song = self._write("12.m4a")
        decoy = self._write("important.txt")
        stray_dir = self._dir() / "12.m4a.d"
        stray_dir.mkdir()

        cache.clear_audio()

        assert not song.exists()
        assert decoy.exists(), "a file ticli did not write must not be deleted"
        assert stray_dir.is_dir()

    def test_a_decoy_file_survives_eviction(self, monkeypatch):
        monkeypatch.setattr(cache_mod, "BYTES_PER_GB", 1024)
        cache = MetadataCache(budget_gb=0)
        song = self._write("12.m4a", 4096)
        decoy = self._write("important.txt", 4096)

        cache.enforce_budget()

        assert not song.exists()
        assert decoy.exists()

    def test_half_written_files_go_too(self):
        cache = MetadataCache()
        part = self._write("12.part")
        cache.clear_audio()
        assert not part.exists()

    def test_a_file_that_is_already_gone_does_not_raise(self, monkeypatch):
        cache = MetadataCache()
        self._write("12.m4a")
        monkeypatch.setattr(
            cache_mod.Path, "unlink",
            lambda self, *a, **kw: (_ for _ in ()).throw(FileNotFoundError(self)))
        assert cache.clear_audio() == (0, 0)  # no exception, nothing claimed

    def test_one_unlinkable_file_does_not_abort_the_rest(self, monkeypatch):
        cache = MetadataCache()
        stuck = self._write("12.m4a")
        others = [self._write(f"{i}.m4a") for i in (13, 14)]
        real_unlink = cache_mod.Path.unlink

        def _unlink(self, *a, **kw):
            if self.name == stuck.name:
                raise PermissionError(self)
            return real_unlink(self, *a, **kw)

        monkeypatch.setattr(cache_mod.Path, "unlink", _unlink)

        removed, kept = cache.clear_audio()

        assert (removed, kept) == (2, 1)
        assert stuck.exists()
        assert not any(f.exists() for f in others)
        assert cache.audio_count() == 1, "the count must match what is really left"


class TestTheCacheTracker:
    """The tracker is the source of truth for intent; the disk is for existence.

    Both halves are load-bearing. "Tracker decides" is what makes the settings
    page cheap and eviction ordered by something better than `atime`. "Disk
    decides existence" is Garrett's standing requirement that a song deleted
    from the folder by hand is handled durably — it must not be reported as
    present, played as though it exists, or counted against the budget for
    ever.
    """

    def _song(self, cache, track_id, size=32, **stamp):
        directory = cache_mod.audio_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{track_id}.m4a"
        path.write_bytes(b"x" * size)
        _stamp(cache, track_id, size=size, **stamp)
        return path

    def test_it_survives_a_round_trip_through_the_file(self):
        cache = MetadataCache()
        cache.note_cached(12, ".m4a", 4096, quality="LOSSLESS")
        cache.note_played(12)
        again = MetadataCache()
        record = again.audio_record(12)
        assert record["quality"] == "LOSSLESS"
        assert record["bytes"] == 4096
        assert record["plays"] == 1

    def test_it_is_written_atomically_and_privately(self):
        cache = MetadataCache()
        cache.note_cached(12, ".m4a", 4096)
        path = cache_mod.tracker_file()
        assert path.exists()
        assert oct(path.stat().st_mode)[-3:] == "600"
        assert not list(path.parent.glob("audio.tmp"))

    def test_a_corrupt_tracker_reads_as_empty_rather_than_raising(self):
        cache_mod.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_mod.tracker_file().write_text("{ not json")
        assert MetadataCache().cached_usage() == (0, 0)

    def test_it_is_not_the_metadata_index(self):
        """Different settings gate them, so they cannot share a file: turning
        the metadata index off must not blind eviction."""
        cache = MetadataCache(metadata=False, songs=True)
        cache.note_cached(12, ".m4a", 4096, quality="HIGH")
        assert cache.audio_record(12)["quality"] == "HIGH"
        assert not cache_mod.index_file().exists()

    def test_metadata_only_mode_records_nothing(self):
        cache = MetadataCache(metadata=True, songs=False)
        cache.note_cached(12, ".m4a", 4096)
        cache.note_played(12)
        assert cache.audio_record(12) is None

    # ── disk is the authority on existence ──

    def test_a_song_deleted_by_hand_stops_being_claimed(self):
        cache = MetadataCache()
        path = self._song(cache, 12, size=100, plays=3, last=1_000)
        assert cache.cached_usage() == (1, 100)

        path.unlink()  # the user dragged it to the trash
        dropped, adopted = cache.reconcile()

        assert (dropped, adopted) == (1, 0)
        assert cache.cached_usage() == (0, 0)
        assert cache.audio_record(12) is None

    def test_a_file_the_tracker_never_heard_of_is_adopted(self):
        """A cache written before the tracker existed, or a crash between the
        rename and the tracker write. Zero plays, and no known tier — unknown
        is not the same as "whatever you have set now"."""
        cache = MetadataCache()
        directory = cache_mod.audio_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "77.m4a").write_bytes(b"x" * 64)

        dropped, adopted = cache.reconcile()

        assert (dropped, adopted) == (0, 1)
        assert cache.audio_record(77)["plays"] == 0
        assert cache.audio_record(77)["quality"] is None
        assert cache.cached_usage() == (1, 64)

    def test_a_size_that_changed_on_disk_is_corrected(self):
        cache = MetadataCache()
        path = self._song(cache, 12, size=100)
        path.write_bytes(b"x" * 250)
        cache.reconcile()
        assert cache.cached_usage() == (1, 250)

    def test_a_half_written_file_is_not_a_cached_song(self):
        cache = MetadataCache()
        directory = cache_mod.audio_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "12.part").write_bytes(b"x" * 64)
        assert cache.reconcile() == (0, 0)
        assert cache.cached_usage() == (0, 0)

    def test_the_readout_costs_no_directory_walk(self, monkeypatch):
        """The whole reason to maintain an index. The settings page reads
        this on every paint."""
        cache = MetadataCache()
        self._song(cache, 12, size=100)
        self._song(cache, 13, size=200)
        monkeypatch.setattr(
            cache_mod, "owned_audio_files",
            lambda: pytest.fail("the settings readout walked the directory"))
        assert cache.cached_usage() == (2, 300)

    def test_eviction_takes_the_file_out_of_the_tracker_too(self, monkeypatch):
        monkeypatch.setattr(cache_mod, "BYTES_PER_GB", 1024)
        cache = MetadataCache(budget_gb=1)
        self._song(cache, 12, size=2048, plays=1, last=1_000)

        cache.enforce_budget()

        assert cache.audio_record(12) is None, \
            "the tracker went on claiming a song eviction had deleted"
        assert cache.cached_usage() == (0, 0)

    def test_clearing_the_cache_clears_the_tracker(self):
        cache = MetadataCache()
        self._song(cache, 12, size=100)
        cache.clear_audio()
        assert cache.cached_usage() == (0, 0)


def _run_together(jobs, timeout=30.0):
    """Start every job on its own thread, wait for all of them, re-raise.

    A failure inside a thread is otherwise a printed traceback and a green
    test, which for a concurrency test is the worst possible outcome — it
    would pass exactly as loudly with no lock at all.
    """
    errors = []

    def wrap(job):
        def run():
            try:
                job()
            except BaseException as e:  # noqa: BLE001 - re-raised below
                errors.append(e)
        return run

    threads = [threading.Thread(target=wrap(job), daemon=True) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    alive = [t for t in threads if t.is_alive()]
    assert not alive, \
        f"{len(alive)} tracker writer(s) never finished — a deadlock, not a race"
    if errors:
        raise errors[0]


def _widen_the_window(cache, pause=0.002):
    """Hold every tracker read open for a moment, so the race is certain.

    The losing interleave is a thread switch between reading `self._tracker`
    and replacing it — a handful of bytecodes with no I/O in them, because
    `_save_tracker` reassigns the memo *before* it touches the disk. That is
    a real window (the interpreter preempts on its own switch interval, and
    a listening session runs these calls for hours), but it is far too narrow
    for a test to land on by throwing threads at it: with the lock taken out
    entirely, a million unsynchronised increments still came back correct.

    A test that only fails one run in a thousand is not a test, so this makes
    the scheduler's worst case happen every single time instead: sleeping
    inside the window guarantees a second writer gets in. With the lock, the
    second writer is stopped at the door and waits — the sleep only makes the
    test slower. Without it, every overlap loses an update and the assertions
    below fail flat.
    """
    original = cache._load_tracker

    def slow_load():
        tracks = original()
        time.sleep(pause)
        return tracks

    cache._load_tracker = slow_load
    return cache


class TestTrackerWritesAreSerialized:
    """One MetadataCache, many threads, and every write is read-modify-write.

    `note_played` runs on the playback monitor, `note_cached` on the download
    daemon and again on the refetch job, `forget_cached` on the bulk-download
    writer and inside `enforce_budget`, `reconcile` on a startup daemon. They
    all copy `self._tracker`, edit the copy and replace the whole thing, so
    unsynchronised they lose each other's edits wholesale.

    The old comment in cache.py priced that at "one play count". It is worse
    than that: a `note_played` straddling a `forget_cached` puts back a row
    for a file that has just been deleted, and a `forget_cached` straddling a
    `note_cached` deletes a row for a file that is really there. Both are the
    settings page and eviction believing the cache holds something it does
    not, which is exactly what the tracker exists to prevent.

    These are timing tests, so they are written to fail rather than to flake:
    the assertions are on exact final state, the work per thread is fixed, and
    every key belongs to exactly one thread except where the point is that it
    does not.
    """

    def test_two_threads_counting_plays_lose_none_of_them(self):
        """The purest form of a lost update: `plays` is read, incremented and
        written back, so an unlocked overlap silently makes two plays one."""
        cache = _widen_the_window(MetadataCache())
        rounds = 25

        def count():
            for _ in range(rounds):
                cache.note_played(12)

        _run_together([count, count])

        assert cache.audio_record(12)["plays"] == 2 * rounds, \
            "a play counted on one thread was overwritten by the other"

    def test_the_file_agrees_with_the_memo_when_the_threads_stop(self):
        """The tracker is only useful because it survives a restart, so the
        last writer must have written *its own* result, not a stale copy."""
        cache = _widen_the_window(MetadataCache())

        def count():
            for _ in range(15):
                cache.note_played(12)

        _run_together([count, count])

        assert MetadataCache().audio_record(12)["plays"] == 30

    def test_a_hammer_of_mixed_writers_loses_nothing(self):
        """Four writers doing four different jobs at once, on keys nobody else
        touches, so the whole final state is stated exactly.

        Distinct keys are the point: with no shared key there is no ambiguity
        about what the answer should be, and *every* difference from it is an
        edit some thread dropped on the floor.
        """
        cache = MetadataCache()
        rounds = 15

        # Pre-seeded rows for the deleting thread to take away. Separate ids
        # so that "was it removed" and "was it kept" are different questions.
        doomed = [str(900_000 + n) for n in range(rounds)]
        for key in doomed:
            _stamp(cache, key, plays=1, last=1.0, size=7)
        _widen_the_window(cache)  # after the seeding, which is single-threaded

        cached = [str(100_000 + n) for n in range(rounds)]
        played = [str(200_000 + n) for n in range(rounds)]
        both = [str(300_000 + n) for n in range(rounds)]

        def note_cached():
            for n, key in enumerate(cached):
                cache.note_cached(key, ".m4a", n, quality="LOSSLESS")

        def note_played():
            for key in played:
                cache.note_played(key)
                cache.note_played(key)

        def refetch():
            for key in both:
                cache.note_cached(key, ".flac", 11)
                cache.note_played(key)

        def forget():
            for key in doomed:
                cache.forget_cached([key])

        _run_together([note_cached, note_played, refetch, forget])

        tracker = MetadataCache()._load_tracker()
        assert set(tracker) == set(cached) | set(played) | set(both), \
            "a row was lost, or a deleted one came back"
        for n, key in enumerate(cached):
            assert tracker[key]["bytes"] == n
            assert tracker[key]["quality"] == "LOSSLESS"
            assert tracker[key]["plays"] == 0
        for key in played:
            assert tracker[key]["plays"] == 2
        for key in both:
            assert tracker[key]["plays"] == 1
            assert tracker[key]["ext"] == ".flac"

    def test_a_delete_beside_a_download_does_not_undo_it(self):
        """`forget_cached` decides what to remove inside the lock, so a row
        that arrived after the caller drew up its list is not swept up with
        it — the file is on disk and the tracker has to say so."""
        cache = MetadataCache()
        arrivals = [str(500_000 + n) for n in range(20)]
        for key in arrivals:
            _stamp(cache, key, size=3)
        _widen_the_window(cache)
        survivors = [str(600_000 + n) for n in range(20)]

        def download():
            for key in survivors:
                cache.note_cached(key, ".m4a", 3)

        def evict():
            for key in arrivals:
                cache.forget_cached([key])

        _run_together([download, evict])

        tracker = cache._load_tracker()
        assert not set(tracker) & set(arrivals), "an evicted row survived"
        assert set(survivors) <= set(tracker), \
            "a track that really landed was deleted by a concurrent eviction"

    def test_reconcile_beside_a_play_keeps_both(self):
        """`reconcile`'s directory walk is outside the lock and its
        load-modify-save is inside it, so a play counted while the disk was
        being read is edited rather than overwritten."""
        cache = MetadataCache()
        directory = cache_mod.audio_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "77.m4a").write_bytes(b"x" * 64)
        _widen_the_window(cache)

        def reconcile():
            for _ in range(15):
                cache.reconcile()

        def count():
            for _ in range(15):
                cache.note_played(77)

        _run_together([reconcile, count])

        record = cache.audio_record(77)
        assert record is not None, "reconcile dropped a file that is on disk"
        assert record["plays"] == 15, "reconcile overwrote counted plays"
        assert record["bytes"] == 64

    # ── the lock is at the leaves ──

    def test_evicting_and_clearing_do_not_take_the_lock_twice(self):
        """Both delete files and then hand the casualties to `forget_cached`,
        which is where the lock lives. A plain Lock is not reentrant, so a
        second acquire on the same path is a hang, not a failed assertion —
        hence the timeout rather than a bare call."""
        cache = MetadataCache(budget_gb=0)
        directory = cache_mod.audio_dir()
        directory.mkdir(parents=True, exist_ok=True)
        for tid in (12, 13):
            (directory / f"{tid}.m4a").write_bytes(b"x" * 64)
            _stamp(cache, tid, size=64)

        _run_together([cache.enforce_budget, cache.clear_audio], timeout=10.0)

        assert cache.cached_usage() == (0, 0)

    def test_reads_never_wait_on_a_writer(self):
        """The settings page reads `cached_usage` on every paint and playback
        reads `audio_record` before every track. Whole-dict replacement is
        what makes those safe without a lock, and they must stay that way —
        a read behind a disk write is a stutter on the UI thread.

        Read from another thread while this one pins the lock, so a read that
        did start taking it shows up as the deadlock it would be rather than
        wedging the suite.
        """
        cache = MetadataCache()
        cache.note_cached(12, ".m4a", 4096, quality="LOSSLESS")

        def read():
            assert cache.audio_record(12)["quality"] == "LOSSLESS"
            assert cache.cached_usage() == (1, 4096)
            assert cache.cheapest_resident() == (0, cache.audio_value(12)[1])

        with cache._tracker_lock:  # a writer is mid-cycle
            _run_together([read], timeout=5.0)

    def test_every_tracker_write_goes_through_one_place(self):
        """The lock is only worth anything if nothing writes around it, so
        `_mutate_tracker` is the sole caller of `_save_tracker`."""
        import inspect
        for name in ("note_cached", "note_played", "forget_cached", "reconcile"):
            source = inspect.getsource(getattr(MetadataCache, name))
            assert "_mutate_tracker" in source, f"{name} writes off the leash"
            assert "_save_tracker" not in source, \
                f"{name} saves the tracker without the lock"
        assert "_tracker_lock" in inspect.getsource(MetadataCache._mutate_tracker)


class TestPlayCountEviction:
    """Garrett's rule: a point per play, and the oldest among the tracks with
    the fewest plays goes first.

    The reason it is not plain LRU, in his words: a four-hour binge on a new
    playlist must not evict long-term staples. Under LRU it does.
    """

    def _song(self, cache, track_id, size, plays, last):
        directory = cache_mod.audio_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{track_id}.m4a"
        path.write_bytes(b"x" * size)
        _stamp(cache, track_id, plays=plays, last=last, size=size)
        return path

    def _cache(self, monkeypatch, budget=1):
        monkeypatch.setattr(cache_mod, "BYTES_PER_GB", 1024)
        return MetadataCache(budget_gb=budget)

    def test_the_staple_survives_the_binge(self, monkeypatch):
        """The whole design, in one test. The staple was last played days
        ago and would go first under LRU; it has twenty plays, so it stays."""
        cache = self._cache(monkeypatch)
        staple = self._song(cache, 1, 400, plays=20, last=1_000)
        binge = [self._song(cache, i, 400, plays=1, last=9_000 + i)
                 for i in range(2, 5)]

        cache.enforce_budget()

        assert staple.exists(), "a binge evicted a long-term staple"
        assert sum(1 for p in binge if p.exists()) < 3

    def test_within_the_same_play_count_the_oldest_goes(self, monkeypatch):
        cache = self._cache(monkeypatch)
        older = self._song(cache, 1, 700, plays=2, last=1_000)
        newer = self._song(cache, 2, 700, plays=2, last=2_000)

        cache.enforce_budget()

        assert not older.exists()
        assert newer.exists()

    def test_one_play_goes_before_two_however_recent(self, monkeypatch):
        cache = self._cache(monkeypatch)
        once = self._song(cache, 1, 700, plays=1, last=9_999)
        twice = self._song(cache, 2, 700, plays=2, last=1)

        cache.enforce_budget()

        assert not once.exists()
        assert twice.exists()

    def test_a_half_written_file_goes_before_any_song(self, monkeypatch):
        cache = self._cache(monkeypatch)
        song = self._song(cache, 1, 700, plays=0, last=0)
        directory = cache_mod.audio_dir()
        leak = directory / "999.part"
        leak.write_bytes(b"x" * 700)

        cache.enforce_budget()

        assert not leak.exists()
        assert song.exists()

    def test_the_stamp_is_ours_not_the_filesystems(self, monkeypatch):
        """`relatime` makes atime roughly daily-granular, which cannot order
        a listening session — so the ordering must not move when atime does."""
        cache = self._cache(monkeypatch)
        loved = self._song(cache, 1, 700, plays=5, last=1_000)
        spare = self._song(cache, 2, 700, plays=1, last=9_000)
        os.utime(loved, (1, 1))          # ancient by the filesystem's reckoning
        os.utime(spare, (2_000_000_000, 2_000_000_000))

        cache.enforce_budget()

        assert loved.exists(), "eviction fell back to atime"
        assert not spare.exists()

    def test_a_play_is_a_point_and_a_timestamp(self):
        cache = MetadataCache()
        before = time.time()
        cache.note_played(12)
        cache.note_played(12)
        record = cache.audio_record(12)
        assert record["plays"] == 2
        assert record["last"] >= before


class TestCacheAdmission:
    """Refuse only under pressure — Garrett's decision, 2026-07-26.

    With room in the budget nothing is refused, because at the moment a song
    starts you know almost nothing about it. Under pressure the candidate has
    to beat the cheapest thing already in there.
    """

    def _cache(self, monkeypatch, budget=1):
        monkeypatch.setattr(cache_mod, "BYTES_PER_GB", 1024)
        return MetadataCache(budget_gb=budget)

    def _resident(self, cache, track_id, size, plays, last):
        directory = cache_mod.audio_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{track_id}.m4a").write_bytes(b"x" * size)
        _stamp(cache, track_id, plays=plays, last=last, size=size)

    def test_with_room_to_spare_everything_is_cached(self, monkeypatch):
        cache = self._cache(monkeypatch)
        assert cache.should_cache(99, size=100) is True

    def test_an_empty_cache_never_refuses(self, monkeypatch):
        cache = self._cache(monkeypatch)
        assert cache.should_cache(99, size=100_000) is True

    def test_a_full_cache_of_staples_refuses_a_new_track(self, monkeypatch):
        cache = self._cache(monkeypatch)
        self._resident(cache, 1, 1024, plays=9, last=1_000)
        assert cache.should_cache(99, size=100) is False

    def test_a_full_cache_of_one_play_tracks_does_not_freeze(self, monkeypatch):
        """The failure mode this rule had to avoid: a brand-new track has zero
        plays, so a naive comparison refuses everything for ever and the cache
        freezes into whatever it held the day it filled. The song being played
        counts the play it is earning right now, so it displaces the oldest
        *other* one-play track and nothing else."""
        cache = self._cache(monkeypatch)
        self._resident(cache, 1, 1024, plays=1, last=1_000)
        assert cache.should_cache(99, size=100) is True

    def test_a_track_does_not_have_to_beat_itself(self, monkeypatch):
        """Replaying something already cached must not read as a newcomer
        losing to itself."""
        cache = self._cache(monkeypatch)
        self._resident(cache, 1, 1024, plays=1, last=1_000)
        assert cache.should_cache(1, size=100) is True

    def test_song_caching_off_refuses_everything(self, monkeypatch):
        cache = self._cache(monkeypatch)
        cache.songs = False
        assert cache.should_cache(99, size=1) is False

    def test_a_zero_budget_refuses_everything(self, monkeypatch):
        cache = self._cache(monkeypatch, budget=0)
        assert cache.should_cache(99, size=1) is False

    def test_the_rule_is_one_function(self):
        """Admission and eviction must not drift apart: both read
        `audio_value`, so a change to the rule is a change in one place."""
        import inspect
        source = inspect.getsource(MetadataCache.should_cache)
        assert "audio_value" in source
        assert "audio_value" in inspect.getsource(MetadataCache.enforce_budget)

    def test_the_decision_can_be_asked_at_any_moment(self):
        """Shaped for the scratch tier: the same question, asked at the end of
        a track instead of the start, must need no new rule — only better
        evidence, which is the play that has by then been counted."""
        cache = MetadataCache()
        at_start = cache.audio_value(12, playing=True)
        cache.note_played(12)
        at_end = cache.audio_value(12)
        assert at_end[0] == at_start[0], \
            "the play counted at track end must be the same one admission assumed"


class _RecordingAudio:
    """Stands in for AudioPlayer, recording what it was asked to play."""

    def __init__(self):
        self.plays = []
        self.is_paused = False

    def play_url(self, url, seek=0, title="", cache_key=None, local=None,
                 quality=None):
        self.plays.append({"url": url, "local": local, "quality": quality})


def _streaming_track(tid, calls, quality="LOSSLESS", url="https://cdn/x.mp4"):
    """A track whose `get_stream()` counts every time it is asked."""
    track = _track(tid)

    def get_stream():
        calls.append(tid)
        return types.SimpleNamespace(
            audio_quality=quality,
            get_stream_manifest=lambda: types.SimpleNamespace(
                is_bts=True, get_urls=lambda: [url]))

    track.get_stream = get_stream
    return track


def _cached_file(cache, track_id, quality="LOSSLESS", ext=".m4a", size=64):
    directory = cache_mod.audio_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{track_id}{ext}"
    path.write_bytes(b"x" * size)
    _stamp(cache, track_id, plays=1, last=time.time(), quality=quality,
           size=size, ext=ext)
    return path


class TestACachedTrackCostsNoRequest:
    """Playing something already on this disk must not spend a `playbackinfo`.

    Downloads were checked before the stream URL; the cache was not, so
    `play_url` only discovered the cached file *after* it had been handed a
    URL — and then ignored it. Replaying a 20-track cached playlist cost 20
    requests it threw away, plus up to 20 more from the prefetch, against the
    exact endpoint whose burst rate got the owner's IP blocked (INCIDENTS #1).
    """

    def _player_with(self, quality="HIGH"):
        p = HeadlessTidalPlayer(quality=quality)
        p.session = _FakeSession(latency=0)
        p._cache = MetadataCache(songs=True)
        p.audio = _RecordingAudio()
        return p

    def _play(self, p, track):
        p._play_track(track)
        assert _wait_for(lambda: p.audio.plays or not p._track_changing)
        _settle()

    def test_a_cached_track_makes_no_request_at_all(self):
        p = self._player_with()
        path = _cached_file(p._cache, 12)
        calls = []
        self._play(p, _streaming_track(12, calls))

        assert calls == [], "a cached track still asked TIDAL for a stream"
        assert p.audio.plays[0]["local"] == str(path)
        assert p.audio.plays[0]["url"] == ""

    def test_a_track_that_is_not_cached_still_costs_one(self):
        p = self._player_with()
        calls = []
        self._play(p, _streaming_track(12, calls))
        assert calls == [12]
        assert p.audio.plays[0]["url"] == "https://cdn/x.mp4"

    def test_a_file_deleted_by_hand_falls_through_to_the_network(self):
        """The tracker still claims it; the disk is what decides."""
        p = self._player_with()
        _cached_file(p._cache, 12).unlink()
        calls = []
        self._play(p, _streaming_track(12, calls))
        assert calls == [12]
        assert p.audio.plays[0]["local"] is None

    def test_the_prefetch_does_not_spend_one_either(self):
        p = self._player_with()
        _cached_file(p._cache, 13)
        calls = []
        upcoming = _streaming_track(13, calls)
        p._queue = [_track(12), upcoming]
        p._queue_index = 0
        p._current_track = p._queue[0]
        p._current_track.duration = 100
        p._playing = True
        p._play_offset = 95   # inside PREFETCH_LEAD of the end

        p._maybe_prefetch_next()
        _settle()

        assert calls == [], "the prefetch fetched a URL for a cached track"

    def test_the_granted_tier_is_recorded_against_the_file(self):
        p = self._player_with()
        calls = []
        self._play(p, _streaming_track(12, calls, quality="HIGH"))
        assert p.audio.plays[0]["quality"] == "HIGH"


class TestReplayingAtAHigherQuality:
    """A copy stored below the tier now selected is re-fetched; one stored
    above it is not; one whose tier was never recorded is left alone."""

    def _player_with(self, quality):
        p = HeadlessTidalPlayer(quality=quality)
        p.session = _FakeSession(latency=0)
        p._cache = MetadataCache(songs=True)
        p.audio = _RecordingAudio()
        return p

    def test_a_low_copy_is_re_fetched_at_the_higher_setting(self):
        p = self._player_with("MAX")
        _cached_file(p._cache, 12, quality="HIGH")
        calls = []
        p._play_track(_streaming_track(12, calls))
        assert _wait_for(lambda: p.audio.plays)
        _settle()
        assert calls == [12], "the old lower-quality copy was served instead"
        assert p.audio.plays[0]["local"] is None

    def test_a_higher_copy_is_never_thrown_away_for_a_lower_setting(self):
        """Listening at LOW for a while must not destroy a hi-res copy."""
        p = self._player_with("LOW")
        path = _cached_file(p._cache, 12, quality="HI_RES_LOSSLESS")
        calls = []
        p._play_track(_streaming_track(12, calls))
        assert _wait_for(lambda: p.audio.plays)
        _settle()
        assert calls == []
        assert p.audio.plays[0]["local"] == str(path)

    def test_an_unrecorded_tier_is_left_alone(self):
        """Every file cached before the tracker existed has no tier. Unknown
        is not evidence of a downgrade, and re-fetching someone's whole
        library on the strength of a missing field is not something to do
        unasked — the settings page has an action for exactly that."""
        p = self._player_with("MAX")
        path = _cached_file(p._cache, 12, quality=None)
        calls = []
        p._play_track(_streaming_track(12, calls))
        assert _wait_for(lambda: p.audio.plays)
        _settle()
        assert calls == []
        assert p.audio.plays[0]["local"] == str(path)

    def test_a_tier_tidal_will_not_serve_does_not_cause_a_re_fetch(self):
        """The gate has already seen a downgrade, so re-fetching would spend
        a request to be handed the same thing back."""
        p = self._player_with("MAX")
        p._quality_ceiling = "HIGH"
        path = _cached_file(p._cache, 12, quality="HIGH")
        calls = []
        p._play_track(_streaming_track(12, calls))
        assert _wait_for(lambda: p.audio.plays)
        _settle()
        assert calls == []
        assert p.audio.plays[0]["local"] == str(path)

    def test_the_replacement_leaves_no_second_copy_behind(self, monkeypatch):
        """`_cached_audio_path` globs by stem, so a re-fetch that lands under
        a different extension would leave the stale file as a possible
        answer."""
        _fake_get(monkeypatch, content_type="audio/flac")
        audio = player_mod.AudioPlayer("mpv", cache=MetadataCache(songs=True))
        stale = cache_mod.audio_dir()
        stale.mkdir(parents=True, exist_ok=True)
        (stale / "12.m4a").write_bytes(b"old")

        audio._start_download(URL, 12, audio._download_gen, quality="LOSSLESS")

        assert _wait_for(lambda: (stale / "12.flac").exists())
        _settle()
        assert not (stale / "12.m4a").exists(), "the old copy is still there"
        assert audio.cache.audio_record(12)["quality"] == "LOSSLESS"


class TestPlaysAreCountedOnce:
    def _player(self):
        p = HeadlessTidalPlayer()
        p._cache = MetadataCache(songs=True)
        p._current_track = _track(12)
        p._current_track.duration = 200
        p._playing = True
        return p

    def test_a_skip_is_not_a_play(self):
        p = self._player()
        p._play_offset = 5
        p._maybe_count_play()
        assert p._cache.audio_record(12) is None

    def test_a_listen_is(self):
        p = self._player()
        p._play_offset = cache_mod.PLAY_COUNTS_AFTER + 1
        p._maybe_count_play()
        assert p._cache.audio_record(12)["plays"] == 1

    def test_it_is_counted_once_however_many_ticks_go_past(self):
        p = self._player()
        p._play_offset = cache_mod.PLAY_COUNTS_AFTER + 1
        for _ in range(20):
            p._maybe_count_play()
        assert p._cache.audio_record(12)["plays"] == 1

    def test_the_next_track_gets_its_own_point(self):
        p = self._player()
        p._play_offset = cache_mod.PLAY_COUNTS_AFTER + 1
        p._maybe_count_play()
        p._play_gen += 1  # what _play_track does
        p._maybe_count_play()
        assert p._cache.audio_record(12)["plays"] == 2

    def test_a_short_track_counts_at_half_way(self):
        p = self._player()
        p._current_track.duration = 20
        p._play_offset = 11
        p._maybe_count_play()
        assert p._cache.audio_record(12)["plays"] == 1

    def test_metadata_only_mode_counts_nothing(self):
        p = self._player()
        p._cache = MetadataCache(metadata=True, songs=False)
        p._play_offset = 500
        p._maybe_count_play()
        assert p._cache.audio_record(12) is None


class TestDisableSongsPrompt:
    """Disabling song caching and clearing what is on disk are separate
    concerns, so the prompt has three answers, not two."""

    def _settings(self):
        p = _player()
        p._mode = p.MODE_SETTINGS
        p._settings_cursor = [s["key"] for s in config_mod.SETTINGS_ROWS].index("cache_songs")
        return p

    def _song(self, name="12.m4a"):
        path = cache_mod.audio_dir()
        path.mkdir(parents=True, exist_ok=True)
        f = path / name
        f.write_bytes(b"x" * 32)
        return f

    def test_turning_it_off_asks_first_and_changes_nothing_yet(self):
        p = self._settings()
        song = self._song()

        p._handle_settings_key(player_mod.KEY_RIGHT)

        assert p._disable_songs_pending is True
        assert p.config["cache_songs"] is True
        assert p._cache.keeps_audio is True
        assert song.exists()
        assert "Clear cached songs as well?" in p._build_display().renderable.plain

    def test_yes_disables_and_clears(self):
        p = self._settings()
        song = self._song()
        p._handle_settings_key(player_mod.KEY_RIGHT)

        p._handle_key("y")

        assert p._disable_songs_pending is False
        assert p.config["cache_songs"] is False
        assert p._cache.keeps_audio is False
        assert not song.exists()
        assert p._cache.audio_count() == 0

    def test_enter_is_also_yes(self):
        p = self._settings()
        song = self._song()
        p._handle_settings_key(player_mod.KEY_RIGHT)

        p._handle_key(player_mod.KEY_ENTER)

        assert p.config["cache_songs"] is False
        assert not song.exists()

    def test_no_disables_but_keeps_the_files(self):
        p = self._settings()
        song = self._song()
        p._handle_settings_key(player_mod.KEY_RIGHT)

        p._handle_key("n")

        assert p._disable_songs_pending is False
        assert p.config["cache_songs"] is False, "no means keep the files, not keep caching"
        assert p._cache.keeps_audio is False
        assert song.exists()
        assert json.loads(config_mod.CONFIG_FILE.read_text())["cache_songs"] is False
        assert p._cache.audio_count() == 1

    def test_esc_cancels_the_whole_thing(self):
        p = self._settings()
        song = self._song()
        p._handle_settings_key(player_mod.KEY_RIGHT)

        p._handle_key(player_mod.KEY_ESC)

        assert p._disable_songs_pending is False
        assert p.config["cache_songs"] is True, "a cancel must not toggle at all"
        assert p._cache.keeps_audio is True
        assert song.exists()
        assert not config_mod.CONFIG_FILE.exists(), "a cancel must not write the config"

    def test_an_unrecognised_key_cancels(self):
        p = self._settings()
        song = self._song()
        p._handle_settings_key(player_mod.KEY_RIGHT)

        p._handle_key("z")

        assert p.config["cache_songs"] is True
        assert song.exists()

    def test_turning_it_back_on_asks_nothing(self):
        p = self._settings()
        p._handle_settings_key(player_mod.KEY_RIGHT)
        p._handle_key("y")

        p._handle_settings_key(player_mod.KEY_RIGHT)

        assert p._disable_songs_pending is False
        assert p.config["cache_songs"] is True
        assert p._cache.keeps_audio is True


class TestClearCacheAction:
    """A user-invokable clear, independent of the toggle. An action, not a
    value, so it is a keybinding like logout rather than a SETTINGS_SPEC row."""

    def _settings(self):
        p = _player()
        p._mode = p.MODE_SETTINGS
        return p

    def _song(self, name="12.m4a"):
        path = cache_mod.audio_dir()
        path.mkdir(parents=True, exist_ok=True)
        f = path / name
        f.write_bytes(b"x" * 32)
        return f

    def test_the_page_offers_the_action(self):
        p = self._settings()
        self._song()
        p._cache.invalidate_audio_count()
        rendered = p._build_settings_display().plain
        # like "[o] log out", with what the cache is actually costing
        # like "[o] log out", with what the cache is actually costing
        assert "Cache         1 song  · 0.000 GB of 2.000 GB   [x] clear" \
            in rendered

    def test_the_footer_still_fits_eighty_columns(self):
        """The settings footer is already exactly as wide as the panel allows,
        which is why the clear action is a line rather than another hint.

        It runs to a second line at 80 columns now that `[h] hide` is on it,
        which is what the fit's two hint rows are for. What must still be true
        is that nothing overflows the panel and that every hint arrives whole —
        `[Esc] back` above all, since it is the way off the page.
        """
        from rich.console import Console

        p = self._settings()
        console = Console(width=80)
        with console.capture() as cap:
            console.print(p._build_display())
        out = cap.get().splitlines()
        assert [line for line in out if "[↑/↓] select" in line], "footer not found"
        assert all(len(line) <= 80 for line in out)
        # The hint rows the player laid out for this window, every one of them
        # on the screen verbatim: two at most, and none of them clipped
        hints = [line.plain.strip() for line in p._build_hints(p._fit)]
        assert 1 <= len(hints) <= 2, hints
        for line in hints:
            assert "…" not in line, line
            assert any(line in row for row in out), (line, out)
        assert any("[Esc] back" in line for line in hints), "no way off the page"

    def test_x_asks_before_deleting(self):
        p = self._settings()
        song = self._song()
        p._cache.invalidate_audio_count()

        p._handle_settings_key("x")

        assert p._clear_cache_pending is True
        assert song.exists(), "nothing goes until the answer comes back"
        assert "Delete 1 cached song?" in p._build_display().renderable.plain

    def test_y_clears(self):
        p = self._settings()
        songs = [self._song(f"{i}.m4a") for i in (12, 13)]

        p._handle_settings_key("x")
        p._handle_key("y")

        assert p._clear_cache_pending is False
        assert not any(f.exists() for f in songs)
        assert p._cache.audio_count() == 0
        assert "Cleared 2 songs" in p._toast

    def test_any_other_key_cancels(self):
        p = self._settings()
        song = self._song()

        p._handle_settings_key("x")
        p._handle_key("n")

        assert p._clear_cache_pending is False
        assert song.exists()

    def test_clearing_leaves_the_toggle_alone(self):
        """Clearing is not disabling — new tracks are still kept afterwards."""
        p = self._settings()
        self._song()

        p._handle_settings_key("x")
        p._handle_key("y")

        assert p.config["cache_songs"] is True
        assert p._cache.keeps_audio is True

    def test_a_decoy_survives_the_action(self):
        p = self._settings()
        song = self._song()
        decoy = cache_mod.audio_dir() / "notes.txt"
        decoy.write_bytes(b"mine")

        p._handle_settings_key("x")
        p._handle_key("y")

        assert not song.exists()
        assert decoy.exists()

    def test_a_file_that_cannot_go_is_reported_not_hidden(self, monkeypatch):
        """Windows can refuse to unlink a file another process holds open.
        The toast has to say so rather than claim a clean sweep."""
        p = self._settings()
        stuck = self._song()
        monkeypatch.setattr(
            cache_mod.Path, "unlink",
            lambda self, *a, **kw: (_ for _ in ()).throw(PermissionError(self)))

        p._handle_settings_key("x")
        p._handle_key("y")

        assert stuck.exists()
        assert "still in use" in p._toast
        assert p._cache.audio_count() == 1, "the count must match the disk"

    def test_the_key_collides_with_nothing_already_bound(self):
        """x must not already mean something on this page or in the player."""
        p = _player()
        p._mode = p.MODE_PLAYER
        before = (p._mode, p._mini_player, p._show_more, p._quit_pending)
        p._handle_player_key("x")
        assert (p._mode, p._mini_player, p._show_more, p._quit_pending) == before


class TestDiskUsageOnScreen:
    """The song count answers "how many"; the byte total answers "how much" —
    the question the budget row above it is about."""

    def _settings(self):
        p = _player()
        p._mode = p.MODE_SETTINGS
        return p

    def _song(self, name="12.m4a", size=32):
        path = cache_mod.audio_dir()
        path.mkdir(parents=True, exist_ok=True)
        f = path / name
        f.write_bytes(b"x" * size)
        return f

    def test_gigabytes_to_three_decimals(self):
        gb = cache_mod.BYTES_PER_GB
        assert cache_mod.format_gb(0) == "0.000 GB"
        assert cache_mod.format_gb(45 * 1024 ** 2) == "0.044 GB"
        assert cache_mod.format_gb(12 * gb + gb // 2) == "12.500 GB"
        # Two decimals would round a handful of tracks away to nothing
        assert cache_mod.format_gb(2 * 1024 ** 2) == "0.002 GB"

    def test_an_empty_cache_reads_zero(self):
        p = self._settings()
        rendered = p._build_settings_display().plain
        assert "Cache         0 songs · 0.000 GB of 2.000 GB" in rendered

    def test_the_total_is_what_is_really_on_disk(self):
        p = self._settings()
        self._song("12.m4a", size=3 * 1024 ** 2)
        self._song("13.m4a", size=1024 ** 2)
        p._cache.invalidate_audio_count()
        assert "Cache         2 songs · 0.004 GB" \
            in p._build_settings_display().plain

    def test_the_row_fits_eighty_columns_even_when_full(self, monkeypatch):
        """A full cache reads longer than an empty one — 12.500 GB across a
        four-figure song count is the widest this line can get."""
        from rich.console import Console

        p = self._settings()
        monkeypatch.setattr(p._cache, "audio_count", lambda: 9999)
        monkeypatch.setattr(
            p._cache, "disk_bytes", lambda: 12 * cache_mod.BYTES_PER_GB + 1024 ** 3 // 2)
        console = Console(width=80)
        with console.capture() as cap:
            console.print(p._build_display())
        lines = cap.get().splitlines()
        assert all(len(line) <= 80 for line in lines)
        matched = [l for l in lines if "[x] clear" in l]
        assert matched, "the cache line is missing"
        assert "9999 songs · 12.500 GB" in matched[0], "the row wrapped"

    def test_it_is_measured_once_and_remembered(self, monkeypatch):
        """No stat-per-frame: the settings page repaints on every keystroke."""
        cache = MetadataCache()
        calls = []
        real = cache.total_bytes
        monkeypatch.setattr(
            cache, "total_bytes", lambda: (calls.append(1), real())[1])

        assert cache.disk_bytes() == cache.disk_bytes() == cache.disk_bytes()
        assert len(calls) == 1

    def test_a_download_landing_moves_the_number(self):
        p = self._settings()
        assert "0.000 GB" in p._build_settings_display().plain
        self._song("12.m4a", size=64 * 1024 ** 2)
        p._cache.invalidate_audio_count()  # what AudioPlayer calls on a keep
        assert "0.062 GB" in p._build_settings_display().plain

    def test_clearing_the_cache_moves_the_number_back(self):
        p = self._settings()
        self._song("12.m4a", size=64 * 1024 ** 2)
        p._cache.invalidate_audio_count()

        p._handle_settings_key("x")
        p._handle_key("y")

        assert "Cache         0 songs · 0.000 GB" \
            in p._build_settings_display().plain

    def test_eviction_moves_the_number_back(self):
        cache = MetadataCache(budget_gb=0)
        d = cache_mod.audio_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "12.m4a").write_bytes(b"x" * (4 * 1024 ** 2))
        assert cache.disk_bytes() > 0

        cache.enforce_budget()

        assert cache.disk_bytes() == 0
        assert cache.audio_count() == 0


class TestClearWhilePlaying:
    """"Clear cache should clear cache": a song being read is deleted too, and
    playback survives it."""

    def test_unlinking_a_file_being_read_succeeds_and_the_reader_keeps_it(self):
        """POSIX semantics, asserted rather than assumed: the descriptor
        outlives the name, which is why a playing track is safe to delete."""
        cache = MetadataCache()
        path = cache_mod.audio_dir()
        path.mkdir(parents=True, exist_ok=True)
        song = path / "12.m4a"
        song.write_bytes(b"audio-bytes")

        with open(song, "rb") as reading:
            removed, kept = cache.clear_audio()
            assert (removed, kept) == (1, 0), "an open file must still be cleared"
            assert not song.exists()
            assert reading.read() == b"audio-bytes", "the reader keeps its bytes"

    def test_a_playing_process_survives_its_cached_file_being_cleared(self):
        """The real thing: a live child process reading the file keeps going
        after the clear. Uses a local player-shaped process, no audio device
        and no network."""
        import subprocess
        import sys as _sys

        cache = MetadataCache()
        path = cache_mod.audio_dir()
        path.mkdir(parents=True, exist_ok=True)
        song = path / "12.m4a"
        song.write_bytes(b"z" * 4096)

        # Opens the file, then keeps reading it slowly — what a player does
        proc = subprocess.Popen(
            [_sys.executable, "-c",
             "import sys,time\n"
             "f=open(sys.argv[1],'rb')\n"
             "time.sleep(0.2)\n"
             "assert len(f.read())==4096\n"
             "time.sleep(1.5)\n", str(song)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.1)  # let it open the file
            removed, kept = cache.clear_audio()

            assert (removed, kept) == (1, 0)
            assert not song.exists()
            time.sleep(0.4)
            assert proc.poll() is None, "playback died when its file was cleared"
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_a_player_that_had_not_opened_it_yet_is_restarted_not_skipped(self):
        """The race the existing source_vanished/_monitor_playback path was
        written for: the file went before the player opened it, so the process
        exits at once and the track resumes from the network."""
        from ticli.player import AudioPlayer

        audio = AudioPlayer("mpv", cache=MetadataCache())
        audio._cache_file = str(cache_mod.audio_dir() / "12.m4a")
        audio._cache_persistent = True
        assert audio.source_vanished() is True, "a cleared file must be noticed"

        p = _player()
        p.audio = audio
        p._current_track = types.SimpleNamespace(id=12, name="T", duration=200)
        p._playing = True
        p._play_offset = 30
        p._play_start_time = None
        restarted = []
        p._play_track = lambda track, seek=0: restarted.append((track.id, seek))

        # What _monitor_playback does once the process has been dead two polls
        if p.audio.source_vanished() and p._track_has_time_left():
            p._play_track(p._current_track, seek=p._get_position())

        assert restarted == [(12, 30)], "the track must resume, not be skipped"

    def test_a_track_that_had_finished_is_not_restarted(self):
        """Clearing mid-track leaves the file gone at the natural end too —
        that is an advance, not a rescue."""
        p = _player()
        p._current_track = types.SimpleNamespace(id=12, name="T", duration=200)
        p._play_offset = 199.5
        p._play_start_time = None
        assert p._track_has_time_left() is False
        p._play_offset = 100
        assert p._track_has_time_left() is True

    def test_an_unknown_duration_still_resumes(self):
        p = _player()
        p._current_track = types.SimpleNamespace(id=12, name="T", duration=0)
        p._play_offset = 500
        p._play_start_time = None
        assert p._track_has_time_left() is True


class TestRealHttpDownload:
    """One end-to-end over loopback, so the bytes are proven to come off a
    real socket through real `requests` — the fake above can only prove the
    writing half."""

    def test_bytes_off_the_wire_are_byte_identical(self):
        import http.server
        import threading as _threading
        from ticli.player import AudioPlayer

        payload = bytes(range(256)) * 400  # 102,400 bytes, several chunks

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "audio/mp4")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        _threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            audio = AudioPlayer("mpv", cache=MetadataCache(songs=True))
            url = f"http://127.0.0.1:{server.server_address[1]}/track.mp4"
            audio._start_download(url, 99, audio._download_gen)
            assert _wait_for(lambda: _audio_files())
            _settle()
            landed = _audio_files()
            assert [f.name for f in landed] == ["99.m4a"]
            assert landed[0].read_bytes() == payload
        finally:
            server.shutdown()
            server.server_close()


class TestRepaintWake:
    """Data that arrives in the background has to reach the screen, not wait
    for the next idle tick."""

    def test_a_wake_returns_the_input_wait_at_once(self):
        import os
        import select

        p = _player()
        p._wake_r, p._wake_w = os.pipe()
        os.set_blocking(p._wake_r, False)
        # A stdin that never has anything to say, so only the wake can end
        # the wait
        stdin_r, stdin_w = os.pipe()
        try:
            fake_stdin = types.SimpleNamespace(fileno=lambda: stdin_r)
            import sys as sys_mod
            old = sys_mod.stdin
            sys_mod.stdin = fake_stdin
            try:
                p._wake()
                start = time.monotonic()
                assert p._read_keys(select, timeout=1.0) == []
                assert time.monotonic() - start < 0.2, "the wake did not cut the wait short"
                # And with nothing pending it still waits out the timeout
                start = time.monotonic()
                assert p._read_keys(select, timeout=0.2) == []
                assert time.monotonic() - start >= 0.15
            finally:
                sys_mod.stdin = old
        finally:
            for fd in (p._wake_r, p._wake_w, stdin_r, stdin_w):
                os.close(fd)

    def test_wake_is_a_no_op_before_the_loop_starts(self):
        _player()._wake()  # must not raise


class TestLocalSearchIndex:
    def test_every_cached_track_is_scannable_by_text(self):
        """What a future "search my own playlists" needs: one flat pass over
        the index, with the playlist each hit came from."""
        session = _FakeSession()
        p = _player(session)
        _load_playlists(p)
        _open(p, session._playlists[0])
        _open(p, session._playlists[1])

        rows = list(MetadataCache().iter_tracks())
        assert {pid for pid, _ in rows} == {"p1", "p2"}
        assert len(rows) == 8
        hits = [r for _, r in rows if "track 7" in r["name"].lower()]
        assert len(hits) == 1
        # Artist and album are stored as text, so they are searchable too
        assert hits[0]["artists"] == ["Artist 7"]
        assert hits[0]["album"] == "Album 7"
