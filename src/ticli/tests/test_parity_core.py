"""Parity plumbing: the refetch guard, agent notices, and what `state.track` says."""

import types

import pytest
import tidalapi

from ticli import agentq, humancli
from ticli.commands import AGENT, HUMAN, Commands
from ticli.player import AGENT_NOTICE_SECONDS, HeadlessTidalPlayer
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")


class _NoNetwork:
    is_pkce = False

    def __getattr__(self, name):
        raise AssertionError(f"request attempted: {name}")


def _track(tid):
    return types.SimpleNamespace(id=tid, name=f"Track {tid}", duration=200,
                                 artists=[types.SimpleNamespace(name="Artist")], album=None)


def _player(**config):
    p = HeadlessTidalPlayer()
    p.config.update(config)
    p.session = _NoNetwork()
    p.audio = types.SimpleNamespace(stop=lambda: None)
    p._queue = [_track(1), _track(2), _track(3)]
    p._queue_index = 0
    p._current_track = p._queue[0]
    p._play_track = lambda track, seek=0: None
    p._wake = lambda: None
    p._reconnect = lambda recheck=False: "online"
    p.commands = Commands(p, sleep=lambda s: None)
    return p


PLAN = {"downloads": ["1", "2"], "cache": ["3"], "skipped": 4, "unknown": 1, "bytes": 3000}


def _planned(p, target="HIGH"):
    p._quality_name = "HIGH"
    p._refetch_candidates = lambda: dict(PLAN)
    p._upgrade_target = lambda: p.QUALITY_MAP[target]
    started = []
    p._start_refetch_job = lambda paced=False: (started.append(paced),
                                                setattr(p, "_refetch_job", {"state": "running"}))
    return started


class TestRefetchGuard:
    def test_plan_counts_and_estimates_with_no_request(self):
        p = _player()
        _planned(p)
        reply = p.commands.execute("refetch.plan", {}, caller=AGENT)
        assert reply["ok"]
        r = reply["result"]
        assert (r["songs"], r["downloads"], r["cached"], r["skipped"], r["unknown"]) == (3, 2, 1, 4, 1)
        assert r["requests"] == 6 and r["eta_s"] == 6.0 and r["bytes"] == 3000
        assert r["target"] == "HIGH" and not r["running"] and "note" not in r

    def test_plan_says_when_the_login_caps_the_tier(self):
        p = _player()
        _planned(p, target="MEDIUM")
        r = p.commands.execute("refetch.plan", {}, caller=AGENT)["result"]
        assert r["target"] == "MEDIUM" and "MEDIUM" in r["note"]

    def test_plan_is_a_read_so_no_notice(self):
        p = _player()
        _planned(p)
        p.commands.execute("refetch.plan", {}, caller=AGENT)
        assert not p._toast

    def test_an_agent_refetch_needs_the_dangerous_switch(self):
        p = _player()
        started = _planned(p)
        reply = p.commands.execute("refetch", {}, caller=AGENT)
        assert reply["code"] == "dangerous_off" and started == []

    def test_allowed_it_starts_paced_and_says_what_it_does(self):
        p = _player(allow_dangerous_commands=True)
        started = _planned(p)
        reply = p.commands.execute("refetch", {}, caller=AGENT)
        assert reply["ok"] and started == [True]
        assert reply["result"]["started"] and reply["result"]["songs"] == 3
        assert p._toast == "agent: started a re-fetch"

    def test_the_human_is_never_gated(self):
        p = _player()
        started = _planned(p)
        assert p.commands.execute("refetch", {}, caller=HUMAN)["ok"] and started == [False]

    def test_a_running_refetch_is_not_started_twice(self):
        p = _player(allow_dangerous_commands=True)
        started = _planned(p)
        p._refetch_job = {"state": "running"}
        r = p.commands.execute("refetch", {}, caller=AGENT)["result"]
        assert r["running"] and not r["started"]
        assert started == [True]  # the job itself ignores a second start

    def test_human_cli_lines(self):
        plan = {"songs": 3, "target": "HIGH", "downloads": 2, "cached": 1, "bytes": 3 << 20,
                "requests": 6, "eta_s": 6.0, "tier": "HIGH"}
        assert humancli.line("refetch.plan", {"ok": True, "result": plan}).startswith(
            "would upgrade 3 songs to HIGH (2 downloads, 1 cached")
        assert humancli.line("refetch", {"ok": True, "result": {**plan, "started": True}}).startswith(
            "re-fetching 3 songs")
        nothing = {"songs": 0, "tier": "HIGH"}
        assert humancli.line("refetch.plan", {"ok": True, "result": nothing}) == "nothing below HIGH to upgrade"

    def test_the_human_cli_previews_and_stops_when_nothing_to_do(self, monkeypatch, capsys):
        asked = []

        class _Link:
            def ask(self, cmd, args=None, timeout=None):
                asked.append(cmd)
                return {"ok": True, "result": {"songs": 0, "tier": "HIGH"}}

        with pytest.raises(SystemExit) as stop:
            humancli._preview_refetch(_Link())
        assert stop.value.code == 0 and asked == ["refetch.plan"]
        assert "nothing below HIGH" in capsys.readouterr().out


class TestAgentNotice:
    def test_queue_add_says_how_many(self):
        p = _player()
        p._known[("track", "7")] = _track(7)
        p._known[("track", "8")] = _track(8)
        assert p.commands.execute("queue.add", {"track_ids": [7, 8], "position": "next"}, caller=AGENT)["ok"]
        assert p._toast == "agent: queued 2 tracks to play next"
        assert p._toast_until - __import__("time").time() > AGENT_NOTICE_SECONDS - 1

    def test_play_track_names_it(self):
        p = _player()
        p._play_track = lambda track, seek=0: setattr(p, "_current_track", track)
        assert p.commands.execute("play.track", {"track_id": 2}, caller=AGENT)["ok"]
        assert p._toast == 'agent: playing "Track 2"'

    def test_a_failing_describer_falls_back_to_the_verb(self, monkeypatch):
        from ticli import commands
        monkeypatch.setitem(commands.AGENT_TOASTS, "next", lambda p, a, r: r["missing"])
        p = _player()
        p.commands.execute("next", {}, caller=AGENT)
        assert p._toast == "agent: next"

    def test_human_actions_show_no_agent_notice(self):
        p = _player()
        p.commands.execute("queue.add", {"track_ids": [1]}, caller=HUMAN)
        assert not p._toast.startswith("agent:")


class TestStateTrack:
    def test_liked_and_granted_quality(self):
        p = _player()
        p._liked_ids = {1}
        p._playing_granted = tidalapi.Quality.high_lossless
        track = p.commands.execute("status", {}, caller=HUMAN)["result"]["track"]
        assert track["liked"] is True and track["quality"] == "HIGH"
        state = agentq.compact_state(p.commands.execute("status", {}, caller=HUMAN)["result"])
        assert state["track"]["liked"] is True and state["track"]["quality"] == "HIGH"

    def test_unliked_and_unknown_quality(self):
        p = _player()
        p._liked_ids = {"2"}
        track = p.commands.execute("status", {}, caller=HUMAN)["result"]["track"]
        assert track["liked"] is False and track["quality"] is None

    def test_liked_matches_across_id_types(self):
        p = _player()
        p._liked_ids = {"1"}
        assert p.commands.execute("status", {}, caller=HUMAN)["result"]["track"]["liked"] is True

    def test_a_new_track_forgets_the_old_grant(self, monkeypatch):
        from ticli import player as player_mod
        monkeypatch.setattr(player_mod.threading, "Thread",
                            lambda *a, **k: types.SimpleNamespace(start=lambda: None))
        p = HeadlessTidalPlayer()
        p._playing_granted = tidalapi.Quality.hi_res_lossless
        p.audio = None
        p._local_source = lambda track: (None, None)
        p._reconnect = lambda recheck=False: "offline"
        p._wake = lambda: None
        p._play_track(_track(5))
        assert p._playing_granted is None


class TestJobsInState:
    def test_a_running_download_shows_in_state_and_status(self):
        p = _player()
        p._download_job = {"state": "running", "bulk": True, "tier": "HIGH", "tracks": 5, "done": 2,
                           "failed": 0, "error": "", "slots": []}
        status = p.commands.execute("status", {}, caller=HUMAN)["result"]
        assert status["jobs"] == {"download": {"state": "running", "tier": "HIGH", "tracks": 5,
                                               "done": 2, "failed": 0}}
        assert agentq.compact_state(status)["jobs"]["download"]["done"] == 2

    def test_a_finished_job_stays_out_of_state(self):
        p = _player()
        p._refetch_job = {"state": "done", "tier": "HIGH", "done": 3, "total": 3, "failed": 0}
        status = p.commands.execute("status", {}, caller=HUMAN)["result"]
        assert status["jobs"]["refetch"]["state"] == "done"
        assert "jobs" not in agentq.compact_state(status)


class TestHumanLines:
    def _line(self, cmd, result):
        return humancli.line(cmd, {"ok": True, "result": result})

    def test_queue_and_history(self):
        assert self._line("queue.move", {"index": 0, "queue_length": 3}) == "moved to #1 of 3"
        assert self._line("queue.clear", {"removed": 1, "queue_length": 1}) == \
            "cleared 1 track from the queue"
        assert self._line("history.list", {"history": ["a", "b"]}) == "a\nb"
        assert self._line("history.list", {"history": []}) == "no search history"

    def test_cache_status(self):
        text = self._line("cache.status", {
            "songs": {"count": 2, "bytes": 2 << 20, "budget_bytes": 2 << 30, "enabled": True},
            "downloads": {"count": 1, "bytes": 1 << 20}, "metadata": {"bytes": 1024, "cap_bytes": 100 << 20}})
        assert text.splitlines() == ["cached songs: 2, 2.0 MB of 2.0 GB", "downloads: 1, 1.0 MB",
                                     "metadata: 1.0 KB of 100.0 MB"]

    def test_track_info(self):
        text = self._line("track.info", {
            "track": {"id": 5, "title": "T", "artists": ["A"], "album": "Al", "duration_seconds": 61,
                      "explicit": True},
            "quality": "HIGH", "liked": True, "downloaded": {"tier": "MAX"}, "cached": None})
        assert text.splitlines() == ["5  A - T [1:01]", "album: Al | explicit",
                                     "quality: HIGH | liked | downloaded (MAX)"]

    def test_a_queue_move_is_pinned_to_the_track_it_saw(self):
        class _Link:
            who, picks, labels = humancli.HUMAN, True, {}

            def ask(self, cmd, args=None, timeout=None):
                return {"ok": True, "result": {"tracks": [{"id": 7}, {"id": 8}]}}

        cmd, args = humancli.prepare(_Link(), "queue.move", {"index": 1, "to": 0})
        assert args == {"index": 1, "to": 0, "track_id": 8}

    def test_the_y_n_prompt_names_what_was_resolved(self, monkeypatch):
        asked = []
        monkeypatch.setattr(humancli.click, "confirm", lambda text, default: asked.append(text) or False)
        assert not humancli.confirmed("download.delete", {"track_id": 5})
        assert asked == ['"download delete 5" is dangerous. Continue?']
