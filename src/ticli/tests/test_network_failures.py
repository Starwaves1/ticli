"""No network is one failure, not one per request.

tidalapi set no timeout, so a socket that accepted and never answered hung
login forever; and with the network gone, every loop that steps on after a
failure (the tier ladder, the paced bulk run, the legacy restore) turned one
outage into a request per item, each one read as a rate limit if its URL
happened to carry a track id containing 429 or 4006.
"""

import json
import time
import types

import pytest
import requests

from ticli import player as player_mod
from ticli.player import HeadlessTidalPlayer
from ticli.tests.test_bulk_downloads import _bulk, _bulk_library, _counting
from ticli.utils import net
from ticli.utils.cache import CachedTrack
from urllib3.exceptions import (ConnectTimeoutError, MaxRetryError,
                                NewConnectionError, ReadTimeoutError)

OFFLINE_TEXT = (
    "HTTPSConnectionPool(host='api.tidal.com', port=443): Max retries exceeded "
    "with url: /v1/tracks/154291836/playbackinfo?audioquality=HI_RES_LOSSLESS"
    "&playbackmode=STREAM&assetpresentation=FULL&countryCode=US (Caused by "
    "NameResolutionError(\"<urllib3.connection.HTTPSConnection object at "
    "0x104b2e990>: Failed to resolve 'api.tidal.com' ([Errno 8] nodename nor "
    "servname provided, or not known)\"))")


def _offline(track_id=154291836):
    return requests.exceptions.ConnectionError(OFFLINE_TEXT.replace("154291836", str(track_id)))


def _wait_for(cond, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


class TestClassification:
    def test_a_name_resolution_failure_is_offline_not_a_rate_limit(self):
        e = _offline()
        assert net.is_transport_failure(e)
        assert not player_mod._rate_limited(e)
        assert not player_mod._looks_rate_limited(OFFLINE_TEXT)

    def test_ids_containing_the_codes_are_not_the_codes(self):
        for text in ("GET /v1/tracks/429/playbackinfo failed",
                     "url: /v1/tracks/4006?countryCode=US",
                     "?trackId=4006&x=1", "track 14290 not found"):
            assert not player_mod._looks_rate_limited(text), text

    @pytest.mark.parametrize("text", [
        "429 Too Many Requests",
        "429 Client Error: Too Many Requests for url: https://api.tidal.com/v1/tracks/1",
        "Too many requests",
        "401 subStatus 4006 Session does not have streaming privileges",
        '{"status": 401, "subStatus": 4006}',
    ])
    def test_genuine_rate_limits_still_are(self, text):
        assert player_mod._looks_rate_limited(text)

    def test_a_401_whose_body_says_4006_is_a_rate_limit(self):
        response = types.SimpleNamespace(
            status_code=401, json=lambda: {"subStatus": 4006})
        e = requests.HTTPError("401 Client Error: Unauthorized for url", response=response)
        assert player_mod._rate_limited(e)

    def test_a_read_timeout_is_not_offline(self):
        assert not net.is_transport_failure(requests.exceptions.ReadTimeout("slow"))
        assert net.is_transport_failure(requests.exceptions.ConnectTimeout("no"))

    def test_a_wrapped_connection_error_is_offline(self):
        try:
            try:
                raise _offline()
            except requests.exceptions.ConnectionError as inner:
                raise RuntimeError("stream failed") from inner
        except RuntimeError as outer:
            assert net.is_transport_failure(outer)


def _raised(make):
    try:
        make()
    except Exception as e:
        return e
    raise AssertionError("nothing raised")


class _StallingRaw:
    def stream(self, chunk_size, decode_content=True):
        raise ReadTimeoutError(None, "https://sp-ad-fa.audio.tidal.com/x", "Read timed out.")
        yield b""


class TestAStalledReadIsNotAnOutage:
    def test_a_read_timeout_inside_iter_content_is_not_offline(self):
        response = requests.Response()
        response.raw = _StallingRaw()
        e = _raised(lambda: list(response.iter_content(1024)))
        assert isinstance(e, requests.exceptions.ConnectionError)
        assert not net.is_transport_failure(e)

    def test_connect_timeouts_and_refused_connections_still_are(self):
        url = "https://api.tidal.com/v1/tracks/1"
        connect = requests.exceptions.ConnectTimeout(
            MaxRetryError(None, url, ConnectTimeoutError(None, "timed out")))
        refused = requests.exceptions.ConnectionError(
            MaxRetryError(None, url, NewConnectionError(None, "Connection refused")))
        assert net.is_transport_failure(connect)
        assert net.is_transport_failure(refused)


class _RecordingAdapter(requests.adapters.BaseAdapter):
    def __init__(self):
        super().__init__()
        self.timeouts = []

    def send(self, request, timeout=None, **kwargs):
        self.timeouts.append(timeout)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"access_token": "a", "expires_in": 60, "token_type": "Bearer"}'
        response.headers["Content-Type"] = "application/json"
        response.request = request
        response.url = request.url
        return response

    def close(self):
        pass


class TestEveryRequestHasATimeout:
    def test_api_calls_and_token_posts_carry_the_default(self):
        session = net.tidal_session()
        adapter = _RecordingAdapter()
        session.request_session.mount("https://", adapter)
        session.request_session.mount("http://", adapter)
        session.request.request("GET", "tracks/1")
        session.token_refresh("refresh-token")
        session.request_session.get("https://example.invalid/x", timeout=(1, 2))
        assert adapter.timeouts[:2] == [net.API_TIMEOUT, net.API_TIMEOUT]
        assert adapter.timeouts[2] == (1, 2)
        assert all(t is not None for t in adapter.timeouts)

    def test_the_player_builds_its_session_that_way(self):
        p = HeadlessTidalPlayer()
        assert isinstance(p.session.request_session, net.TimeoutSession)


class TestTheTierLadderStopsOnAnOutage:
    def test_one_get_stream_for_a_max_fetch_with_no_network(self):
        p = HeadlessTidalPlayer(quality="MAX")
        p.session = types.SimpleNamespace(audio_quality=None, is_pkce=False)
        calls = []

        def _stream():
            calls.append(1)
            raise _offline(1234)

        real = types.SimpleNamespace(id=1234, get_stream=_stream)
        with pytest.raises(requests.exceptions.ConnectionError):
            p._stream_at_best_tier(real, "MAX")
        assert len(calls) == 1


class TestAPacedRunStopsOnAnOutage:
    def test_a_bulk_download_with_no_network_asks_once(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.02)
        p, tracks, server, _payloads = _bulk_library(10)
        try:
            calls = []

            def _fail(tid):
                raise _offline(tid)

            _counting(tracks, calls, fail=_fail)
            job = _bulk(p, tier="MAX")
            assert calls == [1], calls
            assert job["state"] == "failed"
            assert player_mod.OFFLINE_MESSAGE in p._toast
            assert "rate-limiting" not in p._toast
        finally:
            server.close()

    def test_a_bulk_download_of_cached_rows_with_no_network_asks_once(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.02)
        p, _tracks, server, _payloads = _bulk_library(10)
        try:
            rows = [CachedTrack({"id": tid, "name": f"Row {tid}", "duration": 100})
                    for tid in range(1, 11)]
            p._download_tracks = rows
            p._download_track = rows[0]
            calls = []

            def _track(tid):
                calls.append(tid)
                raise _offline(tid)

            p.session.track = _track
            job = _bulk(p)
            assert calls == [1], calls
            assert job["state"] == "failed"
            assert player_mod.OFFLINE_MESSAGE in p._toast
        finally:
            server.close()

    def test_a_refetch_with_no_network_asks_once_and_says_it_stopped(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.0)
        p = HeadlessTidalPlayer()
        calls = []

        def _track(tid):
            calls.append(tid)
            raise _offline(tid)

        p.session = types.SimpleNamespace(audio_quality=None, is_pkce=False, track=_track)
        p._refetch_candidates = lambda: {"downloads": [1, 2, 3], "cache": []}
        p._start_refetch_job()
        assert _wait_for(lambda: (p._refetch_job or {}).get("state") != "running")
        assert calls == [1]
        assert p._refetch_job["state"] == "failed"
        line = p._build_refetch_line().plain
        assert f"Stopped — {player_mod.OFFLINE_MESSAGE}" in line
        assert "upgrade all" not in line

    def test_a_cdn_outage_mid_fetch_stops_the_run(self, monkeypatch):
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.0)
        fetched = []

        def _fetch(item, handle, slot):
            fetched.append(item)
            raise _offline()

        run = player_mod._PacedRun(items=range(50), resolve=lambda i: i, fetch=_fetch,
                                   alive=lambda: True, report=lambda **k: None)
        done, failed, blocked = run.run()
        assert fetched == [0]
        assert (done, failed, blocked) == (0, 1, "")
        assert run.offline


class TestLegacyRestoreStopsOnAnOutage:
    def test_twenty_ids_one_request_and_the_file_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(player_mod, "_restore_sleep", lambda seconds: None)
        monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path)
        state_file = tmp_path / "player_state.json"
        monkeypatch.setattr(player_mod, "STATE_FILE", state_file)
        state_file.write_text(json.dumps({
            "track_ids": list(range(1, 21)), "queue_index": 4,
            "position": 10, "search_history": []}))
        before = state_file.read_bytes()
        calls = []

        def _track(tid):
            calls.append(tid)
            raise _offline()

        p = HeadlessTidalPlayer()
        p.session = types.SimpleNamespace(track=_track, is_pkce=False)
        p._restore_state()

        assert _wait_for(lambda: player_mod.OFFLINE_MESSAGE in p._toast)
        time.sleep(0.1)
        assert calls == [5]
        assert p._queue == []
        assert p._restore_pending is True
        p._save_state()
        assert state_file.read_bytes() == before


class _Audio:
    def __init__(self, seekable=True):
        self.plays = []
        self.seekable = seekable

    def play_url(self, url, **kwargs):
        self.plays.append(url)

    def seek_to(self, target):
        return self.seekable


class TestPlayFailureSaysSo:
    def _player(self, track_fn=None):
        p = HeadlessTidalPlayer()
        p.session = types.SimpleNamespace(audio_quality=None, is_pkce=False,
                                          track=track_fn or (lambda tid: None))
        p.audio = _Audio()
        return p

    def _unreachable_track(self):
        def _stream():
            raise _offline()
        return types.SimpleNamespace(id=154291836, name="Song", artists=[],
                                     duration=200, get_stream=_stream)

    def test_a_stream_that_cannot_be_fetched_shows_a_toast(self):
        p = self._player()
        p._play_track(self._unreachable_track())
        assert _wait_for(lambda: player_mod.OFFLINE_MESSAGE in p._toast)
        assert p._playing is False
        assert p.audio.plays == []

    def test_a_cached_row_that_cannot_be_resolved_shows_a_toast(self):
        def _track(tid):
            raise _offline()

        p = self._player(_track)
        row = types.SimpleNamespace(id=7, cached=True, name="Row", artists=[])
        p._play_track(row)
        assert _wait_for(lambda: player_mod.OFFLINE_MESSAGE in p._toast)
        assert p._playing is False

    def test_a_seek_that_falls_back_to_replaying_shows_the_toast(self):
        p = self._player()
        p.audio = _Audio(seekable=False)
        p._current_track = self._unreachable_track()
        p._seek_target = 30.0
        p._last_seek_apply = 0.0
        p._flush_seek()
        assert _wait_for(lambda: player_mod.OFFLINE_MESSAGE in p._toast)
