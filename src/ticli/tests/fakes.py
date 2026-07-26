"""Shared stand-ins for the network, so every test fakes what production calls.

There is one of these because there is one thing to get wrong: `fetch_to_file`
opens a `requests.Session` per download (46 segments of a hi-res track are 46
requests to the same host, and one connection instead of 46 is a measured
2.3 s and 45 TLS handshakes). A test that only replaces `requests.get` is
faking a function production no longer calls on that path, which is the shape
of INCIDENTS #2 — so `patch_get` replaces both, together, and nothing has to
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
