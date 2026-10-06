import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

# Kept in step with player.AUDIO_PLAYERS by a test, not an import: player's import chain is the whole TUI.
BACKENDS = ("mpv", "ffplay")

# ffplay's option parser is ffmpeg's, which takes single-dash long options.
VERSION_FLAGS = {"mpv": "--version", "ffplay": "-version"}

DETAIL_CHARS = 90

PROBE_TIMEOUT = 3.0

BROKEN_INSTALL = "broken_install"
CRASHED = "crashed"
KILLED = "killed"
EXIT_STATUS = "exit_status"
NOT_RUNNABLE = "not_runnable"
HUNG = "hung"

# dyld also kills an unloadable binary with SIGABRT; that case is caught earlier, by its message.
_FAULT_SIGNALS = {signal.SIGSEGV, signal.SIGBUS, signal.SIGILL,
                  signal.SIGFPE, signal.SIGABRT}

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
    code: str
    player: str
    summary: str
    detail: str = ""
    hint: str = ""


class SpawnError(Exception):
    def __init__(self, failure: PlayerFailure):
        super().__init__(failure.summary)
        self.failure = failure


@dataclass(frozen=True)
class BackendProbe:
    player: str
    path: str
    version: str = ""
    failure: Optional[PlayerFailure] = None

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
    # First match, not last: dyld follows its refusal with every path it searched.
    for line in lines:
        for pattern in _LINKER_PATTERNS:
            match = pattern.search(line)
            if match:
                return line, os.path.basename(match.group(1).rstrip(":,"))
    return None


def _is_homebrew(player: str) -> bool:
    path = shutil.which(player)
    if not path:
        return False
    real = os.path.realpath(path)
    return "/Cellar/" in real or real.startswith(("/opt/homebrew/", "/home/linuxbrew/"))


def reinstall_hint(player: str) -> str:
    if _is_homebrew(player):
        # upgrade, not reinstall: it also upgrades stale dependencies, the usual breakage (a dylib bump)
        return f"try: brew upgrade {player}"
    if sys.platform == "darwin":
        return f"reinstall {player}"
    return f"reinstall {player} with your package manager"


def classify_exit(player: str, returncode: Optional[int], stderr: str = "",
                  limit: int = DETAIL_CHARS) -> Optional[PlayerFailure]:
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
    reason = error.strerror or str(error)
    return PlayerFailure(
        NOT_RUNNABLE, player, f"{player} can't be started — {reason.lower()}",
        detail=str(error), hint=reinstall_hint(player))


def probe_backend(player: str, path: str,
                  timeout: float = PROBE_TIMEOUT) -> BackendProbe:
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
    probes = []
    for player in players:
        path = shutil.which(player)
        if path:
            probes.append(probe_backend(player, path))
    return probes


def describe(failure: PlayerFailure, probes: list) -> str:
    active = next((p for p in probes if p.player == failure.player), None)
    broken = active is not None and not active.ok
    if broken and failure.code not in (BROKEN_INSTALL, NOT_RUNNABLE):
        failure = active.failure
    parts = [failure.summary]
    if failure.hint:
        parts.append(failure.hint)
    if broken:
        working = [p.player for p in probes if p.ok and p.player != failure.player]
        if working:
            parts.append(f"{working[0]} is OK")
    return "; ".join(parts)
