import requests
from urllib3.exceptions import ReadTimeoutError

API_TIMEOUT = (5, 30)


class TimeoutSession(requests.Session):
    on_transport_failure = None  # the player's: any request that can't reach TIDAL means offline
    refresh_status = None  # tidalapi raises one AuthenticationError for every failed refresh, 5xx included

    def request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = API_TIMEOUT
        try:
            response = super().request(method, url, **kwargs)
        except requests.exceptions.ConnectionError as e:
            if self.on_transport_failure is not None and is_transport_failure(e):
                self.on_transport_failure()
            raise
        data = kwargs.get("data")
        if isinstance(data, dict) and data.get("grant_type") == "refresh_token":
            self.refresh_status = response.status_code
        return response


def tidal_session():
    from ticli.utils.testhooks import session_factory

    factory = session_factory()
    if factory is not None:
        return factory()
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
