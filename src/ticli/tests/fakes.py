"""Shared stand-ins for the network, so every test fakes what production calls.

There is one of these because there is one thing to get wrong: `fetch_to_file`
opens a `requests.Session` per download (46 segments of a hi-res track are 46
requests to the same host, and one connection instead of 46 is a measured
2.3 s and 45 TLS handshakes). A test that only replaces `requests.get` is
faking a function production no longer calls on that path, and stays green
over code it never exercises — so `patch_get` replaces both, together, and
nothing has to
remember.
"""


class FakeSession:
    """A `requests.Session` whose `get` is whatever the test supplied."""

    def __init__(self, get):
        self.get = get
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self.closed = True


def patch_get(monkeypatch, module, get):
    """Point both `requests.get` and `requests.Session().get` at `get`.

    Returns the list of sessions handed out, so a test can assert that an
    abandoned download closed its connection pool rather than leaking it.
    """
    sessions = []

    def _session():
        made = FakeSession(get)
        sessions.append(made)
        return made

    monkeypatch.setattr(module.requests, "get", get)
    monkeypatch.setattr(module.requests, "Session", _session)
    return sessions


# ── TIDAL as the player's agent queue sees it ──
#
# Every call goes through `request_session.request`, the one place tidalapi
# reaches the network, so the queue's pacing and counting hook sees exactly
# what production would: a Playlist.add is a POST plus its reparse GET.


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.ok = status_code < 400
        self._body = body or {}

    def json(self):
        return self._body


class FakeHTTPError(Exception):
    def __init__(self, response):
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.answers = []  # FakeResponses to give, in order, before the default 200s

    def request(self, method, url, **kwargs):
        self.calls.append(f"{method} {url}")
        return self.answers.pop(0) if self.answers else FakeResponse()


def _paged(items, limit, offset):
    """TIDAL's default page when no limit is asked for is not "everything"."""
    return list(items)[offset:offset + (limit or 10)]


def fake_track(tid, name=None, artists=None, duration=200):
    import types
    return types.SimpleNamespace(
        id=tid, name=name or f"Track {tid}", duration=duration, explicit=False,
        artists=[types.SimpleNamespace(name=a) for a in (artists or [f"Artist {tid}"])],
        album=types.SimpleNamespace(name=f"Album {tid}", cover=None))


class FakePlaylist:
    def __init__(self, session, pid, name, tracks=()):
        self.session, self.id, self.name = session, pid, name
        self.items = list(tracks)
        self.adds = []
        self.description = ""
        self.edits = []

    @property
    def num_tracks(self):
        return len(self.items)

    def add(self, ids):
        self.session.http("POST", f"playlists/{self.id}/items")
        self.session.http("GET", f"playlists/{self.id}")
        self.adds.append(list(ids))
        self.items += [fake_track(t) for t in ids]
        return list(ids)

    def tracks(self, limit=None, offset=0):
        self.session.http("GET", f"playlists/{self.id}/items")
        return _paged(self.items, limit, offset)

    def delete(self):
        self.session.http("DELETE", f"playlists/{self.id}")
        self.session.playlists.pop(self.id, None)
        return True

    def edit(self, title=None, description=None):
        self.session.http("POST", f"playlists/{self.id}")
        self.edits.append((title, description))
        return True


class FakeFavorites:
    def __init__(self, session):
        self.session = session
        self.albums, self.artists, self.playlists = set(), set(), set()

    def add_track(self, ids):
        self.session.http("POST", "favorites/tracks")

    def remove_track(self, tid):
        self.session.http("DELETE", f"favorites/tracks/{tid}")


def _favorite_methods():
    def make(kind, on):
        store = f"{kind}s"

        def method(self, obj_id):
            self.session.http("POST" if on else "DELETE", f"favorites/{store}/{obj_id}")
            (getattr(self, store).add if on else getattr(self, store).discard)(str(obj_id))
        return method

    for kind in ("album", "artist", "playlist"):
        setattr(FakeFavorites, f"add_{kind}", make(kind, True))
        setattr(FakeFavorites, f"remove_{kind}", make(kind, False))


_favorite_methods()


class FakeUser:
    def __init__(self, session):
        self.session = session
        self.favorites = FakeFavorites(session)

    def playlists(self):
        self.session.http("GET", "users/playlists")
        return list(self.session.playlists.values())

    def create_playlist(self, name, description):
        self.session.http("POST", "users/playlists")
        playlist = FakePlaylist(self.session, f"00000000-0000-4000-8000-{len(self.session.playlists):012d}", name)
        playlist.description = description
        self.session.playlists[playlist.id] = playlist
        return playlist


class FakeTidal:
    """A tidalapi Session stand-in whose requests can be counted and failed."""

    is_pkce = True

    def __init__(self, search_tracks=(), search_playlists=(), search_albums=(), search_artists=()):
        self.request_session = FakeHTTP()
        self.search_tracks = list(search_tracks)
        self.search_playlists = list(search_playlists)
        self.search_albums = list(search_albums)
        self.search_artists = list(search_artists)
        self.album_tracks = {}
        self.playlists = {}
        self.user = FakeUser(self)

    @property
    def requests(self):
        return self.request_session.calls

    def load_oauth_session(self, token_type, access_token, refresh_token=None, expiry_time=None,
                           is_pkce=False):
        self.access_token = access_token
        self.http("GET", "sessions")
        return True

    def http(self, method, path):
        response = self.request_session.request(method, path)
        if response.status_code >= 400:
            raise FakeHTTPError(response)
        return response

    def add_playlist(self, pid, name, tracks=()):
        self.playlists[pid] = FakePlaylist(self, pid, name, tracks)
        return self.playlists[pid]

    def search(self, query, models=None, limit=50, offset=0):
        self.http("GET", "search")
        return {"tracks": self.search_tracks[:limit], "albums": self.search_albums[:limit],
                "artists": self.search_artists[:limit], "playlists": self.search_playlists[:limit]}

    def playlist(self, pid):
        self.http("GET", f"playlists/{pid}")
        if pid not in self.playlists:
            raise FakeHTTPError(FakeResponse(404))
        return self.playlists[pid]

    def album(self, aid):
        import types
        self.http("GET", f"albums/{aid}")
        tracks = self.album_tracks.get(str(aid), [])
        return types.SimpleNamespace(id=aid, name=f"Album {aid}", num_tracks=len(tracks),
                                     tracks=lambda limit=None, offset=0: _paged(tracks, limit, offset))

    def track(self, tid):
        self.http("GET", f"tracks/{tid}")
        return fake_track(tid)


def fake_playlist(pid, name, num_tracks=3):
    import types
    return types.SimpleNamespace(id=pid, name=name, num_tracks=num_tracks, creator=None,
                                 description="")


def fake_album(aid, name, artist="Artist", tracks=()):
    import types
    return types.SimpleNamespace(id=aid, name=name, num_tracks=10, year=2020, cover=None, tracks=lambda: list(tracks),
                                 artist=types.SimpleNamespace(name=artist),
                                 artists=[types.SimpleNamespace(name=artist)])


class FakeClock:
    """time.time + time.sleep for the queue: sleeping moves time, never waits,
    unless `hold` is set, which parks the sleeper until it is released."""

    def __init__(self, start=1_000_000.0):
        import threading
        self.t = start
        self.hold = None
        self.sleeps = []
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.t

    def sleep(self, seconds):
        if self.hold is not None:
            self.hold.wait(5)
        with self._lock:
            self.sleeps.append(round(seconds, 3))
            self.t += max(0.0, seconds)
