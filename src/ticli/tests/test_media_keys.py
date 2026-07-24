"""Tests for macOS media-key support.

Uses a fake mpv JSON IPC socket server — no real mpv process required.
"""

import json
import os
import socket
import tempfile
import threading

import pytest

from ticli import player

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="mpv IPC uses Unix sockets"
)


class FakeMpv:
    """Minimal stand-in for mpv's JSON IPC socket."""

    def __init__(self, path):
        self.path = path
        self.props = {}
        self.keybinds = {}
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(path)
        self._server.listen(8)
        self._running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def press(self, key):
        """Simulate a media key press by running its bound command."""
        cmd = self.keybinds[key].split()
        assert cmd[0] == "set"
        self.props[cmd[1]] = cmd[2]

    def _serve(self):
        while self._running:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    req = json.loads(line)
                    # mpv interleaves async events with replies
                    conn.sendall(b'{"event":"property-change"}\n')
                    conn.sendall((json.dumps(self._reply(req)) + "\n").encode())
        except OSError:
            pass
        finally:
            conn.close()

    def _reply(self, req):
        cmd = req["command"]
        rid = req.get("request_id")
        if cmd[0] == "keybind":
            self.keybinds[cmd[1]] = cmd[2]
            return {"error": "success", "data": None, "request_id": rid}
        if cmd[0] == "set_property":
            self.props[cmd[1]] = cmd[2]
            return {"error": "success", "request_id": rid}
        if cmd[0] == "get_property":
            if cmd[1] not in self.props:
                return {"error": "property not found", "request_id": rid}
            return {"error": "success", "data": self.props[cmd[1]], "request_id": rid}
        return {"error": "invalid parameter", "request_id": rid}

    def close(self):
        self._running = False
        self._server.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


@pytest.fixture
def mpv(monkeypatch):
    monkeypatch.setattr(player, "IS_MACOS", True)
    tmpdir = tempfile.mkdtemp(dir="/tmp")  # keep socket path under the sun_path limit
    fake = FakeMpv(os.path.join(tmpdir, "mpv.sock"))
    audio = player.AudioPlayer("mpv")
    audio._ipc_path = fake.path
    yield fake, audio
    fake.close()


class TestMediaKeyBinding:
    """mpv key bindings installed over IPC."""

    def test_binds_every_media_key(self, mpv):
        fake, audio = mpv
        audio.poll_media_key()
        assert set(fake.keybinds) == set(player.MEDIA_KEY_ACTIONS)

    def test_bindings_set_the_polled_property(self, mpv):
        fake, audio = mpv
        audio.poll_media_key()
        for key, action in player.MEDIA_KEY_ACTIONS.items():
            assert fake.keybinds[key] == f"set {player.MEDIA_KEY_PROP} {action}"

    def test_binds_only_once_per_track(self, mpv):
        fake, audio = mpv
        audio.poll_media_key()
        fake.keybinds.clear()
        audio.poll_media_key()
        assert fake.keybinds == {}

    def test_play_url_forces_rebinding(self, mpv):
        _, audio = mpv
        audio.poll_media_key()
        assert audio._media_keys_bound
        audio.stop()
        assert not audio._media_keys_bound

    def test_media_title_pushed_to_now_playing(self, mpv):
        fake, audio = mpv
        audio._media_title = "Blinding Lights — The Weeknd"
        audio.poll_media_key()
        assert fake.props["force-media-title"] == "Blinding Lights — The Weeknd"

    def test_no_media_title_without_one(self, mpv):
        fake, audio = mpv
        audio.poll_media_key()
        assert "force-media-title" not in fake.props


class TestMediaKeyPolling:
    """Reading pressed keys back out of mpv."""

    def test_idle_poll_returns_none(self, mpv):
        _, audio = mpv
        assert audio.poll_media_key() is None

    @pytest.mark.parametrize(
        "key,action",
        [("NEXT", "next"), ("PREV", "prev"), ("PLAY", "toggle"),
         ("PLAYONLY", "play"), ("PAUSEONLY", "pause"), ("STOP", "pause")],
    )
    def test_press_returns_action(self, mpv, key, action):
        fake, audio = mpv
        audio.poll_media_key()
        fake.press(key)
        assert audio.poll_media_key() == action

    def test_press_is_consumed_once(self, mpv):
        fake, audio = mpv
        audio.poll_media_key()
        fake.press("NEXT")
        assert audio.poll_media_key() == "next"
        assert audio.poll_media_key() is None

    def test_request_survives_async_events(self, mpv):
        fake, audio = mpv
        fake.props["pause"] = False
        reply = audio._mpv_request({"command": ["get_property", "pause"]})
        assert reply["request_id"] == 1
        assert reply["data"] is False

    def test_request_without_socket(self, mpv):
        _, audio = mpv
        audio._ipc_path = None
        assert audio._mpv_request({"command": ["get_property", "pause"]}) is None

    def test_request_on_dead_socket(self, mpv):
        fake, audio = mpv
        fake.close()
        assert audio._mpv_request({"command": ["get_property", "pause"]}) is None


class TestOtherPlatforms:
    """Nothing happens off macOS or off mpv."""

    def test_no_ipc_when_not_macos(self, mpv, monkeypatch):
        fake, audio = mpv
        monkeypatch.setattr(player, "IS_MACOS", False)
        assert audio.poll_media_key() is None
        assert fake.keybinds == {}

    def test_no_ipc_for_ffplay(self, mpv):
        fake, audio = mpv
        audio.player_cmd = "ffplay"
        assert audio.poll_media_key() is None
        assert fake.keybinds == {}

    def test_no_ipc_without_socket(self, mpv):
        fake, audio = mpv
        audio._ipc_path = None
        assert audio.poll_media_key() is None
        assert fake.keybinds == {}


class RecordingPlayer(player.HeadlessTidalPlayer):
    """HeadlessTidalPlayer with playback actions stubbed out (no TIDAL session)."""

    def __init__(self, playing):
        self._playing = playing
        self.calls = []

    def _next_track(self):
        self.calls.append("next")

    def _prev_track(self):
        self.calls.append("prev")

    def _toggle_play(self):
        self.calls.append("toggle")


class TestMediaKeyDispatch:
    """Actions map onto the player's existing controls."""

    @pytest.mark.parametrize(
        "action,playing,expected",
        [
            ("next", True, ["next"]),
            ("prev", True, ["prev"]),
            ("toggle", True, ["toggle"]),
            ("toggle", False, ["toggle"]),
            ("play", False, ["toggle"]),
            ("play", True, []),       # already playing
            ("pause", True, ["toggle"]),
            ("pause", False, []),     # already paused
            (None, True, []),
            ("bogus", True, []),
        ],
    )
    def test_dispatch(self, action, playing, expected):
        p = RecordingPlayer(playing)
        p._handle_media_key(action)
        assert p.calls == expected
