# Incidents

Seven things that went wrong, and what each changed. This is the highest-value
file in `ai/` — every rule in WORKING-RULES.md that matters came from one of
these.

---

## 1. The IP block

**What happened.** A research agent, authorized to probe the live API, made 53
sequential `playbackinfopostpaywall` calls in 2.8 seconds while measuring
whether per-track file sizes could be summed for a playlist. TIDAL rate-limited
it, then escalated to `401 subStatus 4006 "Session does not have streaming
privileges"`. A token refresh did not clear it; roughly 60–90 seconds of
waiting did, while the rest of the API kept working. Later, with a second agent
fetching TIDAL doc pages from the same IP, the owner hit a full bot-detection
block page in his browser.

**Impact.** The owner's music stopped. He reported it mid-session:
*"Your testing is getting me blocked rn."*

**Cause, honestly.** The main thread's research brief explicitly authorized
live probing without specifying a rate. That was the error — not the agent's.

**What changed.**
- ≤1 request per 15 seconds during development, as a hard ceiling in every brief.
- Stop-don't-retry on any 429 / 4006 / block page.
- Build against fakes and read the installed library source by default.
- The rule proved cheap: subsequent agents shipped PKCE login, search overhaul,
  artwork, and the artist page having made **zero, zero, one, and zero** live
  requests respectively.

**Second-order effect on the product.** It killed a feature design. Summing
exact per-track sizes for a playlist requires one API call per track — the
exact pattern that caused the block. Download size estimates had to become
duration × bitrate arithmetic instead, which costs nothing.

---

## 2. FULL cache mode had never written a byte

**What happened.** Commit `c66e285` shipped cache-budget settings with a FULL
mode described as retaining audio for offline replay. It had never worked. Three
compounding faults:

1. The `ffmpeg -c copy` invocation wrote to a `.audio.part` path — no
   recognizable extension, so ffmpeg could not infer a muxer and errored out
   immediately, writing zero bytes.
2. The non-persistent path named an AAC stream `.flac` — wrong container for
   the codec, so ffplay's resume-from-cache was dead too.
3. The entire block sat inside the ffplay-only branch. On mpv — the default
   backend, and what the owner actually runs — it did nothing at all.

**Why the tests didn't catch it.** They asserted bookkeeping: that a flag was
set, that a fake process object existed, that the index recorded an entry. Five
tests stayed green for the entire life of an inert feature.

**Why the commit message said it worked.** The main thread wrote *"FULL retains
the FLAC ffplay already downloads"* with confidence, from the agent's report,
without any evidence that bytes reached disk. That was overstated and it was
the main thread's error.

**What changed.**
- Tests assert file contents now: byte-for-byte equality with the source, on
  both backends, extension derived from `Content-Type`, no junk after an
  abandoned download, `.part` never served as whole, eviction against real
  files, and one end-to-end test over a loopback HTTP server.
- The fix removed ffmpeg from the caching path entirely (a plain streaming
  `requests` GET), rather than repairing the ffmpeg call — fewer dependencies,
  backend-independent.
- Verified against the live CDN: bytes identical (`sha 51f0e8b8`), and a replay
  with a deliberately invalid URL still played, proving the second play never
  touched the network.

---

## 3. Playback failed silently across the whole library

**What happened.** After the owner switched to PKCE login, search worked, the
UI behaved normally, tracks appeared to play — and **no sound came out**.

Two stacked causes:

1. PKCE unlocks lossless, which TIDAL serves as an MPEG-DASH manifest of
   fragmented-MP4 segments rather than one contiguous file. The generated HLS
   playlist omitted `#EXT-X-MAP`, so the initialization segment was listed as
   ordinary audio. fMP4 fragments carry no `moov`, so every segment failed
   alone. mpv's actual words: `could not find corresponding trex`,
   `trun track id unknown, no tfhd was found`, `unrecognized file format`.
2. **Nothing surfaced it.** mpv ran `--really-quiet`, ffplay `-loglevel quiet`,
   both with `stderr=DEVNULL`. The monitor saw a dead process and advanced to
   the next track, which failed identically. The whole library, in silence,
   behind a normal-looking UI.

A third latent problem: even a correct playlist would have failed, because
ffmpeg's default protocol whitelist for a *file* input is `file,crypto,data`,
so every `https://` segment is refused.

**The deeper bug was #2, not #1.** A format problem is a bug; a format problem
that presents as silence is a bug you cannot diagnose.

**What changed.**
- Playlists are generated here now, correctly, at HLS version 7 with
  `#EXT-X-MAP` and real per-segment durations. Per-backend flags handle the
  protocol whitelist (mpv's key-value lists split on commas, so it needs the
  length-prefixed form).
- Errors are captured to a per-player log; `AudioPlayer.failure()` reads the
  backend's own last line when the process exited with a *positive* status
  (zero is end-of-track, negative is a signal — i.e. `stop()`/`pause()` working).
- A failed start now **stops with a toast showing the player's real error**
  rather than burning through the queue. There is a test asserting the queue is
  not advanced.
- Choice made on evidence: HLS over handing mpv the DASH manifest, because this
  ffmpeg has **no DASH demuxer compiled in** (`ffmpeg -demuxers` lists only `hls`).

---

## 4. A migration that was dead code

**What happened.** A config migration renamed saved quality values so that
correcting an off-by-one in the quality tiers wouldn't silently downgrade
existing users. It computed the corrected value into `cfg`, and then the loop
immediately below re-read the *raw file dict* — discarding it. A saved `"LOW"`
still loaded as `"LOW"`: the exact silent downgrade the migration existed to
prevent.

**How it was caught.** The next agent's migration tests failed on first run.
The tests were written to go through a written file and a save/reload
round-trip rather than calling `_migrate` directly — which is the only reason
they caught it.

**What changed.** Migration tests always exercise a real written file plus a
round-trip, never the migration function in isolation. Later migrations (v2→v3,
cache mode → two booleans) follow that pattern and also pop superseded keys so
a later save can't resurrect them.

---

## 5. The flaky test was a real race

**What happened.** `test_the_oldest_download_is_the_one_evicted` failed roughly
once in four runs. It looked like an `mtime`/`atime` precision problem.

**It wasn't.** Two cache sweeps could run concurrently — the download thread
triggers its own. The loser's `unlink()` raised, hit `except OSError: continue`
without decrementing the running total, and consequently **evicted one file too
many**. A real bug in eviction, surfaced as test noise.

**What changed.** `unlink(missing_ok=True)` in `enforce_budget`, the test
rewritten to place files with stated times and no threads, and a new test
pinning the race directly. Rule adopted: a flaky test is a bug until proven
otherwise.

---

## 6. The cache did not recognise the file it was writing

**What happened.** `AudioPlayer._start_download` writes the in-progress copy to
`{audio_dir}/{track_id}.part` — no extension, because the container is only
known once the CDN's first response header arrives, and the file is renamed to
`{track_id}{ext}` when it is whole. `cache.is_owned_audio()` stripped `.part`
and then *required* a recognised audio extension, so it expected
`{track_id}{ext}.part` and returned **False for every part file production has
ever written**.

**Impact.** A part file leaked by any exit that is not `stop()` — SIGKILL, a
crash, power loss — is counted against the budget (`total_bytes` is a plain
directory size and never asks the predicate) while being invisible to
`owned_audio_files`. So it could be neither evicted nor cleared. Measured with
the real modules, 1 GB budget, a 1.5 GB leak beside a 0.25 GB song:
`enforce_budget()` **deleted the song and kept the leak**, and "clear cached
songs" returned `(0 deleted, 0 kept)`. Past that point every sweep evicts every
real song and the cache can never hold anything again — while the settings page
says `0 songs cached · 1.500 GB`, honest about the bytes and silent about why.

**Why the tests didn't catch it.** They asserted on `12.m4a.part`, in four
places. **Production never creates that name.** The tests and the predicate
agreed with each other and both disagreed with the writer — which is INCIDENTS
#2 exactly: five green tests over an inert feature, because the assertion was
about a belief rather than about what was on disk.

**What changed.**
- The predicate widened: on a `.part` the extension is optional. The extended
  form is still accepted (a rename landing mid-sweep is still ours), and
  `audio_count()` still excludes `.part` by suffix.
- The four tests now use the name the writer writes, and a new one
  monkeypatches `fetch_to_file` to capture the path `_start_download` actually
  opens and asserts the predicate against **that** — pinning it to the writer,
  not to a name. Three more assert the leak scenario in bytes on disk.
- Rule reinforced, and it is the generalizable one: **a test for a filename
  must obtain the filename from the code that writes it.** Where a test cannot
  do that, the constant belongs in one place that both sides import.

---

## 7. Two songs at once, from a bug we had already written down

**What happened.** The owner: *"there was a primary song playing and another
song playing at that same time with the same client."* One ticli process, two
audible streams.

`play_url` killed the old backend and spawned the new one under **two separate
acquisitions** of `AudioPlayer._lock`: `self.stop()` — which takes the lock,
reaps, sets `_process = None`, and *releases* — followed by `with self._lock:`
around the `Popen`. Two track-starts arriving inside one reap each reached the
spawn: the loser's `stop()` ran after the winner had already cleared
`_process`, so its `if self._process and ... poll() is None` was false, it
reaped nothing, and both processes ended up playing.

**Why it was audible rather than merely untidy.** `_process` is a single slot
and the only handle ticli keeps. Everything that stops, pauses, seeks or shuts
down acts on it alone, and mpv's IPC socket is one fixed path per ticli pid
that the last spawn takes over. So the orphan answered nothing: space bar
silenced one stream and left the other going, the UI showed one track while two
were audible, and `q` exited without touching it — the process outlived the app.

**The part that stings.** This exact failure was traced and written down on
2026-07-24, in `BUGS-2026-07-24-resume-trace.md` item 4: *"tighter interleaving
double-spawns mpv → orphaned process, double audio."* It prescribed three
fixes. Two shipped — `_track_changing` and the two-consecutive-dead-polls rule
— and both close the **monitor's** door onto that window. The third, the
re-entrancy guard on the window itself, did not. Item 4 was then marked FIXED
at the top of that file, and the seam it was really about stayed open for two
weeks. **A multi-part fix marked done because its visible symptom stopped is
the failure mode here**; the file now records which third is outstanding.

**Why no test caught it.** The suite had 1,453 tests and not one could see a
leaked process. Every fake stood in for a player that cannot die:
`test_player_controls.py`'s `_FakeProc` defines only `poll()` — no `terminate`,
no `wait`, no `kill` — and `test_cache.py`'s `_Proc.poll()` returns `None`
forever with `terminate()` as `pass`, so a killed process and a leaked one are
indistinguishable. The assertions were about the spawned command line and about
`_process` pointing at the newest spawn — which is true with the bug fully
present. INCIDENTS #2 again, in the process domain: green tests over an
invariant nothing was checking.

**What changed.**
- `_stop_locked()` and `_play_url_locked()`: the reap and the respawn are one
  critical section. `resume()`'s hand-written `_lock.release()` / re-acquire
  around `play_url` — the same seam by another door — went with it.
- A regression file that asserts **process liveness, not bookkeeping**: every
  process ever spawned, except the one `_process` currently names, must have
  been terminated or killed. It runs against fakes that actually die, and once
  against real OS children, where the pre-fix failure reads *"pids still
  running that ticli can no longer stop: [12148]"*.
- The lesson that generalizes past locks: **when a test cannot distinguish the
  fixed code from the broken code, it is not a weak test, it is not a test.**
  The first version of the concurrency test here started two threads and hoped;
  it passed 25/25 against the unfixed code, because CPython locks are unfair
  and the releasing thread barges before the parked one wakes. It had to be
  made to fail on purpose before it was worth committing.

---

## Two hypotheses the main thread got wrong

Not incidents, but the same lesson from the other direction — both were caught
because briefs demanded measurement before fixing.

**"The artwork is raw ANSI, so Rich can't measure it."** Offered as the prime
hypothesis for stranded frames in the TUI. Wrong: artwork was already proper
Rich `Text` with per-cell `Style` objects. The real cause was that Rich's
`Live` repaints by *counting rows* — it walks the cursor up exactly as many
rows as the last frame was tall. That arithmetic holds only while the terminal
hasn't moved, and a width change makes terminals **reflow**: a 22-row frame
laid out for 100 columns becomes up to 44 rows at 60, Rich erases 22, and the
other 22 are stranded forever. Fix: the alternate screen, where every refresh
homes the cursor and writes every row, so there is no arithmetic to get wrong.

**"mpv's IPC quit is faster than SIGTERM."** Offered as a way to cut the audio
tail after quitting. Measured across 6 paired trials: `terminate()` 37–75ms,
IPC `quit` 39–78ms. Not faster, and it costs a socket connect that can itself
time out. Correctly rejected; the real cause of the lag was save-before-stop
ordering.

**The generalizable point.** Briefs that say "diagnose with measurements first,
and a refuted hypothesis is a valuable result" produce agents that push back on
the person briefing them. That is the desired behavior, and it caught both.
