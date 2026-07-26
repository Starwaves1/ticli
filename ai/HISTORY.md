# History

Chronological, with reasoning. Commit hashes are the source of truth — the
messages carry measurements and rejected alternatives, so `git show <hash>` is
worth reading for anything below.

Starting point: `729bfde`, a working but clunky TIDAL terminal client forked
from `odonald/ticli`. The owner's framing: *"The service works, but it's
clunky."* He brainstormed the roadmap first and approved an order: resume →
add-to-playlist → settings → search → caching → artwork → OS integration. That
order mostly held, with bug fixes and one large detour (lossless audio) folded
in as they surfaced.

---

## 2026-07-24 — foundations

### `fa62887` Resume-at-position, playlist editing, mpv IPC hardening

Three things at once, because they turned out to be entangled.

**Resume.** Reopening restores the last track at its last position, **paused —
never autoplay**. That was a locked product decision. State persists to
`~/.config/ticli/player_state.json` with atomic writes (temp + `os.replace`)
and a 10s autosave so a crash doesn't lose position.

**Playlist editing.** `y` adds the cursor track to a playlist (server-side
duplicate handling → "Already in" toast); `x` removes it when browsing one of
your own playlists. Reusable toast infrastructure came from this.

**mpv IPC hardening.** The owner reported *"really buggy behavior"* during
testing. Two tracer agents produced 11 verified root causes with measured
timings (`ai/BUGS-2026-07-24-resume-trace.md`). The significant ones:

- mpv's IPC socket takes 85–150ms to accept connections after spawn; commands
  in that window were silently swallowed, so pause did nothing while the UI
  showed paused.
- `is_paused` returned a flag with no liveness check, so a dead mpv looked
  paused and the next space skipped a track.
- The monitor could false-advance during a track change (~8–10% of manual
  changes double-skipped).
- Position was wall-clock, stamped at spawn ~0.3–0.7s before audio started,
  drifting further with every restart.

Fixes: ack-checked IPC commands, liveness in `is_paused`, a `_track_changing`
guard plus a two-dead-polls requirement, and position read from mpv rather
than the wall clock.

**Two regressions introduced and fixed in the same session**, both worth
knowing about because they were subtle:

1. Quitting during a restore saved an empty queue over the state file. Fixed
   with a `_restore_pending` latch.
2. Then the latch cleared in a `finally` even when the restore *failed*, so the
   10s autosave collapsed a 25-track queue to a single track. Fixed by clearing
   the latch only on successful attach, with a merge-position-only path while
   pending.

### `a5942f3` macOS media keys

Constraint from the owner: *"The dependency, compatibility, and mess caused on
other platforms is a no-go."* A scout agent returned **GO** with zero new
dependencies — mpv already contains Apple's `MPRemoteCommandCenter`
integration, live even in a headless `--no-video` CLI process (proven via
`sample`/`vmmap` showing a Cocoa runloop).

mpv's *default* responses are hostile (NEXT → playlist-next, STOP → quit), so
ticli rebinds the seven media keys over the existing IPC socket at priority 15
to write a `user-data/ticli/media-key` property, which the existing 0.5s
monitor tick reads. No new threads, no extra wakeups. `--force-media-title`
puts "Track — Artist" in Control Center. Gated on `IS_MACOS`; inert elsewhere.

This was the first git-worktree merge — the owner's first, walked through
step by step.

### `9e2b2aa` Config module and settings page

`~/.config/ticli/config.json`, atomic writes, corrupt file falls back to
defaults rather than raising, unknown keys preserved on save. `SETTINGS_SPEC`
is a table that drives defaults, validation, cycling *and* the settings UI, so
a new setting is one row. `--quality` became a per-run override that is never
written back.

Deliberate: logout lives outside `SETTINGS_SPEC` as a keybinding, because the
table is a pure value table and adding an "action" kind would ripple through
`coerce`/`cycle_value`/`load_config`. That precedent held for clear-cache and
PKCE sign-in later.

### `bf95d96` Logout to settings, smart previous, volume

Smart previous: >30s into a track, back restarts it rather than skipping to
the previous track — gapless mpv seek where possible, respawn as fallback.
Threshold exclusive at 30. Both the keyboard and the macOS media key route
through the same method, so they can't diverge.

### `5dd12c3` Quality tiers corrected, page size applied to search

**The quality menu was lying.** `LOW` requested tidalapi's `low_320k` and
`HIGH` requested `high_lossless` — so the top two options were identical and
true 96k was unreachable. Names now map 1:1 to tidalapi's own tiers
(`media.py:57-62`), the settings page states what each actually streams, and a
v1→v2 migration renames saved values so nobody is silently downgraded.

Search had been hardcoded to `limit=8` split 5/3/2 across tracks/albums/
artists — the real reason search felt thin. Now one request at the user's page
size, split 50/30/20, with rows a thin category can't fill handed back to
tracks.

---

## 2026-07-25 — performance, honesty, and lossless

### `bd4f95f` Input latency

The owner: *"Navigating ticli feels very laggy... General delay in input
regardless of network activity."* That last clause was the useful part — it
ruled out the network and pointed at the input/render path.

Two independent causes, both measured:

1. **Keypress → pixels was gated on Rich's 4fps refresh thread.** `Live.update()`
   with the default `refresh=False` only swaps the renderable; the terminal
   write happens on a background thread. Key *handling* took 0.0002ms; the
   paint took **231ms on average**. Painting inline: **1.1ms**.
2. **Arrow bursts were silently dropped, 9 out of 10.** `_read_key` read one
   byte, saw `\x1b`, then `os.read(fd, 7)`. With key repeat the next arrow's
   bytes are already buffered, so one read returned `\x1b[B\x1b[B\x1b[` — a
   string matching no known key, discarded entirely. Measured against a real
   pty: a 10-arrow burst moved the cursor **one row**. Holding an arrow didn't
   scroll slowly; it was *ignoring* nine presses in ten.

Also: `get_url()` moved off the UI thread (a real freeze on every track start),
play/pause repeat suppression became timestamp-based, and idle terminal traffic
went from ~20KB/s to **zero** by skipping byte-identical repaints.

Refuted with measurements: the 0.25s select timeout adds no latency (0.004ms
per press), and `_build_display()` was never expensive (0.02–0.07ms).

### `c66e285` Metadata caching

Playlists took seconds to load. They now paint from a local index immediately
and are replaced by the live fetch, which **still runs every time**. Measured
against a simulated 1.5s-per-request session, time to first paint: **1510ms →
0.5ms**.

The design principle worth keeping: **the cache is a first paint, never an
answer.** There is no TTL-based skip-the-fetch path. So the staleness window is
exactly one round trip — the same wait the old code imposed unconditionally —
and the cache can change *when* you see something but never *what* you
eventually see.

The index stores plain-text name/artists/album and `iter_tracks()` spans all
playlists, which is what later made local playlist search possible with no
schema change. TIDAL has no server-side API for searching your own playlists.

### `46c6c24` FULL cache actually downloads; instant stop on quit

See INCIDENTS #2 for the full story of a feature that had never written a byte.

Also: quitting used to save state (up to 0.2s of mpv IPC plus a file write)
*before* stopping audio, so music played on. Now: read position → stop audio →
write the file into the silence. And a cached file vanishing mid-playback
restarts the track from the network at its position rather than silently
skipping it.

### `a67e111` / `3fadef5` Settings rework

Owner-specified: cache mode split into two independent booleans (metadata,
songs), budget in whole GB defaulting to 2, type-a-digit inline numeric entry
that commits on leaving the field, a live count of cached songs, and later
usage in GB to three decimals.

Volume went to 250% — with the ceiling **discovered from the running backend**
rather than assumed per platform. Measured offline through a generated tone:
mpv gives +10.6dB at 150% and +23.9dB at 250% (its cubic curve — ~15× linear,
so it *will* clip real music, which is what the blue caution at ≥105% is for),
and needs `--volume-max` at spawn or it refuses live changes above 130. ffplay
hard-clamps at 100 and now says so on the row.

Clear-cache (`x`) deletes an **explicit list of files ticli created** — never a
directory wipe. There is a test asserting a decoy `important.txt` survives.
The owner's reasoning: *"Just in case anyone puts their 401k information into
the cache folder."* Clearing genuinely clears: a file being played is deleted
too, and playback survives (verified with real mpv — the unlink succeeded, the
file vanished, and mpv played to the end).

### `5d08445` PKCE login — and the discovery that the quality badge still lied

Research (`ai/reference/download-research-2026-07-25.md`) live-probed the API
and found: requesting `LOSSLESS` or `HI_RES_LOSSLESS` **silently returned
`audioQuality: HIGH`** — AAC-LC 320kbps, identical manifest hash to HIGH. The
account was fine (`highestSoundQuality: HI_RES`, premium); the *client* wasn't
entitled.

Cause: ticli used `login_oauth()` (device authorization grant) with tidalapi's
standard client credentials. tidalapi ships a second credential pair belonging
to a client TIDAL grants hi-res to, reachable only via `login_pkce()`, whose
own docstring says it is *"the only way how to get access to HiRes … FLAC
files."*

Implemented as opt-in (`[u]` in settings, or `--login-flow pkce`), because the
PKCE flow requires copying a failed redirect URL back into the terminal — the
redirect URI is fixed in tidalapi's config and re-sent in the token exchange,
so no localhost listener can substitute, and it must work over SSH anyway.

**The load-bearing detail:** stored tokens must record *which flow issued them*
(`is_pkce`), because `token_refresh` picks client credentials from that flag.
A record that lost it refreshes against the wrong client and the session dies
hours later, looking like a random logout.

Quality gating became evidence-based: `_note_granted_quality` records only a
*downgrade*, because being granted what you asked for says nothing about the
tiers above it. Nothing is gated until a track has played; an ambiguous answer
gates nothing. Gated tiers stay listed and dimmed with the reason, because a
hidden option looks like a missing feature.

A correction to the brief, found by the agent: the credential swap at
`session.py:475-480` is **commented out** in the installed tidalapi. It doesn't
matter — `token_refresh` branches on `is_pkce` independently — and calling it
would break a device session's refresh.

### `68f292f` Search overhaul

Tab cycles the scope (All / Tracks / Albums / Artists / My Playlists);
Shift-Tab goes back. Scrolling past the last row appends the next page with the
cursor left in place.

Two design decisions worth preserving:

- **Tab was made *not* to fetch.** It's a key people press repeatedly, and one
  request per press is the fan-out that caused the IP block. (The owner later
  revisited this with a better answer: cache each scope per session, so Tab can
  apply instantly and still cost nothing after the first visit.)
- **Pool-first paging**, which fixed a bug nobody had noticed: `session.search`
  takes one `offset` shared across all three types, so in All mode each page's
  leftovers would have been silently skipped. Results go into a pool and pages
  draw from it — correctness *and* rate-limit relief, since pages 2 and 3 in
  All mode cost zero requests.

**My Playlists** searches the local index with zero network — the feature TIDAL
has no API for, free because of the caching work.

### `c47ea3f` Album art as pixel art

Half-block Unicode (`▀`, foreground = top pixel, background = bottom), default
on, cached, toggleable. **No new dependency**, which was the interesting
constraint.

The insight: a JPEG block's **DC coefficient is that block's mean**, so
decoding only the DC terms yields the image at 1/8 scale with no IDCT and no
chroma reconstruction. A 320×320 cover becomes 40×40 — already more than a
20×10 cell grid can show. And TIDAL serves **progressive** JPEG, whose first
scan *is* the DC scan, so the decoder reads one scan and stops: **~2.5ms**.
Accuracy against ffmpeg as ground truth: max error 1/255, mean 0.06.

The whole feature cost **one** network request to build — a single
unauthenticated fetch of TIDAL's public placeholder to learn the format.

Renderings cache to disk keyed on cover id *and* cell size (the pixels *are*
the render), deliberately outside the audio directory so the song count, byte
total and eviction budget are untouched.

### `48004f1` Silent playback failure on lossless

See INCIDENTS #3. This is where FLAC was confirmed real: granted `LOSSLESS`,
`codecs=FLAC`, `bit_depth=16`, `sample_rate=44100`, and ffmpeg decoding
`flac (fLaC), 44100 Hz, stereo, s16` from the segments. Three API requests
total, spaced 16 seconds apart.

### `d31d96b` Alternate-screen repaint

See INCIDENTS, "two hypotheses the main thread got wrong". The fix — running
`Live` with `screen=True` — is structural rather than corrective: on the
alternate screen every refresh homes the cursor and writes every row, so
nothing can be stranded by a resize, by a frame that shrank, or by artwork
appearing and vanishing. Costs ~6% more bytes per repaint and no new syscalls.

Side effects, both good: the app stops wiping the scrollback it was launched
from, and hands the terminal back untouched on exit, the way `less` and `vim`
do.

New test infrastructure came with it: `vt.py`, a headless terminal model that
handles cursor moves, erase, **and reflow on resize** — the last being the part
a merely-clipping model would let the bug through. 5 of its 17 tests fail on
the previous rendering.

### `0b689e0` Scrubbing, and richer playlist search

`↑` focuses the player (it was a dead key there), `←`/`→` then seek ±10s, `↓`
or Esc releases. From a list, `↑` at the top does the same and `↓` returns the
cursor where it was. Focus is visible, never hidden state.

Details worth keeping: seeking past the end stops 2s short, because landing on
EOF makes both backends exit and the monitor reads that as "track ended". Held
arrows move the bar instantly but let at most one seek per 0.3s reach the
backend. ffplay's respawn deliberately avoids the normal play path, which would
cost a `get_stream()` per scrub *and* abandon the in-flight cache download.

Playlist search gained playlist-name matching, ranked track title > artist >
album > playlist name. Creator is deliberately unmatched — on your own
playlists it's your name on every row.

### `49db411` Artist page tabs

Top Tracks / Albums / Playlists / Suggestions, switched with Tab, each fetched
lazily on first visit and cached per artist for the session. Opening the page
costs 1 request; browsing all four costs 5; revisits cost 0 — with a test
asserting 40 rapid Tab presses still total 5 calls.

**Playlists has no direct API.** `Artist` exposes no playlist accessor and
there is no `artists/{id}/playlists` endpoint. The tab is instead backed by
`Artist.page()` — the same document listen.tidal.com renders — filtered to
`Playlist` instances. So it's real data, but its content depends on the artist:
one whose page has no playlist module shows "No playlists feature this artist",
which is a fact about the artist rather than a broken tab, and looks visibly
different from a failure.

---

### `443ddec` Search Tab applies instantly, every scope cached

The owner revisited the deliberate "Tab does not fetch" decision with the
resolution that makes both properties true at once: **cache each scope for the
session**, so Tab applies immediately and still costs nothing after the first
search.

The enabling detail, found by the agent: TIDAL applies `limit` **per type**, so
asking for `[Track, Album, Artist]` is the *same single request* a scoped
search already made — the other scopes come free. Measured and asserted in
tests: query + Enter costs 1 request; tabbing through all five scopes twice
costs 0; 40 rapid Tab presses from cold cost 1; My Playlists always costs 0.
One extra page deepens *every* scope.

Structural consequence worth knowing: `_search_results`, `_search_cursor`,
`_search_pool`, `_search_offset` and friends became **properties over the
current scope's view record**, so Tab changes which record is read rather than
copying or syncing state. `exhausted` became per-category, because one fetch
feeding all scopes means "Albums ran out" says nothing about Tracks. The
reservoir is never consumed — each scope carries its own depth into it.

Presence of a view record *is* the "has this scope been answered" flag, and the
scope row marks answered scopes with a dim `·` so "Tab is free from here" is
visible rather than implicit.

---

## In flight at time of writing

- **Responsive narrow-width layout + `v` volume overlay** — width-derived
  progress bar (currently a fixed 50 columns, which wraps catastrophically when
  narrow), hotkey hints that never split a `[key] label` pair, artwork sizing
  on width as well as height, and a volume overlay on `v` replacing the footer.

## Specified, not built

- **Downloads.** Fully researched. `d` opens a download screen; quality picker
  as a single column with the cursor pre-placed on the current setting;
  hovering shows "download now" and a size estimate. Files land in
  `~/Music/Ticli`, tagged and organized, exempt from eviction. Size estimates
  cost zero requests (duration × bitrate; −0.3% to −2.1% error on AAC — note
  FLAC is variable-bitrate, so this needs recalibration).
- **Play-count-tiered eviction.** The owner's design: a point per play, evict
  the oldest among the tracks with the fewest plays. Rationale: a 4-hour binge
  on a new playlist must not evict long-term staples, which pure LRU does. His
  refinements: stamp `time.time()` ourselves rather than trusting filesystem
  `atime` (which `relatime` makes ~daily-granular), run only when song caching
  is enabled, and exempt downloads entirely.
