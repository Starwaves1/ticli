"""Hooks that let tests run a real `python -m ticli.playerd` against a fake TIDAL.

Off unless TICLI_TEST_HOOKS=1 is set as well, so a stray variable can never
swap a user's session or move their tokens:

- TICLI_TEST_SESSION="name": `tidal_session()` returns `ticli.tests.fake_tidal.<name>()`
  instead of a tidalapi session. Only that module: an environment variable
  must not be able to import and run arbitrary code in the player.
- Tokens skip the OS keyring and use the file under $HOME/.config/ticli: the
  macOS Keychain is not redirected by HOME.
- TICLI_TEST_CODE / TICLI_TEST_LEGACY fake a code fingerprint or an old player,
  for the handover tests.

`tests/fake_tidal.py` is the session the real-process tests use.
"""

import os

ENABLE = "TICLI_TEST_HOOKS"
SESSION = "TICLI_TEST_SESSION"


def enabled() -> bool:
    return os.environ.get(ENABLE) == "1"


def session_factory():
    name = os.environ.get(SESSION) if enabled() else None
    if not name:
        return None
    from ticli.tests import fake_tidal

    factory = getattr(fake_tidal, name, None)
    if name.startswith("_") or getattr(factory, "__module__", None) != fake_tidal.__name__:
        raise ValueError(f"{SESSION}={name!r} names no session factory in ticli.tests.fake_tidal")
    return factory


CODE = "TICLI_TEST_CODE"
LEGACY = "TICLI_TEST_LEGACY"


def code():
    """TICLI_TEST_CODE="id@at" stands in for this process's code fingerprint."""
    raw = os.environ.get(CODE) if enabled() else None
    if not raw:
        return None
    ident, _, at = raw.partition("@")
    return {"id": ident, "at": float(at or 0)}


def legacy():
    """TICLI_TEST_LEGACY="cmd,cmd": the player plays a build from before `hello`
    that also lacks these commands. None when unset."""
    raw = os.environ.get(LEGACY) if enabled() else None
    return None if raw is None else {c for c in raw.split(",") if c}
