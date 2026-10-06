"""Agent commands in the player (ADR-0001, ADR-0007, ADR-0008): one queue in arrival
order, 2 s between requests, merged adds and likes, ETAs, the trip.

A real socket and a real player core in a thread; TIDAL is `fakes.FakeTidal`,
counted at the one place tidalapi touches the network, and time is
`fakes.FakeClock`, so every wait is arithmetic, not sleeping.
"""

import io
import json
import threading
import time

import pytest
from rich.console import Console

from ticli import agentq, ipc, playerd
from ticli.player import HeadlessTidalPlayer
from ticli.tests.fakes import FakeClock, FakeResponse, FakeTidal, fake_track
from ticli.utils import throttle
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")


class _Audio:
    player_cmd = "mpv"
    is_paused = False
    is_playing = True

    def volume_ceiling(self):
        return 130

    def pause(self):
        self.is_paused = True

    def resume(self):
        self.is_paused = False
        return True

    def stop(self):
        pass

    def get_time_pos(self):
        return None

    def set_volume(self, value):
        pass


def make_core(session):
    core = HeadlessTidalPlayer()
    core.console = Console(file=io.StringIO())
    core.session = session
    core.audio = _Audio()
    core._queue = [fake_track(1), fake_track(2), fake_track(3)]
    core._queue_index = 0

    def _play(track, seek=0):
        core._current_track = track
        core._playing = True
        core._play_offset = seek
        core._play_start_time = time.time()
        core._wake()

    core._play_track = _play
    core._save_state = lambda: None
    core._remember_last_playlist = lambda playlist: None
    _play(core._queue[0])
    return core


class Harness:
    def __init__(self, session=None, clock=None):
        self.session = session or FakeTidal()
        self.clock = clock or FakeClock()
        self.core = make_core(self.session)
        self.road = self.session.add_playlist("road", "Road trip")
        self.gym = self.session.add_playlist("gym", "Gym")
        self.core._editable_playlists = [self.road, self.gym]
        self.server = playerd.PlayerServer(self.core, clock=self.clock, sleep=self.clock.sleep)
        self.server.listen()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.keeper = ipc.connect()  # a client stays, so the player doesn't leave between calls

    def _serve(self):
        try:
            self.server.serve()
        finally:
            self.server.close()

    def agent(self, cmd, args=None, key=None):
        conn = ipc.connect()
        try:
            return conn.request(cmd, args or {}, caller="agent", key=key, timeout=5)
        finally:
            conn.close()

    def human(self, cmd, args=None):
        conn = ipc.connect()
        try:
            return conn.request(cmd, args or {}, caller="human", timeout=5)
        finally:
            conn.close()

    def do(self, *items):
        return self.agent("agent.do", {"commands": [
            {"cmd": c, "args": a} for c, a in items]})

    def idle(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while self.server.agent_queue.busy():
            assert time.monotonic() < deadline, "the agent queue never drained"
            time.sleep(0.01)

    def hold(self):
        """Make the throttle busy and park the queue's sleeper, so commands wait."""
        throttle.acquire(now=self.clock, sleep=self.clock.sleep)
        self.clock.hold = threading.Event()

    def release(self):
        self.clock.hold.set()
        self.idle()

    def stop(self):
        self.keeper.close()
        self.core.running = False
        self.server.wake()
        self.thread.join(2)


@pytest.fixture
def player():
    made = []

    def _make(**kw):
        made.append(Harness(**kw))
        return made[-1]

    yield _make
    for h in made:
        h.stop()


def _adds(pid, *ids):
    return ("playlist.add", {"id": pid, "track_ids": list(ids)})


class TestCoalescing:
    def test_twenty_waiting_adds_to_one_playlist_are_two_requests(self, player):
        h = player()
        h.hold()
        replies = [h.agent(*_adds("road", 100 + i)) for i in range(20)]
        h.release()
        assert h.session.requests == ["POST playlists/road/items", "GET playlists/road"]
        assert h.road.adds == [[str(100 + i) for i in range(20)]]
        assert all(r["ok"] and r["result"]["job"] == replies[0]["result"]["job"] for r in replies)
        assert replies[-1]["result"]["merged"] == '20 adds to "Road trip" -> 2 requests'

    def test_more_than_a_hundred_ids_split_into_requests_of_a_hundred(self, player):
        h = player()
        reply = h.do(_adds("road", *range(150)))
        h.idle()
        assert reply["ok"] and reply["cost"]["requests"] == 4
        assert [len(a) for a in h.road.adds] == [100, 50]
        assert len(h.session.requests) == 4

    def test_adds_to_two_playlists_merge_per_playlist_in_arrival_order(self, player):
        h = player()
        reply = h.do(_adds("road", 1), _adds("gym", 2), _adds("road", 3), _adds("gym", 4),
                     _adds("road", 5))
        h.idle()
        assert h.road.adds == [["1", "3", "5"]] and h.gym.adds == [["2", "4"]]
        assert h.session.requests == ["POST playlists/road/items", "GET playlists/road",
                                      "POST playlists/gym/items", "GET playlists/gym"]
        records = reply["result"]
        assert records[4]["merged"] == '3 adds to "Road trip" -> 2 requests'
        assert records[3]["merged"] == '2 adds to "Gym" -> 2 requests'
        assert "merged" not in records[0]

    def test_a_read_of_the_playlist_between_adds_keeps_them_apart(self, player):
        h = player()
        h.do(_adds("road", 1), ("playlist.tracks", {"id": "road"}), _adds("road", 2))
        h.idle()
        assert h.road.adds == [["1"], ["2"]]

    def test_likes_merge_into_one_request_unlikes_are_one_each(self, player):
        h = player()
        h.do(("like", {"track_ids": [1]}), ("like", {"track_ids": [2]}),
             ("like", {"track_ids": [3]}))
        h.idle()
        assert h.session.requests == ["POST favorites/tracks"]
        assert {1, 2, 3} <= h.core._liked_ids
        h.do(("unlike", {"track_ids": [1]}), ("unlike", {"track_ids": [2]}))
        h.idle()
        assert h.session.requests[1:] == ["DELETE favorites/tracks/1", "DELETE favorites/tracks/2"]

    def test_queue_edits_cost_nothing_and_answer_at_once(self, player):
        h = player()
        reply = h.agent("queue.remove", {"index": 2})
        assert reply["ok"] and reply["result"] == {"queue_length": 2}
        assert reply["cost"] == {"requests": 0, "wait_s": 0, "eta_s": 0.0}
        assert h.session.requests == []


class TestTiming:
    def test_eta_counts_two_seconds_per_request_ahead(self):
        clock = FakeClock()
        throttle.acquire(now=clock, sleep=clock.sleep)  # next slot at +2 s
        clock.hold = threading.Event()
        queue = agentq.TidalQueue(lambda job: {"ok": True}, lambda cmd, args: len(args["n"]),
                                  clock=clock, sleep=clock.sleep)
        infos = queue.submit_many([("x", {"n": [1, 2]}, None, None, False),
                                   ("y", {"n": [1]}, None, None, False),
                                   ("z", {"n": [1, 2, 3]}, None, None, False)])
        # x: requests at +2, +4; y at +6; z at +8, +10, +12.
        assert [i["eta_s"] for i in infos] == [4.0, 6.0, 12.0]
        assert [i["position"] for i in infos] == [1, 2, 3]
        assert queue.eta_last() == 12.0
        clock.hold.set()
        queue.stop()

    def test_a_read_waits_its_turn_and_says_how_long(self, player):
        h = player()
        reply = h.do(_adds("road", 7), ("search", {"query": "x"}))
        records = reply["result"]
        assert records[0]["queued"] == 1 and records[0]["eta_s"] == 2.0
        assert records[1]["ok"] and records[1]["result"]["tracks"] == []
        # The POST went at 0 s, the reparse GET at 2 s, the search at 4 s.
        assert reply["cost"]["wait_s"] == 4.0 and reply["cost"]["requests"] == 3
        assert h.session.requests[-1] == "GET search"

    def test_an_action_answers_at_once_with_position_and_eta(self, player):
        h = player()
        h.hold()
        first = h.agent(*_adds("road", 1))
        second = h.agent("next")
        assert first["result"]["queued"] == 1 and first["cost"]["eta_s"] == 4.0
        assert second["result"]["queued"] == 2 and second["cost"]["eta_s"] == 6.0
        status = h.agent("status")
        assert [(p["cmd"], p["eta_s"]) for p in status["result"]["pending"]] == [
            ("playlist.add", 4.0), ("next", 6.0)]
        assert status["state"]["pending"] == 2
        h.release()
        done = h.agent("status")["result"]
        assert done["pending"] == [] and [d["cmd"] for d in done["done"]] == ["playlist.add", "next"]


class TestTheTrip:
    def test_a_429_trips_every_agent_tidal_command_and_only_them(self, player):
        h = player()
        h.session.request_session.answers = [FakeResponse(429)]
        reply = h.agent("search", {"query": "x"})
        assert reply["ok"] is False and reply["code"] == "rate_limited"
        assert "ticli agent unblock" in reply["fix"] and "your human" in reply["fix"]
        assert throttle.tripped()["reason"] == "http_429"
        calls = len(h.session.requests)
        for cmd, args in (_adds("road", 1), ("search", {"query": "y"}), ("next", {})):
            refused = h.agent(cmd, args)
            assert refused["code"] == "rate_limited", cmd
        assert len(h.session.requests) == calls
        assert h.agent("pause")["ok"]  # local, no TIDAL
        human = h.human("search", {"query": "z"})
        assert human["ok"] and len(h.session.requests) == calls + 1

    def test_a_4006_trips_and_a_plain_401_does_not(self, player):
        h = player()
        h.session.request_session.answers = [FakeResponse(401)]
        assert h.agent("search", {"query": "x"})["code"] == "auth_failed"
        assert throttle.tripped() is None
        h.session.request_session.answers = [FakeResponse(401, {"subStatus": 4006})]
        assert h.agent("search", {"query": "x"})["code"] == "rate_limited"
        assert throttle.tripped()["reason"] == "substatus_4006"

    def test_queued_actions_behind_a_trip_fail_and_say_so_in_status(self, player):
        h = player()
        h.hold()
        h.agent(*_adds("road", 1))
        throttle.trip("http_429")
        h.release()
        assert h.road.adds == []
        assert h.agent("status")["result"]["done"][-1] == {
            "job": 1, "cmd": "playlist.add", "ok": False, "code": "rate_limited"}


class TestReplies:
    def test_a_typical_reply_is_small_and_complete(self, player):
        h = player()
        for reply in (h.agent("pause"), h.agent(*_adds("road", 1, 2, 3))):
            assert set(reply) == {"id", "ok", "result", "state", "next", "cost"}
            assert len(json.dumps(reply, separators=(",", ":"))) < 500
        state = h.agent("status")["state"]
        assert state["track"]["title"] == "Track 1" and state["queue"] == {"len": 3, "index": 0}
        assert state["switches"] == {"ai": True, "dangerous": False}

    def test_next_forms_follow_the_result_and_the_state(self, player):
        h = player(session=FakeTidal(search_tracks=[fake_track(42)]))
        reply = h.agent("search", {"query": "x"})
        assert reply["next"][:2] == ["play track 42", "playlist add <playlist_id> 42"]
        assert "pause" in reply["next"] and len(reply["next"]) <= 5
        h.agent("pause")
        assert "resume" in h.agent("status")["next"]

    def test_errors_are_code_reason_fix(self, player):
        h = player()
        reply = h.agent("cache.clear")
        assert set(reply) == {"id", "ok", "code", "reason", "fix"}
        assert reply["code"] == "dangerous_off" and "Ask your human" in reply["fix"]

    def test_a_wrong_key_is_refused_before_anything_queues(self, player, monkeypatch):
        config_mod.save_config({**config_mod.DEFAULTS,
                                "ai_control_key": config_mod.hash_ai_key("sesame")})
        h = player()
        h.core.config = config_mod.load_config()
        h.core.commands._sleep = lambda s: None
        assert h.agent(*_adds("road", 1), key="nope")["code"] == "wrong_key"
        assert h.agent(*_adds("road", 1), key="sesame")["ok"]
        h.idle()
        assert h.road.adds == [["1"]]


class TestDo:
    def test_runs_in_order_local_first_then_queued_behind_tidal(self, player):
        h = player()
        reply = h.do(("queue.remove", {"index": 2}), _adds("road", 9),
                     ("queue.remove", {"index": 1}))
        records = reply["result"]
        assert records[0]["result"] == {"queue_length": 2}
        assert records[1]["queued"] == 1 and records[2]["queued"] == 2
        h.idle()
        assert [t.id for t in h.core._queue] == [1]

    def test_a_refusal_stops_the_batch_and_skips_the_rest(self, player):
        h = player()
        reply = h.do(("pause", {}), ("cache.clear", {}), ("resume", {}))
        assert reply["ok"] is False
        assert [r.get("code") for r in reply["result"]] == [None, "dangerous_off", "skipped"]
        assert h.core._playing is False

    def test_blocking_create_returns_the_new_playlist(self, player):
        h = player()
        reply = h.do(("playlist.create", {"name": "Fresh"}))
        created = reply["result"][0]["result"]["playlist"]
        assert created["name"] == "Fresh" and h.session.playlists[created["id"]].name == "Fresh"

    def test_rejects_a_malformed_batch(self, player):
        h = player()
        assert h.agent("agent.do", {"commands": "pause"})["code"] == "bad_args"


def test_the_player_stays_while_agent_work_is_queued(player):
    h = player()
    h.hold()
    h.agent(*_adds("road", 1))
    h.core._playing = False
    h.keeper.close()
    time.sleep(0.05)
    assert h.thread.is_alive() and not h.server.should_exit()
    h.clock.hold.set()
    h.idle()
    h.server.wake()
    h.thread.join(2)
    assert not h.thread.is_alive() and h.road.adds == [["1"]]
