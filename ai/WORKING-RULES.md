# Working rules

Constraints the code can't show. The ones marked **hard** have already caused
real damage when violated.

## TIDAL API

**Hard: at most one request per 15 seconds during development.** An agent made
53 sequential `playbackinfo` calls in under 3 seconds and got the owner's IP
blocked by TIDAL's bot detection. It escalated from `429` to `401 subStatus
4006 "Session does not have streaming privileges"`, took 60–90 seconds to
clear at the API level, and the edge block took longer. **The consequence is
not a failed request — it is the owner's music stopping.**

**Hard: on any 429, any 401 with subStatus 4006, or any bot-detection page,
stop making requests entirely and report.** Never retry. Retries extend these
blocks.

- To search, read playlists or check status at runtime, use `ticli agent
  <verb>`, never a script over the internals. It enforces the rules above in
  code; importing `credential_store` and calling tidalapi directly bypasses
  them and is how the IP block happened.
- Build against fakes. To learn tidalapi's behaviour, read its installed source
  instead of probing the live API.
- Signed stream URLs expire in ~1 hour. Fetch a track's URL immediately before
  using it, never all up front for a batch.
- tidalapi auto-refreshes an expired token **only** when the response body says
  `"The token has expired."` It will not auto-refresh a 4006. A long-running
  job must tolerate a mid-run refresh.

## Dependencies

**No new Python dependencies.** `ffmpeg` is implicit via ffplay, but mpv users
may not have it: anything using ffmpeg must degrade gracefully when it's
absent.

## Power

**No new polling loops or timers.** Piggyback on the monitor thread's existing
0.5s tick. Near-zero idle power is a product goal.

## Platforms

macOS and Linux only. Windows is deliberately unsupported (the input path is
`termios`/`tty`); don't attempt a port as a side quest.

## Testing

Tests must not touch the real network, the TIDAL token store, `~/Music`, or
`~/.config/ticli`. The suite-wide redirects live in `tests/conftest.py`;
anything new that writes at startup needs a redirect there first.
