"""Offline-first (ADR-0009, ADR-0003): starting without network from stored tokens,
reconnecting only when an action needs TIDAL, refusing writes, playing what is on
disk, and the metadata cache that makes browsing work offline."""

import json
import threading
import time
import types

import pytest
import requests

from ticli import agentq
from ticli import player as player_mod
from ticli.commands import AGENT, HUMAN, OFFLINE, ONLINE, SIGNED_OUT
from ticli.player import HeadlessTidalPlayer
from ticli.utils import cache as cache_mod
from ticli.utils import downloads
from ticli.utils.cache import MetadataCache, age_label

TOKENS = {"token_type": "Bearer", "access_token": "stored", "refresh_token": "r",
          "expiry_time": None, "is_pkce": True}


def _track(tid, name=None):
    return types.SimpleNamespace(id=tid, name=name or f"Track {tid}", duration=200,
                                 artists=[types.SimpleNamespace(name="Artist")], album=None)


class _Session:
    """Counts every call; `fail` decides what a load does."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
        self.user = None
        self.is_pkce = False
        self.access_token = None

    def load_oauth_session(self, token_type, access_token, refresh_token=None,
                           expiry_time=None, is_pkce=False):
        self.calls.append("sessions")
        if self.fail is not None:
            raise self.fail
        self.access_token = access_token
        self.user = types.SimpleNamespace(id=1, first_name="Ada", last_name=None,
                                          favorites=types.SimpleNamespace(tracks=lambda limit=999: []))
        return True

    def check_login(self):
        self.calls.append("check_login")
        return True

    def __getattr__(self, name):
        raise AssertionError(f"request attempted while offline: session.{name}")


def _dead():
    return requests.exceptions.ConnectionError("Max retries exceeded: Name or service not known")


def _unauthorized():
    response = types.SimpleNamespace(status_code=401)
    error = requests.HTTPError("401 Client Error: Unauthorized")
    error.response = response
    return error


@pytest.fixture
def tokens(monkeypatch):
    stored = {"value": dict(TOKENS)}
    monkeypatch.setattr(player_mod, "load_tokens", lambda: stored["value"])
    monkeypatch.setattr(player_mod, "save_tokens", lambda data: stored.update(value=data))
    return stored


def _offline_player(tokens, fail=None):
    p = HeadlessTidalPlayer()
    p.session = _Session(fail=_dead())
    p._wake = lambda: None
    assert p._login(interactive=False)
    p.session.fail = fail
    p._plays = []
    p._play_track = lambda track, seek=0, **kw: p._plays.append(track.id)
    return p


class TestOfflineStart:
    def test_stored_tokens_and_no_network_start_offline(self, tokens):
        p = HeadlessTidalPlayer()
        p.session = _Session(fail=_dead())
        assert p._login(interactive=False) is True
        assert p._connectivity == OFFLINE
        assert p.session.access_token == "stored" and p.session.is_pkce is True
        assert p.snapshot()["connectivity"] == OFFLINE

    def test_an_interactive_login_does_not_pretend(self, tokens, monkeypatch):
        p = HeadlessTidalPlayer()
        p.session = _Session(fail=_dead())
        p.session.login_oauth = lambda: (_ for _ in ()).throw(_dead())
        printed = []
        p.console = types.SimpleNamespace(print=lambda *a, **k: printed.append(" ".join(map(str, a))))
        tokens["value"] = None
        assert p._login() is False
        assert any("Can't reach TIDAL to sign in" in line for line in printed)

    def test_status_reports_connectivity(self, tokens):
        p = _offline_player(tokens)
        status = p.commands.execute("status", caller=HUMAN)["result"]
        assert status["connectivity"] == OFFLINE
        assert agentq.compact_state(status)["connectivity"] == OFFLINE


class TestReconnect:
    def test_idle_offline_makes_no_requests(self, tokens):
        p = _offline_player(tokens)
        before = list(p.session.calls)
        p._load_favorites()
        p._maybe_prefetch_next()
        assert not p._advance(1, automatic=True)
        assert p.session.calls == before

    def test_an_action_reconnects_once(self, tokens):
        p = _offline_player(tokens)
        p.session.fail = None
        p._load_favorites = lambda: None
        assert p._reconnect() == ONLINE
        assert p.session.calls.count("sessions") == 2  # the start, then this action

    def test_still_offline_stays_offline_and_keeps_answering(self, tokens):
        p = _offline_player(tokens, fail=_dead())
        assert p._reconnect() == OFFLINE
        assert p._reconnect() == OFFLINE
        assert p.session.calls.count("sessions") == 3  # one per action, never by itself

    def test_concurrent_actions_share_one_attempt(self, tokens):
        p = _offline_player(tokens)
        gate = threading.Event()
        original = p.session.load_oauth_session

        def slow(*a, **k):
            gate.wait(2)
            p.session.fail = _dead()
            return original(*a, **k)

        p.session.__dict__["load_oauth_session"] = slow
        results = []
        threads = [threading.Thread(target=lambda: results.append(p._reconnect())) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        gate.set()
        for t in threads:
            t.join(3)
        assert results == [OFFLINE] * 4
        assert p.session.calls.count("sessions") == 2

    def test_a_401_is_signed_out_not_logged_out(self, tokens, monkeypatch):
        deleted = []
        monkeypatch.setattr("ticli.utils.credential_store.delete_tokens", lambda: deleted.append(1))
        p = _offline_player(tokens, fail=_unauthorized())
        assert p._reconnect() == SIGNED_OUT
        assert p.running is True and deleted == []
        assert tokens["value"]["access_token"] == "stored"
        assert "[o]" in p._toast
        assert p._reconnect() == SIGNED_OUT
        assert p.session.calls.count("sessions") == 2, "a dead token waits for a human sign-in"
        result = p.commands.execute("like", {"track_ids": [1]}, caller=AGENT)
        assert result["code"] == "signed_out" and "[o]" in result["fix"]

    def test_signing_in_again_brings_it_back(self, tokens):
        p = _offline_player(tokens, fail=_unauthorized())
        p._reconnect()
        p.session.fail = None
        p._load_favorites = lambda: None
        p.commands.execute("login.reload", caller=HUMAN)
        assert p._connectivity == ONLINE and p._toast == "Signed in again"


class TestRefusals:
    @pytest.mark.parametrize("name,args", [
        ("like", {"track_ids": [1]}), ("unlike", {"track_ids": [1]}),
        ("playlist.add", {"id": "p", "track_ids": [1]}), ("playlist.create", {"name": "N"}),
        ("download", {"track_ids": [1]}), ("play.radio", {}),
    ])
    def test_writes_are_refused_not_queued(self, tokens, name, args):
        p = _offline_player(tokens, fail=_dead())
        p._current_track = _track(1)
        result = p.commands.execute(name, args, caller=AGENT)
        assert result["ok"] is False and result["code"] == "offline"
        assert "downloaded/cached" in result["fix"]
        assert not p._picker_busy

    def test_a_read_never_opened_says_offline(self, tokens):
        p = _offline_player(tokens, fail=_dead())
        result = p.commands.execute("album.tracks", {"id": 5}, caller=AGENT)
        assert result["code"] == "offline"

    def test_a_read_opened_before_answers_from_disk_with_its_age(self, tokens, monkeypatch):
        p = _offline_player(tokens, fail=_dead())
        p._cache.put_items("album:5", [_track(51), _track(52)])
        later = time.time() + 3 * 86400
        monkeypatch.setattr(cache_mod.time, "time", lambda: later)
        result = p.commands.execute("album.tracks", {"id": 5}, caller=AGENT)["result"]
        assert [t.id for t in result["tracks"]] == [51, 52]
        assert result["offline"] is True and result["age"] == "cached 3 days ago"

    def test_search_offline_searches_your_own_music(self, tokens):
        p = _offline_player(tokens, fail=_dead())
        p._cache.put_playlists([types.SimpleNamespace(id="p1", name="Road trip", num_tracks=1,
                                                      creator=None)])
        p._cache.put_playlist_tracks("p1", [_track(7, "Highway Song")])
        result = p.commands.execute("search", {"query": "highway"}, caller=AGENT)["result"]
        assert result["source"] == "local" and [t.id for t in result["tracks"]] == [7]

    def test_agent_replies_suggest_what_plays_from_disk(self):
        state = {"connectivity": OFFLINE, "playing": False, "track": None}
        forms = agentq.next_forms("search", {"tracks": [{"id": 9}]}, state)
        assert forms[:2] == ["play track 9", "play downloads 0"]
        assert not any(f.startswith(("like", "playlist add")) for f in forms)


def _download(tid, title):
    relative = f"Artist/Album/{title}.m4a"
    path = downloads.download_dir() / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    downloads.record(tid, relative, "HIGH", 1)


class TestPlaybackOffline:
    def test_next_and_auto_advance_skip_what_is_not_on_disk(self, tokens):
        p = _offline_player(tokens, fail=_dead())
        _download(3, "Three")
        p._queue = [_track(1), _track(2), _track(3), _track(4)]
        p._queue_index = 0
        assert p._advance(1, automatic=True)
        assert p._plays == [3] and p._queue_index == 2
        assert p._toast == "Offline — skipped 1 track with no local copy"
        assert not p._advance(1)
        assert "nothing further" in p._toast
        assert p.session.calls == ["sessions"]

    def _pick_from_album(self, p, index):
        p._run = lambda name, **args: p.commands.execute(name, args, caller=HUMAN)
        p._browse_tracks = [_track(11), _track(12), _track(13)]
        p._lists[("album", "9")] = list(p._browse_tracks)
        p._browse_source = ("album", "9")
        p._play_browse(index)

    def test_picking_a_track_with_no_local_copy_tries_the_network_then_skips(self, tokens):
        p = _offline_player(tokens, fail=_dead())
        _download(13, "Thirteen")
        self._pick_from_album(p, 0)
        assert p.session.calls.count("sessions") == 2, "one reconnect attempt for the pick"
        assert p._plays == [13] and p._queue_index == 2
        assert p._toast == "Offline — skipped 2 tracks with no local copy"

    def test_picking_a_track_when_the_network_is_back_plays_that_track(self, tokens):
        p = _offline_player(tokens, fail=None)
        p._load_favorites = lambda: None
        _download(13, "Thirteen")
        self._pick_from_album(p, 0)
        assert p._connectivity == ONLINE
        assert p._plays == [11] and p._queue_index == 0

    def test_picking_a_local_copy_offline_needs_no_network(self, tokens):
        p = _offline_player(tokens, fail=_dead())
        _download(12, "Twelve")
        self._pick_from_album(p, 1)
        assert p.session.calls == ["sessions"]
        assert p._plays == [12]

    def test_a_list_played_offline_starts_on_its_first_local_copy(self, tokens):
        p = _offline_player(tokens, fail=_dead())
        _download(12, "Twelve")
        p._cache.put_items("album:9", [_track(11), _track(12)])
        assert p.commands.execute("play.album", {"id": 9}, caller=HUMAN)["result"]["index"] == 1
        assert p._plays == [12]
        assert "skipped 1 track" in p._toast

    def test_a_list_with_nothing_local_is_refused_offline(self, tokens):
        p = _offline_player(tokens, fail=_dead())
        p._cache.put_items("album:9", [_track(11)])
        assert p.commands.execute("play.album", {"id": 9})["code"] == "offline"
        assert p._plays == []

    def test_a_low_tier_cached_copy_still_plays_offline(self, tokens):
        p = _offline_player(tokens, fail=_dead())
        p._quality_name = "MAX"
        path = cache_mod.audio_dir() / "5.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        p._cache.note_cached(5, ".m4a", 1, quality="LOW")
        assert p._local_source(_track(5))[0] == str(path)

    def test_the_downloads_screen_plays(self, tokens):
        p = _offline_player(tokens, fail=_dead())
        p._run = lambda name, **args: p.commands.execute(name, args, caller=HUMAN)
        _download(21, "Older")
        time.sleep(0.01)
        _download(22, "Newer")
        p._open_downloads()
        p._handle_downloads_key(player_mod.KEY_DOWN)
        p._handle_downloads_key(player_mod.KEY_ENTER)
        assert p._plays == [21]
        assert [t.id for t in p._queue] == [22, 21]

    def test_my_music_is_the_offline_search_scope(self, tokens):
        p = _offline_player(tokens, fail=_dead())
        _download(31, "Night Drive")
        p._handle_player_key("s")
        assert p._search_filter == "music"
        p._search_query = "night"
        p._do_search()
        assert [r["obj"].id for r in p._search_results] == [31]


class TestTheTui:
    def test_offline_lists_show_their_age(self, monkeypatch):
        ui = HeadlessTidalPlayer()
        ui._fetch = lambda *a, **k: None
        ui._cache.put_items("album:5", [_track(51)])
        later = time.time() + 3 * 86400
        monkeypatch.setattr(cache_mod.time, "time", lambda: later)
        ui._open_album(types.SimpleNamespace(id=5, name="An Album"))
        assert [t.id for t in ui._browse_tracks] == [51], "painted from disk at once"
        assert "cached" not in ui._build_browse_display().plain
        ui._apply_state({"connectivity": OFFLINE})
        assert "cached 3 days ago" in ui._build_browse_display().plain
        assert "offline" in ui._build_display().title

    def test_signed_out_says_how_to_sign_in(self):
        ui = HeadlessTidalPlayer()
        ui._apply_state({"connectivity": SIGNED_OUT})
        assert "[o] sign in" in ui._build_display().title
        called = []
        ui._sign_in_again = lambda: called.append(1)
        ui._handle_player_key("o")
        assert called == [1]


class TestMetadataCache:
    def _entry(self, cache, key, used, size=2000):
        real = time.time
        cache_mod.time.time = lambda: used
        try:
            cache.put(key, [{"id": key, "name": "x" * size}])
        finally:
            cache_mod.time.time = real

    def _kept(self, *keys):
        fresh = MetadataCache()
        return {k for k in keys if fresh.get(k) is not None}

    def test_eviction_takes_the_least_recently_opened_first_and_the_library_last(self):
        cache = MetadataCache(metadata_cap=10_000)
        cache.put("playlists", [{"id": "mine", "name": "Mine"}])
        self._entry(cache, "playlist:mine", used=1)
        self._entry(cache, "favorites:tracks", used=2)
        self._entry(cache, "album:old", used=3)
        self._entry(cache, "album:newer", used=4)
        self._entry(cache, "search:q", used=5)
        keys = ("playlists", "playlist:mine", "favorites:tracks", "album:old", "album:newer", "search:q")
        assert self._kept(*keys) == set(keys) - {"album:old"}, "least recently opened goes first"
        assert lists_size() <= 10_000

    def test_the_library_goes_only_when_nothing_else_is_left(self):
        cache = MetadataCache(metadata_cap=5_000)
        cache.put("playlists", [{"id": "mine", "name": "Mine"}])
        self._entry(cache, "playlist:mine", used=1)
        self._entry(cache, "album:a", used=2)
        self._entry(cache, "album:b", used=3)
        self._entry(cache, "playlist:other", used=4)
        assert self._kept("playlist:mine", "album:a", "album:b", "playlist:other") == \
            {"playlist:mine", "playlist:other"}

    def test_eviction_goes_below_the_cap_so_the_next_write_is_free(self):
        cache = MetadataCache(metadata_cap=10_000)
        for i in range(5):
            self._entry(cache, f"album:{i}", used=i + 1)
        assert lists_size() <= 9_000
        kept = self._kept(*(f"album:{i}" for i in range(5)))
        self._entry(cache, "search:tiny", used=6, size=10)
        assert self._kept(*(f"album:{i}" for i in range(5))) == kept

    def test_opening_a_list_protects_it(self):
        cache = MetadataCache(metadata_cap=5_000)
        self._entry(cache, "album:a", used=1)
        self._entry(cache, "album:b", used=2)
        cache.get("album:a")
        self._entry(cache, "album:c", used=time.time())
        assert self._kept("album:a", "album:b", "album:c") == {"album:a", "album:c"}

    def test_an_open_writes_nothing(self):
        cache = MetadataCache()
        cache.put("album:a", [{"id": 1}])
        stamps = {f.name: f.stat().st_mtime_ns for f in cache_mod.lists_dir().iterdir()}
        for _ in range(5):
            MetadataCache().get("album:a")
            cache.get("album:a")
        assert {f.name: f.stat().st_mtime_ns for f in cache_mod.lists_dir().iterdir()} == stamps

    def test_a_read_opens_only_its_own_file(self, monkeypatch):
        writer = MetadataCache()
        for key in ("favorites:tracks", "album:1", "album:2", "playlists"):
            writer.put(key, [{"id": key, "name": key}])
        opened = []
        real = cache_mod.Path.read_text
        monkeypatch.setattr(cache_mod.Path, "read_text",
                            lambda self, *a, **k: opened.append(self.name) or real(self, *a, **k))
        assert [t.id for t in MetadataCache().get_items("favorites:tracks")] == ["favorites:tracks"]
        assert opened == [cache_mod.list_file("favorites:tracks").name]

    def test_the_cap_is_100_mb(self):
        assert MetadataCache().cap_bytes == 100 * 1024 * 1024

    def test_age_labels(self):
        now = 1_000_000
        assert age_label(now - 5, now) == "cached just now"
        assert age_label(now - 60, now) == "cached 1 minute ago"
        assert age_label(now - 7200, now) == "cached 2 hours ago"
        assert age_label(now - 3 * 86400, now) == "cached 3 days ago"

    def test_an_old_cache_file_still_loads(self):
        cache_mod.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        long_ago = time.time() - 90 * 86400
        cache_mod.index_file().write_text(json.dumps({"version": 1, "entries": {
            "playlists": {"fetched": long_ago, "data": [{"id": "p1", "name": "Road trip",
                                                         "num_tracks": 1, "editable": True}]},
            "playlist:p1": {"fetched": long_ago, "used": long_ago,
                            "data": [{"id": 7, "name": "Song", "artists": ["A"]}]}}}))
        cache = MetadataCache()
        assert [p.name for p in cache.get_playlists()] == ["Road trip"]
        assert [t.name for t in cache.get_playlist_tracks("p1")] == ["Song"]
        assert [t.id for t in cache.get_items("playlist:p1")] == [7]
        assert cache.fetched_at("playlist:p1") == pytest.approx(long_ago)

    def test_an_old_single_file_index_is_split_once_then_removed(self):
        cache_mod.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_mod.index_file().write_text(json.dumps({"version": 1, "entries": {
            "playlists": {"fetched": 1, "used": 1, "data": [{"id": "p1", "name": "Mine"}]},
            "playlist:p1": {"fetched": 1, "used": 1, "data": [{"id": 7, "name": "Song"}]},
            "album:9": {"fetched": 2, "used": 2, "data": [{"id": 9, "name": "Other"}]}}}))
        MetadataCache().get("album:9")
        assert not cache_mod.index_file().exists()
        for key in ("playlists", "playlist:p1", "album:9"):
            assert cache_mod.list_file(key).exists()
        MetadataCache(metadata_cap=150).put("search:x", [])
        assert self._kept("playlist:p1", "album:9") == {"playlist:p1"}, \
            "migrated lists are sized, dated and still know the library"

    def test_a_write_that_dies_mid_manifest_never_corrupts_it(self, monkeypatch):
        cache = MetadataCache()
        cache.put("album:1", [{"id": 1}])
        good = cache_mod.manifest_file().read_bytes()
        real_replace = cache_mod.os.replace

        def crash(src, dst):
            if str(dst).endswith("manifest.json"):
                raise OSError("disk pulled")
            real_replace(src, dst)
        monkeypatch.setattr(cache_mod.os, "replace", crash)
        cache.put("album:2", [{"id": 2}])
        monkeypatch.setattr(cache_mod.os, "replace", real_replace)

        assert cache_mod.manifest_file().read_bytes() == good
        assert not list(cache_mod.lists_dir().glob(".*.tmp")), "no temp file left behind"
        fresh = MetadataCache()
        assert [t.id for t in fresh.get_items("album:1")] == [1]
        fresh.put("album:3", [{"id": 3}])
        assert self._kept("album:1", "album:3") == {"album:1", "album:3"}

    def test_a_torn_list_file_is_just_missing(self):
        cache = MetadataCache()
        cache.put("album:1", [{"id": 1}])
        cache.put("album:2", [{"id": 2}])
        path = cache_mod.list_file("album:1")
        path.write_text(path.read_text()[:10])
        assert MetadataCache().get("album:1") is None
        assert [t.id for t in MetadataCache().get_items("album:2")] == [2]

    def test_another_process_s_write_is_seen(self):
        reader, writer = MetadataCache(), MetadataCache()
        assert reader.get("album:1") is None
        writer.put_items("album:1", [_track(1)])
        assert [t.id for t in reader.get_items("album:1")] == [1]
        writer.put_items("album:1", [_track(2)])
        assert [t.id for t in reader.get_items("album:1")] == [2]
        writer.clear_metadata()
        assert reader.get("album:1") is None

    def test_another_process_s_eviction_is_seen(self):
        reader, writer = MetadataCache(), MetadataCache(metadata_cap=3_000)
        self._entry(writer, "album:old", used=1)
        assert reader.get("album:old") is not None
        self._entry(writer, "album:new", used=2)
        assert reader.get("album:old") is None


def lists_size() -> int:
    return sum(f.stat().st_size for f in cache_mod.lists_dir().glob("*.json")
               if f.name != "manifest.json")
