# Data path audit — streaming, caching, downloading

**Date** 2026-07-26 · **Commit** `c71de29` · **Scope** read-only. Nothing was
changed, nothing committed, `player.py` was not touched (another agent held it).

**Zero requests were made to TIDAL or its CDN.** Every measurement below comes
from a loopback HTTP/HTTPS server, a locally-encoded FLAC-in-MP4 file, the real
cached track that was in `~/Library/Caches/ticli/audio` at the start of the
session, and the installed `tidalapi` source.

Measured vs inferred is marked on every claim.

---

## Verdict

The data path is **well designed and unusually well reasoned**, and it is
**not clean**. Those are both true and they are about different layers.

The *shape* is right: one downloader (`fetch_to_file`) for both tiers, a plain
`requests` GET rather than ffmpeg, `.part` + `os.replace` so a partial file can
never be served, generation counters instead of locks, network never on the UI
thread, deletion by explicit name. The HLS-from-DASH work is genuinely
excellent — `#EXT-X-MAP`, the per-backend protocol whitelist and the
`--cache-secs` fix are each things most implementations get wrong, and the
reasoning is preserved where the next person will find it.

What is *not* clean is the seam between the layers. Five of the six findings
below are cases where two pieces of the path each behave correctly and
disagree with each other: the downloader names a file the cache module does not
recognise, the player fetches a stream URL it then throws away, the monitor
reads a truncated stream as a finished one. None of them is visible from inside
the function that causes it, which is why they survived.

Two of them (F1, F2) are silent-failure bugs of exactly the kind INCIDENTS #2
and #3 were written about.

---

## What is genuinely good, and why

1. **One downloader, one code path.** `fetch_to_file` serves the cache tier and
   the downloads tier, BTS and segmented, both backends. The docstring's claim
   ("having written it twice is how one of them would quietly stop working") is
   the correct instinct and it is honoured — I could not find a second copy.

2. **The segmented-stream handling is right.** `_hls_playlist` is correct where
   tidalapi's own `get_hls()` is not, `_hls_segments` reading the segment list
   back out of the playlist keeps a stream one string everywhere else, and
   `stream_sources()` makes "download it" and "play it" the same input.

3. **`--cache=yes --cache-secs=60` is real and measured here too.** On a local
   HLS playlist of 45 fMP4 segments (990 kbps, 176 s), bytes pulled in the
   first 3 seconds: **9.68 MB with the flags, 2.37 MB without** — 4.1× more
   buffer. (Measured.)

4. **Writes are safe.** 256 KB chunks into one handle, `os.replace` for the
   rename, `unlink(missing_ok=True)` in eviction (the INCIDENT #5 race), 0o700
   directories, no directory ever removed. `_cached_audio_path` looks up by
   stem and refuses `.part` — a partial file cannot be served.

5. **Downloads genuinely outrank the cache at playback.** `_play_track` looks
   for `downloads.path_for()` *before* asking for a stream URL, and a track
   with a downloaded copy costs **zero** requests and zero bytes. That path is
   clean, and it is the model the other two should follow (see F3, F4).

6. **The generation counters are, as far as I can tell, correct.**
   `_download_gen` is bumped only by `stop()`; `seek_to` deliberately does not
   bump it (the reasoning in CLAUDE.md is right — it would abandon the copy
   being fetched for the very track being scrubbed). `_play_gen` is checked at
   all three landing points in `_play_track`. `_artwork_request`'s whole-tuple
   compare covers resize and track change together. `_take_prefetched` checks
   id *and* age. I found no late-landing result that can take a newer one's
   place.

---

## Findings, ranked by real-world impact

### F1 — A leaked `.part` file is invisible to the cache and permanently evicts real songs

> **FIXED 2026-07-26.** `is_owned_audio` now makes the extension optional on a
> `.part`; the four tests use the name production writes, and a new test takes
> the path from `_start_download` itself rather than restating it. Filed as
> INCIDENTS #6. Everything below is the diagnosis as written.

**Measured, reproducible, and the tests test a filename production never writes.**

`_start_download` writes `part = base + ".part"`, where `base` is
`{audio_dir}/{track_id}` with **no extension** (the extension is not known until
the CDN answers). So the file on disk is `467461385.part`.

`cache.is_owned_audio()` strips `.part` and then requires a recognised audio
extension — it expects `467461385.m4a.part`. Measured:

```
467461385.m4a         owned=True
467461385.m4a.part    owned=True     <- what every test in test_cache.py uses
467461385.part        owned=False    <- what _start_download actually writes
```

`test_cache.py` asserts on `12.m4a.part` in at least four places (lines 708,
801, 871, 912). Production never creates that name.

Consequences, all measured with the real modules and a 1 GB budget:

```
audio/111.part   1.500 GB   (leaked by a crash / kill -9 / power loss)
audio/222.m4a    0.250 GB   (a real cached song)

total_bytes() sees:                    1.750 GB
owned_audio_files() sees:              ['222.m4a']
audio_count() reports:                 1 song
enforce_budget() freed:                0.250 GB  -> deleted the real song
remaining on disk:                     ['111.part']
clear_audio() returned:                (0 deleted, 0 kept)
remaining after "clear cached songs":  ['111.part']
```

So: the leaked file **is** counted against the budget (`total_bytes` is a plain
`_dir_size`), is **not** evictable, and is **not** deletable by the user. Once
leaked bytes exceed the budget, every sweep evicts every real song and the cache
can never hold anything again — while the settings page says `0 songs cached ·
1.500 GB`, which is honest about the bytes and silent about why.

A `.part` is leaked by any exit that is not `stop()`: SIGKILL, a crash, power
loss, the OS reaping the process. The in-process paths (`_DownloadSuperseded`,
an exception) do clean up.

**Cheapest fix.** Make `is_owned_audio` accept a bare `{digits}.part` as well as
`{digits}{ext}.part` — three lines in `cache.py`, no change to the writer, no
migration. Then rewrite the four tests to use the name production writes.

**Risk.** Effectively none: the predicate only widens over names that are
already `{track_id}`-shaped, so the `important.txt` decoy test still passes
(verified — that file is still not owned). The one thing to keep is that
`audio_count()` must keep excluding `.part`; it does, by suffix.

---

### F2 — A stream that dies mid-track exits 0, so the monitor reads it as "the track ended" and silently skips

> **FIXED 2026-07-26.** `_stream_ended_early()` + a branch in
> `_monitor_playback` before the auto-advance. The owner chose **stop with an
> honest toast** over the automatic restart proposed below — a silent retry
> against a dead URL is the same silence with more requests. `[space]` resumes
> from where it died. Everything below is the diagnosis as written.

**Measured. This is INCIDENT #3's failure mode on a path the INCIDENT #3 fix
does not cover.**

Harness: the same 45-segment local HLS playlist, but the server returns
`403 Request has expired` from segment 3 onward — i.e. exactly what a signed
TIDAL URL does an hour after it was issued.

```
mpv    (ticli flags)   exit=  0   after 12.1s of a 176s track   stderr: (silent)
ffplay (ticli flags)   exit=  0   after 12.1s of a 176s track   stderr: (silent)
```

Both backends play what they had buffered and then exit **0 with an empty
stderr**. `AudioPlayer.failure()` returns None for exit 0 — correctly, by its
own contract — so `_monitor_playback` falls through to
`self._play_queue_index(self._queue_index + 1)`. The user hears 12 seconds of a
3-minute song and the player moves on as if nothing happened. If the cause is
systemic (expired session, network down), it walks the queue.

Where this is reachable:

- A pause longer than the ~1 h URL lifetime, then resume. On mpv the process is
  still alive and holding a dead URL; on ffplay, `resume()` re-spawns against
  `self._current_url` — the *same expired* string — because `AudioPlayer` has no
  session and cannot get a fresh one. (Read.)
- Any network blip mid-track. (Read; the harness models it exactly.)
- A segmented stream whose local `.m3u8` was pruned by `HLS_KEEP = 4` while it
  was still playing — reachable by starting three or four downloads from the
  download screen while a track plays, since each `_download_stream_url` writes
  another playlist. ffplay's resume and seek both re-read that file. (Inferred
  from reading `_write_hls_playlist`; not reproduced.)

**The information to detect it already exists.** `_track_has_time_left()` is
already used two branches above, in the `source_vanished` case.

**Cheapest fix.** In `_monitor_playback`, before the auto-advance branch: if the
player is gone, there was no `failure()`, and `_track_has_time_left()` is true,
this is a truncated stream, not an ending — toast it and call
`_start_current_from_position()`, which already re-fetches a fresh URL through
`_play_track`. Bound it to one retry per track so a wrong `duration` cannot
loop; the natural latch is comparing `_get_position()` against the position of
the previous retry (no new thread, no new state machine).

**Risk.** A track whose TIDAL `duration` is longer than the actual audio would
restart once and then advance. That is a one-off extra spawn, against a class of
failure that is currently completely silent — the same trade `_track_has_time_left`'s
own docstring already accepts ("better one extra respawn than a silently dropped
track").

---

### F3 — Every play of an already-cached track still pays a `playbackinfo` request it throws away

> **FIXED 2026-07-26.** `_local_copy()` answers "is there a copy on this disk
> good enough to play?" before anything is asked of TIDAL, in `_play_track`
> and in `_maybe_prefetch_next`. "Good enough" is a real question now that the
> tracker records the granted tier: below the current setting is skipped (the
> re-download-at-higher-quality behaviour Garrett asked for), above it is
> kept, unrecorded is left alone. The stated cost — the quality gate learns
> nothing on a cache hit — is in HISTORY rather than hidden. Everything below
> is the diagnosis as written.

**Read, then confirmed by tracing the two functions.**

`_play_track`:

```python
owned = downloads.path_for(real.id)
url = "" if owned else (self._take_prefetched(real.id) or self._stream_url(real))
```

The downloads tier is checked first and short-circuits the request. The **cache**
tier is not checked at all here — `play_url` only discovers the cached file
*after* it has been handed a URL, and then ignores the URL. So a track that will
play entirely from `~/Library/Caches/ticli/audio` still costs one full
`get_stream()` round trip.

`_maybe_prefetch_next` has the same hole and neither check: it spends a request
on the next track's URL whether or not that track is downloaded *or* cached.

Scale: replaying a 20-track cached playlist costs **20 unnecessary
`playbackinfo` calls** plus up to 20 more from the prefetch. That is the exact
endpoint whose burst rate got the owner's IP blocked (INCIDENTS #1) — not at
this rate, but it is bandwidth and rate-limit budget spent on nothing, on the
path a heavy user hits most.

**Cheapest fix.** Hoist the existing lookup: `self.audio._cached_audio_path(id)`
already answers "is it whole on disk" with one glob and no network. Check it
next to the `downloads.path_for` line, and again in `_maybe_prefetch_next`
before the thread is started.

**Risk, and it is real.** `_stream_url` is also where `_note_granted_quality`
gets its only free evidence of entitlement, so skipping it on cache hits means
the quality gate learns more slowly. It still learns on every cache *miss*,
which is every new track, so the gate is not blinded — but a user whose whole
library is cached would see the gate stay unset after a PKCE upgrade until the
next new song. Worth stating in the settings copy rather than hiding. The other
risk — the cached file vanishing between the check and the spawn — is already
handled by `source_vanished` + `_monitor_playback`, which is the machinery that
exists for precisely this window.

---

### F4 — A track that is already in the cache is re-downloaded from scratch when the user downloads it

> **FIXED 2026-07-26**, by exactly the route sketched below: the cache tracker
> records the granted tier, and `_promote_cached_copy()` copies rather than
> fetches on an **exact** tier match with a file that is really there.
> Everything below is the diagnosis as written.

**Read.** `_start_download_job` goes straight to `_download_stream_url` → one
API request → `fetch_to_file` over the CDN. It never looks at
`audio_dir()/{id}.*`. Play a hi-res track, then press `[d]`: **1 API request +
~30 MB fetched again** for bytes already on the disk.

The reverse direction is clean — `play_url` prefers `local`, and
`_start_download` is not called when a downloaded copy is playing, so no
double-fetch there.

**Why it cannot simply be fixed.** The cache does not record *which tier* a
cached file is. Files are `{track_id}{ext}`, and `.m4a` is AAC-HIGH on a
device-flow session and FLAC-in-MP4 on a PKCE one — indistinguishable by name.
The download screen lets the user pick a tier, so promoting a cached file
without knowing its tier would silently hand them the wrong quality, which is
squarely against the "never display a value that isn't real" rule.

**Cheapest honest fix.** Record the granted quality when the download lands.
`_note_granted_quality` already sees it. A `audio:{track_id}` entry in the
existing metadata index (`{"quality": ..., "ext": ...}`) is stdlib, needs no new
file, and is disposable by construction. Then `_start_download_job` can
`shutil.copyfile` when the tiers match — 0 requests, 0 network bytes, and the
tagging step runs on the copy unchanged.

**Risk.** The cached source can be evicted mid-copy; on POSIX the open
descriptor survives, so the copy completes. If the index and the disk disagree,
fall through to the network — the same "the index is a hint, the file is the
answer" rule `downloads.path_for` already uses. A partial-tier match (cached
HIGH, download asked for HIRES) must fetch; only exact matches promote.

---

### F5 — ffplay has the same readahead weakness mpv just had, and one flag fixes it

> **FIXED 2026-07-26.** `-infbuf` added to `_hls_flags()` on the ffplay branch
> (segmented only). Covered by a real-backend test that counts segments
> requested in the first four seconds — 12 of 12 with the flag, 6 of 12
> without. Everything below is the diagnosis as written.

**Measured**, on the 45-segment local HLS playlist (990 kbps, 176 s, 21.8 MB).
Bytes served, so higher = more buffered:

| backend / flags | @1 s | @3 s | total in 8 s |
|---|---|---|---|
| mpv, ticli's `--cache=yes --cache-secs=60` | 9.19 MB | 9.68 MB | 10.16 MB |
| mpv, no cache flags (the bug that was fixed) | 1.88 | 2.37 | 2.86 |
| **ffplay, ticli's current flags** | **1.40** | **1.40** | **2.37** |
| **ffplay, `+ -infbuf`** | **21.71** | 21.71 | 21.71 |

ffplay currently sits at the *un-fixed* mpv level — about 1.4 MB, roughly two
segments, ~11 s of audio — because its read thread stops once every stream has
≥25 packets and >1 s queued (`MIN_FRAMES` / `stream_has_enough_packets` in
ffplay.c; the constant is inferred, the behaviour is measured). Any segment
fetch slower than that window is an audible hitch, which is the same symptom
that was reported for mpv.

`-infbuf` ("do not limit the input buffer size") pulls the **entire track** in
under a second. On the non-segmented BTS path neither backend needs anything —
both already read the whole file immediately (ffplay 23.7 MB @1 s, mpv 47.8 MB
including range re-reads), so this belongs in `_hls_flags()`, next to the mpv
cache flags it mirrors, not in `_ffplay_cmd`.

**Risk.** `-infbuf` is unbounded by design: the whole track lands in ffplay's
RAM (~30 MB for hi-res, more for a long 24/192 track). ffmpeg documents it for
realtime streams. There is no `cache-secs` analogue for ffplay, so the choice is
between ~11 s and unbounded; for a VOD track of known, bounded length,
unbounded is the right side of that trade. It also *shortens* the window in
which the playback fetch and the background download overlap, which slightly
helps F6.

---

### F6 — The settings page does O(N²) disk reads on the UI thread, twice a second

> **FIXED 2026-07-26.** `downloads.usage()` returns both numbers from one
> index read, and the player memoises it — dropped when the page is opened
> and when a download lands. Both halves of the audit's suggestion, because
> the linear version is still disk work and `_repaint` pays it on every idle
> tick. Everything below is the diagnosis as written.

**Measured.** `_build_settings_display` calls `downloads.downloaded_count()` and
`downloads.total_bytes()`. Each iterates the index and calls `path_for(tid)` —
and **`path_for` re-reads and re-parses the entire `downloads.json` every
call**. So one repaint is 2(N+1) file reads and 2(N+1) JSON parses of an
O(N)-sized file.

```
 50 downloads:    4.54 ms per settings-page repaint
200 downloads:   42.70 ms
500 downloads:  229.48 ms
```

`_repaint` builds the display on every idle tick before deciding whether the
render is identical, so this is paid **twice a second while the settings page is
open**, not just on a keystroke. At 500 downloads that is 229 ms of blocking
disk and JSON work on the UI thread — the same order as the 231 ms
keypress-to-repaint latency the `auto_refresh=False` work existed to remove.

**Cheapest fix.** Load the index once and pass it down: `downloaded_count` and
`total_bytes` each become one read plus N stats (linear, ~1 ms at 500). If that
is still felt, memoise them the way `cache.disk_bytes()` / `audio_count()`
already are, invalidated when a download lands — the pattern is already in the
codebase.

**Risk.** Almost none; the semantics ("stat every file, the index is only a
hint") are preserved. Only the redundant re-reads go.

---

### F7 — No connection reuse in `fetch_to_file`: 46 TLS handshakes per hi-res track

> **FIXED 2026-07-26.** One `requests.Session()` per call, in the same `with`
> as the output handle. The six test fakes that replaced `requests.get` moved
> with it — `tests/fakes.py:patch_get` replaces the function *and* the session
> together, because a fake of something production has stopped calling is
> INCIDENTS #2's shape. Everything below is the diagnosis as written.

**Measured, and smaller than expected.** The one real cached hi-res track
(`467461385.m4a`, FLAC 24/48, 175.9 s, 29,575,234 bytes, 1.345 Mbps) parses to
**45 media segments + 1 initialization segment**, mean 657 KB each. So a hi-res
track download is **46 sequential `requests.get()` calls**, each of which builds
its own Session, its own pool and its own TLS connection.

Loopback HTTPS, 46 requests of 657 KB, median of 5 runs:

| | fresh `requests.get` | one `requests.Session` | delta |
|---|---|---|---|
| no added latency | 0.250 s | 0.032 s | +0.218 s (7.8×) |
| +40 ms per connection setup (models TCP+TLS at a 20 ms RTT CDN) | 2.430 s | 0.086 s | **+2.34 s** |

So on a real CDN the missing Session costs roughly **2.3 s of wall time and 45
extra TCP+TLS handshakes per hi-res track**, plus the CDN-side connection load.
Nothing user-visible waits on the cache download, so the real cost is (a) the
deliberate-download progress bar, where the user *is* watching, and (b) 45
avoidable handshakes' worth of CDN politeness.

**Cheapest fix.** One `requests.Session()` inside `fetch_to_file`, closed in a
`with`. Not a module-level global — a per-call Session keeps the function pure,
keeps the two tiers independent, and cannot leak sockets between the cache
thread and a download job. Roughly four lines. No new dependency.

**Risk.** Very low. The only behavioural change is `Connection: keep-alive`
across segments of the same track, to the same host. If the CDN spreads segments
across hosts the pool handles it. Worth one test asserting the byte-for-byte
equality that already exists still holds.

---

### F8 — The ffplay scratch download uses one fixed filename for every track

**Read.** When audio caching is off and the backend is ffplay,
`_start_download` writes to `tempfile.gettempdir()/ticli-cache-{pid}.part` — a
name with no track in it, shared by every track in the process.

`play_url` calls `stop()` (bumping `_download_gen`) before starting the new
download, so the old thread does abandon itself — but only at its next
`abandoned()` check, which is **one 256 KB chunk read away**. In that window the
new thread has already `open(part, "wb")`-ed the same path (truncating it) and
the old thread writes its next chunk into it. The result is an interleaved
scratch file, which is what ffplay's `resume()` then plays.

Impact is narrow — ffplay only, `cache_songs` off, a fast skip — and the failure
is "the resumed track sounds wrong" rather than a crash. But it is a genuine
data race and the fix is free: put the track id (or the generation) in the name,
exactly as the cache tier already does.

---

## The question that was asked first: is every cached track fetched twice?

**Yes, when `cache_songs` is on: confirmed by reading `play_url`.** The player
process is spawned against `url`, and then, off the lock,
`_start_download(url, ...)` fetches the same bytes over a second connection.
Two full copies over the network.

It is narrower than it first looks:

- With `cache_songs` **off** and mpv, `_start_download` returns immediately —
  no second fetch. The double fetch is the price of the cache, not a constant.
- With a **downloaded** copy, `url` is `""` and neither fetch happens.
- With a **cached** copy, `have_kept` is true and no download starts (though
  F3 means a request was still spent getting the URL).

Bytes at each tier for a 3-minute track (LOW/HIGH from the 2026-07-25 research;
HIRES from the real cached file; LOSSLESS from `NOMINAL_BITRATE`):

| tier | one copy | what a cache-filling play actually costs |
|---|---|---|
| LOW (AAC 96k) | 2.4 MB | 4.8 MB |
| HIGH (AAC 320k) | 7.9 MB | 15.9 MB |
| LOSSLESS (FLAC 16/44.1) | ~19 MB | ~38 MB |
| HIRES (measured, FLAC 24/48, 1.345 Mbps) | 29.6 MB | 59.2 MB |

An hour of hi-res listening that fills the cache is ~1.2 GB instead of ~600 MB.

### The alternatives, and what each breaks

- **Play from the growing `.part`.** Both backends would hit EOF at the write
  head and stop; neither tail-follows a file. Seeking past the write head fails.
  For segmented streams the concatenated fMP4 has no `sidx`, so seeking backward
  means rescanning. And `.part` → final `os.replace` under a playing process
  changes the file identity mid-playback. This is several new failure modes to
  save bandwidth nothing is waiting on. **Not worth it.**

- **Download first, then play.** Correct on bandwidth, wrong on the thing the
  user feels: ~30 MB before the first sound on hi-res. **No.**

- **`mpv --stream-record=<file>`.** This is the exact single-fetch answer and it
  is a real mpv option — and it is wrong here for three reasons the project has
  already decided: ffplay has no equivalent (so the cache becomes
  backend-dependent again, which is the specific thing INCIDENT #2's fix bought
  back), the download would once more ride on the player process, and it records
  from the current position, so a track started mid-way records a partial file.
  **Rejected.**

- **A local HTTP proxy the player reads through.** The only design that is both
  single-fetch and backend-independent. It needs a listening socket and a
  serving thread — against the standing "no new threads/timers" rule — and it
  puts a server in the path of playback. **Not for this codebase.**

- **Prefetch the *bytes*, not just the URL** — the one that actually works.
  `_maybe_prefetch_next` already fires `PREFETCH_LEAD = 20` s before the end of
  a track and already resolves the next track. If it also fetched that track
  into the cache, then when it starts it is a cache hit, plays from disk, and is
  never fetched a second time. Steady state is **one fetch per track instead of
  two** — the double fetch disappears for every track after the first.
  20 s is enough for 29.6 MB at ~12 Mbps, which is most home connections but not
  all; a prefetch that does not finish in time simply falls back to today's
  behaviour, so the failure mode is "no worse than now".

  What it needs: its own generation counter (the shared `_download_gen` is
  bumped by every `stop()`, so a prefetch download would be cancelled by the
  track change it exists for), a rule that it never runs when `cache_songs` is
  off, and acceptance that bandwidth is spent on a track the user may skip —
  bounded by the same `PREFETCH_LEAD` that already bounds the URL prefetch, and
  by only ever prefetching one track.

  This is the largest available win and the only one that halves the bytes. It
  is also the most invasive, and it should land **after** F1 (or it will fill
  the cache with unrecognisable `.part` files twice as fast).

---

## Smaller notes

- **`_start_download` swallows every failure into `logger.debug`.** A cache that
  can never write — disk full, permissions, a CDN 403 on every segment — is
  invisible forever; the settings page just says `0 songs cached` and there is
  no way to find out why. This is the shape of INCIDENT #2. One toast on the
  first non-`_DownloadSuperseded` failure of a session would be honest and cost
  nothing.
- **`cache._save`'s `OSError → debug`** has the same shape one level down:
  metadata caching can appear on and be doing nothing.
- **`_download_job_gen` is checked after the file has landed**, so cancelling
  during the tagging step still leaves a recorded download. Arguably correct
  (the bytes are whole), but it contradicts the docstring's "abandons its
  half-written file".
- **`downloads.discard_scratch(final)`** in `_start_download_job`'s exception
  paths targets `{final.name}.part`, while the file actually written is
  `.ticli-{id}.part` in the download root. `_discard_staging` does the real
  work; the other call is a no-op. Harmless, but it reads as coverage that
  isn't there.
- **`_maybe_prefetch_next` latches `_prefetch_id` and only clears it in
  `_play_track`**, so changing the queue without changing track leaves the
  prefetch armed for a track that is no longer next. Harmless — `_take_prefetched`
  checks the id — but it means one wasted request.
- **HLS playlists are never cleaned up on exit.** `/tmp/ticli-hls-{pid}/` is
  pruned to `HLS_KEEP = 4` on each write and left behind when the process ends.
  A few KB per run; worth an `atexit` only if someone is already there.
- **`allowed_segment_extensions`.** ffmpeg 8.1 refuses HLS segments whose path
  has no recognised extension (reproduced accidentally in this audit: `/seg0`
  was rejected, `/seg0.mp4?token=…` accepted). Real TIDAL segment URLs end
  `.mp4?token=…` so this works today, but the segmented path now depends on a
  detail of TIDAL's URL shape that nothing asserts and nothing would explain if
  it changed. Worth a sentence in the code, not a flag.
- **UI thread otherwise looks clean.** `_cached_audio_path`'s glob, artwork
  fetch/decode, and every `_stream_url` all run on daemon threads.
  `cache.audio_count()` / `disk_bytes()` are memoised and correctly invalidated.
  The only measured UI-thread offender is F6.

---

## Method

- Segment count and sizes: mp4 box walk of the real cached hi-res track
  (45 `moof`/`mdat` pairs + `ftyp`/`moov`, 619-byte init, mean segment 657,213 B).
- Readahead and expiry: local `http.server` with a byte-counting write loop,
  serving a locally encoded fragmented FLAC-in-MP4 (`ffmpeg -f lavfi anoisesrc`,
  176 s, 990 kbps, 45 fragments) as both a whole file and a 45-segment HLS
  playlist with `#EXT-X-MAP`, driven with ticli's exact backend flags at
  `-volume 0` / `--volume=0`.
- Connection reuse: loopback HTTPS (self-signed), 46 × 657 KB, fresh
  `requests.get` vs one `Session`, median of 5, with and without a 40 ms
  per-connection accept delay.
- Cache predicates and budget: the real `ticli.utils.cache` module against a
  temporary `CACHE_DIR`.
- Downloads index cost: the real `ticli.utils.downloads` module against a
  temporary root with 50/200/500 synthetic entries and real files.
- Harnesses left in `/tmp/ticli-audit/` (`harness.py`, `serve.py`, `run.sh`,
  `expire.py`, `readahead.py`); they are throwaway, not proposed tests.
