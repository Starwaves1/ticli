# The player runs as a background process; every client talks to it over a Unix socket

One background process (`python -m ticli.playerd`) owns the audio backend, TIDAL session, queue, saved state, downloads and caches, and holds the single-instance lock. The TUI, `ticli <verb>` and `ticli agent <verb>` are clients on a Unix domain socket in the state directory (mode 0600, JSON lines, stdlib only), calling the same commands `commands.py` already defines. The player pushes state changes, never polled: a connecting TUI gets one snapshot, then deltas, and computes the progress position itself (ADR-0003). The first client starts it; it exits when the last client has gone, nothing is playing (paused counts as not playing) and no download is running. No idle timer.

The same `HeadlessTidalPlayer` class is both halves: headless as the player core, and as the TUI with `remote` set, where player-level state is a mirror of the pushed snapshot and TIDAL is reached only through read commands (search, album/playlist tracks, artist sections, library playlists). A first sign-in and the PKCE paste need a terminal, so they happen in the TUI, which saves the tokens and has the player reload them. Cover art is still fetched by the TUI: it comes unauthenticated from TIDAL's image CDN, not the API.

## Considered options

- **A control socket inside the TUI process.** Rejected: playback and downloads would still die with the terminal, and several TUIs could not attach.
- **TCP on localhost.** Rejected: reachable by any local user; a 0600 socket file is not.
