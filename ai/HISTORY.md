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

## 2026-07-26 — the data path audit, acted on

A read-only audit of streaming, caching and downloading
(`ai/reference/data-path-audit-2026-07-26.md`, zero live requests, everything
measured against a loopback server and the real cached hi-res track) produced
eight findings. What follows is the work order being executed; each finding is
its own commit so any one can be reverted alone, and each is marked **Fixed**
in the audit rather than deleted from it.

The audit's own summary of *why* these existed is worth keeping: five of the
eight were seams — two pieces of the path each behaving correctly and
disagreeing with each other. None of them is visible from inside the function
that causes it, which is why they survived a green suite.

### F1 — a leaked `.part` file was invisible to the cache, and evicted real songs

`AudioPlayer._start_download` opens `{audio_dir}/{track_id}.part` — **no
extension**, because the container is not known until the CDN's first response
header arrives; the file is renamed to `{track_id}{ext}` when it is whole.
`cache.is_owned_audio()` stripped `.part` and then *required* an audio
extension, so it answered False for every part file production has ever
written.

The consequence is not cosmetic. `total_bytes()` is a plain directory size and
does not consult the predicate, so a leaked part file — from a SIGKILL, a
crash, a power loss, anything that is not `stop()` — **is** counted against the
budget, while `owned_audio_files()` cannot see it. Measured with the real
modules at a 1 GB budget, a 1.5 GB leak beside a 0.25 GB song:
`enforce_budget()` deleted the song, kept the leak, and `clear_audio()`
returned `(0, 0)`. Once leaked bytes exceed the budget the cache can never hold
anything again, while the settings page reads `0 songs cached · 1.500 GB` —
honest about the bytes, silent about why.

The fix widens the predicate: on a `.part`, the extension is optional. The
extended `{track_id}{ext}.part` form is still accepted, because a rename
landing mid-sweep is still ours. `audio_count()` keeps excluding `.part` by
suffix, so a half-written file is still not a song.

**The tests were part of the bug.** `test_cache.py` asserted on `12.m4a.part`
in four places — a filename production never creates. They now use what the
writer writes, and a new test monkeypatches `fetch_to_file` to capture the path
`_start_download` actually opens and asserts `is_owned_audio` on *that*, so the
predicate is pinned to the writer rather than to a name somebody believed the
writer used. Three more assert the leak scenario in bytes on disk: the leak is
evicted before the real song, `clear_audio` removes it, and it is still not
counted as a song. Filed as INCIDENTS #6 — it is INCIDENTS #2's lesson again.

The `important.txt` decoy tests still pass: the predicate only widened over
names that are already `{track_id}`-shaped.

### F2 — a stream that died mid-track looked exactly like one that finished

Measured against a loopback HLS server returning `403 Request has expired`
from segment 3 of 45: **both** mpv and ffplay play out whatever they had
buffered and then exit **`0` with an empty stderr**, 12.1 s into a 176 s
track. That is byte for byte what reaching the end of a song looks like, so
`failure()` says nothing — correctly, by its own contract — and
`_monitor_playback` fell through to the auto-advance. The user heard twelve
seconds of a three-minute song and the player moved on with nothing said. If
the cause is systemic (an expired session, the network down) it walks the
whole queue like that. INCIDENTS #3 through a door its fix does not cover.

Reachable without anything exotic: a pause longer than the ~1 h life of a
signed URL, because ffplay's `resume()` respawns against the *same* expired
string and `AudioPlayer` has no session to get a fresh one with; or any
network blip mid-track.

The clock is the only witness, so `_stream_ended_early()` is the whole fix:
duration known, and the position more than `STREAM_TRUNCATED_MARGIN` (15 s)
short of it. **Stop and say so** — `Playback stopped early — the stream ended
at 0:12 of 2:56. [space] to resume` — rather than retry. The audit proposed an
automatic restart with a one-retry latch; the owner's established answer for a
stream the backend could not play is a stop with an honest toast, and a silent
retry loop against a dead URL would be the same silence with more requests.
The position is kept, so `[space]` restarts through `_play_track`, which
fetches a fresh URL.

Deliberately the **opposite** of `_track_has_time_left`'s rule on an unknown
duration. There, "can't say" means resume: a wrong guess costs one extra
spawn. Here a wrong "yes" stops the queue on a track that really did end, so
"can't say" advances exactly as before. The 15 s margin is for the same
reason — TIDAL's `duration` is metadata and disagrees with the audio by a
second or two, and the measured failure stopped 164 s short.

Tests assert what the brief asked for: the queue index does not move, no
restart is attempted, and the toast carries both times. Plus the premise, run
against the real backends over loopback — exit 0, `failure()` None, dead well
short of the end.

**Found on the way, and fixed:** `_split` in `test_buffering.py` returned the
whole 24-second track as a *single* segment (it kept only the first `sidx`),
so the segment the buffering tests delay was never requested and those
assertions were passing over a stream with nothing to buffer. Fixed to cut at
every `moof`. With 12 real segments the delayed one had to move from index 2
to 6: at the very start of a track nothing is buffered yet, so a slow segment
there stalls any player — measured, mpv reports `paused-for-cache` once at
2.1 s and then runs 21 s ahead for the rest of the track, which is the
property the test means to assert.

### F5 — ffplay had the readahead weakness mpv had just been fixed for

Bytes served on one 45-segment playlist (990 kbps, 176 s, 21.8 MB):

| backend / flags | @1 s | @3 s | @8 s |
|---|---|---|---|
| mpv, `--cache=yes --cache-secs=60` | 9.19 MB | 9.68 | 10.16 |
| mpv, no cache flags (the bug that was fixed) | 1.88 | 2.37 | 2.86 |
| **ffplay, before this** | **1.40** | 1.40 | 2.37 |
| **ffplay, `+ -infbuf`** | **21.71** | 21.71 | 21.71 |

ffplay sat at the *un-fixed* mpv level — about two segments, ~11 s — because
its read thread stops once every stream has enough packets queued
(`MIN_FRAMES` / `stream_has_enough_packets` in ffplay.c). Any segment slower
than that window is the hitch that was reported for mpv.

There is no `--cache-secs` analogue for ffplay, so the choice is between ~11 s
and unbounded; for a VOD track of known, bounded length (~30 MB hi-res)
unbounded is the right side of that trade. In `_hls_flags()` next to the mpv
cache flags it mirrors, so it is **segmented only** — on the BTS path both
backends already read the whole file at once and need nothing.

Tested by running the real ffplay against the loopback server and counting
segments requested: 12 of 12 within four seconds with the flag, 6 of 12
without, and the second of those is a control — without it the first would
pass on any track short enough to fit ffplay's default queue anyway.

### F6 — the settings page did O(N²) JSON reads on the UI thread

`downloaded_count()` and `total_bytes()` each iterated the index and called
`path_for()` per track, and **`path_for()` re-read and re-parsed the whole
`downloads.json` every call** — so one repaint was 2(N+1) reads and 2(N+1)
parses of an O(N)-sized file:

```
 50 downloads    4.54 ms per settings repaint
200 downloads   42.70 ms
500 downloads  229.48 ms
```

paid **twice a second** while the page is open, because `_repaint` builds the
display before it can decide whether the frame changed. That is the same order
as the 231 ms keypress latency the `auto_refresh=False` work existed to remove
(`bd4f95f`), on the UI thread, growing back in a new place.

`downloads.usage()` now returns both numbers from **one** index read and one
stat per entry, and the player memoises the pair, dropped by the two things
that move it: opening the page, and a download landing. Both halves, because
the linear version is still disk work and `_repaint` pays it on every idle
tick. The memo lives on the player instance rather than in the module — a
module-level one would outlive a test and leak into the next.

Semantics unchanged and deliberately so: every file is still stat-ed and the
index is still only a hint about where to look, so a track dragged to the
trash is still not downloaded.

Tests assert the reads rather than the milliseconds — a timing threshold on a
shared machine is a flaky test waiting to happen. One index read for both
numbers, no growth with the library, zero re-measures across ten repaints, and
a re-measure when the page is opened.

### F7 — 46 TLS handshakes per hi-res track

The real cached hi-res track (FLAC 24/48, 175.9 s, 29,575,234 bytes) parses to
**45 media segments plus an initialization segment**, mean 657 KB each — so a
hi-res download is 46 sequential `requests.get` calls, each building its own
Session, its own pool and its own TLS connection. Loopback HTTPS, 46 × 657 KB,
median of 5:

| | fresh `requests.get` | one `Session` | delta |
|---|---|---|---|
| no added latency | 0.250 s | 0.032 s | 7.8× |
| +40 ms per connection setup (TCP+TLS at a 20 ms RTT CDN) | 2.430 s | 0.086 s | **+2.34 s** |

One `requests.Session()` per call of `fetch_to_file`, in the same `with` as
the output handle. Per call rather than module-level: the `with` closes the
pool on every path out including the abandoned one, so an abandoned download
cannot leave sockets alive, and the cache thread and a download job never
share one.

**The test fakes had to move with it.** Six sites replaced `requests.get`,
which `fetch_to_file` no longer calls — a fake of a function production has
stopped calling is INCIDENTS #2's shape exactly. `tests/fakes.py` now has one
`patch_get` that replaces `requests.get` *and* `requests.Session().get`
together, so nothing has to remember. Asserted over a real loopback server
with `protocol_version = "HTTP/1.1"` and a per-connection counter: nine
segments, **one** connection, bytes identical; plus the pool closed on an
abandoned download, and a mid-track 404 still raising where it always did.

### The cache tracker, and the value function on top of it

Garrett's framing, and it is the reason this is one change rather than four:

> "Caching needs a cache tracker and the actual cached files. The cache
> tracker will be updated and the cached files are merely a downstream result
> of that."

`CACHE_DIR/audio.json` — its own file, not a section of `metadata.json`,
because the two are gated by different settings: this is bookkeeping about
*audio*, so it lives and dies with `cache_songs`, and turning the metadata
index off must not blind eviction. Per track: the extension, the tier TIDAL
**granted**, the size, the play count, and when it was last played.

**Tracker is the authority on intent; disk is the authority on existence.**
That second half is Garrett's standing requirement — a song deleted from the
folder by hand has to be handled durably — and it is what stops "tracker is
the source of truth" turning into "trust the tracker blindly". `reconcile()`
is where the two meet: an entry whose file is gone is dropped and the totals
corrected, a file with no entry is adopted at **zero plays and no known
tier**, and a size that moved is corrected. It runs on a daemon thread at
startup, because it is a directory listing plus a stat per file and that walk
must never happen on a paint. Either order of a crash is survivable, because
it reads both sides.

**The value function**, one place, `audio_value(track_id, playing)` →
`(plays, last played)`. Garrett's rule from the start: a point per play, evict
the oldest among the tracks with the fewest plays. Not LRU, because a
four-hour binge on a new playlist would evict long-term staples and the whole
point of counting plays is that it cannot. His refinement held too: the
timestamp is **ours**, stamped at play time, never the filesystem's `atime`,
which `relatime` makes roughly daily-granular.

A play is counted on the monitor's existing 0.5 s tick, once per `_play_gen`,
after `PLAY_COUNTS_AFTER` (30 s, or half of anything shorter). **A skip is not
a play** — counting one at track start would let a shuffle through a hundred
previews out-score a staple, which is the exact thing the rule exists to stop.

**Admission: refuse only under pressure**, as decided. With room in the budget
nothing is refused and behaviour is exactly as before. When the cache is full,
the candidate has to beat the cheapest resident; a refused track streams
without being cached, which is byte for byte the `cache_songs`-off path.

The freeze problem, and the rule chosen for it. A brand-new track has zero
plays, so a naive comparison refuses *everything* once the cache is full and
the cache freezes into whatever it held that day. The rule here is
`playing=True`: **the song being listened to counts the play it is earning
right now.** So it displaces the oldest *other* one-play track and nothing
else — the same rule, honestly applied to a song that is actually being
played, and no special case. It needs no scratch tier, and it is deliberately
expressible at any moment: moving the decision from the start of a track to
its end (where "was it actually listened to?" is answerable) is a change of
*caller*, not of rule. `_admit()` is that one caller.

### F3 — every play of a cached track paid a request it threw away

`_play_track` checked `downloads.path_for()` before asking for a stream URL
but never checked the cache, because `play_url` only discovered the cached
file *after* it had been handed a URL — and then ignored the URL. So replaying
a 20-track cached playlist cost **20 `playbackinfo` requests** thrown away,
plus up to 20 more from the prefetch, against the exact endpoint whose burst
rate got the owner's IP blocked (INCIDENTS #1).

`_local_copy()` now answers "is there a copy on this disk good enough to
play?" before anything is asked of TIDAL, for both tiers, and
`_maybe_prefetch_next` asks the same question before spending its request.

Which makes the *quality* question unavoidable, and both wrong answers matter.
A copy stored **below** the tier now selected is skipped, so the track is
fetched at the quality that was asked for — that is Garrett's "songs should
re-download at higher quality when played again". A copy stored **above** it
is kept, because being temporarily set to LOW must never destroy a hi-res
copy. A copy whose tier was never recorded counts as good enough: unknown is
not evidence of a downgrade, and re-fetching a whole library on the strength
of a missing field is not something to do unasked.

Two smaller things that fall out. `_stream_description()` returns `(url,
granted tier)` from the one request `_stream_url` already made, which is where
the tracker's tier comes from. And a re-fetch that lands under a *different*
extension unlinks the copy it replaced — `_cached_audio_path` globs by stem,
so the stale file would otherwise still be a possible answer.

Known cost, stated rather than hidden: the quality gate learns from stream
descriptions, so it learns nothing on a cache hit. It still learns on every
cache *miss*, which is every new track; only a user whose entire library is
cached would notice.

### F4 — downloading something already cached re-fetched all of it

`_start_download_job` went straight to the network. Play a hi-res track and
press `[d]`: one API request and ~30 MB fetched again for bytes two
directories away. The two tiers are separate in lifetime and ownership, not
in bytes.

`_promote_cached_copy()` copies the cached file into the download's staging
path, after which the existing rename, tagging and record steps run unchanged.
Three conditions, each of them a way of getting it wrong: the tracker has to
know the tier the cached copy was **granted** (`.m4a` is AAC-HIGH on a
device-flow session and FLAC-in-MP4 on a PKCE one, and the names are
identical); it has to be an **exact** match, so nothing is promoted upwards or
downwards; and the file has to be there at the moment of use. Anything else
falls through to the network.

The download index now records `granted` beside `quality` for the same reason
— `quality` is what the user asked for and what the screen says, `granted` is
what TIDAL served and the only one that can be compared with anything.

### Re-fetch everything at the current quality, and two tiers that read as two

Three requests from Garrett, and they turned out to be one screen.

**`[R]` re-fetches every local copy at the tier currently selected** — both
tiers, because he asked for "specifically downloaded songs as well as cached
songs". Downloads go back into `~/Music/Ticli` through the same
`_download_to_music` the download screen uses (tagged, and the old file
removed if the container changed); cached songs go back into the cache.

This is **the most rate-limit-dangerous thing in the app**, and the incident
it could repeat is the worst one in this project's history — 53 `playbackinfo`
calls in 2.8 s got the owner's IP blocked and his music stopped mid-session.
So the design question was not "how fast can this go" but "how do we make it
impossible for a user to do that to themselves". Four answers, all structural:

* **Serial.** One track, one thread, no fan-out, nothing to tune.
* **Paced.** `REFETCH_MIN_INTERVAL` (2 s) between the *starts*, so a run of
  instant failures cannot become a burst. A track is two requests (resolve,
  then stream), so the ceiling is about one request a second and the sustained
  rate is far below that — an order of magnitude under the 19/s that caused
  the block.
* **Interruptible.** A generation counter, bumped by `Esc`, checked before
  every track *and* passed into `fetch_to_file` as its `abandoned` callback,
  so a cancel lands inside a chunk read rather than at the end of a 30 MB
  file. On this page `Esc` stops the run rather than leaving, so the key that
  starts the only long job here is also the key that stops it.
* **Stop, never retry, on evidence of a block.** A 429, or a 401 with
  subStatus 4006, ends the whole run and says so in red. Retrying is what
  turned a rate limit into an edge block.

And opt-in twice: a key, then a confirmation that says how many songs, roughly
how many bytes and roughly how long *before* anything is fetched. The estimate
is the size already recorded against each copy, so it costs nothing — asking
TIDAL for real sizes would be one request per track, which is the pattern that
caused the incident. Copies already at the target tier are skipped and counted,
because spending a request to be handed back the file you already have is the
opposite of the point.

**The two tiers now read as two rows of one small table:**

```
   Cache      9999 songs · 12.000 GB of 2.000 GB   [x] clear
   Downloads  9999 songs · 12.000 GB · not counted against the budget
   /Users/garrett/Music/Ticli   [R] re-fetch all at HIRES
```

Aligned, so the two numbers can be read against each other — which was the
point of the request. Each row carries only what is true of its own tier: the
cache has a budget and an `[x]`, the downloads have a folder and neither,
because nothing in ticli deletes a track somebody asked for and their bytes are
not the cache's to reclaim. `of`, not `/`, because the budget can be 0 and a
fraction with a zero denominator reads as broken. Three decimals, as before.

The folder is now the **full absolute path** — `expanduser()`, deliberately
not `resolve()`, which on macOS rewrites `/Users/garrett/…` to
`/System/Volumes/Data/Users/garrett/…` through the firmlink: accurate and
unreadable. `[R]` shares that line, and which of the two goes first depends on
what fits, with the tie-break stated: a path that runs off the end is a
truncated path, an action that runs off the end is a feature nobody can find.
A run in progress takes the line for the same reason, only more so.

Fits 80x24 with `[x]`, `[o]`, `[u]` and `[R]` all on screen and three settings
rows, and overflows at no size (checked 40x15 through 120x40). Below 80x24 it
is degraded and that is explicitly fine.

The settings readouts are cheap by construction now: the cache's from the
tracker, the downloads' from one memoised index read.

### `[h]` puts the controls away until you do something other than listen

The owner's ask, verbatim: *"add little h to hide button which only hides
instructions until any valid key is pressed except for arrows or space"*. He
wants to look at the player without the cheat-sheet under it, keep skipping and
navigating without it coming back, and have it return the moment he does
anything else — because that is when he might want to read it.

**One rule, checked once, before everything else in `_handle_key`.** Hidden,
any key that is not an arrow or the spacebar un-hides. Not hidden, `h` hides.
A second `h` therefore un-hides, which is a toggle that falls out of the rule
rather than a second rule.

**Un-hiding does not consume the key.** `s` restores the footer *and* opens
search, in that order. This is deliberately unlike scrub focus, which consumes
`←`/`→`: there the two meanings compete for one key and only one can win, while
here nothing competes — `h` is the only key this feature binds and no screen
bound it before. A key you have to press twice is worse than a footer that came
back a moment early.

**A key that does nothing un-hides too**, and this is the one place the
decision went against the brief's leaning. Un-hiding only on a *bound* key
needs a second copy of the mode dispatch to say which keys those are, and that
copy is exactly the hand-maintained duplicate `609e423` deleted from the
footer — it would go stale the first time anyone added a binding. It is also
the better behaviour: you pressed something, nothing happened, and the answer
to why is the row that just came back. `[z]` on the player screen is a test.

`k` — the other play/pause key — is deliberately *not* in the hold set. The
rule the user is told is `[h] hide` plus "arrows and space"; an alias that
behaves differently from every other letter is a rule nobody can read off the
screen.

**Hidden is a footer with no hints in it**, which is the same answer the mini
player already gives, so nothing downstream needed a flag: the blank row above
the footer, the rows the body gets back, `_Fit.relax` and the identity crop all
follow from one empty list. In an 80x20 queue the freed rows go straight to the
list, which is measured rather than asserted (`test_the_body_gets_the_rows_back`
counts rows on the screen before and after).

**`[h] hide` is a `Hint` like any other**, appended in `_mode_hints` rather than
into eight branches, at rank 9 — above every other hint in every mode, so it is
the first thing a narrow window drops. At 40x9 the footer is `[space] [←/→] [s]
[v] [m]` and `[h]` has already gone. The one cost: the settings footer now runs
to two rows at 80 columns instead of one, which is what the fit's two hint rows
are for and which the 80x24 test was updated to state.

Everywhere the footer is drawn, so: player, browse, artist, queue, playlists,
the picker's list, download, settings. **Not** the three screens where `h` is
the letter h — a search query, a settings row being typed into, the picker's
new-playlist name — which is `_can_open_volume()` reused rather than restated;
not the mini player, which has no footer; and not under the volume overlay,
whose `←/→ adjust` is a control rather than a cheat-sheet, and hiding beneath
it would be a change nobody can see until they close it.

Transient by construction: an instance attribute, not in `SETTINGS_SPEC`, not
in the saved state, gone on restart. No new threads, timers or requests — it is
a bool read by the renderer.

136 new tests, all through `vt.py`: what is on the screen before and after each
keypress, plus the no-overflow matrix re-run with the footer hidden (every mode
× 6 widths × 4 heights × 2 titles, and every size from 30x6 to 140x44).

---

## 2026-07-27

### A downloaded song is never re-fetched for a tier you did not ask it for

Garrett: *"Check that a downloaded song (not cached song) is never
unconsentually upgraded in quality when it is streamed at a higher quality. It
should just stream from download and not stream data."*

`_local_copy` applied **one rule to both tiers**: a copy stored below the tier
currently selected in settings is passed over, and playback falls through to
the network. For the cache that is right and has not changed — it is
machine-owned, disposable, and a stale tier there is worth one request. For a
download it was wrong. Downloading an album at LOSSLESS and later setting the
app to HI-RES made every one of those songs stream: the file he deliberately
put in `~/Music/Ticli` was passed over and his data was spent on audio already
on his disk. (His file was never overwritten — that half was already right —
but the fetch is the thing he did not want.)

**The rule now: a downloaded copy always wins.** It is played whatever the
quality setting says, and no tier comparison happens on the download tier at
all. Quality changes for a download when the user asks, with `[R]` on the
settings page, which is the consent and already exists.

The honesty half, which is not optional here: a download can now play *below*
the tier the settings page shows, so the player's badge stopped being the
setting's label and became **what is really playing** — `LOSSLESS ·
downloaded`, or a bare `downloaded` when the index never recorded a tier,
because "we do not know" is honest and the setting's label would not be. It
rides the status line that was already drawn, next to `Queue: 3/12`. **Not** a
toast: this is true of every track of a downloaded album, and a notice per
track is nagging. No setting for it, and the setting's label comes straight
back the moment the bytes come off the network.

Mechanically it is one function split in two. `_local_source(track)` returns
`(path, badge)`; `_local_copy` is the path half, kept for the prefetch, which
only wants to know whether the network is needed. `_tier_is_enough` is now the
cache's rule alone. The badge is assigned whole, behind the same `_play_gen`
check as the play it describes, so a late-landing start cannot label the wrong
song. Both tiers are still verified by `stat` at the moment of use, so a file
deleted by hand still falls through to the network.

Two second-order effects, both checked. The quality gate (`_note_granted_quality`)
learns nothing on a local-copy path because no stream is described — already
true and already documented; the change only turns a stream into a local play
for a track the user had already fetched at a tier the gate had seen, so
nothing widened. And a download at a *lower* tier now shadows a *higher*
cached copy of the same track — which cannot arise in practice, because
`_drop_superseded_cache_copy` deletes the cached copy the moment a download
lands, and is the right answer anyway: the user's file wins.

12 tests (`TestADownloadedTrackIsNeverUpgradedBehindYourBack`), asserting the
observable thing: what the audio backend was handed, and that the network was
touched zero times — enforced by making `get_stream`, `_stream_description`,
`_stream_url` and `requests.get` all fail the test. Every stored tier against
every setting, the bytes on disk being the bytes played, the prefetch spending
nothing either, a hand-deleted download still falling through, the cache rule
proved *not* to have moved with it, and the music folder byte-for-byte
unchanged by playing. 1365 in the suite.

---

*(The two sections that used to close this file — "in flight: responsive
layout" and "specified, not built: downloads, eviction" — are stale: all three
shipped between 2026-07-25 and 2026-07-30. See DECISIONS.md for the built
state; corrected 2026-08-02 under the "fix what is now wrong" rule.)*

---

## 2026-08-02

### The state file can no longer crash startup by being a JSON list

`~/.config/ticli/player_state.json` containing `[]` — valid JSON, wrong shape —
crashed `_restore_state` with an `AttributeError`: it caught only
`JSONDecodeError`/`OSError` around `json.loads` and then called `.get()` on
whatever parsed. Every other loader in the codebase (`load_config`,
`cache._load`, `cache._load_tracker`, `downloads.load_index`, even
`_remember_last_playlist` three screens up in the same file) has the
`isinstance(data, dict)` guard and honors "missing or corrupt → defaults,
never raises". This one reader missed it.

The fix is one method, `HeadlessTidalPlayer._read_state_dict()`: read
`STATE_FILE`, return the dict, or `{}` for missing/`OSError`/
`JSONDecodeError`/`UnicodeDecodeError`/non-dict JSON — the same contract,
stated in its docstring. All three readers (`_restore_state`,
`_merge_position_into_saved_state`, `_remember_last_playlist`) now go through
it, each keeping its exact prior semantics: restore over an empty dict returns
before spawning the fetch thread, the position merge finding no `track_ids`
writes nothing (a `[]` file stays byte-for-byte `[]` — asserted), and the
playlist pin still starts from `{}` and heals the file into a dict. The method
is deliberately the *single* place state is read, mirroring
`_write_state_file` on the write side — a later change serializes access
around this pair, and a test can monkeypatch it trivially.

**Rejected:** pasting the `isinstance` guard into each reader inline — three
copies of the contract is how one of them went missing in the first place, and
it leaves no seam for the planned serialization. Also rejected: folding any
pacing or locking in now; later stages own that, this commit is the guard
alone.

Six tests (`TestNonDictStateFile`), asserting the observable thing: a `[]`
file and a bare-string file leave the queue unattached and cost **exactly 0**
`track()` fetches on a counting fake session; a following `_save_state`
writes a well-formed dict that a second player restores cleanly, round-tripped
through the real file; the pin lands over a `[]` file and the file is a dict
afterwards. Verified red first: the three crash tests fail with the exact
`AttributeError` when the fix is stashed. 1404 in the suite.

### Session restore stopped spending one request per saved track

The worst remaining API burst in production was the launch path. `_save_state`
wrote the queue as bare ids, so `_restore_state` could only rebuild it by
calling `session.track(tid)` for every saved id — serially, unpaced,
swallowing every failure including 429s (`except Exception: pass` per track),
and fetching on after the user had moved on. A queue that is a whole playlist
(`self._queue = list(self._browse_tracks)`) made that hundreds of unpaced
requests at every launch — the shape of ai/INCIDENTS #1, fired by opening the
app.

**Measured: a saved N-track queue cost N `track()` requests at every launch;
it now costs 0.** The codebase already had the answer in record-shaped rows.
`_save_state` writes `"tracks": [track_record(t) …]` next to the ids (the
single-current-track fallback mirrors it), and `_restore_state` builds
`CachedTrack` shims from them and attaches queue, index and current
**synchronously** — run() calls it before the UI loop starts, so there is no
thread and no `_restore_pending` latch on this path, and full saves stay
enabled. The current track appears paused at its saved position exactly as
before (same `1 <= position < duration - 2` clamp, never autoplay), and
`_play_track`'s existing `_resolve_track` swap fetches the real track — one
request — when a track is actually played. A save straight after a restore is
lossless: the shims flatten back through `track_record` unchanged, asserted
through the real file. `"track_ids"` stays in the file on purpose — it is
what every pre-record build reads, so a downgrade still resumes.

The legacy path (a file with only ids — every pre-upgrade file) still fetches
on a daemon thread, but frugally and obediently now:

- **Paced.** At least `REFETCH_MIN_INTERVAL` (2.0 s) between request
  *starts* — the bulk jobs' floor, reusing the constant. The sleep is one
  module function, `_restore_sleep`, so tests fake the wait and still prove
  the floor was asked for (each recorded gap ≈ 2.0 s) with no real
  two-second sleeps in the suite, and there is no module state to leak.
- **Stops dead on a block.** Any exception `_looks_rate_limited` recognises
  ends the whole restore: no further requests, no attach, latch left set (so
  full saves stay suppressed and the file keeps the whole queue), and a
  toast in the bulk job's words — "TIDAL is rate-limiting — restore stopped.
  Nothing will be retried." Measured in the test: a 429 on the 2nd fetch
  means exactly 2 requests ever.
- **Stands down when superseded.** `self.running and self._restore_pending`
  is re-checked before each fetch, *after* the pacing wait (the widest
  window), so a radio started mid-restore stops the fetching itself, not
  just the attach — and quietly, because nothing went wrong.

The legacy path retires itself: the first full save of a session writes
records next to the ids.

**Consumer audit**, since `_current_track` and queue rows can now be shims
until played: the display builders (player, mini, queue, up-next, browse,
artist) are getattr/hasattr-based and want exactly the shim's fields;
artwork's `cover_id_of` already documents answering None for cached rows (no
art until played, same as cached browse rows); like-toggle, add-to-playlist,
`_merge_position_into_saved_state` and `_download_mark` use only `.id`;
download targeting resolves in `_download_plan`, prefetch in
`_maybe_prefetch_next`, playback in `_play_track`. **One real gap: track
radio.** `_start_track_radio` called `track.get_track_radio` directly, which
a shim does not have — a caught AttributeError presenting as an untrue
"Radio unavailable" toast. It resolves first now, the established pattern.
(The window existed before this change — `_current_track` is briefly a
cached row right after playing one — but a record restore made it
indefinite.)

What was *not* done, and why:

- **No cap on the saved queue size.** With zero requests there is nothing to
  cap; a 500-track queue is ~60 KB of JSON.
- **Rejected pacing-only for the new format.** Pacing N launch requests
  still spends N requests on a list the user may never touch, and at 2 s a
  track a 300-track queue would take ten minutes to finish attaching.
  Records make the whole question disappear.
- **Left BUGS #6's queue-shift wart on the legacy path.** A non-rate-limit
  failure still drops that track and shifts indices there. Records moot it
  for every file this build writes, and the legacy path is one save away
  from extinction — placeholder machinery for it would be dead code in a
  week. (Status corrected inline in the BUGS file.)

Adapted deliberately: `test_a_save_after_a_bad_file_writes_a_dict_a_second_
player_restores` now exercises the record path — its intent (heal, then
restore) is format-independent — and the id-only fixtures elsewhere in
test_resume.py now pin the legacy path, which is still real for pre-upgrade
files.

One hardening note: the record path runs synchronously with no thread-level
catch-all around it, so the fields it does arithmetic on (`queue_index`,
`position`, the record's `duration`) are type-checked and read as their
defaults when corrupt — the same "never raises" contract the non-dict state
file fix set, which the legacy path only met by accident of its thread's
`except`.

11 new tests (10 in test_resume.py, 1 in test_radio.py), all counting fake
sessions and real state files: a record file restores queue, index, title,
duration, artist and position with **exactly 0** `track()` calls, then play
resolves **exactly once**; a live queue round-trips through the file with 0
requests and the bytes still carry `track_ids`; save → restore → save leaves
the file's queue identical; corrupt record fields read as defaults, not a
crash; an unusable "tracks" entry falls back to the id fetch; a legacy
file's fetch gaps each ask for the full 2.0 s floor; a 429 on the 2nd fetch
makes exactly 2 calls ever, attaches nothing, keeps the latch, shows the
toast, and the suppressed save keeps the file whole; a supersede landing
inside the pacing wait stops after 1 call with no toast; radio from a
restored shim resolves and starts. Verified red first with the player.py
change stashed: the record restores fail, the 429 halt fails, the record
mirror is a KeyError, the radio test fails 0 == 1. 1415 in the suite.

### Player state writes are serialized, the tracker fix's sibling

`player_state.json` had the same disease d5316e7 cured in the cache tracker:
several read-modify-write writers on different threads with nothing holding
them apart. `_save_state` runs on the monitor thread every 10 seconds and on
the main thread at shutdown; `_merge_position_into_saved_state` runs under it
while a restore is pending; `_remember_last_playlist` runs on the
add-to-playlist background thread. Each reads the file (or the in-memory
fields), edits, and replaces the file whole — so an autosave landing between
the pin's read and write was clobbered by the stale copy the pin had read
(fresh queue and position gone from disk; a crash before the next autosave
loses up to 10 seconds of session), and the reverse erased the pin the user
had just watched a toast confirm.

The fix reads as the tracker's sibling on purpose. One `threading.Lock`
(`_state_lock`) held across each *complete* load-modify-save cycle — the read
included, because the stale read is what loses the other writer's update; a
lock around the write alone would fix nothing. It is a leaf taken exactly
once per cycle: `_save_state`'s restore-pending branch used to call
`_merge_position_into_saved_state`, and a plain Lock is not reentrant, so the
merge (and the full save) split into unlocked `_locked` halves with the lock
taken in the public wrappers — the shape `_mutate_tracker` set. Reads
(`_restore_state`, any bare `_read_state_dict`) stay lock-free and stay
right: `_write_state_file`'s temp+rename replaces the file whole, so a reader
sees one generation of it, never a torn one. WORKING-RULES' blanket "No
locks" bullet was factually stale since d5316e7 and now states the refined
convention: in-memory reads never locked, a multi-writer load-modify-save
cycle over one on-disk JSON file always is (tracker, player state — and
general enough to cover the downloads index when it joins).

Measured: with the lock neutered (restructure kept, `nullcontext` for the
lock), both race tests fail flat five runs out of five with the exact
lost-update symptoms — the pin's stale read resurrects a previous session's
`track_ids [9]` over the fresh `[1, 2]`, and the merge's `position 77.0`
rolls back to `42.5`. With the lock, green every run. Also verified red with
the whole player.py change stashed.

4 tests (`TestStateWritesAreSerialized` in test_resume.py), siblings of
test_cache.py's `TestTrackerWritesAreSerialized` down to the technique:
`_run_together` re-raises thread errors and calls a still-alive thread a
deadlock, `_widen_the_window` sleeps inside `_read_state_dict` so the losing
interleave happens every run instead of once in a thousand (the pin test adds
an Event so the autosave provably lands inside the pin's window). All assert
the final bytes of the real file: pin beside autosave keeps both; merge
beside pin keeps both; shutdown beside a monitor save — both routed through
the restore-pending merge branch, the one where a reentrant acquire would
deadlock — finishes and leaves a well-formed file carrying one write's whole
truth; and a structural test that every path reaching `_write_state_file`
(full save, merge, pin, and the merge reached through `_save_state`) already
holds `_state_lock`.

Out of scope, same rule as the tracker's: the cross-*process* race — two
ticli instances sharing one state file — remains accepted; the loser loses
that process's most recent save and the file stays well-formed either way.
Also rejected: an RLock (would paper over a double-acquire instead of making
call structure explicit — the tracker precedent chose leaf discipline), and
locking `_restore_state`'s read (it is not a read-modify-write; a lock there
buys nothing and puts a disk read behind a writer). 1419 in the suite.

### Download index writes are serialized — the third of the family

`downloads.json` was the file WORKING-RULES' lock rule already named as next
("general enough to cover the downloads index when it joins"), and it had
joined: `record` and `remove` are both `load_index` → mutate → `_save_index`
with nothing holding them apart. The bulk runner's careful design — every
`record` callable runs on its own single writer thread (`_PacedRun._flush`) —
serializes the runner against *itself* and nothing else, and since 9b51224
the downloads list's `[x]` runs `remove` on the UI thread, reachable while a
run is going. Losing the race one way resurrects a deleted row, which is
mostly harmless because every read stats the disk. The other way a
just-finished download's row vanishes: the file exists in `~/Music/Ticli`
but is unindexed, shows as not downloaded, and would be re-downloaded — an
orphan in the one tier where nothing may clean up.

Same cure as its siblings (d5316e7, the player state): a module-level
`_index_lock` and `_mutate_index(change)` mirroring `_mutate_tracker`, the
lock held across the whole load-copy-change-save cycle — the read included,
because the stale read is what loses the other writer's update — with
`change` returning False for "nothing moved" to skip the write. `remove`'s
single unlink happens *inside* the cycle: one syscall on one exact path is
not the directory walk the tracker keeps outside its lock, and moving it out
would re-open a window where the row and the file disagree. Semantics are
preserved exactly — row gone + True when the file was already missing, row
kept + False when a real file cannot be unlinked. Reads (`load_index`,
`path_for`, `present`, `usage`) stay lock-free: the file is replaced whole
and every load parses a fresh dict. The runner's single-thread commit design
stays untouched — it still keeps commits ordered and off the fetch workers —
but the lock is the guarantee now, not the scheduler, and the comments in
player.py say so instead of claiming the thread alone was the defence.

Measured: with the lock neutered (`nullcontext`, restructure kept), all four
new tests fail flat five runs out of five — the record-beside-remove pair
ends with the fresh row erased from the file in both start orders, and a
four-thread hammer of 40 distinct `record`s leaves **7 rows of 40** in the
final file. With the lock, green every run; also verified red with the whole
src change stashed.

4 tests (`TestIndexWritesAreSerialized` in test_downloads.py), siblings of
`TestTrackerWritesAreSerialized` down to the technique: `_run_together`
re-raises thread errors and calls a still-alive thread a deadlock,
`_widen_the_window` sleeps inside `load_index` so the losing interleave
happens every run. All assert the final bytes of the real `downloads.json`
and real files under a tmp download root: record-beside-remove in both
orders (new row present, deleted row gone, deleted file unlinked), the
hammer (all 40 rows with their exact paths and byte counts), and a
structural test that both writers reach `_save_index` holding `_index_lock`.

Out of scope, same rule as the tracker's and the state file's: the
cross-*process* race over `downloads.json` remains accepted. Rejected:
locking the readers (nothing to gain — the file is replaced whole — and it
would put a disk read behind the UI thread's writer), and having `remove`
unlink outside the lock (the tracker precedent is about directory *walks*;
splitting one exact unlink from its row's removal is what a torn delete
looks like). WORKING-RULES' precedent list now names `_index_lock` alongside
its two siblings. 1423 in the suite.

### The record shims now deliver the "corrupt → defaults" contract they claimed

Adversarial verification of the record-shaped restore (4dfaee2) reproduced a
hole in its stated contract. The restore's comment promised "corrupt →
defaults, never raises", but the sanitization only covered the state file's
own scalars (`queue_index`, `position`) and the dict-with-id row guard — the
record's *fields* were passed to `CachedTrack` raw, and two corrupt shapes
hard-crashed the app:

- `tracks=[{"id": 1, "duration": 200, "artists": 5}]` — `TypeError: 'int'
  object is not iterable` iterating `artists` inside `CachedTrack.__init__`,
  synchronously inside `run()` with no catch-all: the app cannot start.
- `tracks=[{"id": 1, "name": "Track 1", "duration": "long"}]` — the restore
  "succeeds", but the corrupt string rides the shim and the **first frame**
  crashes instead (`duration > 0` in `_build_progress_line`; `_seek_by`
  identically). The branch's own corrupt-fields test blessed exactly this
  fixture, asserting only that `_restore_state` does not raise — the crash
  had moved downstream, not gone.

What makes both must-fix rather than edge-case: the corrupt file *persists*.
Neither path deletes or rewrites it before crashing, so it is not one crash,
it is a crash at every launch until someone deletes the file by hand.

The fix landed at the **shim layer** — `CachedTrack.__init__` and
`CachedPlaylist.__init__` now guarantee every field its documented type
(`name` non-empty str else "?", `duration`/`num_tracks` int-or-float-not-bool
else 0, `artists` only the non-empty str elements of a list — a number in an
artists list is corruption, not an artist, so it is dropped, never
stringified — `album`/`creator` non-empty str → `_Named` else None, `id`
verbatim since it is only compared and resolved, and a non-dict record reads
as all defaults). **Rejected: sanitizing in `_restore_state` only.** The
metadata cache builds the same shims from the same record shape out of
`metadata.json` and had the same exposure; a per-caller copy of the contract
is how one goes missing (INCIDENTS #6, and 530adf4's own commit message:
"three copies of the contract is how one of them went missing"). One place
both consumers import. The restore's dict-with-id row guard is untouched —
rows without an id still fall back to the id fetch.

`search_history` had a pre-existing hole of the same class, reproduced too:
530adf4's guarded reader still sliced before checking shape, so
`{"search_history": {"not": "alist"}}` raised `TypeError: unhashable type
'slice'` at every launch. A non-list history now reads as `[]` and non-str
elements of a list are dropped, before the `[:200]`; the write side is
unchanged (it slices what memory holds, now guaranteed a list of strings).

An honest note for the record: 4dfaee2's hardening claim was narrower than
its wording. "The fields it does arithmetic on are type-checked and read as
their defaults" was true of the scalars it named and untrue of the record's
own fields — which are also "fields it does arithmetic on", one frame later.

Also corrected in this commit, both found in the same verification pass:
`_PacedRun`'s "Lock-free" bullet still claimed the single drain thread was
what stopped concurrent index writes losing rows — false since f9a38e8,
whose commit updated the two sibling comments but missed this one; it now
says what `_flush`'s does (the lock is the guarantee, the thread keeps
commits ordered and off the fetch workers). And WORKING-RULES' amended lock
rule overstated its lock-free half — shared in-memory reads are not "never"
locked: `_reclaim_deferred` (3cd3720) is an in-place-mutated set and locks
every touch, reads included. The rule now states the real convention —
whole-object-replaced state reads lock-free; state that must be mutated in
place is locked on every touch — with the reclaim set named as precedent.

18 tests. In test_cache.py, field-by-field coercion asserting the resulting
values and types, never just "no raise": artists 5 and `[5, "X", ""]`,
duration "long" and True (bool passes `isinstance(int)` and must still read
0), name 5, album 7, nested junk, non-dict records, and the playlist
siblings. In test_resume.py, the corrupt-record test now pins the whole
journey for both evidence fixtures — restore → shims carry typed defaults →
the real `_build_player_display` renders ("--:--" for the unknown duration,
"?" for the absent name) → a following `_save_state` writes the sanitized
bytes, asserted against the file, so the corrupt file heals instead of
crashing every future launch — plus the search-history shapes (dict, int,
list with junk elements → the str-only list, capped at 200). Verified red
first with the src change stashed at f9a38e8: artists=5 fails with the exact
TypeError inside `CachedTrack`, the dict history with the unhashable-slice
TypeError, duration "long" rides the shim uncoerced and the display builder
crash was reproduced directly. 1441 in the suite (before the merge with the
tier-renaming work below; the merged suite's count is in ai/README.md).

---

## 2026-08-02 — quality tiers renamed to TIDAL's own: LOW / MEDIUM / HIGH / MAX

Owner decision, from a screenshot of the official app's quality screen (Low
96k · High 16-bit/44.1 · Max up to 24-bit/192): ticli's tier names must match
TIDAL's player, with the 320k AAC rung — which the app files under Low's
bitrate dropdown rather than naming — surfaced as **MEDIUM**. The old names
were tidalapi's wire values; the README had documented the pre-`5dd12c3` v1
scheme ever since and contradicted itself ("HIGH — lossless FLAC" beside
"LOSSLESS — 16-bit FLAC"), which is what prompted the whole change.

**Research first** (Opus 5 subagent, read-only): tidalapi 0.8.11's `Quality`
enum values *are* the wire protocol (`media.py` puts them straight into the
`audioquality` query param), and every load-bearing comparison in ticli
already ran in wire spelling on both sides — `QUALITY_RANK`, `granted`, the
cache tracker's tier. Nothing compares a ticli name to a persisted value. So
the ticli names could move freely, and did:

- `QUALITY_CHOICES` → `["LOW", "MEDIUM", "HIGH", "MAX"]`; `QUALITY_MAP`,
  `QUALITY_LABELS`, `QUALITY_MEANINGS`, `NOMINAL_BITRATE` re-keyed. Default
  `LOSSLESS` → `HIGH` (same stream, new name).
- **Config v5** with `QUALITY_V4_RENAMES` — copied from the v1 precedent, and
  it chains: a v1 file's "LOW" (which streamed 320k) → v2 "HIGH" → v5
  "MEDIUM", same bytes throughout. Skipping this would have been silent
  corruption both ways: saved "HIRES" coerces to the default (a downgrade)
  and saved "HIGH" keeps its spelling while quadrupling its data use.
- **The one dangerous collision is "HIGH"** — 320k AAC in the old scheme,
  16-bit FLAC in the new. Saved configs are disambiguated by the version
  number. The CLI has no version, so `--quality HIGH` takes the new meaning
  and says so on stderr every use; `LOSSLESS`/`HIRES` are accepted as
  aliases and corrected out loud. Rejected: hard-erroring on HIGH (breaks
  every old script over a naming preference; the collision is one-signed —
  better audio, never worse).
- **Badge = the tier's own name**, as the app badges tracks. Rejected: a
  format badge ("24/192"), because MAX's real resolution varies per master
  and a 24/192 claim over a 24/48 file is the class of lie WORKING-RULES
  bans. `QUALITY_LABELS` is an identity map now but stays as the seam.
  The settings-ladder badge suppression heuristic went with it.
- The gate note translates the ceiling through `_tier_label` — "TIDAL sends
  MEDIUM instead", not the wire spelling.
- Legacy asked-names in `downloads.json` are translated **at read time**
  (one dict lookup at the single display fallback); the index is never
  rewritten. `INDEX_VERSION` and `CACHE_VERSION` deliberately untouched — a
  bump discards the download index / the granted tiers, orphaning the whole
  library for a display string.
- Deleted the duplicate `--quality` click definition in `player.py`'s
  `main()` (it had already drifted from cli.py's); it delegates now.

**Tests: 1403 pass** (1365 at the last entry). The rename forced every
fixture to declare its vocabulary — asked-name slots (`_download(p, tier)`,
`_player(quality=...)`) moved, wire slots (`granted=`, `note_cached`,
`session.audio_quality` asserts) stayed. The shared fixture default
`_player(quality="HIGH")` changed *meaning* (320k → FLAC), which three
estimate tests had to follow. `test_download_queue.py:446` still asserts a
"HIGH" ask sends "LOSSLESS" on the wire — the bridge, now visibly a bridge.

**Both READMEs rewritten** (root + `src/`, the PyPI long-description — easy
to miss). Quality and login are now documented from the code: device flow =
AAC-only, PKCE = the only flow TIDAL streams FLAC to, `u` upgrades in place.
Hotkeys condensed to one player table + one per-screen extras table. mpv is
the documented player throughout — owner: *"MPV pog yes. Dropped ff"*.

**Not done, on purpose:** ffplay's code path is untouched — the drop is
docs-level; removing the fallback (volume clamping, seek-by-respawn, tests)
was not asked for and is recorded as an open question in DECISIONS.md. And
nothing was probed live: whether this account's MAX really returns 24/192
remains unverified, as the research doc already said.

### Same day, follow-up — the badge shows formats after all

The tier-name badge lasted one review: Garrett wants formats — *"even if
sometimes it falls back to a slightly different quality"*, i.e. he accepts
that MAX's `24/192 FLAC` label is the tier's nominal ceiling rather than the
master's real resolution, which was the exact honesty concern that had
argued for names. Owner's call, recorded as such. `QUALITY_LABELS` became
`96k AAC / 320k AAC / 16/44.1 FLAC / 24/192 FLAC`, the settings ladder shows
the format beside each name again (70 columns for all four, inside the
80-column budget), and the gate note now reads "TIDAL sends 320k AAC
instead". Five rendered-screen assertions moved with it. 1403 still pass.

## 2026-08-03 — dead-code sweep and duplication cleanup

### (this commit) Fifteen behavior-preserving reductions, adversarially verified

A five-lens workflow (dead code and simplification, per module, plus a
cross-module pass) produced 17 findings; every one went to an adversarial
verifier told to refute it — grep the whole repo including string forms and
`ai/`, read every call site, default to refuted. All 17 survived (one was a
duplicate; one verdict was lost to a symbol-name mismatch in the harness and
recovered from the journal). Of the 16 unique findings, 15 were applied and
one declined. Net −24 lines, and each policy now stated once:

- **Dead removals.** `tags.MP4_ABSOLUTE_OFFSETS` (never read; the refusal
  logic in `_refuses_offsets` uses the byte literals, and must — its `tfhd`
  check is flag-conditional while `saio` is not). `config.BYTES_PER_GB`
  (shadowed by the live copy in cache.py that every reference, including all
  test monkeypatches, resolves to). `load_config`'s version pre-seed line (a
  proven no-op: `_migrate` uses the identical default and unconditionally
  restamps). Unused `avail=70` default on `_download_status_row`; the
  never-passed `margin` parameter of `_track_has_time_left` (the 2s is now in
  the docstring instead of a dead knob).
- **Duplication, player.py.** The terminate/wait(2s)/kill reap existed three
  times → `_reap_process()` (callers keep deciding what `_process` becomes).
  The "Page X/Y" footer existed six times → `_page_footer()` (search keeps
  its own copy: it appends the result count inside the same branch). The
  `[Tab]` row with its collapse-when-narrow fallback existed twice →
  `_tab_row(order, labels, active, mark=)`, where `mark` is search's
  answered-dot and the narrow form keeps the active entry's dot — output
  byte-identical, which the rendered-screen tests confirm. The refetch
  unknown/skipped/upgrade classification existed twice → one `classify`
  closure. One redundant `body + footer` concatenation built only for its
  `len()`.
- **cache.py.** The atomic write-then-rename discipline existed three times
  in one file (the third inline in `enforce_budget` to dodge `_save`'s
  recursion) → module-level `_atomic_write_json`; callers keep their own
  mkdir and their own idea of what a failed write means, so behavior is
  unchanged. `reconcile`'s save-skip is now `tracks != before` — the counts
  disjuncts were provably implied. `audio_record` and `note_cached` dropped
  isinstance guards on tracker values that `_load_tracker` already filters
  to dicts at the load boundary (the invariant `note_played` had relied on
  all along).
- **artwork.py.** `_decode_dc_scan` re-resolved huffman/quant tables and the
  plane per MCU per component — loop-invariant, so ~1,600 MCUs × 3
  components of redundant dict lookups per 320px cover. Hoisted once before
  the loop; the missing-table errors still raise before any output exists
  (a frame with zero MCUs is already rejected as "empty frame"), verified
  byte-identical on both fixture JPEGs and the corrupt variants.

**Declined, on purpose:** removing the `cols + ART_MARGIN <= width` conjunct
in `art_size`. It is provably redundant *under today's constants* (verified
exhaustively to width 1000), but only under them — `ART_WIDTH_SHARE` has been
retuned before (0.4 → 0.6), and a future tune or a smaller `ART_SIZES` entry
would silently make it load-bearing again. A guard whose truth depends on
tuning stays.

**Left alone as false positives** (vulture still flags them, each checked):
the `signum` parameters (signal-handler signature), `_merge_position_into_saved_state`
and `_stream_url` and `cached_usage` and `downloaded_count` (all called —
vulture misses usage through locals/tests), `no_wrap`/`__defaults__` (Rich
API and functools internals).

Suite: 1,446 passed, 8 skipped — identical count before and after, three
consecutive full runs.

---

## 2026-08-07 — two songs at once: the kill/spawn seam in `play_url`

The owner: *"there was a primary song playing and another song playing at that
same time with the same client."* One process, two audible streams. Diagnosed
by a five-lens workflow (process lifecycle, generation counters, initiation
paths, monitor/auto-advance, resume/lock discipline), each report then handed
to a refuter told to kill it and to check reachability and citation accuracy.
All five lenses converged on the same mechanism and every refuter confirmed it
— three by driving the real `AudioPlayer.play_url` with a faked `Popen` and
watching two live processes come out.

**The bug.** `play_url` did `self.stop()` — which takes `_lock`, reaps, sets
`_process = None`, releases — and *then* `with self._lock:` around the `Popen`.
Two starts landing inside one reap (a double-tap on `n`, which has no repeat
window; `KEY_REPEAT_WINDOW` guards only the space bar) each reached the spawn,
because the loser's `stop()` ran after `_process` was already None and so
reaped nothing. `_process` is a single slot and the only handle anything uses,
and mpv's IPC socket is one fixed path per pid that the last spawn takes over —
so the orphan was not merely leaked, it was unreachable: deaf to the space bar,
invisible to the UI, and still playing after `q`.

**The fix.** `stop()` → lock-taking shell + unlocked `_stop_locked()`;
`play_url` → shell + `_play_url_locked()`, which reaps and respawns inside one
acquisition and returns `(have_kept, gen)` so the caller starts the download
off the lock (that comment at the old `_start_download` call was load-bearing
and still is). `_lock` is a plain non-reentrant `Lock`, so the unlocked
`_locked` half is the only shape available — the one `seek_to` has always had,
and the one WORKING-RULES already prescribed. Total time the lock is held is
unchanged: the reap was always inside it. It is now one acquire/release pair
instead of two.

Two adjacent seams closed with it. `resume()`'s ffplay-without-cache path did
`self._lock.release()` … `play_url(...)` … `self._lock.acquire()` by hand —
the same window, reached by pressing space before the cache copy is ready.
And `_reap_process` sent SIGKILL without a second `wait()`, so a backend that
ignored SIGTERM for 2s became a zombie the moment the caller dropped the
handle. `_stop_locked` also now clears `_process` when it finds it already
exited: a dead handle there is not a process, it is a stale answer, and
`failure()` reads it — a stream the backend had refused went on reporting its
error after we had stopped the track.

**Predicted, and half-fixed, two weeks ago.** `BUGS-2026-07-24-resume-trace.md`
item 4 called this exactly — *"tighter interleaving double-spawns mpv →
orphaned process, double audio"* — and prescribed three fixes. Two shipped and
hold; both close the *monitor's* door onto the window rather than the window.
Item 4 was marked FIXED anyway. That file and INCIDENTS #7 now record which
third was outstanding.

**Rejected**, each for a stated reason: debouncing `n` (does not close it —
`_flush_seek` and media keys are keyboard-free entrants — and it would break
the buffered-burst input path that makes held arrows scroll smoothly); a
Player-level mutex around `_play_track` (would be held across `_stream_description`,
a network round trip, violating both the leaf-lock rule and network-never-on-
the-UI-thread, and it is not a leaf); a PID registry or `start_new_session` +
`killpg` sweeps (new in-place-mutated shared state that must then be locked on
every touch, a new process-group assumption, and it cleans up after the bug
instead of preventing it); re-checking `_play_gen` just before the `Popen`
(narrows, does not close — the two racing workers carry different,
individually-current generations); shortening the reap (the window is the
lock-release boundary, not the reap's duration).

**Ruled out during diagnosis**, all with the guard that defeats each: the
`_play_gen` read-modify-write being non-atomic (0 lost updates in 1.6M
contended increments, and subsumed anyway); resume-on-launch autoplaying over
a manual play (`_restore_state` contains no play call at all); the monitor
auto-advancing on top of an in-flight change (`_track_changing` + two dead
polls hold); prefetch pre-starting a decoder (it stores a URL string, never a
`Popen`); radio queue extension; a completed download respawning the player; a
wedged stderr pipe (`_open_stderr` is a truncated regular file, and says why);
two `AudioPlayer` instances. Still open and not this bug: **two ticli
processes** — `#8`'s multi-instance lockfile, which produces the same symptom
with no race at all.

**Tests: 1,453 → 1,467.** The new file asserts process *liveness*, not
bookkeeping — every process ever spawned, except the one `_process` currently
names, must have been terminated or killed. That distinction is the whole
point: the suite already had 1,453 tests and none could see a leaked process,
because every fake stood in for a player that cannot die (`_FakeProc` defines
only `poll()`; `test_cache.py`'s `_Proc.poll()` returns `None` forever with
`terminate()` as `pass`). One test runs the race against **real OS children**;
pre-fix it reports *"pids still running that ticli can no longer stop:
[12148] (ticli thinks it is playing 12149)"*.

**Worth recording about the test itself.** The first version started two
threads and hoped. It passed **25/25 against the unfixed code** — CPython locks
are unfair, so the thread that releases barges and re-acquires before the
parked waiter is even woken, and the scheduler essentially never lands in the
seam (the diagnosis measured well under 1% of contended track changes, which is
why this was a once-a-month bug that could not be reproduced on purpose). The
committed version pins the interleaving instead of gambling on it: it stops the
releasing thread from immediately re-taking the lock. Once kill and spawn are
one acquisition, `play_url` no longer calls `stop()`, the hook is never
invoked, and there is nothing to land in. A test that cannot fail against the
broken code was not a weak test — it was not a test.

**Not done.** Nothing was changed about `#8`'s multi-instance lockfile, which
remains the other route to this symptom and should be asked about before
assuming this fix covers a future sighting. `_play_from_cache` (`resume()`'s
ffplay-with-cache path) still spawns without reaping; it was verified
unreachable with a live process — `pause()` sets `_process = None` before
`_paused = True` under one lock — so it was left alone rather than given a
guard whose necessity nobody could demonstrate. The `_download_gen += 1` that
both racing threads used to execute may have been able to make the winner's
download abandon itself; with the race closed there is only one bump per track
change, so the question is moot rather than answered.

Suite: 1,467 passed, 8 skipped. Verified stable — 25 consecutive runs of the
new file, 6 of the timing-sensitive set (input_latency, buffering, seek,
download_queue, bulk_downloads), 3 of the touched-area files, and 3 full runs,
all green.

---

## 2026-08-07 — one ticli at a time

The second route to "a primary song playing and another song playing at that
same time": not a race inside one process (that was earlier today), but two
ticli processes. Two instances were observed a minute apart on 2026-07-24 and
written down as `BUGS-2026-07-24-resume-trace.md` item 8; the owner listed
"multi-instance state-file clobbering" as one of three open bugs the same day.
Nothing had stopped a second one from starting.

**What two instances actually cost.** Both play, over each other, and neither
can stop the other — the same symptom as the `play_url` seam, reached from
outside. Both also write `~/.config/ticli/player_state.json`, and item 8's
description of that had gone stale: the atomic write shipped in July, so they
no longer *tear* the file, they take turns winning it. One sharper hazard did
survive — `_write_state_file`'s temp path is `player_state.tmp`, fixed and
therefore shared, so two instances write the same temp file and the loser's
`os.replace` can fail outright, swallowed at debug level by
`_save_state_locked`. All of it is moot once there is only one instance.

**The fix.** `_take_instance_lock()` at the top of `run()`, before the audio
backend, the login, the cache reconcile or the restore — a second instance
must be refused before it opens anything shared, not after. An advisory
`fcntl.flock` on `~/.config/ticli/instance.lock`, held for the life of the
process because closing the descriptor is what releases it.

**Deliberately not the pid lockfile item 8 proposed.** A pid file has to work
out whether the pid in it is still alive, and it is wrong in both directions:
it strands the app after a crash and it can match a recycled pid. The kernel
releases an `flock` when the holder's last descriptor goes — including on
SIGKILL, a closed terminal, a power cut — so there is no staleness to detect
and no cleanup path to write. Verified with a real child process and a real
SIGKILL rather than assumed, because the entire choice rests on it.

**Refuses only on a positive answer.** No `fcntl`, an unwritable state
directory, or a filesystem that will not take a lock (the NFS home directory
case) all return "could not ask" and ticli starts exactly as before. A guard
against an honest mistake must never become the thing that stops the owner
playing his music, and the failure mode of getting this wrong — locked out
with no recourse but deleting a file by hand — is worse than the bug.

What the second instance sees, verified end to end with two real processes:

    ticli is already running (pid 12410).
    Only one copy can run at a time: two play over each other, and each one's
    saved position overwrites the other's.
    Quit the running copy first, or use the terminal it is in.

**A near-miss worth recording.** The lock is the first thing `run()` writes,
a handful of tests call `run()`, and one of them
(`test_run_clamps_before_anything_plays`) redirects the config directory but
not the state directory. So the first full-suite run after the guard landed
created `~/.config/ticli/instance.lock` — in the owner's real config
directory, which the testing rules forbid. Caught by checking rather than by
assuming, and fixed where it belonged: `STATE_DIR` and `STATE_FILE` are now
redirected suite-wide in `tests/conftest.py`, autouse, next to the
`DOWNLOAD_ROOT` rail that has always been there. Per-test redirection had been
correct for years and was still one new startup write away from being wrong.
`test_the_suite_never_locks_the_owners_real_config_dir` is the tripwire.

**Product decision the owner still owns.** A late instance is *refused*, not
started read-only. Read-only would have fixed the state clobbering and left
the two-songs symptom completely untouched, which is the symptom that prompted
this. Refusing is the stronger claim and it is the one recorded in DECISIONS
as needing his confirmation; if he wants a second window for browsing while
the first plays, that is a different feature (a second instance that never
takes the audio backend) rather than a loosening of this.

**Not done.** The shared `player_state.tmp` path is untouched. With one
instance it is unreachable, and giving it a per-pid name would be a fix to
code that can no longer be reached by the thing it protects against — but it
is the one piece of item 8 that would come back if the lock is ever weakened,
and it is noted in the file rather than silently left.

Suite: 1,467 → 1,480 (13 new; one asserts the conftest rail itself). Verified
stable across 20 consecutive runs of both new files and three full runs, plus
an end-to-end check with two real processes.
