# Draft: pull request to `odonald/ticli`

Material for a detailed PR from `Starwaves1/ticli`. Adapt freely — this is
written to be lifted, not pasted verbatim.

**Recommendation on `ai/`:** keep it out of the PR. It's internal working
memory — incident postmortems, agent briefs, roadmap — and it names the fork
owner's specific setup throughout. The PR should carry the code, the tests, and
the `CLAUDE.md` subsystem documentation. If upstream wants the reasoning, the
commit messages already carry it; they were written for that.

---

## Summary

This branch takes ticli from a working prototype to a polished daily-use
player. Twenty commits, **605 tests** (from 5), no new Python dependencies.

The headline change is that **lossless audio actually works** — the client was
silently receiving 320kbps AAC while its own UI reported LOSSLESS. Alongside
that: a large input-latency fix, metadata caching, an on-disk audio cache, a
settings system, album art, search filters and paging, scrubbing, an artist
page, and macOS media keys.

## Highlights

### Real lossless audio (`5d08445`, `48004f1`)

The client authenticated with tidalapi's device-flow credentials, which are not
entitled to hi-res. Requesting `LOSSLESS` or `HI_RES_LOSSLESS` returned
`audioQuality: HIGH` — AAC, with a byte-identical manifest to HIGH. Verified by
live probe.

Adds an **opt-in PKCE login** (`[u]` in settings, or `--login-flow pkce`),
which authenticates as the client TIDAL grants hi-res to. Confirmed working:
granted `LOSSLESS`, `codecs=FLAC`, 16-bit/44.1kHz, decoded by ffmpeg as
`flac (fLaC), 44100 Hz, stereo, s16`.

Two consequences handled:

- **Stored tokens now record which flow issued them.** `token_refresh` selects
  client credentials from `is_pkce`; a record without it refreshes against the
  wrong client and the session dies hours later, looking like a random logout.
  Existing tokens are migrated by a defaulted read — nobody is logged out.
- **Lossless arrives as MPEG-DASH segments**, not a contiguous file. The
  playlist tidalapi generates omits `#EXT-X-MAP`, so every fragmented-MP4
  segment is undecodable alone. ticli now builds the HLS playlist itself
  (version 7, `#EXT-X-MAP`, real per-segment durations) and passes the
  per-backend flags needed — including a length-prefixed `protocol_whitelist`
  for mpv, whose key-value lists split on commas. HLS was chosen over handing
  mpv the DASH manifest because the shipped ffmpeg has no DASH demuxer.

Quality tiers were also **off by one**: `LOW` requested `low_320k` and `HIGH`
requested `high_lossless`, so the top two options were identical and 96k was
unreachable. Corrected, with a config migration so existing users keep the
stream they had. Tiers TIDAL won't serve are shown dimmed with the reason
rather than hidden.

### Playback no longer fails silently (`48004f1`)

Both backends ran with output discarded (`--really-quiet`, `-loglevel quiet`,
`stderr=DEVNULL`). A failed start looked like a track ending, so the monitor
advanced — and a systematic failure played an entire library as silence behind
a normal-looking UI. Errors are now captured and a failed start **stops with
the backend's own message** in a toast.

### Input latency: 231ms → 1.1ms (`bd4f95f`)

Two measured causes:

- `Live.update()` defaults to `refresh=False`, which only swaps the renderable;
  the terminal write happened on a 4fps background thread. Key handling took
  0.0002ms and the paint took 231ms. Now painted inline, with `auto_refresh`
  off entirely.
- `_read_key` read one byte, saw `ESC`, then a fixed 7 more. Under key repeat
  the next arrow's bytes are already buffered, so the read returned a
  multi-arrow blob matching no known key and was discarded. Measured against a
  pty: a 10-arrow burst moved the cursor **1 row**. Now 10.

Idle terminal traffic went from ~20KB/s to zero (byte-identical repaints are
skipped), and the stream-URL fetch moved off the UI thread.

### The TUI survives being resized (`d31d96b`)

Rich's `Live` repaints by counting rows — it walks the cursor up as many rows
as the last frame was tall. Terminals *reflow* on a width change, so a 22-row
frame at 100 columns becomes up to 44 rows at 60; Rich erases 22 and strands
the rest, permanently, on every repaint. ticli now renders on the **alternate
screen**, where every refresh homes the cursor and rewrites every row. It also
stops clearing the user's scrollback at startup and restores the terminal on
exit.

Ships with `vt.py`, a headless terminal model (cursor moves, erase, and reflow
on resize) so display behavior is testable — 5 of its 17 tests fail against the
previous rendering.

### Caching (`c66e285`, `46c6c24`)

A metadata index makes playlists paint from disk instantly — measured against a
1.5s-per-request session, first paint went **1510ms → 0.5ms** — while the live
fetch still runs every time and replaces what was drawn. The cache changes
*when* you see something, never *what* you eventually see; there is
deliberately no TTL-based skip-the-fetch path.

Optional whole-track audio caching (a plain streaming HTTP GET on a daemon
thread, no ffmpeg) with a GB budget and eviction. Deletion only ever unlinks
files ticli itself created — never a directory — with a test asserting an
unrelated file in the cache directory survives a clear.

### Album art with no new dependencies (`c47ea3f`)

Covers render as half-block pixel art. The stdlib can't decode JPEG, so this
does: a JPEG block's DC coefficient *is* that block's mean, so decoding only
the DC terms yields the image at 1/8 scale — and TIDAL serves progressive JPEG,
whose first scan is the DC scan, so it reads one scan and stops. **~2.5ms per
cover**, within 1/255 of ffmpeg's output. Falls back to ffmpeg for exotic
JPEGs, and to no artwork when neither works.

### Everything else

- **Resume** at the last position, paused, on relaunch (`fa62887`)
- **Settings page** (`c`) driven by a spec table; config migrations for every
  schema change (`9e2b2aa`, `a67e111`, `3fadef5`)
- **Search**: type filters applied instantly on Tab with every scope cached for
  the session, paging on scroll, and **search within your own playlists** from
  the local index — TIDAL has no server-side API for it (`68f292f`, `443ddec`).
  One request covers every scope, because TIDAL applies `limit` per type.
- **Scrubbing**: `↑` focuses the player, `←`/`→` seek ±10s (`0b689e0`)
- **Artist page** with lazy-loaded tabs (`49db411`)
- **macOS media keys** via mpv's built-in `MPRemoteCommandCenter` — no new
  dependencies, inert on other platforms (`a5942f3`)
- **Add/remove playlist tracks** (`y` / `x`) (`fa62887`)
- **Smart previous**: >30s restarts the track (`bf95d96`)

## Known limitations

- **Windows is unsupported.** The input path uses `termios`/`tty` and selects
  on `sys.stdin`; the app cannot start there. Pre-existing, and a deliberate
  scope decision on this fork.
- The PKCE login requires pasting a redirect URL back into the terminal. The
  redirect URI is fixed in tidalapi's config and re-sent during the token
  exchange, so a localhost listener can't substitute — and it has to work over
  SSH regardless.
- Songs cached before a PKCE upgrade remain AAC; the cached copy wins over the
  network. The upgrade toast says so and points at clear-cache.

## Testing

605 tests, no network access (one loopback HTTP server), no dependency on a
TIDAL account. They deliberately assert observable reality rather than internal
state: bytes on disk and their content, escape sequences and resulting screen
state, and request counts (several assert that N rapid keypresses cost exactly
M network calls).

## Notes for review

The commit messages carry the reasoning, measurements, and rejected
alternatives for each change — `git log` is the intended companion to this
summary. Several changes were made only after a hypothesis was measured and
disproved; where an obvious-looking approach was rejected, the message says why.
