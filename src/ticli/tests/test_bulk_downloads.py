"""Tests for downloading a whole playlist, album, section or queue.

Three things are under test and they pull in opposite directions, which is
why they are asserted separately and in terms of what can be observed:

* **The API rate did not change.** Resolving is still serial and still paced,
  so a twelve-track run makes twelve stream requests and never has two in
  flight. The assertions are request *counts* and in-flight peaks, because
  ai/INCIDENTS #1 is the worst thing that has happened to this project and a
  flag can be set by a run that kept going.
* **The fetch really is three-wide.** `fetch_to_file` is instrumented where
  the player actually calls it, and the peak has to be 3 — not 1, which would
  mean the feature does nothing, and not 12.
* **The bytes are right.** Every track lands with the audio the server sent,
  the index keeps every row despite three workers, and a cancel leaves no
  half-written file anywhere.

Nothing here touches the network or the owner's `~/Music`: the download root,
cache directory and config file are redirected by the fixture, and the CDN is
a loopback HTTP server.
"""

import inspect
import threading
import time
import types
import unittest.mock as mock

import pytest
from rich.cells import cell_len

from ticli import player as player_mod
from ticli.player import HeadlessTidalPlayer
from ticli.tests.fakes import patch_get
from ticli.tests.test_downloads import (
    _Server, _box_text, _mp4, _player, _track, _wait_for, isolated)  # noqa: F401
from ticli.utils import cache as cache_mod
from ticli.utils import downloads
from ticli.utils.cache import MetadataCache
from ticli.utils.config import QUALITY_CHOICES


def _bulk_track(tid, server):
    """A track whose stream is a route of its own, so twelve of them are
    twelve distinguishable files in twelve distinguishable places."""
    track = _track(tid=tid)
    track.name = f"Track {tid}"
    track.track_num = tid
    track.duration = 100 + tid

    def _stream():
        return types.SimpleNamespace(
            audio_quality="HIGH",
            get_stream_manifest=lambda: types.SimpleNamespace(
                is_bts=True, get_urls=lambda: [server.url(f"/t{tid}.mp4")]))

    track.get_stream = _stream
    return track


def _bulk_library(count):
    """`(player, tracks, server, payloads)` — `count` tracks, one route each."""
    payloads = {tid: _mp4(audio=bytes([tid % 251]) * 3000)
                for tid in range(1, count + 1)}
    server = _Server({f"/t{tid}.mp4": body for tid, body in payloads.items()})
    p = _player()
    tracks = [_bulk_track(tid, server) for tid in range(1, count + 1)]
    p._download_tracks = tracks
    p._download_track = tracks[0]
    p._download_label = "A Playlist"
    return p, tracks, server, payloads


def _counting(tracks, calls, fail=None):
    """Wrap every track's `get_stream` so the test can count API requests and
    make a chosen one fail."""
    for track in tracks:
        original = track.get_stream

        def _stream(track=track, original=original):
            calls.append(track.id)
            if fail is not None:
                fail(track.id)
            return original()

        track.get_stream = _stream


def _bulk(p, tier="HIGH", timeout=30):
    p._start_bulk_download_job(tier)
    assert _wait_for(
        lambda: (p._download_job or {}).get("state") not in (None, "running"),
        timeout), "the bulk download never finished"
    return p._download_job


# ── what [d] means where it was pressed ──


class TestPlayAllDownloadsTheWholeThing:
    """The bug: `_target_track_for_picker` fell through to `_current_track`,
    so pressing `d` on the "Play All" row opened a box about whatever happened
    to be playing — a different song entirely."""

    def _browsing(self):
        p = _player()
        p._mode = p.MODE_BROWSE
        p._browse_tracks = [_track(1), _track(2), _track(3)]
        p._browse_title = "Random Access Memories"
        p._browse_cursor = -1          # the Play All row
        p._current_track = _track(99)  # something else is playing
        return p

    def test_d_on_play_all_is_about_the_list_not_the_playing_track(self):
        p = self._browsing()
        p._handle_key("d")
        assert p._download_open is True
        assert [t.id for t in p._download_tracks] == [1, 2, 3]
        assert 99 not in [t.id for t in p._download_tracks], \
            "it opened a box about an unrelated song"
        text = _box_text(p)
        assert "Random Access Memories" in text
        assert "3 tracks" in text

    def test_a_row_is_still_one_track(self):
        p = self._browsing()
        p._browse_cursor = 1
        p._handle_key("d")
        assert [t.id for t in p._download_tracks] == [2]
        text = _box_text(p)
        assert "tracks" not in text, "a single track was counted"
        assert "Get Lucky" in text

    def test_shift_d_takes_the_whole_list_from_any_row(self):
        p = self._browsing()
        p._browse_cursor = 1
        p._handle_key("D")
        assert [t.id for t in p._download_tracks] == [1, 2, 3]

    def test_the_queue_has_no_play_all_row_so_shift_d_is_the_way(self):
        p = _player()
        p._mode = p.MODE_QUEUE
        p._queue = [_track(1), _track(2), _track(3), _track(4)]
        p._queue_cursor = 2
        p._handle_key("d")
        assert [t.id for t in p._download_tracks] == [3]
        p._handle_key(player_mod.KEY_ESC)
        p._handle_key("D")
        assert [t.id for t in p._download_tracks] == [1, 2, 3, 4]
        assert "Queue" in _box_text(p)

    def test_an_artist_section_with_no_track_under_the_cursor(self):
        """The Albums tab holds albums, so there is no one track to mean and
        the old fallback opened a box about the playing song."""
        p = _player()
        p._mode = p.MODE_ARTIST
        p._artist = types.SimpleNamespace(id=5, name="Daft Punk")
        p._artist_section = "tracks"
        p._artist_sections = {("5", "tracks"): {
            "state": "ready",
            "items": [{"type": "album", "obj": types.SimpleNamespace(id=9)},
                      {"type": "track", "obj": _track(1)},
                      {"type": "track", "obj": _track(2)}]}}
        p._artist_cursor = 0           # the album row
        p._current_track = _track(99)
        p._handle_key("d")
        assert [t.id for t in p._download_tracks] == [1, 2]
        assert "Daft Punk" in _box_text(p)

    def test_a_track_row_on_the_artist_page_is_still_one_track(self):
        p = _player()
        p._mode = p.MODE_ARTIST
        p._artist = types.SimpleNamespace(id=5, name="Daft Punk")
        p._artist_sections = {("5", "tracks"): {
            "state": "ready", "items": [{"type": "track", "obj": _track(7)}]}}
        p._artist_cursor = 0
        p._handle_key("d")
        assert [t.id for t in p._download_tracks] == [7]

    def test_the_single_track_box_is_exactly_as_it_was(self):
        """Same three lines and the same button: this is one box with a
        different first line and a count, not a second box."""
        p = _player()
        p._current_track = _track()
        p._handle_key("d")
        lines = _box_text(p).splitlines()
        assert "Get Lucky" in lines[1] and "Daft Punk" in lines[1]
        estimate = downloads.estimate_bytes(_track().duration, "HIGH")
        assert f"~{downloads.format_bytes(estimate)}" in lines[2]
        assert "HIGH" in lines[3]
        assert "[Enter] download" in lines[4]


class TestTheBulkEstimateCostsNothing:
    """Summing durations is arithmetic. A real per-track size is one
    `playbackinfo` each, which is ai/INCIDENTS #1 exactly."""

    def test_the_total_is_the_sum_and_no_request_is_made(self, monkeypatch):
        patch_get(monkeypatch, player_mod,
                  lambda *a, **k: pytest.fail("the estimate made a request"))
        p = _player()
        p._mode = p.MODE_BROWSE
        p._browse_tracks = [_track(1, duration=100), _track(2, duration=200),
                            _track(3, duration=300)]
        p._browse_cursor = -1
        p.session.track = lambda tid: pytest.fail("the estimate resolved a track")
        p._handle_key("d")
        for _ in range(4):
            p._handle_key(player_mod.KEY_DOWN)
            p._handle_key(player_mod.KEY_UP)
        expected = sum(downloads.estimate_bytes(d, "HIGH") for d in (100, 200, 300))
        assert p._download_bulk_estimate("HIGH") == expected
        assert downloads.format_bytes(expected) in _box_text(p)

    def test_the_arrows_re_total_it(self):
        p = _player()
        p._download_tracks = [_track(1, duration=100), _track(2, duration=200)]
        p._download_track = p._download_tracks[0]
        p._download_label = "Two"
        p._download_cursor = QUALITY_CHOICES.index("MEDIUM")
        medium = _box_text(p)
        p._download_cursor = QUALITY_CHOICES.index("HIGH")
        high = _box_text(p)
        assert medium != high, "the number did not follow the tier"
        assert downloads.format_bytes(
            p._download_bulk_estimate("HIGH")) in high

    def test_every_size_still_carries_the_estimate_mark(self):
        p = _player()
        p._download_tracks = [_track(1, duration=100), _track(2, duration=200)]
        p._download_track = p._download_tracks[0]
        p._download_label = "Two"
        assert "~" in _box_text(p)


# ── the API half ──


class TestTheApiStaysSerialAndPaced:
    """The parallelism goes only on the CDN fetch. The peak API request rate
    of a three-wide run has to be identical to a one-wide run's."""

    def test_twelve_tracks_cost_twelve_streams_and_never_two_at_once(
            self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.02)
        p, tracks, server, _payloads = _bulk_library(12)
        try:
            inflight, peak, calls = [], [], []
            for track in tracks:
                original = track.get_stream

                def _stream(track=track, original=original):
                    calls.append(track.id)
                    inflight.append(1)
                    peak.append(len(inflight))
                    time.sleep(0.01)
                    try:
                        return original()
                    finally:
                        inflight.pop()

                track.get_stream = _stream

            job = _bulk(p)
            assert job["state"] == "done", job.get("error")
            assert job["done"] == 12 and job["failed"] == 0
            assert len(calls) == 12, f"{len(calls)} stream requests for 12 tracks"
            assert sorted(calls) == list(range(1, 13)), "a track was resolved twice"
            assert max(peak) == 1, f"{max(peak)} API requests were in flight"
        finally:
            server.close()

    def test_the_starts_are_paced(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.3)
        p, tracks, server, _payloads = _bulk_library(4)
        try:
            starts = []
            for track in tracks:
                original = track.get_stream

                def _stream(original=original):
                    starts.append(time.monotonic())
                    return original()

                track.get_stream = _stream
            assert _bulk(p)["state"] == "done"
            gaps = [b - a for a, b in zip(starts, starts[1:])]
            assert all(g >= 0.28 for g in gaps), f"paced too fast: {gaps}"
        finally:
            server.close()

    def test_hammering_the_key_does_not_fan_out(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.05)
        p, tracks, server, _payloads = _bulk_library(6)
        try:
            calls = []
            _counting(tracks, calls)
            p._download_open = True
            for _ in range(40):
                p._handle_download_key("d")
                p._handle_download_key(player_mod.KEY_ENTER)
            assert _wait_for(
                lambda: (p._download_job or {}).get("state") == "done", 30)
            assert len(calls) == 6, \
                f"40 keypresses became {len(calls)} requests for 6 tracks"
        finally:
            server.close()

    def test_the_engine_is_the_one_the_refetch_uses(self):
        """Two copies of the pacing is how one of them quietly stops
        honouring it, so there is one runner and both jobs name it."""
        source = inspect.getsource(HeadlessTidalPlayer)
        assert source.count("_PacedRun(") == 2
        assert "workers=1" in inspect.getsource(
            HeadlessTidalPlayer._start_refetch_job)
        assert "workers=DOWNLOAD_WORKERS" in inspect.getsource(
            HeadlessTidalPlayer._start_bulk_download_job)


class TestABlockStopsTheWholeRun:
    def test_a_429_on_the_third_track_stops_it_and_retries_nothing(
            self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.02)
        p, tracks, server, _payloads = _bulk_library(10)
        try:
            calls = []

            def _fail(tid):
                if tid == 3:
                    raise RuntimeError("429 Too Many Requests")

            _counting(tracks, calls, fail=_fail)
            job = _bulk(p)
            assert job["state"] == "blocked"
            # Three asked for, the third refused, and **nothing after it**.
            # The count is the assertion: a flag can be set by a run that
            # kept going.
            assert calls == [1, 2, 3], calls
            assert "rate-limiting" in p._toast
        finally:
            server.close()

    def test_the_tier_ladder_is_not_walked_past_a_rate_limit(self, monkeypatch):
        """Stepping down a tier is three more requests into a limiter that is
        already saying no."""
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.02)
        p, tracks, server, _payloads = _bulk_library(4)
        try:
            calls = []
            _counting(tracks, calls, fail=lambda tid: (_ for _ in ()).throw(
                RuntimeError("401 subStatus 4006 Session does not have"
                             " streaming privileges")))
            assert _bulk(p, tier="MAX")["state"] == "blocked"
            assert calls == [1], calls
        finally:
            server.close()

    def test_the_box_says_it_stopped_and_that_nothing_was_retried(self):
        p = _player()
        p._download_tracks = [_track(1), _track(2)]
        p._download_track = p._download_tracks[0]
        p._download_label = "Two"
        p._download_job = {"state": "blocked", "bulk": True, "done": 1,
                           "failed": 0, "tracks": 2, "error": "429",
                           "slots": p._new_download_slots(), "tier": "HIGH"}
        text = _box_text(p)
        assert "Stopped — 429" in text
        assert "nothing retried" in text

    def test_an_ordinary_failure_does_not_stop_the_rest(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.02)
        p, tracks, server, _payloads = _bulk_library(4)
        try:
            tracks[1].get_stream = lambda: (_ for _ in ()).throw(
                RuntimeError("404 Not Found"))
            job = _bulk(p)
            assert job["state"] == "done"
            assert job["done"] == 3 and job["failed"] == 1
        finally:
            server.close()


# ── the fetch half ──


class TestThreeAtATime:
    def test_the_peak_number_of_fetches_in_flight_is_three(self, monkeypatch):
        """Not one (that is the serial run, and the feature would do nothing)
        and not twelve (that is the fan-out the rate limit exists to prevent).
        Instrumented where the player actually calls `fetch_to_file`."""
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.02)
        p, _tracks, server, _payloads = _bulk_library(12)
        try:
            real = player_mod.fetch_to_file
            inflight, peak = [], []

            def _slow(*args, **kwargs):
                inflight.append(1)
                peak.append(len(inflight))
                try:
                    time.sleep(0.2)
                    return real(*args, **kwargs)
                finally:
                    inflight.pop()

            monkeypatch.setattr(player_mod, "fetch_to_file", _slow)
            assert _bulk(p)["state"] == "done"
            assert len(peak) == 12
            assert max(peak) == player_mod.DOWNLOAD_WORKERS == 3, \
                f"peak concurrency was {max(peak)}"
        finally:
            server.close()

    def test_a_resolved_track_waits_for_a_free_worker(self, monkeypatch):
        """The resolver must not run ahead: a signed stream URL expires in
        about an hour, and holding five hundred of them is how that becomes a
        bug nobody can reproduce."""
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.0)
        p, tracks, server, _payloads = _bulk_library(9)
        try:
            real = player_mod.fetch_to_file
            resolved, finished = [], []

            def _slow(*args, **kwargs):
                try:
                    time.sleep(0.15)
                    return real(*args, **kwargs)
                finally:
                    finished.append(1)

            monkeypatch.setattr(player_mod, "fetch_to_file", _slow)
            for track in tracks:
                original = track.get_stream

                def _stream(original=original):
                    resolved.append(len(finished))
                    return original()

                track.get_stream = _stream
            assert _bulk(p)["state"] == "done"
            # Track N+3 cannot have been resolved before track N landed
            for index, done_when_resolved in enumerate(resolved):
                assert done_when_resolved >= index - 2, \
                    f"track {index} resolved with only {done_when_resolved} done"
        finally:
            server.close()

    def test_three_is_not_a_setting(self):
        """"No parallelism to tune" is the property ai/WORKING-RULES protects,
        and it stays true where it matters: the number is a constant and
        nothing in the config can move it."""
        from ticli.utils.config import SETTINGS_SPEC

        assert player_mod.DOWNLOAD_WORKERS == 3
        described = " ".join(str(spec) for spec in SETTINGS_SPEC).lower()
        for word in ("worker", "parallel", "concurren", "at once"):
            assert word not in described
        # And nothing reads it back out of the config either
        assert 'get("workers"' not in inspect.getsource(player_mod)
        assert inspect.getsource(
            HeadlessTidalPlayer._start_bulk_download_job).count(
                "DOWNLOAD_WORKERS") == 1


class TestTheBytesOfABulkRun:
    def test_every_track_lands_with_the_bytes_the_server_sent(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.01)
        p, _tracks, server, payloads = _bulk_library(7)
        try:
            job = _bulk(p)
            assert job["state"] == "done" and job["done"] == 7
            root = downloads.download_dir()
            for tid, payload in payloads.items():
                path = root / "Daft Punk" / "Random Access Memories" / \
                    f"{tid:02d} Track {tid}.m4a"
                assert path.is_file(), f"track {tid} is not where the box said"
                # Tagging rewrites the container, so the audio is what has to
                # survive byte for byte
                audio = payload[payload.index(b"mdat") + 4:]
                written = path.read_bytes()
                assert written[written.index(b"mdat") + 4:][:len(audio)] == audio
        finally:
            server.close()

    def test_the_index_records_every_one_of_them(self, monkeypatch):
        """Three workers, one JSON file. A read-modify-write from each of them
        loses rows, so the workers hand their record back and the runner's own
        thread is the only writer."""
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.0)
        p, _tracks, server, _payloads = _bulk_library(9)
        try:
            assert _bulk(p)["done"] == 9
            index = downloads.load_index()
            assert sorted(int(k) for k in index) == list(range(1, 10)), index
            assert downloads.downloaded_count() == 9
        finally:
            server.close()

    def test_a_cancel_mid_run_leaves_no_part_file(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.02)
        p, _tracks, server, _payloads = _bulk_library(10)
        try:
            real = player_mod.fetch_to_file

            def _slow(*args, **kwargs):
                time.sleep(0.15)
                return real(*args, **kwargs)

            monkeypatch.setattr(player_mod, "fetch_to_file", _slow)
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: (p._download_job or {}).get("done", 0) >= 2, 20)
            p._cancel_download()
            time.sleep(0.8)

            leftovers = [f for f in downloads.download_dir().rglob("*")
                         if f.is_file()
                         and (f.name.endswith(downloads.PART_SUFFIX)
                              or f.name.startswith(".ticli-"))]
            assert leftovers == [], leftovers
            assert p._download_job["state"] == "cancelled"
        finally:
            server.close()

    def test_a_cancelled_run_stops_asking_tidal_for_anything(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.05)
        p, tracks, server, _payloads = _bulk_library(12)
        try:
            calls = []
            _counting(tracks, calls)
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: len(calls) >= 2, 10)
            p._cancel_download()
            stopped_at = len(calls)
            time.sleep(0.5)
            assert len(calls) <= stopped_at + 1, "it kept resolving after cancel"
        finally:
            server.close()

    def test_a_whole_run_still_only_writes_under_the_music_folder(
            self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.0)
        p, _tracks, server, _payloads = _bulk_library(5)
        try:
            stranger = downloads.download_dir()
            stranger.mkdir(parents=True, exist_ok=True)
            (stranger / "important.txt").write_bytes(b"not ours")
            assert _bulk(p)["done"] == 5
            assert (stranger / "important.txt").read_bytes() == b"not ours"
        finally:
            server.close()


# ── the rate number ──


class TestTheRateIsAThreeSampleMean:
    """Sample on the monitor's existing 0.5 s tick, keep three, show the mean.
    No timer and no thread — the tick already exists, and "no new polling
    loops or timers" is a hard rule here."""

    def _running(self):
        p = _player()
        slots = p._new_download_slots()
        p._download_job = {"state": "running", "bulk": True, "tier": "HIGH",
                           "track_id": None, "tracks": 4, "done": 0,
                           "failed": 0, "slots": slots, "error": ""}
        slots[0].update(title="Nightcall", state="running", total=10_000_000)
        return p, slots[0]

    def _tick(self, p, slot, done, at):
        """One monitor tick, with the clock stated rather than measured."""
        slot["done"] = done
        with mock.patch.object(player_mod.time, "monotonic", lambda: at):
            p._sample_download_rates()

    def test_nothing_is_shown_before_the_first_window_closes(self):
        p, slot = self._running()
        self._tick(p, slot, 0, 100.0)
        assert slot["samples"] == ()
        assert p._slot_rate(slot) == 0.0
        assert player_mod._format_rate(p._slot_rate(slot)) == ""
        assert "MB/s" not in p._download_bulk_status_row(p._download_job, 70).plain

    def test_the_number_is_the_mean_of_the_last_three_samples(self):
        p, slot = self._running()
        self._tick(p, slot, 0, 100.0)
        for index, done in enumerate((1_000_000, 3_000_000, 6_000_000), start=1):
            self._tick(p, slot, done, 100.0 + index * 0.5)
        # 2 MB/s, 4 MB/s then 6 MB/s, each over half a second
        assert [round(s) for s in slot["samples"]] == \
            [2_000_000, 4_000_000, 6_000_000]
        assert round(p._slot_rate(slot)) == 4_000_000

    def test_only_three_samples_are_kept(self):
        p, slot = self._running()
        self._tick(p, slot, 0, 100.0)
        for index in range(1, 9):
            self._tick(p, slot, index * 1_000_000, 100.0 + index * 0.5)
        assert len(slot["samples"]) == player_mod.RATE_SAMPLES == 3
        # A steady 2 MB/s, whatever the window held before it
        assert round(p._slot_rate(slot)) == 2_000_000

    def test_a_stall_shows_up_inside_the_window(self):
        p, slot = self._running()
        self._tick(p, slot, 0, 100.0)
        for index in range(1, 4):
            self._tick(p, slot, index * 1_000_000, 100.0 + index * 0.5)
        assert round(p._slot_rate(slot)) == 2_000_000
        for index in range(4, 7):
            self._tick(p, slot, 3_000_000, 100.0 + index * 0.5)
        assert p._slot_rate(slot) == 0.0

    def test_it_is_formatted_to_one_decimal(self):
        assert player_mod._format_rate(5.2 * 1024 * 1024) == "5.2MB/s"
        assert player_mod._format_rate(1536) == "1.5KB/s"
        assert player_mod._format_rate(0) == ""

    def test_a_finished_slot_starts_its_window_again(self):
        p, slot = self._running()
        self._tick(p, slot, 0, 100.0)
        self._tick(p, slot, 5_000_000, 100.5)
        assert slot["samples"]
        slot.update(state="done", done=0)
        self._tick(p, slot, 0, 101.0)
        assert slot["mark"] is None, "a finished slot kept measuring"

    def test_the_combined_rate_is_the_sum_of_the_slots(self):
        p, _slot = self._running()
        slots = p._download_job["slots"]
        for index, rate in enumerate((2 * 1024 * 1024, 3 * 1024 * 1024,
                                      1 * 1024 * 1024)):
            slots[index].update(state="running", samples=(rate, rate, rate))
        assert "6.0MB/s" in p._download_bulk_status_row(p._download_job, 74).plain

    def test_the_monitor_tick_is_where_it_happens(self):
        source = inspect.getsource(HeadlessTidalPlayer._monitor_playback)
        assert "_sample_download_rates()" in source
        assert source.count("time.sleep(0.5)") == 1, \
            "the monitor grew a second sleep"
        # And nothing anywhere started a thread or a timer to do it
        rates = inspect.getsource(HeadlessTidalPlayer._sample_download_rates)
        for word in ("Thread", "Timer", "sleep"):
            assert word not in rates


# ── the bars on the screen ──


class TestTheBarsOnScreen:
    def _running(self):
        p = _player()
        p._download_tracks = [_track(i) for i in range(1, 7)]
        p._download_track = p._download_tracks[0]
        p._download_label = "OutRun"
        p._download_open = True
        slots = p._new_download_slots()
        p._download_job = {"state": "running", "bulk": True, "tier": "HIGH",
                           "track_id": None, "tracks": 6, "done": 1,
                           "failed": 0, "slots": slots, "error": ""}
        return p, slots

    def test_the_bar_steps_by_half_cells(self):
        """`╸` is the left half of `━`, which is what makes the bar move every
        frame instead of once a character (ai/bar-styles-demo.py)."""
        assert player_mod._bar_split(0.0, 10) == ("", "┈" * 10)
        assert player_mod._bar_split(0.05, 10) == ("╸", "┈" * 9)
        assert player_mod._bar_split(0.10, 10) == ("━", "┈" * 9)
        assert player_mod._bar_split(0.15, 10) == ("━╸", "┈" * 8)
        assert player_mod._bar_split(1.0, 10) == ("━" * 10, "")
        for step in range(0, 41):
            filled, rest = player_mod._bar_split(step / 40, 10)
            assert len(filled) + len(rest) == 10, step

    def test_the_rendered_bar_advances_a_half_cell_at_a_time(self):
        """Not the helper — the row the box actually draws."""
        p, slots = self._running()
        slots[0].update(title="Nightcall", state="running", total=48_000)
        halves = []
        for done in range(0, 48_001, 1_000):
            slots[0]["done"] = done
            row = p._download_bar_rows(p._download_job, 70)[0].plain
            bar = "".join(ch for ch in row if ch in "━╸┈")
            assert len(bar) in (24, 25), bar   # a half cell adds one glyph
            halves.append(2 * bar.count("━") + bar.count("╸"))
        # 24 cells is 48 half-cell positions, and every one of the 49 samples
        # is a different picture — the bar never sticks for two frames
        assert halves == list(range(49))

    def test_a_finished_row_collapses_to_a_tick_and_the_real_size(self):
        p, slots = self._running()
        slots[0].update(title="A Real Hero", state="done", size=19_600_000,
                        banked=19_600_000)
        row = p._download_bar_rows(p._download_job, 70)[0].plain
        assert row.startswith("✓ A Real Hero")
        assert "18.7 MB" in row
        assert "~" not in row, "a counted size must not carry the estimate mark"
        assert "MB/s" not in row, "a finished row has no rate"

    def test_an_idle_slot_draws_nothing(self):
        p, slots = self._running()
        slots[0].update(title="Nightcall", state="running", total=100)
        assert len(p._download_bar_rows(p._download_job, 70)) == 1

    def test_the_bottom_line_says_the_totals_and_the_key(self):
        p, slots = self._running()
        slots[0].update(state="running", done=1_000_000, banked=4_000_000)
        slots[1].update(state="running", done=2_000_000)
        line = p._download_bulk_status_row(p._download_job, 70).plain
        assert "6.7 MB fetched" in line
        assert "5 to go" in line
        assert "[x] cancel" in line

    def test_the_columns_line_up(self):
        p, slots = self._running()
        slots[0].update(title="Nightcall", state="running", done=1, total=2)
        slots[1].update(title="A Real Hero", state="done", size=1000)
        rows = p._download_bar_rows(p._download_job, 70)
        assert len({cell_len(r.plain) for r in rows}) == 1, \
            "a finished row is a different width from a running one"

    def test_the_box_fits_eighty_by_twentyfour_and_stays_centred(self):
        """Driven through the real `Live` into `vt.Screen`, because the
        question is what is on the terminal, not what a builder returned."""
        from ticli.tests.test_display import Harness

        p, slots = self._running()
        for index, title in enumerate(("Nightcall", "Under Your Spell",
                                       "A Real Hero")):
            slots[index].update(title=title, state="running",
                                done=(index + 1) * 3_000_000, total=20_000_000,
                                samples=(4.5 * 1024 * 1024,))
        h = Harness(width=80, height=24, artwork=None)
        try:
            h.player._show_artwork = False
            for key, value in p.__dict__.items():
                if key.startswith("_download"):
                    setattr(h.player, key, value)
            h.player._mode = h.player.MODE_BROWSE
            h.player._browse_tracks = p._download_tracks
            h.player._browse_title = "OutRun"
            h.start()
            h.repaint()

            rows = h.screen.text()
            assert len(rows) == 24
            for row in rows:
                assert cell_len(row) <= 80, repr(row)
            # Two top borders and two bottom ones: the panel, and the box
            # inside it. Nothing above, below or in the scrollback — the
            # frame is whole and it is the only thing on the terminal.
            assert [i for i, r in enumerate(rows) if "╭" in r] == [0, 5]
            assert len([r for r in rows if "╰" in r]) == 2
            assert rows[0].startswith("╭")
            assert [r for r in rows if r][-1].startswith("╰")
            assert h.stranded() == []

            body = "\n".join(rows)
            assert "▸ Nightcall" in body
            assert "▸ Under Your Spell" in body
            assert "[x] cancel" in body
            assert "…" not in body, "the box was clipped rather than fitting"

            # Centred inside the panel: the gap left of the box's own top
            # border and the gap right of it differ by at most one column
            top = [r for r in rows if "╭─" in r and "Ticli" not in r]
            assert len(top) == 1, top
            stripped = top[0].strip()
            left = top[0].index("╭") - top[0].index("│")
            right = 79 - top[0].rindex("│") + (79 - top[0].rindex("╮") - 1)
            assert stripped.startswith("│") and stripped.endswith("│")
            assert abs(left - right) <= 2, (left, right, top[0])
        finally:
            if h.live is not None:
                h.live.stop()

    def test_a_short_window_drops_bars_before_it_overflows(self):
        p, slots = self._running()
        for index, title in enumerate(("One", "Two", "Three")):
            slots[index].update(title=title, state="running", done=1000,
                                total=2000)
        tall = player_mod._Fit(inner=74, rows=20, page_rows=10, hint_rows=2)
        assert len(p._build_download_overlay(tall)) == 9, "three bars and a box"
        for rows in range(9, 4, -1):
            fit = player_mod._Fit(inner=74, rows=rows, page_rows=10,
                                  hint_rows=1)
            lines = p._build_download_overlay(fit)
            assert len(lines) <= rows, (rows, len(lines))
            # Whatever else goes, the count and the way out do not
            text = "\n".join(line.plain for line in lines)
            assert "[x] cancel" in text
            assert "to go" in text

    def test_a_narrow_box_shortens_the_summary_rather_than_clipping_it(self):
        p, slots = self._running()
        slots[0].update(state="running", done=1_000_000, samples=(1024 * 1024,))
        for avail in range(24, 80):
            line = p._download_bulk_status_row(p._download_job, avail)
            assert "[x] cancel" in line.plain
            assert cell_len(line.plain.rstrip()) <= max(avail, 22), avail

    def test_the_bars_read_no_disk_and_no_index(self, monkeypatch):
        """A repaint is twice a second; `load_index()` re-parses the whole
        file (ai/reference F6). Every number on a bar is on the slot."""
        p, slots = self._running()
        slots[0].update(title="Nightcall", state="running", done=10, total=20)
        monkeypatch.setattr(downloads, "load_index",
                            lambda: pytest.fail("a repaint read the index"))
        monkeypatch.setattr(downloads, "usage",
                            lambda: pytest.fail("a repaint walked the disk"))
        for _ in range(5):
            _box_text(p)


# ── ai/INCIDENTS #5, with three writers instead of two ──


class TestEvictionAtThreeWide:
    """`unlink(missing_ok=True)` fixed the underlying bug — a sweep that read
    "already gone" as "still costing us" and evicted one file too many. This
    pins that it still holds when three sweeps really do run at once, rather
    than assuming it."""

    def _library(self, monkeypatch, count, size, budget_gb):
        monkeypatch.setattr(cache_mod, "BYTES_PER_GB", 1000)
        cache = MetadataCache(songs=True)
        cache.budget_gb = budget_gb
        directory = cache_mod.audio_dir()
        directory.mkdir(parents=True, exist_ok=True)
        for tid in range(1, count + 1):
            (directory / f"{tid}.m4a").write_bytes(b"x" * size)
            cache.note_cached(tid, ".m4a", size, quality="HIGH")
        return cache, directory

    def _sweep(self, cache, times=3):
        errors = []

        def _run():
            try:
                cache.enforce_budget()
            except Exception as e:      # a sweep must never raise
                errors.append(e)

        threads = [threading.Thread(target=_run) for _ in range(times)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return errors

    def test_three_concurrent_sweeps_evict_exactly_what_one_would(
            self, monkeypatch):
        cache, directory = self._library(monkeypatch, count=5, size=300,
                                         budget_gb=1)
        assert self._sweep(cache) == []
        left = sorted(f.name for f in directory.glob("*.m4a"))
        # 1500 bytes against a 1000-byte budget: exactly two have to go, and
        # a race must not take a third
        assert len(left) == 3, left
        assert cache.total_bytes() == 900

    def test_the_budget_is_left_full_not_emptied(self, monkeypatch):
        """The failure this is written against is *over*-eviction, so the
        assertion is a floor as well as a ceiling."""
        cache, _directory = self._library(monkeypatch, count=10, size=250,
                                          budget_gb=2)
        assert self._sweep(cache) == []
        total = cache.total_bytes()
        assert total <= 2000
        assert total == 2000, f"{total} bytes left: a sweep evicted too much"

    def test_a_file_already_gone_is_not_paid_for_twice(self, monkeypatch):
        """The deterministic half of the same bug, kept beside the threaded
        one: the loser of a race must count the byte it did not free."""
        cache, directory = self._library(monkeypatch, count=4, size=300,
                                         budget_gb=1)
        (directory / "1.m4a").unlink()      # the other sweep got there first
        cache.enforce_budget()
        assert cache.total_bytes() == 900
