"""The agent surface: JSON contract, throttle enforcement, resolve ranking.

Two of these are regression tests for failures that actually happened on
2026-08-25, the session that motivated the feature:

- an agent fired ~30 requests in seconds because the rate rules lived in
  Markdown (TestThrottle asserts the brake is now in the request path);
- a resolver whose remix penalty outweighed its artist bonus served
  "The Journey" by H.E.R. for Folamour's (TestResolve replays it).

Everything runs against fakes — zero live requests (docs/adr/0001-tidal-rate-limits.md).
The
throttle tests inject `now`/`sleep` and then assert the *file on disk*,
because the reservation arithmetic and the trip record are the observable
reality here; a test that only checked return values could stay green over
a brake that never engaged.
"""

import json

import pytest
from click.testing import CliRunner

from ticli import agent as agent_mod
from ticli import ipc
from ticli.cli import cli
from ticli.tests.agent_harness import Harness
from ticli.tests.fakes import FakeResponse, FakeTidal
from ticli.utils import throttle


# ---------------------------------------------------------------------------
# Fakes


class FakeArtist:
    def __init__(self, name):
        self.name = name
        self.id = abs(hash(name)) % 10**6


class FakeAlbum:
    def __init__(self, name):
        self.name = name


class FakeTrack:
    def __init__(self, id, name, artists, album="", duration=240):
        self.id = id
        self.name = name
        self.artists = [FakeArtist(a) for a in artists]
        self.album = FakeAlbum(album)
        self.duration = duration
        self.explicit = False


@pytest.fixture(autouse=True)
def spawned(monkeypatch):
    """No test starts a real player: it would use the owner's real session."""
    calls = []

    def spawn(*a, **kw):
        calls.append(a)
        return "error: no player in tests"
    monkeypatch.setattr(ipc, "spawn_player", spawn)
    return calls


@pytest.fixture
def player():
    made = []

    def _make(**kw):
        made.append(Harness(**kw))
        return made[-1]

    yield _make
    for h in made:
        h.stop()


def agent(*args, **kw):
    result = CliRunner().invoke(cli, ["agent", *args], **kw)
    return result, json.loads(result.output)


@pytest.fixture
def stored_tokens(monkeypatch):
    """A stored session exists, and nothing touches the real keychain."""
    record = {"token_type": "Bearer", "access_token": "tok",
              "refresh_token": "ref", "expiry_time": None,
              "is_pkce": True, "version": 2}
    monkeypatch.setattr(agent_mod, "load_tokens", lambda: dict(record))
    monkeypatch.setattr(agent_mod, "save_tokens", lambda data: None)
    return record


# ---------------------------------------------------------------------------
# Throttle


class TestThrottle:
    def test_two_acquires_are_spaced_by_the_interval(self):
        clock = [1000.0]
        sleeps = []
        throttle.acquire(now=lambda: clock[0], sleep=sleeps.append)
        assert sleeps == []  # first caller goes immediately
        throttle.acquire(now=lambda: clock[0], sleep=sleeps.append)
        assert sleeps == [pytest.approx(throttle.MIN_INTERVAL_SECONDS)]
        # The reservation survives on disk, where the next process finds it
        state = json.loads(throttle._throttle_path().read_text())
        assert state["next_free_at"] == pytest.approx(
            1000.0 + 2 * throttle.MIN_INTERVAL_SECONDS)

    def test_a_late_caller_is_not_made_to_wait(self):
        clock = [1000.0]
        sleeps = []
        throttle.acquire(now=lambda: clock[0], sleep=sleeps.append)
        clock[0] += 60  # a minute later: the reservation has long expired
        throttle.acquire(now=lambda: clock[0], sleep=sleeps.append)
        assert sleeps == []

    def test_trip_is_written_and_blocks_and_first_trip_wins(self):
        first = throttle.trip("http_429", detail="original evidence", now=lambda: 1.0)
        second = throttle.trip("substatus_4006", detail="later blur", now=lambda: 2.0)
        assert second == first  # the racing later symptom must not overwrite
        on_disk = json.loads(throttle._throttle_path().read_text())["tripped"]
        assert on_disk["reason"] == "http_429"
        with pytest.raises(throttle.Tripped):
            throttle.acquire(now=lambda: 3.0, sleep=lambda s: None)

    def test_unblock_clears_the_trip_on_disk(self):
        throttle.trip("http_429")
        assert throttle.unblock() is True
        assert json.loads(throttle._throttle_path().read_text())["tripped"] is None
        throttle.acquire(now=lambda: 0.0, sleep=lambda s: None)  # flows again
        assert throttle.unblock() is False  # honest about a no-op

    def test_player_running_sees_the_players_real_lock(self):
        """Kept in step by a test rather than an import (the cli.py
        QUALITY_NAMES precedent): throttle.py must not import player's
        chain, so `_player_running` rebuilds the lock path itself. This
        takes the lock through player's own `_take_instance_lock` and
        asserts the agent surface sees it — if either side's directory or
        filename drifts, the probe goes blind and this fails."""
        import os
        from ticli import player as player_mod

        assert agent_mod._player_running() is False
        fd, other = player_mod._take_instance_lock()
        assert other is None
        try:
            assert agent_mod._player_running() is True
        finally:
            os.close(fd)  # closing is what drops a flock
        assert agent_mod._player_running() is False


# ---------------------------------------------------------------------------
# The request path is throttled


class TestEveryRequestIsThrottled:
    def test_search_goes_through_the_players_queue(self, player):
        h = player(session=FakeTidal(search_tracks=[FakeTrack(1, "Baby", ["Four Tet"])]))
        result, out = agent("search", "four tet baby")
        assert result.exit_code == 0 and out["tracks"][0]["title"] == "Baby"
        assert out["cost"]["requests"] == 1 and h.session.requests == ["GET search"]
        assert throttle.next_free_at() == h.clock() + throttle.MIN_INTERVAL_SECONDS

    def test_a_429_trips_the_stop_and_reports_structured(self, player):
        h = player()
        h.session.request_session.answers = [FakeResponse(429)]
        result, out = agent("search", "q")
        assert result.exit_code == 1
        assert out["ok"] is False and out["error"] == out["code"] == "rate_limited"
        assert "unblock" in out["hint"]
        # The trip is on disk, where the NEXT invocation (a new process) finds it.
        assert json.loads(throttle._throttle_path().read_text())["tripped"]["reason"] == "http_429"

    def test_tripped_state_fails_fast_without_a_request(self, player):
        h = player()
        throttle.trip("http_429")
        result, out = agent("search", "q")
        assert result.exit_code == 1 and out["error"] == "rate_limited"
        assert h.session.requests == []  # stopped means stopped


# ---------------------------------------------------------------------------
# Resolve


FOLAMOUR_FIELD = [
    FakeTrack(100, "The Journey", ["H.E.R."], album="H.E.R."),
    FakeTrack(176427254, "The Journey (feat. Zeke Manyika)",
              ["Folamour", "Zeke Manyika"], album="The Journey"),
    FakeTrack(204254674, "The Journey (feat. Zeke Manyika) (Alex Martyn Remix)",
              ["Folamour", "Zeke Manyika"], album="The Journey (Remixes)"),
]


class TestResolve:
    @pytest.fixture(autouse=True)
    def _player(self, player):
        self.player = player

    def _resolve(self, monkeypatch, capsys, tracks, artist, title):
        h = self.player(session=FakeTidal(search_tracks=tracks))
        _, out = agent("resolve", "--artist", artist, "--title", title)
        return out, h.session.requests

    def test_the_her_incident_cannot_recur(self, monkeypatch, stored_tokens, capsys):
        """Replays 2026-08-25 exactly: an exact-title wrong-artist hit, the
        right artist behind a feat. credit, and a remix. The old scorer's
        remix penalty (-3) rivaled its artist bonus (+4) and H.E.R. won.
        Artist is now a gate ahead of every score, so this ordering is
        structural, not a tuning accident."""
        out, calls = self._resolve(monkeypatch, capsys, FOLAMOUR_FIELD,
                                   "Folamour", "The Journey")
        assert len(calls) == 1  # resolve costs exactly one request
        assert out["best"]["id"] == 176427254
        assert out["best"]["artists"] == ["Folamour", "Zeke Manyika"]
        # feat. is a credit, not a version: the plain ask is confidently met
        assert out["confident"] is True
        # the remix is present, ranked below, and labeled for what it is
        remix = next(c for c in out["candidates"] if c["id"] == 204254674)
        assert remix["unrequested_qualifier"] is True
        # the wrong-artist exact title is last despite its exact title
        assert out["candidates"][-1]["id"] == 100

    def test_a_remix_still_resolves_when_it_is_all_there_is(self, monkeypatch, stored_tokens, capsys):
        out, _ = self._resolve(
            monkeypatch, capsys,
            [FakeTrack(1, "Laguna (Kessler Remix)", ["Facta"])],
            "Facta", "Laguna")
        assert out["best"]["id"] == 1  # served, not silently withheld
        assert out["confident"] is False  # but never called certain

    def test_asking_for_the_remix_is_not_penalized(self, monkeypatch, stored_tokens, capsys):
        out, _ = self._resolve(
            monkeypatch, capsys,
            [FakeTrack(1, "Laguna (Kessler Remix)", ["Facta"]),
             FakeTrack(2, "Laguna", ["Facta"])],
            "Facta", "Laguna (Kessler Remix)")
        assert out["best"]["id"] == 1
        assert out["confident"] is True

    def test_no_results_is_ok_false_free(self, monkeypatch, stored_tokens, capsys):
        out, _ = self._resolve(monkeypatch, capsys, [], "Nobody", "Nothing")
        assert out["ok"] is True
        assert out["best"] is None
        assert out["confident"] is False
        assert out["candidates"] == []


# ---------------------------------------------------------------------------
# The CLI contract


class TestCliContract:
    def test_plain_ticli_still_owns_the_default(self):
        """The group refactor must not change what bare `ticli` means: no
        subcommand -> the player runs. Asserted through --help text staying
        the player's, and the agent group being reachable."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Terminal music player" in result.output
        result = runner.invoke(cli, ["agent", "--help"])
        assert result.exit_code == 0
        assert "JSON" in result.output

    def test_status_costs_zero_requests_and_is_json(self, monkeypatch, stored_tokens):
        def no_network():
            raise AssertionError("status without --verify must not build a session")
        monkeypatch.setattr(agent_mod, "_session", no_network)
        runner = CliRunner()
        result = runner.invoke(cli, ["agent", "status"])
        assert result.exit_code == 0
        out = json.loads(result.output)
        assert out == {
            "ok": True,
            "ai_control": {"allow_ai_control": True, "allow_dangerous_commands": False,
                           "key_required": False},
            "session_stored": True,
            "flow": "pkce",
            "flac_capable": True,
            "player_running": False,
            "throttle": {
                "min_interval_seconds": throttle.MIN_INTERVAL_SECONDS,
                "tripped": None,
            },
        }

    def test_not_logged_in_is_a_structured_error(self, monkeypatch, spawned):
        monkeypatch.setattr(ipc, "spawn_player", lambda *a, **kw: spawned.append(a) or "login")
        result, out = agent("search", "anything")
        assert result.exit_code == 1
        assert out["ok"] is False and out["error"] == "not_logged_in"
        assert "log in" in out["hint"] and len(spawned) == 1

    def test_playlist_create_and_add_shapes(self, player):
        h = player()
        result, out = agent("playlist", "create", "Morning Uplift")
        assert result.exit_code == 0
        new_id = out["playlist"]["id"]
        assert out["playlist"] == {"id": new_id, "name": "Morning Uplift", "num_tracks": 0,
                                   "description": ""}
        assert h.session.playlists[new_id].name == "Morning Uplift"

        result, out = agent("playlist", "add", new_id, "11", "22")
        assert result.exit_code == 0
        assert {k: out[k] for k in ("ok", "playlist_id", "requested", "queued")} == {
            "ok": True, "playlist_id": new_id, "requested": 2, "queued": 1}
        h.idle()
        assert h.session.playlists[new_id].adds == [["11", "22"]]  # ids reach the API as strings

    def test_every_registry_command_is_a_verb(self):
        from ticli.cli import COVERED
        from ticli.commands import COMMANDS
        result = CliRunner().invoke(cli, ["agent", "--help"])
        for name in set(COMMANDS) - COVERED:
            assert name.replace(".", " ") in result.output, name

    def test_a_generated_verb_runs_in_the_player(self, player):
        h = player()
        result, out = agent("queue", "remove", "2")
        assert result.exit_code == 0 and out["result"] == {"queue_length": 2}
        assert [t.id for t in h.core._queue] == [1, 2]
        result, out = agent("queue", "remove", "index=0")  # the playing entry: plays the next
        h.idle()
        assert out["ok"] and [t.id for t in h.core._queue] == [2]
        result, out = agent("queue", "remove", "x", "y")
        assert result.exit_code == 1 and out["code"] == "bad_args"

    def test_do_reads_a_batch_from_stdin_and_merges_adds(self, player):
        h = player()
        batch = json.dumps(["playlist add road 1",
                            {"cmd": "playlist.add", "args": {"id": "road", "track_ids": [2]}},
                            "queue list"])
        result, out = agent("do", input=batch)
        assert result.exit_code == 0 and out["ok"]
        adds, merged, listed = out["result"]
        assert adds["job"] == merged["job"] and merged["merged"].startswith("2 adds")
        assert len(listed["result"]["tracks"]) == 3
        h.idle()
        assert h.road.adds == [["1", "2"]]
        assert h.session.requests == ["POST playlists/road/items", "GET playlists/road"]

    def test_status_lists_what_the_running_player_has_queued(self, player, monkeypatch):
        h = player()
        monkeypatch.setattr(agent_mod, "_player_running", lambda: True)
        h.hold()
        agent("playlist", "add", "road", "1")
        agent("playlist", "add", "road", "2")
        _, out = agent("status")
        assert out["pending"] == [{"job": 1, "cmd": "playlist.add", "eta_s": 4.0, "merged": 2}]
        assert out["state"]["pending"] == 1 and "status" in out["next"]
        h.release()
        _, out = agent("status")
        assert out["pending"] == [] and out["done"][0]["ok"] is True

    def test_a_typical_reply_stays_small(self, player):
        player()
        for args in (("pause",), ("playlist", "add", "road", "1"), ("next",)):
            result = CliRunner().invoke(cli, ["agent", *args])
            assert result.exit_code == 0 and len(result.output) < 450, result.output

    def test_unblock_without_a_terminal_is_refused_and_keeps_the_trip(self, stored_tokens):
        throttle.trip("http_429")
        result = CliRunner().invoke(cli, ["agent", "unblock"])  # CliRunner's stdin is no TTY
        payload = json.loads(result.output)
        assert result.exit_code == 1 and payload["error"] == "human_only"
        assert "ask your human to run `ticli agent unblock` in a terminal" in payload["hint"]
        assert json.loads(throttle._throttle_path().read_text())["tripped"] is not None

    def test_unblock_via_cli_clears_a_real_trip(self, monkeypatch, stored_tokens):
        from ticli import commands
        monkeypatch.setattr(commands, "cli_caller", lambda stdin=None: commands.HUMAN)
        throttle.trip("http_429")
        runner = CliRunner()
        result = runner.invoke(cli, ["agent", "unblock"])
        assert result.exit_code == 0
        assert json.loads(result.output)["was_tripped"] is True
        assert json.loads(throttle._throttle_path().read_text())["tripped"] is None


# ---------------------------------------------------------------------------
# Docs


class TestAgentDocs:
    def _docs(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["agent", "docs"])
        assert result.exit_code == 0
        return result.output

    def test_every_verb_that_exists_is_documented(self):
        """Walks the real click group rather than a hand-kept list, so a verb
        added without documentation fails here — the docs cannot silently
        fall behind the surface. Nested groups (playlist) are walked too."""
        docs = self._docs()
        agent_group = cli.commands["agent"]

        def walk(group, prefix):
            for name, command in group.commands.items():
                full = f"{prefix} {name}"
                if hasattr(command, "commands"):
                    walk(command, full)
                else:
                    assert full in docs, f"undocumented verb: {full}"

        walk(agent_group, "ticli agent")

    def test_docs_carry_the_load_bearing_rules(self):
        """Not full prose assertions — the phrases an agent's behaviour
        hinges on: the trip procedure, the batching rule, what is not yet
        possible, and the sanctioned-path rule."""
        docs = self._docs()
        assert "stop and report to the human" in docs   # the trip procedure
        assert "batch them, never add in a loop" in docs
        assert "not in this surface yet" in docs         # playback honesty
        assert "the only sanctioned path" in docs
        assert "Human-only" in docs                      # unblock ownership

    def test_docs_is_prose_and_says_so_in_agent_help(self):
        """docs is the one non-JSON verb; the group help must carry the
        exception so the JSON contract stays honest."""
        docs = self._docs()
        with pytest.raises(json.JSONDecodeError):
            json.loads(docs)
        runner = CliRunner()
        result = runner.invoke(cli, ["agent", "--help"])
        assert "docs excepted" in result.output

    def test_top_level_help_points_agents_at_docs(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "ticli agent docs" in result.output


# ---------------------------------------------------------------------------
# Error classification (from the 2026-08-25 Opus audit: api_error broke the
# "each carries a hint" promise, and 404s masqueraded as outages)


class TestErrorClassification:
    def test_a_404_is_not_found_not_an_outage(self, player):
        player()
        result, out = agent("playlist", "show", "nope")
        assert result.exit_code == 1 and out["error"] == "not_found"
        assert "playlist list" in out["hint"]  # says where real ids come from

    def test_other_failures_are_api_error_with_a_hint(self, player):
        h = player()
        h.session.request_session.answers = [FakeResponse(500)]
        result, out = agent("playlist", "show", "road")
        assert result.exit_code == 1 and out["error"] == "api_error"
        assert out["hint"]  # the docs promise every code carries one


class TestDocsGapFixes:
    """The phrases added after the fresh-agent simulations — each pins a
    branch a cold agent hit and the docs left undefined."""

    def test_the_new_rules_are_in_the_docs(self):
        runner = CliRunner()
        docs = runner.invoke(cli, ["agent", "docs"]).output
        assert "never auto-create" in docs           # sim 1: missing playlist
        assert "no destructive verb" in docs         # sim 5: delete request
        assert "from your own tally" in docs         # sim 7: trip mid-task
        assert "it proves the login, not the audio" in docs  # sim 4: --verify
        assert "not_found" in docs                   # audit: error codes
        assert "Also not here yet" in docs           # audit: browse/settings


class TestPermissions:
    """`ticli agent` obeys the human's switches before it spends a request."""

    @pytest.fixture
    def no_player(self, monkeypatch, spawned):
        connects = []
        monkeypatch.setattr(ipc, "connect", lambda *a: connects.append(a))
        return spawned, connects

    def _settings(self, **values):
        from ticli.utils import config as config_mod
        config_mod.save_config({**config_mod.DEFAULTS, **values})

    def test_ai_control_off_refuses_actions_without_starting_the_player(self, no_player):
        self._settings(allow_ai_control=False)
        for args in (("playlist", "add", "road", "1"), ("pause",), ("do", '["pause"]')):
            result, out = agent(*args)
            refusal = out["result"][0] if args[0] == "do" else out
            assert result.exit_code == 1 and refusal["code"] == "ai_control_off", args
            assert "never edit config.json" in refusal["fix"]
        assert no_player == ([], [])

    def test_ai_control_off_answers_reads_from_disk_without_the_player(self, no_player):
        self._settings(allow_ai_control=False)
        for args in (("playlist", "list"), ("queue", "list"), ("search", "x"),
                     ("settings", "get"), ("do", '["queue list", "download list"]')):
            result, out = agent(*args)
            assert result.exit_code == 0 and out["ok"], args
        assert out["result"][0]["result"]["source"] == "disk"
        assert no_player == ([], [])

    def test_a_failing_disk_read_is_one_json_error(self, no_player, monkeypatch):
        from ticli.utils.cache import MetadataCache

        def boom(self):
            raise RuntimeError("corrupt")
        monkeypatch.setattr(MetadataCache, "get_playlists", boom)
        self._settings(allow_ai_control=False)
        result, payload = agent("playlist", "list")
        assert result.exit_code == 1 and payload["ok"] is False
        assert payload["error"] == "local_read_failed" and payload["hint"]

    def test_a_set_key_is_required(self, no_player):
        from ticli.utils.config import hash_ai_key
        self._settings(ai_control_key=hash_ai_key("open sesame"))
        _, missing = agent("playlist", "list")
        assert missing["error"] == "key_required"
        assert no_player == ([], [])

    def test_the_key_comes_from_the_environment_or_the_flag(self, player):
        from ticli.utils.config import hash_ai_key
        self._settings(ai_control_key=hash_ai_key("open sesame"))
        h = player()
        by_env, out_env = agent("playlist", "list", env={"TICLI_AI_KEY": "open sesame"})
        _, out_flag = agent("--key", "open sesame", "queue", "list")
        assert out_env["ok"] and out_flag["ok"]
        assert [p["id"] for p in out_env["playlists"]] == ["road", "gym"]
        h.core.commands._sleep = lambda s: None
        _, wrong = agent("pause", env={"TICLI_AI_KEY": "nope"})
        assert wrong["code"] == "wrong_key"

    def test_status_is_never_gated_and_reports_the_switches(self, stored_tokens):
        from ticli.utils.config import hash_ai_key
        self._settings(allow_ai_control=False, ai_control_key=hash_ai_key("k"))
        payload = json.loads(CliRunner().invoke(cli, ["agent", "status"]).output)
        assert payload["ok"]
        assert payload["ai_control"] == {"allow_ai_control": False,
                                         "allow_dangerous_commands": False,
                                         "key_required": True}

    def test_docs_state_the_honour_system(self):
        from ticli.agent_docs import DOCS
        for phrase in ("Only the\nhuman can change them", "never edit `config.json`",
                       "TICLI_AI_KEY", "ask\nyour human", "dangerous_off", "key_required"):
            assert phrase in DOCS, phrase
