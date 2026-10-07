"""Agent commands in the player (ADR-0001, ADR-0007, ADR-0008): one queue in arrival
order, 2 s between requests, merged adds and likes, ETAs, the trip.

A real socket and a real player core in a thread; TIDAL is `fakes.FakeTidal`,
counted at the one place tidalapi touches the network, and time is
`fakes.FakeClock`, so every wait is arithmetic, not sleeping.
"""

import json
import threading
import time

import pytest

from ticli import agentq
from ticli import player as player_mod
from ticli.tests.agent_harness import GYM, ROAD, Harness
from ticli.tests.fakes import FakeClock, FakeResponse, FakeTidal, fake_track
from ticli.utils import throttle
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")


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
        replies = [h.agent(*_adds(ROAD, 100 + i)) for i in range(20)]
        h.release()
        assert h.session.requests == [f"POST playlists/{ROAD}/items", f"GET playlists/{ROAD}"]
        assert h.road.adds == [[str(100 + i) for i in range(20)]]
        assert all(r["ok"] and r["result"]["job"] == replies[0]["result"]["job"] for r in replies)
        assert replies[-1]["result"]["merged"] == '20 adds to "Road trip" -> 2 requests'

    def test_more_than_a_hundred_ids_split_into_requests_of_a_hundred(self, player):
        h = player()
        reply = h.do(_adds(ROAD, *range(150)))
        h.idle()
        assert reply["ok"] and reply["cost"]["requests"] == 4
        assert [len(a) for a in h.road.adds] == [100, 50]
        assert len(h.session.requests) == 4

    def test_adds_to_two_playlists_merge_per_playlist_in_arrival_order(self, player):
        h = player()
        reply = h.do(_adds(ROAD, 1), _adds(GYM, 2), _adds(ROAD, 3), _adds(GYM, 4),
                     _adds(ROAD, 5))
        h.idle()
        assert h.road.adds == [["1", "3", "5"]] and h.gym.adds == [["2", "4"]]
        assert h.session.requests == [f"POST playlists/{ROAD}/items", f"GET playlists/{ROAD}",
                                      f"POST playlists/{GYM}/items", f"GET playlists/{GYM}"]
        records = reply["result"]
        assert records[4]["merged"] == '3 adds to "Road trip" -> 2 requests'
        assert records[3]["merged"] == '2 adds to "Gym" -> 2 requests'
        assert "merged" not in records[0]

    def test_a_read_of_the_playlist_between_adds_keeps_them_apart(self, player):
        h = player()
        h.do(_adds(ROAD, 1), ("playlist.tracks", {"id": ROAD}), _adds(ROAD, 2))
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


class TestQueueAdd:
    def test_known_tracks_cost_nothing_and_answer_at_once(self, player):
        h = player()
        reply = h.agent("queue.add", {"track_ids": [2]})
        assert reply["ok"] and reply["result"]["queue_length"] == 4
        assert reply["cost"] == {"requests": 0, "wait_s": 0, "eta_s": 0.0}
        assert h.session.requests == []

    def test_unknown_ids_wait_their_turn_two_seconds_apart_and_return_the_result(self, player):
        h = player()
        h.do(_adds(ROAD, 7))  # the POST at 0 s, its reparse GET at 2 s
        reply = h.agent("queue.add", {"track_ids": [500, 501], "position": "next"})
        assert reply["ok"] and reply["result"]["added"] == 2, reply
        assert [t["id"] for t in reply["result"]["tracks"]] == [500, 501]
        assert h.clock.sleeps == [2.0, 2.0, 2.0], "four requests, 2 s apart"
        assert reply["cost"]["requests"] == 2
        assert h.session.requests[-2:] == ["GET tracks/500", "GET tracks/501"]
        assert [t.id for t in h.core._queue] == [1, 500, 501, 2, 3]

    def test_an_album_not_yet_opened_is_one_queued_job(self, player):
        h = player()
        h.session.album_tracks["77"] = [fake_track(10), fake_track(11)]
        reply = h.agent("queue.add", {"album": "77"})
        assert reply["ok"] and reply["result"]["added"] == 2
        assert h.session.requests == ["GET albums/77"]
        assert [t.id for t in h.core._queue][-2:] == [10, 11]


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
        reply = h.do(_adds(ROAD, 7), ("search", {"query": "x"}))
        records = reply["result"]
        assert records[0]["queued"] == 1 and records[0]["eta_s"] == 2.0
        assert records[1]["ok"] and records[1]["result"]["tracks"] == []
        # The POST went at 0 s, the reparse GET at 2 s, the search at 4 s.
        assert reply["cost"]["wait_s"] == 4.0 and reply["cost"]["requests"] == 3
        assert h.session.requests[-1] == "GET search"

    def test_an_action_answers_at_once_with_position_and_eta(self, player):
        h = player()
        h.hold()
        first = h.agent(*_adds(ROAD, 1))
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
        for cmd, args in (_adds(ROAD, 1), ("search", {"query": "y"}), ("next", {})):
            refused = h.agent(cmd, args)
            assert refused["code"] == "rate_limited", cmd
        assert len(h.session.requests) == calls
        assert h.agent("pause")["ok"]  # local, no TIDAL
        human = h.human("search", {"query": "z"})
        assert human["ok"] and len(h.session.requests) == calls + 1

    def test_a_4006_trips_and_a_plain_401_does_not(self, player, monkeypatch):
        monkeypatch.setattr(player_mod, "load_tokens", lambda: {"token_type": "Bearer", "access_token": "a"})
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
        h.agent(*_adds(ROAD, 1))
        throttle.trip("http_429")
        h.release()
        assert h.road.adds == []
        assert h.agent("status")["result"]["done"][-1] == {
            "job": 1, "cmd": "playlist.add", "ok": False, "code": "rate_limited"}


class TestReplies:
    def test_a_typical_reply_is_small_and_complete(self, player):
        h = player()
        for reply in (h.agent("pause"), h.agent(*_adds(ROAD, 1, 2, 3))):
            assert set(reply) == {"id", "ok", "result", "state", "next", "cost"}
            assert len(json.dumps(reply, separators=(",", ":"))) < 500
        state = h.agent("status")["state"]
        assert state["track"]["title"] == "Track 1" and state["queue"] == {"len": 3, "index": 0}
        assert state["switches"] == {"ai": True, "dangerous": False}

    def test_next_forms_follow_the_result_and_the_state(self, player):
        h = player(session=FakeTidal(search_tracks=[fake_track(42)]))
        reply = h.agent("search", {"query": "x"})
        assert reply["next"][:3] == ["play track 42", "queue add 42", "playlist add <playlist_id> 42"]
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
        assert h.agent(*_adds(ROAD, 1), key="nope")["code"] == "wrong_key"
        assert h.agent(*_adds(ROAD, 1), key="sesame")["ok"]
        h.idle()
        assert h.road.adds == [["1"]]


class TestDo:
    def test_runs_in_order_local_first_then_queued_behind_tidal(self, player):
        h = player()
        reply = h.do(("queue.remove", {"index": 2}), _adds(ROAD, 9),
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
    h.agent(*_adds(ROAD, 1))
    h.core._playing = False
    h.keeper.close()
    time.sleep(0.05)
    assert h.thread.is_alive() and not h.server.should_exit()
    h.clock.hold.set()
    h.idle()
    h.server.wake()
    h.thread.join(2)
    assert not h.thread.is_alive() and h.road.adds == [["1"]]


def _settle(fn, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not fn():
        assert time.monotonic() < deadline, "never settled"
        time.sleep(0.01)


def _gated(playlist):
    """Make `playlist.add` wait for the returned event, as a slow TIDAL would."""
    gate, real = threading.Event(), playlist.add
    gate.entered = threading.Event()

    def add(ids):
        gate.entered.set()
        gate.wait(5)
        return real(ids)
    playlist.add = add
    return gate


class TestPlaylistWritesDontBlockEachOther:
    def test_an_agent_add_in_flight_never_makes_a_human_add_busy(self, player):
        h = player()
        gate = _gated(h.road)
        h.agent(*_adds(ROAD, "251380837"))
        assert gate.entered.wait(3)
        human = h.human("playlist.add", {"id": GYM, "track_ids": ["251380838"]})
        assert human["ok"], human
        _settle(lambda: h.gym.adds == [["251380838"]])
        gate.set()
        h.idle()
        assert h.road.adds == [["251380837"]]

    def test_a_human_add_in_flight_makes_queued_agent_adds_wait_not_fail(self, player):
        h = player()
        gate = _gated(h.gym)
        assert h.human("playlist.add", {"id": GYM, "track_ids": ["9"]})["ok"]
        _settle(lambda: h.core._picker_busy)
        h.hold()
        first = h.agent(*_adds(ROAD, "1"))
        second = h.agent(*_adds(ROAD, "2"))
        assert first["result"]["job"] == second["result"]["job"]
        h.clock.hold.set()
        time.sleep(0.1)
        assert h.road.adds == [] and h.agent("status")["result"]["done"] == []
        gate.set()
        h.idle()
        assert h.road.adds == [["1", "2"]] and h.gym.adds == [["9"]]
        assert h.agent("status")["result"]["done"] == [
            {"job": first["result"]["job"], "cmd": "playlist.add", "ok": True,
             "merged": 2, "added": 2}]

    def test_human_reads_and_transport_are_not_held_by_the_agent_queue(self, player):
        h = player()
        h.hold()
        h.agent(*_adds(ROAD, "1"))
        assert h.human("search", {"query": "z"})["ok"] and h.human("next")["ok"]
        assert h.session.requests == ["GET search"]
        h.release()


class TestLocalCommandsKeepTheirPlace:
    def test_next_then_pause_ends_paused(self, player):
        h = player()
        h.hold()
        n, p = h.agent("next"), h.agent("pause")
        assert n["result"]["queued"] == 1 and p["result"]["queued"] == 2
        assert h.core._playing is True
        h.release()
        assert h.core._playing is False and h.core._queue_index == 1

    def test_reads_still_answer_at_once(self, player):
        h = player()
        h.hold()
        h.agent("next")
        listed = h.agent("queue.list")
        assert listed["ok"] and "queued" not in listed["result"] and listed["result"]["index"] == 0
        h.release()

    def test_with_nothing_queued_a_local_command_runs_at_once(self, player):
        h = player()
        reply = h.agent("pause")
        assert reply["ok"] and "queued" not in (reply["result"] or {})
        assert h.core._playing is False


class TestAgentDownloadsArePaced:
    @pytest.fixture
    def jobs(self, player, monkeypatch):
        from ticli import player as player_mod
        monkeypatch.setattr(player_mod, "REFETCH_MIN_INTERVAL", 0.01)
        h = player()
        acquired, active, peak = [], [0], [0]
        monkeypatch.setattr(throttle, "acquire", lambda *a, **k: acquired.append(1))
        h.core._download_plan = lambda track, tier: {"track_id": track.id, "title": "t"}

        def deliver(plan, abandoned=None, progress=None, record=True):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            time.sleep(0.03)
            active[0] -= 1
            return None, "", 1, False
        h.core._download_deliver = deliver
        h.core._track_estimate = lambda track, tier: 1
        return h, acquired, peak

    def _finished(self, h):
        _settle(lambda: (h.core._download_job or {}).get("state") == "done")

    def test_an_agent_download_takes_one_slot_and_one_throttle_turn_per_track(self, jobs):
        h, acquired, peak = jobs
        assert h.agent("download", {"track_ids": [1, 2, 3]})["ok"]
        self._finished(h)
        assert len(acquired) == 3 and peak[0] == 1

    def test_one_track_is_paced_too(self, jobs):
        h, acquired, _peak = jobs
        assert h.agent("download", {"track_ids": [2]})["ok"]
        self._finished(h)
        assert len(acquired) == 1

    def test_a_human_download_is_unchanged(self, jobs):
        h, acquired, peak = jobs
        assert h.human("download", {"track_ids": [1, 2, 3]})["ok"]
        self._finished(h)
        assert acquired == [] and peak[0] >= 1

    def test_an_agent_refetch_is_paced(self, jobs):
        h, acquired, _peak = jobs
        h.core._refetch_candidates = lambda: {"downloads": ["1", "2"], "cache": []}
        fetched = []
        h.core._refetch_one = lambda kind, key, tier, gen: fetched.append(key)
        assert h.agent("refetch")["ok"]
        _settle(lambda: (h.core._refetch_job or {}).get("state") == "done")
        assert fetched == ["1", "2"] and len(acquired) == 2

    def test_unknown_ids_are_in_the_estimate(self, player):
        h = player()
        assert agentq.estimate(h.core, "download", {"track_ids": [1, 2]}) == 0
        assert agentq.estimate(h.core, "download", {"track_ids": [1, 251380837, 251380838]}) == 2


class TestPartialAndBatchedWrites:
    def test_a_merged_add_failing_partway_says_what_got_in(self, player):
        h = player()
        h.hold()
        ids = [str(251380000 + i) for i in range(150)]
        h.agent(*_adds(ROAD, *ids[:75]))
        h.agent(*_adds(ROAD, *ids[75:]))
        h.session.request_session.answers = [FakeResponse(200), FakeResponse(200), FakeResponse(500)]
        h.release()
        row = h.agent("status")["result"]["done"][-1]
        assert row["ok"] is False and row["merged"] == 2
        assert row["added"] == 100 and row["failed_from"] == 100 and row["not_added"] == ids[100:]
        assert [len(a) for a in h.road.adds] == [100]

    def test_create_with_many_tracks_adds_a_hundred_at_a_time(self, player):
        h = player()
        ids = [str(251380000 + i) for i in range(150)]
        assert agentq.estimate(h.core, "playlist.create", {"name": "x", "track_ids": ids}) == 5
        reply = h.do(("playlist.create", {"name": "Big", "track_ids": ids}))
        created = h.session.playlists[reply["result"][0]["result"]["playlist"]["id"]]
        assert [len(a) for a in created.adds] == [100, 50]

    def test_a_refusal_after_a_queued_item_skips_only_what_follows(self, player):
        h = player()
        reply = h.do(_adds(ROAD, "1"), ("logout", {}), ("pause", {}))
        h.idle()
        assert [r.get("code") for r in reply["result"]] == [None, "dangerous_off", "skipped"]
        assert reply["ok"] is False and h.road.adds == [["1"]] and h.core._playing is True

    def test_do_failing_to_queue_replies_exactly_once(self, player, monkeypatch):
        h = player()
        posts = []
        real_post, real_submit = h.server._post, h.server.agent_queue.submit_many
        h.server._post = lambda client, rid, msg: posts.append(msg) or real_post(client, rid, msg)

        def half(items):
            real_submit(items[:1])
            raise RuntimeError("queue full")
        monkeypatch.setattr(h.server.agent_queue, "submit_many", half)
        reply = h.do(("playlist.create", {"name": "A"}), _adds(ROAD, "1"))
        assert reply["ok"] is False and reply["code"] == "failed"
        h.idle()
        time.sleep(0.05)
        assert len(posts) == 1

    def test_a_background_thread_that_cannot_start_is_not_counted(self, monkeypatch):
        from ticli import commands

        class Unstartable:
            def __init__(self, *a, **k):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")
        monkeypatch.setattr(commands.threading, "Thread", Unstartable)
        with pytest.raises(RuntimeError):
            commands._background(lambda: None)
        monkeypatch.undo()
        assert commands.in_flight() == 0
