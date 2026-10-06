"""Hooks that let tests run a real `python -m ticli.playerd` against a fake TIDAL.

Off unless TICLI_TEST_HOOKS=1 is set as well, so a stray variable can never
swap a user's session or move their tokens:

- TICLI_TEST_SESSION="name": `tidal_session()` returns `ticli.tests.fake_tidal.<name>()`
  instead of a tidalapi session. Only that module: an environment variable
  must not be able to import and run arbitrary code in the player.
- Tokens skip the OS keyring and use the file under $HOME/.config/ticli: the
  macOS Keychain is not redirected by HOME.

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
