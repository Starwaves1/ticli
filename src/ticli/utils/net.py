import requests

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


def is_transport_failure(exc) -> bool:
    # ReadTimeout is deliberately not here: TIDAL answering slowly is not TIDAL being unreachable.
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, requests.exceptions.ConnectionError):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False
