import requests
from urllib3.exceptions import ReadTimeoutError

API_TIMEOUT = (5, 30)


class TimeoutSession(requests.Session):
    def request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = API_TIMEOUT
        return super().request(method, url, **kwargs)


def tidal_session():
    import tidalapi

    session = tidalapi.Session()
    # tidalapi passes no timeout, so a socket that accepts and never answers hangs forever; it reads
    # request_session on every call, login and token refresh included.
    session.request_session = TimeoutSession()
    return session


def _stalled_read(exc) -> bool:
    return any(isinstance(e, ReadTimeoutError) for e in (*exc.args, exc.__context__, exc.__cause__))


def is_transport_failure(exc) -> bool:
    # ReadTimeout is deliberately not here: TIDAL answering slowly is not TIDAL being unreachable.
    # requests re-raises a read timeout during iter_content as a plain ConnectionError, so a stalled CDN
    # segment has to be told apart from a dead network by what it wraps.
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, requests.exceptions.ConnectionError):
            return not _stalled_read(exc)
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False
