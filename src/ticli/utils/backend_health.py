"""Why the audio backend died, in words a user can act on.

Exists because of 2026-10-05: a Homebrew upgrade moved `libunibreak` from 7
to 8 while the installed `libass` still linked against 7, so mpv could not
even load. dyld kills a process it cannot link with **SIGABRT** — return code
`-6` — and `failure()` read every negative code as "we killed it ourselves",
so the monitor fell through to the truncated-stream branch and the user saw
"Playback stopped early — the stream ended at 0:00", on every track. That
reads as TIDAL, or the network. It was neither, and the one sentence that
said so (`Library not loaded: …libunibreak.7.dylib`) was on stderr the whole
time — as the *first* line, while the toast showed the last one.

Three pieces, all pure or nearly so, so they test without a backend:

- `classify_exit` turns a return code plus everything the player said into a
  `PlayerFailure` with a stable `code`. It scans *all* of stderr: the dynamic
  linker's useful line comes first and its search path list comes after.
- `classify_spawn_error` does the same for a binary that could not be
  started at all (uninstalled mid-session, lost its execute bit).
- `probe_backends` runs each installed backend's version flag. **Only after
  a failure** — never at startup, which must not pay for a health check
  that almost always passes. It answers the question the failure alone
  cannot: is it this stream, or is the player itself broken?

Both platforms ticli runs on are covered: macOS's dyld (SIGABRT, "Library
not loaded" / "Symbol not found") and glibc's ld.so on Linux (exit 127,
"error while loading shared libraries" / "symbol lookup error").
"""

import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

# Kept in step with player.AUDIO_PLAYERS by a test rather than an import —
# player's import chain is the whole TUI.
BACKENDS = ("mpv", "ffplay")

# Each backend's spelling of "print your version and exit". ffplay's option
# parser is ffmpeg's, which takes single-dash long options.
VERSION_FLAGS = {"mpv": "--version", "ffplay": "-version"}

# How much of a backend's own complaint is quoted in a summary — a toast is
# one line. Matches player.PLAYER_ERROR_CHARS, which passes it explicitly.
DETAIL_CHARS = 90

# A version check that takes longer than this is itself a finding — a healthy
# backend answers in tens of milliseconds.
PROBE_TIMEOUT = 3.0

# Stable identifiers for what went wrong. Tests and logs key on these; the
# wording of `summary` is free to change.
BROKEN_INSTALL = "broken_install"   # the dynamic linker refused the binary
CRASHED = "crashed"                 # died on a fault signal
KILLED = "killed"                   # ended by a signal that was not ours
EXIT_STATUS = "exit_status"         # exited non-zero on its own
NOT_RUNNABLE = "not_runnable"       # could not be spawned at all
HUNG = "hung"                       # version probe never answered

# Signals that mean the program itself faulted, as opposed to being told to
# stop. SIGABRT is here because abort() is how a program gives up — dyld's
# refusal is caught before this, by its message.
_FAULT_SIGNALS = {signal.SIGSEGV, signal.SIGBUS, signal.SIGILL,
                  signal.SIGFPE, signal.SIGABRT}

# The dynamic linker, on each platform, refusing to load the binary. Group 1
# is the library (or symbol) it could not resolve.
_LINKER_PATTERNS = (
    # macOS: "dyld[6141]: Library not loaded: /opt/homebrew/opt/x/lib/x.7.dylib"
    re.compile(r"Library not loaded:\s*(\S+)"),
    # macOS: "dyld[6141]: Symbol not found: _ass_foo"
    re.compile(r"Symbol not found:\s*(\S+)"),
    # glibc: "mpv: error while loading shared libraries: libass.so.9: cannot open…"
    re.compile(r"error while loading shared libraries:\s*([^:\s]+)"),
    # glibc: "mpv: symbol lookup error: /lib/libass.so.9: undefined symbol: foo"
    re.compile(r"symbol lookup error:.*undefined symbol:\s*(\S+)"),
    # glibc: "/lib/libc.so.6: version `GLIBC_2.38' not found (required by mpv)"
    re.compile(r"version `([^']+)' not found"),
)


@dataclass(frozen=True)
class PlayerFailure:
    """One backend failure. `summary` is a sentence fit for a toast, led by
    the part that matters so a narrow terminal clips the least useful end."""
    code: str
    player: str
    summary: str
    detail: str = ""     # the most telling line the player printed, untrimmed
    hint: str = ""       # what the user can do about it, when anything


class SpawnError(Exception):
    """The backend binary could not be started. Its own type because the
    `OSError` underneath is also what every `requests` failure is, and a
    network error must not be reported as a broken player."""

    def __init__(self, failure: PlayerFailure):
        super().__init__(failure.summary)
        self.failure = failure


@dataclass(frozen=True)
class BackendProbe:
    """What one installed backend said when asked its version."""
    player: str
    path: str
    version: str = ""                         # first line of the answer
    failure: Optional[PlayerFailure] = None   # None means it answered

    @property
    def ok(self) -> bool:
        return self.failure is None


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def _said(stderr: str) -> list:
    return [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]


def _linker_refusal(lines: list) -> Optional[tuple]:
    """(line, what-was-missing) for the first line the dynamic linker wrote,
    else None. First, not last: dyld follows its refusal with a list of every
    path it tried, which is the least useful thing to show anyone."""
    for line in lines:
        for pattern in _LINKER_PATTERNS:
            match = pattern.search(line)
            if match:
                return line, os.path.basename(match.group(1).rstrip(":,"))
    return None


def _is_homebrew(player: str) -> bool:
    """Whether `player` resolves into a Homebrew prefix — the case where
    `brew upgrade` is the fix, because Homebrew is what broke it."""
    path = shutil.which(player)
    if not path:
        return False
    real = os.path.realpath(path)
    return "/Cellar/" in real or real.startswith(("/opt/homebrew/", "/home/linuxbrew/"))


def reinstall_hint(player: str) -> str:
    """How to repair a broken install of `player` on this machine.

    `brew upgrade <player>` rather than `reinstall`: it also upgrades the
    player's outdated dependencies, and an out-of-step dependency is exactly
    what broke it on 2026-10-05 (libass, not mpv, was the stale one)."""
    if _is_homebrew(player):
        return f"try: brew upgrade {player}"
    if sys.platform == "darwin":
        return f"reinstall {player}"
    return f"reinstall {player} with your package manager"


def classify_exit(player: str, returncode: Optional[int], stderr: str = "",
                  limit: int = DETAIL_CHARS) -> Optional[PlayerFailure]:
    """What a finished backend process's exit means, or None if it is not a
    failure (still running, or exit 0 — the end of a track, or a stream that
    ran dry, which is the clock's call and not this function's).

    A negative return code is a signal. ticli drops its handle on every
    process it kills, so by the time anyone classifies an exit, a signal on
    a process still held is one that ticli did not send.

    `limit` caps how much of the player's own words go into `summary`;
    `detail` keeps them whole for the log.
    """
    if returncode is None or returncode == 0:
        return None
    lines = _said(stderr)
    refusal = _linker_refusal(lines)
    if refusal:
        line, missing = refusal
        return PlayerFailure(
            BROKEN_INSTALL, player,
            f"{player} can't start — broken install, {missing} missing",
            detail=line, hint=reinstall_hint(player))
    last = lines[-1] if lines else ""
    if returncode < 0:
        name = _signal_name(-returncode)
        try:
            fault = signal.Signals(-returncode) in _FAULT_SIGNALS
        except ValueError:
            fault = False
        if fault:
            return PlayerFailure(CRASHED, player, f"{player} crashed ({name})",
                                 detail=last)
        return PlayerFailure(
            KILLED, player,
            f"{player} was killed ({name}) by something outside ticli",
            detail=last)
    if last:
        return PlayerFailure(EXIT_STATUS, player, f"{player} error: {last[:limit]}",
                             detail=last)
    return PlayerFailure(EXIT_STATUS, player,
                         f"{player} exited with status {returncode}")


def classify_spawn_error(player: str, error: OSError) -> PlayerFailure:
    """A backend that could not be started at all."""
    reason = error.strerror or str(error)
    return PlayerFailure(
        NOT_RUNNABLE, player, f"{player} can't be started — {reason.lower()}",
        detail=str(error), hint=reinstall_hint(player))


def probe_backend(player: str, path: str,
                  timeout: float = PROBE_TIMEOUT) -> BackendProbe:
    """Ask one backend its version. Never raises."""
    flag = VERSION_FLAGS.get(player, "--version")
    try:
        result = subprocess.run(
            [path, flag],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return BackendProbe(player, path, failure=PlayerFailure(
            HUNG, player, f"{player} didn't answer {flag} in {timeout:g}s"))
    except OSError as e:
        return BackendProbe(player, path, failure=classify_spawn_error(player, e))
    failure = classify_exit(player, result.returncode, result.stderr)
    if failure:
        return BackendProbe(player, path, failure=failure)
    said = _said(result.stdout) or _said(result.stderr)
    return BackendProbe(player, path, version=said[0] if said else "")


def probe_backends(players=BACKENDS) -> list:
    """Probe every backend that is on PATH, in preference order. A backend
    that is not installed is not listed: absence is not a fault."""
    probes = []
    for player in players:
        path = shutil.which(player)
        if path:
            probes.append(probe_backend(player, path))
    return probes


def describe(failure: PlayerFailure, probes: list) -> str:
    """The toast for `failure`, informed by what the probe found.

    Led by the failure, then the fix, then — only if the active backend is
    the broken one — which other backend still works, so the user knows the
    machine can play at all. A probe that found nothing wrong adds nothing.
    """
    active = next((p for p in probes if p.player == failure.player), None)
    broken = active is not None and not active.ok
    if broken and failure.code not in (BROKEN_INSTALL, NOT_RUNNABLE):
        # The exit looked like a stream problem, but the player can't even
        # print its version — that is the real failure, and the real fix
        failure = active.failure
    parts = [failure.summary]
    if failure.hint:
        parts.append(failure.hint)
    if broken:
        working = [p.player for p in probes if p.ok and p.player != failure.player]
        if working:
            parts.append(f"{working[0]} is OK")
    return "; ".join(parts)
