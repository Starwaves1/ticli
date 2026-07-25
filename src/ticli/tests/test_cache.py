"""Tests for the on-disk metadata cache.

The point of the cache is that a playlist you have opened before paints with
no network wait at all, while still converging on the truth — so these tests
run a fake session with deliberate latency and assert on both the timing and
the eventual contents. Nothing here touches the network or the real cache
directory: CACHE_DIR and the config file are both redirected to tmp_path.
"""

import json
import time
import types

import pytest

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
    """Keep every player built here off the real config and cache directories."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config" / "config.json")
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    return tmp_path


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
    def _audio_file(self, size, name="a"):
        path = cache_mod.audio_dir()
        path.mkdir(parents=True, exist_ok=True)
        f = path / f"{name}.audio"
        f.write_bytes(b"\0" * size)
        return f

    def test_eviction_brings_the_directory_under_budget(self):
        cache = MetadataCache(budget_mb=1)
        files = [self._audio_file(400_000, name=str(i)) for i in range(5)]
        assert cache.total_bytes() > 1024 * 1024

        freed = cache.enforce_budget()

        assert freed > 0
        assert cache.total_bytes() <= 1024 * 1024
        assert any(f.exists() for f in files), "eviction must not empty the cache"

    def test_least_recently_used_audio_goes_first(self):
        cache = MetadataCache(budget_mb=1)
        old = self._audio_file(700_000, name="old")
        new = self._audio_file(700_000, name="new")
        past = time.time() - 10_000
        import os
        os.utime(old, (past, past))

        cache.enforce_budget()

        assert not old.exists()
        assert new.exists()

    def test_a_zero_budget_evicts_metadata_too(self):
        cache = MetadataCache(budget_mb=0)
        cache.put("playlist:p1", [{"id": 1, "name": "T"}])
        cache.put("playlist:p2", [{"id": 2, "name": "U"}])
        cache.enforce_budget()
        assert cache.total_bytes() == 0 or cache.get("playlist:p1") is None

    def test_writing_enforces_the_budget_without_being_asked(self):
        cache = MetadataCache(budget_mb=1)
        self._audio_file(2_000_000, name="big")
        cache.put("playlist:p1", [{"id": 1, "name": "T"}])
        assert cache.total_bytes() <= 1024 * 1024

    def test_lowering_the_setting_evicts_immediately(self):
        p = _player()
        self._audio_file(2_000_000, name="big")
        p.config["cache_budget_mb"] = 1
        p._apply_setting("cache_budget_mb", 1)
        assert p._cache.total_bytes() <= 1024 * 1024

    def test_the_default_budget_is_under_two_gigabytes(self):
        assert 0 < config_mod.DEFAULTS["cache_budget_mb"] < 2048


class TestCacheModes:
    def test_off_writes_nothing_and_reads_nothing(self):
        session = _FakeSession()
        p = _player(session)
        p.config["cache_mode"] = "OFF"
        p._apply_setting("cache_mode", "OFF")
        _load_playlists(p)
        assert not cache_mod.index_file().exists()

        warm = _load_playlists(p)
        assert warm >= LATENCY * 0.8, "OFF must not serve a cached first paint"

    def test_switching_off_clears_what_was_already_stored(self):
        session = _FakeSession()
        p = _player(session)
        _load_playlists(p)
        assert cache_mod.index_file().exists()

        p._apply_setting("cache_mode", "OFF")

        assert not cache_mod.index_file().exists()
        assert MetadataCache().get_playlists() is None

    def test_only_full_keeps_audio(self):
        assert MetadataCache(mode="METADATA").keeps_audio is False
        assert MetadataCache(mode="FULL").keeps_audio is True
        assert MetadataCache(mode="OFF").enabled is False

    def test_the_settings_page_offers_the_cache_rows(self):
        keys = [spec["key"] for spec in config_mod.SETTINGS_SPEC]
        assert "cache_mode" in keys and "cache_budget_mb" in keys
        assert config_mod.coerce(config_mod.get_spec("cache_budget_mb"), 99_999) == 8192
        assert config_mod.coerce(config_mod.get_spec("cache_mode"), "nonsense") == "METADATA"


class TestAudioRetention:
    """The budget is only honest if audio actually lands in the directory it
    sizes. These drive AudioPlayer's bookkeeping directly — no ffmpeg, no
    ffplay, no network."""

    def _audio(self, mode):
        from ticli.player import AudioPlayer
        return AudioPlayer("ffplay", cache=MetadataCache(mode=mode))

    def test_audio_is_only_stored_in_full_mode(self):
        assert self._audio("METADATA")._cached_audio_path(12) is None
        assert self._audio("OFF")._cached_audio_path(12) is None
        path = self._audio("FULL")._cached_audio_path(12)
        assert path and path.endswith("12.audio")
        assert cache_mod.audio_dir() in cache_mod.Path(path).parents

    def test_a_finished_download_survives_the_track_ending(self):
        audio = self._audio("FULL")
        kept = audio._cached_audio_path(12)
        part = kept + ".part"
        cache_mod.Path(part).write_bytes(b"flac" * 100)
        audio._cache_file = part
        audio._keep_file = kept
        audio._cache_process = types.SimpleNamespace(poll=lambda: 0, terminate=lambda: None)

        audio.stop()

        assert cache_mod.Path(kept).exists()
        assert not cache_mod.Path(part).exists()

    def test_an_interrupted_download_is_thrown_away(self):
        audio = self._audio("FULL")
        kept = audio._cached_audio_path(12)
        part = kept + ".part"
        cache_mod.Path(part).write_bytes(b"half a track")
        audio._cache_file = part
        audio._keep_file = kept
        audio._cache_process = types.SimpleNamespace(poll=lambda: None, terminate=lambda: None)

        audio.stop()

        assert not cache_mod.Path(part).exists()
        assert not cache_mod.Path(kept).exists()

    def test_a_stored_track_is_not_deleted_when_it_is_replayed(self):
        audio = self._audio("FULL")
        kept = cache_mod.Path(audio._cached_audio_path(12))
        kept.write_bytes(b"whole track")
        audio._cache_file = str(kept)
        audio._cache_persistent = True

        audio.stop()

        assert kept.exists()

    def test_retaining_a_track_sweeps_the_budget(self):
        audio = self._audio("FULL")
        audio.cache.budget_mb = 0
        kept = audio._cached_audio_path(12)
        cache_mod.Path(kept + ".part").write_bytes(b"x" * 5000)
        audio._cache_file = kept + ".part"
        audio._keep_file = kept
        audio._cache_process = types.SimpleNamespace(poll=lambda: 0, terminate=lambda: None)

        audio.stop()

        assert audio.cache.total_bytes() == 0


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
