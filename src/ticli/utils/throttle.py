"""Cross-process request throttle for the agent surface.

An agent once fired ~30 requests in seconds while building a playlist, unaware
of the rate-limit rules in a Markdown file (2026-08-25); the one before it got
the owner's IP blocked. This moves the brake into code.

- **Spacing.** Each request reserves a slot: under an `flock` on the state
  file, read `next_free_at`, claim `max(now, next_free_at)`, write back claim +
  interval, release, then sleep until the claimed time. Reservation-then-sleep,
  so the lock is never held across a wait and N processes serialize into N
  spaced slots instead of stampeding.
- **The trip.** A 429, or a 401 with TIDAL's subStatus 4006 ("Session does not
  have streaming privileges", the bot-detection escalation), writes a tripped
  record and every agent request then fails fast: the rule is stop and report,
  since retries extend blocks. Only a human's `ticli agent unblock` clears it.

The state file sits next to the instance lock and player state; the directory
is read at call time so the test suite can redirect it.
"""

import fcntl
import json
import os
import time
from pathlib import Path

# Same as player.STATE_DIR, kept in step by a test rather than an import:
# player's import chain is the whole TUI and `ticli agent --help` must stay instant.
STATE_DIR = Path.home() / ".config" / "ticli"

# Double the TUI's 1.0s interactive floor (SEARCH_FETCH_MIN_INTERVAL): agents are
# unattended and looping. Spacing for a handful of calls, not a bulk budget.
MIN_INTERVAL_SECONDS = 2.0


def _throttle_path() -> Path:
    """Derived at call time so redirecting STATE_DIR redirects this too."""
    return STATE_DIR / "agent-throttle.json"


class Tripped(Exception):
    """The stop is in force; carries the record."""

    def __init__(self, record: dict):
        self.record = record
        super().__init__(record.get("reason", "tripped"))


def _read_state(fd) -> dict:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 65536)
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(fd, state: dict) -> None:
    payload = json.dumps(state).encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload)


def _locked_state():
    """Open the state file and take its flock. Caller must close the fd, which
    releases the lock. Do the whole read-modify-write under one hold."""
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(_throttle_path(), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def acquire(now=time.time, sleep=time.sleep) -> None:
    """Block until this process may make one request, or raise Tripped.
    `now` and `sleep` are injectable for tests."""
    fd = _locked_state()
    try:
        state = _read_state(fd)
        if state.get("tripped"):
            raise Tripped(state["tripped"])
        current = now()
        start = max(current, float(state.get("next_free_at", 0)))
        state["next_free_at"] = start + MIN_INTERVAL_SECONDS
        _write_state(fd, state)
    finally:
        os.close(fd)
    wait = start - current
    if wait > 0:
        sleep(wait)


def trip(reason: str, detail: str = "", now=time.time) -> dict:
    """Record the stop. Returns the record in force."""
    record = {"reason": reason, "detail": detail, "at": now()}
    fd = _locked_state()
    try:
        state = _read_state(fd)
        # First trip wins: keep the original evidence.
        if not state.get("tripped"):
            state["tripped"] = record
            _write_state(fd, state)
        else:
            record = state["tripped"]
    finally:
        os.close(fd)
    return record


def tripped() -> dict | None:
    """The trip record if the stop is in force, else None."""
    if not _throttle_path().exists():
        return None
    fd = _locked_state()
    try:
        return _read_state(fd).get("tripped")
    finally:
        os.close(fd)


def unblock() -> bool:
    """Clear the trip. Returns whether one was in force. Human-only."""
    if not _throttle_path().exists():
        return False
    fd = _locked_state()
    try:
        state = _read_state(fd)
        was = bool(state.get("tripped"))
        state["tripped"] = None
        _write_state(fd, state)
        return was
    finally:
        os.close(fd)
