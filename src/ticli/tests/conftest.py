"""Suite-wide safety rails.

Rules applied to every test in the package rather than per module, because a
test that forgets one must not be the thing that finds out.

The download folder defaults to `~/Music/Ticli`, which is the owner's own
music. Nothing in the suite may write there. `DOWNLOAD_ROOT` is read at call
time by `downloads.download_dir()`, so pointing it at tmp_path here is enough
— no test needs to know.

`STATE_DIR` is the same problem one directory over: `~/.config/ticli` holds the
saved session and the single-instance lock. Only a handful of tests call
`run()`, and one of them (`test_run_clamps_before_anything_plays`) redirects
the config but not the state — which was enough for the instance lock to
appear in the owner's real config directory the first time run() took one.
Redirecting it here rather than there, because the next test to call `run()`
would have had the same hole.

The cache directory, the config directory and the credential store (keychain
and its fallback file under `~/.config/ticli`) are redirected per test by the
fixtures below, but a per-test fixture only protects the test it runs in. A
thread a test leaves running outlives that test's monkeypatch, and when the
monkeypatch is undone the module constant goes back to the *real* path — one
such thread once deleted the owner's real cache index. So every path constant
is also redirected for the whole session, and the per-test fixtures restore to
that scratch directory, never to the real one.
"""

import pytest

from ticli import player as player_mod
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod
from ticli.utils import credential_store as credential_store_mod
from ticli.utils import downloads as downloads_mod
from ticli.utils import throttle as throttle_mod


@pytest.fixture(autouse=True, scope="session")
def never_the_real_dirs_after_teardown(tmp_path_factory):
    base = tmp_path_factory.mktemp("session-scratch")
    mp = pytest.MonkeyPatch()
    mp.setattr(cache_mod, "CACHE_DIR", base / "cache")
    mp.setattr(downloads_mod, "DOWNLOAD_ROOT", base / "Music")
    mp.setattr(player_mod, "STATE_DIR", base / "state")
    mp.setattr(player_mod, "STATE_FILE", base / "state" / "player_state.json")
    mp.setattr(throttle_mod, "STATE_DIR", base / "state")
    mp.setattr(config_mod, "CONFIG_DIR", base / "config")
    mp.setattr(config_mod, "CONFIG_FILE", base / "config" / "config.json")
    mp.setattr(credential_store_mod, "keyring", None)
    mp.setattr(credential_store_mod, "FALLBACK_DIR", base / "credentials")
    mp.setattr(credential_store_mod, "FALLBACK_FILE", base / "credentials" / "session.json")
    yield base
    mp.undo()


@pytest.fixture(autouse=True)
def never_the_real_music_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(downloads_mod, "DOWNLOAD_ROOT", tmp_path / "Music" / "Ticli")


@pytest.fixture(autouse=True)
def never_the_real_state_dir(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(player_mod, "STATE_DIR", state)
    monkeypatch.setattr(player_mod, "STATE_FILE", state / "player_state.json")
    # The agent surface keeps its throttle state in the same directory, via
    # its own module-level STATE_DIR (player's import chain is too heavy for
    # `ticli agent --help`). Redirected here with the rest, because a test
    # that touches the throttle must never read or trip the owner's real one.
    monkeypatch.setattr(throttle_mod, "STATE_DIR", state)


@pytest.fixture(autouse=True)
def never_the_real_config(tmp_path, monkeypatch):
    # The owner's config.json must never decide a test's outcome.
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config" / "config.json")


@pytest.fixture(autouse=True)
def never_the_real_tokens(tmp_path, monkeypatch):
    # The owner's keychain and ~/.config/ticli/session.json hold a real TIDAL login.
    monkeypatch.setattr(credential_store_mod, "keyring", None)
    monkeypatch.setattr(credential_store_mod, "FALLBACK_DIR", tmp_path / "credentials")
    monkeypatch.setattr(credential_store_mod, "FALLBACK_FILE", tmp_path / "credentials" / "session.json")


@pytest.fixture(autouse=True)
def never_the_real_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
