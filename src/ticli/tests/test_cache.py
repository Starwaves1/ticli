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

    monkeypatch.setattr(player_mod.requests, "get", _get)
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
        monkeypatch.setattr(player_mod.requests, "get", _boom)
        audio = self._audio()

        audio._start_download(URL, 12, audio._download_gen)
        _settle()

        assert _audio_files(pending=True) == []
        assert audio._cache_file is None

    def test_a_half_written_file_is_never_served_as_whole(self):
        audio = self._audio()
        path = cache_mod.audio_dir()
        path.mkdir(parents=True, exist_ok=True)
        (path / "12.m4a.part").write_bytes(BODY[:10])
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
        # measured: what is under test is "least recently used goes first",
        # and nothing about that should depend on when the test ran, how long
        # a download thread took, or whether a sweep it started is still in
        # flight. The version of this that raced one was flaky about 1 run in 4.
        audio = self._audio()
        directory = cache_mod.audio_dir()
        directory.mkdir(parents=True, exist_ok=True)
        old = directory / "12.m4a"
        old.write_bytes(BODY)
        os.utime(old, (1_000_000, 1_000_000))
        # A second track that only overshoots the budget with the first there
        big = directory / "13.m4a"
        big.write_bytes(b"x" * 1_040_000)
        os.utime(big, (2_000_000, 2_000_000))
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
        self._song("3.m4a.part")  # still downloading — not a song yet
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
        assert cache_mod.is_owned_audio("12345.m4a.part")
        assert not cache_mod.is_owned_audio("important.txt")
        assert not cache_mod.is_owned_audio("mixtape.m4a")
        assert not cache_mod.is_owned_audio("12345")
        assert not cache_mod.is_owned_audio(".part")

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
        part = self._write("12.m4a.part")
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


class TestDisableSongsPrompt:
    """Disabling song caching and clearing what is on disk are separate
    concerns, so the prompt has three answers, not two."""

    def _settings(self):
        p = _player()
        p._mode = p.MODE_SETTINGS
        p._settings_cursor = [s["key"] for s in config_mod.SETTINGS_SPEC].index("cache_songs")
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
        assert "1 song cached · 0.000 GB   [x] clear cache" in rendered

    def test_the_footer_still_fits_eighty_columns(self):
        """The settings footer is already exactly as wide as the panel allows,
        which is why the clear action is a line rather than another hint."""
        from rich.console import Console

        p = self._settings()
        console = Console(width=80)
        with console.capture() as cap:
            console.print(p._build_display())
        lines = [line for line in cap.get().splitlines() if "[↑/↓] select" in line]
        assert lines, "footer not found"
        assert all(len(line) <= 80 for line in cap.get().splitlines())
        assert "[Esc] back" in lines[0], "the footer must not wrap at 80 columns"

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
        assert "0 songs cached · 0.000 GB" in rendered

    def test_the_total_is_what_is_really_on_disk(self):
        p = self._settings()
        self._song("12.m4a", size=3 * 1024 ** 2)
        self._song("13.m4a", size=1024 ** 2)
        p._cache.invalidate_audio_count()
        assert "2 songs cached · 0.004 GB" in p._build_settings_display().plain

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
        matched = [l for l in lines if "clear cache" in l]
        assert matched, "the cache line is missing"
        assert "9999 songs cached · 12.500 GB" in matched[0], "the row wrapped"

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

        assert "0 songs cached · 0.000 GB" in p._build_settings_display().plain

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
