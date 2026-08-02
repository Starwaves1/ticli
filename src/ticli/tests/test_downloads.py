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
import re
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
from ticli.utils.config import QUALITY_CHOICES
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


def _box_text(player) -> str:
    """The download box as plain text, laid out the way a real window lays it
    out — through the same builder `_build_display` calls, so a test can
    never read a box the screen would not draw."""
    return "\n".join(row.plain for row in
                     player._build_download_overlay(player_mod.ROOMY_FIT))


def _rendered(player) -> str:
    """The whole pane as plain text, confirmations and all."""
    from rich.console import Console

    console = Console(width=100, height=40)
    player.console = console
    with console.capture() as cap:
        console.print(player._build_display())
    return cap.get()


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
                     for tier in ("LOW", "MEDIUM", "HIGH", "MAX")}
        assert all(v > 0 for v in estimates.values())
        # Strictly increasing: a higher tier is never a smaller file
        assert list(estimates.values()) == sorted(estimates.values())

    def test_the_arithmetic_is_duration_times_nominal_bitrate(self):
        # 197 s at 320 kbps: the research measured the real file at 7,931,562
        # bytes and this estimate at 7,880,000 — a 0.65% under-read
        assert downloads.estimate_bytes(197, "MEDIUM") == 7_880_000

    def test_an_unknown_duration_says_so_rather_than_showing_zero(self):
        assert downloads.estimate_bytes(0, "HIGH") == 0
        assert downloads.format_bytes(0) == "—"

    def test_opening_the_box_and_walking_every_tier_costs_no_request(self, monkeypatch):
        """Four tiers hovered is four numbers shown and **zero** calls. One
        `playbackinfo` per hover is what ai/INCIDENTS #1 was, so this is the
        assertion the estimates exist for."""
        patch_get(monkeypatch, player_mod,
                  lambda *a, **k: pytest.fail("no requests here"))
        p = _player(quality="HIGH")
        p._current_track = _track(duration=197)
        p._open_download()
        # Walked with the arrows, painted at every stop, the way a held-down
        # key would do it — and back up again, twice over
        for key in ([player_mod.KEY_UP] * 5 + [player_mod.KEY_DOWN] * 5) * 2:
            p._handle_key(key)
            assert p._download_tier() in _box_text(p)
        seen = set()
        for _ in range(len(QUALITY_CHOICES)):
            text = _box_text(p)
            seen.add(p._download_tier())
            assert re.search(r"~\d+(\.\d+)? [KMG]B", text), text
            p._handle_key(player_mod.KEY_UP)
        assert seen == set(QUALITY_CHOICES)

    def test_one_tier_at_a_time_and_the_number_follows_it(self):
        p = _player(quality="LOW")
        p._current_track = _track(duration=197)
        p._open_download()
        assert "LOW" in _box_text(p)
        p._handle_key(player_mod.KEY_DOWN)
        text = _box_text(p)
        assert "MEDIUM" in text and "LOW" not in text, text
        assert "~" + downloads.format_bytes(
            downloads.estimate_bytes(197, "MEDIUM")) in text, text


class TestTheSizeIsExactWhenItIsFree:
    """A `~` means an estimate and no `~` means a byte count. The only two
    places a real one can be had for nothing are the download index and the
    cache tracker — anywhere else it is a request, and a request per hover is
    ai/INCIDENTS #1."""

    def _opened(self, quality="HIGH"):
        p = _player(quality=quality)
        p._current_track = _track(duration=197)
        p._open_download()
        return p

    def test_a_matching_download_record_shows_the_exact_bytes(self):
        p = self._opened()
        path = downloads.download_dir() / "A" / "B" / "01 T.m4a"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x" * 4321)
        # granted is the wire spelling: the hovered tier is HIGH, whose wire
        # value is LOSSLESS — that match is what makes the byte count exact
        downloads.record(42, path.relative_to(downloads.download_dir()),
                         "HIGH", 4321, granted="LOSSLESS")
        p._download_known = None
        text = _box_text(p)
        assert downloads.format_bytes(4321) in text, text
        assert "~" not in text, text
        assert "\u2713 on disk" in text, text

    def test_a_matching_cache_record_shows_the_exact_bytes(self):
        p = self._opened()
        p._cache.note_cached(42, ".m4a", 9999, quality="LOSSLESS")
        p._download_known = None
        text = _box_text(p)
        assert downloads.format_bytes(9999) in text, text
        assert "~" not in text, text

    def test_a_record_at_another_tier_is_still_an_estimate(self):
        p = self._opened()
        p._cache.note_cached(42, ".m4a", 9999, quality="LOW")
        p._download_known = None
        text = _box_text(p)
        assert "~" in text, text
        assert downloads.format_bytes(9999) not in text, text

    def test_a_track_nothing_is_known_about_is_an_estimate(self):
        assert "~" in _box_text(self._opened())

    def test_the_two_index_reads_are_not_paid_on_every_repaint(self, monkeypatch):
        """The box repaints twice a second and `load_index()` re-reads and
        re-parses the whole file every call: 229 ms per repaint at 500
        downloads, on the UI thread (F6 in the data-path audit). Measured on
        the reads themselves rather than on a clock, because the number that
        must not grow is the number of reads."""
        p = self._opened()
        reads = []
        real = downloads.load_index
        monkeypatch.setattr(downloads, "load_index",
                            lambda: (reads.append(1), real())[1])
        p._download_known = None
        for _ in range(20):
            _box_text(p)
        # One `path_for` (which loads the index itself) and nothing after it
        assert len(reads) == 1, reads
        # and a landed download drops it, so the exact size can appear
        p._download_known = None
        _box_text(p)
        assert len(reads) == 2, reads


class TestTheCursorStartsWhereSettingsIs:
    def test_pre_placed_on_the_tier_in_settings(self):
        for quality, index in (("LOW", 0), ("MEDIUM", 1), ("HIGH", 2), ("MAX", 3)):
            p = _player(quality=quality)
            p._current_track = _track()
            p._open_download()
            assert p._download_cursor == index
            assert p._download_open is True

    def test_d_then_enter_and_d_then_d_do_the_same_thing(self):
        """The two ways he described the common case, and both close the box
        rather than leaving him somewhere to get out of."""
        for finish in (player_mod.KEY_ENTER, "d"):
            p = _player(quality="HIGH")
            p._current_track = _track()
            started = []
            p._start_download_job = lambda tier: started.append(tier)
            p._handle_key("d")
            assert p._download_open is True and started == []
            p._handle_key(finish)
            assert started == ["HIGH"], finish
            assert p._download_open is False, finish

    def test_esc_spends_nothing(self):
        p = _player()
        p._current_track = _track()
        p._start_download_job = lambda tier: pytest.fail("Esc started a download")
        p._handle_key("d")
        p._handle_key(player_mod.KEY_ESC)
        assert p._download_open is False
        assert p._download_job is None


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
        assert p._download_open is True
        assert p._mode == p.MODE_QUEUE, "the box is over the queue, not instead of it"
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


class TestATierThatCannotBeServedStepsDown:
    """A tier this login cannot have is usually not an error — TIDAL grants
    something lower and says so. But it can fail outright, and giving up then
    leaves the user with nothing when a good copy was one rung down."""

    def _track_failing_above(self, server, ceiling):
        """A track whose `get_stream` raises for every tier above `ceiling`,
        recording which tiers were asked for."""
        asked = []
        track = _track()

        def _stream():
            tier = str(track._session.audio_quality)
            asked.append(tier)
            if player_mod.QUALITY_RANK.get(tier, 0) > \
                    player_mod.QUALITY_RANK[ceiling]:
                raise RuntimeError(f"{tier} is not available for this track")
            return types.SimpleNamespace(
                audio_quality=tier,
                get_stream_manifest=lambda: types.SimpleNamespace(
                    is_bts=True, get_urls=lambda: [server.url("/track.mp4")]))

        track.get_stream = _stream
        return track, asked

    def test_it_walks_down_until_one_works(self):
        server = _Server({"/track.mp4": _mp4()})
        try:
            p = _player(quality="MAX")
            track, asked = self._track_failing_above(server, "LOSSLESS")
            track._session = p.session
            p._download_track = track
            job = _download(p, "MAX")

            assert job["state"] == "done", job.get("error")
            assert asked == ["HI_RES_LOSSLESS", "LOSSLESS"], \
                "it did not step down exactly one rung at a time"
            # The file is real and it is on the disk
            assert downloads.path_for(track.id) is not None

            # And the box says which rung it stopped on, rather than letting a
            # MAX request that came back HIGH read as a hi-res file
            assert job["landed"] == "HIGH"
            p._download_open = True
            assert "HIGH" in _rendered(p)
        finally:
            server.close()

    def test_the_recorded_tier_is_the_one_that_was_served(self):
        server = _Server({"/track.mp4": _mp4()})
        try:
            p = _player(quality="MAX")
            track, _ = self._track_failing_above(server, "HIGH")
            track._session = p.session
            p._download_track = track
            assert _download(p, "MAX")["state"] == "done"
            entry = downloads.load_index()[str(track.id)]
            assert entry["granted"] == "HIGH", \
                "a 320k AAC file recorded as hi-res would mislead [R] for ever"
        finally:
            server.close()

    def test_a_rate_limit_is_never_stepped_past(self):
        """Retrying is what turned a rate limit into an edge block
        (ai/INCIDENTS #1). Three more requests at lower tiers is the worst
        possible response to a limiter already saying no."""
        p = _player(quality="MAX")
        asked = []
        track = _track()

        def _stream():
            asked.append(str(p.session.audio_quality))
            raise RuntimeError("429 Too Many Requests")

        track.get_stream = _stream
        p._download_track = track
        job = _download(p, "MAX")
        assert job["state"] == "failed"
        assert len(asked) == 1, f"it retried a rate limit {len(asked)} times"

    def test_the_common_case_is_still_one_request(self):
        server = _Server({"/track.mp4": _mp4()})
        try:
            p = _player(quality="MEDIUM")
            track, asked = self._track_failing_above(server, "HI_RES_LOSSLESS")
            track._session = p.session
            p._download_track = track
            assert _download(p, "MEDIUM")["state"] == "done"
            assert asked == ["HIGH"], "a tier that works costs one request"
        finally:
            server.close()

    def test_the_ladder_is_the_tier_and_everything_under_it(self):
        assert HeadlessTidalPlayer._tier_ladder("MAX") == \
            ["MAX", "HIGH", "MEDIUM", "LOW"]
        assert HeadlessTidalPlayer._tier_ladder("MEDIUM") == ["MEDIUM", "LOW"]
        assert HeadlessTidalPlayer._tier_ladder("LOW") == ["LOW"]


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
            # the manifest is already written; the tier is what a stream says
            p._stream_description = lambda t: (path, "LOSSLESS")

            job = _download(p, "HIGH")
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


class TestDownloadingSomethingAlreadyCached:
    """Play a hi-res track, then press `[d]`. The bytes are already here.

    The two tiers are separate in *lifetime and ownership* — the cache is
    machine-owned and evictable, the download is the user's and is not — but
    they are not separate in bytes. Re-fetching ~30 MB that is on the disk
    two directories away costs an API request and the whole file again.
    """

    def _cached(self, cache, track_id, quality, body=b"cached bytes", ext=".m4a"):
        directory = cache_mod.audio_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{track_id}{ext}"
        path.write_bytes(body)
        cache.note_cached(track_id, ext, len(body), quality=quality)
        return path

    def _player_expecting_no_network(self, monkeypatch, quality="HIGH"):
        p = _player(quality=quality)
        patch_get(monkeypatch, player_mod,
                  lambda *a, **k: pytest.fail("the cached copy was re-fetched"))
        track = _track()
        track.get_stream = lambda: pytest.fail("a promotion asked for a stream")
        p._download_track = track
        return p

    def test_a_matching_cached_copy_is_copied_not_re_fetched(self, monkeypatch):
        p = self._player_expecting_no_network(monkeypatch)
        body = _mp4()
        self._cached(p._cache, 42, "LOSSLESS", body=body)

        job = _download(p, "HIGH")

        assert job["state"] == "done", job.get("error")
        landed = downloads.download_dir() / "Daft Punk" / \
            "Random Access Memories" / "08 Get Lucky.m4a"
        assert landed.exists()
        # Tagged in place, so it is not byte-identical — but the audio is
        assert AUDIO in landed.read_bytes()

    def test_promotion_reads_the_cached_copy_rather_than_consuming_it(self, monkeypatch):
        """The promotion itself must `copy`, never `move`.

        The cached file can be open in the backend at that very moment, and
        the download has to be able to fail and leave the cache exactly as it
        found it. Checked *during* the operation, because the copy is deleted
        afterwards on purpose — see the next test — and an end-state
        assertion could no longer tell a copy from a move.
        """
        p = self._player_expecting_no_network(monkeypatch)
        cached = self._cached(p._cache, 42, "LOSSLESS", body=_mp4())
        seen = []
        real_write = player_mod.tags.write_tags
        # Tagging is the last step before the index row, so the cache copy is
        # still expected to be on disk here
        monkeypatch.setattr(player_mod.tags, "write_tags",
                            lambda *a, **k: (seen.append(cached.exists()),
                                             real_write(*a, **k))[1])
        _download(p, "HIGH")
        assert seen == [True], "promoting moved the cache's own copy"

    def test_the_superseded_cached_copy_is_reclaimed(self, monkeypatch):
        """Once the download exists, `_local_copy` prefers it for ever, so the
        cached copy can never be played again. It is not a spare — it is a
        second copy of identical audio holding cache budget and competing in
        eviction against songs that can still be used."""
        p = self._player_expecting_no_network(monkeypatch)
        cached = self._cached(p._cache, 42, "LOSSLESS", body=_mp4())
        assert _download(p, "HIGH")["state"] == "done"

        assert not cached.exists(), "two copies of the same audio survived"
        assert p._cache.audio_record(42) is None, \
            "the tracker still claims a song that is gone"
        assert p._cache.cached_usage() == (0, 0)
        # The download itself is untouched — this only ever reclaims the
        # machine-owned side
        assert downloads.path_for(42) is not None

    def test_the_track_playing_right_now_keeps_its_cached_copy(self, monkeypatch):
        """Unlinking it is safe on POSIX once the backend has the file open —
        but if it has not opened it yet it exits and restarts from the
        network, which is an audible hiccup paid for a bookkeeping tidy-up.
        One duplicate for the length of one track is nothing."""
        p = self._player_expecting_no_network(monkeypatch)
        cached = self._cached(p._cache, 42, "LOSSLESS", body=_mp4())
        p._current_track = _track()          # id 42, the one being downloaded
        assert _download(p, "HIGH")["state"] == "done"
        assert cached.exists(), "it deleted the file under the playing track"
        assert p._cache.audio_record(42) is not None

    def test_the_deferred_reclaim_fires_once_the_track_moves_on(self, monkeypatch):
        """Skipping the playing track is right, but skipped must not mean
        forgiven: nothing else ever takes the duplicate. `reconcile()` keeps
        a tracker entry whose file is present, and eviction ranks by plays —
        the much-played track you download while listening is exactly the one
        eviction keeps for ever. So the drop defers the id and the monitor's
        tick retries it the first time the track is no longer current."""
        p = self._player_expecting_no_network(monkeypatch)
        cached = self._cached(p._cache, 42, "LOSSLESS", body=_mp4())
        p._current_track = _track()          # id 42, the one being downloaded
        assert _download(p, "HIGH")["state"] == "done"
        assert cached.exists()

        p._reclaim_deferred_copies()         # the tick, track still playing
        assert cached.exists(), "it reclaimed the copy under the playing track"

        p._current_track = _track(tid=99)    # the song moved on
        p._reclaim_deferred_copies()         # the same tick, one track later
        assert not cached.exists(), "the deferred reclaim never fired"
        assert p._cache.audio_record(42) is None

    def test_the_startup_sweep_reclaims_a_duplicate_that_survived_a_quit(self):
        """Quit while the deferred track is still playing and the pair is on
        disk the next morning. The sweep beside `reconcile()` catches it —
        and every duplicate created before the drop existed at all."""
        p = _player(quality="HIGH")
        cached = self._cached(p._cache, 42, "LOSSLESS", body=_mp4())
        dest = downloads.destination(downloads.track_metadata(_track()), ".m4a")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_mp4())
        downloads.record(42, dest.relative_to(downloads.download_dir()),
                         "HIGH", dest.stat().st_size, granted="LOSSLESS")

        p._reclaim_download_duplicates()

        assert not cached.exists(), "the sweep left the duplicate behind"
        assert p._cache.audio_record(42) is None
        assert downloads.path_for(42) is not None, "the sweep took the download"

    def test_the_sweep_defers_the_playing_track_to_the_tick(self):
        """A restore can already be audible by the time the sweep runs, and
        the sweep must not cause the very hiccup the per-download drop
        avoids. Same deferral, same retry."""
        p = _player(quality="HIGH")
        cached = self._cached(p._cache, 42, "LOSSLESS", body=_mp4())
        dest = downloads.destination(downloads.track_metadata(_track()), ".m4a")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_mp4())
        downloads.record(42, dest.relative_to(downloads.download_dir()),
                         "HIGH", dest.stat().st_size, granted="LOSSLESS")
        p._current_track = _track()

        p._reclaim_download_duplicates()
        assert cached.exists(), "the sweep unlinked the playing track's copy"

        p._current_track = _track(tid=99)
        p._reclaim_deferred_copies()
        assert not cached.exists()

    def test_a_hand_deleted_download_does_not_take_the_cached_copy_with_it(self):
        """The sweep walks `downloads.present()`, not the raw index: a
        download the user dragged to the trash is not a download, and
        `_local_source` falls back to the cached copy then — reclaiming it
        would delete the one copy that still plays."""
        p = _player(quality="HIGH")
        cached = self._cached(p._cache, 42, "LOSSLESS", body=_mp4())
        dest = downloads.destination(downloads.track_metadata(_track()), ".m4a")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_mp4())
        downloads.record(42, dest.relative_to(downloads.download_dir()),
                         "HIGH", dest.stat().st_size, granted="LOSSLESS")
        dest.unlink()                        # deleted by hand, index left stale

        p._reclaim_download_duplicates()

        assert cached.exists(), "the sweep trusted the index over the disk"

    def test_a_file_gone_at_unlink_time_still_forgets_the_entry(self, monkeypatch):
        """Eviction racing the drop: the path was looked up, then the file
        went. The entry must still be forgotten — already gone is the state
        the drop wanted (the reading `clear()` gives FileNotFoundError), and
        keeping it recreates the exact entry-with-no-file the drop cleans up
        when it *sees* it."""
        p = _player(quality="HIGH")
        cached = self._cached(p._cache, 42, "LOSSLESS", body=_mp4())
        real = player_mod.cached_audio_path

        def raced(track_id):
            path = real(track_id)
            cached.unlink()      # eviction wins between the lookup and unlink
            return path

        monkeypatch.setattr(player_mod, "cached_audio_path", raced)
        p._drop_superseded_cache_copy(42)
        assert p._cache.audio_record(42) is None, \
            "a vanished file left its tracker entry behind"

    def test_reclaim_works_with_cache_songs_off_but_the_files_kept(self, monkeypatch):
        """Turn `cache_songs` off and answer "n" to `Clear cached songs as
        well?` and the files stay on disk — still unreachable behind a
        download, and with the setting off nothing else (`reconcile` early
        returns, no cache write ever sweeps) will ever reclaim them. The
        drop is gated on evidence, not on the setting."""
        p = _player(quality="HIGH")
        cached = self._cached(p._cache, 42, "LOSSLESS", body=_mp4())
        p._cache = MetadataCache(songs=False)     # the "n" answer: files kept
        track = _track()
        track.get_stream = lambda: types.SimpleNamespace(
            audio_quality="LOSSLESS",
            get_stream_manifest=lambda: types.SimpleNamespace(
                is_bts=True, get_urls=lambda: ["https://cdn/x.mp4"]))
        p._download_track = track
        # No promotion with the setting off, so the bytes come off the wire
        patch_get(monkeypatch, player_mod, lambda *a, **k: _FakeStream(_mp4()))

        assert _download(p, "HIGH")["state"] == "done"

        assert not cached.exists(), \
            "cache_songs off left the unreachable duplicate for ever"
        assert p._cache.audio_record(42) is None

    def test_the_recorded_tier_is_the_one_that_was_granted(self, monkeypatch):
        p = self._player_expecting_no_network(monkeypatch)
        self._cached(p._cache, 42, "LOSSLESS", body=_mp4())
        _download(p, "HIGH")
        entry = downloads.load_index()["42"]
        assert entry["granted"] == "LOSSLESS"
        assert entry["quality"] == "HIGH"

    def test_a_lower_cached_tier_is_never_promoted(self, monkeypatch):
        """A cached 320k copy does not satisfy a MAX download. Silently installing
        the wrong quality in someone's music folder is worse than re-fetching
        — `.m4a` is AAC on a device-flow session and FLAC on a PKCE one, and
        the names are identical."""
        p = _player(quality="MAX")
        self._cached(p._cache, 42, "HIGH", body=_mp4())
        calls = []
        p._download_track = _track()
        p._download_track.get_stream = lambda: (
            calls.append(1) or types.SimpleNamespace(
                audio_quality="HI_RES_LOSSLESS",
                get_stream_manifest=lambda: types.SimpleNamespace(
                    is_bts=True, get_urls=lambda: ["https://cdn/hi.mp4"])))
        patch_get(monkeypatch, player_mod,
                  lambda *a, **k: _FakeStream(_mp4()))

        job = _download(p, "MAX")

        assert job["state"] == "done", job.get("error")
        assert calls, "it promoted a lower-quality file instead of fetching"

    def test_a_cached_file_of_unknown_tier_is_never_promoted(self, monkeypatch):
        """Everything cached before the tracker existed has no tier. Unknown
        must never be read as "the tier you asked for"."""
        p = _player(quality="HIGH")
        directory = cache_mod.audio_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "42.m4a").write_bytes(_mp4())
        p._cache.reconcile()  # adopts it, at no known tier
        calls = []
        p._download_track = _track()
        p._download_track.get_stream = lambda: (
            calls.append(1) or types.SimpleNamespace(
                audio_quality="LOSSLESS",
                get_stream_manifest=lambda: types.SimpleNamespace(
                    is_bts=True, get_urls=lambda: ["https://cdn/x.mp4"])))
        patch_get(monkeypatch, player_mod, lambda *a, **k: _FakeStream(_mp4()))

        _download(p, "HIGH")

        assert calls, "an unknown tier was treated as a match"

    def test_a_cached_entry_whose_file_is_gone_falls_through(self, monkeypatch):
        p = _player(quality="HIGH")
        self._cached(p._cache, 42, "LOSSLESS", body=_mp4()).unlink()
        calls = []
        p._download_track = _track()
        p._download_track.get_stream = lambda: (
            calls.append(1) or types.SimpleNamespace(
                audio_quality="LOSSLESS",
                get_stream_manifest=lambda: types.SimpleNamespace(
                    is_bts=True, get_urls=lambda: ["https://cdn/x.mp4"])))
        patch_get(monkeypatch, player_mod, lambda *a, **k: _FakeStream(_mp4()))

        _download(p, "HIGH")

        assert calls, "the tracker was trusted over the disk"


class _FakeStream:
    """A streaming response over a fixed body."""

    def __init__(self, body, content_type="audio/mp4"):
        self.headers = {"Content-Type": content_type,
                        "Content-Length": str(len(body))}
        self._body = body

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False


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
        p._download_open = True
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
        downloads.record(11, kept.relative_to(root), "HIGH", kept.stat().st_size)

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

    def test_the_box_never_claims_a_tag_that_was_not_written(self):
        p = _player()
        p._download_track = _track()
        job = {"state": "done", "path": "/x/y.m4a", "tier": "HIGH", "track_id": 42}
        p._download_job = dict(job, tags="")
        assert "untagged" in _box_text(p)
        p._download_job = dict(job, tags="title, artist")
        assert "untagged" not in _box_text(p)


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
        assert "Downloads     0 songs" in p._build_settings_display().plain
        self._library(3)   # e.g. downloaded from another window, or by hand
        p._handle_key("c")
        assert "Downloads     3 songs" in p._build_settings_display().plain


class TestTheTwoTiersReadAsTwoTiers:
    """Garrett: "Downloaded songs should not count towards cache limit…
    both should have a disk usage metric… good cached/downloaded buttons and
    metrics." Two rows of one little table, so the numbers can be read against
    each other, and each row carries only what is true of its own tier."""

    def _page(self, songs, song_bytes, kept, kept_bytes, budget=2):
        p = _player()
        p._user_display_name = "Garrett"
        p.config["cache_budget_gb"] = budget
        p._cache.audio_count = lambda: songs
        p._cache.disk_bytes = lambda: song_bytes
        p._download_usage = lambda: (kept, kept_bytes)
        return p._build_settings_display().plain

    def test_the_cache_is_shown_against_its_budget(self):
        gb = cache_mod.BYTES_PER_GB
        text = self._page(12, gb // 2, 0, 0, budget=4)
        assert "Cache        12 songs · 0.500 GB of 4.000 GB" in text

    def test_the_downloads_carry_no_budget_and_say_so(self):
        gb = cache_mod.BYTES_PER_GB
        text = self._page(0, 0, 3, 3 * gb)
        assert "Downloads     3 songs · 3.000 GB · exempt from the budget" in text
        # No [x] on the downloads row: the cache is cleared wholesale and the
        # downloads are gone through one at a time, behind [d]
        rows = [line for line in text.splitlines() if "Downloads" in line]
        assert rows and "[x]" not in rows[0]
        assert "[d] list" in rows[0]

    def test_the_two_numbers_line_up(self):
        """The point of the redesign: they are meant to be compared."""
        gb = cache_mod.BYTES_PER_GB
        text = self._page(7, gb, 1234, 9 * gb)
        rows = [line for line in text.splitlines()
                if line.startswith(("   Cache ", "   Downloads "))]
        assert len(rows) == 2, rows
        assert rows[0].index("·") == rows[1].index("·")

    def test_the_page_fits_eighty_by_twentyfour_with_every_action_on_it(self):
        """The bar `9c96d63` set, and the one Garrett restated: fit at 80x24
        with [x], [o] and [u] on screen. Below that, degraded is fine."""
        from rich.console import Console

        gb = cache_mod.BYTES_PER_GB
        p = _player()
        p._mode = p.MODE_SETTINGS
        p._user_display_name = "Garrett"
        p._cache.audio_count = lambda: 9999
        p._cache.disk_bytes = lambda: 12 * gb
        p._download_usage = lambda: (9999, 12 * gb)
        console = Console(width=80, height=24)
        p.console = console
        with console.capture() as cap:
            console.print(p._build_display())
        lines = cap.get().splitlines()
        assert len(lines) <= 24, f"the pane is {len(lines)} rows in a 24-row window"
        assert all(len(line) <= 80 for line in lines)
        rendered = "\n".join(lines)
        for key in ("[x]", "[o]", "[u]", "[R]"):
            assert key in rendered, f"{key} fell off the page"


class TestReFetchingEverything:
    """The one action in the app that deliberately makes hundreds of requests.

    ai/INCIDENTS #1 is 53 `playbackinfo` calls in 2.8 s getting the owner's IP
    blocked and his music stopping. So the tests here are about the brakes:
    that it asks first, that it is serial and paced, that it can be stopped,
    and that evidence of a block ends it rather than being retried.
    """

    def _library(self, downloaded=(), cached=()):
        p = _player(quality="MAX")
        root = downloads.download_dir()
        for tid, granted in downloaded:
            path = root / "A" / "B" / f"{tid:02d} T.m4a"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"music")
            downloads.record(tid, path.relative_to(root), "MEDIUM", 5,
                             granted=granted)
        for tid, granted in cached:
            directory = cache_mod.audio_dir()
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{tid}.m4a").write_bytes(b"music")
            p._cache.note_cached(tid, ".m4a", 5, quality=granted)
        return p

    # ── what it says it will do ──

    def test_it_counts_both_tiers(self):
        p = self._library(downloaded=[(1, "HIGH")], cached=[(2, "LOSSLESS")])
        plan = p._refetch_candidates()
        assert plan["downloads"] == ["1"]
        assert plan["cache"] == ["2"]

    def test_anything_already_at_the_target_tier_is_left_alone(self):
        p = self._library(downloaded=[(1, "HI_RES_LOSSLESS")],
                          cached=[(2, "HI_RES_LOSSLESS")])
        plan = p._refetch_candidates()
        assert plan["downloads"] == [] and plan["cache"] == []
        assert plan["skipped"] == 2

    def test_a_download_also_in_the_cache_is_only_counted_once(self):
        p = self._library(downloaded=[(1, "HIGH")], cached=[(1, "HIGH")])
        plan = p._refetch_candidates()
        assert plan["downloads"] == ["1"] and plan["cache"] == []

    def test_a_file_deleted_by_hand_is_not_re_fetched(self):
        p = self._library(downloaded=[(1, "HIGH")])
        downloads.path_for(1).unlink()
        assert p._refetch_candidates()["downloads"] == []

    def test_the_plan_costs_no_network(self, monkeypatch):
        p = self._library(downloaded=[(1, "HIGH")], cached=[(2, "HIGH")])
        patch_get(monkeypatch, player_mod,
                  lambda *a, **k: pytest.fail("counting made a request"))
        p.session.track = lambda tid: pytest.fail("counting resolved a track")
        assert p._refetch_candidates()["bytes"] == 10

    # ── it asks first ──

    def test_nothing_happens_until_the_confirmation_is_answered(self, monkeypatch):
        p = self._library(downloaded=[(1, "HIGH")])
        started = []
        monkeypatch.setattr(p, "_start_refetch_job", lambda: started.append(1))
        p._mode = p.MODE_SETTINGS
        p._handle_key("R")
        assert p._refetch_pending is True
        assert started == [], "it started before it asked"

        rendered = _rendered(p)
        assert "Upgrade 1 song to MAX?" in rendered
        assert "About" in rendered, "it did not say how much it would fetch"

    def test_any_other_key_is_a_true_no_op(self, monkeypatch):
        p = self._library(downloaded=[(1, "HIGH")])
        started = []
        monkeypatch.setattr(p, "_start_refetch_job", lambda: started.append(1))
        p._mode = p.MODE_SETTINGS
        p._handle_key("R")
        p._handle_key("n")
        assert started == []
        assert p._refetch_pending is False and p._refetch_plan is None

    def test_y_starts_it(self, monkeypatch):
        p = self._library(downloaded=[(1, "HIGH")])
        started = []
        monkeypatch.setattr(p, "_start_refetch_job", lambda: started.append(1))
        p._mode = p.MODE_SETTINGS
        p._handle_key("R")
        p._handle_key("y")
        assert started == [1]

    def test_with_nothing_to_do_it_says_so_rather_than_starting(self, monkeypatch):
        p = self._library(downloaded=[(1, "HI_RES_LOSSLESS")])
        p._mode = p.MODE_SETTINGS
        p._handle_key("R")
        assert "Nothing is below MAX" in _rendered(p)

    # ── it only ever moves upwards ──
    #
    # Comparing `granted != wanted` made [R] a *downgrade* button: set the
    # quality to MEDIUM (320k AAC) with a lossless library and it re-fetched every song as
    # AAC, one request each, and reported it as success.

    def test_a_lower_setting_never_downgrades_what_is_on_disk(self):
        p = self._library(downloaded=[(1, "HI_RES_LOSSLESS")],
                          cached=[(2, "LOSSLESS")])
        p._quality_name = "MEDIUM"
        plan = p._refetch_candidates()
        assert plan["downloads"] == [], "it would have re-fetched hi-res as AAC"
        assert plan["cache"] == [], "it would have re-fetched lossless as AAC"
        assert plan["skipped"] == 2

    def test_an_equal_tier_is_not_an_upgrade(self):
        p = self._library(downloaded=[(1, "LOSSLESS")])
        p._quality_name = "HIGH"
        assert p._refetch_candidates()["downloads"] == []

    def test_the_target_is_capped_by_what_tidal_will_serve(self):
        """Asking for hi-res on a login that is only served HIGH does not make
        hi-res available — and re-fetching the library to be handed back the
        same AAC is one request per track for nothing."""
        p = self._library(downloaded=[(1, "HIGH")])
        p._quality_ceiling = "HIGH"          # observed downgrade, not a guess
        assert p._upgrade_target() == "HIGH"
        assert p._refetch_candidates()["downloads"] == []

    def test_the_ceiling_still_allows_an_upgrade_below_it(self):
        p = self._library(downloaded=[(1, "LOW")])
        p._quality_ceiling = "HIGH"
        plan = p._refetch_candidates()
        assert plan["downloads"] == ["1"], "LOW -> HIGH is a real upgrade"

    def test_an_unrecorded_tier_is_left_alone_and_counted(self):
        """A copy from before the tracker, or one `reconcile()` adopted, has no
        recorded tier. "Higher than unknown" is not something anyone can
        honestly claim, so it is skipped — and said out loud rather than folded
        into "already at this quality"."""
        p = self._library(downloaded=[(1, None)], cached=[(2, None)])
        plan = p._refetch_candidates()
        assert plan["downloads"] == [] and plan["cache"] == []
        assert plan["unknown"] == 2
        assert plan["skipped"] == 0
        p._mode = p.MODE_SETTINGS
        p._handle_key("R")
        assert "unrecorded quality" in _rendered(p)

    # ── the brakes ──

    def test_it_is_serial_and_paced(self, monkeypatch):
        """One at a time, and never faster than REFETCH_MIN_INTERVAL between
        the *starts* — so a run of instant failures cannot become a burst."""
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.3)
        p = self._library(downloaded=[(1, "HIGH"), (2, "HIGH"), (3, "HIGH")])
        starts = []
        overlapping = []
        running = []

        def _one(kind, key, tier, gen):
            starts.append(time.monotonic())
            overlapping.append(len(running))
            running.append(key)
            time.sleep(0.02)
            running.pop()

        monkeypatch.setattr(p, "_refetch_one", _one)
        p._start_refetch_job()
        assert _wait_for(lambda: (p._refetch_job or {}).get("state") == "done", 10)

        assert len(starts) == 3
        assert overlapping == [0, 0, 0], "two tracks were fetched at once"
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        assert all(g >= 0.28 for g in gaps), f"paced too fast: {gaps}"

    def test_a_rate_limit_stops_everything_and_retries_nothing(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.01)
        p = self._library(downloaded=[(1, "HIGH"), (2, "HIGH"), (3, "HIGH")])
        seen = []

        def _one(kind, key, tier, gen):
            seen.append(key)
            raise RuntimeError("429 Too Many Requests")

        monkeypatch.setattr(p, "_refetch_one", _one)
        p._start_refetch_job()
        assert _wait_for(lambda: (p._refetch_job or {}).get("state") == "blocked", 10)

        assert seen == ["1"], "it kept going after TIDAL said stop"
        assert "rate-limiting" in p._toast

    def test_a_streaming_privileges_401_stops_it_too(self):
        # The escalation the incident actually hit, once 429s were ignored
        assert player_mod._looks_rate_limited(
            "401 subStatus 4006 Session does not have streaming privileges")
        assert not player_mod._looks_rate_limited("404 Not Found")

    def test_one_failure_does_not_stop_the_rest(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.01)
        p = self._library(downloaded=[(1, "HIGH"), (2, "HIGH")])

        def _one(kind, key, tier, gen):
            if key == "1":
                raise RuntimeError("that one is gone")

        monkeypatch.setattr(p, "_refetch_one", _one)
        p._start_refetch_job()
        assert _wait_for(lambda: (p._refetch_job or {}).get("state") == "done", 10)
        assert p._refetch_job["done"] == 1 and p._refetch_job["failed"] == 1

    def test_esc_stops_it_and_it_stays_stopped(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.05)
        p = self._library(downloaded=[(i, "HIGH") for i in range(1, 8)])
        seen = []

        def _one(kind, key, tier, gen):
            seen.append(key)
            time.sleep(0.05)

        monkeypatch.setattr(p, "_refetch_one", _one)
        p._mode = p.MODE_SETTINGS
        p._start_refetch_job()
        assert _wait_for(lambda: len(seen) >= 2, 5)
        p._handle_key(player_mod.KEY_ESC)

        assert p._mode == p.MODE_SETTINGS, "Esc left the page instead of stopping"
        stopped_at = len(seen)
        time.sleep(0.4)
        assert len(seen) <= stopped_at + 1, "it kept fetching after being stopped"
        assert "cancelled" in p._toast

    def test_a_second_press_does_not_start_a_second_run(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.05)
        p = self._library(downloaded=[(i, "HIGH") for i in range(1, 6)])
        runs = []
        monkeypatch.setattr(p, "_refetch_one",
                            lambda *a: (runs.append(1), time.sleep(0.05)))
        p._start_refetch_job()
        p._start_refetch_job()
        p._start_refetch_job()
        time.sleep(0.3)
        p._cancel_refetch()
        assert len(runs) <= 5, "held [R] fanned out into more than one run"

    # ── it says what it is doing ──

    def test_progress_is_on_the_page_while_it_runs(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.05)
        p = self._library(downloaded=[(i, "HIGH") for i in range(1, 5)])
        monkeypatch.setattr(p, "_refetch_one", lambda *a: time.sleep(0.05))
        p._user_display_name = "Garrett"
        p._start_refetch_job()
        assert _wait_for(lambda: (p._refetch_job or {}).get("done", 0) >= 1, 5)
        rendered = p._build_settings_display().plain
        assert "Re-fetching at MAX" in rendered
        assert "/4" in rendered
        assert "[Esc]" in rendered
        p._cancel_refetch()

    # ── and the bytes really change ──

    def test_a_cached_song_is_replaced_with_the_new_tier(self, monkeypatch):
        """End to end over a loopback server: the file on disk afterwards is
        the bytes the server sent, and the tracker says the new tier."""
        fresh = _mp4(audio=b"HI-RES" * 5000)
        server = _Server({"/hires.mp4": fresh})
        try:
            p = self._library(cached=[(42, "HIGH")])
            p.audio = player_mod.AudioPlayer("mpv", cache=p._cache)
            real = _track(42)
            real.get_stream = lambda: types.SimpleNamespace(
                audio_quality="HI_RES_LOSSLESS",
                get_stream_manifest=lambda: types.SimpleNamespace(
                    is_bts=True, get_urls=lambda: [server.url("/hires.mp4")]))
            p.session.track = lambda tid: real

            p._refetch_one("cache", "42", "MAX", p._refetch_gen)

            landed = cache_mod.cached_audio_path(42)
            assert landed is not None
            assert open(landed, "rb").read() == fresh
            assert p._cache.audio_record(42)["quality"] == "HI_RES_LOSSLESS"
        finally:
            server.close()

    def test_a_downloaded_song_is_replaced_in_the_music_folder(self):
        fresh = _mp4(audio=b"BETTER" * 5000)
        server = _Server({"/hires.mp4": fresh})
        try:
            p = self._library(downloaded=[(42, "HIGH")])
            real = _track(42)
            real.get_stream = lambda: types.SimpleNamespace(
                audio_quality="HI_RES_LOSSLESS",
                get_stream_manifest=lambda: types.SimpleNamespace(
                    is_bts=True, get_urls=lambda: [server.url("/hires.mp4")]))
            p.session.track = lambda tid: real

            p._refetch_one("download", "42", "MAX", p._refetch_gen)

            landed = downloads.path_for(42)
            assert landed is not None and landed.exists()
            assert b"BETTER" in landed.read_bytes()
            assert downloads.load_index()["42"]["granted"] == "HI_RES_LOSSLESS"
            # The old file was at a different path (the index row is rewritten
            # too), and nothing in the folder is a leftover of it
            files = sorted(f.name for f in downloads.download_dir().rglob("*")
                           if f.is_file())
            assert files == ["08 Get Lucky.m4a"], files
        finally:
            server.close()

    def test_a_blocked_run_says_so_on_the_page(self):
        p = self._library()
        p._refetch_job = {"state": "blocked", "done": 3, "error": "429"}
        p._user_display_name = "Garrett"
        rendered = p._build_settings_display().plain
        assert "rate-limiting" in rendered
        assert "nothing retried" in rendered


class _RecordingAudio:
    """Stands in for AudioPlayer, recording what it was asked to play."""

    def __init__(self):
        self.plays = []
        self.is_paused = False

    def play_url(self, url, seek=0, title="", cache_key=None, local=None,
                 quality=None):
        self.plays.append({"url": url, "local": local, "quality": quality})


def _downloaded(track_id=42, granted="LOSSLESS", body=b"downloaded bytes"):
    """A real file in the download folder plus the index row pointing at it."""
    root = downloads.download_dir()
    path = root / "Daft Punk" / "Random Access Memories" / "08 Get Lucky.m4a"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    downloads.record(track_id, path.relative_to(root), "HIGH", len(body),
                     granted=granted)
    return path


class TestADownloadedTrackIsNeverUpgradedBehindYourBack:
    """Garrett: "a downloaded song … should just stream from download and not
    stream data."

    The cache and the downloads used to share one rule — a copy stored below
    the tier now selected is passed over and re-fetched — and for the cache
    that is right: it is ours, disposable, and re-fetching it costs only a
    request. A download is the user's own file. Turning the quality setting up
    is not a request to re-download a song already sitting in ~/Music/Ticli,
    and doing it anyway spends his data on audio he chose to keep. Quality
    changes for a download when he asks, with `[R]`, and never otherwise.

    Everything here is asserted the way the suite asserts things: what was
    handed to the audio backend, and how many times the network was touched —
    which is zero, enforced by making every network entry point fail the test.
    """

    def _player_offline(self, quality, monkeypatch):
        p = _player(quality=quality)
        p._cache = MetadataCache(songs=True)
        p.audio = _RecordingAudio()
        # Three doors to the network, all of them nailed shut
        p._stream_description = lambda *a, **k: pytest.fail(
            "a downloaded track asked TIDAL to describe a stream")
        p._stream_url = lambda *a, **k: pytest.fail(
            "a downloaded track asked TIDAL for a stream URL")
        patch_get(monkeypatch, player_mod,
                  lambda *a, **k: pytest.fail("a downloaded track hit the CDN"))
        return p

    def _streaming_track(self, calls, tid=42, quality="LOSSLESS"):
        track = _track(tid)

        def get_stream():
            calls.append(tid)
            return types.SimpleNamespace(
                audio_quality=quality,
                get_stream_manifest=lambda: types.SimpleNamespace(
                    is_bts=True, get_urls=lambda: ["https://cdn/x.mp4"]))

        track.get_stream = get_stream
        return track

    def _play(self, p, track):
        p._play_track(track)
        assert _wait_for(lambda: p.audio.plays or not p._track_changing)
        time.sleep(0.05)

    def test_a_download_below_the_setting_plays_from_disk_for_free(self, monkeypatch):
        """The bug, in one case: downloaded at CD quality, app set to MAX."""
        p = self._player_offline("MAX", monkeypatch)
        path = _downloaded(granted="LOSSLESS")
        calls = []

        self._play(p, self._streaming_track(calls))

        assert calls == [], "the downloaded song was re-fetched at the new tier"
        assert p.audio.plays[0]["local"] == str(path)
        assert p.audio.plays[0]["url"] == ""

    def test_every_stored_tier_against_every_setting_plays_from_disk(self, monkeypatch):
        """Above, below, equal and unrecorded — the download always wins, so
        there is no combination in which the file is passed over."""
        for setting in QUALITY_CHOICES:
            for stored in ("LOW", "HIGH", "LOSSLESS", "HI_RES_LOSSLESS", None):
                p = self._player_offline(setting, monkeypatch)
                path = _downloaded(granted=stored)
                calls = []

                self._play(p, self._streaming_track(calls))

                assert calls == [], f"{stored} download re-fetched at {setting}"
                assert p.audio.plays[0]["local"] == str(path), (setting, stored)

    def test_the_bytes_played_are_the_downloaded_bytes(self, monkeypatch):
        """The path is only a claim; the file behind it is the audio."""
        p = self._player_offline("MAX", monkeypatch)
        _downloaded(granted="LOW", body=b"the file he downloaded")

        self._play(p, self._streaming_track([]))

        played = p.audio.plays[0]["local"]
        with open(played, "rb") as fh:
            assert fh.read() == b"the file he downloaded"

    def test_the_prefetch_does_not_spend_a_request_either(self, monkeypatch):
        """The URL would be fetched moments before `_play_track` threw it
        away — the same wasted request, one track earlier."""
        p = self._player_offline("MAX", monkeypatch)
        _downloaded(track_id=43, granted="HIGH")
        calls = []
        upcoming = self._streaming_track(calls, tid=43)
        p._queue = [_track(42), upcoming]
        p._queue_index = 0
        p._current_track = p._queue[0]
        p._current_track.duration = 100
        p._playing = True
        p._play_offset = 95   # inside PREFETCH_LEAD of the end

        p._maybe_prefetch_next()
        time.sleep(0.05)

        assert calls == []

    def test_a_download_deleted_by_hand_falls_through_to_the_network(self):
        """The index still claims it; the disk decides. Unchanged by the new
        rule, and the reason `path_for` stats the file every time."""
        p = _player(quality="HIGH")
        p._cache = MetadataCache(songs=True)
        p.audio = _RecordingAudio()
        _downloaded(granted="LOSSLESS").unlink()
        calls = []

        self._play(p, self._streaming_track(calls))

        assert calls == [42], "a file that is gone was played anyway"
        assert p.audio.plays[0]["local"] is None
        assert p.audio.plays[0]["url"] == "https://cdn/x.mp4"

    def test_a_cached_copy_below_the_setting_is_still_re_fetched(self):
        """The other half of the rule, and the one that did not change. The
        cache is machine-owned and disposable, so a stale tier there is worth
        one request; a download is the user's file and is not ours to replace.
        Same player, same tier, opposite answers."""
        p = _player(quality="MAX")
        p._cache = MetadataCache(songs=True)
        p.audio = _RecordingAudio()
        directory = cache_mod.audio_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "42.m4a").write_bytes(b"cached bytes")
        p._cache.note_cached(42, ".m4a", 12, quality="LOSSLESS")
        calls = []

        self._play(p, self._streaming_track(calls))

        assert calls == [42], "the cache rule was loosened along with downloads"
        assert p.audio.plays[0]["local"] is None

    def test_nothing_on_these_paths_writes_to_the_music_folder(self, monkeypatch):
        """Playing is reading. The one file in the folder is the one the test
        put there, before and after."""
        p = self._player_offline("MAX", monkeypatch)
        _downloaded(granted="LOW")
        root = downloads.download_dir()
        before = sorted(str(f.relative_to(root)) for f in root.rglob("*")
                        if f.is_file())

        self._play(p, self._streaming_track([]))

        after = sorted(str(f.relative_to(root)) for f in root.rglob("*")
                       if f.is_file())
        assert after == before == ["Daft Punk/Random Access Memories/"
                                   "08 Get Lucky.m4a"]

    def test_the_player_says_what_it_is_really_playing(self, monkeypatch):
        """Honesty in the interface. A badge reading MAX over a HIGH
        download is the same kind of claim as a quality menu offering tiers
        TIDAL never served. It says the tier the file holds and where the
        file came from — on the status line it was already drawing, so
        nothing is toasted once per track of a downloaded album."""
        p = self._player_offline("MAX", monkeypatch)
        _downloaded(granted="LOSSLESS")

        self._play(p, self._streaming_track([]))
        text = _rendered(p)

        assert "16/44.1 FLAC · downloaded" in text
        assert "24/192" not in text, "it claimed the tier of the setting"

    def test_an_unrecorded_tier_claims_nothing(self, monkeypatch):
        """A download adopted before the index recorded tiers. "We do not
        know" is an honest badge; the setting's label would not be."""
        p = self._player_offline("MAX", monkeypatch)
        _downloaded(granted=None)

        self._play(p, self._streaming_track([]))
        text = _rendered(p)

        assert "downloaded" in text
        assert "24/192" not in text

    def test_a_streamed_track_still_shows_the_setting(self):
        """The badge is not a permanent takeover — the setting is the truth
        again the moment the bytes come off the network."""
        p = _player(quality="HIGH")
        p._cache = MetadataCache(songs=False)
        p.audio = _RecordingAudio()
        _downloaded(granted="HIGH")
        self._play(p, self._streaming_track([]))
        assert "downloaded" in _rendered(p)

        self._play(p, self._streaming_track([], tid=99))

        text = _rendered(p)
        assert "downloaded" not in text
        assert "16/44.1 FLAC" in text


class TestSettingsShowsTheFolder:
    def test_the_folder_is_shown_as_a_full_absolute_path(self):
        """Garrett asked for the absolute path rather than `~/Music/Ticli` —
        one you can paste into Finder. Not `resolve()`, which on macOS turns
        /Users/... into /System/Volumes/Data/Users/... through the firmlink."""
        shown = downloads.display_dir()
        assert shown.startswith("/")
        assert "~" not in shown
        assert "/System/Volumes/Data/" not in shown

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
        assert "Downloads     1 song " in text
        # Exemption from the budget is a fact about the number beside it, not
        # prose about the feature, so it is on the row and survives a short
        # window the way the count and the path do
        assert "exempt from the budget" in text


class TestSeeingAndRemovingWhatYouHave:
    """The downloads list and its delete.

    Garrett: downloads were "really incomplete and pretty janky" — the only
    evidence of one was a count on the settings page — and, on a delete,
    "yes, with a confirmation".

    Deleting is a policy change and this class is written as one. The rules
    that did *not* change are the ones asserted hardest: the exact recorded
    path and nothing else, no directory ever, nothing touched before the
    answer comes back, and a file ticli did not write is not reachable at all.
    Every assertion here is about bytes on disk or about the grid a real
    terminal would show — never about a function's return value alone.
    """

    def _download(self, tid, artist="Daft Punk", album="RAM", name="Get Lucky",
                  ext=".flac", data=b"music", granted=None):
        root = downloads.download_dir()
        path = root / artist / album / f"{tid:02d} {name}{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        downloads.record(tid, path.relative_to(root), "HIGH", len(data),
                         granted=granted)
        return path

    def _open(self, player):
        player._mode = player.MODE_SETTINGS
        player._handle_key("d")
        return player

    def _list(self, player) -> str:
        return player._build_downloads_display().plain

    # ── the list ──

    def test_the_downloads_row_opens_the_list(self):
        self._download(1)
        p = _player()
        p._user_display_name = "Garrett"
        assert "[d] list" in p._build_settings_display().plain
        self._open(p)
        assert p._mode == p.MODE_DOWNLOADS
        # ← comes back to the settings row you pressed [d] on, not the player
        p._handle_key("\x1b")
        assert p._mode == p.MODE_SETTINGS

    def test_the_list_shows_title_artist_tier_and_size(self):
        self._download(7, artist="Fleetwood Mac", album="Rumours",
                       name="Dreams", ext=".m4a", data=b"x" * 4096,
                       granted=HeadlessTidalPlayer.QUALITY_MAP["MEDIUM"])
        text = self._list(self._open(_player()))
        assert "Dreams" in text
        assert "Fleetwood Mac" in text
        # The tier TIDAL *granted*, not the one that was asked for — the
        # record above asked HIGH (FLAC) and was handed 320k AAC
        assert "MEDIUM" in text and "HIGH" not in text
        # Bytes on this disk, so no `~`
        assert "4.0 KB" in text
        assert "~" not in text

    def test_an_empty_folder_says_so_rather_than_showing_nothing(self):
        text = self._list(self._open(_player()))
        assert "Nothing downloaded yet" in text
        assert str(downloads.download_dir()) in text

    def test_a_file_deleted_by_hand_is_not_listed(self):
        kept = self._download(1, name="Get Lucky")
        gone = self._download(2, name="Doin It Right")
        gone.unlink()          # the user, in Finder
        p = self._open(_player())
        text = self._list(p)
        assert "Get Lucky" in text
        assert "Doin It Right" not in text
        # And the index row it left behind is not a crash waiting for a
        # keypress: paging and [x] both still work over what is really there
        p._handle_key("\x1b[B")
        p._handle_key("x")
        assert p._downloads_delete["title"] == "Get Lucky"
        assert kept.exists()

    # ── the delete ──

    def test_a_delete_removes_that_exact_file_and_nothing_else(self):
        doomed = self._download(1, name="Dreams", data=b"a" * 100)
        spared = self._download(2, name="Get Lucky", data=b"b" * 100)
        sibling = self._download(3, artist="Sufjan Stevens", album="Illinois",
                                 name="Chicago", data=b"c" * 100)
        p = self._open(_player())
        titles = [r["title"] for r in p._download_rows()]
        p._downloads_cursor = titles.index("Dreams")

        p._handle_key("x")
        assert doomed.exists(), "nothing may be touched before the answer"
        p._handle_key("y")

        assert not doomed.exists()
        assert spared.read_bytes() == b"b" * 100
        assert sibling.read_bytes() == b"c" * 100
        # The folder the file was in stays, empty or not — ~/Music is the
        # user's and ticli removes no directory at all
        assert doomed.parent.is_dir()
        assert doomed.parent.parent.is_dir()
        assert downloads.download_dir().is_dir()
        # And the index row went with the file
        assert set(downloads.load_index()) == {"2", "3"}

    def test_cancelling_leaves_the_file_and_the_index_row_alone(self):
        path = self._download(1, name="Dreams", data=b"a" * 100)
        before = json.loads(downloads.index_file().read_text())
        p = self._open(_player())

        p._handle_key("x")
        p._handle_key("n")      # anything that is not y

        assert p._downloads_delete is None
        assert path.read_bytes() == b"a" * 100
        assert json.loads(downloads.index_file().read_text()) == before
        assert "Dreams" in self._list(p)

    def test_esc_also_cancels_and_deletes_nothing(self):
        path = self._download(1, name="Dreams")
        p = self._open(_player())
        p._handle_key("x")
        p._handle_key("\x1b")
        assert path.exists()
        assert downloads.load_index()

    def test_a_decoy_file_in_the_music_folder_survives_everything(self):
        """The download tier's `important.txt`.

        Mirrors test_cache's decoy tests exactly: somebody's own files live
        in ~/Music, and a delete that reached one would be unforgivable.
        `remove()` starts from an index row, so a file ticli did not write
        has no path into it — asserted rather than trusted.
        """
        root = downloads.download_dir()
        song = self._download(1, name="Dreams")
        decoy = root / "important.txt"
        decoy.write_text("tax return")
        mixtape = song.parent / "mixtape.flac"
        mixtape.write_bytes(b"not ours")
        stray_dir = root / "Bootlegs"
        stray_dir.mkdir()

        p = self._open(_player())
        p._handle_key("x")
        p._handle_key("y")
        # And an id that is in no index row at all
        assert downloads.remove(999) is False

        assert not song.exists()
        assert decoy.read_text() == "tax return"
        assert mixtape.read_bytes() == b"not ours"
        assert stray_dir.is_dir()

    def test_deleting_the_last_row_leaves_a_list_that_still_draws(self):
        self._download(1, name="Dreams")
        p = self._open(_player())
        p._handle_key("x")
        p._handle_key("y")
        assert "Nothing downloaded yet" in self._list(p)
        p._handle_key("x")      # nothing to ask about
        assert p._downloads_delete is None

    # ── the marker ──

    def test_a_downloaded_track_is_marked_in_browse_queue_and_search(self):
        self._download(1, name="Get Lucky")
        p = _player()
        p._forget_downloads()
        have, missing = _track(1), _track(2)
        missing.name = "Doin It Right"

        p._browse_tracks = [have, missing]
        browse = p._build_browse_display().plain
        p._queue = [have, missing]
        queue = p._build_queue_display().plain
        p._search_results = [{"type": "track", "name": t.name, "artist": "",
                              "obj": t} for t in (have, missing)]
        search = p._build_search_display().plain

        for text in (browse, queue, search):
            marked = [line for line in text.splitlines() if "↓" in line]
            assert len(marked) == 1, text
            assert "Get Lucky" in marked[0]
            assert "Doin It Right" not in marked[0]

    def test_the_marker_follows_a_delete_without_a_restart(self):
        self._download(1, name="Get Lucky")
        p = _player()
        p._browse_tracks = [_track(1)]
        assert "↓" in p._build_browse_display().plain
        self._open(p)
        p._handle_key("x")
        p._handle_key("y")
        p._mode = p.MODE_BROWSE
        assert "↓" not in p._build_browse_display().plain

    # ── what it costs ──

    def test_painting_two_hundred_marked_rows_costs_one_index_read(self, monkeypatch):
        """Finding F6 of ai/reference/data-path-audit-2026-07-26.md, which is
        what a `path_for` per row would be: 229 ms of UI-thread JSON at 500
        entries, twice a second. Assert the reads, not the milliseconds."""
        for tid in range(200):
            self._download(tid, name=f"Song {tid}")
        p = _player()
        p._page_size = 200
        p._browse_tracks = [_track(tid) for tid in range(200)]
        p._forget_downloads()

        reads = []
        real = downloads.load_index

        def _load():
            reads.append(1)
            return real()

        monkeypatch.setattr(downloads, "load_index", _load)

        text = p._build_browse_display().plain
        assert text.count("↓") == 200
        assert len(reads) == 1, f"{len(reads)} index reads for one page"

        # And the next nine frames re-measure nothing at all
        for _ in range(9):
            p._build_browse_display()
        assert len(reads) == 1

    def test_the_list_costs_one_index_read_however_long_it_is(self, monkeypatch):
        for tid in range(200):
            self._download(tid, name=f"Song {tid}")
        p = self._open(_player())
        p._page_size = 200
        reads = []
        real = downloads.load_index

        def _load():
            reads.append(1)
            return real()

        monkeypatch.setattr(downloads, "load_index", _load)
        assert self._list(p).count("↓") == 200
        assert len(reads) == 1

    def test_the_list_and_the_markers_make_no_request_at_all(self, monkeypatch):
        """ai/INCIDENTS #1. Nothing on this screen needs the network: every
        field comes off the path the downloader wrote or the index row beside
        it, which is why there is no [Enter] to play a row."""
        def _refuse(*a, **k):
            raise AssertionError("the downloads screen made a request")

        for tid in range(5):
            self._download(tid, name=f"Song {tid}")
        p = _player()
        p.session = types.SimpleNamespace(
            audio_quality=None, is_pkce=False, track=_refuse, request=_refuse)
        # Patched after the player is built, so what is being pinned is the
        # screen rather than the constructor
        for name in ("get", "post", "put", "delete", "request", "Session"):
            monkeypatch.setattr(player_mod.requests, name, _refuse)
        self._open(p)
        self._list(p)
        p._handle_key("\x1b[B")
        p._handle_key("x")
        p._handle_key("y")
        p._browse_tracks = [_track(1)]
        p._build_browse_display()
        assert len(downloads.load_index()) == 4

    # ── the whole grid ──

    def test_the_list_fits_eighty_by_twentyfour(self):
        from rich.console import Console

        for tid in range(40):
            self._download(tid, name=f"A Reasonably Long Song Title {tid}",
                           data=b"x" * 300_000)
        p = self._open(_player())
        console = Console(width=80, height=24)
        p.console = console
        with console.capture() as cap:
            console.print(p._build_display())
        lines = cap.get().splitlines()
        assert len(lines) <= 24, f"the pane is {len(lines)} rows in 24"
        body = "\n".join(lines)
        assert "Downloads" in body
        assert "[←/Esc]" in body

    def test_the_confirmation_is_on_screen_whole_at_eighty_columns(self):
        from rich.console import Console

        self._download(1, name="Come On! Feel the Illinoise!")
        p = self._open(_player())
        p._handle_key("x")
        console = Console(width=80, height=24)
        p.console = console
        with console.capture() as cap:
            console.print(p._build_display())
        text = cap.get()
        assert "Delete Come On! Feel the Illinoise! from your music folder?" in text
        # The part that says how to answer must never be the part that is cut
        assert "y to confirm, any other key to cancel" in text


def _run_together(jobs, timeout=30.0):
    """Start every job on its own thread, wait for all of them, re-raise.

    Same shape as test_cache's helper, for the same reason: a failure inside
    a thread is otherwise a printed traceback and a green test, and a writer
    still alive after the timeout is a deadlock, not a race.
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
        f"{len(alive)} index writer(s) never finished — a deadlock, not a race"
    if errors:
        raise errors[0]


def _widen_the_window(monkeypatch, pause=0.002):
    """Hold every index read open for a moment, so the race is certain.

    The losing interleave is a thread switch between `_mutate_index`'s load
    and its save. That window is real — the bulk runner's writer thread
    records a finished download while [x] deletes another on the UI thread —
    but far too narrow for a test to land on by throwing threads at it.
    Sleeping inside `load_index` makes the scheduler's worst case happen
    every run instead of once in a thousand: without the lock every overlap
    loses a writer's edit and the assertions below fail flat; with it the
    second writer waits at the door and the sleep only slows the test down.

    Patched on the module, which is where `_mutate_index` looks it up, so
    the sleep lands inside the locked cycle.
    """
    original = downloads.load_index

    def slow_load():
        tracks = original()
        time.sleep(pause)
        return tracks

    monkeypatch.setattr(downloads, "load_index", slow_load)


class TestIndexWritesAreSerialized:
    """`record` and `remove` are both load-modify-save over downloads.json,
    and they run on different threads: the bulk runner's single writer
    thread commits finished downloads while the downloads list's [x] runs
    `remove` on the UI thread, reachable the whole time a run is going. The
    runner's one-thread-calls-the-callables design serializes the runner
    against itself and nothing else — `_index_lock` is what serializes the
    pair.

    Losing the race one way resurrects a deleted row, which is mostly
    harmless because every read stats the disk. The other way is not: a
    just-finished download's row vanishes, the file sits in the music folder
    unindexed, shows as not downloaded, and would be re-downloaded — an
    orphan in the one tier where nothing may clean up. Timing tests, so they
    are written to fail rather than flake: fixed work per thread, a widened
    window, and assertions on the exact final bytes of the real index file
    and the real files under a tmp download root.
    """

    def _file(self, tid, name, data=b"a" * 64):
        root = downloads.download_dir()
        path = root / "Fleetwood Mac" / "Rumours" / f"{tid:02d} {name}.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _rows_on_disk(self):
        data = json.loads(downloads.index_file().read_text())
        assert data["version"] == downloads.INDEX_VERSION
        return data["tracks"]

    def _record_beside_remove(self, monkeypatch, record_starts_first):
        """One finished download recorded beside one [x] delete. Both orders
        lose without the lock — remove saving last erases the fresh row,
        record saving last resurrects the deleted one — and the final file
        must show neither."""
        doomed = self._file(2, "Gold Dust Woman")
        root = downloads.download_dir()
        downloads.record(2, doomed.relative_to(root), "HIGH",
                         doomed.stat().st_size)
        fresh = self._file(1, "Dreams")
        _widen_the_window(monkeypatch)

        def commit():
            downloads.record(1, fresh.relative_to(root), "HIGH",
                             fresh.stat().st_size)

        def delete():
            assert downloads.remove(2) is True

        jobs = [commit, delete] if record_starts_first else [delete, commit]
        _run_together(jobs)

        rows = self._rows_on_disk()
        assert "1" in rows, \
            "a just-finished download's row was erased by the delete"
        assert rows["1"]["path"] == str(fresh.relative_to(root))
        assert "2" not in rows, "the deleted row came back"
        assert not doomed.exists()
        assert fresh.read_bytes() == b"a" * 64

    def test_a_record_landing_beside_a_delete_keeps_both_outcomes(self, monkeypatch):
        self._record_beside_remove(monkeypatch, record_starts_first=True)

    def test_the_same_pair_the_other_way_round_loses_nothing_either(self, monkeypatch):
        self._record_beside_remove(monkeypatch, record_starts_first=False)

    def test_a_hammer_of_concurrent_records_keeps_every_row(self, monkeypatch):
        """Four threads recording distinct tracks at once. Unlocked, every
        overlap keeps only the later writer's whole dict, so most of the
        rows never reach the file; the final file must hold all of them."""
        root = downloads.download_dir()
        rounds, lanes = 10, 4
        paths = {}
        for lane in range(lanes):
            for n in range(rounds):
                tid = lane * 100 + n
                paths[tid] = self._file(tid, f"Song {tid}", data=b"x" * (tid + 1))
        _widen_the_window(monkeypatch)

        def writer(lane):
            def job():
                for n in range(rounds):
                    tid = lane * 100 + n
                    downloads.record(tid, paths[tid].relative_to(root),
                                     "HIGH", tid + 1)
            return job

        _run_together([writer(lane) for lane in range(lanes)])

        rows = self._rows_on_disk()
        assert set(rows) == {str(tid) for tid in paths}, \
            "a concurrent record's row never made it to the file"
        for tid, path in paths.items():
            assert rows[str(tid)]["path"] == str(path.relative_to(root))
            assert rows[str(tid)]["bytes"] == tid + 1

    def test_record_and_remove_hold_the_lock_across_the_save(self, monkeypatch):
        """Structural: every write reaches `_save_index` with `_index_lock`
        held, so a new writer that bypassed `_mutate_index` fails here
        rather than one run in a thousand."""
        held = []
        original = downloads._save_index

        def guarded(tracks):
            held.append(downloads._index_lock.locked())
            original(tracks)

        monkeypatch.setattr(downloads, "_save_index", guarded)
        path = self._file(1, "Dreams")
        downloads.record(1, path.relative_to(downloads.download_dir()),
                         "HIGH", 64)
        assert downloads.remove(1) is True
        assert held == [True, True], \
            "a writer reached _save_index without holding _index_lock"
        assert self._rows_on_disk() == {}
        assert not path.exists()
