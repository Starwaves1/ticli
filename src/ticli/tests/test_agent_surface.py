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
from ticli.cli import cli
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


def fake_session_returning(tracks, calls):
    """A session whose search() answers with `tracks` and counts into `calls`."""

    class FakeSession:
        def search(self, query, models=None, limit=50, offset=0):
            calls.append(query)
            return {"tracks": list(tracks)}

    return FakeSession()


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
    def test_search_acquires_before_calling(self, monkeypatch, stored_tokens, capsys):
        order = []
        monkeypatch.setattr(throttle, "acquire", lambda **kw: order.append("acquire"))
        calls = []
        session = fake_session_returning([FakeTrack(1, "Baby", ["Four Tet"])], calls)
        monkeypatch.setattr(agent_mod, "_session", lambda: session)
        real_search = session.search
        session.search = lambda *a, **kw: (order.append("request"), real_search(*a, **kw))[1]
        agent_mod.search("four tet baby", ("track",), 10)
        assert order == ["acquire", "request"]

    def test_a_429_trips_the_stop_and_reports_structured(self, monkeypatch, stored_tokens, capsys):
        class FakeResponse:
            status_code = 429
            def json(self):
                return {}

        class Boom(Exception):
            response = FakeResponse()

        class FakeSession:
            def search(self, *a, **kw):
                raise Boom("too many requests")

        monkeypatch.setattr(agent_mod, "_session", lambda: FakeSession())
        with pytest.raises(SystemExit) as exc:
            agent_mod.search("q", ("track",), 10)
        assert exc.value.code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert out["error"] == "rate_limited"
        assert "unblock" in out["hint"]
        # The trip is on disk, where the NEXT invocation (a new process)
        # finds it — that persistence is the entire point
        assert json.loads(throttle._throttle_path().read_text())["tripped"]["reason"] == "http_429"

    def test_tripped_state_fails_fast_without_a_request(self, monkeypatch, stored_tokens, capsys):
        throttle.trip("http_429")
        requests_made = []
        session = fake_session_returning([], requests_made)
        monkeypatch.setattr(agent_mod, "_session", lambda: session)
        with pytest.raises(SystemExit):
            agent_mod.search("q", ("track",), 10)
        assert requests_made == []  # stopped means stopped


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
    def _resolve(self, monkeypatch, capsys, tracks, artist, title):
        calls = []
        monkeypatch.setattr(agent_mod, "_session",
                            lambda: fake_session_returning(tracks, calls))
        monkeypatch.setattr(throttle, "acquire", lambda **kw: None)
        agent_mod.resolve(artist, title, 10)
        return json.loads(capsys.readouterr().out), calls

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

    def test_not_logged_in_is_a_structured_error(self, monkeypatch):
        monkeypatch.setattr(agent_mod, "load_tokens", lambda: None)
        runner = CliRunner()
        result = runner.invoke(cli, ["agent", "search", "anything"])
        assert result.exit_code == 1
        out = json.loads(result.output)
        assert out["ok"] is False
        assert out["error"] == "not_logged_in"
        assert "hint" in out

    def test_playlist_create_and_add_shapes(self, monkeypatch, stored_tokens):
        created = []
        added = []

        class FakePlaylist:
            id = "pl-1"
            name = "Morning Uplift"
            num_tracks = 0
            description = ""
            def add(self, ids):
                added.extend(ids)
                return list(range(len(ids)))
            def tracks(self):
                return []

        class FakeUser:
            def create_playlist(self, name, description):
                created.append((name, description))
                return FakePlaylist()

        class FakeSession:
            user = FakeUser()
            def playlist(self, pid):
                assert pid == "pl-1"
                return FakePlaylist()

        monkeypatch.setattr(agent_mod, "_session", lambda: FakeSession())
        monkeypatch.setattr(throttle, "acquire", lambda **kw: None)
        runner = CliRunner()

        result = runner.invoke(cli, ["agent", "playlist", "create", "Morning Uplift"])
        assert result.exit_code == 0
        out = json.loads(result.output)
        assert out["playlist"] == {"id": "pl-1", "name": "Morning Uplift",
                                   "num_tracks": 0, "description": ""}
        assert created == [("Morning Uplift", "")]

        result = runner.invoke(cli, ["agent", "playlist", "add", "pl-1", "11", "22"])
        assert result.exit_code == 0
        out = json.loads(result.output)
        assert out == {"ok": True, "playlist_id": "pl-1", "requested": 2, "added": 2}
        assert added == ["11", "22"]  # ids reach the API as strings

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
    def _failing_session(self, status_code):
        class FakeResponse:
            def __init__(self, code):
                self.status_code = code
            def json(self):
                return {}

        class Boom(Exception):
            pass

        boom = Boom("kaput")
        boom.response = FakeResponse(status_code)

        class FakeSession:
            def playlist(self, pid):
                raise boom

        return FakeSession()

    def _invoke_show(self, monkeypatch, status_code):
        monkeypatch.setattr(agent_mod, "_session",
                            lambda: self._failing_session(status_code))
        monkeypatch.setattr(throttle, "acquire", lambda **kw: None)
        runner = CliRunner()
        result = runner.invoke(cli, ["agent", "playlist", "show", "nope"])
        assert result.exit_code == 1
        return json.loads(result.output)

    def test_a_404_is_not_found_not_an_outage(self, monkeypatch, stored_tokens):
        out = self._invoke_show(monkeypatch, 404)
        assert out["error"] == "not_found"
        assert "playlist list" in out["hint"]  # says where real ids come from

    def test_other_failures_are_api_error_with_a_hint(self, monkeypatch, stored_tokens):
        out = self._invoke_show(monkeypatch, 500)
        assert out["error"] == "api_error"
        assert "hint" in out  # the docs promise every code carries one


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
    def no_session(self, monkeypatch):
        opened = []
        monkeypatch.setattr(agent_mod, "_session", lambda: opened.append(1) or None)
        return opened

    def _settings(self, **values):
        from ticli.utils import config as config_mod
        config_mod.save_config({**config_mod.DEFAULTS, **values})

    def test_ai_control_off_refuses_tidal_verbs(self, stored_tokens, no_session):
        self._settings(allow_ai_control=False)
        result = CliRunner().invoke(cli, ["agent", "search", "x"])
        payload = json.loads(result.output)
        assert result.exit_code == 1 and payload["error"] == "ai_control_off"
        assert "never edit config.json" in payload["hint"]
        assert no_session == []

    def test_ai_control_off_answers_playlist_list_from_disk(self, stored_tokens, no_session):
        self._settings(allow_ai_control=False)
        result = CliRunner().invoke(cli, ["agent", "playlist", "list"])
        payload = json.loads(result.output)
        assert payload["ok"] and payload["source"] == "disk"
        assert no_session == []

    def test_a_set_key_is_required(self, stored_tokens, no_session):
        from ticli.utils.config import hash_ai_key
        self._settings(ai_control_key=hash_ai_key("open sesame"))
        missing = json.loads(CliRunner().invoke(cli, ["agent", "playlist", "list"]).output)
        assert missing["error"] == "key_required"
        assert no_session == []

    def test_the_key_comes_from_the_environment_or_the_flag(self, monkeypatch, stored_tokens):
        from ticli.utils.config import hash_ai_key
        self._settings(ai_control_key=hash_ai_key("open sesame"))

        class FakeUser:
            def playlists(self):
                return []

        class FakeSession:
            user = FakeUser()

        monkeypatch.setattr(agent_mod, "_session", lambda: FakeSession())
        monkeypatch.setattr(agent_mod, "_acquire", lambda: None)
        by_env = CliRunner().invoke(cli, ["agent", "playlist", "list"],
                                    env={"TICLI_AI_KEY": "open sesame"})
        by_flag = CliRunner().invoke(cli, ["agent", "--key", "open sesame", "playlist", "list"])
        assert json.loads(by_env.output)["ok"] and json.loads(by_flag.output)["ok"]

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
