"""Tests for the download tier.

The rule these are written to (ai/WORKING-RULES.md, and INCIDENTS #2 behind
it) is that a test asserts observable reality. A download is bytes in the
user's music folder, so that is what is asserted: the file exists, its bytes
equal the bytes the server sent, its header is the container its extension
claims, and the tags read back out of it. Two of the cases run over a real
loopback HTTP server through real `requests`, because the fake can only ever
prove the writing half.

Nothing here touches the network or the owner's real `~/Music` — the download
root is redirected by the package-wide conftest fixture and again here, and
the cache directory and config file are redirected alongside it.
"""

import http.server
import json
import struct
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
from ticli.utils import downloads, tags
from ticli.utils.cache import MetadataCache


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config" / "config.json")
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(downloads, "DOWNLOAD_ROOT", tmp_path / "Music" / "Ticli")
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    return tmp_path


def _wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return False


def _track(tid=42, duration=200, cover=None):
    album = types.SimpleNamespace(
        name="Random Access Memories", artist=types.SimpleNamespace(name="Daft Punk"),
        num_tracks=13, num_volumes=1, release_date=types.SimpleNamespace(year=2013),
        cover=cover,
    )
    return types.SimpleNamespace(
        id=tid, name="Get Lucky", duration=duration,
        artists=[types.SimpleNamespace(name="Daft Punk"),
                 types.SimpleNamespace(name="Pharrell Williams")],
        album=album, track_num=8, volume_num=1,
        isrc="USQX91300110", copyright="2013 Daft Life Ltd.",
    )


def _player(quality="HIGH"):
    p = HeadlessTidalPlayer(quality=quality)
    p.session = types.SimpleNamespace(audio_quality=None, is_pkce=False,
                                      track=lambda tid: None)
    p._cache = MetadataCache(songs=True)
    return p


class _Server:
    """A loopback HTTP server serving a fixed map of path → bytes."""

    def __init__(self, routes):
        self.routes = routes
        self.hits = []
        # One entry per accepted TCP connection, which is what a keep-alive
        # session buys and a fresh `requests.get` per segment does not
        self.connections = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            # HTTP/1.0 closes after every response, so without this the
            # connection count could never tell the two apart
            protocol_version = "HTTP/1.1"

            def setup(self):
                outer.connections.append(1)
                return super().setup()

            def do_GET(self):
                outer.hits.append(self.path)
                body = outer.routes.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/mp4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path):
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# ── a real, minimal MP4, built here so the tests need no ffmpeg ──


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


AUDIO = bytes(range(256)) * 300  # 76,800 bytes of "samples"


def _mp4(audio: bytes = AUDIO, moov_last: bool = False) -> bytes:
    """ftyp + moov + mdat, with a real stco pointing at the audio.

    The stco is the point: it is an *absolute* file offset, so a tagger that
    grows moov without moving it produces a file that opens and plays nothing.
    """
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    mdat_header = 8

    def build(stco_offset):
        stbl = _box(b"stco", struct.pack(">IiI", 0, 1, stco_offset))
        moov = _box(b"moov", _box(b"mvhd", b"\x00" * 100) + _box(
            b"trak", _box(b"mdia", _box(b"minf", _box(b"stbl", stbl)))))
        return moov

    if moov_last:
        mdat = _box(b"mdat", audio)
        audio_at = len(ftyp) + mdat_header
        return ftyp + mdat + build(audio_at)
    # Two passes: the offset depends on moov's length, which is fixed here
    moov = build(0)
    audio_at = len(ftyp) + len(moov) + mdat_header
    moov = build(audio_at)
    return ftyp + moov + _box(b"mdat", audio)


def _boxes(data: bytes) -> list:
    """Top-level boxes, parsed by hand — the test does not reuse the parser
    it is testing."""
    out = []
    pos = 0
    while pos < len(data):
        size = struct.unpack_from(">I", data, pos)[0]
        out.append((data[pos + 4:pos + 8], pos, size))
        assert size >= 8 and pos + size <= len(data), "box overruns the file"
        pos += size
    return out


def _stco_offset(data: bytes) -> int:
    at = data.index(b"stco") + 4
    return struct.unpack_from(">I", data, at + 8)[0]


def _ilst_text(data: bytes, atom: bytes):
    """Read one text atom back out of a tagged MP4, by hand."""
    at = data.find(atom)
    if at < 0:
        return None
    # atom: [size][name][data box: size 'data' type(4) locale(4) payload]
    size = struct.unpack_from(">I", data, at - 4)[0]
    payload = data[at + 4 + 16:at - 4 + size]
    return payload.decode("utf-8")


# ── size estimates ──


class TestEstimatesCostNothing:
    def test_every_tier_is_answered_with_no_network_call_at_all(self, monkeypatch):
        def _forbidden(*a, **kw):
            raise AssertionError("a size estimate made a network request")

        patch_get(monkeypatch, player_mod, _forbidden)
        p = _player()
        p._download_track = _track(duration=197)
        estimates = {tier: p._download_estimate(tier)
                     for tier in ("LOW", "HIGH", "LOSSLESS", "HIRES")}
        assert all(v > 0 for v in estimates.values())
        # Strictly increasing: a higher tier is never a smaller file
        assert list(estimates.values()) == sorted(estimates.values())

    def test_the_arithmetic_is_duration_times_nominal_bitrate(self):
        # 197 s at 320 kbps: the research measured the real file at 7,931,562
        # bytes and this estimate at 7,880,000 — a 0.65% under-read
        assert downloads.estimate_bytes(197, "HIGH") == 7_880_000

    def test_an_unknown_duration_says_so_rather_than_showing_zero(self):
        assert downloads.estimate_bytes(0, "HIGH") == 0
        assert downloads.format_bytes(0) == "—"

    def test_the_screen_shows_all_four_estimates_and_one_action(self, monkeypatch):
        patch_get(monkeypatch, player_mod,
                  lambda *a, **k: pytest.fail("no requests here"))
        p = _player(quality="LOSSLESS")
        p._download_track = _track(duration=197)
        p._download_cursor = 2  # LOSSLESS
        text = p._build_download_display().plain
        import re
        tiers = ("LOW", "HIGH", "LOSSLESS", "HIRES")
        rows = {}
        for line in text.splitlines():
            words = line.replace("▸", "").split()
            if words and words[0] in tiers:
                rows[words[0]] = line
        assert set(rows) == {"LOW", "HIGH", "LOSSLESS", "HIRES"}
        for tier, line in rows.items():
            assert re.search(r"~\d+(\.\d+)? [KMG]B", line), \
                f"{tier} shows no size estimate of its own"
        assert text.count("download now") == 1, "only the hovered tier acts"
        assert "FLAC is variable" in text, "the ~ is qualified for the tier hovered"


class TestTheCursorStartsWhereSettingsIs:
    def test_pre_placed_on_the_tier_in_settings(self):
        for quality, index in (("LOW", 0), ("HIGH", 1), ("LOSSLESS", 2), ("HIRES", 3)):
            p = _player(quality=quality)
            p._current_track = _track()
            p._open_download()
            assert p._download_cursor == index
            assert p._mode == p.MODE_DOWNLOAD


class TestTheKeyIsFree:
    def test_d_collides_with_nothing_in_any_mode(self):
        """Every mode's handler is read for its own bindings, so a later
        feature taking `d` shows up here rather than in use."""
        import inspect
        p = _player()
        for name in ("_handle_player_key", "_handle_browse_key",
                     "_handle_artist_key", "_handle_queue_key"):
            source = inspect.getsource(getattr(p, name))
            assert source.count('== "d"') == 1, f"{name} should open downloads"
        # Search types every printable key, so `d` must never be a command there
        search = inspect.getsource(p._handle_search_key)
        assert '"d"' not in search

    def test_d_opens_the_screen_from_the_queue_on_the_row_under_the_cursor(self):
        p = _player()
        p._mode = p.MODE_QUEUE
        p._queue = [_track(1), _track(2)]
        p._queue_cursor = 1
        p._handle_key("d")
        assert p._mode == p.MODE_DOWNLOAD
        assert p._download_track.id == 2


# ── the bytes ──


def _download(p, tier="HIGH"):
    p._start_download_job(tier)
    assert _wait_for(lambda: (p._download_job or {}).get("state") != "running"), \
        "the download never finished"
    return p._download_job


class TestASingleFileDownload:
    def test_the_file_lands_with_the_bytes_the_server_sent(self):
        payload = _mp4()
        server = _Server({"/track.mp4": payload})
        try:
            p = _player()
            track = _track()
            track.get_stream = lambda: types.SimpleNamespace(
                audio_quality="HIGH",
                get_stream_manifest=lambda: types.SimpleNamespace(
                    is_bts=True, get_urls=lambda: [server.url("/track.mp4")]))
            p._download_track = track
            job = _download(p)
            assert job["state"] == "done", job.get("error")

            path = downloads.download_dir() / "Daft Punk" / \
                "Random Access Memories" / "08 Get Lucky.m4a"
            assert path.is_file(), "the download is not where the screen said"
            # Tagging rewrote the container, so compare the audio rather than
            # the whole file: every mdat byte must survive untouched
            written = path.read_bytes()
            assert written[written.index(b"mdat") + 4:][:len(AUDIO)] == AUDIO
        finally:
            server.close()

    def test_the_header_is_the_container_the_extension_claims(self):
        server = _Server({"/track.mp4": _mp4()})
        try:
            p = _player()
            track = _track()
            track.get_stream = lambda: types.SimpleNamespace(
                audio_quality="HIGH",
                get_stream_manifest=lambda: types.SimpleNamespace(
                    is_bts=True, get_urls=lambda: [server.url("/track.mp4")]))
            p._download_track = track
            assert _download(p)["state"] == "done"

            path = downloads.download_dir() / "Daft Punk" / \
                "Random Access Memories" / "08 Get Lucky.m4a"
            data = path.read_bytes()
            kinds = [k for k, _o, _s in _boxes(data)]
            assert kinds[0] == b"ftyp", "an .m4a must start with an ftyp box"
            assert b"moov" in kinds and b"mdat" in kinds
            assert data[8:12] == b"isom"
            # ...and the box lengths account for every byte, which is what a
            # demuxer actually needs
            last = _boxes(data)[-1]
            assert last[1] + last[2] == len(data)
        finally:
            server.close()

    def test_the_chunk_offset_still_points_at_the_audio_after_tagging(self):
        """The failure mode this whole tagger is written around: growing moov
        moves mdat, and a stco left behind makes a file that opens and plays
        silence."""
        server = _Server({"/track.mp4": _mp4()})
        try:
            p = _player()
            track = _track()
            track.get_stream = lambda: types.SimpleNamespace(
                audio_quality="HIGH",
                get_stream_manifest=lambda: types.SimpleNamespace(
                    is_bts=True, get_urls=lambda: [server.url("/track.mp4")]))
            p._download_track = track
            assert _download(p)["state"] == "done"

            data = (downloads.download_dir() / "Daft Punk" /
                    "Random Access Memories" / "08 Get Lucky.m4a").read_bytes()
            offset = _stco_offset(data)
            assert data[offset:offset + len(AUDIO)] == AUDIO
        finally:
            server.close()


class TestASegmentedDownload:
    """A lossless stream arrives as MPEG-DASH: an initialization segment and N
    media segments which, written end to end, *are* the file."""

    def test_the_segments_concatenate_into_the_whole_track(self):
        init = _mp4(audio=b"")[:120]
        parts = [b"A" * 5000, b"B" * 5000, b"C" * 1234]
        server = _Server({"/init.mp4": init, "/s1.mp4": parts[0],
                          "/s2.mp4": parts[1], "/s3.mp4": parts[2]})
        try:
            playlist = player_mod._hls_playlist(types.SimpleNamespace(
                urls=[server.url("/init.mp4"), server.url("/s1.mp4"),
                      server.url("/s2.mp4"), server.url("/s3.mp4")],
                first_url=server.url("/init.mp4"),
                timescale=44100, chunk_size=441000, last_chunk_size=220500))
            path = player_mod._write_hls_playlist(7, playlist)

            p = _player()
            track = _track(tid=7)
            track.get_stream = lambda: types.SimpleNamespace(
                audio_quality="LOSSLESS",
                get_stream_manifest=lambda: types.SimpleNamespace(
                    is_bts=False, dash_info=None))
            p._download_track = track
            p._stream_url = lambda t: path  # the manifest is already written

            job = _download(p, "LOSSLESS")
            assert job["state"] == "done", job.get("error")
            landed = downloads.download_dir() / "Daft Punk" / \
                "Random Access Memories" / "08 Get Lucky.m4a"
            assert landed.read_bytes() == init + b"".join(parts), \
                "the concatenation is not the sum of its segments"
            # Every segment was fetched, initialization first
            assert server.hits == ["/init.mp4", "/s1.mp4", "/s2.mp4", "/s3.mp4"]
        finally:
            server.close()


class TestOneConnectionPerTrack:
    """46 requests to one host should not be 46 TLS handshakes.

    The real cached hi-res track (FLAC 24/48, 175.9 s, 29,575,234 bytes)
    parses to 45 media segments plus an initialization segment. Measured over
    loopback HTTPS, 46 x 657 KB, median of 5: 0.250 s with a fresh
    `requests.get` per segment against 0.032 s with one `Session`, and with a
    40 ms per-connection setup delay modelling TCP+TLS at a 20 ms RTT CDN,
    2.430 s against 0.086 s — **+2.3 s and 45 avoidable handshakes per track**.
    """

    def _segmented(self, count=8):
        init = _mp4(audio=b"")[:120]
        parts = [bytes([65 + i]) * 4000 for i in range(count)]
        routes = {"/init.mp4": init}
        routes.update({f"/s{i}.mp4": part for i, part in enumerate(parts)})
        return init, parts, _Server(routes)

    def test_every_segment_of_a_track_shares_one_connection(self):
        init, parts, server = self._segmented()
        try:
            urls = [server.url("/init.mp4")] + \
                [server.url(f"/s{i}.mp4") for i in range(len(parts))]
            part = str(cache_mod.CACHE_DIR / "seg.part")
            cache_mod.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            player_mod.fetch_to_file(urls, part)

            assert len(server.hits) == 9, "every segment should still be fetched"
            assert open(part, "rb").read() == init + b"".join(parts)
            assert len(server.connections) == 1, \
                f"{len(server.connections)} connections for one track"
        finally:
            server.close()

    def test_the_pool_is_closed_when_a_download_is_abandoned(self, monkeypatch):
        """Per call, not module-level: an abandoned download must not leave a
        connection pool alive behind it."""
        class _Response:
            headers = {"Content-Type": "audio/mp4"}

            def raise_for_status(self):
                pass

            def iter_content(self, size):
                yield b"x" * 100

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

        sessions = patch_get(monkeypatch, player_mod, lambda *a, **k: _Response())
        part = str(cache_mod.CACHE_DIR / "abandoned.part")
        cache_mod.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with pytest.raises(player_mod._DownloadSuperseded):
            player_mod.fetch_to_file(["https://cdn/1.mp4"], part,
                                     abandoned=lambda: True)
        assert sessions and all(s.closed for s in sessions)

    def test_a_segment_that_fails_still_raises(self, monkeypatch):
        """The failure path is unchanged: a 4xx mid-track raises where it
        always did, out of `raise_for_status`."""
        init, parts, server = self._segmented(count=3)
        try:
            urls = [server.url("/init.mp4"), server.url("/s0.mp4"),
                    server.url("/gone.mp4"), server.url("/s2.mp4")]
            part = str(cache_mod.CACHE_DIR / "broken.part")
            cache_mod.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with pytest.raises(Exception):
                player_mod.fetch_to_file(urls, part)
            assert "/s2.mp4" not in server.hits, "it kept going after a failure"
        finally:
            server.close()


class TestPartialFilesAreNeverServed:
    def test_the_destination_does_not_exist_until_the_bytes_are_whole(self, monkeypatch):
        """A `.part` under a dot-prefixed scratch name, renamed only when the
        download is complete — so nothing can list or play half a track."""
        seen = []
        gate = threading.Event()

        class _Response:
            headers = {"Content-Type": "audio/mp4"}

            def raise_for_status(self):
                pass

            def iter_content(self, size):
                yield b"x" * 1000
                seen.append(sorted(pp.name for pp in
                                   downloads.download_dir().rglob("*")
                                   if pp.is_file()))
                gate.set()
                time.sleep(0.2)
                yield b"y" * 1000

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

        patch_get(monkeypatch, player_mod, lambda *a, **k: _Response())
        p = _player()
        track = _track()
        track.get_stream = lambda: types.SimpleNamespace(
            audio_quality="HIGH",
            get_stream_manifest=lambda: types.SimpleNamespace(
                is_bts=True, get_urls=lambda: ["https://cdn.example/t.mp4"]))
        p._download_track = track
        p._start_download_job("HIGH")
        assert gate.wait(3)

        # Mid-flight: nothing but the scratch file, and it is not the name a
        # listing or a lookup would ever return
        assert seen == [[".ticli-42.part"]]
        assert downloads.path_for(42) is None
        assert _wait_for(lambda: (p._download_job or {}).get("state") == "done")
        assert downloads.path_for(42) is not None
        assert not list(downloads.download_dir().rglob("*.part"))

    def test_an_abandoned_download_leaves_nothing_behind(self, monkeypatch):
        class _Response:
            headers = {"Content-Type": "audio/mp4"}

            def raise_for_status(self):
                pass

            def iter_content(self, size):
                for _ in range(50):
                    yield b"z" * 1000
                    time.sleep(0.02)

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

        patch_get(monkeypatch, player_mod, lambda *a, **k: _Response())
        p = _player()
        track = _track()
        track.get_stream = lambda: types.SimpleNamespace(
            audio_quality="HIGH",
            get_stream_manifest=lambda: types.SimpleNamespace(
                is_bts=True, get_urls=lambda: ["https://cdn.example/t.mp4"]))
        p._download_track = track
        p._mode = p.MODE_DOWNLOAD
        p._start_download_job("HIGH")
        assert _wait_for(lambda: list(downloads.download_dir().rglob("*.part")))
        p._handle_key("x")  # cancel
        assert p._download_job["state"] == "cancelled"
        assert _wait_for(lambda: not list(downloads.download_dir().rglob("*.part")))
        assert downloads.path_for(42) is None

    def test_a_scratch_file_left_by_a_kill_is_cleared_when_the_screen_opens(self):
        root = downloads.download_dir()
        root.mkdir(parents=True)
        orphan = root / ".ticli-42.part"
        orphan.write_bytes(b"half a track")
        decoy = root / "somebody-elses.part"
        decoy.write_bytes(b"not ours")

        p = _player()
        p._current_track = _track()
        p._open_download()

        assert not orphan.exists(), "the orphaned scratch file was left behind"
        assert decoy.read_bytes() == b"not ours", "a file ticli did not write was deleted"


# ── living alongside the user ──


class TestManualDeletion:
    def test_a_deleted_download_is_simply_not_downloaded(self):
        root = downloads.download_dir()
        path = root / "A" / "B" / "01 T.m4a"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"music")
        downloads.record(11, path.relative_to(root), "HIGH", 5)
        assert downloads.path_for(11) == path
        assert downloads.downloaded_count() == 1

        path.unlink()  # the user dragged it to the trash

        assert downloads.path_for(11) is None, "a deleted file must not be claimed"
        assert downloads.downloaded_count() == 0
        assert downloads.total_bytes() == 0
        # ...and the index that still mentions it is not an error
        assert json.loads(downloads.index_file().read_text())["tracks"]["11"]

    def test_playback_falls_through_to_the_network(self, monkeypatch):
        """The playback path's half: `local` is verified at the moment of use,
        so a file removed between the lookup and the spawn plays from the URL
        rather than failing."""
        from ticli.player import AudioPlayer

        spawned = []

        class _Proc:
            def __init__(self, cmd, **kw):
                spawned.append(cmd)

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(player_mod.subprocess, "Popen", _Proc)
        patch_get(monkeypatch, player_mod,
                  lambda *a, **k: pytest.fail("no fetch expected"))
        audio = AudioPlayer("mpv", cache=MetadataCache(songs=False))
        gone = str(downloads.download_dir() / "not" / "there.m4a")
        audio.play_url("http://cdn.example/t.mp4", local=gone)
        assert spawned[-1][-1] == "http://cdn.example/t.mp4"

    def test_a_download_that_is_there_is_played_from_disk(self, monkeypatch):
        from ticli.player import AudioPlayer

        spawned = []
        monkeypatch.setattr(player_mod.subprocess, "Popen", lambda cmd, **kw: (
            spawned.append(cmd), types.SimpleNamespace(
                poll=lambda: None, terminate=lambda: None,
                wait=lambda timeout=None: 0, kill=lambda: None))[1])
        path = downloads.download_dir() / "A" / "01 T.m4a"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"music")
        audio = AudioPlayer("mpv", cache=MetadataCache(songs=True))
        audio.play_url("http://cdn.example/t.mp4", local=str(path))
        assert spawned[-1][-1] == str(path)
        # ...and stop() must not delete somebody's music
        audio.stop()
        assert path.exists(), "stopping deleted a downloaded file"

    def test_re_downloading_a_deleted_track_works(self):
        server = _Server({"/t.mp4": _mp4()})
        try:
            p = _player()
            track = _track()
            track.get_stream = lambda: types.SimpleNamespace(
                audio_quality="HIGH",
                get_stream_manifest=lambda: types.SimpleNamespace(
                    is_bts=True, get_urls=lambda: [server.url("/t.mp4")]))
            p._download_track = track
            assert _download(p)["state"] == "done"
            path = downloads.path_for(42)
            path.unlink()
            assert downloads.path_for(42) is None

            assert _download(p)["state"] == "done"
            assert downloads.path_for(42) == path
        finally:
            server.close()


class TestDownloadsAreNotTheCache:
    def test_a_download_is_not_evicted_when_the_budget_is_enforced(self):
        root = downloads.download_dir()
        kept = root / "Artist" / "Album" / "01 Kept.m4a"
        kept.parent.mkdir(parents=True)
        kept.write_bytes(b"d" * (4 * 1024 ** 2))
        downloads.record(11, kept.relative_to(root), "LOSSLESS", kept.stat().st_size)

        audio = cache_mod.audio_dir()
        audio.mkdir(parents=True)
        (audio / "99.m4a").write_bytes(b"c" * (4 * 1024 ** 2))

        cache = MetadataCache(budget_gb=0)
        cache.enforce_budget()

        assert not (audio / "99.m4a").exists(), "the cache was not swept at all"
        assert kept.exists(), "eviction reached into the download folder"
        assert downloads.path_for(11) == kept

    def test_clearing_the_cache_leaves_downloads_alone(self):
        root = downloads.download_dir()
        kept = root / "Artist" / "Album" / "01 Kept.m4a"
        kept.parent.mkdir(parents=True)
        kept.write_bytes(b"music")
        audio = cache_mod.audio_dir()
        audio.mkdir(parents=True)
        (audio / "99.m4a").write_bytes(b"cached")

        removed, _ = MetadataCache().clear_audio()

        assert removed == 1
        assert kept.read_bytes() == b"music"

    def test_downloads_do_not_count_against_the_budget(self):
        root = downloads.download_dir()
        big = root / "A" / "B" / "01 Big.m4a"
        big.parent.mkdir(parents=True)
        big.write_bytes(b"x" * (8 * 1024 ** 2))
        assert MetadataCache().total_bytes() == 0, \
            "the cache is sizing a folder that is not its own"

    def test_a_stranger_s_file_in_the_music_folder_survives_everything(self):
        root = downloads.download_dir()
        root.mkdir(parents=True)
        decoy = root / "important.txt"
        decoy.write_text("not ticli's")
        nested = root / "Some Band" / "notes.txt"
        nested.parent.mkdir(parents=True)
        nested.write_text("also not ticli's")

        MetadataCache(budget_gb=0).enforce_budget()
        MetadataCache().clear_audio()
        MetadataCache().clear()
        downloads.discard_scratch(root / "A" / "B" / "01 T.m4a")

        assert decoy.read_text() == "not ticli's"
        assert nested.read_text() == "also not ticli's"
        assert root.is_dir(), "the download folder itself was removed"


# ── metadata ──


class TestMp4Tags:
    def test_the_tags_read_back_out_of_the_file(self, tmp_path):
        path = tmp_path / "t.m4a"
        path.write_bytes(_mp4())
        meta = downloads.track_metadata(_track())
        written = tags.write_tags(path, meta)

        assert "title" in written and "artist" in written and "album" in written
        data = path.read_bytes()
        assert _ilst_text(data, b"\xa9nam") == "Get Lucky"
        assert _ilst_text(data, b"\xa9ART") == "Daft Punk, Pharrell Williams"
        assert _ilst_text(data, b"\xa9alb") == "Random Access Memories"
        assert _ilst_text(data, b"aART") == "Daft Punk"
        assert _ilst_text(data, b"\xa9day") == "2013"
        # trkn is a binary pair, not text: track 8 of 13
        at = data.index(b"trkn")
        assert struct.unpack_from(">HHHH", data, at + 4 + 16) == (0, 8, 13, 0)

    def test_tagging_twice_does_not_stack_udta_boxes(self, tmp_path):
        path = tmp_path / "t.m4a"
        path.write_bytes(_mp4())
        meta = downloads.track_metadata(_track())
        tags.write_tags(path, meta)
        first = path.read_bytes()
        tags.write_tags(path, meta)
        second = path.read_bytes()
        assert first == second
        assert second.count(b"ilst") == 1

    def test_a_moov_at_the_end_needs_no_offset_fixup(self, tmp_path):
        path = tmp_path / "t.m4a"
        path.write_bytes(_mp4(moov_last=True))
        before = _stco_offset(path.read_bytes())
        assert tags.write_tags(path, downloads.track_metadata(_track()))
        data = path.read_bytes()
        assert _stco_offset(data) == before, "nothing moved, so nothing should shift"
        assert data[before:before + len(AUDIO)] == AUDIO

    def test_a_cover_is_embedded_as_a_picture_atom(self, tmp_path):
        path = tmp_path / "t.m4a"
        path.write_bytes(_mp4())
        jpeg = b"\xff\xd8\xff\xe0" + b"j" * 500
        written = tags.write_tags(path, downloads.track_metadata(_track()), jpeg)
        assert "cover art" in written
        data = path.read_bytes()
        at = data.index(b"covr")
        assert struct.unpack_from(">I", data, at + 4 + 8)[0] == tags.DATA_JPEG
        assert jpeg in data

    def test_a_file_with_absolute_fragment_offsets_is_refused_not_guessed_at(self, tmp_path):
        """A fragmented file may address its samples absolutely. This module
        does not rewrite that, so it hands the bytes back untouched rather
        than producing something that looks tagged and plays nothing."""
        # tfhd with base-data-offset-present (flag 0x1)
        tfhd = _box(b"tfhd", struct.pack(">IIQ", 0x000001, 1, 4096))
        moof = _box(b"moof", _box(b"traf", tfhd))
        original = _mp4() + moof
        path = tmp_path / "t.m4a"
        path.write_bytes(original)
        assert tags.write_tags(path, downloads.track_metadata(_track())) == ""
        assert path.read_bytes() == original, "a refused file must be untouched"

    def test_nonsense_bytes_are_left_exactly_as_they_were(self, tmp_path):
        path = tmp_path / "t.m4a"
        path.write_bytes(b"this is not an mp4 at all")
        assert tags.write_tags(path, downloads.track_metadata(_track())) == ""
        assert path.read_bytes() == b"this is not an mp4 at all"
        assert not list(path.parent.glob("*.tagging"))

    def test_an_unknown_container_is_a_missing_tag_not_a_failure(self, tmp_path):
        path = tmp_path / "t.ogg"
        path.write_bytes(b"OggS-ish")
        assert tags.write_tags(path, downloads.track_metadata(_track())) == ""
        assert path.read_bytes() == b"OggS-ish"


class TestFlacTags:
    @staticmethod
    def _flac(with_comment=False):
        streaminfo = b"\x00" * 34
        blocks = [(0, streaminfo)]
        if with_comment:
            blocks.append((4, struct.pack("<I", 3) + b"old" + struct.pack("<I", 0)))
        blocks.append((1, b"\x00" * 16))  # PADDING
        out = b"fLaC"
        for i, (kind, payload) in enumerate(blocks):
            flag = 0x80 if i == len(blocks) - 1 else 0
            out += bytes([flag | kind]) + len(payload).to_bytes(3, "big") + payload
        return out + b"AUDIOFRAMES"

    def test_a_vorbis_comment_block_is_written_and_reads_back(self, tmp_path):
        path = tmp_path / "t.flac"
        path.write_bytes(self._flac())
        assert tags.write_tags(path, downloads.track_metadata(_track()))
        data = path.read_bytes()
        assert data[:4] == b"fLaC"
        assert b"TITLE=Get Lucky" in data
        assert b"ARTIST=Daft Punk, Pharrell Williams" in data
        assert b"TRACKNUMBER=8" in data
        assert b"ISRC=USQX91300110" in data
        assert data.endswith(b"AUDIOFRAMES"), "the audio frames moved"

    def test_an_existing_comment_block_is_replaced_not_duplicated(self, tmp_path):
        path = tmp_path / "t.flac"
        path.write_bytes(self._flac(with_comment=True))
        assert tags.write_tags(path, downloads.track_metadata(_track()))
        data = path.read_bytes()
        assert b"old" not in data
        assert data.count(b"TITLE=") == 1

    def test_the_block_list_is_still_walkable_and_the_last_flag_is_right(self, tmp_path):
        path = tmp_path / "t.flac"
        path.write_bytes(self._flac())
        tags.write_tags(path, downloads.track_metadata(_track()))
        data = path.read_bytes()
        pos, kinds = 4, []
        while True:
            header = data[pos]
            length = int.from_bytes(data[pos + 1:pos + 4], "big")
            kinds.append(header & 0x7F)
            pos += 4 + length
            if header & 0x80:
                break
        assert kinds[0] == 0, "STREAMINFO must stay first"
        assert 4 in kinds
        assert data[pos:] == b"AUDIOFRAMES"


class TestWhatTheFileActuallyCarries:
    def test_the_path_carries_artist_album_and_title_on_its_own(self):
        meta = downloads.track_metadata(_track())
        assert downloads.relative_path(meta, ".m4a").parts == (
            "Daft Punk", "Random Access Memories", "08 Get Lucky.m4a")

    def test_a_slash_in_a_name_does_not_become_a_directory(self):
        meta = downloads.track_metadata(_track())
        meta["album_artist"] = "AC/DC"
        meta["title"] = "Rock 'n' Roll: Part 1"
        parts = downloads.relative_path(meta, ".m4a").parts
        assert parts[0] == "AC_DC"
        assert parts[-1] == "08 Rock 'n' Roll_ Part 1.m4a"

    def test_missing_metadata_still_produces_a_usable_path(self):
        bare = types.SimpleNamespace(id=5, name="", duration=10, artists=[],
                                     album=None, track_num=0, volume_num=0)
        meta = downloads.track_metadata(bare)
        assert downloads.relative_path(meta, ".m4a").parts == (
            "Unknown Artist", "Unknown Album", "Track 5.m4a")

    def test_the_screen_never_claims_a_tag_that_was_not_written(self):
        p = _player()
        p._download_track = _track()
        p._download_job = {"state": "done", "path": "/x/y.m4a", "tags": "",
                           "tier": "HIGH", "track_id": 42}
        text = p._build_download_display().plain
        assert "No tags could be written" in text
        assert "the folder and filename carry" in text.replace("\n   ", " ")


class TestTheSettingsPageIsCheapToPaint:
    """The settings page repaints twice a second, on the UI thread.

    `downloaded_count()` and `total_bytes()` each called `path_for()` per
    track, and `path_for()` re-read and re-parsed the whole `downloads.json`
    every call — 2(N+1) reads and parses of an O(N)-sized file per frame.
    Measured before: 4.54 ms at 50 downloads, 42.70 ms at 200, **229.48 ms at
    500** — the same order as the 231 ms keypress latency `auto_refresh=False`
    existed to remove. Assert the reads, not the milliseconds: a timing
    threshold on a shared machine is a flaky test waiting to happen.
    """

    def _library(self, count):
        root = downloads.download_dir()
        for tid in range(count):
            path = root / "A" / "B" / f"{tid:02d} T.m4a"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"music")
            downloads.record(tid, path.relative_to(root), "HIGH", 5)

    def _counting_reads(self, monkeypatch):
        reads = []
        real = downloads.load_index

        def _load():
            reads.append(1)
            return real()

        monkeypatch.setattr(downloads, "load_index", _load)
        return reads

    def test_both_numbers_cost_one_index_read(self, monkeypatch):
        self._library(20)
        reads = self._counting_reads(monkeypatch)
        assert downloads.usage() == (20, 100)
        assert len(reads) == 1, f"{len(reads)} reads of the index for one frame"

    def test_the_reads_do_not_grow_with_the_library(self, monkeypatch):
        self._library(60)
        reads = self._counting_reads(monkeypatch)
        downloads.usage()
        assert len(reads) == 1

    def test_a_repaint_does_not_re_measure(self, monkeypatch):
        self._library(5)
        p = _player()
        p._user_display_name = "Garrett"
        p._build_settings_display()
        reads = self._counting_reads(monkeypatch)
        for _ in range(10):  # five seconds of idle repaints
            p._build_settings_display()
        assert reads == [], "the page re-measured the download folder per frame"

    def test_opening_the_page_re_measures(self, monkeypatch):
        p = _player()
        p._user_display_name = "Garrett"
        assert "0 songs downloaded" in p._build_settings_display().plain
        self._library(3)   # e.g. downloaded from another window, or by hand
        p._handle_key("c")
        assert "3 songs downloaded" in p._build_settings_display().plain


class TestSettingsShowsTheFolder:
    def test_the_path_and_the_count_are_on_the_settings_page(self):
        root = downloads.download_dir()
        path = root / "A" / "B" / "01 T.m4a"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"music" * 1000)
        downloads.record(11, path.relative_to(root), "HIGH", 5000)

        p = _player()
        p._user_display_name = "Garrett"
        text = p._build_settings_display().plain
        assert str(root) in text
        assert "1 song downloaded" in text
        # Why they are exempt is prose, and prose is what a short window drops
        # first (see _fit_levers) — the count and the path are not
        assert "never evicted and never counted against the cache budget" in text
