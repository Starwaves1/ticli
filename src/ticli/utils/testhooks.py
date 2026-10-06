"""Hooks that let tests run a real `python -m ticli.playerd` against a fake TIDAL.

Off unless TICLI_TEST_HOOKS=1 is set as well, so a stray variable can never
swap a user's session or move their tokens:

- TICLI_TEST_SESSION="module:callable": `tidal_session()` returns `callable()`
  instead of a tidalapi session.
- Tokens skip the OS keyring and use the file under $HOME/.config/ticli: the
  macOS Keychain is not redirected by HOME.

`tests/fake_tidal.py` is the session the real-process tests use.
"""

import importlib
import os

ENABLE = "TICLI_TEST_HOOKS"
SESSION = "TICLI_TEST_SESSION"


def enabled() -> bool:
    return os.environ.get(ENABLE) == "1"


def session_factory():
    spec = os.environ.get(SESSION) if enabled() else None
    if not spec:
        return None
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)
