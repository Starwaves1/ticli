"""Asking for a second playlist while the first is still downloading.

The owner's report was that it was refused. It is now appended to the run
that is already going — deliberately *not* a queue of runs, because a second
run would need a scheduler and would make "is the pacing floor still honoured
between one batch and the next" a question nobody had answered. Appending
keeps one serial paced resolver and one pool of three fetchers, so it is the
same question it already was, and the tests here are written to prove that
rather than to prove a list got longer:

* request **counts** across the boundary, and never two resolves in flight
* a track that is in both playlists reaches the CDN **once** — asserted on the
  server's own hit log, not on a set
* the pacing floor holds between the last track of batch one and the first of
  batch two

The settings-page line is here too, including the state it exists for: a
download that failed used to be silent the moment the box was shut.
"""

import time
import types

import pytest
from rich.cells import cell_len
from rich.console import Console

from ticli import player as player_mod
from ticli.tests.test_bulk_downloads import _bulk, _bulk_library, _counting
from ticli.tests.test_downloads import (
    _box_text, _player, _track, _wait_for, isolated)  # noqa: F401
from ticli.utils import cache as cache_mod
from ticli.utils import downloads


def _pending(p):
    """The ids the live run has ever accepted."""
    return sorted(i for i in p._download_queued_ids())


def _finished(p, timeout=30):
    assert _wait_for(
        lambda: (p._download_job or {}).get("state") not in (None, "running")
        and p._download_run is None, timeout), "the run never finished"
    return p._download_job


# ── a second ask joins the run ──


class TestASecondPlaylistIsQueued:
    def test_it_is_not_refused(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.05)
        p, tracks, server, _payloads = _bulk_library(8)
        try:
            real = player_mod.fetch_to_file
            monkeypatch.setattr(
                player_mod, "fetch_to_file",
                lambda *a, **k: (time.sleep(0.1), real(*a, **k))[1])
            p._download_tracks = tracks[:4]
            p._download_label = "First"
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: p._download_run is not None, 5)

            p._download_tracks = tracks[4:]
            p._download_label = "Second"
            p._start_bulk_download_job("HIGH")
            assert "Queued 4 more" in p._toast, p._toast

            job = _finished(p)
            assert job["state"] == "done"
            assert job["done"] == 8, "the second playlist was not downloaded"
            assert job["tracks"] == 8
            assert sorted(int(k) for k in downloads.load_index()) == \
                list(range(1, 9))
        finally:
            server.close()

    def test_the_request_count_is_the_union_and_no_more(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.05)
        p, tracks, server, _payloads = _bulk_library(9)
        try:
            calls = []
            _counting(tracks, calls)
            real = player_mod.fetch_to_file
            monkeypatch.setattr(
                player_mod, "fetch_to_file",
                lambda *a, **k: (time.sleep(0.1), real(*a, **k))[1])
            p._download_tracks = tracks[:5]
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: p._download_run is not None, 5)
            p._download_tracks = tracks[5:]
            p._start_bulk_download_job("HIGH")

            _finished(p)
            assert sorted(calls) == list(range(1, 10)), calls
            assert len(calls) == 9, f"{len(calls)} requests for 9 tracks"
        finally:
            server.close()

    def test_the_resolver_is_still_one_at_a_time_across_the_boundary(
            self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.02)
        p, tracks, server, _payloads = _bulk_library(10)
        try:
            inflight, peak = [], []
            for track in tracks:
                original = track.get_stream

                def _stream(original=original):
                    inflight.append(1)
                    peak.append(len(inflight))
                    time.sleep(0.01)
                    try:
                        return original()
                    finally:
                        inflight.pop()

                track.get_stream = _stream
            real = player_mod.fetch_to_file
            monkeypatch.setattr(
                player_mod, "fetch_to_file",
                lambda *a, **k: (time.sleep(0.08), real(*a, **k))[1])

            p._download_tracks = tracks[:5]
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: p._download_run is not None, 5)
            p._download_tracks = tracks[5:]
            p._start_bulk_download_job("HIGH")

            _finished(p)
            assert len(peak) == 10
            assert max(peak) == 1, f"{max(peak)} API requests were in flight"
        finally:
            server.close()

    def test_the_pacing_floor_holds_between_the_two_batches(self, monkeypatch):
        """The reason there is no second run: the floor is between *starts*,
        and a batch boundary is not special to a loop that never left."""
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
            p._download_tracks = tracks[:2]
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: len(starts) >= 2, 5)
            p._download_tracks = tracks[2:]
            p._start_bulk_download_job("HIGH")

            _finished(p)
            gaps = [b - a for a, b in zip(starts, starts[1:])]
            assert len(gaps) == 3
            assert all(g >= 0.28 for g in gaps), f"paced too fast: {gaps}"
        finally:
            server.close()

    def test_a_run_that_had_already_drained_goes_round_again(self, monkeypatch):
        """The window this closes was the whole of the join: with two tracks
        in flight and a third asked for a moment later, the run had passed
        the end of its list and the third was silently never downloaded."""
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.0)
        p, tracks, server, _payloads = _bulk_library(3)
        try:
            real = player_mod.fetch_to_file
            gate = []

            def _slow(*args, **kwargs):
                # Long enough that the append lands while the resolver is
                # sitting in its final join with nothing left to resolve
                gate.append(1)
                time.sleep(0.25)
                return real(*args, **kwargs)

            monkeypatch.setattr(player_mod, "fetch_to_file", _slow)
            p._download_tracks = tracks[:2]
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: len(gate) >= 2, 5)
            time.sleep(0.05)          # the resolver is now in its join
            p._download_tracks = tracks[2:]
            p._start_bulk_download_job("HIGH")

            job = _finished(p)
            assert job["done"] == 3, "the late batch was dropped"
            assert "not started" not in p._toast, p._toast
            assert sorted(int(k) for k in downloads.load_index()) == [1, 2, 3]
        finally:
            server.close()

    def test_the_floor_also_holds_between_two_separate_runs(self, monkeypatch):
        """Found while chasing a flaky pacing test, and it was the code that
        was wrong: a run that ended and another that started a millisecond
        later were two requests a millisecond apart. The clock outlives the
        run now, so the floor does not care which run a request came from."""
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
            p._download_tracks = tracks[:2]
            _bulk(p)
            assert p._download_run is None, "the first run had not ended"
            p._download_tracks = tracks[2:]
            _bulk(p)

            gaps = [b - a for a, b in zip(starts, starts[1:])]
            assert len(gaps) == 3
            assert all(g >= 0.28 for g in gaps), f"paced too fast: {gaps}"
        finally:
            server.close()

    def test_the_refetch_shares_the_same_clock(self):
        """One floor for the API, not one per feature."""
        import inspect
        for name in ("_start_bulk_download_job", "_start_refetch_job"):
            source = inspect.getsource(getattr(player_mod.HeadlessTidalPlayer,
                                               name))
            assert "clock=self._api_pace" in source, name

    def test_add_says_whether_the_run_will_take_them(self):
        """`add` publishes first and reads the flag second, so True is a
        guarantee rather than a hope."""
        run = player_mod._PacedRun(items=[1], resolve=lambda i: i,
                                   fetch=lambda i, h, s: None,
                                   alive=lambda: True, report=lambda **k: None)
        assert run.add([2]) is True
        assert run.items == [1, 2]
        run.closed = True
        assert run.add([3]) is False
        assert run.pending() == [1, 2, 3], \
            "an item the run refused must still be visible to the caller"

    def test_a_run_that_has_finished_is_not_appended_to(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.0)
        p, tracks, server, _payloads = _bulk_library(4)
        try:
            p._download_tracks = tracks[:2]
            assert _bulk(p)["done"] == 2
            assert p._download_run is None
            p._download_tracks = tracks[2:]
            job = _bulk(p)
            assert job["done"] == 2 and job["tracks"] == 2, \
                "a finished run was appended to instead of a new one starting"
        finally:
            server.close()


class TestNothingIsDownloadedTwice:
    def test_an_overlapping_playlist_fetches_each_track_once(self, monkeypatch):
        """Asserted on the server's hit log — a set of ids would pass even if
        the CDN had been asked twice."""
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.05)
        p, tracks, server, _payloads = _bulk_library(6)
        try:
            calls = []
            _counting(tracks, calls)
            real = player_mod.fetch_to_file
            monkeypatch.setattr(
                player_mod, "fetch_to_file",
                lambda *a, **k: (time.sleep(0.1), real(*a, **k))[1])
            p._download_tracks = tracks[:4]          # 1 2 3 4
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: p._download_run is not None, 5)
            p._download_tracks = tracks[2:]          # 3 4 5 6 — two overlap
            p._start_bulk_download_job("HIGH")
            assert "Queued 2 more" in p._toast
            assert "2 already queued" in p._toast

            job = _finished(p)
            assert job["tracks"] == 6 and job["done"] == 6
            assert sorted(calls) == [1, 2, 3, 4, 5, 6], calls
            assert sorted(server.hits) == [f"/t{i}.mp4" for i in range(1, 7)], \
                "a track was fetched from the CDN twice"
        finally:
            server.close()

    def test_a_track_already_finished_is_not_queued_again(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.05)
        p, tracks, server, _payloads = _bulk_library(6)
        try:
            real = player_mod.fetch_to_file
            monkeypatch.setattr(
                player_mod, "fetch_to_file",
                lambda *a, **k: (time.sleep(0.12), real(*a, **k))[1])
            p._download_tracks = tracks[:3]
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: (p._download_job or {}).get("done", 0) >= 1, 10)
            # Track 1 has landed; asking for it again must not re-fetch it
            p._download_tracks = [tracks[0], tracks[3]]
            p._start_bulk_download_job("HIGH")
            assert "1 already queued" in p._toast

            job = _finished(p)
            assert job["tracks"] == 4
            assert server.hits.count("/t1.mp4") == 1
        finally:
            server.close()

    def test_asking_for_the_same_playlist_twice_says_so(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.05)
        p, tracks, server, _payloads = _bulk_library(4)
        try:
            real = player_mod.fetch_to_file
            monkeypatch.setattr(
                player_mod, "fetch_to_file",
                lambda *a, **k: (time.sleep(0.12), real(*a, **k))[1])
            p._download_tracks = tracks
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: p._download_run is not None, 5)
            p._start_bulk_download_job("HIGH")
            assert "Already queued" in p._toast, p._toast
            assert p._download_job["tracks"] == 4

            _finished(p)
            assert len(server.hits) == 4
        finally:
            server.close()


class TestOneTrackJoinsTheSameWay:
    def test_d_on_a_single_track_appends_to_a_running_run(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.05)
        p, tracks, server, _payloads = _bulk_library(5)
        try:
            real = player_mod.fetch_to_file
            monkeypatch.setattr(
                player_mod, "fetch_to_file",
                lambda *a, **k: (time.sleep(0.1), real(*a, **k))[1])
            p._download_tracks = tracks[:3]
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: p._download_run is not None, 5)

            p._download_tracks = [tracks[4]]
            p._download_track = tracks[4]
            p._download_open = True
            p._handle_download_key(player_mod.KEY_ENTER)
            assert "Queued 1 more" in p._toast
            assert p._download_open is True, \
                "the box closed on the bars it had just started"

            job = _finished(p)
            assert job["tracks"] == 4 and job["done"] == 4
            assert 5 in [int(k) for k in downloads.load_index()]
        finally:
            server.close()

    def test_it_does_not_start_a_second_resolver(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.02)
        p, tracks, server, _payloads = _bulk_library(6)
        try:
            inflight, peak = [], []
            for track in tracks:
                original = track.get_stream

                def _stream(original=original):
                    inflight.append(1)
                    peak.append(len(inflight))
                    time.sleep(0.02)
                    try:
                        return original()
                    finally:
                        inflight.pop()

                track.get_stream = _stream
            real = player_mod.fetch_to_file
            monkeypatch.setattr(
                player_mod, "fetch_to_file",
                lambda *a, **k: (time.sleep(0.1), real(*a, **k))[1])
            p._download_tracks = tracks[:4]
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: p._download_run is not None, 5)
            for track in tracks[4:]:
                p._download_track = track
                p._download_tracks = [track]
                p._start_download_job("HIGH")

            _finished(p)
            assert max(peak) == 1, f"{max(peak)} API requests were in flight"
            assert len(peak) == 6
        finally:
            server.close()

    def test_a_single_job_of_its_own_still_works_when_nothing_is_running(self):
        from ticli.tests.test_downloads import _download, _mp4, _Server

        server = _Server({"/track.mp4": _mp4()})
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
            assert p._download_run is None
        finally:
            server.close()


class TestEachBatchKeepsItsOwnTier:
    def test_a_hires_ask_appended_to_a_lossless_run_is_fetched_at_hires(
            self, monkeypatch):
        """The tier is already per-track inside the plan, so a second batch
        carries its own. Quietly fetching somebody's hi-res request at the
        first batch's tier is the dishonesty this box exists to avoid."""
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.05)
        p, tracks, server, _payloads = _bulk_library(4)
        try:
            asked = {}
            for track in tracks:
                original = track.get_stream

                def _stream(track=track, original=original):
                    asked[track.id] = str(p.session.audio_quality)
                    return original()

                track.get_stream = _stream
            real = player_mod.fetch_to_file
            monkeypatch.setattr(
                player_mod, "fetch_to_file",
                lambda *a, **k: (time.sleep(0.12), real(*a, **k))[1])

            p._download_tracks = tracks[:2]
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: p._download_run is not None, 5)
            p._download_tracks = tracks[2:]
            p._start_bulk_download_job("MAX")

            _finished(p)
            assert asked[1] == "LOSSLESS" and asked[2] == "LOSSLESS"
            assert asked[3] == "HI_RES_LOSSLESS" and asked[4] == "HI_RES_LOSSLESS"
        finally:
            server.close()


class TestCancelStopsTheWholeRun:
    """`[x]` cancels everything, not just the batch under the cursor. Chosen
    over per-batch cancel because picking a batch needs a list of batches to
    pick from, and that list is the scheduler this design does not have."""

    def test_x_stops_both_batches(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.05)
        p, tracks, server, _payloads = _bulk_library(12)
        try:
            calls = []
            _counting(tracks, calls)
            real = player_mod.fetch_to_file
            monkeypatch.setattr(
                player_mod, "fetch_to_file",
                lambda *a, **k: (time.sleep(0.15), real(*a, **k))[1])
            p._download_tracks = tracks[:6]
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: p._download_run is not None, 5)
            p._download_tracks = tracks[6:]
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: len(calls) >= 2, 10)

            p._cancel_download()
            stopped_at = len(calls)
            time.sleep(0.6)
            assert len(calls) <= stopped_at + 1, "it kept resolving after [x]"
            assert p._download_job["state"] == "cancelled"
            leftovers = [f for f in downloads.download_dir().rglob("*")
                         if f.is_file()
                         and (f.name.endswith(downloads.PART_SUFFIX)
                              or f.name.startswith(".ticli-"))]
            assert leftovers == [], leftovers
        finally:
            server.close()

    def test_a_new_run_can_start_after_a_cancel(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.02)
        p, tracks, server, _payloads = _bulk_library(6)
        try:
            real = player_mod.fetch_to_file
            monkeypatch.setattr(
                player_mod, "fetch_to_file",
                lambda *a, **k: (time.sleep(0.1), real(*a, **k))[1])
            p._download_tracks = tracks[:3]
            p._start_bulk_download_job("HIGH")
            assert _wait_for(lambda: p._download_run is not None, 5)
            p._cancel_download()
            assert _wait_for(lambda: p._download_run is None, 10)

            monkeypatch.setattr(player_mod, "fetch_to_file", real)
            p._download_tracks = tracks[3:]
            job = _bulk(p)
            assert job["state"] == "done" and job["done"] == 3
        finally:
            server.close()


class TestOnlyOnePacedResolverEverRuns:
    """Two of them is two API requests a second between them — not the rate
    either was measured at, and not a rate anybody chose."""

    def test_a_refetch_will_not_start_while_a_download_is_running(self):
        p = _player(quality="MAX")
        p._download_job = {"state": "running", "bulk": True, "tracks": 3,
                           "done": 0, "failed": 0,
                           "slots": p._new_download_slots()}
        p._refetch_candidates = lambda: pytest.fail("it planned a re-fetch")
        p._start_refetch_job()
        assert p._refetch_job is None
        assert "A download is running" in p._toast

    def test_a_download_will_not_start_while_a_refetch_is_running(self):
        p = _player()
        p._refetch_job = {"state": "running", "tier": "MAX", "done": 0,
                          "total": 4, "failed": 0}
        p._download_tracks = [_track(1), _track(2)]
        p._download_track = p._download_tracks[0]
        p._start_bulk_download_job("HIGH")
        assert p._download_job is None
        assert p._download_run is None
        assert "Re-fetching" in p._toast


# ── the box while a run is going ──


class TestTheBoxDescribesTheRun:
    def _live(self, queued=(1, 2, 3), selection=(3, 4, 5)):
        p = _player()
        tracks = {i: _track(i, duration=200) for i in range(1, 7)}
        p._download_tracks = [tracks[i] for i in selection]
        p._download_track = p._download_tracks[0]
        p._download_label = "Second"
        p._download_run = types.SimpleNamespace(
            items=[(tracks[i], "HIGH", i) for i in queued])
        p._download_job = {
            "state": "running", "bulk": True, "tier": "HIGH", "track_id": None,
            "tracks": len(queued), "done": 1, "failed": 0,
            "slots": p._new_download_slots(), "error": "",
            "labels": ("First",),
            "estimate": 3 * downloads.estimate_bytes(200, "HIGH"),
        }
        return p

    def test_the_count_and_the_estimate_are_the_runs(self):
        p = self._live()
        text = _box_text(p)
        assert "3 tracks" in text, "the box still described the selection"
        assert downloads.format_bytes(p._download_job["estimate"]) in text

    def test_the_labels_say_how_many_lists_are_in_it(self):
        p = self._live()
        p._download_job = dict(p._download_job,
                               labels=("First", "Second", "Third"))
        assert "First  +2 more" in _box_text(p)

    def test_the_tier_row_says_what_would_be_added(self):
        p = self._live(queued=(1, 2, 3), selection=(3, 4, 5))
        assert "+ 2 to add" in _box_text(p)

    def test_a_selection_wholly_inside_the_run_says_so(self):
        p = self._live(queued=(1, 2, 3, 4, 5), selection=(3, 4, 5))
        assert "already queued" in _box_text(p)

    def test_the_bars_are_visible_from_a_single_track_box_too(self):
        p = self._live(selection=(4,))
        p._download_job["slots"][0].update(title="Nightcall", state="running",
                                           done=10, total=20)
        text = _box_text(p)
        assert "▸ Nightcall" in text
        assert "[x] cancel" in text, "the only key that stops it was hidden"


# ── the settings page ──


class TestTheSettingsPageSaysWhatIsDownloading:
    def _page(self, job):
        p = _player()
        p._cache = cache_mod.MetadataCache(songs=True)
        p._user_display_name = "Garrett"
        p._download_job = job
        p._mode = p.MODE_SETTINGS
        return p

    def _running(self, **over):
        p = _player()
        slots = p._new_download_slots()
        slots[0].update(state="running", samples=(4 * 1024 * 1024,))
        slots[1].update(state="running", samples=(4.3 * 1024 * 1024,))
        job = {"state": "running", "bulk": True, "tier": "HIGH",
               "track_id": None, "tracks": 18, "done": 3, "failed": 0,
               "slots": slots, "error": "", "labels": ("OutRun",),
               "estimate": 0}
        job.update(over)
        return self._page(job)

    def test_the_line_is_the_one_the_owner_asked_for(self):
        p = self._running()
        line = p._build_download_line().plain
        assert line.startswith("Downloading 3/18")
        assert "8.3MB/s" in line
        assert "[x] cancel" in line

    def test_it_is_on_the_page(self):
        p = self._running()
        assert "Downloading 3/18" in p._build_settings_display().plain

    def test_nothing_is_said_when_nothing_is_downloading(self):
        p = self._page(None)
        assert p._build_download_line() is None
        page = p._build_settings_display().plain
        assert "Downloading" not in page
        # ...and the row it borrows is the [R] offer, exactly as before
        assert "upgrade all to" in page

    def test_a_clean_finish_says_nothing_either(self):
        p = self._page({"state": "done", "bulk": True, "done": 18,
                        "failed": 0, "tracks": 18})
        assert p._build_download_line() is None

    def test_a_rate_limited_run_says_so_and_stays_said(self):
        """The state this line exists for: once the box is shut a failure had
        no way to reach the user at all."""
        p = self._page({"state": "blocked", "bulk": True, "done": 5,
                        "failed": 0, "tracks": 18, "error": "429"})
        line = p._build_download_line()
        assert "rate-limiting" in line.plain
        assert "nothing retried" in line.plain
        assert "5 done" in line.plain
        assert "rate-limiting" in p._build_settings_display().plain

    def test_a_failed_single_download_says_what_the_backend_said(self):
        p = self._page({"state": "failed", "error": "404 Not Found",
                        "track_id": 7})
        assert "Download failed — 404 Not Found" in p._build_download_line().plain

    def test_a_run_that_finished_with_failures_says_how_many(self):
        p = self._page({"state": "done", "bulk": True, "done": 16,
                        "failed": 2, "tracks": 18})
        assert "2 failed" in p._build_download_line().plain

    def test_the_refetch_keeps_the_row_when_it_is_the_one_running(self):
        """A re-fetch is this page's own job and [Esc] here is what stops it,
        so it is never displaced. (They cannot both run.)"""
        p = self._running()
        p._refetch_job = {"state": "running", "tier": "MAX", "done": 1,
                          "total": 4, "failed": 0}
        page = p._build_settings_display().plain
        assert "Re-fetching at MAX" in page

    def test_it_costs_no_row_at_eighty_by_twentyfour(self):
        p = self._running()
        p._current_track = _track()
        console = Console(width=80, height=24)
        p.console = console
        with console.capture() as cap:
            console.print(p._build_display())
        rows = cap.get().rstrip("\n").splitlines()
        assert len(rows) == 24, len(rows)
        for row in rows:
            assert cell_len(row) <= 80, repr(row)
        body = "\n".join(rows)
        assert "Downloading 3/18" in body
        assert "[Esc] back" in body, "the footer was pushed off the pane"

    def test_the_rate_is_the_one_already_sampled_on_the_tick(self):
        """Not a second measurement — the same slot means the bars use."""
        p = self._running()
        for slot in p._download_job["slots"]:
            slot.update(state="running", samples=(1024 * 1024,) * 3)
        assert "3.0MB/s" in p._build_download_line().plain


class TestXOnTheSettingsPage:
    def test_it_cancels_the_download_rather_than_clearing_the_cache(self):
        p = _player()
        p._cache = cache_mod.MetadataCache(songs=True)
        p._mode = p.MODE_SETTINGS
        p._download_job = {"state": "running", "bulk": True, "tracks": 4,
                           "done": 0, "failed": 0,
                           "slots": p._new_download_slots()}
        p._handle_key("x")
        assert p._clear_cache_pending is False, \
            "it offered to clear the cache out from under a download"
        assert p._download_job["state"] == "cancelled"

    def test_and_clears_the_cache_when_nothing_is_downloading(self):
        p = _player()
        p._cache = cache_mod.MetadataCache(songs=True)
        p._mode = p.MODE_SETTINGS
        p._handle_key("x")
        assert p._clear_cache_pending is True

    def test_the_cache_row_stops_advertising_the_key_it_is_not_getting(self):
        p = _player()
        p._cache = cache_mod.MetadataCache(songs=True)
        p._user_display_name = "Garrett"
        idle = p._build_settings_display().plain
        assert "[x] clear" in idle
        p._download_job = {"state": "running", "bulk": True, "tracks": 4,
                           "done": 0, "failed": 0,
                           "slots": p._new_download_slots()}
        busy = p._build_settings_display().plain
        assert "[x] clear" not in busy
        assert "[x] cancel" in busy
