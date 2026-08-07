"""Tests for the one-ticli-at-a-time guard.

Two ticli processes is the second route to the symptom that INCIDENTS #7 was
about: a primary song playing and another song playing at the same time. The
in-process race is fixed; this is the other door. Two instances also take
turns overwriting `player_state.json` — BUGS-2026-07-24 item 8, whose atomic
write fixed *torn* files but not *clobbered* ones, and whose shared `.tmp`
path means the loser's save can fail outright and be swallowed at debug level.

What is asserted here is the behaviour that matters rather than the
implementation: that a second instance is refused, that it is refused before
it can touch anything, that the refusal names the process to go and quit, and
above all that the lock **frees itself when its holder dies** — the property
that makes an advisory `flock` the right answer and a pid file the wrong one.
That last one uses a real child process and a real SIGKILL, because it cannot
be demonstrated any other way.

No TIDAL session and no real player process.
"""

import os
import subprocess
import sys
import textwrap
import time

import pytest

from ticli import player as player_mod
from ticli.player import HeadlessTidalPlayer
from ticli.utils import config as config_mod


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every lock in this file lives under tmp_path, never ~/.config/ticli."""
    monkeypatch.setattr(player_mod, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(player_mod, "STATE_FILE",
                        tmp_path / "state" / "player_state.json")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    return tmp_path


@pytest.fixture
def held(request):
    """Release any descriptor a test took, so tests cannot leak the lock."""
    fds = []
    request.addfinalizer(
        lambda: [os.close(fd) for fd in fds if fd is not None])
    return fds


def test_the_suite_never_locks_the_owners_real_config_dir():
    """Tripwire on the conftest rail, not on this file's own fixture.

    `run()` takes the instance lock before it does anything else, so any test
    that calls it writes a lock file wherever STATE_DIR points. It pointed at
    `~/.config/ticli` the first time, and the only reason that is not still
    true is a suite-wide fixture that a future edit could quietly drop.
    """
    from pathlib import Path
    real = Path.home() / ".config" / "ticli"
    assert player_mod.STATE_DIR != real
    assert not str(player_mod._instance_lock_path()).startswith(str(real))


class TestTheLockItself:
    def test_the_first_caller_gets_it(self, held):
        fd, other = player_mod._take_instance_lock()
        held.append(fd)
        assert fd is not None
        assert other is None, "nothing else holds it, so nothing to report"

    def test_the_second_caller_is_refused_and_told_who_has_it(self, held):
        fd, _ = player_mod._take_instance_lock()
        held.append(fd)

        second_fd, other = player_mod._take_instance_lock()
        assert second_fd is None, "a second holder would defeat the point"
        assert other == os.getpid(), (
            "the refusal must name the process actually holding the lock, "
            "so the user knows which window to go and close")

    def test_the_lock_is_free_again_once_the_holder_lets_go(self, held):
        fd, _ = player_mod._take_instance_lock()
        assert fd is not None
        os.close(fd)

        second_fd, other = player_mod._take_instance_lock()
        held.append(second_fd)
        assert other is None and second_fd is not None, (
            "quitting ticli must leave the next launch able to start")

    def test_it_writes_the_holders_pid_where_the_next_caller_looks(self, held):
        fd, _ = player_mod._take_instance_lock()
        held.append(fd)
        recorded = player_mod._instance_lock_path().read_text().strip()
        assert recorded == str(os.getpid())


class TestAHolderThatDies:
    """The property a pid file cannot have.

    BUGS-2026-07-24 item 8 proposed a pid lockfile. A pid file has to decide
    whether the pid in it is still alive, and it is wrong in both directions:
    it strands the app after a crash, and it can match a recycled pid. An
    advisory flock is released by the kernel when the holder's last descriptor
    goes, which happens on every exit path including the ones that run no
    code at all. That is worth a real process and a real signal to prove.
    """

    def _holder(self, src_root, state_dir):
        """A real child process that takes the lock and then sits on it."""
        script = textwrap.dedent(f"""
            import sys, time
            from pathlib import Path
            sys.path.insert(0, {str(src_root)!r})
            from ticli import player as player_mod
            player_mod.STATE_DIR = Path({str(state_dir)!r})
            fd, other = player_mod._take_instance_lock()
            if fd is None:
                print("FAILED-TO-ACQUIRE", flush=True)
                sys.exit(1)
            print("HELD", flush=True)
            time.sleep(120)
        """)
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = proc.stdout.readline().strip()
        if line != "HELD":
            proc.kill()
            _, err = proc.communicate(timeout=10)
            pytest.fail(f"child never took the lock: {line!r}\n{err}")
        return proc

    @pytest.fixture
    def src_root(self):
        # ticli/player.py -> ticli/ -> src/
        return os.path.dirname(os.path.dirname(
            os.path.abspath(player_mod.__file__)))

    def test_another_process_holding_it_blocks_this_one(
            self, isolated_state, src_root):
        proc = self._holder(src_root, player_mod.STATE_DIR)
        try:
            fd, other = player_mod._take_instance_lock()
            assert fd is None, "a second process must not get the lock too"
            assert other == proc.pid, (
                f"should have named the real holder {proc.pid}, said {other}")
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_a_killed_holder_leaves_no_stale_lock(
            self, isolated_state, src_root, held):
        """SIGKILL runs no cleanup code. The lock must free itself anyway."""
        proc = self._holder(src_root, player_mod.STATE_DIR)
        proc.kill()
        proc.wait(timeout=10)

        deadline = time.time() + 5
        fd = None
        while time.time() < deadline:
            fd, other = player_mod._take_instance_lock()
            if fd is not None:
                break
            time.sleep(0.05)
        held.append(fd)
        assert fd is not None, (
            "the lock survived its holder being SIGKILLed — ticli would "
            "refuse to start after any crash, with no way out but deleting "
            f"{player_mod._instance_lock_path()} by hand")
        # The stale pid is still written in the file; it must not be believed.
        assert player_mod._read_lock_pid(fd) in (0, os.getpid())


class TestRunRefusesTheSecondInstance:
    def test_it_stops_before_touching_the_audio_backend(self, held, monkeypatch,
                                                        capsys):
        """Refused early enough that nothing shared is opened at all.

        The order matters: a second instance that got as far as the cache
        reconcile or the state restore would already be writing over the
        first one's session before it printed its apology.
        """
        fd, _ = player_mod._take_instance_lock()
        held.append(fd)

        def _boom():  # pragma: no cover - the assertion is that this is unused
            raise AssertionError(
                "the second instance reached the audio backend; it must "
                "refuse before anything shared is opened")

        monkeypatch.setattr(player_mod, "_find_audio_player", _boom)
        player = HeadlessTidalPlayer()
        player.run()

        assert player.audio is None, "no backend should have been built"
        assert not player_mod.STATE_FILE.exists(), (
            "the refused instance wrote player state over the running one's")

    def test_it_says_what_is_wrong_and_what_to_do(self, held, capsys):
        fd, _ = player_mod._take_instance_lock()
        held.append(fd)

        HeadlessTidalPlayer().run()

        said = capsys.readouterr().out
        assert "already running" in said
        assert str(os.getpid()) in said, "name the process to go and quit"
        assert "Quit" in said, "an error with no way forward is half an error"

    def test_the_first_instance_is_not_refused(self, monkeypatch):
        """The guard must not stand in the way of the normal case."""
        reached = []
        monkeypatch.setattr(player_mod, "_find_audio_player",
                            lambda: reached.append(True))
        player = HeadlessTidalPlayer()
        player.run()
        assert reached, "a lone ticli was refused its own lock"
        assert player._instance_lock_fd is not None


class TestDegradesRatherThanLocksTheOwnerOut:
    """A guard against a mistake must never become a mistake of its own.

    On a filesystem that will not take an flock — an NFS home directory is the
    real case — the question cannot be answered. Refusing on an answer we
    never got would mean ticli simply stops working, which is far worse than
    the bug being guarded against.
    """

    def test_a_filesystem_that_cannot_lock_still_starts(self, monkeypatch):
        import fcntl

        def _no_locking(fd, op):
            raise OSError(errno_enotsup(), "locking not supported")

        monkeypatch.setattr(fcntl, "flock", _no_locking)
        fd, other = player_mod._take_instance_lock()
        assert other is None, "must not refuse on a question it could not ask"
        assert fd is None, "and must not claim to hold a lock it never took"

    def test_an_unwritable_state_dir_still_starts(self, monkeypatch):
        def _no_open(*a, **k):
            raise OSError("read-only file system")

        monkeypatch.setattr(player_mod.os, "open", _no_open)
        fd, other = player_mod._take_instance_lock()
        assert (fd, other) == (None, None)

    def test_run_continues_when_the_lock_cannot_be_evaluated(self, monkeypatch):
        monkeypatch.setattr(player_mod, "_take_instance_lock",
                            lambda: (None, None))
        reached = []
        monkeypatch.setattr(player_mod, "_find_audio_player",
                            lambda: reached.append(True))
        HeadlessTidalPlayer().run()
        assert reached, "an unanswerable lock question stopped ticli starting"


def errno_enotsup():
    import errno
    return getattr(errno, "ENOTSUP", errno.EINVAL)
