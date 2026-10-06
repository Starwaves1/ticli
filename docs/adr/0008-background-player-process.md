# The player runs as a background process; every client talks to it over a Unix socket

Decided now, built in stage 2. One background process owns the audio backend, TIDAL session, queue, saved state, downloads and caches, and holds the single-instance lock. The TUI, `ticli <verb>` and `ticli agent <verb>` are clients on a Unix domain socket in the state directory (mode 0600, JSON lines, stdlib only), calling the same commands `commands.py` already defines. The player pushes state changes, never polled: a connecting TUI gets one snapshot, then deltas, and computes the progress position itself (ADR-0003). The first client starts it; it exits when the last client has gone, nothing is playing (paused counts as not playing) and no download is running. No idle timer.

## Considered options

- **A control socket inside the TUI process.** Rejected: playback and downloads would still die with the terminal, and several TUIs could not attach.
- **TCP on localhost.** Rejected: reachable by any local user; a 0600 socket file is not.
