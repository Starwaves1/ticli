"""queue.move, queue.clear, history.list, cache.status and track.info: local, zero requests."""

import json
import types

import pytest
from click.testing import CliRunner

from ticli import ipc
from ticli.cli import cli
from ticli.commands import AGENT, HUMAN, Commands, offline_read
from ticli.player import HeadlessTidalPlayer
from ticli.tests.agent_harness import Harness
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod
from ticli.utils import downloads


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", path)
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    return path


class _NoNetwork:
    is_pkce = False

    def __init__(self):
        self.asked = []

    def __getattr__(self, name):
        self.asked.append(name)
        raise AssertionError(f"request attempted: {name}")


def _track(tid):
    return types.SimpleNamespace(id=tid, name=f"Track {tid}", duration=200,
                                 artists=[types.SimpleNamespace(name="Artist", id=70 + tid)],
                                 album=types.SimpleNamespace(name="Album", id=90 + tid))


class _Audio:
    def stop(self):
        pass


def _player(**config):
    p = HeadlessTidalPlayer()
    p.config.update(config)
    p.session = _NoNetwork()
    p.audio = _Audio()
    p._queue = [_track(1), _track(2), _track(3)]
    p._queue_index = 1
    p._current_track = p._queue[1]
    p._plays = []
    p._play_track = lambda track, seek=0: p._plays.append(track.id)
    p._wake = lambda: None
    p.slept = []
    p.commands = Commands(p, sleep=p.slept.append)
    return p


def _agent(p, name, **args):
    return p.commands.execute(name, args, caller=AGENT, key=None)


def _human(p, name, **args):
    return p.commands.execute(name, args, caller=HUMAN)


class TestQueueMove:
    @pytest.mark.parametrize("index,to,order,cur", [
        (0, 2, [2, 3, 1], 0),  # before the current one, to after it
        (2, 0, [3, 1, 2], 2),  # after the current one, to before it
        (1, 2, [1, 3, 2], 2),  # the current one itself
        (1, 1, [1, 2, 3], 1),
        (0, 1, [2, 1, 3], 0),  # onto the current one
        (2, 1, [1, 3, 2], 2),
    ])
    def test_the_same_track_stays_current(self, index, to, order, cur):
        p = _player()
        playing = p._current_track
        result = _human(p, "queue.move", index=index, to=to)
        assert result["ok"] and result["result"] == {
            "index": to, "queue_index": cur, "queue_length": 3}
        assert [t.id for t in p._queue] == order
        assert p._queue[p._queue_index] is playing
        assert p._plays == []

    def test_a_move_drops_the_prefetch(self):
        p = _player()
        p._prefetch_id = 3
        _human(p, "queue.move", index=2, to=0)
        assert p._prefetch_id is None

    @pytest.mark.parametrize("to", [None, -1, 3, "1", 1.0, True])
    def test_a_bad_destination_is_refused(self, to):
        p = _player()
        args = {"index": 0} if to is None else {"index": 0, "to": to}
        result = _human(p, "queue.move", **args)
        assert result["ok"] is False and result["code"] == "bad_args"
        assert "0..2" in result["reason"]
        assert [t.id for t in p._queue] == [1, 2, 3]

    def test_a_stale_track_id_is_refused_with_the_queue(self):
        p = _player()
        result = _human(p, "queue.move", index=2, to=0, track_id=2)
        assert result["ok"] is False and result["code"] == "stale"
        assert result["queue"]["track_ids"] == [1, 2, 3]
        assert [t.id for t in p._queue] == [1, 2, 3]

    def test_an_agent_can_move_with_ai_control_on_and_it_toasts(self):
        p = _player()
        result = _agent(p, "queue.move", index=2, to=0)
        assert result["ok"] and result["result"]["index"] == 0
        assert p.session.asked == []
        assert p._toast == "agent: moved queue entry 3 to 1"


class TestQueueClear:
    def test_only_the_current_track_is_left(self):
        p = _player()
        playing = p._current_track
        result = _human(p, "queue.clear")
        assert result["result"] == {"removed": 2, "queue_length": 1}
        assert p._queue == [playing] and p._queue_index == 0 and p._plays == []

    def test_nothing_current_empties_the_queue(self):
        p = _player()
        p._current_track = None
        result = _human(p, "queue.clear")
        assert result["result"] == {"removed": 3, "queue_length": 0}
        assert p._queue == [] and p._queue_index == -1 and p._plays == []

    def test_a_current_track_outside_the_queue_is_kept(self):
        p = _player()
        p._queue_index = -1
        playing = p._current_track = _track(9)
        result = _human(p, "queue.clear")
        assert result["result"] == {"removed": 2, "queue_length": 1}
        assert p._queue == [playing] and p._queue_index == 0

    def test_an_agent_toast_counts_what_went(self):
        p = _player()
        assert _agent(p, "queue.clear")["ok"]
        assert p._toast == "agent: cleared the queue (2 tracks)"


class TestHistory:
    def test_list_returns_the_history(self):
        p = _player()
        p._search_history = ["a", "b"]
        result = _agent(p, "history.list")
        assert result["result"] == {"history": ["a", "b"]}
        assert result["result"]["history"] is not p._search_history

    def test_with_ai_control_off_an_agent_is_refused(self):
        p = _player(allow_ai_control=False)
        assert _agent(p, "history.list")["code"] == "ai_control_off"
        assert offline_read("history.list", {}, p.config)["code"] == "ai_control_off"


def _stock(tmp_path, monkeypatch):
    monkeypatch.setattr(downloads, "DOWNLOAD_ROOT", tmp_path / "Music")
    audio = cache_mod.audio_dir()
    audio.mkdir(parents=True)
    (audio / "5.m4a").write_bytes(b"x" * 100)
    (audio / "6.flac").write_bytes(b"x" * 50)
    rel = downloads.relative_path({"artist": "A", "album": "B", "title": "T", "track_num": 1}, ".flac")
    path = downloads.download_dir() / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(b"y" * 300)
    downloads.record(7, rel, "MAX", 300, granted="HI_RES_LOSSLESS")
    cache_mod.lists_dir().mkdir(parents=True)
    cache_mod.index_file().write_text("{}")
    (cache_mod.lists_dir() / "k.json").write_text("[1,2,3]")
    return path


class TestCacheStatus:
    def test_figures(self, tmp_path, monkeypatch):
        _stock(tmp_path, monkeypatch)
        p = _player(cache_budget_gb=3)
        p._cache = cache_mod.MetadataCache(songs=True, budget_gb=3)
        out = _agent(p, "cache.status")["result"]
        assert out["songs"] == {"count": 2, "bytes": 150, "budget_bytes": 3 * cache_mod.BYTES_PER_GB,
                                "enabled": True}
        assert out["downloads"]["count"] == 1 and out["downloads"]["bytes"] == 300
        assert out["downloads"]["dir"] == str(downloads.download_dir())
        assert out["metadata"]["bytes"] >= 2 + 7
        assert out["metadata"]["cap_bytes"] == cache_mod.METADATA_CAP_BYTES
        assert out["metadata"]["enabled"] is True
        assert p.session.asked == []

    def test_disk_twin_serves_agents_with_ai_control_off(self, tmp_path, monkeypatch):
        _stock(tmp_path, monkeypatch)
        cfg = config_mod.load_config()
        cfg.update(allow_ai_control=False, cache_songs=False, cache_budget_gb=1)
        reply = offline_read("cache.status", {}, cfg)
        assert reply["ok"]
        out = reply["result"]
        assert out["source"] == "disk"
        assert out["songs"]["count"] == 2 and out["songs"]["enabled"] is False
        assert out["songs"]["budget_bytes"] == cache_mod.BYTES_PER_GB
        assert out["downloads"]["count"] == 1

        p = _player(allow_ai_control=False)
        again = _agent(p, "cache.status")
        assert again["ok"] and p.session.asked == []

    def test_empty_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(downloads, "DOWNLOAD_ROOT", tmp_path / "Music")
        out = offline_read("cache.status", {}, config_mod.load_config())["result"]
        assert out["songs"]["count"] == 0 and out["songs"]["bytes"] == 0
        assert out["downloads"]["count"] == 0 and out["metadata"]["bytes"] == 0


class TestTrackInfo:
    def test_a_queued_track_answers_locally(self):
        p = _player()
        p._liked_ids = {"2"}
        out = _agent(p, "track.info", track_id=2)["result"]
        assert out["source"] == "local" and p.session.asked == []
        assert out["track"]["title"] == "Track 2"
        assert out["track"]["album_id"] == 92 and out["track"]["artist_ids"] == [72]
        assert out["track"]["explicit"] is False
        assert out["quality"] is None and out["liked"] is True
        assert out["downloaded"] is None and out["cached"] is None
        assert _agent(p, "track.info", track_id=1)["result"]["liked"] is False

    def test_downloaded_and_cached_copies_are_reported(self, tmp_path, monkeypatch):
        path = _stock(tmp_path, monkeypatch)
        p = _player()
        p._cache = cache_mod.MetadataCache()
        p._queue.append(_track(7))
        p._queue.append(_track(5))
        p._cache.note_cached(5, ".m4a", 100, quality="HIGH")
        out = _agent(p, "track.info", track_id=7)["result"]
        assert out["downloaded"] == {"tier": "MAX", "bytes": 300, "path": str(path)}
        assert out["cached"] is None
        cached = _agent(p, "track.info", track_id=5)["result"]["cached"]
        assert cached == {"tier": "MEDIUM", "bytes": 100}

    def test_an_unknown_id_is_fetched_once(self):
        p = _player()
        found = _track(42)
        found.audio_quality = "HIGH"
        asked = []
        p.session = types.SimpleNamespace(is_pkce=False, track=lambda tid: asked.append(tid) or found)
        out = _agent(p, "track.info", track_id=42)["result"]
        assert out["source"] == "tidal" and asked == [42]
        assert out["track"]["title"] == "Track 42" and out["quality"] == "MEDIUM"
        assert p._known[("track", "42")] is found

    def test_an_unknown_id_while_offline_is_refused(self):
        p = _player()
        p._connectivity = "offline"
        p._reconnect = lambda *a, **k: "offline"
        result = _agent(p, "track.info", track_id=42)
        assert result["ok"] is False and result["code"] == "offline"
        assert p.session.asked == []

    @pytest.mark.parametrize("args", [{}, {"track_id": ""}])
    def test_a_missing_id_is_refused(self, args):
        result = _agent(_player(), "track.info", **args)
        assert result["ok"] is False and result["code"] == "bad_args"


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr(ipc, "spawn_player", lambda *a, **kw: "error: no player in tests")
    made = []

    def _make():
        made.append(Harness())
        return made[-1]

    yield _make
    for h in made:
        h.stop()


def _cli(*args):
    result = CliRunner().invoke(cli, ["agent", *args])
    return result, json.loads(result.output)


class TestAgentCli:
    def test_the_local_verbs_cost_nothing(self, harness):
        h = harness()
        result, out = _cli("queue", "move", "2", "0")
        assert result.exit_code == 0 and out["ok"]
        assert out["result"] == {"index": 0, "queue_index": 1, "queue_length": 3}
        assert [t.id for t in h.core._queue] == [3, 1, 2]
        assert out["cost"]["requests"] == 0
        assert h.core._toast == "agent: moved queue entry 3 to 1"

        result, out = _cli("track", "info", "1")
        assert result.exit_code == 0 and out["ok"]
        assert out["result"]["source"] == "local" and out["result"]["track"]["id"] == 1
        assert out["cost"]["requests"] == 0

        result, out = _cli("cache", "status")
        assert out["ok"] and set(out["result"]) == {"songs", "downloads", "metadata"}
        assert out["cost"]["requests"] == 0

        h.core._search_history = ["jazz"]
        result, out = _cli("history", "list")
        assert out["ok"] and out["result"] == {"history": ["jazz"]}
        assert out["cost"]["requests"] == 0

        result, out = _cli("queue", "clear")
        assert out["ok"] and out["result"] == {"removed": 2, "queue_length": 1}
        assert out["cost"]["requests"] == 0
        assert len(h.core._queue) == 1
