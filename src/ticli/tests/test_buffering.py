"""How much of a segmented stream the backend keeps in front of the speaker.

A lossless track is fetched four seconds at a time off the network while it
plays. Whatever the player has already buffered is the entire margin it has
against a slow segment, and a segment that arrives later than that margin is
an audible hitch — reported as "hitching around 1:04 and 1:25".

The margin was one second. mpv's --cache defaults to "auto", which means
"enabled for a network stream", and the thing ticli hands it is a *local*
playlist file, so mpv classified the stream as local and left the cache off;
readahead fell back to --demuxer-readahead-secs, default 1. Measured over IPC
against real TIDAL segments, `demuxer-cache-duration` was pinned at 1.02s for
the whole track.

These tests run the real mpv against a loopback server that serves real
fragmented-MP4 segments and delays one of them longer than that old margin.
The assertions are on what the backend itself reports: how deep its buffer
got, and whether it ever had to stop for want of data.
"""

import http.server
import json
import os
import shutil
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

from ticli import player as player_mod
from ticli.player import (
    HLS_CACHE_SECONDS,
    AudioPlayer,
    HeadlessTidalPlayer,
    _hls_playlist,
    _write_hls_playlist,
)

FFMPEG = shutil.which("ffmpeg")
MPV = shutil.which("mpv")

SEGMENT_SECONDS = 2.0
TRACK_SECONDS = 24
# Longer than the one second of buffer the old flags left — a hiccup a
# correctly-buffered player absorbs without a gap. Not one of the first
# segments: at the very start of a track *nothing* is buffered yet, so a slow
# segment there stalls any player, cache flags or not (measured: with segment
# 2 delayed, mpv reports paused-for-cache once at 2.1 s and then runs 21 s
# ahead for the rest of the track). Mid-track is where the cache is the whole
# difference.
SLOW_SEGMENT = 6
SLOW_BY = 2.5
# Where a signed URL stops working part way through a track. Early enough
# that what plays is unmistakably a fraction of the song.
EXPIRE_FROM = 3


def _fragmented_flac(path: Path) -> Path:
    """A real fragmented-MP4 FLAC file, shaped like the ones TIDAL serves."""
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={TRACK_SECONDS}:sample_rate=44100",
         "-ac", "2", "-c:a", "flac", "-f", "mp4",
         "-movflags", "frag_keyframe+empty_moov+dash",
         "-frag_duration", str(int(SEGMENT_SECONDS * 1_000_000)), str(path)],
        check=True, capture_output=True)
    return path


def _split(path: Path):
    """The file cut back into the init segment and the media segments it was
    concatenated from — the same shape `_start_download` writes out.

    One fragment is one `moof`/`mdat` pair, preceded by its own `sidx` when
    ffmpeg wrote one. This walked the boxes and kept the first `sidx` only, so
    it returned the whole 24-second track as a **single** segment: the delayed
    segment was never requested and the buffering assertions were passing over
    a stream that had nothing to buffer. Found while building the expiry test,
    which needs segment 3 to exist before it can refuse to serve it.
    """
    data = path.read_bytes()
    boxes, off = [], 0
    while off + 8 <= len(data):
        size = struct.unpack(">I", data[off:off + 4])[0]
        typ = data[off + 4:off + 8]
        if size == 1:
            size = struct.unpack(">Q", data[off + 8:off + 16])[0]
        if size <= 0:
            break
        boxes.append((off, typ))
        off += size
    cuts = []
    for index, (start, typ) in enumerate(boxes):
        if typ != b"moof":
            continue
        previous = boxes[index - 1] if index else None
        # The index box belongs to the fragment it describes
        cuts.append(previous[0] if previous and previous[1] == b"sidx" else start)
    init = data[:cuts[0]]
    segments = [data[a:b] for a, b in zip(cuts, cuts[1:] + [len(data)])]
    return init, segments


class _Server:
    """Serves the segments, delaying one of them.

    `expire_from` makes every segment at or past that index answer
    `403 Request has expired` — which is what a signed TIDAL URL does about an
    hour after it was issued, and what any network blip looks like from here.
    """

    def __init__(self, init, segments, expire_from=None):
        served = []
        self.served = served

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                name = self.path.strip("/")
                if name == "init.mp4":
                    body = init
                else:
                    index = int(name.split(".")[0])
                    if expire_from is not None and index >= expire_from:
                        served.append(name)
                        body = b"Request has expired"
                        self.send_response(403)
                        self.send_header("Content-Type", "text/plain")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        try:
                            self.wfile.write(body)
                        except OSError:
                            pass
                        return
                    body = segments[index]
                    if index == SLOW_SEGMENT:
                        time.sleep(SLOW_BY)
                served.append(name)
                self.send_response(200)
                self.send_header("Content-Type", "audio/mp4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:  # mpv closed the connection; not our business
                    pass

        class Threaded(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Threaded(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def _ask(ipc_path, prop):
    """One property out of a running mpv."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect(ipc_path)
        sock.sendall(json.dumps({"command": ["get_property", prop]}).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                return None
            buf += chunk
        for line in buf.split(b"\n"):
            if line:
                reply = json.loads(line)
                if "error" in reply:
                    return reply.get("data")
    except (OSError, ValueError):
        return None
    finally:
        sock.close()
    return None


class _Dash:
    """Just enough of tidalapi's dash_info for `_hls_playlist`."""

    def __init__(self, port, count):
        base = f"http://127.0.0.1:{port}"
        self.first_url = f"{base}/init.mp4"
        self.urls = [self.first_url] + [f"{base}/{i}.m4s" for i in range(count)]
        self.timescale = 44100
        self.chunk_size = int(SEGMENT_SECONDS * 44100)
        self.last_chunk_size = self.chunk_size


def _play(seconds=9.0):
    """Play the generated track over loopback with ticli's real mpv flags,
    sampling what mpv says about its own buffer. Returns the samples."""
    with tempfile.TemporaryDirectory() as tmp:
        source = _fragmented_flac(Path(tmp) / "frag.mp4")
        init, segments = _split(source)
        server = _Server(init, segments)
        ipc = os.path.join(tmp, "mpv.sock")
        try:
            playlist = _write_hls_playlist(
                "test-buffering", _hls_playlist(_Dash(server.port, len(segments))))
            flags = AudioPlayer("mpv")._hls_flags()
            proc = subprocess.Popen(
                # --ao=null still runs in real time, so the timings mean what
                # they would with a sound card, without making noise on the
                # machine running the suite
                ["mpv", "--no-video", "--ao=null", "--msg-level=all=error",
                 f"--input-ipc-server={ipc}"] + flags + [playlist],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while not os.path.exists(ipc) and time.monotonic() < deadline:
                    if proc.poll() is not None:
                        pytest.fail("mpv exited before it opened its IPC socket")
                    time.sleep(0.05)
                samples = []
                end = time.monotonic() + seconds
                while time.monotonic() < end and proc.poll() is None:
                    samples.append((
                        _ask(ipc, "time-pos"),
                        _ask(ipc, "demuxer-cache-duration"),
                        _ask(ipc, "paused-for-cache"),
                    ))
                    time.sleep(0.25)
                return samples
            finally:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    proc.kill()
        finally:
            server.close()


def _play_until_it_dies(backend, seconds=25.0):
    """Play a stream that expires part way through, with ticli's real flags,
    through a real AudioPlayer. Returns (exit status, failure(), how long)."""
    with tempfile.TemporaryDirectory() as tmp:
        source = _fragmented_flac(Path(tmp) / "frag.mp4")
        init, segments = _split(source)
        server = _Server(init, segments, expire_from=EXPIRE_FROM)
        try:
            playlist = _write_hls_playlist(
                "test-expiry", _hls_playlist(_Dash(server.port, len(segments))))
            audio = AudioPlayer(backend)
            audio.volume = 0
            started = time.monotonic()
            audio.play_url(playlist, cache_key=None)
            proc = audio._process
            try:
                proc.wait(timeout=seconds)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
                pytest.fail(f"{backend} never exited")
            return proc.returncode, audio.failure(), time.monotonic() - started
        finally:
            try:
                audio.stop()
            except Exception:  # pragma: no cover
                pass
            server.close()


@pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg to build real segments")
class TestADeadStreamIsIndistinguishableFromAnEnding:
    """The measurement finding 2 rests on, against real backends.

    A stream that expires mid-track does not error. Both backends play out
    what they had buffered and then exit **0 with an empty stderr** — byte for
    byte what reaching the end of the track looks like. So `failure()` says
    nothing (correctly, by its own contract) and the monitor cannot tell the
    difference from the process alone. Only the clock can, which is what
    `_stream_ended_early` is.
    """

    @pytest.mark.parametrize("backend", ["mpv", "ffplay"])
    def test_the_backend_exits_zero_with_nothing_to_say(self, backend):
        if not shutil.which(backend):
            pytest.skip(f"{backend} is not installed")
        status, failure, elapsed = _play_until_it_dies(backend)
        assert status == 0, f"{backend} exited {status}, not 0"
        assert failure is None, f"{backend} reported {failure!r}"
        assert elapsed < TRACK_SECONDS - 4, \
            "the stream should have died well short of the end"

    def test_the_clock_is_what_notices(self):
        """The whole of the fix, stated against the numbers above: a track
        that stopped this far short of its stated end did not end."""
        p = HeadlessTidalPlayer()
        p._current_track = types.SimpleNamespace(id=1, duration=TRACK_SECONDS)
        p._play_offset = EXPIRE_FROM * SEGMENT_SECONDS
        assert p._stream_ended_early() is True
        p._play_offset = TRACK_SECONDS - 1
        assert p._stream_ended_early() is False


class TestSegmentedFlags:
    """The flags themselves, so a machine without mpv still notices them going
    missing."""

    def test_mpv_is_told_to_cache_a_segmented_stream(self):
        flags = AudioPlayer("mpv")._hls_flags()
        assert "--cache=yes" in flags
        assert f"--cache-secs={HLS_CACHE_SECONDS}" in flags

    def test_the_buffer_is_deeper_than_a_segment(self):
        # The whole point: a margin measured in segments, not in one second
        assert HLS_CACHE_SECONDS > 4 * SEGMENT_SECONDS

    def test_ffplay_is_not_given_mpv_flags(self):
        flags = AudioPlayer("ffplay")._hls_flags()
        assert not [f for f in flags if f.startswith("--cache")]

    def test_ffplay_is_told_to_buffer_a_segmented_stream(self):
        assert "-infbuf" in AudioPlayer("ffplay")._hls_flags()

    def test_a_whole_file_needs_no_buffering_flag(self):
        """Segmented only. On the BTS path both backends already read the
        whole file at once (ffplay 23.7 MB in the first second), so the flag
        would be an unbounded buffer bought for nothing."""
        audio = AudioPlayer("ffplay")
        assert "-infbuf" not in audio._ffplay_cmd("https://cdn/track.mp4", 0)
        assert "-infbuf" in audio._ffplay_cmd("/tmp/x" + player_mod.HLS_SUFFIX, 0)


def _segments_pulled_in(seconds, extra_flags=(), drop=()):
    """How many distinct media segments ffplay asks for in the first
    `seconds`, playing ticli's own playlist with ticli's own flags."""
    with tempfile.TemporaryDirectory() as tmp:
        source = _fragmented_flac(Path(tmp) / "frag.mp4")
        init, segments = _split(source)
        server = _Server(init, segments)
        try:
            playlist = _write_hls_playlist(
                "test-readahead", _hls_playlist(_Dash(server.port, len(segments))))
            flags = [f for f in AudioPlayer("ffplay")._hls_flags() if f not in drop]
            proc = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error",
                 "-volume", "0"] + list(extra_flags) + flags + [playlist],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(seconds)
                return len({n for n in server.served if n.endswith(".m4s")}), \
                    len(segments)
            finally:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    proc.kill()
        finally:
            server.close()


@pytest.mark.skipif(not FFMPEG or not shutil.which("ffplay"),
                    reason="needs ffplay and ffmpeg to measure readahead")
class TestFfplayReadahead:
    """ffplay had the weakness mpv had: about two segments of buffer.

    Its read thread stops once every stream has enough packets queued, which
    on the 45-segment hi-res playlist measured 1.40 MB — the same level the
    un-fixed mpv sat at, and the level that was reported as hitching.
    `-infbuf` lifts the limit; there is no `--cache-secs` analogue, so the
    choice is between ~11 s and the whole (bounded) track.
    """

    # Real time, real sockets, 12 two-second segments. Measured over this
    # window: with the flag ffplay has all 12; without it, it plateaus at 6
    # and takes them one at a time as playback consumes them.
    WINDOW = 4.0

    def test_the_whole_track_is_pulled_at_once(self):
        pulled, total = _segments_pulled_in(self.WINDOW)
        assert pulled == total, \
            f"only {pulled} of {total} segments in {self.WINDOW}s"

    def test_and_without_the_flag_it_is_not(self):
        """The control. Without this the test above would pass on a track
        short enough to fit in ffplay's default queue anyway."""
        pulled, total = _segments_pulled_in(self.WINDOW, drop=("-infbuf",))
        assert pulled < total, \
            "ffplay buffered the whole track without being asked to — the " \
            "measurement this rests on no longer holds"


@pytest.mark.skipif(not MPV or not FFMPEG,
                    reason="needs mpv and ffmpeg to play real segments")
class TestSegmentedBuffering:
    """What the running backend actually does with real segments off a socket.

    One playback serves both assertions — starting mpv twice would double a
    slow test for no more evidence.
    """

    @classmethod
    def setup_class(cls):
        cls.samples = _play()

    def test_it_played(self):
        positions = [p for p, _, _ in self.samples if isinstance(p, (int, float))]
        assert positions, "mpv never reported a position"
        assert max(positions) > 3.0, "mpv barely got started"

    def test_it_buffers_far_more_than_the_one_second_it_used_to(self):
        depths = [c for _, c, _ in self.samples if isinstance(c, (int, float))]
        assert depths, "mpv never reported its cache duration"
        # Pinned at 1.02s before the fix; with the cache on it runs to the
        # end of this short track
        assert max(depths) > 5.0, f"buffer never got past {max(depths):.2f}s"

    def test_a_segment_slower_than_the_old_margin_is_not_a_hitch(self):
        assert not any(stalled for _, _, stalled in self.samples), \
            "mpv stopped for want of data"
