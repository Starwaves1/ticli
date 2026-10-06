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

A playback failure asks every installed backend for its version
(`backend_health.probe_backends`). That is a real subprocess against whatever
mpv/ffplay this machine has, so a test's outcome would depend on the machine —
and on 2026-10-05 the owner's mpv was exactly the broken one. Stubbed to "no
backends installed", which `describe` treats as nothing to add; tests about
the probe itself use fake binaries or the reference kept in their module.
"""

import pytest

from ticli import player as player_mod
from ticli.utils import backend_health as backend_health_mod
from ticli.utils import downloads as downloads_mod
from ticli.utils import throttle as throttle_mod


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
def never_probe_the_real_backends(monkeypatch):
    monkeypatch.setattr(backend_health_mod, "probe_backends", lambda *a, **k: [])
