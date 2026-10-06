"""A fake TIDAL session for a real `python -m ticli.playerd` (see utils/testhooks.py).

`session()` is the factory named by TICLI_TEST_SESSION. It also refuses every
non-Unix socket connection in that process, so nothing can reach the network.
Track 1 lasts 30 s, every other id 5 s; both stream a silent local WAV.
"""

import socket
import types
import wave
from pathlib import Path

DURATIONS = {1: 30}
RATE = 8000


def _no_network():
    connect = socket.socket.connect

    def _connect(self, address):
        if self.family != socket.AF_UNIX:
            raise OSError(f"fake TIDAL: network blocked ({address!r})")
        return connect(self, address)

    socket.socket.connect = _connect
    socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(
        OSError("fake TIDAL: network blocked"))


def _silence(seconds: int) -> str:
    path = Path.home() / "fake-tidal" / f"silence{seconds}.wav"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes(b"\0\0" * RATE * seconds)
    return str(path)


class _Named:
    def __init__(self, name):
        self.name = name


class FakeTrack:
    cached = False

    def __init__(self, tid):
        self.id = int(tid)
        self.name = f"Fake {self.id}"
        self.duration = DURATIONS.get(self.id, 5)
        self.artists = [_Named("Fake Artist")]
        self.artist = self.artists[0]
        self.album = types.SimpleNamespace(id=900 + self.id, name="Fake Album", cover=None)

    def get_stream(self):
        path = _silence(self.duration)
        manifest = types.SimpleNamespace(is_bts=True, get_urls=lambda: [path])
        return types.SimpleNamespace(audio_quality="LOSSLESS", get_stream_manifest=lambda: manifest)


class FakeSession:
    is_pkce = False
    audio_quality = None
    access_token = "fake"
    token_type = "Bearer"
    refresh_token = "fake"
    expiry_time = None

    def __init__(self):
        favorites = types.SimpleNamespace(tracks=lambda limit=999: [],
                                          add_track=lambda t: True, remove_track=lambda t: True)
        self.user = types.SimpleNamespace(id=1, first_name="Fake", last_name=None,
                                          favorites=favorites, playlists=lambda: [])

    def load_oauth_session(self, *args, **kwargs):
        return True

    def check_login(self):
        return True

    def track(self, tid):
        return FakeTrack(tid)

    def __getattr__(self, name):
        raise RuntimeError(f"fake TIDAL has no session.{name}")


def session():
    _no_network()
    return FakeSession()
