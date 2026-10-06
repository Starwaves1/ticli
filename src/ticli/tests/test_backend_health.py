"""A player that cannot run must say so — and must not look like TIDAL.

2026-10-05: a Homebrew upgrade left mpv unable to load (`libass` linked
against a `libunibreak` that was no longer there). dyld killed it with
SIGABRT on every spawn, `failure()` read the negative code as "we killed it",
and the UI said "Playback stopped early — the stream ended at 0:00" on every
track. These tests pin the classification, the post-failure version probe,
and what reaches the screen. Fake player binaries are POSIX sh scripts, so
everything here runs the same on macOS and Linux; the one test that needs a
real dynamic linker builds its own victim with the system C compiler.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
import types

import pytest
import requests

from ticli import player as player_mod
from ticli.player import HeadlessTidalPlayer
from ticli.utils import backend_health as bh

# Taken at import, before conftest's rail stubs the module attribute
REAL_PROBE_BACKENDS = bh.probe_backends

DYLD_STDERR = (
    "dyld[2430]: Library not loaded: /opt/homebrew/opt/libunibreak/lib/libunibreak.7.dylib\n"
    "  Referenced from: <66461005> /opt/homebrew/Cellar/libass/0.17.5/lib/libass.9.dylib\n"
    "  Reason: tried: '/opt/homebrew/opt/libunibreak/lib/libunibreak.7.dylib' (no such file)\n")
LDSO_STDERR = ("mpv: error while loading shared libraries: libass.so.9: "
               "cannot open shared object file: No such file or directory\n")


def _fake(tmp_path, name, body):
    """An executable sh script standing in for a player binary."""
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


class TestClassifyExit:

    @pytest.mark.parametrize("code", [None, 0])
    def test_running_or_a_clean_exit_is_not_a_failure(self, code):
        assert bh.classify_exit("mpv", code, "anything") is None

    def test_macos_dyld_refusal_is_a_broken_install_naming_the_library(self):
        failure = bh.classify_exit("mpv", -signal.SIGABRT, DYLD_STDERR)
        assert failure.code == bh.BROKEN_INSTALL
        assert "libunibreak.7.dylib" in failure.summary
        # The first line, not dyld's search-path list that follows it
        assert failure.detail.startswith("dyld[2430]: Library not loaded")

    def test_macos_missing_symbol_is_a_broken_install(self):
        failure = bh.classify_exit(
            "mpv", -signal.SIGABRT, "dyld[1]: Symbol not found: _ass_set_foo\n")
        assert failure.code == bh.BROKEN_INSTALL
        assert "_ass_set_foo" in failure.summary

    def test_linux_ldso_refusal_is_a_broken_install_naming_the_library(self):
        failure = bh.classify_exit("mpv", 127, LDSO_STDERR)
        assert failure.code == bh.BROKEN_INSTALL
        assert "libass.so.9" in failure.summary

    def test_linux_undefined_symbol_is_a_broken_install(self):
        failure = bh.classify_exit(
            "ffplay", 127,
            "ffplay: symbol lookup error: /lib/libavcodec.so.61: "
            "undefined symbol: av_foo\n")
        assert failure.code == bh.BROKEN_INSTALL
        assert "av_foo" in failure.summary

    def test_linux_glibc_too_old_is_a_broken_install(self):
        failure = bh.classify_exit(
            "mpv", 1, "mpv: /lib/x86_64-linux-gnu/libc.so.6: version "
                      "`GLIBC_2.38' not found (required by mpv)\n")
        assert failure.code == bh.BROKEN_INSTALL
        assert "GLIBC_2.38" in failure.summary

    def test_a_fault_signal_is_a_crash(self):
        failure = bh.classify_exit("mpv", -signal.SIGSEGV, "")
        assert failure.code == bh.CRASHED
        assert "SIGSEGV" in failure.summary

    def test_abort_without_a_linker_message_is_a_crash(self):
        assert bh.classify_exit("mpv", -signal.SIGABRT, "").code == bh.CRASHED

    def test_an_ordinary_signal_is_killed_from_outside(self):
        failure = bh.classify_exit("mpv", -signal.SIGKILL, "")
        assert failure.code == bh.KILLED
        assert "SIGKILL" in failure.summary

    def test_an_unknown_signal_number_still_classifies(self):
        assert bh.classify_exit("mpv", -200, "").code == bh.KILLED

    def test_a_nonzero_exit_quotes_the_last_line_trimmed(self):
        failure = bh.classify_exit("ffplay", 1, "a\n" + "y" * 300 + "\n", limit=20)
        assert failure.code == bh.EXIT_STATUS
        assert failure.summary == "ffplay error: " + "y" * 20
        assert failure.detail == "y" * 300


class TestHints:

    def test_a_homebrew_player_is_told_to_brew_upgrade(self, monkeypatch):
        monkeypatch.setattr(bh.shutil, "which", lambda p: "/opt/homebrew/bin/mpv")
        monkeypatch.setattr(bh.os.path, "realpath",
                            lambda p: "/opt/homebrew/Cellar/mpv/0.41.0_9/bin/mpv")
        assert bh.reinstall_hint("mpv") == "try: brew upgrade mpv"

    def test_linuxbrew_counts_as_homebrew(self, monkeypatch):
        monkeypatch.setattr(bh.shutil, "which",
                            lambda p: "/home/linuxbrew/.linuxbrew/bin/mpv")
        # Identity: on macOS /home is an automount and resolves elsewhere
        monkeypatch.setattr(bh.os.path, "realpath", lambda p: p)
        assert bh.reinstall_hint("mpv") == "try: brew upgrade mpv"

    def test_a_distro_player_on_linux_points_at_the_package_manager(self, monkeypatch):
        monkeypatch.setattr(bh.shutil, "which", lambda p: "/usr/bin/mpv")
        monkeypatch.setattr(bh.sys, "platform", "linux")
        assert "package manager" in bh.reinstall_hint("mpv")


class TestProbe:
    """`probe_backend` against fake binaries that behave like the real
    failure modes, on either OS."""

    def test_a_healthy_backend_reports_its_version(self, tmp_path):
        mpv = _fake(tmp_path, "mpv",
                    'echo "mpv v0.41.0 Copyright"; echo "built on x"')
        probe = bh.probe_backend("mpv", mpv)
        assert probe.ok and probe.version == "mpv v0.41.0 Copyright"

    def test_ffplay_is_asked_with_its_own_flag(self, tmp_path):
        ffplay = _fake(tmp_path, "ffplay",
                       '[ "$1" = "-version" ] || exit 1; echo "ffplay version 9"')
        assert bh.probe_backend("ffplay", ffplay).ok

    def test_a_backend_the_linker_aborts_is_a_broken_install(self, tmp_path):
        stderr = tmp_path / "dyld.txt"
        stderr.write_text(DYLD_STDERR)
        mpv = _fake(tmp_path, "mpv", f'cat "{stderr}" >&2; kill -ABRT $$')
        probe = bh.probe_backend("mpv", mpv)
        assert not probe.ok
        assert probe.failure.code == bh.BROKEN_INSTALL

    def test_a_backend_that_hangs_is_reported_not_waited_on(self, tmp_path):
        mpv = _fake(tmp_path, "mpv", "sleep 30")
        started = time.monotonic()
        probe = bh.probe_backend("mpv", mpv, timeout=0.3)
        assert probe.failure.code == bh.HUNG
        assert time.monotonic() - started < 5

    def test_a_missing_binary_is_not_runnable(self, tmp_path):
        probe = bh.probe_backend("mpv", str(tmp_path / "gone"))
        assert probe.failure.code == bh.NOT_RUNNABLE

    def test_a_binary_without_its_execute_bit_is_not_runnable(self, tmp_path):
        mpv = _fake(tmp_path, "mpv", "echo hi")
        os.chmod(mpv, 0o644)
        assert bh.probe_backend("mpv", mpv).failure.code == bh.NOT_RUNNABLE

    def test_only_installed_backends_are_probed_in_preference_order(
            self, tmp_path, monkeypatch):
        _fake(tmp_path, "ffplay", 'echo "ffplay version 9"')
        monkeypatch.setenv("PATH", str(tmp_path))
        probes = REAL_PROBE_BACKENDS()
        assert [p.player for p in probes] == ["ffplay"]
        _fake(tmp_path, "mpv", 'echo "mpv v0.41.0"')
        assert [p.player for p in REAL_PROBE_BACKENDS()] == ["mpv", "ffplay"]

    def test_the_backend_list_matches_the_players(self):
        assert tuple(player_mod.AUDIO_PLAYERS) == bh.BACKENDS


@pytest.mark.skipif(not shutil.which("cc"), reason="needs a C compiler")
class TestARealLinkerRefusal:
    """Not a transcript of one: the real dynamic linker of whichever OS this
    is, refusing a binary whose library was deleted — the exact event of
    2026-10-05, minus Homebrew."""

    def test_it_classifies_as_a_broken_install(self, tmp_path):
        ext = "dylib" if sys.platform == "darwin" else "so"
        lib = tmp_path / f"libticlivictim.{ext}"
        (tmp_path / "l.c").write_text("int f(void){return 0;}\n")
        (tmp_path / "m.c").write_text("int f(void); int main(void){return f();}\n")
        shared = ["-dynamiclib", "-install_name", str(lib)] \
            if sys.platform == "darwin" else ["-shared", "-fPIC"]
        subprocess.run(["cc", *shared, "-o", str(lib), str(tmp_path / "l.c")],
                       check=True)
        rpath = [] if sys.platform == "darwin" else [f"-Wl,-rpath,{tmp_path}"]
        victim = tmp_path / "mpv"
        subprocess.run(["cc", "-o", str(victim), str(tmp_path / "m.c"),
                        f"-L{tmp_path}", "-lticlivictim", *rpath], check=True)
        assert bh.probe_backend("mpv", str(victim)).ok   # works while whole
        lib.unlink()

        probe = bh.probe_backend("mpv", str(victim))
        assert probe.failure.code == bh.BROKEN_INSTALL
        assert f"libticlivictim.{ext}" in probe.failure.summary


class TestDescribe:

    def _probe(self, player, failure=None):
        return bh.BackendProbe(player, f"/x/{player}", "v1" if not failure else "",
                               failure)

    def test_a_broken_backend_names_the_fix_and_the_one_that_works(self, monkeypatch):
        monkeypatch.setattr(bh, "reinstall_hint", lambda p: f"try: brew upgrade {p}")
        failure = bh.classify_exit("mpv", -6, DYLD_STDERR)
        text = bh.describe(failure, [self._probe("mpv", failure), self._probe("ffplay")])
        assert text == ("mpv can't start — broken install, libunibreak.7.dylib "
                        "missing; try: brew upgrade mpv; ffplay is OK")

    def test_a_healthy_probe_adds_nothing(self):
        failure = bh.classify_exit("mpv", -signal.SIGSEGV, "")
        text = bh.describe(failure, [self._probe("mpv"), self._probe("ffplay")])
        assert text == "mpv crashed (SIGSEGV)"

    def test_a_probe_that_finds_the_install_broken_overrides_a_vaguer_exit(self):
        broken = bh.classify_exit("mpv", 127, LDSO_STDERR)
        vague = bh.classify_exit("mpv", -signal.SIGKILL, "")
        text = bh.describe(vague, [self._probe("mpv", broken)])
        assert text.startswith("mpv can't start — broken install, libass.so.9")

    def test_no_backends_on_path_adds_nothing(self):
        failure = bh.classify_exit("mpv", 2, "Failed to open x")
        assert bh.describe(failure, []) == "mpv error: Failed to open x"


class TestSpawning:

    def test_a_player_that_cannot_be_spawned_raises_its_own_type(self, tmp_path):
        audio = player_mod.AudioPlayer("mpv", cache=None)
        with pytest.raises(bh.SpawnError) as caught:
            audio._spawn([str(tmp_path / "no-such-mpv")])
        assert caught.value.failure.code == bh.NOT_RUNNABLE
        assert audio._process is None

    def test_spawn_error_is_not_an_oserror(self):
        """The reason it exists: every `requests` failure *is* an OSError,
        and a network error must never be reported as a broken player."""
        assert issubclass(requests.RequestException, OSError)
        assert not issubclass(bh.SpawnError, OSError)


class TestTheMonitorSaysWhatBroke:
    """End to end through `_monitor_playback`, the path that showed
    "stopped early at 0:00" on 2026-10-05."""

    def _player(self, monkeypatch, failure, probes, position=0.0):
        player = HeadlessTidalPlayer()
        player._playing = True
        player._track_changing = False
        player._current_track = types.SimpleNamespace(id=7, duration=150)
        player._queue = [player._current_track, types.SimpleNamespace(id=8, duration=200)]
        player._queue_index = 0
        player._play_start_time = None
        player._play_offset = position
        player.advanced = []
        player._play_queue_index = lambda i: player.advanced.append(i)
        player._save_state = lambda: None
        player.audio = types.SimpleNamespace(
            player_cmd="mpv", is_paused=False, is_playing=False,
            failure=lambda: failure, source_vanished=lambda: False,
            get_time_pos=lambda: None, poll_media_key=lambda: None)
        monkeypatch.setattr(bh, "probe_backends", lambda *a: probes)
        monkeypatch.setattr(bh, "reinstall_hint", lambda p: f"try: brew upgrade {p}")
        ticks = []

        def _tick():
            player.running = len(ticks) < 2
            ticks.append(1)
        monkeypatch.setattr(player_mod.time, "sleep", lambda s: _tick())
        player.running = True
        return player

    def test_the_2026_10_05_failure_reads_as_a_broken_mpv(self, monkeypatch):
        # Before classifying: the hint is computed then, from this host's PATH
        monkeypatch.setattr(bh, "reinstall_hint", lambda p: f"try: brew upgrade {p}")
        failure = bh.classify_exit("mpv", -6, DYLD_STDERR)
        probes = [bh.BackendProbe("mpv", "/x/mpv", failure=failure),
                  bh.BackendProbe("ffplay", "/x/ffplay", "ffplay version 9")]
        player = self._player(monkeypatch, failure, probes)

        player._monitor_playback()

        assert player.advanced == []
        assert player._playing is False
        assert "stopped early" not in player._toast
        assert "libunibreak.7.dylib" in player._toast
        assert "brew upgrade mpv" in player._toast
        assert "ffplay is OK" in player._toast

    def test_a_clean_early_exit_with_healthy_backends_keeps_its_own_words(
            self, monkeypatch):
        probes = [bh.BackendProbe("mpv", "/x/mpv", "mpv v0.41.0")]
        player = self._player(monkeypatch, None, probes, position=12.0)

        player._monitor_playback()

        assert "stopped early" in player._toast

    def test_a_clean_early_exit_from_a_broken_backend_says_so(self, monkeypatch):
        """Exit 0 says the stream died — but if `mpv --version` cannot run,
        that is the sentence the user needs."""
        broken = bh.classify_exit("mpv", 127, LDSO_STDERR)
        probes = [bh.BackendProbe("mpv", "/x/mpv", failure=broken)]
        player = self._player(monkeypatch, None, probes, position=12.0)

        player._monitor_playback()

        assert "libass.so.9" in player._toast
        assert "stopped early" not in player._toast
