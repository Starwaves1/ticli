# Single-fetch playback — feasibility, measured

**Date** 2026-07-26 · **Commit** `e14e41a` · **Scope** read-only.
Nothing under `src/` was changed. `player.py` was not touched (another agent
held it).

**Zero requests were made to TIDAL or its CDN.** Every number below comes from
a loopback HTTP server, the real cached track that was already in
`~/Library/Caches/ticli/audio`, and ticli's exact backend flags. Harnesses are
in `/tmp/ticli-sf/` (`tail.py`, `gap.py`, `overlap.py`, `serve.py`); they are
throwaway, not proposed tests.

**Test material.** `113621048.m4a` — FLAC 16/44.1 in fragmented MP4, 203.59 s,
19,515,420 B, 767 kbps, a 623-byte initialization segment plus **51** media
fragments. This is a real PKCE-session lossless track, i.e. the segmented path,
which is the one the owner actually listens on.

---

## Verdict

| shape | verdict |
|---|---|
| **1. Play the file while it is being written** (no switch) | **Impossible.** Both backends stop dead at the write frontier, and both exit **0**, which `_monitor_playback` reads as "the track ended". |
| **2. Switch playback to the local copy mid-track** | **Works on mpv at ~40 ms, and is not worth it.** Saves 16–27 % on the path that matters, saves *less* the worse the connection is, and needs an mpv-only code path whose failure mode is mpv exiting mid-song. |
| **3. Staging ("first 30 s, then the rest at 20 s")** | **Not load-bearing.** The whole-track download already finishes at t=10.6 s (20 Mbps) and t=23.1 s (10 Mbps). Staging only delays it. |
| **4. Scratch tier for the currently-playing song** | **Yes — do it, but for a different reason than the proposal gives.** It is the natural home for the admission decision and it fixes audit finding F8. |
| **the actual recommendation** | **Byte-prefetch the *next* track** (audit's F-section conclusion). One fetch per track in steady state — a full 50 % — with **no switch, no gap, and no backend-specific code**. |

The honest headline: **the double fetch is worth removing, but not by
switching sources mid-song.** Removing it one track *earlier* — before the
track starts — costs nothing and saves twice as much.

---

## 1. Can playback read the file while it is being written?

**No, on both backends, and it fails silently.** `tail.py` copies the track
into `grow.part` at 1.3× its real bitrate and starts a backend against the
growing file after 1 s of head start:

```
mpv     rate=1.3x prewarm=1.0s  exit=0  played 2.7 s of 204 s   stderr: (empty)
ffplay  rate=1.3x prewarm=1.0s  exit=0  played 2.7 s of 204 s
        stderr: stream 0, offset 0x63e75: partial file
                [flac] invalid residual | [flac] decode_frame() failed
```

Control, same harness, complete file: mpv 15.3 s wall for `--length=15`,
ffplay 15.4 s for `-t 15`, both exit 0. The harness is sound; the backends
genuinely die at the frontier.

The cause is not a missing flag. A `read()` on a regular file at EOF returns
0 bytes rather than blocking, and neither ffmpeg's `file:` protocol nor mpv's
stream layer retries — there is no tail-follow option in either. mpv's
`--cache=yes --cache-secs=60` makes it *worse*, not better: it reads ahead as
fast as the writer produces and therefore reaches EOF sooner.

**And the failure is invisible.** Both exit **0**. `AudioPlayer.failure()`
returns None for exit 0 by its own contract, so `_monitor_playback` falls
through to the auto-advance branch. This is INCIDENT #3's exact failure mode:
a track truncated at 2.7 s, no error, queue walks on. Any design that reads a
partial file has to carry a detector for this — which is audit finding F2, and
F2 is not fixed yet.

**Things that do not rescue it:**

- **`.part` → final rename under an open handle is fine.** Tested on both
  backends, `os.replace` at t=4 s of a 12 s play: mpv exit 0 / 12.3 s wall,
  ffplay exit 0 / 12.4 s wall, no stderr. POSIX keeps the inode, so the
  project's existing reliance on this holds here too. It just does not matter,
  because EOF kills the playback long before the rename.
- **Pre-allocating a sparse file** is worse: the unwritten region reads as
  zeros, so instead of stopping, the decoder decodes silence and garbage.
- **A FIFO** blocks instead of returning EOF, and is the only construct that
  would work — but it is unseekable (no scrubbing, no resume, no `--start`),
  it needs a writer process feeding it, and it is the local-proxy shape the
  audit already rejected for this codebase.

**Shape 1 is refuted.**

---

## 2. If a switch is needed, how audible is it?

### mpv — ~40 ms, and one landmine

`gap.py` streams the HLS playlist with ticli's exact flags, then at t≈8 s
sends `loadfile <local file> replace` over IPC and polls `time-pos` every
10 ms. Silence = wall time from the command to the first sample where the new
source is advancing:

```
run 1: switched at 8.05 s — 36 ms
run 2: switched at 8.05 s — 46 ms
run 3: switched at 8.05 s — 37 ms
```

For comparison, the project's existing numbers: mpv `terminate()` 37–75 ms,
IPC quit 39–78 ms. A `loadfile` switch is at the fast end of what this
codebase already does.

**The landmine.** On a segmented track mpv is spawned with
`--demuxer=lavf --demuxer-lavf-format=hls`, and those are *global* options.
A `loadfile` of the plain fMP4 cache file inherits the forced HLS demuxer and
fails:

```
loadfile {"start": "8.0"}                              -> mpv EXITS, code 3
     log: [lavf] avformat_open_input() failed | Failed to recognize file format.
loadfile {"start": "8.0", "demuxer": "auto", ...}      -> mpv EXITS, code 3
     log: Demuxer auto does not exist.
loadfile {"start": "8.0", "demuxer-lavf-format": ""}   -> works, playing at 9.45 s
```

The IPC reply is `"error": "success"` in **all three** cases — the command was
accepted; the file failed to open afterwards. So a wrong incantation is not a
failed switch, it is **mpv exiting 3 in the middle of a song**. (Exit 3 is
positive, so `failure()` would at least surface it — but the song is over.)

**The splice itself is content-exact.** mpv's `start=` was checked against a
reference PCM decode of the whole track by cross-correlation:

```
start=8.05    began at sample 355005,  wanted 355005   error +0.00 ms  byte-exact
start=20.317  began at sample 895979,  wanted 895980   error -0.02 ms  byte-exact
start=60.123  began at sample 2651424, wanted 2651424  error +0.00 ms  byte-exact
```

So the seek is sample-accurate on FLAC-in-MP4 — no keyframe rounding, no
repeated or clipped audio *from the seek*. The remaining discontinuity is the
~40 ms between reading `time-pos` and the new source starting, which would
replay ~40 ms of audio; it is compensable by adding the measured latency to
the requested start, with a residual of ±5 ms.

**Not measured, and it should be before anyone ships this:** whether
`loadfile replace` flushes mpv's audio-output buffer. If it does, the audible
hole is up to `--audio-buffer` (default 0.2 s) rather than the 40 ms
`time-pos` says. Measuring it needs a real audio capture device, which this
machine does not have. Treat 40 ms as a lower bound.

### ffplay — ~0.37 s, and there is no better option

ffplay has no runtime control, so a switch is kill + respawn with `-ss`, the
same thing `seek_to()` already does. The kill is cheap and the startup is not:

```
terminate() + wait        4.6 ms
Popen spawn               2.8 ms
ffplay wall - audio, 2 s clip   0.375 s   (median of 4)
ffplay wall - audio, 6 s clip   0.369 s   (median of 4)
```

Constant overhead of **~0.37 s** — spawn to first audio plus `-autoexit`
teardown. A mid-track switch on ffplay is therefore a third of a second of
silence, unprompted, in the middle of a song. The project already pays this
for a *user-initiated* scrub, which is a different bargain entirely.

---

## 3. What the switch actually saves — the number that decides it

`overlap.py` runs the backend and a `fetch_to_file`-shaped whole-track
download against one loopback server with a **shared** global rate limit,
which is what one real connection is. Player bytes and download bytes are
tagged separately.

**mpv, segmented, ticli's flags. Track is 19.52 MB; today's double fetch is
39.04 MB.**

| link | download completes | player had already pulled | total if we switch then | saved vs today |
|---|---|---|---|---|
| 20 Mbps (2.5 MB/s) | **10.6 s** | **9.01 MB (46 %)** | 28.5 MB | **27 %** |
| 10 Mbps (1.25 MB/s) | 23.1 s | 10.32 MB (53 %) | 29.8 MB | 24 % |
| 5 Mbps (0.625 MB/s) | ~52 s | ~13.4 MB (69 %) | ~32.9 MB | 16 % |

Three things fall out of that table:

1. **It saves a quarter, not a half.** mpv's `--cache-secs=60` — the fix from
   `c71de29` — means the player has already bought ~9 MB of a 19.5 MB track
   within the first ten seconds. Those bytes are spent before any switch can
   happen. Better buffering and less bandwidth are in direct opposition here,
   and buffering already won that argument on smoothness grounds.
2. **It saves less the worse the connection is.** At 5 Mbps the download is
   competing with the player for the same pipe, so it lands later, by which
   time the player has spent more. The user who most needs the bandwidth back
   gets the least of it. That is backwards.
3. **The 20 s trigger cannot be a fixed number.** At 20 and 10 Mbps the whole
   track is already on disk *before* 20 s; at 5 Mbps it is not there until
   ~52 s. The only correct trigger is "the download landed", which is exactly
   where `_start_download`'s `os.replace` already is. **The staging the
   proposal describes buys nothing** — fetching "the first 30 s" first only
   pushes completion later.

**ffplay is the mirror image and it does not help.** Same run at 20 Mbps:
download completes at 7.7 s with the player having pulled only **1.76 MB
(9 %)**, because ffplay stops reading once its packet queues are full (audit
F5). A switch there would save 45 %. But (a) it costs the 0.37 s hole, and
(b) audit F5 recommends adding `-infbuf`, which pulls the whole track in under
a second and would erase this saving completely. **F5 and the mid-track switch
are mutually exclusive; F5 is the better of the two** — it fixes an audible
hitch, and the switch only saves bytes.

---

## 4. The recommendation: prefetch the bytes, not the switch

The audit's last alternative is the right one and this work confirms why.
`_maybe_prefetch_next` already fires `PREFETCH_LEAD = 20` s before the end of a
track and already resolves the next track and its stream URL. If it also
fetched that track's **bytes**, then when the track starts it is a cache hit,
plays from disk, and is never fetched a second time.

- **Steady state is one fetch per track — the full 50 %.**
- **There is no switch, therefore no gap, therefore nothing to measure.**
- It is backend-independent: mpv and ffplay both just open a local file, which
  is a path that already works today for a replayed track.
- The failure mode is today's behaviour. A prefetch that did not finish in
  time means the track streams and double-fetches exactly as now.

**The one number it needs:** how long a whole-track fetch takes against
`PREFETCH_LEAD = 20`.

| link | whole-track fetch | vs 20 s lead |
|---|---|---|
| 20 Mbps | 10.6 s | comfortable |
| 10 Mbps | 23.1 s | marginal |
| 5 Mbps | ~52 s | far short |

These are measured *with* the current double fetch competing for the pipe. In
steady state the prefetch is the only other consumer, so they are pessimistic.
Still, `PREFETCH_LEAD` should grow for the byte prefetch — 45–60 s, or a
separate `BYTE_PREFETCH_LEAD` — and a prefetch that has not finished when the
track starts must be *kept running* rather than abandoned, so the next play of
that track is a hit even when this one was not.

---

## 5. The scratch tier — do it, and it is better than the proposal thinks

This is the cleanest piece and it is worth doing on its own. But its value is
not "a copy of a song we are not keeping"; it is **where the admission
decision gets to be made late**.

### Where it lives

`CACHE_DIR/scratch/` — a sibling of `audio/` and `artwork/`, exactly the
precedent `artwork_dir()` set, and for the same reason:

- `MetadataCache.total_bytes()` is `_dir_size(audio_dir())` plus the index
  file. Anything in `audio_dir()` counts against the budget **whatever it is
  called** — so a scratch file that must not count cannot live there. This is
  not a naming problem, it is a directory problem.
- `owned_audio_files()` / `audio_count()` / `enforce_budget()` /
  `clear_audio()` all work from `audio_dir()`, so a separate directory is
  automatically exempt from eviction and from the song count, by construction
  rather than by a special case.

### How it is named

`CACHE_DIR/scratch/{track_id}-{pid}.part`, becoming `{track_id}-{pid}{ext}`
when whole.

- The `{track_id}` fixes **audit finding F8**: today the ffplay no-cache
  scratch is `ticli-cache-{pid}.part`, one name for every track in the
  process, and a fast skip can have two download threads writing the same
  file. That race disappears here for free.
- The `{pid}` is what makes crash cleanup possible.
- It needs its **own** predicate (`is_owned_scratch`), *not* a widening of
  `is_owned_audio` — that predicate's job is "what may `clear_audio` and
  `enforce_budget` delete", and the answer for scratch must stay no.

### Promotion is a rename

Because `scratch/` and `audio/` are both under `CACHE_DIR`, promoting a
scratch file into the cache is one `os.replace` on the same filesystem — no
copy, no second write, and safe under an open handle on POSIX (verified in
§1). Putting scratch in `tempfile.gettempdir()` instead would break this on
any Linux with `/tmp` on tmpfs (cross-device rename → `EXDEV`).

### Crash orphans

An orphan in `scratch/` costs disk and nothing else — it is outside
`total_bytes()`, outside `audio_count()`, outside eviction. That already
avoids the F1 disaster (where a leaked `.part` in `audio/` counts against the
budget forever and cannot be evicted or cleared).

Cleanup, in order of cost: (1) `stop()` and normal track end unlink it;
(2) at startup, sweep `scratch/` for files whose `{pid}` is not a live process
— one `os.kill(pid, 0)` per file, on the existing startup path, no timer;
(3) `clear_audio()` should empty `scratch/` too and say so, since "clear
cached songs" that leaves a gigabyte behind is dishonest.

### What it composes with

Scratch + byte-prefetch is the whole design, and it answers question 3
(the "worth caching" test) better than the proposal does:

1. `_maybe_prefetch_next` fetches the next track's bytes into
   `scratch/{id}-{pid}.part`.
2. When that track starts, it plays from the scratch file. **One fetch. No
   switch. No gap.**
3. When the track *ends*, the admission test runs and either `os.replace`s the
   file into `audio/` or unlinks it.

Deciding at the end rather than at 20 s is strictly better: at 20 s you know
almost nothing, and at the end you know whether the user listened to the whole
thing — which is one of the value-function candidates Garrett named. It also
means admission never has to guess.

**The hook, defaulting to today's behaviour** (per DECISIONS, the value
function is not settled and must not be invented here):

```python
def _should_keep(self, track, played_seconds) -> bool:
    """Whether a finished track earns a place in the cache.

    Admission only bites when the cache is full (DECISIONS, 2026-07-26);
    with room to spare everything is kept, which is today's behaviour and
    what this returns until the value function is decided.
    """
    return True
```

One function, one call site, and the day the rule is settled it is the only
thing that changes.

---

## 6. Ordering and dependencies

- **The byte prefetch has the same dependency the audit named: it must land
  after F1.** `is_owned_audio` still does not recognise the bare
  `{track_id}.part` that `_start_download` writes (verified on `e14e41a` —
  `cache.py:62-66` is unchanged). Doubling the number of downloads before that
  is fixed doubles the rate at which leaked `.part` files silently poison the
  budget.
- **The scratch tier does not depend on F1** — it is a different directory
  with its own predicate — but it should land *with or before* the byte
  prefetch, because the prefetch wants somewhere to put bytes it may not keep.
- **F2 should land regardless.** §1 showed that a truncated read exits 0 on
  both backends with an empty stderr on mpv. Any work in this area makes that
  detector more valuable, and it is currently the only thing standing between
  a short read and a silently skipped song.
- **F5 (`-infbuf` for ffplay) and the mid-track switch cannot both happen.**
  Take F5.

---

## 7. Implementation plan (for whoever executes it)

Ordered, each step independently shippable and testable.

1. **F1 first** (already assigned): `is_owned_audio` accepts `{digits}.part`.
2. **`cache.scratch_dir()`** — `CACHE_DIR / "scratch"`, 0o700, alongside
   `audio_dir()` and `artwork_dir()`. Plus `is_owned_scratch(name)`
   (`{digits}-{digits}` stem, audio extension, optional `.part`),
   `owned_scratch_files()`, `clear_scratch()`, and a startup
   `sweep_dead_scratch()` that unlinks files whose pid is not alive.
   *Tests:* a decoy `important.txt` in `scratch/` survives everything;
   `total_bytes()`, `audio_count()` and `enforce_budget()` are unmoved by a
   1 GB file in `scratch/`; a file tagged with a live pid survives the sweep
   and one tagged with a dead pid does not.
3. **Repoint the existing ffplay scratch** at
   `scratch_dir()/{track_id}-{pid}.part`, replacing
   `tempfile.gettempdir()/ticli-cache-{pid}` — this alone closes F8.
   *Test:* two overlapping `_start_download`s for different tracks write two
   different files and neither is interleaved.
4. **Byte prefetch.** In `_maybe_prefetch_next`, once the URL has been
   resolved on its existing daemon thread, also `fetch_to_file` the track into
   `scratch/`. It needs:
   - its **own generation counter** (`_byte_prefetch_gen`) — the shared
     `_download_gen` is bumped by every `stop()`, so the track change the
     prefetch exists for would cancel it;
   - single-flight, one track at a time, never when `cache_songs` is off;
   - a longer lead than `PREFETCH_LEAD = 20` (measured: 10.6 s at 20 Mbps,
     23.1 s at 10, ~52 s at 5) — a separate `BYTE_PREFETCH_LEAD` of 45–60 s;
   - **not** abandoning an unfinished prefetch at track start: let it land in
     `scratch/` and be promoted, so the *next* play is a hit.
   *Tests:* playing a two-track queue over a loopback server fetches each
   track's bytes exactly once (count requests server-side — the project
   already asserts request counts); skipping past a track leaves at most one
   prefetch running; `cache_songs` off means zero prefetch bytes.
5. **Promotion + the admission hook.** `play_url` prefers a whole scratch file
   for this track id, the same way it already prefers `local` then the cache.
   At track end (`_monitor_playback`'s advance branch) call `_should_keep`;
   `os.replace` into `audio_dir()/{track_id}{ext}` on true, unlink on false;
   then `invalidate_audio_count()` + `enforce_budget()` as today.
   *Tests:* the file really moves (bytes on disk in `audio/`, gone from
   `scratch/`); a `_should_keep` stubbed False leaves `audio/` empty and
   `scratch/` clean; killing the process mid-track leaves a file the next
   startup sweep removes.
6. **Then, separately:** F5's `-infbuf`, and F3 (don't spend a `playbackinfo`
   request on a track that is already whole on disk) — F3 gets *more*
   valuable once prefetch means most tracks are cache hits.

**Do not build:** the mid-track `loadfile` switch, the staged
"first 30 s then the rest", or anything that reads a `.part` while it is being
written.

---

## Method

- **Tail-follow:** `/tmp/ticli-sf/tail.py` — a throttled writer at 1.3× the
  track's real bitrate into `grow.part`, backend started 1 s in with ticli's
  flags at `--volume=0` / `-volume 0`; controls on the complete file; a
  separate run doing `os.replace` at t=4 s of a 12 s play.
- **Switch gap:** `/tmp/ticli-sf/gap.py` — mpv over a unix IPC socket, HLS
  playlist of the real track's 51 fragments served from loopback with ticli's
  exact `_hls_flags()`, `time-pos` polled at 10 ms, 3 runs. ffplay overhead
  from wall-time-minus-audio on 2 s and 6 s `-c copy` clips, 4 runs each.
- **Seek accuracy:** `ffmpeg -f s16le` reference decode of the whole track vs
  `mpv --start=T --length=1 --ao=pcm`, located by cross-correlation and then
  compared byte-for-byte.
- **Bandwidth:** `/tmp/ticli-sf/overlap.py` + `serve.py` — one loopback server
  with a shared global byte-rate limit, serving the real track as 51 fMP4
  segments behind an `#EXT-X-MAP` playlist; player requests and download
  requests tagged separately and logged with timestamps.
