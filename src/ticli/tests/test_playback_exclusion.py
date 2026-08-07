"""Tests for the one-process-at-a-time invariant on the audio backend.

`AudioPlayer` keeps exactly one handle to a player subprocess — `_process` —
and that handle is the *only* way anything is ever stopped, paused, seeked or
shut down. So a spawn that lands while another process is still alive does not
merely leak: the loser keeps decoding, keeps making sound, answers to nothing
(mpv's IPC socket is one fixed path per ticli pid, so the last spawn owns it),
and survives the app exiting. That is two songs at once out of one client.

The invariant these tests hold down is therefore about *processes*, not about
bookkeeping: every process ever spawned, except the one `_process` currently
names, must have been terminated or killed. Asserting that `_process` points at
the newest spawn passes with the bug fully present and proves nothing — which
is the ai/INCIDENTS #2 shape, and is exactly what the pre-existing fakes in
test_player_controls.py and test_cache.py cannot see (neither defines
`terminate`, and one makes `poll()` return None forever).

The mechanism guarded here: `play_url` used to call `stop()` — which takes
`_lock`, reaps, and *releases* — and only then re-acquire `_lock` to spawn.
Two starts arriving inside one reap each spawned a backend, because the loser's
`stop()` ran after the winner had already set `_process = None` and so reaped
nothing. See ai/INCIDENTS.md and ai/BUGS-2026-07-24-resume-trace.md item 4,
which predicted this exact failure ("tighter interleaving double-spawns mpv →
orphaned process, double audio") and whose third prescribed fix never shipped.

No TIDAL session and no real player process.
"""

import threading
import time
import types

import pytest

from ticli import player as player_mod
from ticli.player import AudioPlayer
from ticli.utils import config as config_mod


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    """Keep every player built here off the real ~/.config/ticli."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")


@pytest.fixture(autouse=True)
def no_downloads(monkeypatch):
    """play_url starts a cache download off the lock; nothing here wants it."""
    monkeypatch.setattr(AudioPlayer, "_start_download",
                        lambda self, *a, **k: None)


class FakeProc:
    """A player process that can be asked whether it is still making sound.

    Unlike the fakes elsewhere in the suite this one *dies* when it is
    terminated, because a fake that ignores `terminate()` cannot tell a reaped
    process from a leaked one — and that distinction is the whole subject here.
    """

    _next_pid = 9000

    def __init__(self, cmd=None, reap_delay=0.0):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.cmd = cmd
        self.terminated = False
        self.killed = False
        self.waited = 0
        self._reap_delay = reap_delay
        self._exited = False

    # -- the subprocess.Popen surface AudioPlayer actually uses --
    def poll(self):
        return None if not self._exited else (-15 if self.terminated else 0)

    def terminate(self):
        self.terminated = True
        self._exited = True

    def kill(self):
        self.killed = True
        self._exited = True

    def wait(self, timeout=None):
        self.waited += 1
        # Reaping a real mpv takes 37-75ms (ai/INCIDENTS.md). That duration is
        # not the bug, but it is what parks a second starter on the lock.
        if self._reap_delay:
            time.sleep(self._reap_delay)
        self._exited = True
        return self.poll()

    @property
    def alive(self):
        return not self._exited

    def __repr__(self):  # pragma: no cover - failure messages only
        state = "LIVE" if self.alive else (
            "killed" if self.killed else "terminated" if self.terminated
            else "exited")
        return f"<FakeProc pid={self.pid} {state}>"


def live_orphans(audio, spawned):
    """Every spawned process still making sound that `_process` does not name.

    This is the assertion the suite was missing. `_process` is allowed to be
    alive — that is the track playing. Anything *else* still alive is a second
    song nobody can stop.
    """
    return [p for p in spawned if p.alive and p is not audio._process]


@pytest.fixture
def spawned(monkeypatch):
    """Records every process spawned, and hands back killable fakes."""
    procs = []

    def _popen(cmd, **kwargs):
        proc = FakeProc(cmd=cmd)
        procs.append(proc)
        return proc

    monkeypatch.setattr(player_mod.subprocess, "Popen", _popen)
    return procs


class RecordingLock:
    """A real lock that writes down who held it and what happened inside.

    Wrapping rather than subclassing because `threading.Lock` is a factory, not
    a class. Only the surface AudioPlayer uses is provided, deliberately: a
    `release()` appearing here that the fix was supposed to remove is the
    finding, so nothing about the lock's behaviour is simulated.
    """

    def __init__(self, events):
        self._lock = threading.Lock()
        self._events = events

    def acquire(self, *a, **k):
        got = self._lock.acquire(*a, **k)
        if got:
            self._events.append("acquire")
        return got

    def release(self):
        self._events.append("release")
        self._lock.release()

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class TestOneCriticalSection:
    """The structural guard: nothing may take the lock between kill and spawn.

    A released lock in that gap is the whole bug — it is the only moment when
    `_process` is None while a spawn is still owed, so a second starter's
    `stop()` finds nothing to reap and spawns on top.
    """

    def _instrumented(self, monkeypatch, backend="mpv"):
        events = []
        audio = AudioPlayer(backend)
        audio._lock = RecordingLock(events)

        def _popen(cmd, **kwargs):
            events.append("spawn")
            return FakeProc(cmd=cmd)

        monkeypatch.setattr(player_mod.subprocess, "Popen", _popen)
        return audio, events

    def test_a_track_change_never_releases_the_lock_between_them(
            self, monkeypatch):
        audio, events = self._instrumented(monkeypatch)
        old = FakeProc()
        audio._process = old

        def _reap():
            events.append("reap")
            old.terminate()

        monkeypatch.setattr(audio, "_reap_process", _reap)
        audio.play_url("http://cdn/next.m4a")

        assert "reap" in events and "spawn" in events
        between = events[events.index("reap"):events.index("spawn")]
        assert "release" not in between, (
            "the lock was handed back between killing the old process and "
            f"spawning the new one — sequence: {events}")

    def test_the_whole_track_change_is_a_single_acquisition(self, monkeypatch):
        """One acquire, one release. Two of each is the seam, by definition."""
        audio, events = self._instrumented(monkeypatch)
        audio._process = FakeProc()
        audio.play_url("http://cdn/next.m4a")
        assert events.count("acquire") == 1, (
            f"track change took the lock {events.count('acquire')} times: "
            f"{events}")

    def test_a_resume_that_falls_back_to_the_url_holds_its_lock_throughout(
            self, monkeypatch):
        """resume() used to `release()` by hand to call play_url and re-acquire.

        Same seam, reached by pressing space on ffplay before the cache copy
        is ready. WORKING-RULES forbids the shape outright: a plain Lock is not
        reentrant, so the answer is an unlocked `_locked` half, never a manual
        unlock-and-hope.
        """
        audio, events = self._instrumented(monkeypatch, backend="ffplay")
        audio._process = None
        audio._paused = True
        audio._current_url = "http://cdn/track.m4a"
        audio._seek_offset = 12.5

        assert audio.resume() is True
        assert events.count("acquire") == 1, (
            f"resume took the lock {events.count('acquire')} times: {events}")
        assert events == ["acquire", "spawn", "release"], events


def widen_the_seam(audio, monkeypatch, gap=0.05):
    """Hold open the window between `play_url`'s kill and its spawn.

    The window is real and a few bytecodes wide: `play_url` called `stop()`,
    which took `_lock`, reaped, and *released*; only then did it re-acquire
    `_lock` to spawn. A second starter had to arrive in the instant between
    that release and that re-acquire.

    Left to the scheduler that instant is almost never hit — CPython locks are
    not fair, so the thread that just released barges and re-acquires before
    the parked waiter is even woken. Measured under 1% of contended track
    changes, which is a bug you hit once a month and can never reproduce on
    purpose. A test that merely starts two threads and hopes passes with the
    bug fully present; that is the ai/INCIDENTS #2 shape and it is how this
    one survived.

    So this pins the interleaving instead of gambling on it. It does not create
    the window — it stops the releasing thread from immediately re-taking the
    lock, which is the only reason the window is hard to observe. Once kill and
    spawn happen in one acquisition, `play_url` no longer calls `stop()` at
    all, this hook is never invoked, and there is no instant to land in.
    """
    real_stop = audio.stop

    def _stop_then_yield():
        real_stop()
        time.sleep(gap)

    monkeypatch.setattr(audio, "stop", _stop_then_yield)


class TestConcurrentStartsLeaveOneProcess:
    """The observable-reality guard: live processes, not bookkeeping.

    Two `play_url` calls race, arriving inside one reap — the reachable case
    being a double-tap on `n`, which has no repeat window (KEY_REPEAT_WINDOW
    guards only the space bar) and which reaches `play_url` with no network in
    between whenever both tracks are downloaded or cached.
    """

    def _race(self, audio, spawned, stagger):
        """Start B mid-reap, run A on this thread, return the live orphans."""
        errors = []

        def _second():
            time.sleep(stagger)
            try:
                audio.play_url("http://cdn/b.m4a")
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        thread_b = threading.Thread(target=_second, daemon=True)
        thread_b.start()
        audio.play_url("http://cdn/a.m4a")
        thread_b.join(5)
        assert not errors, errors
        assert not thread_b.is_alive(), "a starter never finished"
        return live_orphans(audio, spawned)

    def test_two_starts_inside_one_reap_leave_exactly_one_live_process(
            self, spawned, monkeypatch):
        # A reap slow enough to park the second starter on the lock, which is
        # what a real mpv does anyway (37-75ms, ai/INCIDENTS.md).
        first = FakeProc(reap_delay=0.03)
        audio = AudioPlayer("mpv")
        audio._process = first
        widen_the_seam(audio, monkeypatch)

        orphans = self._race(audio, spawned, stagger=0.005)

        assert first.terminated or first.killed, (
            "the process that was playing was never reaped")
        assert len(spawned) == 2, "both starters should have spawned"
        assert orphans == [], (
            f"{len(orphans)} audio process(es) still playing that nothing can "
            f"stop: {orphans}; _process names {audio._process}. That is a "
            "second song out of one client, and quitting will not end it.")

    @pytest.mark.parametrize("stagger_ms", [0, 2, 5, 10, 20, 29])
    def test_a_contended_track_change_never_orphans_a_player(
            self, spawned, monkeypatch, stagger_ms):
        """The same race across the whole reap, so no arrival time is special.

        Parametrised rather than looped so a failure names the stagger that
        produced it. Every one of these lands the second starter somewhere
        inside the first's reap, which is the precondition for both of them
        reaching the spawn.
        """
        first = FakeProc(reap_delay=0.03)
        audio = AudioPlayer("mpv")
        audio._process = first
        widen_the_seam(audio, monkeypatch)

        orphans = self._race(audio, spawned, stagger=stagger_ms / 1000)

        assert orphans == [], (
            f"stagger {stagger_ms}ms orphaned {len(orphans)} live "
            f"player(s): {orphans}; _process names {audio._process}")

    def test_the_survivor_is_the_track_that_was_asked_for_last(
            self, spawned, monkeypatch):
        """Not just one process — the *right* one.

        Closing the seam by letting the loser win would silence the overlap and
        leave the user on a track they did not pick.
        """
        audio = AudioPlayer("mpv")
        audio._process = FakeProc(reap_delay=0.03)
        widen_the_seam(audio, monkeypatch)

        self._race(audio, spawned, stagger=0.005)

        assert audio._process is spawned[-1], (
            "_process must name the most recent spawn")
        assert audio._current_url == spawned[-1].cmd[-1], (
            "the URL on record disagrees with the process that is playing")


class TestWithRealProcesses:
    """The same invariant against real PIDs, because fakes cannot bleed.

    Everything above runs on a fake that dies politely when asked. A real
    orphan is an OS process that keeps running, and the reason this bug was
    audible rather than merely untidy is that nothing in ticli ever reaped it.
    So this one spawns actual children in place of the backend, races them, and
    then asks the kernel — not the bookkeeping — who is still alive.

    `sleep` stands in for mpv: long-lived, silent, and it dies of SIGTERM the
    same way. The command ticli would really build is exercised everywhere
    else; what is under test here is the process arithmetic.
    """

    def test_a_contended_track_change_leaves_no_process_running(
            self, monkeypatch):
        real_popen = player_mod.subprocess.Popen
        spawned = []

        def _popen(cmd, **kwargs):
            proc = real_popen(["sleep", "30"], **kwargs)
            spawned.append(proc)
            return proc

        monkeypatch.setattr(player_mod.subprocess, "Popen", _popen)

        audio = AudioPlayer("mpv")
        first = real_popen(["sleep", "30"])
        spawned.append(first)
        audio._process = first
        widen_the_seam(audio, monkeypatch)

        errors = []

        def _second():
            time.sleep(0.005)
            try:
                audio.play_url("http://cdn/b.m4a")
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        thread_b = threading.Thread(target=_second, daemon=True)
        thread_b.start()
        audio.play_url("http://cdn/a.m4a")
        thread_b.join(10)
        assert not errors, errors
        assert not thread_b.is_alive()

        try:
            # Give a terminated child a moment to actually go away before
            # calling it a leak; poll() is only truthful once it has.
            deadline = time.time() + 2
            survivors = []
            while time.time() < deadline:
                survivors = [p for p in spawned
                             if p.poll() is None and p is not audio._process]
                if not survivors:
                    break
                time.sleep(0.02)
            assert survivors == [], (
                "pids still running that ticli can no longer stop: "
                f"{[p.pid for p in survivors]} (ticli thinks it is playing "
                f"{audio._process.pid}). These outlive the app.")
            assert audio._process.poll() is None, "the track died with them"
        finally:
            for proc in spawned:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)


class TestReapIsComplete:
    """A reap that does not wait leaves a zombie and drops its last handle."""

    def test_a_process_that_ignores_sigterm_is_killed_and_waited_for(self):
        audio = AudioPlayer("mpv")

        class Stubborn(FakeProc):
            def wait(self, timeout=None):
                self.waited += 1
                if not self.killed:
                    raise player_mod.subprocess.TimeoutExpired("mpv", timeout)
                self._exited = True
                return -9

        proc = Stubborn()
        audio._process = proc
        audio._reap_process()

        assert proc.terminated and proc.killed
        assert proc.waited == 2, (
            "SIGKILL was sent but never waited for — the child stays a zombie "
            "and the handle that could collect it is about to be dropped")

    def test_stop_forgets_a_process_that_already_exited(self, spawned):
        """A dead handle left in `_process` is a stale answer, not a process.

        `failure()` reads it, so a track the backend refused kept reporting its
        error after we had stopped it — 'never display something that isn't
        real'. Nothing is reaped here: it is already gone.
        """
        audio = AudioPlayer("mpv")
        dead = FakeProc()
        dead._exited = True
        audio._process = dead
        audio.stop()

        assert audio._process is None
        assert not dead.terminated and not dead.killed
        assert audio.failure() is None
